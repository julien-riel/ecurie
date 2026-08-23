"""Tiering vers un volume externe (ARCHITECTURE.md §6.4, CONCEPTION.md §4.4).

Un variant lourd migre vers `/Volumes/…` et laisse un lien symbolique à sa place.
La séquence est celle de la conception, dans cet ordre et pas un autre :

    copie → fsync → sha256 de la copie → original en quarantaine → lien symbolique

Le sha256 est relu **sur la copie écrite**, pas retenu du flux d'écriture : une
copie qui ment sur son contenu est exactement ce qu'on cherche à attraper. Tant
que la copie n'est pas vérifiée, l'original ne bouge pas ; et il ne part jamais
au néant, seulement en quarantaine.

L'outil ne modifie pas le manifeste : il affiche le patch `tier: cold` à committer
— toute évolution du parc passe par Git (ARCHITECTURE.md §3).
"""

import os
from dataclasses import dataclass
from pathlib import Path

from ecurie_store.db import LocationRecord, StateDB
from ecurie_store.figures import entierement_couvert
from ecurie_store.hashing import sha256_file
from ecurie_store.trash import move_to_trash

COPY_CHUNK = 8 * 1024 * 1024


class TierError(RuntimeError):
    pass


@dataclass
class TierResult:
    source: str
    dest: str
    sha256: str
    size: int
    reused: bool = False  # la copie était déjà là, à l'identique
    freed_bytes: int = 0  # place rendue sur le volume de départ, après `trash empty`


def migrate_path(
    path: str | Path,
    dest_dir: str | Path,
    *,
    expected_sha256: str | None = None,
    require_other_volume: bool = True,
    home: Path | None = None,
    db: StateDB | None = None,
    plan_id: str | None = None,
) -> TierResult:
    source = Path(path)
    dest_dir = Path(dest_dir)

    if source.is_symlink():
        raise TierError(f"{source} : déjà un lien symbolique — variant déjà déporté")
    if not source.is_file():
        raise TierError(f"{source} : fichier introuvable")

    dest_dir.mkdir(parents=True, exist_ok=True)
    if require_other_volume and os.stat(dest_dir).st_dev == os.stat(source).st_dev:
        raise TierError(
            f"{dest_dir} est sur le même volume que {source} — déporter n'y libérerait rien"
        )

    st = os.stat(source)
    digest = sha256_file(source)
    if expected_sha256 and digest != expected_sha256:
        raise TierError(
            f"{source} : contenu différent de celui observé au scan "
            f"({digest[:12]}… ≠ {expected_sha256[:12]}…) — rescanner"
        )

    dest = dest_dir / source.name
    if dest.exists():
        if dest.is_file() and sha256_file(dest) == digest:
            _finish(source, dest, digest, db, plan_id, home)
            return TierResult(
                str(source),
                str(dest),
                digest,
                st.st_size,
                reused=True,
                freed_bytes=st.st_size if st.st_nlink == 1 else 0,
            )
        raise TierError(f"{dest} existe déjà avec un contenu différent")

    temporaire = dest.with_name(f".ecurie-tier-{os.getpid()}-{dest.name}")
    try:
        _copy_fsync(source, temporaire)
        os.replace(temporaire, dest)
    except OSError as exc:
        if temporaire.exists():
            move_to_trash(temporaire, reason="tier-copie-interrompue", home=home)
        raise TierError(f"{source} : copie vers {dest_dir} impossible ({exc})") from exc

    copie = sha256_file(dest)  # relecture réelle : c'est là que la copie se prouve
    if copie != digest:
        move_to_trash(dest, reason="tier-copie-corrompue", sha256=copie, home=home)
        raise TierError(f"{dest} : copie corrompue ({copie[:12]}… ≠ {digest[:12]}…)")

    _finish(source, dest, digest, db, plan_id, home)
    # Un fichier tenu par d'autres liens durs ne rend rien tant qu'ils tiennent :
    # la copie est faite, la place ne revient pas pour autant.
    return TierResult(
        str(source),
        str(dest),
        digest,
        st.st_size,
        freed_bytes=st.st_size if st.st_nlink == 1 else 0,
    )


def _finish(
    source: Path,
    dest: Path,
    digest: str,
    db: StateDB | None,
    plan_id: str | None,
    home: Path | None,
) -> None:
    """La copie est prouvée : l'original part en quarantaine, un lien prend sa place."""
    remplace_par_lien(source, dest, digest, home=home, plan_id=plan_id)
    if db is not None:
        st = os.stat(dest)
        db.hash_cache_put(str(dest), st.st_size, st.st_mtime, st.st_ino, digest)


def remplace_par_lien(
    source: Path,
    dest: Path,
    digest: str,
    *,
    home: Path | None = None,
    plan_id: str | None = None,
    verifier: bool = False,
) -> None:
    """Met `source` en quarantaine et laisse un lien symbolique vers `dest`.

    `verifier` sert aux autres chemins d'un même inode : leur contenu n'a pas été
    relu par la copie, on le confronte au hash prouvé avant de les déplacer.
    """
    if verifier and sha256_file(source) != digest:
        raise TierError(f"{source} : contenu différent de la copie déjà déportée")
    move_to_trash(
        source,
        reason="tiered",
        sha256=digest,
        plan_id=plan_id,
        home=home,
        extra={"dest": str(dest)},
    )
    os.symlink(dest, source)


def _copy_fsync(source: Path, dest: Path) -> None:
    with open(source, "rb") as lecture, open(dest, "wb") as écriture:
        while block := lecture.read(COPY_CHUNK):
            écriture.write(block)
        écriture.flush()
        os.fsync(écriture.fileno())


def variant_records(records: list[LocationRecord], ref: str) -> list[LocationRecord]:
    return sorted(
        # `variant_refs` et non `variant_ref` : des poids partagés par deux
        # manifestes se déportent en entier ou pas du tout — les couper en deux
        # laisserait la moitié d'un modèle sur chaque volume.
        (r for r in records if ref in r.variant_refs and r.link_kind != "symlink"),
        key=lambda r: r.path,
    )


def dest_for(record: LocationRecord, ref: str, dest_root: Path, common: str) -> Path:
    """Reproduit l'arborescence relative sous `<volume>/<model@variant>/`."""
    relatif = Path(record.path).relative_to(common) if common else Path(Path(record.path).name)
    return (dest_root / ref.replace("/", "_") / relatif).parent


def common_root(records: list[LocationRecord]) -> str:
    if not records:
        return ""
    return os.path.commonpath([str(Path(r.path).parent) for r in records])


def tier_variant(
    records: list[LocationRecord],
    ref: str,
    dest_root: str | Path,
    *,
    require_other_volume: bool = True,
    home: Path | None = None,
    db: StateDB | None = None,
    dry_run: bool = False,
) -> list[TierResult]:
    cibles = variant_records(records, ref)
    if not cibles:
        raise TierError(
            f"{ref} : aucun fichier observé pour ce variant — "
            "lancer `ecurie store scan`, ou le variant n'est pas téléchargé"
        )
    volumes = {r.device for r in cibles}
    if len(volumes) > 1:
        raise TierError(f"{ref} : fichiers répartis sur {len(volumes)} volumes — cas non géré")

    dest_root = Path(dest_root)
    common = common_root(cibles)
    résultats: list[TierResult] = []

    # Plusieurs chemins d'un même inode sont un seul fichier : on le copie une fois
    # et on remplace tous ses chemins par des liens vers la même destination. Les
    # copier chacun déporterait le même contenu plusieurs fois et compterait le gain
    # autant de fois — alors qu'il faut au contraire les convertir TOUS pour que
    # l'inode lâche enfin ses octets.
    par_inode: dict[tuple[int, int], list[LocationRecord]] = {}
    for rec in cibles:
        par_inode.setdefault((rec.device, rec.inode), []).append(rec)

    for grappe in [par_inode[clef] for clef in sorted(par_inode)]:
        premier = grappe[0]
        dossier = dest_for(premier, ref, dest_root, common)
        destination = dossier / Path(premier.path).name
        nlink = premier.meta.get("nlink")
        libérable = not isinstance(nlink, int) or len(grappe) >= nlink

        if dry_run:
            résultats.append(
                TierResult(
                    premier.path,
                    str(destination),
                    premier.sha256 or "",
                    premier.size,
                    freed_bytes=premier.size if libérable else 0,
                )
            )
            résultats.extend(
                TierResult(
                    autre.path, str(destination), autre.sha256 or "", autre.size, reused=True
                )
                for autre in grappe[1:]
            )
            continue

        relu = premier.meta.get("hash_source") == "verified"
        résultat = migrate_path(
            premier.path,
            dossier,
            expected_sha256=premier.sha256 if relu else None,
            require_other_volume=require_other_volume,
            home=home,
            db=db,
        )
        résultat.freed_bytes = premier.size if libérable else 0
        résultats.append(résultat)
        for autre in grappe[1:]:
            remplace_par_lien(
                Path(autre.path), Path(résultat.dest), résultat.sha256, home=home, verifier=True
            )
            résultats.append(
                TierResult(autre.path, résultat.dest, résultat.sha256, autre.size, reused=True)
            )
    return résultats


@dataclass
class VariantFootprint:
    """Ce qu'un variant pèse sur le disque, et ce qu'un déport en rendrait.

    Les deux chiffres diffèrent, et la différence est le sujet même du tiering :
    `bytes` est ce que le variant occupe, `freed_bytes` ce que le volume de
    départ récupérerait vraiment. Un inode dont une référence échappe au parc
    scanné — un lien dur posé ailleurs — ne rend rien du tout : le déporter
    copierait des giga-octets sans en libérer un seul.
    """

    ref: str
    files: int  # chemins observés, liens symboliques exclus
    bytes: int  # octets uniques : un inode compté une fois, quel que soit le nombre de chemins
    freed_bytes: int  # ce que le volume de départ rendrait après `trash empty`
    shared_with: list[str]  # autres variants qui tiennent une partie de ces octets
    devices: list[int]  # volumes portant ces fichiers ; plus d'un, et `tier` refuse
    tiered_links: int  # chemins déjà remplacés par un lien : le variant est déjà déporté

    @property
    def tierable(self) -> bool:
        """Rien à déporter si tout est déjà parti, ou si les fichiers sont répartis."""
        return self.files > 0 and len(self.devices) == 1


def footprints(records: list[LocationRecord]) -> list[VariantFootprint]:
    """L'empreinte disque de chaque variant observé, la plus lourde d'abord.

    Trois pièges, et chacun a sa contrepartie dans le code :

    - **un fichier peut appartenir à deux variants.** `variant_refs` est un
      pluriel, et deux manifestes pointant les mêmes poids doivent tous deux
      afficher leur poids réel : la somme de la colonne dépasse alors le parc, ce
      que `shared_with` explique au lecteur plutôt que de le laisser conclure à
      une erreur de calcul ;
    - **un inode à plusieurs chemins ne pèse qu'une fois.** Le cache HF en est
      plein, et compter chaque chemin doublerait l'empreinte annoncée ;
    - **les liens symboliques ne pèsent rien** mais disent quelque chose : un
      variant qui n'a plus que des liens est déjà déporté, et le proposer au
      déport serait absurde.
    """
    fichiers = [r for r in records if r.link_kind != "symlink"]
    par_ref: dict[str, list[LocationRecord]] = {}
    liens: dict[str, int] = {}
    for r in fichiers:
        for ref in r.variant_refs:
            par_ref.setdefault(ref, []).append(r)
    for r in records:
        if r.link_kind == "symlink" and not r.meta.get("snapshot"):
            for ref in r.variant_refs:
                liens[ref] = liens.get(ref, 0) + 1
                par_ref.setdefault(ref, [])

    résultats: list[VariantFootprint] = []
    for ref, lot in par_ref.items():
        grappes: dict[tuple[int, int], list[LocationRecord]] = {}
        for r in lot:
            grappes.setdefault((r.device, r.inode), []).append(r)
        octets = sum(max(r.size for r in g) for g in grappes.values())
        rendus = sum(max(r.size for r in g) for g in grappes.values() if entierement_couvert(g))
        résultats.append(
            VariantFootprint(
                ref=ref,
                files=len(lot),
                bytes=octets,
                freed_bytes=rendus,
                shared_with=sorted({a for r in lot for a in r.variant_refs} - {ref}),
                devices=sorted({r.device for r in lot}),
                tiered_links=liens.get(ref, 0),
            )
        )
    résultats.sort(key=lambda f: (-f.bytes, f.ref))
    return résultats


def yaml_patch(ref: str) -> str:
    """Le patch à committer — l'outil ne touche jamais au registre lui-même."""
    model_id, _, variant_id = ref.partition("@")
    return (
        f"# registry/models/{model_id}.yaml\n"
        "variants:\n"
        f"  - id: {variant_id}\n"
        "    tier: cold          # ← était hot ; les fichiers vivent maintenant\n"
        "                        #   sur le volume externe, un lien reste en place\n"
    )
