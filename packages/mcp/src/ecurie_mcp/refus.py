"""Un refus d'admission est une donnée, pas un message (CONCEPTION.md §6.3).

La règle de la CLI — chaque erreur porte la commande qui répare — se transpose :
chaque refus porte les options que l'agent peut exécuter, chiffrées à partir de
la même décision d'admission. L'éviction LRU des résidents ni épinglés ni occupés
étant automatique, un refus ne survient que quand il ne reste rien à évincer, et
c'est exactement ce que le payload raconte.

**Le refus vient de la décision, il n'est pas recomposé après coup.** C'est ce
qui a demandé d'ajouter `JobOutcome.admission` : resimuler l'admission après
l'échec pour en reconstituer les chiffres donnerait une seconde vérité, mesurée à
un autre instant — entre les deux, un job a pu finir et libérer la place, et
l'agent lirait des options qui ne correspondent à rien de ce qui lui a été
refusé.

**Ce qui manque à l'exemple de la conception, et pourquoi.** Le §6.3 montre un
champ `basis` (`measured-local`, `inherited-class`) sur le pic demandé et sur les
variants proposés. Ce champ n'existe nulle part dans le code : les trois états de
l'admission sont la tâche 2.3, au jalon J2. `Profile` ne porte que `measured_on`
— le nom de la machine qui a mesuré — et `measured_at`. Le payload l'**omet**
donc plutôt que d'écrire `"measured-local"` en dur, qui serait faux pour tout
utilisateur ayant récupéré un profil mesuré ailleurs. C'est précisément la faute
que J0 a corrigée dans trois documents : un dépôt qui affirme ce qu'il n'a pas
vérifié se trompe aussi sûrement qu'un dépôt qui promet ce qu'il n'a pas fait.

**Le refus parle anglais.** C'est la surface produit. La phrase française que
compose `admission.py` reste celle du terminal et du manifeste ; ici, les
chiffres sont rendus tels quels et les mots sont réécrits.
"""

from dataclasses import dataclass
from typing import Any

from ecurie_core.capabilities import CapabilityContract
from ecurie_core.format import fmt_memory
from ecurie_core.models import Model, Variant
from ecurie_core.registry import Registry
from ecurie_runtime.admission import Admission
from ecurie_runtime.readiness import inspect_variant
from ecurie_runtime.residents import ResidentEntry
from ecurie_runtime.supervisor import Supervisor

CODE = "admission_refused"

# En deçà, proposer de réduire un paramètre est une politesse sans contenu : on
# demanderait à l'agent de diviser son entrée par dix pour gagner de quoi ne pas
# tenir quand même. Le seuil est celui d'une marge qui vaut le geste.
PLANCHER_REDUCTION = 0.1


@dataclass(frozen=True)
class Demande:
    """Ce qui a été demandé, et par quoi il aurait été servi.

    `values` est l'entrée **résolue** — défauts du contrat et du variant
    fusionnés —, celle-là même sur laquelle l'admission a décidé. Les arguments
    bruts de l'agent donneraient un autre pic, et le payload se contredirait.
    """

    capability: str
    ref: str
    peak_bytes: int | None
    values: dict[str, Any]
    contract: CapabilityContract | None = None


def payload(
    supervisor: Supervisor,
    registry: Registry,
    demande: Demande,
    admission: Admission,
    *,
    root,
    config,
) -> dict[str, Any]:
    """Le refus §6.3, chiffré, avec ses options exécutables."""
    residents = supervisor.residents()
    budget = supervisor.budget.bytes

    charge: dict[str, Any] = {
        "error": CODE,
        "reason": _raison_anglaise(demande, admission, budget, residents),
        # La phrase que le contrôle d'admission compose, telle quelle. Elle est
        # écrite pour un humain devant un terminal — elle est française, et elle
        # propose `--hors-budget`, un drapeau que l'agent ne doit jamais prendre
        # de lui-même. Elle voyage quand même : c'est elle que porte le manifeste
        # du job, et pouvoir rapprocher les deux vaut mieux que deux récits.
        "cli_reason": admission.reason,
        "requested": {
            "capability": demande.capability,
            "variant": demande.ref,
            "peak_bytes": demande.peak_bytes,
        },
        "budget_bytes": budget,
        "residents": [
            {
                "variant": entrée.ref,
                "peak_bytes": entrée.peak_bytes,
                "pinned": entrée.pinned,
                "busy": entrée.busy,
            }
            for entrée in residents
        ],
        "options": _options(
            supervisor, registry, demande, admission, residents, root=root, config=config
        ),
    }
    return charge


def _raison_anglaise(
    demande: Demande,
    admission: Admission,
    budget: int,
    residents: list[ResidentEntry],
) -> str:
    """Le refus, en anglais et en chiffres — c'est la surface produit.

    Le §6.3 le demande en toutes lettres : « le refus MCP parle anglais ». La
    phrase de `admission.py` reste celle du terminal et du manifeste, et elle a
    deux défauts ici qui n'en sont pas là-bas : elle est française, et elle
    propose `--hors-budget` — un drapeau que seul un humain prend, jamais l'agent
    qui lit ce payload. La donner telle quelle reviendrait à écrire dans la
    surface produit une instruction qu'on interdit par ailleurs.

    Les chiffres, eux, ne sont pas retraduits : ce sont les mêmes octets, pris
    sur la même décision.
    """
    pic = demande.peak_bytes
    if pic is None:
        return (
            f"{demande.ref} has no measured memory profile, so admission refuses rather "
            "than risk a swap. A human must bench it first."
        )
    if admission.overflow_bytes:
        return (
            f"{demande.ref} needs {fmt_memory(pic)} and the whole Metal budget is "
            f"{fmt_memory(budget)}: unloading everything would not be enough — "
            f"{fmt_memory(admission.overflow_bytes)} short."
        )
    immobiles = [e for e in residents if e.pinned or e.busy]
    if immobiles:
        détail = ", ".join(
            f"{e.ref} ({fmt_memory(e.peak_bytes)}, "
            f"{'busy with a job' if e.busy else 'pinned by its human'})"
            for e in immobiles
        )
        return (
            f"{demande.ref} needs {fmt_memory(pic)} of the {fmt_memory(budget)} budget, "
            f"and what holds the memory cannot be freed: {détail}."
        )
    return (
        f"{demande.ref} needs {fmt_memory(pic)} and does not fit the "
        f"{fmt_memory(budget)} budget."
    )


def _options(
    supervisor: Supervisor,
    registry: Registry,
    demande: Demande,
    admission: Admission,
    residents: list[ResidentEntry],
    *,
    root,
    config,
) -> list[dict[str, Any]]:
    """Ce que l'agent peut faire, dans l'ordre où cela lui coûte le moins.

    Attendre d'abord — c'est gratuit et cela ne change rien au résultat ;
    changer de variant ensuite — le résultat change, la capacité non ; réduire
    l'entrée après — c'est le résultat demandé qu'on entame ; relayer à un
    humain en dernier, parce que c'est le seul qui sorte de la boucle de l'agent.
    """
    options: list[dict[str, Any]] = []

    if demande.peak_bytes is None:
        # Aucun profil : ce refus-là ne se négocie pas, il se mesure. Il est le
        # seul que le mode hors budget lui-même ne force pas — on ne peut pas
        # assumer un dépassement dont on ignore la taille.
        options.append(
            {
                "kind": "human_command",
                "command": f"ecurie bench {demande.ref}",
                "why": "this variant has no measured profile, so its memory peak is unknown; "
                "admission refuses rather than risk a swap",
            }
        )
        return options

    options.extend(_attendre(supervisor, demande, admission, residents))
    options.extend(_autres_variants(supervisor, registry, demande, root=root, config=config))
    reduction = _reduire(supervisor, demande, admission, residents)
    if reduction is not None:
        options.append(reduction)
    options.extend(_relayer(residents, admission))

    if not options:
        # Un refus sans issue reste un refus qu'il faut savoir lire. Le cas
        # existe et il est même le plus simple : une capacité servie par un seul
        # variant dont le pic dépasse le budget entier, sur un parc vide. Rien
        # n'est à évincer, aucun voisin n'existe, aucune pente à réduire — et
        # rendre `options: []` laisserait l'agent boucler sur le même appel. La
        # seule voie restante sort de sa boucle, et c'est celle qu'on nomme.
        options.append(
            {
                "kind": "human_command",
                "command": f"ecurie run {demande.ref} --hors-budget",
                "why": "nothing can be freed and no lighter variant is installed. Running "
                "over budget pages to disk and slows the whole machine, so it is a "
                "human's call, never yours: relay this and let them decide.",
            }
        )
    return options


def _attendre(
    supervisor: Supervisor,
    demande: Demande,
    admission: Admission,
    residents: list[ResidentEntry],
) -> list[dict[str, Any]]:
    """« Réessayer quand le job en cours finit », et ce que cela libérerait.

    Trois conditions, et chacune vient d'un cas où l'option ne réparait rien.

    **Attendre un résident épinglé ne libère rien.** Les deux champs sont
    indépendants : un worker peut être à la fois épinglé par son humain et
    occupé par un job. La fin du job rend le worker libre, pas sa mémoire —
    l'épingle, elle, tient toujours. L'agent réessaierait pour se voir refuser à
    l'identique.

    **Attendre ne sert à rien quand le candidat dépasse le budget entier.** La
    décision le dit alors elle-même (« décharger ne changerait rien ») et pose un
    `overflow_bytes` : vider tout le parc ne suffirait pas.

    **Et il faut que l'attente suffise.** `frees_bytes` se compare au manque : un
    job qui libérera deux gigaoctets quand il en manque huit n'est pas une issue,
    c'est un espoir. On additionne donc ce que les attentes libéreraient, et on
    ne propose que si le total franchit le manque.

    L'identité du job occupant n'est pas publiée — `residents.json` ne porte que
    le pid de qui le tient — d'où un `when` qui nomme le variant. Cela suffit :
    l'agent ne peut de toute façon agir que sur l'attente.
    """
    if admission.overflow_bytes:
        return []
    libérables = [e for e in residents if e.busy and not e.pinned]
    if not libérables:
        return []

    manque = _manque(supervisor, demande, residents)
    if manque > sum(e.peak_bytes for e in libérables):
        # Même en attendant tout ce qui finit, on ne tiendrait pas.
        return []

    return [
        {
            "kind": "retry",
            "when": f"the job running on {entrée.ref} ends",
            "frees_bytes": entrée.peak_bytes,
        }
        for entrée in libérables
    ]


def _manque(
    supervisor: Supervisor, demande: Demande, residents: list[ResidentEntry]
) -> int:
    """Combien d'octets il faudrait libérer pour que ce job entre.

    Ce que les résidents immobiles — épinglés ou occupés — retiennent, plus le
    pic demandé, moins le budget. Zéro quand la place existe déjà, auquel cas le
    refus vient d'ailleurs que de l'arithmétique.
    """
    immobiles = sum(e.peak_bytes for e in residents if e.pinned or e.busy)
    return max(0, immobiles + (demande.peak_bytes or 0) - supervisor.budget.bytes)


def _autres_variants(
    supervisor: Supervisor,
    registry: Registry,
    demande: Demande,
    *,
    root,
    config,
) -> list[dict[str, Any]]:
    """Les variants de la même capacité qui tiendraient, eux.

    Seuls les exécutables sont proposés : suggérer un variant dont les poids ne
    sont pas téléchargés remplacerait un refus par un autre, une minute plus
    tard. `fits_now` vient de `simulate`, qui ne pose aucun verrou — c'est une
    estimation à cet instant, et c'est bien ce que l'agent demande.
    """
    trouvés: list[dict[str, Any]] = []
    for model, variant in _memes_capacites(registry, demande):
        ref = f"{model.id}@{variant.id}"
        if ref == demande.ref:
            continue
        pic = supervisor.peak_bytes(variant, demande.values)
        if pic is None:
            continue
        if not inspect_variant(root, config, model, variant, ref).ready:
            continue
        décision = supervisor.simulate(ref, pic)
        trouvés.append(
            {
                "kind": "variant",
                "ref": ref,
                "peak_bytes": pic,
                "fits_now": décision.admitted,
            }
        )
    # Ce qui tient d'abord, puis le plus léger : l'agent lit la première ligne.
    trouvés.sort(key=lambda o: (not o["fits_now"], o["peak_bytes"]))
    return trouvés


def _memes_capacites(registry: Registry, demande: Demande) -> list[tuple[Model, Variant]]:
    paires: list[tuple[Model, Variant]] = []
    for model in registry.models.values():
        if model.capability != demande.capability:
            continue
        for variant in model.variants:
            paires.append((model, variant))
    return paires


def _reduire(
    supervisor: Supervisor,
    demande: Demande,
    admission: Admission,
    residents: list[ResidentEntry],
) -> dict[str, Any] | None:
    """Jusqu'où l'entrée devrait descendre pour tenir — quand la question a un sens.

    Elle n'en a un que si le profil du variant déclare une pente : un modèle dont
    le pic ne dépend pas de l'entrée ne se réduit pas, et lui demander une image
    plus petite ne changerait pas un octet. Les trois pentes du parc portent sur
    une durée (`max_seconds`, `segment_seconds`) ou une longueur de contexte —
    l'exemple du §6.3, qui réduit une `width`, ne correspond à aucun variant
    réel.

    **L'inversion est bornée par l'intervalle mesuré**, et cette borne est le
    point entier : `expected_peak` pose un plancher au pic mesuré hors de son
    intervalle, si bien qu'une inversion naïve rendrait une valeur qui ne tient
    pas. Une extrapolation vers le bas est une estimation, et ce projet refuse
    d'admettre sur une estimation.

    **La règle du parc prime sur l'arithmétique**, et l'oublier fait proposer une
    valeur qui sera refusée à son tour : `plan_admission` applique d'abord
    `max_heavy_resident` — un seul modèle lourd résident — et cette règle ne se
    négocie pas en réduisant l'entrée. Quand le refus vient de là, ou d'un
    dépassement du budget entier, il n'y a rien à réduire.

    **Et la valeur proposée doit être du type que le contrat accepte** : proposer
    17,6 images à un paramètre déclaré `integer` est un conseil que le schéma
    rejettera. On plancher vers l'entier inférieur, qui tient par construction.
    """
    if admission.overflow_bytes:
        # Le candidat dépasse le budget à lui seul : la décision dit elle-même
        # que décharger ne changerait rien, et réduire l'entrée d'un modèle dont
        # le pic de base excède déjà le budget ne le ferait pas entrer.
        return None
    if _refus_de_regle(admission):
        # Le refus vient de « un seul modèle lourd résident », qui prime sur
        # l'arithmétique du budget (admission.py). Une entrée plus petite ne
        # change pas la règle : la valeur proposée serait refusée à son tour.
        return None

    variant = _variant_demandé(supervisor, demande)
    pente = variant.profile.peak_scaling if variant and variant.profile else None
    if pente is None or pente.bytes_per_unit <= 0:
        return None

    libre = supervisor.budget.bytes - sum(
        e.peak_bytes for e in residents if e.pinned or e.busy
    )
    marge = libre - pente.base_bytes
    if marge <= 0:
        return None

    admissible = marge / pente.bytes_per_unit
    # `measured_range` n'est qu'une paire de flottants : rien dans le modèle ni
    # dans le schéma n'impose l'ordre, et `extrapolates` prend d'ailleurs min et
    # max plutôt que les positions. On fait de même.
    plancher, plafond = min(pente.measured_range), max(pente.measured_range)
    if admissible < plancher * (1 + PLANCHER_REDUCTION):
        # Sous l'intervalle mesuré : on ne sait pas ce que coûterait cette
        # valeur, et le dire serait inventer un chiffre.
        return None
    admissible = min(admissible, plafond)

    courante = demande.values.get(pente.parameter)
    if isinstance(courante, int | float) and admissible >= courante:
        return None  # réduire ce paramètre ne débloquerait rien

    proposée = _au_type_du_parametre(demande, pente.parameter, admissible)
    if proposée is None:
        return None

    return {
        "kind": "reduce_input",
        "parameter": pente.parameter,
        "max_admissible": proposée,
    }


def _refus_de_regle(admission: Admission) -> bool:
    """Le refus vient-il de la règle du parc plutôt que du budget ?

    `Admission` ne porte pas de code : sa raison est une phrase, composée pour
    être lue par un humain. `_refus_parc` est la seule à écrire « la politique
    n'en admet que », et c'est ce que l'on reconnaît ici. Rapprocher deux textes
    est fragile ; y ajouter un code d'énumération serait le geste juste, et il
    appartient au socle — cette fonction est le repère qui le demandera.
    """
    return "la politique n'en admet que" in admission.reason


def _au_type_du_parametre(demande: Demande, parametre: str, valeur: float):
    """La valeur proposée, dans le type que le contrat accepte pour ce champ.

    Un paramètre déclaré `integer` — `max_frames`, `steps` — ne se réduit pas à
    17,6 : le schéma rejetterait la valeur qu'on vient de conseiller, et l'agent
    paierait un aller-retour pour l'apprendre. On plancher vers l'entier
    inférieur, qui tient par construction puisque le pic croît avec la valeur.

    Rend None quand l'arrondi ferait tomber la proposition à zéro ou en dessous :
    conseiller « zéro image » n'est pas une réduction, c'est un autre refus.
    """
    champ = (demande.contract.input_properties.get(parametre) or {}) if demande.contract else {}
    if champ.get("type") == "integer":
        entier = int(valeur)  # troncature, donc vers le bas
        return entier if entier >= 1 else None
    arrondie = round(valeur, 2)
    return arrondie if arrondie > 0 else None


def _variant_demandé(supervisor: Supervisor, demande: Demande) -> Variant | None:
    for model in supervisor.registry.models.values():
        if model.capability != demande.capability:
            continue
        for variant in model.variants:
            if f"{model.id}@{variant.id}" == demande.ref:
                return variant
    return None


def _relayer(residents: list[ResidentEntry], admission: Admission) -> list[dict[str, Any]]:
    """L'épingle est une préférence humaine : l'agent transmet, il ne lève pas.

    C'est la seule option que le serveur refuse d'exécuter lui-même, et le refus
    est de principe : un agent qui saurait déloger ce qu'un humain a épinglé
    aurait, de fait, le pouvoir de décharger n'importe quoi — et l'épingle ne
    voudrait plus rien dire.
    """
    return [
        {
            "kind": "human_command",
            "command": f"ecurie unload {entrée.ref}",
            "why": "pinned by its human — relay this, do not decide it",
        }
        for entrée in residents
        if entrée.pinned and entrée.ref in admission.blockers
    ]
