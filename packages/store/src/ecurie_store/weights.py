"""Où sont les poids d'un variant, ici et maintenant.

Le manifeste dit ce qu'il faut (dépôt, révision épinglée) ; le disque dit ce
qu'on a. Ce module fait la jointure pour le compte du superviseur, qui doit
passer au worker un **chemin local** : un worker ne télécharge jamais rien
(`HF_HUB_OFFLINE=1`), sinon des gigaoctets apparaîtraient hors de toute
comptabilité, et la révision réellement exécutée cesserait d'être celle du
manifeste.

La disposition du cache Hugging Face est stable et documentée :

    <hub>/models--<org>--<nom>/snapshots/<révision>/…    (liens vers ../../blobs/)

On la calcule directement plutôt que d'appeler `snapshot_download` : c'est
instantané, sans réseau, et surtout ça permet de dire précisément *laquelle* des
deux choses manque — le dépôt, ou la révision épinglée.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ecurie_core.config import Config
from ecurie_core.models import Source, Variant

from ecurie_store.db import LocationRecord


class WeightsMissing(RuntimeError):
    """Poids absents ou à une autre révision. Le message porte la commande qui répare."""


@dataclass
class WeightsLocation:
    path: Path
    kind: str  # huggingface | local
    revision: str | None = None
    files: list[str] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.path.exists()


def repo_dir_name(repo: str) -> str:
    """`org/nom` → `models--org--nom`, la convention du cache HF."""
    return "models--" + repo.replace("/", "--")


def hf_hub_dir(config: Config) -> Path:
    hub = config.scan.hf_hub or Path("~/.cache/huggingface/hub")
    return hub.expanduser()


def snapshot_dir(config: Config, repo: str, revision: str) -> Path:
    return hf_hub_dir(config) / repo_dir_name(repo) / "snapshots" / revision


def resolve_weights(config: Config, variant: Variant, *, ref: str) -> WeightsLocation:
    """Chemin local des poids du variant, ou `WeightsMissing` avec la réparation."""
    return resolve_source(config, variant.source, ref=ref)


def resolve_extra_weights(config: Config, variant: Variant, *, ref: str) -> dict[str, Path]:
    """Les dépôts secondaires du variant, par rôle — tokenizer, encodeur visuel…

    Résolus avec la même exigence que les poids principaux : un dépôt absent
    lève, et le message porte la commande qui répare. Un variant à demi présent
    est le pire des états — il passe le contrôle d'admission, coûte son warmup,
    et meurt sur un fichier manquant plusieurs secondes plus tard.

    Un rôle absent est refusé ici plutôt qu'au worker : c'est le manifeste qui
    est fautif, pas l'exécution.
    """
    chemins: dict[str, Path] = {}
    for source in variant.extra_sources or []:
        if not source.role:
            raise WeightsMissing(
                f"{ref} : une source de `extra_sources` n'a pas de `role` — "
                "le worker recevrait un chemin sans savoir ce qu'il contient"
            )
        chemins[source.role] = resolve_source(
            config, source, ref=f"{ref} ({source.role})"
        ).path
    return chemins


def resolve_source(config: Config, source: Source, *, ref: str) -> WeightsLocation:
    """La résolution d'un dépôt, principale ou secondaire — elles sont identiques.

    Publique parce que `pull` en a besoin : télécharger et vérifier la présence se
    font source par source, et une seule des deux façons de trouver un
    instantané doit exister.
    """
    if source.kind == "local":
        if not source.path:
            raise WeightsMissing(f"{ref} : source local sans chemin dans le manifeste")
        chemin = Path(source.path).expanduser()
        if not chemin.exists():
            raise WeightsMissing(f"{ref} : chemin déclaré introuvable — {chemin}")
        return WeightsLocation(path=chemin, kind="local")

    if source.kind != "huggingface":
        raise WeightsMissing(
            f"{ref} : source {source.kind!r} non gérée au v0.3 "
            "(huggingface et local seulement — voir CONCEPTION.md §13.1)"
        )
    if not source.repo or not source.revision:
        raise WeightsMissing(f"{ref} : source huggingface sans dépôt ou sans révision épinglée")

    répertoire = hf_hub_dir(config) / repo_dir_name(source.repo)
    instantané = répertoire / "snapshots" / source.revision
    if instantané.is_dir():
        return WeightsLocation(
            path=instantané,
            kind="huggingface",
            revision=source.revision,
            files=sorted(p.name for p in instantané.iterdir()),
        )

    if répertoire.is_dir():
        présentes = sorted(p.name for p in (répertoire / "snapshots").glob("*")) or ["aucune"]
        raise WeightsMissing(
            f"{ref} : le dépôt {source.repo} est en cache, mais pas à la révision "
            f"{source.revision[:12]} (présentes : {', '.join(r[:12] for r in présentes)}) — "
            f"ecurie pull {ref}"
        )
    raise WeightsMissing(f"{ref} : poids absents du cache — ecurie pull {ref}")


def variant_disk_bytes(records: list[LocationRecord], ref: str) -> int:
    """Occupation réelle d'un variant : une fois par contenu, jamais par lien.

    Compter les liens symboliques d'un instantané HF en plus de leurs blobs
    doublerait la taille annoncée de tout le parc Hugging Face.

    Attention à ce que ce chiffre veut dire : il couvre **tout** ce que le
    résolveur a rattaché à ce variant, donc l'ensemble du dépôt Hugging Face,
    révisions périmées comprises. C'est exactement ce qu'il faut pour la
    comptabilité disque — ces octets sont bien là — mais pas pour le `disk_bytes`
    d'un profil, qui doit décrire la seule révision mesurée. Le banc d'essai
    mesure donc l'instantané épinglé.
    """
    vus: set[tuple] = set()
    total = 0
    for rec in records:
        if ref not in rec.variant_refs or rec.link_kind == "symlink":
            continue
        clé = (rec.sha256,) if rec.sha256 else (rec.device, rec.inode)
        if clé in vus:
            continue
        vus.add(clé)
        total += rec.size
    return total
