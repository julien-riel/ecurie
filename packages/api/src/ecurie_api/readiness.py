"""Un variant est-il exécutable, ici et maintenant — et sinon, que faire ?

Le manifeste déclare `tier: hot`, mais le disque a le dernier mot : un variant
peut être annoncé présent et ses poids avoir été mis en corbeille la veille. Le
§1.1 de la conception distingue l'état déclaré de l'état observé ; cette question
relève entièrement du second, et on ne la répond qu'en regardant.

Trois choses peuvent manquer, et elles n'appellent pas le même geste :

- les **poids**, absents ou à une autre révision → `ecurie pull` ;
- l'**environnement** du runtime, non déclaré ou non synchronisé → `ecurie env sync` ;
- le **profil mesuré**, sans lequel le contrôle d'admission refuse par principe
  plutôt que de risquer un swap → `ecurie bench`.

Les trois modules interrogés composent déjà leur message avec la commande qui
répare : on les transmet tels quels. Une UI qui affiche « indisponible » sans
dire lequel des trois manque envoie l'utilisateur deviner, et c'est exactement ce
que ce projet refuse de faire à la ligne de commande.

On rend **toutes** les causes, pas la première : un variant candidat fraîchement
ajouté au registre les cumule souvent toutes les trois, et les découvrir une par
une coûte trois allers-retours.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ecurie_core.config import Config
from ecurie_core.models import Model, Variant
from ecurie_runtime.envs import EnvError, spec_for_variant
from ecurie_store.weights import WeightsMissing, resolve_weights


@dataclass
class VariantReadiness:
    ready: bool
    blockers: list[str] = field(default_factory=list)
    weights_path: str | None = None


def inspect_variant(
    root: Path, config: Config, model: Model, variant: Variant, ref: str
) -> VariantReadiness:
    blockers: list[str] = []
    weights_path: str | None = None

    try:
        weights = resolve_weights(config, variant, ref=ref)
    except WeightsMissing as exc:
        blockers.append(str(exc))
    else:
        weights_path = str(weights.path)

    try:
        spec_for_variant(root, variant, ref=ref, capability=model.capability)
    except EnvError as exc:
        blockers.append(str(exc))

    if variant.profile is None:
        blockers.append(
            f"{ref} : aucun profil mesuré, donc pic mémoire inconnu — "
            f"l'admission refuse par principe ; mesurer avec ecurie bench {ref}"
        )

    return VariantReadiness(ready=not blockers, blockers=blockers, weights_path=weights_path)
