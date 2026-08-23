"""Les trois capacités de texte de `mlx-lm`, servies par le moteur de `mlx-vlm`.

Ce module ne sert aucune capacité : il porte ce que les trois adaptateurs
voisins — `mlx_vlm_text`, `mlx_vlm_translate`, `mlx_vlm_tools` — ont en commun,
et rien d'autre.

**Pourquoi il existe.** Un modèle vision-langage est d'abord un modèle de langue.
Qwen3.6-27B écrit, traduit et appelle des outils exactement comme le fait
Qwen3-4B, et le parc avait déjà trois adaptateurs pour cela. Ils étaient
pourtant hors d'atteinte : `WORKER_MODULES_BY_CAPABILITY` les enregistre sous le
runtime `mlx-lm`, dont la version installée ne connaît pas l'architecture
`qwen3_5` — elle ne charge pas ces poids. Le modèle savait faire, le parc ne
savait pas le lui demander.

**Ce qui les sépare vraiment.** Une ligne. `mlx_vlm.stream_generate` a la même
signature que celui de `mlx-lm` à deux détails près : il attend le *processor*
là où l'autre attend le tokenizer, et il veut savoir qu'aucune image ne
l'accompagne. Tout le reste — le gabarit de conversation, l'échantillonneur, la
pénalité de répétition, le comptage des jetons, la mesure du pic — est
identique, jusqu'aux noms des paramètres. Recopier `MlxLmBase` pour cela aurait
produit trois cents lignes d'un jumeau qui aurait divergé au premier correctif ;
c'est `_flux` et `_import_runtime` qui sont surchargés, et eux seuls.

Le gain se lit au compte : les trois adaptateurs qui suivent font une trentaine
de lignes chacun, dont l'essentiel est leur en-tête.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).

Écrit contre `mlx-vlm` 0.6.15, dont `models/qwen3_5/` porte l'attention hybride
de cette famille. La borne du pyproject de l'env verrouille l'hypothèse.
"""

import gc
from typing import Any

from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.mlx_lm import Runtime

ENV_NAME = "mlx-vlm"
REPAIR = f"ecurie env sync {ENV_NAME}"


def import_runtime() -> Runtime:
    """Importe mlx-vlm sous les traits du `Runtime` de mlx-lm, ou dit comment réparer.

    Les points d'entrée portent les mêmes noms et les mêmes paramètres dans les
    deux bibliothèques — `make_sampler(temp=…, top_p=…)`,
    `make_logits_processors(repetition_penalty=…)`. C'est ce qui permet à
    `MlxLmBase` de les employer sans savoir lequel des deux il tient.
    """
    try:
        import mlx.core as mx
        from mlx_vlm import load, stream_generate
        from mlx_vlm.sample_utils import make_logits_processors, make_sampler
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-vlm indisponible dans cet environnement ({exc}) — `{REPAIR}`"
        ) from exc
    return Runtime(
        mx=mx,
        load=load,
        stream_generate=stream_generate,
        make_sampler=make_sampler,
        make_logits_processors=make_logits_processors,
    )


class SurMlxVlm:
    """Ce qu'il faut changer à un adaptateur `mlx-lm` pour qu'il tourne sur `mlx-vlm`.

    À gauche de la classe de base dans l'ordre d'héritage, pour que ses
    surcharges l'emportent.
    """

    def __init__(self) -> None:
        super().__init__()
        # Le processor et le tokenizer sont deux objets distincts ici, et chacun
        # a son emploi : le premier va au moteur, qui sait en tirer les entrées
        # multimodales ; le second applique le gabarit de conversation et porte
        # `eos_token`, dont le nettoyage de fin de tour a besoin. Confondre les
        # deux marche jusqu'au jour où l'un des deux manque.
        self._processor: Any = None
        self._thinking = False

    def _import_runtime(self) -> Runtime:
        return import_runtime()

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        annonce = super().load(variant)  # type: ignore[misc]
        processor = self._tokenizer  # type: ignore[attr-defined]
        self._processor = processor
        self._tokenizer = getattr(processor, "tokenizer", processor)  # type: ignore[attr-defined]
        # Le raisonnement à voix haute est coupé par défaut, et c'est un choix
        # qui se défend capacité par capacité. Qwen3.6 pense par défaut ; sur ces
        # trois contrats, ce brouillon ne sert à rien et coûte deux fois : il
        # consomme le `max_tokens` de la réponse, et il s'intercale devant elle,
        # là où un extracteur d'appel d'outil ou une note de traduction
        # l'attendent. Un manifeste qui veut l'inverse pose `thinking: true` dans
        # les `options` de son variant.
        options = self._options  # type: ignore[attr-defined]
        défauts = self._defaults  # type: ignore[attr-defined]
        self._thinking = bool(options.get("thinking", défauts.get("thinking", False)))
        return {**annonce, "versions": self._versions(), "thinking": self._thinking}

    def engendrer(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        """Impose la politique de raisonnement quand l'appelant ne s'est pas prononcé.

        Ici plutôt que dans chacun des trois `infer` : ils sont hérités tels
        quels de leurs jumeaux `mlx-lm`, et c'est tout l'intérêt.
        """
        kwargs.setdefault("thinking", self._thinking)
        return super().engendrer(messages, **kwargs)  # type: ignore[misc]

    def unload(self) -> None:
        self._processor = None
        super().unload()  # type: ignore[misc]
        gc.collect()

    def _flux(
        self,
        invite: Any,
        *,
        max_tokens: int,
        sampler: Any,
        logits_processors: Any = None,
    ) -> Any:
        """La seule ligne qui sépare vraiment les deux runtimes.

        `image=None` n'est pas une précaution : sans lui, le moteur cherche des
        jetons d'image dans une invite qui n'en porte pas.
        """
        runtime = self._runtime  # type: ignore[attr-defined]
        if runtime is None:
            raise WorkerError("modèle non chargé")
        return runtime.stream_generate(
            self._model,  # type: ignore[attr-defined]
            self._processor,
            invite,
            image=None,
            max_tokens=int(max_tokens),
            sampler=sampler,
            **({"logits_processors": logits_processors} if logits_processors else {}),
        )

    def _versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for nom, module in (("mlx", "mlx.core"), ("mlx-vlm", "mlx_vlm")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions
