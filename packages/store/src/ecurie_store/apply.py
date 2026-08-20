"""Exécution d'un plan de récupération (CONCEPTION.md §4.3).

Trois règles gouvernent ce module, et elles ne se négocient pas :

1. **Le plan n'est jamais cru sur parole.** Chaque action revérifie, au moment de
   l'exécuter, que le fichier est bien celui qui a été observé au scan — taille,
   mtime, inode — puis relit son contenu pour en recalculer le sha256. Le disque
   a pu bouger entre le plan et l'exécution ; dans ce cas l'action est refusée,
   pas adaptée.
2. **Rien ne s'efface.** Les actions `trash` déménagent en quarantaine. La seule
   exception apparente est le `rename` atomique de la déduplication : le contenu
   n'y disparaît pas, il reste accessible à l'octet près sous le chemin gardé,
   dont le sha256 vient d'être vérifié identique. Remplacer une copie par un lien
   dur vers son jumeau prouvé n'est pas une suppression.
3. **Tout est journalisé.** Une ligne JSON par action dans
   `~/.ecurie/journal/<plan_id>.jsonl`, y compris les refus et leur motif.
"""

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from ecurie_core.config import ecurie_home

from ecurie_store.hashing import sha256_file
from ecurie_store.plan import action_bytes
from ecurie_store.tier import TierError, migrate_path
from ecurie_store.trash import TrashError, move_to_trash

APPLIED = "applied"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class Outcome:
    index: int
    kind: str
    reason: str
    status: str
    bytes_reclaimed: int = 0
    message: str = ""
    paths: list[str] = field(default_factory=list)


@dataclass
class ApplyReport:
    plan_id: str
    dry_run: bool
    outcomes: list[Outcome] = field(default_factory=list)
    journal_path: Path | None = None
    measured_bytes: int | None = None  # place réellement rendue, relevée sur les volumes

    @property
    def applied_bytes(self) -> int:
        return sum(o.bytes_reclaimed for o in self.outcomes if o.status == APPLIED)

    @property
    def freed_bytes(self) -> int:
        """Octets rendus au disque sur-le-champ. Seule la déduplication en libère :
        le `rename` atomique fait tomber la dernière référence de l'inode."""
        return sum(
            o.bytes_reclaimed for o in self.outcomes if o.status == APPLIED and o.kind == "hardlink"
        )

    @property
    def quarantined_bytes(self) -> int:
        """Octets en attente : ils occupent encore la place jusqu'à `trash empty`.

        Le tiering compte ici, et non plus haut : il copie vers le volume externe,
        puis renvoie l'original en quarantaine *sur son volume de départ*. La place
        n'y revient qu'une fois la corbeille vidée.
        """
        return sum(
            o.bytes_reclaimed
            for o in self.outcomes
            if o.status == APPLIED and o.kind in ("trash", "tier")
        )

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


def apply_plan(
    plan: dict[str, Any],
    *,
    dry_run: bool = False,
    home: Path | None = None,
    journal: bool = True,
    on_outcome: Callable[[Outcome], None] | None = None,
    require_other_volume: bool = True,
) -> ApplyReport:
    plan_id = str(plan.get("plan_id", uuid.uuid4().hex))
    report = ApplyReport(plan_id=plan_id, dry_run=dry_run)

    # Le journal s'écrit au fil de l'eau, une ligne à la fois, et il est vidé après
    # chacune : une interruption au clavier au milieu d'un plan doit laisser la
    # trace de ce qui a déjà bougé sur le disque, pas une page blanche.
    fichier = _open_journal(plan_id, home) if journal and not dry_run else None
    if fichier is not None:
        report.journal_path = Path(fichier.name)

    def note(ligne: dict[str, Any]) -> None:
        if fichier is None:
            return
        fichier.write(json.dumps(ligne, ensure_ascii=False) + "\n")
        fichier.flush()

    try:
        note(
            {
                "event": "start",
                "plan_id": plan_id,
                "scan_id": plan.get("scan_id"),
                "dry_run": dry_run,
                "at": datetime.now(UTC).isoformat(),
            }
        )

        libre_avant = _libre_par_volume(plan)
        for index, action in enumerate(plan.get("actions", [])):
            outcome = _apply_action(index, action, dry_run=dry_run, home=home, plan_id=plan_id,
                                    require_other_volume=require_other_volume)
            report.outcomes.append(outcome)
            note({"event": "action", **asdict(outcome)})
            if on_outcome is not None:
                on_outcome(outcome)

        if libre_avant and not dry_run:
            libre_après = _libre_par_volume(plan)
            report.measured_bytes = sum(
                libre_après.get(volume, octets) - octets for volume, octets in libre_avant.items()
            )

        note(
            {
                "event": "end",
                "plan_id": plan_id,
                "applied_bytes": report.applied_bytes,
                "measured_bytes": report.measured_bytes,
                "counts": report.counts,
                "at": datetime.now(UTC).isoformat(),
            }
        )
    finally:
        if fichier is not None:
            fichier.close()
    return report


def _apply_action(
    index: int,
    action: dict[str, Any],
    *,
    dry_run: bool,
    home: Path | None,
    plan_id: str,
    require_other_volume: bool = True,
) -> Outcome:
    kind = str(action.get("kind", ""))
    reason = str(action.get("reason", ""))
    try:
        if kind == "hardlink":
            return _apply_hardlink(index, action, dry_run=dry_run)
        if kind == "trash":
            return _apply_trash(index, action, dry_run=dry_run, home=home, plan_id=plan_id)
        if kind == "tier":
            return _apply_tier(
                index,
                action,
                dry_run=dry_run,
                home=home,
                plan_id=plan_id,
                require_other_volume=require_other_volume,
            )
    except (OSError, TrashError, TierError) as exc:
        return Outcome(index, kind, reason, FAILED, message=str(exc))
    return Outcome(index, kind, reason, SKIPPED, message=f"action inconnue : {kind!r}")


# --- déduplication par lien dur ------------------------------------------------


def _apply_hardlink(index: int, action: dict[str, Any], *, dry_run: bool) -> Outcome:
    keep = str(action["keep"])
    replace = [str(p) for p in action.get("replace", [])]
    stats = action.get("stats", {})
    attendu = action.get("sha256")
    outcome = Outcome(index, "hardlink", str(action.get("reason", "")), SKIPPED,
                      paths=[keep, *replace])

    if not attendu:
        outcome.message = "aucun sha256 au plan — une taille ne prouve rien"
        return outcome

    écart = _stat_drift(keep, stats.get(keep))
    if écart:
        outcome.message = f"{keep} : {écart}"
        return outcome
    if os.path.islink(keep) or any(os.path.islink(p) for p in replace):
        outcome.message = "lien symbolique dans l'action — la déduplication ne s'y applique pas"
        return outcome
    keep_st = os.lstat(keep)

    digest = sha256_file(keep)
    if digest != attendu:
        outcome.message = f"{keep} : contenu changé depuis le scan ({digest[:12]}…)"
        return outcome

    # Un inode ne rend ses octets que si TOUS ses chemins passent au lien dur : en
    # laisser un derrière, c'est laisser le contenu en place et annoncer un gain nul.
    par_inode: dict[int, list[str]] = {}
    for cible in replace:
        écart = _stat_drift(cible, stats.get(cible))
        if écart:
            outcome.message = f"{cible} : {écart}"
            return outcome
        st = os.lstat(cible)
        if st.st_dev != keep_st.st_dev:
            outcome.message = f"{cible} : autre volume que {keep} — un lien dur ne traverse pas"
            return outcome
        if st.st_ino != keep_st.st_ino:
            par_inode.setdefault(st.st_ino, []).append(cible)

    if not par_inode:
        outcome.message = "déjà dédupliqué (même inode)"
        return outcome

    for chemins in par_inode.values():
        nlink = os.lstat(chemins[0]).st_nlink
        if nlink != len(chemins):
            outcome.message = (
                f"{chemins[0]} : {nlink} lien(s) dur(s) pour {len(chemins)} au plan — "
                "d'autres chemins tiennent ce contenu, rien ne serait libéré"
            )
            return outcome
        # Un seul hachage par inode : les autres chemins sont le même contenu.
        if sha256_file(chemins[0]) != attendu:
            outcome.message = f"{chemins[0]} : contenu différent du plan — action refusée"
            return outcome

    libérés = 0
    faits = 0
    for chemins in par_inode.values():
        for chemin in chemins:
            try:
                if not dry_run:
                    _link_atomically(keep, chemin)
            except OSError as exc:
                # Interrompu au milieu d'une grappe : les chemins déjà convertis le
                # restent (aucune perte, leur contenu est celui de `keep`, vérifié), mais
                # l'inode n'est pas libéré pour autant — ses octets ne sont pas comptés.
                # Le journal doit dire où l'action s'est arrêtée.
                outcome.status = FAILED
                outcome.bytes_reclaimed = libérés
                outcome.message = (
                    f"{chemin} : {exc} — action interrompue après {faits} conversion(s) ; "
                    "les chemins restants sont intacts"
                )
                return outcome
            faits += 1
        libérés += keep_st.st_size

    outcome.status = APPLIED
    outcome.bytes_reclaimed = libérés
    outcome.message = f"{faits} copie(s) remplacée(s) par un lien dur vers {keep}"
    return outcome


def _link_atomically(keep: str, cible: str) -> None:
    """`link` vers un nom temporaire puis `rename` — la cible n'est jamais absente.

    Le contenu remplacé est celui du fichier gardé, sha256 vérifié à l'instant :
    l'octet qui disparaît du chemin `cible` est déjà présent, identique, sous `keep`.
    """
    cible_path = Path(cible)
    temporaire = cible_path.with_name(f".ecurie-link-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    os.link(keep, temporaire)
    try:
        os.replace(temporaire, cible_path)
    except OSError:
        temporaire.unlink(missing_ok=True)  # rm-ok: lien dur temporaire que l'on vient de créer
        raise


# --- mise en quarantaine ---------------------------------------------------------


def _apply_trash(
    index: int, action: dict[str, Any], *, dry_run: bool, home: Path | None, plan_id: str
) -> Outcome:
    path = str(action["path"])
    reason = str(action.get("reason", ""))
    stats = action.get("stats", {})
    outcome = Outcome(index, "trash", reason, SKIPPED, paths=[path])

    if not os.path.lexists(path):
        outcome.message = "déjà disparu"
        return outcome

    if os.path.isdir(path) and not os.path.islink(path):
        if reason != "detached-snapshot":
            outcome.message = "dossier inattendu pour cette action"
            return outcome
        if not _only_symlinks(path):
            outcome.message = "le dossier contient autre chose que des liens — refus"
            return outcome
    else:
        écart = _stat_drift(path, stats.get(path))
        if écart:
            outcome.message = écart
            return outcome
        attendu = action.get("sha256")
        if attendu and not os.path.islink(path):
            digest = sha256_file(path)
            if digest != attendu:
                outcome.message = f"contenu changé depuis le scan ({digest[:12]}…) — refus"
                return outcome

    if not dry_run:
        move_to_trash(
            path,
            reason=reason,
            sha256=action.get("sha256"),
            plan_id=plan_id,
            home=home,
            extra={"variant_ref": action.get("variant_ref")},
        )
    outcome.status = APPLIED
    outcome.bytes_reclaimed = action_bytes(action)
    outcome.message = "mis en quarantaine"
    return outcome


def _only_symlinks(directory: str) -> bool:
    for racine, _, fichiers in os.walk(directory):
        for nom in fichiers:
            if not os.path.islink(os.path.join(racine, nom)):
                return False
    return True


# --- tiering piloté par un plan ---------------------------------------------------


def _apply_tier(
    index: int,
    action: dict[str, Any],
    *,
    dry_run: bool,
    home: Path | None,
    plan_id: str,
    require_other_volume: bool,
) -> Outcome:
    path = str(action["path"])
    dest = str(action.get("dest_volume") or action.get("dest") or "")
    outcome = Outcome(index, "tier", str(action.get("reason", "tier")), SKIPPED, paths=[path])
    if not dest:
        outcome.message = "aucun volume de destination dans l'action"
        return outcome
    écart = _stat_drift(path, action.get("stats", {}).get(path))
    if écart:
        outcome.message = écart
        return outcome
    if dry_run:
        outcome.message = f"déporterait vers {dest}"
        return outcome
    résultat = migrate_path(
        path,
        dest,
        expected_sha256=action.get("sha256"),
        require_other_volume=require_other_volume,
        home=home,
        plan_id=plan_id,
    )
    outcome.status = APPLIED
    outcome.bytes_reclaimed = résultat.size
    outcome.message = f"déporté vers {résultat.dest}"
    return outcome


# --- garde-fous communs ------------------------------------------------------------


def _stat_drift(path: str, expected: dict[str, Any] | None) -> str | None:
    """Message d'écart entre le disque et ce que le plan a observé, ou None."""
    try:
        st = os.lstat(path)
    except OSError:
        return "fichier disparu depuis le scan"
    if not expected:
        return None
    if st.st_size != expected.get("size"):
        return f"taille changée depuis le scan ({st.st_size} ≠ {expected.get('size')})"
    if st.st_mtime != expected.get("mtime"):
        return "date de modification changée depuis le scan"
    if st.st_ino != expected.get("inode"):
        return "inode changé depuis le scan (fichier réécrit)"
    return None


def _libre_par_volume(plan: dict[str, Any]) -> dict[int, int]:
    """Place libre, volume par volume, avant et après l'exécution.

    Le gain annoncé est une déduction ; celui-ci est une mesure. C'est le seul
    chiffre qui répond honnêtement au critère du jalon (« récupère l'espace annoncé
    à ±1 % »), et le seul qui attrape ce que la comptabilité ne peut pas voir —
    un clone APFS, par exemple, partage déjà ses blocs : le supprimer ne rend rien.
    """
    libre: dict[int, int] = {}
    for action in plan.get("actions", []):
        for chemin in [action.get("keep"), action.get("path"), *action.get("replace", [])]:
            if not chemin:
                continue
            dossier = os.path.dirname(str(chemin)) or "."
            try:
                st = os.stat(dossier)
                stats = os.statvfs(dossier)
            except OSError:
                continue
            libre[st.st_dev] = stats.f_bavail * stats.f_frsize
    return libre


def _open_journal(plan_id: str, home: Path | None) -> TextIO:
    dossier = (home or ecurie_home()) / "journal"
    dossier.mkdir(parents=True, exist_ok=True)
    return open(dossier / f"{plan_id}.jsonl", "a", encoding="utf-8")
