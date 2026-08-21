"""Contrôle d'admission mémoire (CONCEPTION.md §5.4, ARCHITECTURE.md §7).

Fonction pure : des résidents, un budget, un candidat — une décision. Aucun
processus, aucun disque, aucune horloge. C'est ce qui la rend simulable
intégralement en test, et c'est important : c'est le seul endroit du projet où
une erreur se paie en swap de trente secondes ou en OOM.

La politique par défaut est celle du §7 de l'architecture : un seul modèle lourd
résident à la fois, les petits restent chauds. Un `warmup_ms` de 2,4 s payé à
chaque phrase de synthèse rend l'outil désagréable ; c'est la raison d'être des
résidents.

Le seuil de lourdeur vaut 8 Gio, corrigé des 6 Go que l'architecture avançait
avant toute mesure : à 6 Go, les quatre profils du parc réel sont tous lourds et
la règle ne discrimine plus rien. Il doit rester d'accord avec le défaut de
`Config.heavy_threshold_bytes` — `core` ne peut pas dépendre de `runtime`, donc
la constante est écrite deux fois et un test vérifie qu'elles disent la même
chose.

Deux refus explicites valent mieux qu'un swap subi :
- un variant **sans profil mesuré** n'est pas admis en usage courant. Il passe
  par le mode mesure (`ecurie bench`), parc vidé, qui écrit son premier profil.
  C'est ce qui rend vivable la règle « jamais de profil estimé » ;
- un variant dont le pic dépasse le budget à lui seul est refusé, jamais tenté.

**Un refus se lit.** Les tailles des messages passent par `fmt_memory` : ces
phrases sont rendues telles quelles par `ecurie ps --for` et par le bandeau de
l'Atelier, et « demande 25704234348 octets, le budget entier est de 19070000000 »
n'apprend rien à personne. Le §4 du plan le demande en toutes lettres — « ce
morceau de 30 s demanderait 24,2 Gio, au-delà des 17,8 disponibles » vaut mieux
qu'un bouton grisé —, et l'exigence porte d'abord sur qui écrit la phrase.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ecurie_core.format import fmt_memory

GIB = 1 << 30
DEFAULT_HEAVY_THRESHOLD = 8 * GIB
DEFAULT_MAX_HEAVY_RESIDENT = 1


@dataclass(frozen=True)
class Resident:
    ref: str  # "model@variant"
    peak_bytes: int
    last_used: float  # horodatage POSIX ; seul l'ordre compte ici
    pinned: bool = False
    # Un job tourne en ce moment sur ce résident. Il est alors aussi intouchable
    # qu'un épinglé, et pour une raison plus forte : l'évincer ne libérerait pas
    # de la mémoire, il détruirait un travail en cours — le job meurt sur un
    # canal fermé, ses fichiers de sortie sont perdus, et la commande qui a
    # provoqué l'éviction n'en sait rien.
    busy: bool = False

    def heavy(self, threshold: int = DEFAULT_HEAVY_THRESHOLD) -> bool:
        return self.peak_bytes > threshold


@dataclass(frozen=True)
class Policy:
    budget_bytes: int
    max_heavy_resident: int = DEFAULT_MAX_HEAVY_RESIDENT
    heavy_threshold_bytes: int = DEFAULT_HEAVY_THRESHOLD


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str
    evict: tuple[str, ...] = ()
    headroom_bytes: int = 0  # budget restant une fois le candidat chargé
    already_resident: bool = False
    measure_mode: bool = False
    blockers: tuple[str, ...] = field(default=())  # résidents épinglés qui bloquent


def plan_admission(
    ref: str,
    peak_bytes: int | None,
    residents: Iterable[Resident],
    policy: Policy,
    *,
    measure: bool = False,
) -> Admission:
    """Que faut-il décharger pour faire tenir ce variant, et est-ce seulement possible ?"""
    parc = sorted(residents, key=lambda r: r.last_used)

    if measure:
        # Mode mesure : le parc est vidé, épinglés compris. Un profil mesuré avec
        # d'autres modèles en mémoire ne mesure pas le modèle, il mesure la machine.
        # Un job en cours, lui, n'est pas une préférence qu'on passe outre : c'est
        # un travail que l'éviction détruirait, et dont le banc ne saurait rien.
        # Le cas ne pouvait pas se poser tant qu'une commande tenait seule le
        # parc ; il se pose depuis qu'un serveur exécute (tâche 4.6).
        occupés = [r for r in parc if r.busy]
        if occupés:
            return Admission(
                admitted=False,
                reason=(
                    "le banc d'essai décharge tout le parc avant de mesurer, et "
                    f"{', '.join(r.ref for r in occupés)} a un job en cours — "
                    "l'évincer détruirait ce travail ; attendre la fin, ou "
                    "ecurie unload --force"
                ),
                blockers=tuple(r.ref for r in occupés),
                measure_mode=True,
            )
        return Admission(
            admitted=True,
            reason="mode mesure : parc entièrement déchargé avant la mesure",
            evict=tuple(r.ref for r in parc),
            headroom_bytes=policy.budget_bytes - (peak_bytes or 0),
            measure_mode=True,
        )

    if peak_bytes is None:
        return Admission(
            admitted=False,
            reason=(
                f"{ref} n'a pas de profil mesuré : son pic mémoire est inconnu. "
                f"Mesurer d'abord — ecurie bench {ref}"
            ),
        )

    déjà = next((r for r in parc if r.ref == ref), None)
    if déjà is not None:
        autres = sum(r.peak_bytes for r in parc if r.ref != ref)
        return Admission(
            admitted=True,
            reason="déjà résident",
            headroom_bytes=policy.budget_bytes - autres - peak_bytes,
            already_resident=True,
        )

    if peak_bytes > policy.budget_bytes:
        return Admission(
            admitted=False,
            reason=(
                f"{ref} demande {fmt_memory(peak_bytes)}, le budget entier est de "
                f"{fmt_memory(policy.budget_bytes)} : décharger ne changerait rien"
            ),
        )

    candidat_lourd = peak_bytes > policy.heavy_threshold_bytes
    if candidat_lourd and policy.max_heavy_resident < 1:
        return Admission(
            admitted=False,
            reason=(
                f"{ref} dépasse le seuil de {fmt_memory(policy.heavy_threshold_bytes)} et la "
                "politique du parc n'admet aucun modèle lourd résident "
                "(max_heavy_resident = 0) : relever le réglage dans ~/.ecurie/config.toml"
            ),
        )
    restants = list(parc)
    évincés: list[str] = []

    def occupé() -> int:
        return sum(r.peak_bytes for r in restants)

    def lourds() -> list[Resident]:
        return [r for r in restants if r.heavy(policy.heavy_threshold_bytes)]

    # 1. La règle du parc : un seul lourd à la fois. Elle prime sur l'arithmétique
    #    du budget — deux modèles lourds qui « tiennent » sur le papier laissent le
    #    système sans marge pour le reste de la machine.
    if candidat_lourd:
        while len(lourds()) >= policy.max_heavy_resident:
            victime = _lru_evictable(lourds())
            if victime is None:
                return _refus_parc(ref, lourds(), policy)
            restants.remove(victime)
            évincés.append(victime.ref)

    # 2. Puis le budget, en LRU.
    while occupé() + peak_bytes > policy.budget_bytes:
        victime = _lru_evictable(restants)
        if victime is None:
            return _refus_epingles(ref, restants, policy, peak_bytes)
        restants.remove(victime)
        évincés.append(victime.ref)

    if évincés:
        raison = "décharge " + ", ".join(évincés) + " (moins récemment utilisés)"
    else:
        raison = "tient dans le budget résiduel"
    return Admission(
        admitted=True,
        reason=raison,
        evict=tuple(évincés),
        headroom_bytes=policy.budget_bytes - occupé() - peak_bytes,
    )


def _lru_evictable(residents: Sequence[Resident]) -> Resident | None:
    candidats = [r for r in residents if not r.pinned and not r.busy]
    return min(candidats, key=lambda r: r.last_used) if candidats else None


def _immobiles(residents: Sequence[Resident]) -> tuple[str, ...]:
    """Ce qu'on ne peut pas décharger, et le mot qui dit pourquoi."""
    return tuple(
        f"{r.ref} ({'en cours de job' if r.busy else 'épinglé'})"
        for r in residents
        if r.pinned or r.busy
    )


def _refus_parc(ref: str, lourds: Sequence[Resident], policy: Policy) -> Admission:
    """Refus dû à la règle du parc, pas au budget — et il faut le dire ainsi.

    Emprunter le message du budget ici annoncerait « il manque 0 octets » et
    citerait comme bloquants des résidents légers qui n'ont aucune part au refus.
    Désépingler l'un d'eux ne débloquerait rien : seuls les lourds comptent.
    """
    bloquants = _immobiles(lourds)
    return Admission(
        admitted=False,
        reason=(
            f"{ref} est un modèle lourd et la politique n'en admet que "
            f"{policy.max_heavy_resident} à la fois ; celui qui occupe la place ne peut "
            f"pas partir ({', '.join(bloquants)}) — attendre, ou ecurie unload --force"
        ),
        blockers=tuple(r.ref for r in lourds if r.pinned or r.busy),
    )


def _refus_epingles(
    ref: str, restants: Sequence[Resident], policy: Policy, peak_bytes: int
) -> Admission:
    bloquants = _immobiles(restants)
    manque = max(0, sum(r.peak_bytes for r in restants) + peak_bytes - policy.budget_bytes)
    return Admission(
        admitted=False,
        reason=(
            f"{ref} ne tient pas : il manque {fmt_memory(manque)} et les résidents restants "
            f"ne peuvent pas partir ({', '.join(bloquants) or 'aucun'}) — "
            f"attendre la fin des jobs, ou ecurie unload --force"
        ),
        blockers=tuple(r.ref for r in restants if r.pinned or r.busy),
    )


def residual_bytes(residents: Iterable[Resident], policy: Policy) -> int:
    return policy.budget_bytes - sum(r.peak_bytes for r in residents)
