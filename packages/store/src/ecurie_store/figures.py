"""Les trois chiffres d'occupation (ARCHITECTURE.md §6.2) et leur classification.

- apparent : somme naïve des tailles — ce que `du` donnerait, faux dès qu'il
  y a des liens durs ;
- réel unique : somme par contenu distinct (sha256 quand il est connu, inode
  sinon — deux liens durs vers le même inode comptent une fois) ;
- récupérable : quatre postes, chacun appelant une action différente.

Règle d'attribution, sans laquelle les postes se recouvriraient et le total
mentirait : **chaque octet appartient à un seul poste**. Pour un contenu donné,
- si toutes ses copies sont jetables (révision obsolète, blob orphelin), tout
  part à la corbeille et rien n'est compté en duplication ;
- s'il en reste une à garder, les copies jetables vont au poste corbeille et les
  copies restantes en trop au poste duplication (même volume seulement — un lien
  dur ne traverse pas les volumes) ;
- si les copies gardées appartiennent toutes à des variants jamais utilisés,
  elles basculent au poste « jamais utilisés », qui les absorbe entièrement.

Le poste « jamais utilisés » reste **inconnu** — pas zéro — tant que la table
`runs` est vide : aucun usage observé ne veut pas dire aucun usage.

`classify()` est la seule autorité : `compute_figures` en fait des totaux, le
générateur de plan en fait des actions. Les deux ne peuvent donc pas diverger,
et le gain annoncé par `ecurie store plan` est celui du rapport de `status`.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ecurie_store.db import LocationRecord

# Qui garde-t-on quand deux gestionnaires détiennent le même contenu ? Celui dont
# l'outillage a le plus besoin du vrai fichier à sa place.
MANAGER_PRIORITY = ["hf", "ollama", "lmstudio", "comfy", "declared", "tier"]

TRASH_REASONS = {"hf_stale": "hf-stale-revision", "orphan": "orphan-blob"}


@dataclass
class DuplicateGroup:
    sha256: str
    size: int
    paths: list[str]
    reclaimable_bytes: int  # copies physiques en trop, liables en dur (même volume)


@dataclass
class ColdLink:
    """Lien symbolique laissé par le tiering : la trace d'un variant déporté."""

    path: str
    target: str
    available: bool
    variant_ref: str | None = None


@dataclass
class Recoverable:
    duplication_bytes: int = 0
    hf_stale_bytes: int = 0
    orphan_bytes: int = 0
    unused_known: bool = False
    unused_bytes: int = 0

    @property
    def total_known_bytes(self) -> int:
        return (
            self.duplication_bytes
            + self.hf_stale_bytes
            + self.orphan_bytes
            + (self.unused_bytes if self.unused_known else 0)
        )


@dataclass
class Figures:
    apparent_bytes: int
    real_unique_bytes: int
    recoverable: Recoverable
    by_manager: dict[str, tuple[int, int]]  # manager → (octets apparents, nb fichiers)
    duplicates: list[DuplicateGroup] = field(default_factory=list)
    unresolved_bytes: int = 0  # octets non rattachés à un variant du registre
    unresolved_count: int = 0
    unverified_bytes: int = 0  # contenu sans sha256 : compté pour lui-même
    unverified_count: int = 0
    announced_bytes: int = 0  # sha256 annoncé par le gestionnaire, jamais relu
    cold: list[ColdLink] = field(default_factory=list)
    unused_variants: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)  # contenu ≠ hash annoncé

    @property
    def cold_unavailable(self) -> list[ColdLink]:
        return [c for c in self.cold if not c.available]


# --- classification par contenu ------------------------------------------------


@dataclass
class DupAction:
    """Une déduplication par lien dur, forcément à l'intérieur d'un même volume."""

    keep: str
    replace: list[str]
    bytes_reclaimed: int


@dataclass
class Attribution:
    """Ce qu'un contenu distinct offre à récupérer, et par quelle action."""

    trash_poste: str = ""  # hf_stale | orphan, si des copies partent à la corbeille
    trash_bytes: int = 0
    trash_paths: list[tuple[str, int]] = field(default_factory=list)  # (chemin, octets libérés)
    dup_bytes: int = 0
    dup_actions: list[DupAction] = field(default_factory=list)
    unused_bytes: int = 0
    unused_paths: list[tuple[str, int]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (chemin, motif d'abandon)

    @property
    def dup_paths(self) -> list[str]:
        return sorted({p for a in self.dup_actions for p in [a.keep, *a.replace]})


@dataclass
class ContentGroup:
    key: str  # sha256, ou "inode:<dev>:<ino>" faute de hash
    size: int
    records: list[LocationRecord]

    @property
    def sha256(self) -> str | None:
        return None if self.key.startswith("inode:") else self.key

    @property
    def jetables(self) -> list[LocationRecord]:
        return [r for r in self.records if r.meta.get("stale") or r.meta.get("orphan")]

    @property
    def gardes(self) -> list[LocationRecord]:
        jetes = {r.path for r in self.jetables}
        return [r for r in self.records if r.path not in jetes]

    def attribution(self, unused: set[str] | None = None) -> Attribution:
        unused = unused or set()
        result = Attribution()
        if self.size == 0:
            return result

        jetables, gardes = self.jetables, self.gardes
        result.trash_poste = "hf_stale" if any(r.meta.get("stale") for r in jetables) else "orphan"

        inodes_gardes = {(r.device, r.inode) for r in gardes}
        cibles = [r for r in jetables if (r.device, r.inode) not in inodes_gardes]

        # Un inode partagé entre plusieurs chemins ne libère ses octets qu'une fois,
        # et un inode tenu hors du parc n'en libère aucun : on ne propose pas de
        # déplacer un fichier pour rien.
        result.trash_paths, écartés = _partage(cibles, self.size)
        result.skipped.extend(écartés)
        result.trash_bytes = sum(octets for _, octets in result.trash_paths)

        if not inodes_gardes:
            return result

        if gardes and all(_jamais_utilise(r, unused) for r in gardes):
            result.unused_paths, écartés = _partage(gardes, self.size)
            result.skipped.extend(écartés)
            result.unused_bytes = sum(octets for _, octets in result.unused_paths)
            return result

        # Duplication : un lien dur ne traverse pas les volumes, on dédoublonne
        # volume par volume — deux copies sur deux disques restent deux copies.
        par_device: dict[int, list[LocationRecord]] = defaultdict(list)
        for r in gardes:
            par_device[r.device].append(r)
        for _, cohorte in sorted(par_device.items()):
            par_inode = _by_inode(cohorte)
            if len(par_inode) < 2:
                continue
            couverture = {clef: entierement_couvert(g) for clef, g in par_inode.items()}
            keep = _preferred_keeper([g[0] for g in par_inode.values()], couverture)
            remplaçables: list[str] = []
            libérés = 0
            for clef, grappe in sorted(par_inode.items()):
                if clef == (keep.device, keep.inode):
                    continue
                if not entierement_couvert(grappe):
                    result.skipped.extend(
                        (r.path, "lien dur tenu hors du parc scanné") for r in grappe
                    )
                    continue
                remplaçables.extend(sorted(r.path for r in grappe))
                libérés += self.size
            if remplaçables:
                result.dup_actions.append(
                    DupAction(keep=keep.path, replace=remplaçables, bytes_reclaimed=libérés)
                )
                result.dup_bytes += libérés
        return result


def _jamais_utilise(rec: LocationRecord, unused: set[str]) -> bool:
    """Ce fichier n'a servi à aucun des variants auxquels il appartient.

    Le pluriel est la seule chose qui compte ici : deux manifestes peuvent
    pointer les mêmes poids pour deux capacités, et il suffit qu'**un** des deux
    ait servi pour que ces octets ne soient pas récupérables. Regarder la seule
    étiquette principale proposerait à la corbeille des poids employés tous les
    jours par l'autre.
    """
    refs = rec.variant_refs
    return bool(refs) and all(ref in unused for ref in refs)


def _by_inode(records: list[LocationRecord]) -> dict[tuple[int, int], list[LocationRecord]]:
    """Un numéro d'inode n'a de sens que sur son volume : deux disques peuvent très
    bien porter tous les deux l'inode 42, sur deux fichiers sans rapport."""
    grappes: dict[tuple[int, int], list[LocationRecord]] = defaultdict(list)
    for r in sorted(records, key=lambda r: r.path):
        grappes[(r.device, r.inode)].append(r)
    return dict(grappes)


def entierement_couvert(grappe: list[LocationRecord]) -> bool:
    """Le parc scanné voit-il TOUTES les références de cet inode ?

    Sinon, retirer les chemins qu'on connaît ne libère pas un octet : la référence
    restée dehors tient le contenu. On retient le `nlink` le plus élevé que la
    grappe connaisse — un seul enregistrement sans cette information (base créée
    avant qu'on la relève) ne doit pas rendre la grappe entière crédule.
    """
    nlinks = [n for r in grappe if isinstance(n := r.meta.get("nlink"), int)]
    if not nlinks:
        return True
    return len({r.path for r in grappe}) >= max(nlinks)


def _partage(
    records: list[LocationRecord], size: int
) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
    """Répartit `size` entre les chemins, et écarte ceux qui ne rendraient rien.

    `size` va au premier chemin de chaque inode, zéro aux liens durs suivants —
    les octets sont déjà comptés. Un inode dont une référence échappe au parc est
    écarté entièrement : déplacer ses chemins ne libérerait pas un octet, autant
    le dire plutôt que de le proposer.
    """
    retenus: list[tuple[str, int]] = []
    écartés: list[tuple[str, str]] = []
    for grappe in _by_inode(records).values():
        if not entierement_couvert(grappe):
            écartés.extend((r.path, "lien dur tenu hors du parc scanné") for r in grappe)
            continue
        for index, r in enumerate(grappe):
            retenus.append((r.path, size if index == 0 else 0))
    return sorted(retenus), sorted(écartés)


def _preferred_keeper(
    records: list[LocationRecord], couverture: dict[tuple[int, int], bool] | None = None
) -> LocationRecord:
    """Qui garde-t-on ? D'abord l'inode qu'on ne pourrait pas libérer de toute façon
    (une référence lui échappe) : le garder rend récupérables tous les autres. Ensuite
    le gestionnaire qui a le plus besoin du vrai fichier, puis le variant rattaché."""
    couverture = couverture or {}

    def rang(rec: LocationRecord) -> tuple[int, int, int, str]:
        manager = rec.manager.split(":", 1)[0]
        priorité = (
            MANAGER_PRIORITY.index(manager)
            if manager in MANAGER_PRIORITY
            else len(MANAGER_PRIORITY)
        )
        libérable = couverture.get((rec.device, rec.inode), True)
        return (1 if libérable else 0, priorité, 0 if rec.variant_ref else 1, rec.path)

    return min(records, key=rang)


def group_by_content(records: Iterable[LocationRecord]) -> list[ContentGroup]:
    """Regroupe les Locations par contenu distinct, dans l'ordre des niveaux de
    hachage de CONCEPTION.md §1.2.

    D'abord l'identité d'inode : deux chemins d'un même inode SONT le même
    artefact, qu'ils portent tous les deux un hash ou aucun. Les séparer parce
    qu'un seul est haché ferait compter deux fois les mêmes octets physiques —
    et promettre deux fois la même récupération.

    Ensuite le hash, qui nomme le contenu et rapproche les inodes distincts. La
    taille exacte entre dans la clé : deux fichiers de tailles différentes ne sont
    jamais le même contenu, même si un nom de blob l'affirme.
    """
    par_inode: dict[tuple[int, int], list[LocationRecord]] = defaultdict(list)
    for r in records:
        if r.link_kind == "symlink":
            continue
        par_inode[(r.device, r.inode)].append(r)

    groups: dict[tuple[str, int], list[LocationRecord]] = defaultdict(list)
    for (device, inode), grappe in par_inode.items():
        taille = max(r.size for r in grappe)
        sha = _sha_de_grappe(grappe)
        groups[(sha or f"inode:{device}:{inode}", taille)].extend(grappe)
    return [
        ContentGroup(key=clef, size=taille, records=sorted(group, key=lambda r: r.path))
        for (clef, taille), group in sorted(groups.items())
    ]


def _sha_de_grappe(grappe: list[LocationRecord]) -> str | None:
    """Le hash d'un inode : le relu fait foi, l'annoncé à défaut. Deux hash relus
    contradictoires pour un même inode sont impossibles ; si cela arrive, on
    préfère ne pas nommer le contenu plutôt que de choisir au hasard."""
    relus = {r.sha256 for r in grappe if r.sha256 and r.meta.get("hash_source") == "verified"}
    if len(relus) == 1:
        return next(iter(relus))
    if relus:
        return None
    annoncés = {r.sha256 for r in grappe if r.sha256}
    return next(iter(annoncés)) if len(annoncés) == 1 else None


def classify(
    records: Iterable[LocationRecord], unused: set[str] | None = None
) -> list[tuple[ContentGroup, Attribution]]:
    return [(g, g.attribution(unused)) for g in group_by_content(records)]


def cold_links(records: Iterable[LocationRecord]) -> list[ColdLink]:
    """Les liens laissés par le tiering, et rien d'autre.

    Le cache HF est lui aussi un champ de liens symboliques — chaque
    `snapshots/<révision>/<fichier>` pointe vers un blob —, et les compter comme
    des variants déportés annoncerait un parc entier sur volume externe. Seul
    `meta.snapshot`, posé par le scanner HF, les distingue.

    Extrait de `compute_figures` pour la même raison qu'il en sortait : la route
    du tiering n'a besoin que de cette liste, et la lui faire payer une
    classification complète du parc reviendrait à hacher trente giga-octets pour
    afficher trois lignes.
    """
    return [
        ColdLink(
            path=r.path,
            target=str(r.meta.get("target", "")),
            available=bool(r.meta.get("available")),
            variant_ref=r.variant_ref,
        )
        for r in records
        if r.link_kind == "symlink" and not r.meta.get("snapshot")
    ]


def telemetry_is_conclusive(
    first_run_at: str | None, unused_after_days: int, now: datetime | None = None
) -> bool:
    """La télémétrie observe-t-elle depuis assez longtemps pour conclure ?

    Sans cette prudence, la toute première exécution enregistrée ferait basculer
    d'un coup tout le reste du parc au poste « jamais utilisé » : un journal ouvert
    hier ne prouve rien sur les trois mois d'avant. Tant qu'il est trop jeune, le
    poste reste inconnu — c'est la même règle que « inconnu, pas zéro ».
    """
    if not first_run_at:
        return False
    try:
        depuis = datetime.fromisoformat(first_run_at)
    except ValueError:
        return False
    if depuis.tzinfo is None:
        depuis = depuis.replace(tzinfo=UTC)
    return depuis <= (now or datetime.now(UTC)) - timedelta(days=unused_after_days)


def unused_variants(
    records: Iterable[LocationRecord],
    last_runs: dict[str, str],
    unused_after_days: int,
    now: datetime | None = None,
) -> set[str]:
    """Variants sans exécution enregistrée depuis N jours. N'a de sens que si la
    télémétrie existe : sans elle, tout paraîtrait inutilisé."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=unused_after_days)
    refs = {ref for r in records for ref in r.variant_refs}
    unused: set[str] = set()
    for ref in sorted(refs):
        last = last_runs.get(ref)
        if last is None:
            unused.add(ref)
            continue
        try:
            when = datetime.fromisoformat(last)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when < cutoff:
            unused.add(ref)
    return unused


# --- les trois chiffres --------------------------------------------------------


def compute_figures(
    records: list[LocationRecord],
    *,
    last_runs: dict[str, str] | None = None,
    telemetry: bool = False,
    unused_after_days: int = 90,
    now: datetime | None = None,
) -> Figures:
    links = [r for r in records if r.link_kind == "symlink"]
    files = [r for r in records if r.link_kind != "symlink"]

    apparent = sum(r.size for r in files)

    by_manager: dict[str, tuple[int, int]] = {}
    for r in files:
        size, count = by_manager.get(r.manager, (0, 0))
        by_manager[r.manager] = (size + r.size, count + 1)

    unused = unused_variants(files, last_runs or {}, unused_after_days, now) if telemetry else set()

    recoverable = Recoverable(unused_known=telemetry)
    duplicates: list[DuplicateGroup] = []
    real_unique = 0

    for group, attribution in classify(files, unused):
        real_unique += group.size
        recoverable.duplication_bytes += attribution.dup_bytes
        recoverable.unused_bytes += attribution.unused_bytes
        if attribution.trash_poste == "hf_stale":
            recoverable.hf_stale_bytes += attribution.trash_bytes
        else:
            recoverable.orphan_bytes += attribution.trash_bytes
        if attribution.dup_bytes and group.sha256:
            duplicates.append(
                DuplicateGroup(
                    sha256=group.sha256,
                    size=group.size,
                    paths=attribution.dup_paths,
                    reclaimable_bytes=attribution.dup_bytes,
                )
            )
    duplicates.sort(key=lambda d: (-d.reclaimable_bytes, d.sha256))

    unresolved = [r for r in files if r.variant_ref is None]
    unverified = [r for r in files if r.sha256 is None]

    return Figures(
        apparent_bytes=apparent,
        real_unique_bytes=real_unique,
        recoverable=recoverable,
        by_manager=dict(sorted(by_manager.items())),
        duplicates=duplicates,
        unresolved_bytes=sum(r.size for r in unresolved),
        unresolved_count=len(unresolved),
        unverified_bytes=sum(r.size for r in unverified),
        unverified_count=len(unverified),
        announced_bytes=sum(r.size for r in files if r.meta.get("hash_source") == "announced"),
        cold=cold_links(links),
        unused_variants=sorted(unused),
        # Une divergence entre le nom du blob et son contenu ne doit pas s'oublier
        # entre deux `verify` : elle reste sous les yeux à chaque `status`.
        mismatched=sorted(
            r.path
            for r in files
            if isinstance(r.meta.get("announced_sha256"), str)
            and r.sha256
            and r.meta["announced_sha256"] != r.sha256
        ),
    )


def fmt_bytes(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f} Go"
    if n >= 1e6:
        return f"{n / 1e6:.1f} Mo"
    if n >= 1e3:
        return f"{n / 1e3:.0f} ko"
    return f"{n} o"
