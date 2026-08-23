"""Génération de texte par un modèle vision-langage — `mlx-vlm`, sans image.

Même contrat, même invite et même sortie que `workers.mlx_lm` : ce qui change
est le moteur, et c'est `SurMlxVlm` qui s'en charge. Le corps de `infer` est
hérité sans une ligne de plus.

Un modèle multimodal servant `text-generation` n'est pas un détournement. Une
capacité décrit ce qui entre et ce qui sort — ici une invite et un texte —, pas
l'architecture qui l'honore. Qwen3.6-27B écrit avec la même tête de langue,
qu'on lui montre une image ou non ; lui refuser ce contrat parce qu'il sait
aussi voir reviendrait à trier les modèles sur ce qu'ils savent en trop.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).
"""

from ecurie_runtime.workers.base import main
from ecurie_runtime.workers.mlx_lm import MlxLmWorker
from ecurie_runtime.workers.mlx_vlm_lm import SurMlxVlm


class MlxVlmTextWorker(SurMlxVlm, MlxLmWorker):
    """Génération de texte, sur le moteur de mlx-vlm."""

    name = "mlx-vlm-text"


if __name__ == "__main__":
    raise SystemExit(main(MlxVlmTextWorker))
