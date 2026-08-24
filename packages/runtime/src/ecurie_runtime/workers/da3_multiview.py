"""Adaptateur `multiview-to-3d` : N photos d'une scène, un seul repère pour toutes.

Second emploi de l'environnement `depth-anything`, et le seul adaptateur du parc
qui relie plusieurs images **entre elles**. La frontière avec ses deux voisines
est la raison d'être de cette capacité, et elle se vérifie dans leurs contrats :
`depth-estimation` prend `image` — une chaîne — et rend une carte plus une caméra
dans un repère propre à cette image ; `image-to-mesh` prend `image` aussi et rend
une surface fermée sans aucune caméra. Ni l'une ni l'autre n'a de champ pouvant
accueillir deux photos, donc aucune ne peut dire où étaient les appareils les uns
par rapport aux autres. C'est cette consistance inter-vues qui est neuve, et rien
d'autre.

**L'export d'amont n'est pas appelé par son chemin habituel, et c'est délibéré.**
`DepthAnything3.inference(export_dir=…, export_format="glb")` écrit trois choses
dans le dossier qu'on lui donne : le `scene.glb` qu'on veut, un sous-dossier
`depth_vis/` d'un JPEG par vue, et un `scene.jpg` qui est — mesuré, sha256
identique, 109 032 octets — la **copie octet pour octet** de `depth_vis/0000.jpg`.
Autrement dit l'aperçu d'amont montre la première vue et sa profondeur, rien de
plus : une reconstruction dont les caméras 1 à 31 seraient fausses aurait
exactement le même. Et les JPEG de `depth_vis/` atterriraient dans le dossier du
job sans qu'aucun champ du contrat ne les déclare — invisibles de `job.files`,
jamais nettoyés. On appelle donc `export_to_glb` directement, avec
`export_depth_vis=False` : le dossier du job ne reçoit que ce que le contrat
promet, vérifié après coup par la liste de ses entrées.

**L'aperçu est composé ici, et il montre ce que la capacité apporte.** Un bandeau
d'une vignette par vue — la photo, sa profondeur, la couleur du tronc de pyramide
que l'export donne à sa caméra — puis un **plan** : le nuage vu de dessus, et les
centres de caméra reliés dans l'ordre reçu. Une pose fausse sort de l'arc et se
voit ; c'était impossible sur l'aperçu d'amont.

**Le pic mémoire se lit au pilote Metal, pas au RSS.** Ce worker est le second de
la famille à le faire, après la correction de `depth_anything.py` : `ru_maxrss`
n'impute pas les tampons Metal au processus, et un pic sous-déclaré est
exactement l'OOM que le contrôle d'admission existe pour empêcher. Mesuré ici,
un relevé pris juste après l'inférence donne le même chiffre à 0,02 Go près
qu'une sonde à 20 ms pendant le calcul — le relevé après coup suffit donc, et
c'est ce qui a décidé de ne pas lancer de fil de mesure.

**Le cache MPS est vidé avant chaque job, et ce n'est pas de l'hygiène.** Sans
cela, `driver_allocated_memory` cumule d'un job à l'autre : mesuré, la séquence
4 puis 8 puis 16 vues dans un même processus rend 6,55 / 10,67 / 12,77 Go quand
les mêmes N mesurés séparément coûtent 6,55 / 9,62 / 10,30. Le pic cessait de
décrire le job pour décrire son historique, et la pente du banc n'aurait rien
mesuré du tout.

**Un mot sur OpenMP.** Comme pour `depth_anything`, le chargement lie deux copies
du runtime OpenMP — torch et pycolmap — et la bibliothèque avorte le processus
plutôt que de continuer. `KMP_DUPLICATE_LIB_OK` est posé avant tout import lourd.

Rien de torch, numpy, trimesh ni PIL n'est importé au niveau du module (voir
`workers/__init__.py`) : la CI importe tous les adaptateurs sans Apple Silicon,
sans poids et sans venv de runtime.
"""

import gc
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    Worker,
    WorkerError,
    main,
    peak_rss_bytes,
)

# La palette de profondeur vient de l'adaptateur voisin plutôt que d'une seconde
# écriture : les deux capacités tournent sur le même env, sur la même famille de
# poids, et deux palettes différentes rendraient leurs sorties incomparables au
# coup d'œil — ce qui est précisément l'usage d'un aperçu.
from ecurie_runtime.workers.depth_anything import _coloriser, normaliser

ENV_NAME = "depth-anything"
REPAIR = f"ecurie env sync {ENV_NAME}"

NUAGE = "scene.glb"
APERCU = "apercu.jpg"
CAMERAS = "cameras.json"

#: Ce que le job a le droit de contenir en plus de ses entrées. Tout le reste est
#: une trace laissée par une bibliothèque, et le job le dit.
DEPOSES = frozenset({NUAGE, APERCU, CAMERAS})

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}

# Les bornes du contrat, revérifiées ici : un worker peut être appelé sans passer
# par la validation du contrat, et une valeur hors bornes ne doit pas se
# découvrir au milieu d'une inférence de trente secondes.
VUES_MIN, VUES_MAX = 2, 32
RES_MIN, RES_MAX, RES_DEFAUT = 256, 1024, 504
POINTS_MIN, POINTS_MAX, POINTS_DEFAUT = 50_000, 2_000_000, 1_000_000
CONF_MIN, CONF_MAX, CONF_DEFAUT = 0.0, 90.0, 40.0

#: Points retenus par vue pour le plan de l'aperçu. Le nuage complet compte des
#: centaines de milliers de points ; en dessiner quatre mille par vue suffit à
#: montrer une empreinte au sol, et coûte un dixième de seconde.
POINTS_APERCU = 4000

#: Géométrie de l'aperçu. Le plan occupe la plus grande part : c'est lui qui
#: porte l'information inter-vues, les vignettes ne disent que « quelle scène ».
LARGEUR = 1024
PLAN_HAUTEUR = 560
MARGE = 16
ECART = 4
VIGNETTE_MAX = 88
FOND = (18, 20, 24)
TRAIT = (150, 155, 165)

# Deux copies d'OpenMP dans le même processus : torch en apporte une, pycolmap
# l'autre. Sans ce drapeau, la bibliothèque avorte le processus au chargement.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


# --- ce qui se vérifie sans poids ---------------------------------------------


@dataclass(frozen=True)
class Demande:
    """Ce qui a été demandé, résolu, et ce qui n'a pas pu l'être."""

    process_res: int
    conf_thresh_percentile: float
    max_points: int
    seed: int
    warnings: tuple[str, ...] = ()


def plan_multivue(
    *,
    entree: Mapping[str, Any],
    params: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> Demande:
    """Traduit une demande du protocole en réglages de reconstruction.

    Fonction pure, sans torch : c'est tout ce qui se vérifie sans les poids — la
    priorité des trois couches et les bornes du contrat. `max_points` porte au
    passage la traduction qui manquerait ailleurs : le kwarg d'amont s'appelle
    `num_max_points`, et recopier le nom du contrat le ferait ignorer en silence.
    """
    couches = (entree, params, defaults)
    res = _entier("process_res", RES_DEFAUT, RES_MIN, RES_MAX, couches)
    points = _entier("max_points", POINTS_DEFAUT, POINTS_MIN, POINTS_MAX, couches)
    seed = _entier("seed", 0, 0, 2**32 - 1, couches)
    conf = _reel("conf_thresh_percentile", CONF_DEFAUT, CONF_MIN, CONF_MAX, couches)

    avertissements: list[str] = []
    if res % 14:
        avertissements.append(
            f"process_res = {res} n'est pas un multiple de 14 : l'encodeur travaille "
            "par patchs de 14 pixels et arrondira. La grille de sortie ne sera pas "
            "celle qui a été demandée"
        )
    return Demande(
        process_res=res,
        conf_thresh_percentile=conf,
        max_points=points,
        seed=seed,
        warnings=tuple(avertissements),
    )


def resolve_vues(valeur: Any, job_dir: Path, champ: str = "images") -> list[Path]:
    """Les N chemins de vues, relatifs au dossier du job quand ils le sont.

    Premier champ **tableau de fichiers** du parc, livré le jour même que cette
    capacité. Le superviseur copie chaque fichier sous `inputs/NNN-nom` et
    transmet la liste des chemins relatifs, dans l'ordre reçu ; le banc d'essai
    en passe des chemins absolus. Les deux formes arrivent ici, plus une
    troisième — la chaîne JSON que produit un `-p images=[…]` au terminal —,
    parce que refuser celle-là obligerait à écrire deux fois la même liste.

    L'ordre n'est pas cosmétique : la première vue fixe le repère dans lequel
    tout le reste est rendu. Deux jobs aux mêmes fichiers dans un autre ordre ne
    sont pas le même job, et l'empreinte que le superviseur calcule le dit déjà.
    """
    if isinstance(valeur, str):
        texte = valeur.strip()
        if not texte.startswith("["):
            raise WorkerError(
                f"`{champ}` : liste de fichiers attendue, reçu une chaîne unique. "
                "Cette capacité ne traite pas une vue isolée — c'est ce que fait "
                "`depth-estimation`"
            )
        try:
            valeur = json.loads(texte)
        except ValueError as exc:
            raise WorkerError(f"`{champ}` : liste JSON illisible ({exc})") from exc

    if not isinstance(valeur, Sequence) or isinstance(valeur, (str, bytes)):
        raise WorkerError(f"`{champ}` : liste de fichiers attendue, reçu {type(valeur).__name__}")

    chemins: list[Path] = []
    for rang, brut in enumerate(valeur):
        if not isinstance(brut, str) or not brut.strip():
            raise WorkerError(f"{champ}[{rang}] : chemin de fichier attendu")
        chemin = Path(brut.strip()).expanduser()
        if not chemin.is_absolute():
            chemin = job_dir / chemin
        if not chemin.is_file():
            raise WorkerError(f"{champ}[{rang}] introuvable : {chemin}")
        if chemin.suffix.lower() not in IMAGES:
            raise WorkerError(
                f"{champ}[{rang}] : format non géré ({chemin.suffix or 'sans extension'}) — "
                f"images acceptées : {', '.join(sorted(IMAGES))}"
            )
        chemins.append(chemin)

    if len(chemins) < VUES_MIN:
        raise WorkerError(
            f"{len(chemins)} vue(s) : il en faut au moins {VUES_MIN}. Une vue unique "
            "ne porte aucune information inter-vues, et c'est la seule chose que "
            "cette capacité apporte"
        )
    if len(chemins) > VUES_MAX:
        raise WorkerError(
            f"{len(chemins)} vues : le contrat en accepte {VUES_MAX} au plus. Le pic "
            "mesuré à trente-deux vues est de 11,8 Go, et la courbe est un escalier "
            "de l'allocateur MPS — au-delà, rien n'a été mesuré"
        )
    return chemins


def weights_dir(variant: dict[str, Any]) -> Path:
    """Le dossier de poids transmis par le superviseur, vérifié avant usage."""
    brut = str(variant.get("weights_path") or "").strip()
    chemin = Path(brut)
    if not brut or not chemin.is_dir():
        raise WorkerError(
            f"poids introuvables : {brut or '(vide)'} — le superviseur transmet un chemin "
            f"local déjà vérifié, un worker ne télécharge jamais (`{REPAIR}` si l'env est "
            "en cause)"
        )
    return chemin


def couleur_vue(rang: int, total: int) -> tuple[int, int, int]:
    """La couleur que l'export d'amont donne au tronc de pyramide de cette caméra.

    Réécrite plutôt qu'importée de `depth_anything_3.utils.export.glb`, dont elle
    est privée : ce que l'aperçu doit garantir, c'est que la vignette d'une vue
    porte la même couleur que sa caméra dans le GLB. Un import privé le
    romprait le jour où l'amont renomme sa fonction, et sans rien signaler.
    """
    h = (rang + 0.5) / max(total, 1)
    secteur = int(h * 6.0)
    f = h * 6.0 - secteur
    v, s = 0.95, 0.85
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r, g, b = ((v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q))[secteur % 6]
    return int(r * 255), int(g * 255), int(b * 255)


def centres_cameras(np: Any, extrinsics: Any) -> Any:
    """Les centres optiques, en repère monde, depuis des extrinsèques **w2c**.

    La carte du modèle est explicite : `extrinsics` est world-to-camera. Le
    centre d'une caméra est donc −Rᵀt et non t, et prendre t rendrait un arc
    plausible mais faux — le genre d'erreur qu'un aperçu montrant une seule vue
    n'aurait jamais révélée.
    """
    ext = np.asarray(extrinsics, dtype="float64")
    rotations, translations = ext[:, :3, :3], ext[:, :3, 3]
    return -np.einsum("nji,nj->ni", rotations, translations)


def alignement(np: Any, ext0: Any, points: Any) -> Any:
    """La matrice 4×4 qui amène le repère monde dans celui du GLB.

    Reprise de l'export d'amont pour que l'aperçu et le nuage décrivent le même
    espace : orientation de la première caméra, bascule des axes de la vision
    par ordinateur vers ceux de glTF (Y et Z inversés), puis centrage sur la
    **médiane** du nuage — médiane et non moyenne, parce qu'un ciel mal estimé
    envoie quelques milliers de points à l'infini et déplacerait tout le reste.
    """
    w2c0 = np.eye(4)
    w2c0[:3, :4] = np.asarray(ext0, dtype="float64")
    A = np.diag([1.0, -1.0, -1.0, 1.0]) @ w2c0
    centre = np.zeros(3)
    if len(points):
        transformés = (A[:3, :3] @ np.asarray(points, dtype="float64").T).T + A[:3, 3]
        centre = np.median(transformés, axis=0)
    recentrage = np.eye(4)
    recentrage[:3, 3] = -centre
    return recentrage @ A


def _reglage(nom: str, *couches: Mapping[str, Any]) -> Any:
    for couche in couches:
        valeur = couche.get(nom)
        if valeur is not None:
            return valeur
    return None


def _entier(
    nom: str, defaut: int, plancher: int, plafond: int, couches: tuple[Mapping[str, Any], ...]
) -> int:
    valeur = _reglage(nom, *couches)
    if valeur is None:
        return defaut
    if isinstance(valeur, bool):
        raise WorkerError(f"{nom} : entier attendu, reçu un booléen")
    try:
        entier = int(valeur)
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"{nom} : entier attendu, reçu {valeur!r}") from exc
    if not plancher <= entier <= plafond:
        raise WorkerError(
            f"{nom} = {entier} : le contrat borne ce paramètre à [{plancher} ; {plafond}]"
        )
    return entier


def _reel(
    nom: str, defaut: float, plancher: float, plafond: float, couches: tuple[Mapping[str, Any], ...]
) -> float:
    valeur = _reglage(nom, *couches)
    if valeur is None:
        return defaut
    if isinstance(valeur, bool):
        raise WorkerError(f"{nom} : nombre attendu, reçu un booléen")
    try:
        reel = float(valeur)
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"{nom} : nombre attendu, reçu {valeur!r}") from exc
    if not plancher <= reel <= plafond:
        raise WorkerError(
            f"{nom} = {reel} : le contrat borne ce paramètre à [{plancher} ; {plafond}]"
        )
    return reel


def _traces(avant: set[str], apres: set[str]) -> list[str]:
    """Ce que le job contient en plus de ses entrées et de ses sorties déclarées.

    Ce contrôle existe pour une raison précise : l'export d'amont, appelé par son
    chemin habituel, dépose un `depth_vis/` d'un JPEG par vue et un `scene.jpg`
    que le contrat ne déclare pas. On l'appelle autrement, et on **vérifie** que
    l'autrement tient, plutôt que de le croire. `avant` est relevé après la copie
    des entrées, si bien que `inputs/` n'y compte pas pour une trace.
    """
    nouveaux = sorted(apres - avant - DEPOSES)
    if not nouveaux:
        return []
    return [
        f"{len(nouveaux)} entrée(s) apparue(s) dans le dossier du job hors des sorties "
        f"déclarées par le contrat : {', '.join(nouveaux)}"
    ]


# --- l'adaptateur -------------------------------------------------------------


class DA3MultiviewWorker(Worker):
    """Reconstruction spatiale multi-vues, par Depth Anything 3 Large 1.1."""

    name = "da3-multiview"

    def __init__(self) -> None:
        self._torch: Any = None
        self._np: Any = None
        self._model: Any = None
        self._export: Any = None
        self._device = "cpu"
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._peak_driver = 0
        self._pic_du_job = 0

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        chemin = weights_dir(variant)
        try:
            import numpy as np
            import torch
            from depth_anything_3.api import DepthAnything3
            from depth_anything_3.utils.export import export_to_glb
        except ImportError as exc:
            raise WorkerError(
                f"runtime depth-anything indisponible dans cet environnement ({exc}) — `{REPAIR}`"
            ) from exc

        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})
        self._torch, self._np, self._export = torch, np, export_to_glb
        if not torch.backends.mps.is_available():
            # Refusé plutôt que replié en silence : le modèle tourne sur CPU,
            # mais trente secondes deviennent des minutes et le profil mesuré
            # sur MPS ne décrirait plus rien de ce qui s'exécute.
            raise WorkerError(
                "backend MPS indisponible (torch.backends.mps.is_available() est faux) — "
                f"vérifier que runtimes/{ENV_NAME}/.venv tourne sur un Python arm64"
            )
        self._device = "mps"

        modèle = DepthAnything3.from_pretrained(str(chemin))
        self._model = modèle.to(self._device).eval()
        self._mps_counters()
        return {
            "device": self._device,
            "vues_max": VUES_MAX,
            "versions": self._versions(),
        }

    # --- exécution -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._model is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")
        np = self._np

        plan = plan_multivue(
            entree=request.input, params=request.params, defaults=self._defaults
        )
        avertissements = list(plan.warnings)

        progress(5, "lecture des vues")
        vues = resolve_vues(request.get("images"), request.output_dir)
        images, tailles = self._lire(vues)
        if len(set(tailles)) > 1:
            avertissements.append(
                f"les {len(vues)} vues n'ont pas toutes la même définition "
                f"({', '.join(sorted({f'{w}×{h}' for w, h in tailles}))}) : chacune est "
                "ramenée séparément à la grille de traitement, et les intrinsèques "
                "rendues diffèrent d'une vue à l'autre"
            )
        if len(vues) == VUES_MIN:
            avertissements.append(
                "deux vues seulement : c'est la configuration la plus fragile de cette "
                "capacité. La pose relative y repose sur une seule paire, sans aucune "
                "redondance pour la contredire"
            )

        avant = _entrees(request.output_dir)
        progress(15, f"reconstruction de {len(vues)} vues")
        prédiction, calcul = self._inferer(images, plan)
        # Relevé ici, avant que les post-traitements ne rendent la mémoire : le
        # pilote redescend, et le maximum se tient à chaque relevé.
        self._mps_counters()

        progress(65, "écriture du nuage")
        points_écrits, lus = self._ecrire_nuage(prédiction, request.output_dir, plan)
        avertissements += lus

        progress(85, "composition de l'aperçu")
        self._ecrire_apercu(prédiction, request.output_dir / APERCU)
        focales = self._ecrire_cameras(prédiction, vues, request.output_dir / CAMERAS, plan)

        avertissements += _traces(avant, _entrees(request.output_dir))
        if points_écrits >= plan.max_points:
            avertissements.append(
                f"nuage tronqué à {plan.max_points} points : l'export en tire autant au "
                "sort parmi ceux qui passent le seuil de confiance, et `seed` décide "
                "lesquels. Relever `max_points` pour garder tout ce que le modèle a vu"
            )

        sortie = {
            "point_cloud": NUAGE,
            "preview": APERCU,
            "cameras": CAMERAS,
            "view_count": len(vues),
            "point_count": points_écrits,
            # Toujours faux, et mesuré plutôt que supposé : sur ces poids
            # `prediction.is_metric` vaut `{}` et `scale_factor` vaut `None`.
            # Deux lots de photos de la même pièce ne donnent pas la même unité.
            "is_metric": False,
        }
        métriques: dict[str, Any] = {
            "device": self._device,
            "view_count": len(vues),
            "process_res": plan.process_res,
            "point_count": points_écrits,
            "max_points": plan.max_points,
            "infer_ms": int(calcul * 1000),
            "focal_length_px": round(float(np.mean(focales)), 3) if len(focales) else None,
            # Deux chiffres, deux questions. Le premier est le maximum du worker
            # depuis son chargement — c'est ce que le parc doit réserver, et
            # c'est ce que le protocole attend sous ce nom ; le second est ce
            # que cette reconstruction-ci a coûté, ce qui n'a de sens que parce
            # que le cache MPS est vidé au début de chaque job. Sans le second,
            # un job à deux vues qui suit un job à trente-deux dans le même
            # worker déclarerait le pic du gros.
            "peak_memory_bytes": self.peak_memory_bytes(),
            "job_peak_memory_bytes": self._pic_du_job or None,
        }
        if avertissements:
            métriques["warnings"] = avertissements
        return InferResult(output=sortie, metrics=métriques)

    def _lire(self, vues: list[Path]) -> tuple[list[Any], list[tuple[int, int]]]:
        from PIL import Image

        np = self._np
        images, tailles = [], []
        for chemin in vues:
            try:
                with Image.open(chemin) as ouverte:
                    tailles.append(ouverte.size)
                    images.append(np.array(ouverte.convert("RGB")))
            except OSError as exc:
                raise WorkerError(f"vue illisible ({chemin.name}) : {exc}") from exc
        return images, tailles

    def _inferer(self, images: list[Any], plan: Demande) -> tuple[Any, float]:
        """L'inférence, cache MPS vidé d'abord — voir l'en-tête du module."""
        torch = self._torch
        try:
            torch.mps.empty_cache()
        except (AttributeError, RuntimeError):
            pass
        début = time.monotonic()
        try:
            with torch.no_grad():
                prédiction = self._model.inference(
                    images,
                    # Chaîne vide et non `None` : l'amont fait `"gs" in export_format`
                    # sans vérifier, et `None` lève un TypeError qui ne parle de rien.
                    # L'export est appelé séparément, voir l'en-tête du module.
                    export_format="",
                    process_res=plan.process_res,
                )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(
                f"reconstruction impossible sur {len(images)} vues : "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        # Le coût de **ce** job, et non le maximum du worker depuis son
        # chargement. Les deux sont utiles et ne répondent pas à la même
        # question : `peak_memory_bytes` dit ce que le worker occupe et sert au
        # budget, ce chiffre-ci dit ce qu'une reconstruction à N vues coûte. Il
        # n'a de sens que parce que le cache a été vidé juste avant.
        self._pic_du_job = self._lire_pilote()
        return prédiction, time.monotonic() - début

    def _lire_pilote(self) -> int:
        mps = getattr(self._torch, "mps", None) if self._torch is not None else None
        if mps is None:
            return 0
        try:
            return int(mps.driver_allocated_memory())
        except (AttributeError, RuntimeError):
            return 0

    # --- les trois sorties ---------------------------------------------------

    def _ecrire_nuage(
        self, prédiction: Any, job_dir: Path, plan: Demande
    ) -> tuple[int, list[str]]:
        """Le GLB, puis sa relecture — un fichier écrit n'est pas un fichier juste.

        Le tirage de l'échantillonnage d'amont passe par `np.random.choice`, donc
        par le générateur **global** de numpy : sans graine, deux jobs identiques
        ne rendent pas le même nuage dès que le cap mord. L'état du générateur est
        rendu ensuite, pour qu'un job ne décide pas des tirages du suivant.
        """
        np = self._np
        état = np.random.get_state()
        np.random.seed(plan.seed)
        try:
            chemin = Path(
                self._export(
                    prédiction,
                    str(job_dir),
                    num_max_points=plan.max_points,
                    conf_thresh_percentile=plan.conf_thresh_percentile,
                    show_cameras=True,
                    # Le seul moyen de ne pas laisser `depth_vis/` et un
                    # `scene.jpg` trompeur dans le dossier du job.
                    export_depth_vis=False,
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise WorkerError(
                f"export du nuage impossible : {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            np.random.set_state(état)
        self._mps_counters()
        return self._relire(chemin, int(np.asarray(prédiction.depth).shape[0]))

    def _relire(self, chemin: Path, vues: int) -> tuple[int, list[str]]:
        """Compte les points du GLB et vérifie qu'il porte bien une caméra par vue.

        Un banc au vert ne regarde pas ce qu'un fichier contient. Ici on l'ouvre :
        un nuage vide et un nuage juste ont la même taille de fichier à quelques
        octets près, et un GLB sans troncs de pyramide serait un nuage sans
        caméras — c'est-à-dire tout ce que cette capacité promet en moins.
        """
        try:
            import trimesh
        except ImportError:
            return 0, ["trimesh absent : le nuage n'a pas été relu après écriture"]
        try:
            scène = trimesh.load(str(chemin))
        except Exception as exc:  # noqa: BLE001
            raise WorkerError(
                f"le nuage écrit ne se relit pas : {type(exc).__name__}: {exc}"
            ) from exc

        points, cameras = 0, 0
        for géométrie in getattr(scène, "geometry", {}).values():
            nom = type(géométrie).__name__
            if nom == "PointCloud":
                points += int(len(géométrie.vertices))
            elif nom == "Path3D":
                cameras += 1

        avertissements: list[str] = []
        if points == 0:
            avertissements.append(
                "le nuage est vide : aucun pixel n'a passé le seuil de confiance. "
                "Abaisser `conf_thresh_percentile`, ou y lire que la scène n'était pas "
                "reconstructible"
            )
        if cameras != vues:
            avertissements.append(
                f"{cameras} tronc(s) de pyramide dans le GLB pour {vues} vues : les "
                "caméras rendues ne correspondent pas aux vues soumises"
            )
        return points, avertissements

    def _ecrire_cameras(
        self, prédiction: Any, vues: list[Path], cible: Path, plan: Demande
    ) -> Any:
        """Une pose et une matrice intrinsèque par vue, plus ce qu'il faut pour les lire."""
        np = self._np
        ext = np.asarray(prédiction.extrinsics, dtype="float64")
        intr = np.asarray(prédiction.intrinsics, dtype="float64")
        centres = centres_cameras(np, ext)
        charge = {
            "convention": (
                "extrinsics = world-to-camera, matrice 3×4 [R|t] ; le centre optique "
                "d'une caméra est -Rᵀt et non t. intrinsics = matrice 3×3 en pixels de "
                "la grille de traitement, pas de l'image d'entrée."
            ),
            "frame": (
                "Repère monde du modèle, commun à toutes les vues. Son orientation "
                "et son origine sont arbitraires ; c'est le fichier scene.glb qui les "
                "ramène dans les axes glTF de la première vue."
            ),
            "is_metric": False,
            "echelle": (
                "Relative. Mesuré sur ces poids : `is_metric` vaut {} et `scale_factor` "
                "vaut None. Deux lots de photos de la même pièce ne donnent pas la même "
                "unité, et aucune distance ici n'est en mètres."
            ),
            "process_res": plan.process_res,
            "views": [
                {
                    "index": rang,
                    "file": vue.name,
                    "extrinsics": ext[rang].tolist(),
                    "intrinsics": intr[rang].tolist(),
                    "center": centres[rang].tolist(),
                    "focal_length_px": round(float(intr[rang][0][0]), 4),
                }
                for rang, vue in enumerate(vues)
            ],
        }
        cible.write_text(json.dumps(charge, ensure_ascii=False, indent=2), encoding="utf-8")
        return intr[:, 0, 0]

    def _ecrire_apercu(self, prédiction: Any, cible: Path) -> None:
        """Le bandeau des vues et le plan des caméras — voir l'en-tête du module."""
        np = self._np
        from PIL import Image, ImageDraw

        depth = np.asarray(prédiction.depth, dtype="float32")
        conf = np.asarray(prédiction.conf, dtype="float32")
        intr = np.asarray(prédiction.intrinsics, dtype="float64")
        ext = np.asarray(prédiction.extrinsics, dtype="float64")
        vignettes = np.asarray(prédiction.processed_images)
        n = int(depth.shape[0])

        côté = min(VIGNETTE_MAX, max(16, (LARGEUR - 2 * MARGE - (n - 1) * ECART) // n))
        bande = 2 * côté + MARGE
        hauteur = MARGE + bande + PLAN_HAUTEUR

        toile = Image.new("RGB", (LARGEUR, hauteur), FOND)
        cadres = ImageDraw.Draw(toile)
        rangée = n * côté + (n - 1) * ECART
        gauche = (LARGEUR - rangée) // 2
        for rang in range(n):
            x = gauche + rang * (côté + ECART)
            photo = Image.fromarray(vignettes[rang]).resize((côté, côté), Image.LANCZOS)
            toile.paste(photo, (x, MARGE))
            plan = depth[rang]
            échelle = normaliser(np, plan, float(plan.min()), float(plan.max()))
            profondeur = Image.fromarray(_coloriser(np, échelle, "turbo"), "RGB")
            toile.paste(profondeur.resize((côté, côté), Image.LANCZOS), (x, MARGE + côté))
            # Le cadre est ce qui relie la vignette à sa caméra dans le plan et
            # dans le GLB : sans lui, rien ne dit quelle photo a été prise d'où.
            cadres.rectangle(
                [x, MARGE, x + côté - 1, MARGE + 2 * côté - 1],
                outline=couleur_vue(rang, n),
                width=2,
            )

        points, couleurs = self._nuage_leger(depth, conf, intr, ext, vignettes)
        A = alignement(np, ext[0], points)
        projeter = lambda X: (A[:3, :3] @ np.atleast_2d(X).T).T + A[:3, 3]  # noqa: E731
        nuage = projeter(points) if len(points) else np.zeros((0, 3))
        caméras = projeter(centres_cameras(np, ext))

        # Le cadrage tient compte des caméras ET du cœur du nuage, jamais de sa
        # queue : le sol d'une scène d'extérieur s'étend jusqu'à l'horizon, et
        # laisser son étendue décider de l'échelle réduirait l'arc des caméras à
        # quelques pixels — c'est pourtant lui qu'on vient voir.
        cadrables = caméras[:, [0, 2]]
        if len(nuage):
            rayons = np.linalg.norm(nuage[:, [0, 2]], axis=1)
            proches = nuage[rayons <= np.percentile(rayons, 85)][:, [0, 2]]
            if len(proches):
                cadrables = np.concatenate([cadrables, proches])
        bas, haut_boite = cadrables.min(axis=0), cadrables.max(axis=0)
        étendue = np.maximum(haut_boite - bas, 1e-6) * 1.08
        centre = (bas + haut_boite) / 2
        haut = MARGE + bande
        k = float(
            min((LARGEUR - 2 * MARGE) / étendue[0], (PLAN_HAUTEUR - 2 * MARGE) / étendue[1])
        )
        milieu = haut + PLAN_HAUTEUR / 2

        def écran(p: Any) -> Any:
            p = np.atleast_2d(p)
            return np.stack(
                [
                    LARGEUR / 2 + (p[:, 0] - centre[0]) * k,
                    milieu - (p[:, 2] - centre[1]) * k,
                ],
                -1,
            )

        if len(nuage):
            uv = écran(nuage).astype("int32")
            dedans = (
                (uv[:, 0] >= 0) & (uv[:, 0] < LARGEUR)
                & (uv[:, 1] >= haut) & (uv[:, 1] < hauteur)
            )
            tableau = np.asarray(toile).copy()
            tableau[uv[dedans, 1], uv[dedans, 0]] = couleurs[dedans]
            toile = Image.fromarray(tableau)

        dessin = ImageDraw.Draw(toile)
        uvc = écran(caméras)
        dessin.line([tuple(p) for p in uvc], fill=TRAIT, width=1)
        # L'alignement est une isométrie : une longueur mesurée dans le cadrage
        # vaut la même chose en repère monde, et l'axe optique peut donc s'y
        # allonger sans conversion.
        portée = float(np.max(étendue)) * 0.12
        regards = écran(projeter(centres_cameras(np, ext) + self._avants(ext) * portée))
        for rang in range(n):
            teinte = couleur_vue(rang, n)
            dessin.line([tuple(uvc[rang]), tuple(regards[rang])], fill=teinte, width=2)
            u, w = uvc[rang]
            dessin.ellipse([u - 4, w - 4, u + 4, w + 4], fill=teinte, outline=(0, 0, 0))
            # La police par défaut de Pillow ne porte ni accents ni tiret cadratin
            # (vérifié : les glyphes sortent en tofu). L'aperçu n'écrit donc que
            # des chiffres, et la phrase qui va avec vit dans le contrat.
            if rang in (0, n - 1):
                dessin.text((u + 7, w - 5), str(rang), fill=(235, 238, 245))
        toile.save(cible, format="JPEG", quality=88)

    def _avants(self, ext: Any) -> Any:
        """L'axe optique de chaque caméra en repère monde : la troisième ligne de R, transposée."""
        return self._np.asarray(ext, dtype="float64")[:, 2, :3]

    def _nuage_leger(
        self, depth: Any, conf: Any, intr: Any, ext: Any, images: Any
    ) -> tuple[Any, Any]:
        """Quelques milliers de points par vue, pour le plan seulement.

        Rétroprojeter le nuage complet une seconde fois coûterait autant que
        l'export lui-même pour une image de mille pixels de large. Le pas est
        régulier plutôt que tiré au sort : l'aperçu d'un job doit être le même
        d'une exécution à l'autre, `seed` ou pas.
        """
        np = self._np
        n, h, w = depth.shape
        pas = max(1, int((h * w / POINTS_APERCU) ** 0.5))
        us, vs = np.meshgrid(np.arange(0, w, pas), np.arange(0, h, pas))
        pixels = np.stack([us, vs, np.ones_like(us)], -1).reshape(-1, 3).astype("float64")
        seuil = float(np.percentile(conf, CONF_DEFAUT))

        points, couleurs = [], []
        for rang in range(n):
            d = depth[rang][vs, us].reshape(-1)
            c = conf[rang][vs, us].reshape(-1)
            valides = np.isfinite(d) & (d > 0) & (c >= seuil)
            if not valides.any():
                continue
            rayons = np.linalg.inv(intr[rang]) @ pixels[valides].T
            dans_camera = rayons * d[valides][None, :]
            R, t = ext[rang][:3, :3], ext[rang][:3, 3]
            points.append((R.T @ (dans_camera - t[:, None])).T)
            couleurs.append(images[rang][vs, us].reshape(-1, 3)[valides])
        if not points:
            return np.zeros((0, 3)), np.zeros((0, 3), dtype="uint8")
        return np.concatenate(points), np.concatenate(couleurs).astype("uint8")

    # --- mémoire et versions -------------------------------------------------

    def peak_memory_bytes(self) -> int | None:
        """Le pic vu du pilote Metal, ou le RSS s'il est plus grand.

        Les deux comptent, et pour la même raison : sur mémoire unifiée ils
        occupent le même budget, mais aucun des deux ne voit l'autre — `ru_maxrss`
        n'impute pas les tampons Metal au processus, et le pilote ne connaît pas
        les tableaux numpy. Le maximum est tenu à chaque relevé plutôt que lu une
        fois à la fin, parce que `driver_allocated_memory` redescend.
        """
        self._mps_counters()
        return max(self._peak_driver, peak_rss_bytes() or 0) or None

    def _mps_counters(self) -> None:
        self._peak_driver = max(self._peak_driver, self._lire_pilote())

    def unload(self) -> None:
        self._model = None
        gc.collect()
        if self._torch is not None and self._device == "mps":
            try:
                self._torch.mps.empty_cache()
            except (AttributeError, RuntimeError):
                pass

    def _versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for nom, module in (
            ("torch", "torch"),
            ("depth-anything-3", "depth_anything_3"),
            ("numpy", "numpy"),
            ("trimesh", "trimesh"),
        ):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


def _entrees(dossier: Path) -> set[str]:
    try:
        return {chemin.name for chemin in dossier.iterdir()}
    except OSError:
        return set()


if __name__ == "__main__":
    raise SystemExit(main(DA3MultiviewWorker))
