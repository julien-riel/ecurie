"""`ecurie pull` — mettre les poids là où `weights.py` les attend.

`weights.py` dit où les poids d'un variant *devraient* être ; ce module est ce
qui les y met — et surtout ce qui refuse de le faire quand les conditions ne
sont pas réunies. Trois refus, tous chiffrés dans leur message :

1. **révision non épinglée** : sans sha complet, la révision exécutée cesse
   d'être celle du manifeste et le profil mesuré devient caduc sans préavis
   (CONCEPTION.md §10.3). `main`, le placeholder tout-zéros et les sha courts
   sont refusés ;
2. **garde de disque** : un téléchargement qui ferait passer la part libre du
   volume sous `config.free_disk_ratio` est refusé *avant* d'écrire un octet —
   pas à mi-parcours, quand il ne reste déjà plus la place de faire le ménage ;
3. **source non gérée** : seul `kind: huggingface` se télécharge ici.

Le registre n'est jamais modifié. `resolve_revision` va chercher le sha de
`main` et `revision_patch` affiche le patch à committer : toute évolution du
parc passe par Git (ARCHITECTURE.md §3).

Le réseau est injectable de bout en bout (`api=`, `download=`, `disk_usage=`) :
les tests de ce module n'ouvrent aucune connexion, et ça se lit dans leurs
fixtures plutôt que dans une promesse.
"""

import os
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ecurie_core.config import Config
from ecurie_core.models import Source, Variant
from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.errors import HfHubHTTPError
from huggingface_hub.utils import filter_repo_objects

from ecurie_store.figures import fmt_bytes
from ecurie_store.weights import (
    WeightsMissing,
    hf_hub_dir,
    resolve_source,
    resolve_weights,
    snapshot_dir,
)

# Le cache HF nomme son dossier d'instantané d'après le sha complet du commit.
# Un sha court y serait introuvable : `resolve_weights` chercherait
# `snapshots/41d3337`, le téléchargement aurait écrit `snapshots/41d3337e8b7f…`.
REVISION_LENGTH = 40


class PullError(RuntimeError):
    """Refus argumenté. Le message porte les chiffres et la commande qui répare."""


@dataclass(frozen=True)
class RepoFileInfo:
    """Un fichier du dépôt distant, tel que l'API HF l'annonce."""

    path: str
    size: int


@dataclass
class RevisionInfo:
    """Ce qu'il faut pour épingler : le sha, et de quoi juger s'il faut le faire."""

    repo: str
    sha: str
    last_modified: str | None = None
    license: str | None = None
    gated: bool = False


@dataclass
class DiskGuard:
    """La garde des 15 % de disque libre, avec les quatre nombres qui la justifient."""

    volume: Path
    total_bytes: int
    free_bytes: int
    needed_bytes: int
    min_ratio: float

    @property
    def free_after(self) -> int:
        return self.free_bytes - self.needed_bytes

    @property
    def ratio_before(self) -> float:
        return self.free_bytes / self.total_bytes if self.total_bytes else 0.0

    @property
    def ratio_after(self) -> float:
        # Un volume de taille nulle ne s'interprète pas : refuser vaut mieux que
        # diviser par zéro, et mieux encore que laisser passer par défaut.
        return self.free_after / self.total_bytes if self.total_bytes else 0.0

    @property
    def ok(self) -> bool:
        return self.ratio_after >= self.min_ratio

    @property
    def message(self) -> str:
        return (
            f"{fmt_bytes(self.needed_bytes)} à écrire sur {self.volume} — "
            f"libre {fmt_bytes(self.free_bytes)} ({self.ratio_before:.1%}) "
            f"→ {fmt_bytes(self.free_after)} ({self.ratio_after:.1%}), "
            f"seuil {self.min_ratio:.1%}"
        )


@dataclass
class Presence:
    """Ce que le disque a déjà, à la révision épinglée et à elle seule."""

    path: Path | None
    present: bool  # l'instantané de la révision existe
    complete: bool  # ... et tous les fichiers attendus s'y trouvent
    missing: list[str] = field(default_factory=list)
    bytes_on_disk: int = 0
    reason: str | None = None  # ce que dit `WeightsMissing` quand ce n'est pas là


@dataclass
class PullPlan:
    """Tout ce qu'un `pull` ferait, chiffré, avant qu'il ne fasse quoi que ce soit."""

    ref: str
    repo: str
    revision: str
    allow_patterns: list[str] | None
    hub_dir: Path
    snapshot_dir: Path
    selected: list[RepoFileInfo]
    excluded: list[RepoFileInfo]
    missing: list[RepoFileInfo]
    presence: Presence
    guard: DiskGuard
    # `None` pour les poids, le rôle de la source secondaire sinon. Ce qui
    # s'affiche vient de là : « qwen2-tokenizer » à côté d'un dépôt de 11 Mo dit
    # tout de suite pourquoi un second téléchargement part.
    role: str | None = None

    @property
    def selected_bytes(self) -> int:
        """Poids du variant à cette révision, une fois les `allow_patterns` appliqués."""
        return sum(f.size for f in self.selected)

    @property
    def excluded_bytes(self) -> int:
        """Ce que les `allow_patterns` épargnent au disque."""
        return sum(f.size for f in self.excluded)

    @property
    def download_bytes(self) -> int:
        """Ce qu'il reste réellement à télécharger : un pull interrompu reprend."""
        return sum(f.size for f in self.missing)


@dataclass
class PullResult:
    ref: str
    plan: PullPlan
    downloaded: bool
    dry_run: bool
    path: Path | None
    bytes_downloaded: int  # mesuré sur le disque après coup, pas l'estimation
    note: str


# --- révisions ---------------------------------------------------------------------


def is_pinned(revision: str | None) -> bool:
    """Un sha de commit complet, et rien d'autre — surtout pas `main`."""
    if not revision or len(revision) != REVISION_LENGTH or set(revision) == {"0"}:
        return False
    return _est_hex(revision)


def check_revision(ref: str, revision: str | None) -> str:
    """Renvoie la révision épinglée, ou refuse en disant laquelle des règles casse."""
    répare = f"épingler d'abord — `ecurie pull {ref} --resolve` donne le sha de main"
    if not revision:
        raise PullError(f"{ref} : aucune révision dans le manifeste — {répare}")
    if set(revision) == {"0"}:
        raise PullError(f"{ref} : révision placeholder {revision!r} — {répare}")
    if not _est_hex(revision):
        # `main` suit la branche : deux pulls à un mois d'écart ne donneraient pas
        # les mêmes poids, et le profil mesuré ne dirait plus rien du modèle servi.
        raise PullError(f"{ref} : révision flottante {revision!r} — {répare}")
    if len(revision) != REVISION_LENGTH:
        raise PullError(
            f"{ref} : sha court {revision!r} ({len(revision)} caractères) — le cache HF "
            f"nomme son instantané d'après le sha complet, celui-ci resterait introuvable "
            f"après téléchargement ; {répare}"
        )
    return revision


def resolve_revision(repo: str, *, revision: str = "main", api: Any = None) -> RevisionInfo:
    """Sha, licence et date d'un dépôt HF — de quoi épingler en connaissance de cause.

    Sert aussi à vérifier qu'une révision déjà épinglée existe encore (le job
    hebdomadaire de veille, CONCEPTION.md §10) : passer le sha en `revision`.
    """
    api = api or HfApi()
    try:
        info = api.model_info(repo, revision=revision)
    except HfHubHTTPError as exc:
        raise PullError(_erreur_hf(repo, revision, exc)) from exc
    except OSError as exc:
        raise PullError(_erreur_reseau(repo, exc)) from exc
    return RevisionInfo(
        repo=repo,
        sha=getattr(info, "sha", "") or "",
        last_modified=_iso(getattr(info, "last_modified", None)),
        license=_licence(info),
        gated=bool(getattr(info, "gated", False)),
    )


def revision_patch(ref: str, info: RevisionInfo) -> str:
    """Le patch à committer — `pull` ne touche jamais au registre lui-même."""
    model_id, _, variant_id = ref.partition("@")
    lignes = [
        f"# registry/models/{model_id}.yaml",
        "variants:",
        f"  - id: {variant_id}",
        "    source:",
        f"      repo: {info.repo}",
        f'      revision: "{info.sha}"',
    ]
    daté = info.last_modified or "date inconnue"
    lignes.append(f"    # {info.repo} @ {daté}, licence {info.license or 'non déclarée'}")
    if info.gated:
        lignes.append("    # dépôt sous conditions (gated) : accès à accorder avant le pull")
    return "\n".join(lignes) + "\n"


# --- inventaire distant -------------------------------------------------------------


def list_repo_files(repo: str, revision: str, *, api: Any = None) -> list[RepoFileInfo]:
    """Les fichiers du dépôt à cette révision, avec leurs tailles annoncées."""
    api = api or HfApi()
    try:
        entrées = api.list_repo_tree(repo, revision=revision, recursive=True)
        return _fichiers(entrées)
    except HfHubHTTPError as exc:
        raise PullError(_erreur_hf(repo, revision, exc)) from exc
    except OSError as exc:
        raise PullError(_erreur_reseau(repo, exc)) from exc


def select_files(
    files: Iterable[RepoFileInfo], allow_patterns: Sequence[str] | None
) -> tuple[list[RepoFileInfo], list[RepoFileInfo]]:
    """Partage l'inventaire en (retenus, écartés) selon les `allow_patterns`.

    Le filtre est celui de `huggingface_hub`, pas une réimplémentation : la garde
    de disque doit chiffrer exactement ce que `snapshot_download` écrira, sinon
    elle protège un volume contre un téléchargement qui n'aura pas lieu. À noter
    que le `*` de fnmatch traverse les `/` — `*.json` prend aussi les fichiers des
    sous-dossiers, ce qui surprend quiconque écrit ces patterns de mémoire.
    """
    fichiers = list(files)
    if allow_patterns is None:
        return fichiers, []
    retenus = list(
        filter_repo_objects(fichiers, allow_patterns=list(allow_patterns), key=lambda f: f.path)
    )
    gardés = {f.path for f in retenus}
    return retenus, [f for f in fichiers if f.path not in gardés]


# --- ce que le disque a déjà ---------------------------------------------------------


def variant_presence(
    config: Config,
    variant: Variant,
    *,
    ref: str,
    expected: Sequence[str] | None = None,
    source: Source | None = None,
) -> Presence:
    """Le variant est-il déjà là, à la révision épinglée ? Sans réseau.

    `expected` est la liste des fichiers attendus (chemins relatifs à
    l'instantané) : sans elle, on ne peut rien dire de mieux que « le dossier de
    la révision existe » — un instantané interrompu à mi-course en a un aussi.

    `source` désigne le dépôt à examiner quand le variant en a plusieurs. Par
    défaut, celui des poids.
    """
    try:
        location = (
            resolve_weights(config, variant, ref=ref)
            if source is None
            else resolve_source(config, source, ref=ref)
        )
    except WeightsMissing as exc:
        return Presence(path=None, present=False, complete=False, reason=str(exc))

    if expected is None:
        tout = [str(p.relative_to(location.path)) for p in location.path.rglob("*")]
        return Presence(
            path=location.path,
            present=True,
            complete=True,
            bytes_on_disk=_octets(location.path, tout),
        )

    # `exists()` suit le lien symbolique : un instantané dont le blob a été mis en
    # quarantaine par le GC a bien ses entrées, mais elles ne mènent nulle part.
    absents = {nom for nom in expected if not (location.path / nom).exists()}
    return Presence(
        path=location.path,
        present=True,
        complete=not absents,
        missing=sorted(absents),
        bytes_on_disk=_octets(location.path, [n for n in expected if n not in absents]),
    )


# --- garde de disque -----------------------------------------------------------------


def disk_guard(
    path: Path,
    needed_bytes: int,
    *,
    min_ratio: float,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> DiskGuard:
    """Mesure la marge du volume qui accueillerait `needed_bytes`.

    Le volume se mesure sur le premier parent qui existe : au tout premier pull,
    le dossier du cache HF n'est pas encore créé.
    """
    volume = _volume(path)
    usage = disk_usage(volume)
    return DiskGuard(
        volume=volume,
        total_bytes=int(usage.total),
        free_bytes=int(usage.free),
        needed_bytes=needed_bytes,
        min_ratio=min_ratio,
    )


# --- plan et exécution ----------------------------------------------------------------


def plan_pulls(
    config: Config,
    variant: Variant,
    *,
    ref: str,
    api: Any = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> list[PullPlan]:
    """Un plan par dépôt du variant, les poids d'abord.

    La plupart des variants n'en ont qu'un et cette liste en compte un. Ceux qui
    en déclarent d'autres — un tokenizer publié à part, un encodeur visuel — les
    obtiennent ici : un `pull` qui n'en ramènerait qu'un laisserait un variant
    marqué présent et incapable de charger.
    """
    return [
        plan_pull(config, variant, ref=ref, api=api, disk_usage=disk_usage, source=source)
        for source in variant.sources
    ]


def plan_pull(
    config: Config,
    variant: Variant,
    *,
    ref: str,
    api: Any = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    source: Source | None = None,
) -> PullPlan:
    """Tout vérifier, tout chiffrer, ne rien écrire. C'est le `--dry-run`.

    Interroge l'API HF pour l'inventaire de la révision : c'est ce qui rend les
    chiffres exacts (taille demandée, reste à télécharger). Pour savoir si un
    variant est là sans toucher au réseau, `variant_presence` suffit.

    `source` vise un dépôt précis quand le variant en déclare plusieurs ; par
    défaut, celui des poids.
    """
    source = source or variant.source
    if source.kind != "huggingface":
        raise PullError(
            f"{ref} : source {source.kind!r} — `pull` ne télécharge que depuis Hugging Face "
            "(une source local pointe déjà sur des fichiers, voir CONCEPTION.md §13.1)"
        )
    if not source.repo:
        raise PullError(f"{ref} : source huggingface sans dépôt dans le manifeste")
    revision = check_revision(ref, source.revision)

    fichiers = list_repo_files(source.repo, revision, api=api)
    retenus, écartés = select_files(fichiers, source.allow_patterns)
    if not retenus:
        raise PullError(
            f"{ref} : les allow_patterns {source.allow_patterns} n'ont retenu aucun des "
            f"{len(fichiers)} fichiers de {source.repo}@{revision[:12]} — patterns à corriger"
        )

    presence = variant_presence(
        config, variant, ref=ref, expected=[f.path for f in retenus], source=source
    )
    absents = set(presence.missing)
    manquants = [f for f in retenus if f.path in absents] if presence.present else retenus
    hub = hf_hub_dir(config)

    return PullPlan(
        ref=ref,
        role=source.role,
        repo=source.repo,
        revision=revision,
        allow_patterns=list(source.allow_patterns) if source.allow_patterns else None,
        hub_dir=hub,
        snapshot_dir=snapshot_dir(config, source.repo, revision),
        selected=retenus,
        excluded=écartés,
        missing=manquants,
        presence=presence,
        guard=disk_guard(
            hub,
            sum(f.size for f in manquants),
            min_ratio=config.free_disk_ratio,
            disk_usage=disk_usage,
        ),
    )


def run_pulls(
    config: Config,
    variant: Variant,
    *,
    ref: str,
    plans: Sequence[PullPlan] | None = None,
    **kwargs: Any,
) -> list[PullResult]:
    """Télécharge tous les dépôts du variant, les poids d'abord.

    Un seul appel pour un variant ordinaire. L'ordre compte quand il y en a
    plusieurs : les poids en premier, parce que c'est le seul téléchargement que
    la garde de disque peut refuser utilement — refuser un tokenizer de 11 Mo
    après avoir écrit 3 Go n'aiderait personne.
    """
    sources = variant.sources
    plans = list(plans) if plans is not None else [None] * len(sources)
    return [
        run_pull(config, variant, ref=ref, plan=plan, source=source, **kwargs)
        for source, plan in zip(sources, plans, strict=True)
    ]


def run_pull(
    config: Config,
    variant: Variant,
    *,
    ref: str,
    plan: PullPlan | None = None,
    source: Source | None = None,
    dry_run: bool = False,
    force: bool = False,
    ignore_disk_guard: bool = False,
    api: Any = None,
    download: Callable[..., Any] = snapshot_download,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> PullResult:
    """Télécharge le variant à sa révision épinglée, ou dit pourquoi il ne le fait pas.

    `force` retélécharge un variant déjà complet ; `ignore_disk_guard` passe outre
    la garde des 15 % — les deux sont des décisions de l'appelant, jamais des
    valeurs par défaut.

    `source` désigne le dépôt à ramener quand le variant en déclare plusieurs.
    """
    source = source or variant.source
    plan = plan or plan_pull(
        config, variant, ref=ref, api=api, disk_usage=disk_usage, source=source
    )

    if plan.presence.complete and not force:
        return PullResult(
            ref=ref,
            plan=plan,
            downloaded=False,
            dry_run=dry_run,
            path=plan.presence.path,
            bytes_downloaded=0,
            note=(
                f"déjà complet à la révision {plan.revision[:12]} "
                f"({fmt_bytes(plan.presence.bytes_on_disk)} sur le disque)"
            ),
        )

    if not plan.guard.ok and not ignore_disk_guard:
        raise PullError(
            f"{ref} : refus, la garde de disque ne passe pas — {plan.guard.message}. "
            "Faire de la place (`ecurie store plan`), déporter un variant "
            "(`ecurie store tier`), ou passer outre en connaissance de cause."
        )

    if dry_run:
        return PullResult(
            ref=ref,
            plan=plan,
            downloaded=False,
            dry_run=True,
            path=plan.snapshot_dir,
            bytes_downloaded=0,
            note=(
                f"{fmt_bytes(plan.download_bytes)} à télécharger "
                f"({len(plan.missing)} fichier(s)), rien n'a été écrit"
            ),
        )

    avant = plan.presence.bytes_on_disk
    try:
        chemin = download(
            repo_id=plan.repo,
            revision=plan.revision,
            allow_patterns=plan.allow_patterns,
            cache_dir=str(plan.hub_dir),
            local_files_only=False,
            # Sans le transmettre, `--force` ne forçait rien : le cache rendait
            # les fichiers déjà présents et la commande se terminait en annonçant
            # un retéléchargement qui n'avait pas eu lieu — exactement ce qu'on
            # cherche à contourner quand on soupçonne un fichier corrompu.
            force_download=force,
        )
    except HfHubHTTPError as exc:
        raise PullError(_erreur_hf(plan.repo, plan.revision, exc)) from exc
    except OSError as exc:
        raise PullError(_erreur_reseau(plan.repo, exc)) from exc

    après = variant_presence(
        config, variant, ref=ref, expected=[f.path for f in plan.selected], source=source
    )
    if not après.complete:
        aperçu = ", ".join(après.missing[:5]) + ("…" if len(après.missing) > 5 else "")
        raise PullError(
            f"{ref} : téléchargement incomplet, {len(après.missing)} fichier(s) attendus "
            f"manquent encore dans {chemin} ({aperçu}) — relancer `ecurie pull {ref}`"
        )
    return PullResult(
        ref=ref,
        plan=plan,
        downloaded=True,
        dry_run=False,
        path=Path(chemin),
        bytes_downloaded=max(après.bytes_on_disk - avant, 0),
        note=f"{plan.repo}@{plan.revision[:12]} → {chemin}",
    )


# --- détails ---------------------------------------------------------------------------


def _est_hex(revision: str) -> bool:
    return all(c in "0123456789abcdef" for c in revision)


def _fichiers(entrées: Iterable[Any]) -> list[RepoFileInfo]:
    """`list_repo_tree` mêle fichiers et dossiers ; seul le dossier porte `tree_id`."""
    fichiers = []
    for entrée in entrées:
        if getattr(entrée, "tree_id", None) is not None:
            continue
        taille = getattr(entrée, "size", None)
        if taille is None:
            continue
        fichiers.append(RepoFileInfo(path=entrée.path, size=int(taille)))
    return sorted(fichiers, key=lambda f: f.path)


def _octets(racine: Path, noms: Iterable[str]) -> int:
    """Octets réellement occupés, un contenu compté une seule fois.

    Les fichiers d'un instantané HF sont des liens symboliques vers `blobs/` :
    `stat` suit le lien (c'est voulu, c'est le contenu qu'on pèse) mais deux
    entrées peuvent viser le même blob, d'où la déduplication par inode.
    """
    vus: set[tuple[int, int]] = set()
    total = 0
    for nom in noms:
        try:
            st = os.stat(racine / nom)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in vus:
            continue
        vus.add((st.st_dev, st.st_ino))
        total += st.st_size
    return total


def _volume(path: Path) -> Path:
    for candidat in [path, *path.parents]:
        if candidat.exists():
            return candidat
    return Path(path.anchor or ".")


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _licence(info: Any) -> str | None:
    card = getattr(info, "card_data", None)
    if card is not None:
        licence = card.get("license") if hasattr(card, "get") else getattr(card, "license", None)
        if licence:
            return str(licence)
    for tag in getattr(info, "tags", None) or []:
        if isinstance(tag, str) and tag.startswith("license:"):
            return tag.split(":", 1)[1]
    return None


def _erreur_reseau(repo: str, exc: Exception) -> str:
    """Panne qui n'est pas une réponse HTTP : hors ligne, DNS, disque plein.

    `OfflineModeIsEnabled` dérive de `ConnectionError`, donc d'`OSError`, et
    passait donc à travers la prise à `HfHubHTTPError`. Le résultat était un
    traceback au lieu d'un refus — sur le seul chemin du projet qui touche au
    réseau, c'est le message qui compte.
    """
    offline = "HF_HUB_OFFLINE" in str(exc) or os.environ.get("HF_HUB_OFFLINE")
    cause = (
        "HF_HUB_OFFLINE est posé : le mode hors ligne interdit toute requête"
        if offline
        else f"{type(exc).__name__}: {exc}"
    )
    return f"{repo} : Hugging Face injoignable — {cause}"


def _erreur_hf(repo: str, revision: str, exc: Exception) -> str:
    """Un dépôt HF inexistant répond 401, pas 404 : le message doit dire les deux.

    C'est exactement le piège rencontré sur `mlx-community/Qwen3-TTS-1.7B-8bit`,
    dépôt qui n'a jamais existé et dont le 401 se lit comme un défaut de jeton.
    """
    statut = getattr(getattr(exc, "response", None), "status_code", None)
    if statut in (401, 403, 404):
        return (
            f"{repo}@{revision[:12]} : HTTP {statut} — dépôt inexistant, renommé, "
            f"privé, ou sous conditions d'accès (`hf auth login`). Vérifier le nom "
            f"du dépôt dans le manifeste avant de chercher un problème de jeton."
        )
    return f"{repo}@{revision[:12]} : l'API Hugging Face a refusé — {exc}"
