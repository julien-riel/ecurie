"""Appel d'outils par un modèle vision-langage — `mlx-vlm`, sans image.

Jumeau de `workers.mlx_lm_tools`, dont il hérite l'extracteur tolérant, la
validation superficielle des arguments et la métrique `template_tools`. Écurie
n'exécute toujours aucun outil : elle juge le choix et le remplissage.

Deux choses ont dû être réglées ailleurs pour que cet héritage suffise, et
aucune des deux n'appartient à ce fichier :

- le gabarit de Qwen3.6 rend ses appels en **XML imbriqué** et non en JSON.
  L'extracteur de `mlx_lm_tools` a gagné une stratégie `xml_function`, qui
  profite aussi aux modèles servis par `mlx-lm` ;
- le modèle **raisonne à voix haute** par défaut. Le brouillon s'intercalait
  devant l'appel ; `SurMlxVlm` le coupe à la racine, et ce qui en réchapperait
  est séparé de la réponse par `sans_raisonnement`.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).
"""

from ecurie_runtime.workers.base import main
from ecurie_runtime.workers.mlx_lm_tools import MlxLmToolsWorker
from ecurie_runtime.workers.mlx_vlm_lm import SurMlxVlm


class MlxVlmToolsWorker(SurMlxVlm, MlxLmToolsWorker):
    """Appel d'outils, sur le moteur de mlx-vlm."""

    name = "mlx-vlm-tools"


if __name__ == "__main__":
    raise SystemExit(main(MlxVlmToolsWorker))
