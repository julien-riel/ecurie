"""Traduction par un modèle vision-langage — `mlx-vlm`, sans image.

Jumeau de `workers.mlx_lm_translate`, dont il hérite la composition d'invite,
le nettoyage de la sortie et le refus de deviner la langue de départ. Seul le
moteur diffère.

C'est sur cette capacité que le mode « thinking » coupé par `SurMlxVlm` compte
le plus : une traduction se compare au caractère près à sa référence, et un
brouillon de raisonnement placé devant elle ferait chuter la note d'un modèle
qui a pourtant traduit juste.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).
"""

from ecurie_runtime.workers.base import main
from ecurie_runtime.workers.mlx_lm_translate import MlxLmTranslateWorker
from ecurie_runtime.workers.mlx_vlm_lm import SurMlxVlm


class MlxVlmTranslateWorker(SurMlxVlm, MlxLmTranslateWorker):
    """Traduction, sur le moteur de mlx-vlm."""

    name = "mlx-vlm-translate"


if __name__ == "__main__":
    raise SystemExit(main(MlxVlmTranslateWorker))
