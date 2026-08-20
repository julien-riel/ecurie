"""Les trois chiffres d'occupation (ARCHITECTURE.md §6.2).

- apparent : somme naïve des tailles — ce que `du` donnerait, faux dès qu'il
  y a des liens durs ;
- réel unique : somme par contenu distinct (sha256 quand il est connu, inode
  sinon — deux liens durs vers le même inode comptent une fois) ;
- récupérable : quatre postes, chacun appelant une action différente. Le poste
  « jamais utilisés » reste inconnu tant que la télémétrie (v0.2) est vide.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from ecurie_store.db import LocationRecord


@dataclass
class DuplicateGroup:
    sha256: str
    size: int
    paths: list[str]
    reclaimable_bytes: int  # copies physiques en trop, liables en dur (même volume)


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


def _identity(rec: LocationRecord) -> str:
    return rec.sha256 or f"inode:{rec.device}:{rec.inode}"


def compute_figures(records: list[LocationRecord]) -> Figures:
    apparent = sum(r.size for r in records)

    by_manager: dict[str, tuple[int, int]] = {}
    for r in records:
        size, count = by_manager.get(r.manager, (0, 0))
        by_manager[r.manager] = (size + r.size, count + 1)

    groups: dict[str, list[LocationRecord]] = defaultdict(list)
    for r in records:
        groups[_identity(r)].append(r)
    real_unique = sum(max(g.size for g in group) for group in groups.values())

    duplicates: list[DuplicateGroup] = []
    duplication_bytes = 0
    for key, group in groups.items():
        if not key.startswith("inode:"):
            inodes_by_device: dict[int, set[int]] = defaultdict(set)
            for r in group:
                inodes_by_device[r.device].add(r.inode)
            reclaimable = sum(
                (len(inodes) - 1) * group[0].size for inodes in inodes_by_device.values()
            )
            if reclaimable > 0:
                duplication_bytes += reclaimable
                duplicates.append(
                    DuplicateGroup(
                        sha256=key,
                        size=group[0].size,
                        paths=sorted(r.path for r in group),
                        reclaimable_bytes=reclaimable,
                    )
                )
    duplicates.sort(key=lambda d: d.reclaimable_bytes, reverse=True)

    # Postes stale/orphelins : une fois par contenu distinct, pas par chemin.
    hf_stale = sum(
        max(r.size for r in group)
        for group in groups.values()
        if any(r.meta.get("stale") for r in group)
    )
    orphans = sum(
        max(r.size for r in group)
        for group in groups.values()
        if any(r.meta.get("orphan") for r in group)
        and not any(r.meta.get("stale") for r in group)  # jamais compté deux fois
    )

    unresolved = [r for r in records if r.variant_ref is None]

    return Figures(
        apparent_bytes=apparent,
        real_unique_bytes=real_unique,
        recoverable=Recoverable(
            duplication_bytes=duplication_bytes,
            hf_stale_bytes=hf_stale,
            orphan_bytes=orphans,
        ),
        by_manager=dict(sorted(by_manager.items())),
        duplicates=duplicates,
        unresolved_bytes=sum(r.size for r in unresolved),
        unresolved_count=len(unresolved),
    )


def fmt_bytes(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f} Go"
    if n >= 1e6:
        return f"{n / 1e6:.1f} Mo"
    if n >= 1e3:
        return f"{n / 1e3:.0f} ko"
    return f"{n} o"
