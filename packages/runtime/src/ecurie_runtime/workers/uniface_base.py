"""Socle commun des adaptateurs `uniface` : poids locaux, détection, mémoire.

Six capacités s'y appuient — détection, points clés, régions, empreinte
d'identité, orientation de la tête, direction du regard. Elles n'ont en commun
que la pile (onnxruntime, sans torch ni MLX) et une contrainte de structure qui
mérite d'être dite ici plutôt que répétée six fois : **cinq de ces six capacités
ont besoin d'un détecteur avant leur propre modèle.** PIPNet décrit un visage
qu'on lui donne, il ne le cherche pas ; BiSeNet découpe un visage cadré ; ArcFace
encode un visage redressé sur ses cinq points. Un variant de ces capacités porte
donc deux jeux de poids, et le manifeste les nomme tous les deux.

**Le worker ne télécharge pas, et uniface voudrait le faire.** La bibliothèque
résout ses poids elle-même : elle les cherche dans `UNIFACE_CACHE_DIR`, et les
rapatrie de GitHub Releases ou du miroir Hugging Face quand ils manquent. Deux
gestes rendent ce chemin inerte. On pose `UNIFACE_CACHE_DIR` sur le dossier de
poids que le superviseur a transmis — c'est un instantané `ecurie pull`, et les
fichiers y portent déjà le nom qu'uniface leur donne. Puis on vérifie soi-même
présence **et** empreinte avant de construire quoi que ce soit : `verify_model_weights`
efface un fichier dont le SHA-256 ne correspond pas, puis retélécharge, ce qui
d'un instantané versionné ferait un dossier que plus personne ne sait dater.
Contrairement à `rtmlib`, il n'y a ici aucune dette de provenance : les poids
sont sur Hugging Face sous une forme que `ecurie pull` sait prendre, et le
manifeste les épingle à la révision près.

**Le pic se mesure au RSS, et c'est le second cas du parc où il dit vrai.**
Partout ailleurs le RSS ignore la mémoire Metal — le v0.3 l'a payé d'un facteur
38. Ici il n'y a pas de mémoire Metal : onnxruntime tourne sur CoreML ou sur le
CPU, et tout ce qu'il alloue est dans le RSS du processus.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    Worker,
    WorkerError,
    peak_rss_bytes,
)

ENV_NAME = "uniface"
REPAIR = f"ecurie env sync {ENV_NAME}"

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# Le détecteur employé quand le manifeste n'en nomme aucun, et le choix n'est pas
# celui qu'on attendrait. Ce n'est ni le plus gros ni le plus récent : MESURÉ sur
# la charge type, `retinaface_mnet050` trouve les quatre visages aux trois
# définitions, là où `mnet_v2` en trouve deux à 320, **un** à 640 et quatre à
# 1280 — une réponse non monotone, donc inutilisable en amont d'une autre
# capacité. Un détecteur qui manque un visage ne se rattrape pas : les points
# clés, les régions et l'empreinte de ce visage-là n'existeront pas.
#
# SCRFD est écarté pour une autre raison, et elle vaut d'être dite : ses poids
# viennent d'InsightFace, dont le model zoo réserve **tous** les modèles à la
# recherche non commerciale.
DETECTEUR_PAR_DEFAUT = "retinaface_mnet050"


def import_runtime() -> tuple[Any, Any, Any]:
    """cv2, numpy et uniface, ou la commande qui répare l'environnement."""
    try:
        import cv2
        import numpy as np
        import uniface
    except ImportError as exc:
        raise WorkerError(
            f"runtime uniface indisponible dans cet environnement ({exc}) — `{REPAIR}`"
        ) from exc
    return cv2, np, uniface


def weights_dir(variant: dict[str, Any]) -> Path:
    """Le dossier de poids transmis par le superviseur, vérifié avant usage."""
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


def resolve_image(valeur: Any, job_dir: Path, champ: str = "image") -> Path:
    """Le chemin d'une image, relatif au dossier du job quand il l'est."""
    brut = str(valeur or "").strip()
    if not brut:
        raise WorkerError(f"aucune image en entrée : le champ `{champ}` est vide")
    chemin = Path(brut).expanduser()
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


def _familles(uniface: Any) -> list[tuple[Any, Any]]:
    """Couples (énumération de poids, classe) connus de la bibliothèque.

    L'appariement se fait par la valeur : les énumérations d'uniface héritent de
    `str`, si bien que `RetinaFaceWeights("retinaface_mnet_v2")` retrouve seule le
    membre. C'est ce qui permet au manifeste de nommer un fichier — celui qu'on
    voit dans le dépôt de poids, et celui qu'`allow_patterns` télécharge — plutôt
    qu'un couple famille/variante qu'il faudrait tenir à jour ici.
    """
    from uniface import constants as const

    return [
        (const.RetinaFaceWeights, uniface.RetinaFace),
        (const.SCRFDWeights, uniface.SCRFD),
        (const.CenterFaceWeights, uniface.CenterFace),
        (const.BlazeFaceWeights, uniface.BlazeFace),
        (const.YOLOv5FaceWeights, uniface.YOLOv5Face),
        (const.YOLOv8FaceWeights, uniface.YOLOv8Face),
        (const.PIPNetWeights, uniface.PIPNet),
        (const.FaceMeshWeights, uniface.FaceMesh),
        (const.LandmarkWeights, uniface.Landmark106),
        (const.ParsingWeights, uniface.BiSeNet),
        (const.ArcFaceWeights, uniface.ArcFace),
        (const.AdaFaceWeights, uniface.AdaFace),
        (const.EdgeFaceWeights, uniface.EdgeFace),
        (const.HeadPoseWeights, uniface.HeadPose),
        (const.GazeWeights, uniface.MobileGaze),
    ]


def resoudre_poids(uniface: Any, cle: str) -> tuple[Any, Any]:
    """`"pipnet_r18_wflw_98"` → (classe uniface, membre d'énumération)."""
    for enum_cls, model_cls in _familles(uniface):
        try:
            return model_cls, enum_cls(cle)
        except ValueError:
            continue
    raise WorkerError(
        f"poids inconnus d'uniface : {cle!r} — le manifeste doit nommer un fichier du "
        "dépôt de poids, sans son extension (par exemple « retinaface_mnet_v2 »)"
    )


def exiger_present(uniface: Any, racine: Path, membre: Any) -> Path:
    """Vérifie qu'un poids est là **et** intact, sans jamais laisser uniface agir.

    Deux échecs distincts, et les confondre coûterait cher. Un fichier absent est
    un `ecurie pull` qui n'a pas été fait, et le message porte la commande. Un
    fichier présent dont l'empreinte diverge est autre chose : le dépôt de poids
    a bougé sous une révision qu'on croyait épinglée. Laisser faire uniface le
    ferait effacer puis retélécharger en silence, et l'instantané versionné
    cesserait de décrire ce qui a servi.
    """
    from uniface import constants as const

    info = const.MODEL_REGISTRY.get(membre)
    extension = os.path.splitext(info.url)[1] if info else ".onnx"
    chemin = racine / f"{membre.value}{extension}"
    if not chemin.is_file():
        raise WorkerError(
            f"poids absents de l'instantané : {chemin.name} — les obtenir par "
            f"`ecurie pull <ref>` ; un worker ne télécharge jamais"
        )
    attendu = getattr(info, "sha256", None)
    if attendu and _sha256(chemin) != attendu:
        raise WorkerError(
            f"empreinte inattendue pour {chemin.name} : le dépôt de poids a divergé de "
            f"ce qu'uniface {getattr(uniface, '__version__', '?')} attend. Le job est "
            "refusé plutôt que de laisser la bibliothèque effacer l'instantané et le "
            "retélécharger, ce qui ferait perdre la révision épinglée"
        )
    return chemin


def exiger_poids_de_tache(options: dict[str, Any]) -> str:
    """La clé de poids que le variant déclare, ou le reproche qui dit quoi ajouter.

    Cinq capacités sur six chargent deux modèles : un détecteur, puis le leur.
    Deviner le second reviendrait à choisir un modèle à la place du manifeste —
    et le job produirait des points clés, des régions ou une empreinte sans que
    rien ne dise lesquels.
    """
    cle = str(options.get("weights") or "").strip()
    if not cle:
        raise WorkerError(
            "le manifeste ne nomme pas les poids de la tâche : ajouter "
            "`options.weights` au variant (par exemple « pipnet_r18_wflw_98 »)"
        )
    return cle


def _sha256(chemin: Path) -> str:
    digest = hashlib.sha256()
    with open(chemin, "rb") as handle:
        while bloc := handle.read(1 << 22):
            digest.update(bloc)
    return digest.hexdigest()


def ecrire_json(cible: Path, document: dict[str, Any]) -> None:
    cible.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


class UnifaceWorker(Worker):
    """Base des six adaptateurs : poids locaux, détecteur amont, mémoire, réglages."""

    #: Nom du fichier JSON produit, et clé de sortie qui le porte au contrat.
    sortie_json = "faces.json"
    sortie_cle = "faces"
    #: Faux pour l'adaptateur de détection, dont le détecteur *est* la tâche.
    tache_separee = True

    def __init__(self) -> None:
        self.cv2: Any = None
        self.np: Any = None
        self.uniface: Any = None
        self.racine: Path | None = None
        self.detecteur: Any = None
        self.modele: Any = None
        self.defaults: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.cle_detecteur: str = ""
        self.taille_detecteur: int | None = None

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        cv2, np, uniface = import_runtime()
        self.cv2, self.np, self.uniface = cv2, np, uniface
        self.defaults = dict(variant.get("defaults") or {})
        self.options = dict(variant.get("options") or {})
        self.racine = weights_dir(variant)

        # Le levier qui rend le chemin réseau d'uniface inerte : la bibliothèque
        # cherche ses poids ici, et ils y sont déjà.
        os.environ["UNIFACE_CACHE_DIR"] = str(self.racine)

        options: dict[str, Any] = {}
        cle_detecteur = str(self.options.get("detector") or DETECTEUR_PAR_DEFAUT)
        cle_tache = str(self.options.get("weights") or "").strip()

        if self.tache_separee:
            cle_tache = exiger_poids_de_tache(self.options)
            self.modele = self._construire(cle_tache)
            options["weights"] = cle_tache
        else:
            # Détection seule : le variant nomme son détecteur par `weights`, et
            # `detector` n'aurait pas de sens — il n'y a rien en amont.
            cle_detecteur = cle_tache or cle_detecteur

        # La taille d'entrée du variant, quand il en pose une : la connaître dès
        # le chargement évite de reconstruire le détecteur au premier job, ce qui
        # gonflerait la latence du premier cas mesuré par le banc d'essai.
        taille = self.defaults.get("input_size") or self.options.get("input_size")
        self.detecteur = self._construire_detecteur(
            cle_detecteur, int(taille) if taille else None
        )
        options["detector"] = cle_detecteur

        import onnxruntime

        options["providers"] = list(onnxruntime.get_available_providers())
        options["versions"] = {
            "onnxruntime": onnxruntime.__version__,
            "uniface": getattr(uniface, "__version__", "?"),
        }
        return options

    def _construire(self, cle: str, **kwargs: Any) -> Any:
        classe, membre = resoudre_poids(self.uniface, cle)
        exiger_present(self.uniface, self.racine, membre)
        try:
            return classe(model_name=membre, **kwargs)
        except Exception as exc:  # noqa: BLE001 — remonte avec la réparation
            raise WorkerError(
                f"chargement de {cle} impossible : {type(exc).__name__}: {exc}"
            ) from exc

    def _construire_detecteur(self, cle: str, taille: int | None = None) -> Any:
        # Le seuil est un paramètre du contrat, donc réglable par job : on charge
        # le détecteur au seuil le plus bas et on filtre nous-mêmes. Le fixer ici
        # obligerait à recharger le modèle pour changer un nombre.
        classe, _ = resoudre_poids(self.uniface, cle)
        réglages: dict[str, Any] = {"confidence_threshold": 0.02}
        réglages.update(_taille_entree(classe, taille))
        self.cle_detecteur = cle
        self.taille_detecteur = taille
        return self._construire(cle, **réglages)

    def unload(self) -> None:
        self.detecteur = None
        self.modele = None

    def peak_memory_bytes(self) -> int | None:
        # onnxruntime n'alloue pas sur Metal : le RSS dit ici la vérité.
        return peak_rss_bytes()

    # --- réglages ------------------------------------------------------------

    def reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        """Entrée du job, puis options du variant, puis défauts du manifeste."""
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        for couche in (self.options, self.defaults):
            if couche.get(nom) is not None:
                return couche[nom]
        return defaut

    # --- exécution -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.detecteur is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")

        progress(5, "lecture de l'image")
        chemin = resolve_image(request.get("image"), request.output_dir)
        image = self._lire(chemin)

        progress(20, "détection des visages")
        visages = self.detecter(image, request)

        progress(45, f"{len(visages)} visage(s)")
        return self.traiter(image, visages, request, progress)

    def detecter(self, image: Any, request: InferRequest) -> list[Any]:
        """Les visages retenus, du plus grand au plus petit, seuil et plafond appliqués.

        Le tri par surface n'est pas cosmétique : `max_faces` coupe la liste, et
        garder les plus grands est le seul choix défendable — un visage de quinze
        pixels au fond d'une photo de groupe ne donne ni points clés utilisables,
        ni empreinte comparable.
        """
        seuil = float(self.reglage(request, "confidence_threshold", 0.5))
        plafond = int(self.reglage(request, "max_faces", 10))

        # `input_size` décide de la taille à laquelle l'image entre dans le
        # réseau, et le coût suit sa surface. Elle est figée à la construction —
        # les ancres en dépendent — donc en changer suppose de reconstruire. On
        # ne le fait que sur changement effectif : le cas courant, celui du banc
        # d'essai comme celui d'une série de jobs au même réglage, ne recharge
        # rien.
        taille = self.reglage(request, "input_size", None)
        if taille is not None and int(taille) != self.taille_detecteur:
            self.detecteur = self._construire_detecteur(self.cle_detecteur, int(taille))

        try:
            trouvés = self.detecteur.detect(image)
        except Exception as exc:  # noqa: BLE001 — remonte avec le contexte
            raise WorkerError(f"détection impossible : {type(exc).__name__}: {exc}") from exc
        retenus = [f for f in trouvés if float(f.confidence) >= seuil]
        retenus.sort(key=_surface, reverse=True)
        return retenus[:plafond]

    def traiter(
        self, image: Any, visages: list[Any], request: InferRequest, progress: ProgressFn
    ) -> InferResult:
        raise NotImplementedError

    # --- images --------------------------------------------------------------

    def _lire(self, chemin: Path) -> Any:
        image = self.cv2.imread(str(chemin), self.cv2.IMREAD_COLOR)
        if image is None:
            raise WorkerError(f"image illisible : {chemin.name}")
        return image

    def ecrire_image(self, image: Any, cible: Path) -> None:
        if not self.cv2.imwrite(str(cible), image):
            raise WorkerError(f"écriture impossible : {cible.name}")

    def recadrer(self, image: Any, visage: Any, marge: float = 0.0) -> Any:
        """Le rectangle du visage, borné à l'image et élargi d'une marge relative.

        Les réseaux d'orientation et de regard sont entraînés sur un cadrage un
        peu plus large que la boîte du détecteur ; leur donner la boîte nue fait
        dériver l'angle sans que rien ne le signale.
        """
        hauteur, largeur = image.shape[:2]
        x1, y1, x2, y2 = (float(v) for v in visage.bbox)
        dx, dy = (x2 - x1) * marge, (y2 - y1) * marge
        x1, y1 = max(0, int(x1 - dx)), max(0, int(y1 - dy))
        x2, y2 = min(largeur, int(x2 + dx)), min(hauteur, int(y2 + dy))
        if x2 - x1 < 2 or y2 - y1 < 2:
            raise WorkerError("boîte de visage vide après recadrage — image trop petite")
        return image[y1:y2, x1:x2]

    def boite(self, visage: Any) -> list[int]:
        return [int(round(float(v))) for v in visage.bbox]


def _taille_entree(classe: Any, taille: int | None) -> dict[str, Any]:
    """`input_size` sous la forme qu'attend ce détecteur-là, ou rien.

    Trois formes coexistent chez uniface, et passer la mauvaise lève un
    `TypeError` au chargement : RetinaFace, SCRFD et CenterFace veulent un couple
    (largeur, hauteur), les deux YOLO un entier, BlazeFace ne l'expose pas du tout
    — sa résolution est celle des poids. On lit donc la signature plutôt que de
    tenir une table qui aurait vieilli à la première version d'amont.
    """
    import inspect

    if taille is None:
        return {}
    paramètre = inspect.signature(classe.__init__).parameters.get("input_size")
    if paramètre is None:
        return {}
    if "tuple" in str(paramètre.annotation):
        return {"input_size": (int(taille), int(taille))}
    return {"input_size": int(taille)}


def _surface(visage: Any) -> float:
    x1, y1, x2, y2 = (float(v) for v in visage.bbox)
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)
