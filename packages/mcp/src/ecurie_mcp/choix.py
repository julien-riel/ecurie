"""Quel variant sert une capacité, ici et maintenant.

La règle existait déjà, en TypeScript, dans l'Atelier (`apps/ui/src/ecrans/choix.ts`) :
le titulaire d'abord, à défaut le premier exécutable, **jamais un variant qui ne
l'est pas**. Elle n'avait pas d'équivalent Python parce qu'aucun appelant Python
n'avait à choisir : la CLI reçoit une référence explicite, et l'API la reçoit du
client. Un agent, lui, demande une capacité — « transcris ce fichier » — et non
un variant. C'est le premier endroit du projet où le serveur choisit.

`Registry.incumbent_for` ne suffit pas, et de deux façons : il ignore
l'exécutabilité — le titulaire d'`image-to-mesh` a 7,37 Go de poids qui peuvent
ne pas être sur ce disque —, et il rend `None` pour la plupart des capacités,
faute d'A/B qui aurait désigné un titulaire (tâche 5.6, gelée avec l'évaluation).
Onze des douze capacités promises sont dans ce cas : sans repli sur le premier
exécutable, le catalogue serait vide.

**L'exécutabilité se lit sur le disque, et ce n'est pas gratuit.** `inspect_variant`
résout les poids, vérifie le venv du runtime et le profil : trois lectures par
variant, soixante-douze variants au registre. Le catalogue les paie une fois au
démarrage ; un appel d'outil ne réexamine que le variant qu'il va lancer.
"""

from dataclasses import dataclass

from ecurie_core.config import Config
from ecurie_core.models import Model, Variant
from ecurie_core.registry import Registry
from ecurie_runtime.readiness import inspect_variant

from ecurie_mcp.contexte import Contexte


@dataclass(frozen=True)
class Retenu:
    """Le variant qui servira, et ce qu'il a fallu écarter pour en arriver là."""

    model: Model
    variant: Variant
    ref: str
    titulaire: bool
    ecartes: tuple[tuple[str, str], ...] = ()  # (ref, la première cause)

    @property
    def capability(self) -> str:
        return self.model.capability


def variants_de(registry: Registry, capability: str) -> list[tuple[Model, Variant]]:
    """Tous les variants déclarés pour cette capacité, titulaire en tête.

    L'ordre décide du repli : à défaut de titulaire, c'est le premier exécutable
    de cette liste qui sert. Le tri est donc stable et explicite — titulaire,
    puis `status: active` avant les candidats, puis l'ordre du registre — plutôt
    que l'ordre d'itération d'un dictionnaire, qui ferait dépendre le choix du
    nom des fichiers.
    """
    paires: list[tuple[Model, Variant]] = []
    for model in registry.models.values():
        if model.capability != capability:
            continue
        for variant in model.variants:
            paires.append((model, variant))

    def rang(paire: tuple[Model, Variant]) -> tuple[int, int]:
        model, _ = paire
        return (0 if model.incumbent else 1, 0 if model.status == "active" else 1)

    return sorted(paires, key=rang)


def choisir(
    root, config: Config, registry: Registry, capability: str
) -> Retenu | None:
    """Le variant qui sert cette capacité, ou None si aucun n'est exécutable.

    Rend aussi ce qui a été écarté et pourquoi : une capacité sans variant prêt
    doit pouvoir dire « les poids ne sont pas téléchargés » plutôt que de
    disparaître du catalogue sans un mot. C'est ce que `ecurie_catalog` affiche,
    et c'est la différence entre un outil absent et un outil qui explique
    comment le rendre présent.
    """
    ecartes: list[tuple[str, str]] = []
    for model, variant in variants_de(registry, capability):
        ref = f"{model.id}@{variant.id}"
        etat = inspect_variant(root, config, model, variant, ref)
        if etat.ready:
            return Retenu(
                model=model,
                variant=variant,
                ref=ref,
                titulaire=bool(model.incumbent),
                ecartes=tuple(ecartes),
            )
        ecartes.append((ref, etat.blockers[0] if etat.blockers else "indisponible"))
    return None


def choisir_dans(contexte: Contexte, capability: str) -> Retenu | None:
    """`choisir`, avec la racine et la config du contexte."""
    return choisir(contexte.root, contexte.config, contexte.registry(), capability)
