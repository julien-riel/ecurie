"""Socle commun des adaptateurs `torch-vision` : device MPS, mémoire, images.

Deux capacités s'y appuient — le détourage et l'agrandissement — et elles ne
partagent que cela : la pile PyTorch/MPS, la façon de mesurer ce qu'elle consomme,
et le soin à apporter aux images en entrée. Tout le reste leur est propre.

La mesure mérite d'être répétée ici, parce que s'y tromper coûte l'OOM que le
contrôle d'admission existe pour empêcher : **le RSS ne compte pas la mémoire
Metal**. Relevé le 20 août 2026 sur SDXL, `ru_maxrss` plafonnait à 0,42 Gio
pendant que le pilote en réservait 15,95. Et `driver_allocated_memory` est
instantané : il redescend aussi vite qu'il monte, donc le maximum se tient à
chaque relevé plutôt que se lit une fois à la fin.

Rien de torch n'est importé au niveau du module (voir `workers/__init__.py`).
"""

import gc
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import Worker, WorkerError, peak_rss_bytes

ENV_NAME = "torch-vision"
REPAIR = f"ecurie env sync {ENV_NAME}"

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def import_torch() -> Any:
    """Importe torch, ou explique comment réparer l'environnement."""
    try:
        import torch
    except ImportError as exc:
        raise WorkerError(
            f"runtime torch indisponible dans cet environnement ({exc}) — `{REPAIR}`"
        ) from exc
    return torch


def import_pil() -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise WorkerError(f"Pillow absent de l'environnement ({exc}) — `{REPAIR}`") from exc
    return Image


def resolve_image(valeur: Any, job_dir: Path, champ: str = "image") -> Path:
    """Le chemin de l'image, relatif au dossier du job quand il l'est.

    Le superviseur copie l'entrée dans le dossier du job et transmet un chemin
    relatif — c'est ce qui rend le job rejouable ailleurs. Un chemin absolu reste
    accepté : le banc d'essai en passe.

    `champ` nomme l'entrée fautive, comme chez `uniface_base` et pour la même
    raison : l'empreinte visuelle en reçoit deux (`image` et `compare_to`), et un
    message qui parlerait toujours d'`image` enverrait corriger le mauvais champ.
    """
    brut = str(valeur or "").strip()
    if not brut:
        raise WorkerError(f"aucune image en entrée : le champ `{champ}` est vide")
    chemin = Path(brut)
    if not chemin.is_absolute():
        chemin = job_dir / chemin
    if not chemin.is_file():
        raise WorkerError(f"{champ} introuvable : {chemin}")
    if chemin.suffix.lower() not in IMAGES:
        raise WorkerError(
            f"format non géré : {chemin.suffix or '(sans extension)'} — "
            f"attendu {', '.join(sorted(IMAGES))}"
        )
    return chemin


def weights_dir(variant: dict[str, Any]) -> Path:
    """Le dossier de poids transmis par le superviseur, vérifié avant usage.

    `from_pretrained` ne court-circuite le réseau que sur un **dossier** : sur une
    chaîne quelconque contenant un « / », il la prend pour un identifiant de dépôt
    et échoue par un message qui parle du Hub plutôt que des poids manquants.
    """
    brut = str(variant.get("weights_path") or "").strip()
    if not brut:
        raise WorkerError("aucun chemin de poids transmis par le superviseur")
    chemin = Path(brut)
    if not chemin.is_dir():
        raise WorkerError(
            f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
            "déjà vérifié, un worker ne télécharge jamais"
        )
    return chemin


class TorchVisionWorker(Worker):
    """Base des adaptateurs de vision : device, compteurs mémoire, déchargement."""

    def __init__(self) -> None:
        self.torch: Any = None
        self.model: Any = None
        self.defaults: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self._peak_driver: int = 0

    # --- device --------------------------------------------------------------

    def ensure_mps(self, torch: Any) -> None:
        if not torch.backends.mps.is_available():
            raise WorkerError(
                "backend MPS indisponible (torch.backends.mps.is_available() est faux) — "
                "cet adaptateur ne sert que sur Apple Silicon ; vérifier que "
                f"runtimes/{ENV_NAME}/.venv utilise un Python arm64"
            )

    # --- réglages ------------------------------------------------------------

    def reglage(self, request: Any, nom: str, defaut: Any) -> Any:
        """Entrée du job, puis options du variant, puis défauts du manifeste."""
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        for couche in (self.options, self.defaults):
            if couche.get(nom) is not None:
                return couche[nom]
        return defaut

    # --- mémoire -------------------------------------------------------------

    def mps_counters(self) -> dict[str, int]:
        """Compteurs MPS instantanés. Aucun n'est un pic — les noms le disent.

        Tout relevé nourrit au passage le maximum retenu pour le profil : c'est
        la seule façon d'attraper une pointe que le pilote a déjà rendue quand le
        worker répond au protocole.
        """
        mps = getattr(self.torch, "mps", None) if self.torch is not None else None
        if mps is None:
            return {}
        try:
            compteurs = {
                "mps_current_allocated_bytes": int(mps.current_allocated_memory()),
                "mps_driver_allocated_bytes": int(mps.driver_allocated_memory()),
                "mps_recommended_max_bytes": int(mps.recommended_max_memory()),
            }
        except (AttributeError, RuntimeError):
            return {}
        self._peak_driver = max(self._peak_driver, compteurs["mps_driver_allocated_bytes"])
        return compteurs

    def peak_memory_bytes(self) -> int | None:
        self.mps_counters()
        return max(self._peak_driver, peak_rss_bytes() or 0) or None

    def unload(self) -> None:
        """Rend la mémoire au budget, pas seulement à Python.

        Sans `empty_cache`, l'allocateur MPS garde ses pools : le processus a bien
        libéré ses tenseurs, le pilote tient toujours les octets, et le résident
        suivant se voit refuser l'admission pour une mémoire que plus personne
        n'utilise.
        """
        self.model = None
        if self.torch is not None:
            gc.collect()
            try:
                self.torch.mps.empty_cache()
            except (AttributeError, RuntimeError):
                pass

    # --- versions ------------------------------------------------------------

    def versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for nom, module in (("torch", "torch"), ("transformers", "transformers")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions
