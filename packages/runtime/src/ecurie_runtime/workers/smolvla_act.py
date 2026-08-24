"""Adaptateur `lerobot` : une image et une consigne entrent, un tronçon d'actions sort.

Seizième famille du parc, et la seule dont la sortie soit une **action**. Les
quinze autres rendent quelque chose qu'on regarde, qu'on lit ou qu'on compare ;
celle-ci rend des nombres qu'un contrôleur exécuterait. Aucun robot du parc ne
les exécute, et tout ce fichier est écrit pour que cela reste visible jusqu'au
bout : l'incarnation, la convention et les unités sortent **avec** le tronçon,
chaque job porte l'avertissement, et le fichier déposé se relit seul.

**Quatre levées certaines si l'on suit la description du modèle plutôt que son
exécution.** Les trois premières ont été reproduites sur cette machine avant
d'écrire une ligne ; la quatrième est lue dans le code d'amont et non provoquée,
ce qui est dit à sa place plutôt que laissé croire.

*Le `device: cuda` du pipeline de processeurs.* `PreTrainedConfig.__post_init__`
corrige bien le `device: "cuda"` du `config.json` de la politique — mesuré, il
imprime « Device 'cuda' is not available. Switching to 'mps'. » Mais le pipeline
de processeurs est un **autre fichier** : `policy_preprocessor.json` porte sa
propre étape `device_processor` avec `"device": "cuda"` en dur, et son
`__post_init__` appelle `get_safe_torch_device`, qui lève
`ValueError: Requested device 'cuda' but CUDA is not available.` La surcharge
`preprocessor_overrides={"device_processor": {"device": …}}` n'est pas une
précaution : sans elle, `make_pre_post_processors` ne rend jamais la main.

*Les clés d'images.* Le `config.json` déclare `observation.images.camera1/2/3`.
Un batch construit sur `observation.image` lève
`ValueError: All image features are missing from the batch`. Les clés ne sont pas
écrites en dur ici : elles sont **lues** sur `config.image_features`, ce qui est
le seul moyen de servir un autre membre de la famille sans réécrire ce fichier.

*La dimension de l'état.* Le `config.json` du titulaire annonce
`observation.state` de forme `[6]`. **C'est périmé** : les statistiques publiées
à côté sont de dimension 8, et c'est le normaliseur qui décide. Mesuré, un état
de dimension 6 lève `RuntimeError: The size of tensor a (6) must match the size
of tensor b (8)`. La dimension attendue est donc lue dans le fichier de
statistiques, jamais dans la configuration.

*La forme du bruit.* `sample_actions` construit `(bsize, chunk_size,
max_action_dim)` quand on ne lui en donne pas — soit `(1, 50, 32)` et non
`(1, 50, 7)`. C'est le seul des quatre points que je n'ai pas fait lever : il est
lu dans `modeling_smolvla.py`, et c'est cette forme-là qui est employée ici,
vérifiée par le fait que tous les appels passent. Le tronçon est ensuite ramené à
`action_feature.shape[0]` par la politique elle-même, ce qui explique qu'une
forme d'action ne convienne pas en entrée d'un intégrateur qui travaille en
dimension rembourrée.

**Ce qui n'est PAS refusé, contrairement à `cad_recode`.** L'adaptateur de CAO
refuse de charger au-delà de `transformers 4`, parce qu'au-delà la capacité ne
tombe pas en panne : elle ment. Ici la panne serait bruyante — le format des
fichiers de processeurs a déjà changé une fois en amont, et un changement de plus
rendrait `policy_preprocessor.json` illisible avec une exception. Une version
inattendue de lerobot est donc **signalée**, pas refusée : refuser coûterait un
job que la borne du `pyproject.toml` empêche déjà.

**Le pic à inscrire est le RSS, pas le pilote Metal.** Mesuré au banc : pilote
1 455 374 336 octets (1,36 Gio), RSS 3 438 411 776 (3,20 Gio), identiques à
l'octet aux trois cas. L'écart vient du mmap des poids, des bibliothèques torch
et du tokeniseur, qui n'entrent pas dans la comptabilité Metal et sortent
pourtant du même budget unifié. `peak_memory_bytes` rend le plus grand des deux,
comme partout dans le parc.

Rien de torch, lerobot, numpy, PIL ni safetensors n'est importé au niveau du
module (voir `workers/__init__.py`) : la CI importe tous les adaptateurs sans
Apple Silicon et sans venv de runtime.
"""

import gc
import hashlib
import json
import os
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

ENV_NAME = "lerobot"
REPAIR = f"ecurie env sync {ENV_NAME}"

SORTIE_JSON = "actions.json"

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

#: Les champs d'images du contrat, dans l'ordre. Il est significatif : le premier
#: alimente la première caméra que le modèle déclare, et permuter deux vues change
#: la sortie sans qu'aucun message ne le signale.
CHAMPS_IMAGES = ("image", "image2", "image3")

#: Bornes du contrat, redites ici parce qu'un worker peut être appelé sans passer
#: par la validation du contrat — c'est alors le seul endroit qui les tienne.
PAS_MIN, PAS_MAX, PAS_DEFAUT = 1, 20, 10
ETAT_MAX = 64

#: Ce que le manifeste doit déclarer pour que le tronçon se relise. Aucun de ces
#: trois-là ne se lit sur les poids : un checkpoint sait combien de nombres il
#: produit, il ne sait pas quelle machine les exécute ni dans quelle convention.
#: Les rendre obligatoires est la décision de fond de cet adaptateur — servir un
#: tronçon anonyme reviendrait à publier sept flottants que n'importe qui peut
#: prendre pour n'importe quoi.
OPTIONS_REQUISES = ("embodiment", "space", "units")

ESPACES = ("cartesian-delta", "joint-delta", "joint-absolute")
UNITES = ("controller-normalized", "m+rad", "deg")

#: La branche de lerobot sur laquelle cet adaptateur a été écrit et mesuré. Une
#: autre n'est pas refusée — voir l'en-tête — mais elle est dite.
LEROBOT_MESURE = "0.6"

#: Rôle sous lequel le manifeste déclare le second dépôt. La dorsale
#: vision-langage de SmolVLA n'est pas dans le dépôt de la politique : c'est un
#: SmolVLM2 publié à part, sous sa propre licence et sa propre révision.
ROLE_VLM = "vision_encoder"


# --- ce qui se vérifie sans poids ---------------------------------------------
#
# Rien ici ne touche torch ni lerobot : c'est ce qui rend ces fonctions
# vérifiables en CI, sans Apple Silicon, sans venv de runtime et sans poids.


@dataclass(frozen=True)
class Demande:
    """Ce qui a été demandé, résolu, et ce qui n'a pas pu l'être."""

    steps: int
    seed: int
    warnings: tuple[str, ...] = ()


def plan_action(
    *,
    entree: Mapping[str, Any],
    params: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> Demande:
    """Traduit une demande du protocole en réglages d'échantillonnage.

    Fonction pure : la priorité des trois couches — entrée du job, options du
    variant, défauts du manifeste — et les bornes du contrat revérifiées ici,
    parce qu'un worker peut être appelé sans passer par la validation du contrat.
    """
    couches = (entree, params, defaults)
    return Demande(
        steps=_entier("steps", PAS_DEFAUT, PAS_MIN, PAS_MAX, couches),
        seed=_entier("seed", 0, 0, 2**32 - 1, couches),
    )


def lire_consigne(brut: Any, champ: str = "instruction") -> tuple[str, list[str]]:
    """La consigne nettoyée, et ce qu'il faut en penser.

    Les retours à la ligne sont ramenés à des espaces : le modèle en ajoute un
    lui-même en fin de consigne, et une consigne sur deux lignes se retrouverait
    coupée au milieu d'un jeton sans que rien ne le dise.

    **L'avertissement sur la langue est le seul contrôle possible, et il est
    partiel.** Ces modèles n'ont vu que de l'anglais, et la panne est muette :
    mesuré sur un vrai job, « ramasse le cube rouge posé à gauche » rend un
    tronçon de forme irréprochable, sans la moindre erreur ; le seul signe est
    indirect, et il vient du contrôle de domaine — ce tronçon-là sort de
    l'enveloppe sur 32 valeurs quand les trois consignes anglaises du banc n'en
    sortent sur aucune. Un caractère hors ASCII imprimable trahit à coup sûr une
    autre langue ; une phrase française sans accent passe au travers, et il
    n'existe aucun moyen honnête de la reconnaître ici. C'est pourquoi le contrat
    le dit aussi.
    """
    texte = " ".join(str(brut or "").split())
    if not texte:
        raise WorkerError(f"aucune consigne en entrée : le champ `{champ}` est vide")

    avertissements: list[str] = []
    intrus = sorted({c for c in texte if not (32 <= ord(c) < 127)})
    if intrus:
        détail = ", ".join(repr(c) for c in intrus[:5])
        avertissements.append(
            f"`{champ}` contient {len(intrus)} caractère(s) hors ASCII imprimable "
            f"({détail}) : ces modèles n'ont vu que de l'anglais, et l'échec est muet. "
            "Mesuré, une consigne en français rend un tronçon parfaitement bien formé, "
            "dans les bornes, et hors distribution — rien dans la sortie ne le dit"
        )
    return texte, avertissements


def lire_etat(brut: Any, attendu: int | None, champ: str = "state") -> list[float]:
    """L'état articulaire vers une liste de flottants, à la dimension du modèle.

    La dimension attendue vient des **statistiques de normalisation**, jamais du
    `config.json` : celui du titulaire annonce `observation.state` de forme `[6]`
    alors que ses statistiques sont de dimension 8, et c'est le normaliseur qui
    décide. Un état de dimension 6 lève `RuntimeError: The size of tensor a (6)
    must match the size of tensor b (8)` — un message qui ne nomme ni le champ ni
    le fichier, et qui envoie chercher au mauvais endroit. Le refus est donc pris
    ici, où l'on sait dire les deux nombres.
    """
    valeur = brut
    if isinstance(valeur, str):
        try:
            valeur = json.loads(valeur)
        except ValueError as exc:
            raise WorkerError(
                f"`{champ}` illisible : une liste de nombres était attendue, "
                f"reçu {brut!r}"
            ) from exc
    if not isinstance(valeur, Sequence) or isinstance(valeur, (str, bytes)):
        raise WorkerError(f"`{champ}` : liste de nombres attendue, reçu {type(brut).__name__}")

    nombres: list[float] = []
    for rang, élément in enumerate(valeur):
        if isinstance(élément, bool) or not isinstance(élément, (int, float)):
            raise WorkerError(f"`{champ}`[{rang}] : nombre attendu, reçu {élément!r}")
        nombres.append(float(élément))

    if not nombres:
        raise WorkerError(f"`{champ}` est vide : le modèle a besoin de savoir où le bras est")
    if len(nombres) > ETAT_MAX:
        raise WorkerError(f"`{champ}` : {len(nombres)} composantes, le contrat en borne {ETAT_MAX}")
    if attendu is not None and len(nombres) != attendu:
        raise WorkerError(
            f"`{champ}` de dimension {len(nombres)}, ce modèle en attend {attendu}. "
            "La dimension est celle des statistiques de normalisation publiées avec les "
            "poids, et non celle qu'annonce `config.json` — sur ce checkpoint les deux "
            "diffèrent, et c'est la première qui décide"
        )
    return nombres


def verifier_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """L'incarnation, la convention et les unités, ou un refus qui dit quoi écrire.

    **Refuser plutôt que servir**, et c'est LA décision de ce fichier. Ces trois
    valeurs ne se lisent nulle part dans un checkpoint : il sait combien de
    nombres il produit, pas quelle machine les exécute ni ce qu'ils veulent dire.
    Les rendre facultatives donnerait un tronçon anonyme — sept flottants dont on
    ne peut pas dire s'ils sont des mètres, des degrés ou des consignes de
    contrôleur, ni s'il faut les ajouter à la pose courante ou les viser. C'est
    exactement l'objet que le contrat existe pour empêcher.

    Le refus tombe au chargement, pas au premier job : un manifeste incomplet est
    une faute de manifeste, et la découvrir après le warmup ne l'améliore pas.
    """
    manquantes = [clé for clé in OPTIONS_REQUISES if not str(options.get(clé) or "").strip()]
    if manquantes:
        raise WorkerError(
            f"options manquantes au manifeste : {', '.join(manquantes)}. Un tronçon "
            "d'actions ne circule pas sans son incarnation — `embodiment` nomme le robot "
            f"et son contrôleur, `space` vaut {' ou '.join(ESPACES)}, `units` vaut "
            f"{' ou '.join(UNITES)}. Aucune des trois ne se lit sur les poids"
        )

    espace = str(options["space"]).strip()
    if espace not in ESPACES:
        raise WorkerError(f"`space` = {espace!r} : attendu {', '.join(ESPACES)}")
    unités = str(options["units"]).strip()
    if unités not in UNITES:
        raise WorkerError(f"`units` = {unités!r} : attendu {', '.join(UNITES)}")

    pince = options.get("gripper_index")
    if pince is not None and (isinstance(pince, bool) or not isinstance(pince, int)):
        raise WorkerError(f"`gripper_index` : entier ou absent, reçu {pince!r}")

    return {
        "embodiment": str(options["embodiment"]).strip(),
        "space": espace,
        "units": unités,
        "gripper_index": pince,
    }


def domaine(
    actions: Sequence[Sequence[float]], bornes: Mapping[str, Sequence[float]] | None
) -> dict[str, Any]:
    """Le tronçon confronté à l'enveloppe des actions vues à l'entraînement.

    **Le seul contrôle de fond disponible sans robot**, et il faut dire exactement
    ce qu'il vaut : il ne juge pas le geste, il constate qu'aucun nombre ne sort
    de la plage que le modèle a lui-même publiée. Un tronçon massivement hors
    bornes signale une entrée hors distribution — mauvaise dimension d'état,
    image d'un autre monde, consigne dans une autre langue — et non une panne.

    Le compte et la marge sont rendus avec le drapeau, et c'est mesuré qui l'a
    voulu : sur le titulaire, la commande de pince dépasse ±1 de quelques
    centièmes à peu près à chaque tronçon, parce que l'intégrateur de flux ne sait
    pas s'arrêter pile sur une valeur binaire. Un booléen seul ferait passer ce
    dépassement-là pour la même chose qu'un tronçon parti ailleurs.

    La marge est rapportée à l'étendue de chaque composante : un dépassement de
    0,02 sur une pince qui vaut ±1 et le même sur une rotation qui vaut ±0,26 ne
    disent pas la même chose.
    """
    if not bornes:
        return {
            "domain_ok": False,
            "out_of_domain": 0,
            "domain_margin": None,
            "warnings": [
                "ce variant ne publie pas les statistiques de ses actions : le domaine "
                "n'est pas vérifiable, et `domain_ok` vaut faux faute de pouvoir répondre "
                "plutôt que par constat"
            ],
        }

    mini, maxi = list(bornes["min"]), list(bornes["max"])
    hors, marge = 0, 0.0
    for pas in actions:
        for axe, valeur in enumerate(pas):
            if axe >= len(mini):
                continue
            étendue = max(maxi[axe] - mini[axe], 1e-9)
            dépassement = max(mini[axe] - valeur, valeur - maxi[axe], 0.0)
            if dépassement > 0.0:
                hors += 1
                marge = max(marge, dépassement / étendue)
    return {
        "domain_ok": hors == 0,
        "out_of_domain": hors,
        "domain_margin": round(marge, 6),
        "warnings": [],
    }


def empreinte(actions: Sequence[Sequence[float]]) -> str:
    """Empreinte du tronçon, pour que la reproductibilité se constate au lieu de se croire.

    Deux jobs de même graine doivent rendre la même — c'est ainsi que le banc
    vérifie le déterminisme sans comparer trois cent cinquante nombres à l'œil. Et
    deux consignes différentes doivent en rendre deux : si elles n'en rendent
    qu'une, le canal de langue est mort et le modèle n'écoute pas. Ce seul champ
    porte deux des quatre contrôles possibles sans robot.

    Les nombres sont sérialisés avec toute leur précision : arrondir masquerait
    précisément l'écart d'un dernier bit qu'on cherche à voir.
    """
    condensat = hashlib.sha256()
    for pas in actions:
        condensat.update(",".join(repr(float(v)) for v in pas).encode("utf-8"))
        condensat.update(b";")
    return condensat.hexdigest()


def version_lerobot(version: str) -> list[str]:
    """Signale une branche de lerobot autre que celle sur laquelle on a mesuré.

    Signale, et ne refuse pas — contrairement à `cad_recode`, qui refuse
    transformers ≥5. La différence est dans le mode de panne : là-bas, une version
    trop récente rend du Python plausible et **faux**, sans exception ; ici, un
    changement de format des fichiers de processeurs lève au chargement, bruyamment.
    Un refus n'y ajouterait rien que la borne du `pyproject.toml` ne fasse déjà.
    """
    if version.strip().startswith(LEROBOT_MESURE + "."):
        return []
    return [
        f"lerobot {version} : cet adaptateur a été écrit et mesuré sur la branche "
        f"{LEROBOT_MESURE}.x, et le format des fichiers de processeurs a déjà changé une "
        f"fois en amont. Vérifier runtimes/{ENV_NAME}/pyproject.toml"
    ]


def resolve_image(valeur: Any, job_dir: Path, champ: str) -> Path:
    """Le chemin d'une vue, relatif au dossier du job quand il l'est.

    Le superviseur copie l'entrée dans le dossier du job et transmet un chemin
    relatif — c'est ce qui rend le job rejouable ailleurs. Un chemin absolu reste
    accepté : le banc d'essai en passe.
    """
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


def weights_dir(variant: Mapping[str, Any]) -> Path:
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


def vlm_dir(variant: Mapping[str, Any]) -> Path:
    """La dorsale vision-langage, qui vient d'un **autre dépôt** que la politique.

    SmolVLA n'embarque pas son encodeur : sa configuration nomme
    `HuggingFaceTB/SmolVLM2-500M-Video-Instruct`, et sans ce second dépôt le
    chargement partirait le chercher sur le réseau — ce qu'un worker du parc n'a
    pas le droit de faire, et que `HF_HUB_OFFLINE=1` transformerait en panne
    illisible plusieurs secondes après le début du warmup.

    Le manifeste le déclare dans `extra_sources` sous `role: vision_encoder`,
    `ecurie pull` le ramène et le superviseur le transmet ici. Le message d'échec
    nomme le rôle et le champ, parce que les deux façons de se tromper — un
    manifeste sans `extra_sources`, un `pull` qui n'a ramené qu'un dépôt — ne se
    réparent pas de la même manière.
    """
    chemins = variant.get("extra_paths") or {}
    brut = str(chemins.get(ROLE_VLM) or "").strip()
    if not brut:
        raise WorkerError(
            "aucune dorsale vision-langage transmise : ce variant a besoin d'un second "
            f"dépôt, déclaré dans `extra_sources` du manifeste avec `role: {ROLE_VLM}`. "
            "La politique ne contient que ses propres poids, pas son encodeur"
        )
    chemin = Path(brut)
    if not (chemin / "config.json").is_file():
        raise WorkerError(
            f"config.json absent de {chemin} — vérifier les `allow_patterns` de la source "
            f"`{ROLE_VLM}` du manifeste, puis `ecurie pull {variant.get('ref') or ''}`"
        )
    return chemin


def fichier_statistiques(preprocesseur: Mapping[str, Any]) -> str | None:
    """Le nom du fichier de statistiques, lu dans le pipeline plutôt que deviné.

    Il s'appelle aujourd'hui `policy_preprocessor_step_5_normalizer_processor.safetensors`,
    et le **5** est un rang dans une liste d'étapes. Écrire ce nom en dur reviendrait
    à parier qu'aucune étape ne sera jamais insérée avant. Le pipeline le déclare
    lui-même sous `state_file` ; on le lit là.
    """
    for étape in preprocesseur.get("steps") or []:
        if "normalizer" in str(étape.get("registry_name") or "") and étape.get("state_file"):
            return str(étape["state_file"])
    return None


def _entier(
    nom: str, defaut: int, plancher: int, plafond: int, couches: tuple[Mapping[str, Any], ...]
) -> int:
    valeur = None
    for couche in couches:
        if couche.get(nom) is not None:
            valeur = couche[nom]
            break
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


# --- l'adaptateur -------------------------------------------------------------


class SmolvlaActWorker(Worker):
    """Vue de caméra, consigne et état vers un tronçon de commandes, par SmolVLA."""

    name = "smolvla-act"

    def __init__(self) -> None:
        self.torch: Any = None
        self.policy: Any = None
        self.pre: Any = None
        self.post: Any = None
        self.defaults: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.incarnation: dict[str, Any] = {}
        self.identite: dict[str, Any] = {}
        self.cameras: tuple[str, ...] = ()
        self.etat_dim: int | None = None
        self.dof: int = 0
        self.bornes: dict[str, list[float]] | None = None
        self.avertissements_chargement: list[str] = []
        self._peak_driver: int = 0

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        chemin = weights_dir(variant)
        vlm = vlm_dir(variant)
        self.defaults = dict(variant.get("defaults") or {})
        self.options = dict(variant.get("options") or {})
        # Avant tout chargement : un manifeste sans incarnation est une faute de
        # manifeste, et le découvrir après le warmup ne l'améliore pas.
        self.incarnation = verifier_options(self.options)

        try:
            import lerobot
            import torch
            from lerobot.policies.factory import make_pre_post_processors
            from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig
            from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy
        except ImportError as exc:
            raise WorkerError(
                f"runtime lerobot indisponible dans cet environnement ({exc}) — `{REPAIR}`"
            ) from exc

        self.torch = torch
        self._exiger_mps(torch)
        self.avertissements_chargement = version_lerobot(str(getattr(lerobot, "__version__", "?")))

        try:
            config = SmolVLAConfig.from_pretrained(str(chemin))
        except Exception as exc:  # noqa: BLE001 — code amont : le message importe plus que le type
            raise WorkerError(
                f"configuration illisible depuis {chemin} : {type(exc).__name__}: {exc}"
            ) from exc

        # Les deux seules retouches, et chacune répare une adresse qui ne vaut
        # que sur la machine de l'entraîneur : `cuda`, et un identifiant de dépôt
        # que `from_pretrained` irait chercher sur le réseau.
        config.device = "mps"
        config.vlm_model_name = str(vlm)

        try:
            policy = SmolVLAPolicy.from_pretrained(str(chemin), config=config)
        except Exception as exc:  # noqa: BLE001
            raise WorkerError(
                f"chargement de la politique impossible depuis {chemin} : "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self.policy = policy.eval().to("mps")

        try:
            self.pre, self.post = make_pre_post_processors(
                config,
                str(chemin),
                preprocessor_overrides={
                    # SANS CETTE LIGNE, RIEN NE DÉMARRE. Voir l'en-tête : le
                    # `device: "cuda"` de `policy_preprocessor.json` est un
                    # fichier distinct de celui que lerobot corrige tout seul, et
                    # `get_safe_torch_device` lève au lieu de se replier.
                    "device_processor": {"device": "mps"},
                    # Le tokeniseur du pipeline est désigné par un identifiant de
                    # dépôt. Pointé sur le dossier local, il ne sort pas.
                    "tokenizer_processor": {"tokenizer_name": str(vlm)},
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise WorkerError(
                f"pipeline de processeurs inconstructible depuis {chemin} : "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        self.cameras = tuple(config.image_features)
        if not self.cameras:
            raise WorkerError(
                f"aucune caméra déclarée par {chemin}/config.json : cette capacité part "
                "d'une vue, et un modèle qui n'en déclare aucune ne la sert pas"
            )
        action = config.action_feature
        self.dof = int(action.shape[0]) if action is not None else 0
        if self.dof <= 0:
            raise WorkerError(f"{chemin}/config.json ne déclare aucune action à produire")

        self.etat_dim, self.bornes, lus = self._statistiques(chemin)
        self.avertissements_chargement += lus

        pince = self.incarnation.get("gripper_index")
        if pince is not None and not 0 <= pince < self.dof:
            raise WorkerError(
                f"`gripper_index` = {pince} hors des {self.dof} composantes que ce modèle "
                "produit — l'index est celui de la sortie, pas celui de l'état"
            )

        self.identite = {
            "ref": variant.get("ref"),
            "repo": variant.get("repo"),
            "revision": variant.get("revision"),
            **self.incarnation,
        }
        self.mps_counters()

        return {
            "device": "mps",
            "cameras": list(self.cameras),
            "dof": self.dof,
            "horizon": int(getattr(config, "chunk_size", 0) or 0),
            "state_dim": self.etat_dim,
            "vision_encoder": str(vlm),
            **self.incarnation,
            "versions": self.versions(),
        }

    def _exiger_mps(self, torch: Any) -> None:
        """MPS ou rien, et refusé plutôt que replié en silence.

        Un profil mesuré sur Metal ne décrit pas un run processeur : retomber sans
        le dire ferait inscrire au manifeste un pic qui n'a jamais eu lieu, et le
        contrôle d'admission s'appuie dessus.
        """
        if not torch.backends.mps.is_available():
            raise WorkerError(
                "backend MPS indisponible (torch.backends.mps.is_available() est faux) — "
                "cet adaptateur ne sert que sur Apple Silicon ; vérifier que "
                f"runtimes/{ENV_NAME}/.venv utilise un Python arm64"
            )

    def _statistiques(
        self, chemin: Path
    ) -> tuple[int | None, dict[str, list[float]] | None, list[str]]:
        """La dimension de l'état et l'enveloppe des actions, lues sur les poids.

        Deux faits sortent de ce fichier et de nulle part ailleurs. La dimension
        de `observation.state` d'abord : le `config.json` du titulaire annonce 6
        et les statistiques en portent 8, et c'est le normaliseur qui applique.
        L'enveloppe des actions ensuite — `min`, `max`, `q01`, `q99` —, sans
        laquelle il n'y a aucun contrôle de fond possible sans robot.

        Le fichier est retrouvé par le `state_file` que le pipeline déclare, et
        non par son nom : celui-ci porte le **rang** de l'étape de normalisation,
        qu'une version d'amont peut déplacer.

        Un fichier absent n'empêche pas de servir : la dimension de l'état retombe
        alors sur le `config.json`, le domaine devient invérifiable, et les deux
        sont dits. Refuser ici priverait le parc d'un variant qui fonctionne pour
        un contrôle qui n'existait pas avant lui.
        """
        avertissements: list[str] = []
        pipeline = chemin / "policy_preprocessor.json"
        if not pipeline.is_file():
            return None, None, [
                f"policy_preprocessor.json absent de {chemin} : ni la dimension de l'état "
                "ni l'enveloppe des actions ne sont vérifiables. Vérifier les "
                "`allow_patterns` du manifeste"
            ]
        try:
            document = json.loads(pipeline.read_text(encoding="utf-8"))
        except ValueError as exc:
            return None, None, [f"policy_preprocessor.json illisible : {exc}"]

        nom = fichier_statistiques(document)
        if nom is None or not (chemin / nom).is_file():
            return None, None, [
                "aucun fichier de statistiques de normalisation dans le pipeline de ce "
                "variant : le domaine des actions n'est pas vérifiable"
            ]

        try:
            from safetensors.numpy import load_file
        except ImportError as exc:
            return None, None, [f"safetensors absent de l'environnement ({exc}) — `{REPAIR}`"]

        try:
            tenseurs = load_file(str(chemin / nom))
        except Exception as exc:  # noqa: BLE001 — fichier amont : le message importe
            return None, None, [f"statistiques illisibles ({nom}) : {type(exc).__name__}: {exc}"]

        état = tenseurs.get("observation.state.mean")
        dimension = int(état.shape[0]) if état is not None else None
        if dimension is None:
            avertissements.append(
                "aucune statistique `observation.state` dans ce variant : la dimension "
                "attendue de l'état n'est pas vérifiable, et une dimension fausse ne "
                "lèvera qu'au fond du modèle"
            )

        bornes: dict[str, list[float]] | None = {}
        for clé in ("min", "max", "q01", "q99"):
            tenseur = tenseurs.get(f"action.{clé}")
            if tenseur is None:
                bornes = None
                avertissements.append(
                    f"`action.{clé}` absent des statistiques : le domaine des actions "
                    "n'est pas vérifiable sur ce variant"
                )
                break
            bornes[clé] = [float(v) for v in tenseur.reshape(-1)]
        return dimension, bornes, avertissements

    # --- inférence -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.policy is None or self.torch is None:
            raise WorkerError("infer avant load — aucune politique en mémoire")

        plan = plan_action(
            entree=request.input, params=request.params, defaults=self.defaults
        )
        avertissements = list(self.avertissements_chargement) + list(plan.warnings)

        progress(5, "lecture de la consigne")
        consigne, dits = lire_consigne(request.get("instruction"))
        avertissements += dits
        état = lire_etat(request.get("state"), self.etat_dim)

        progress(20, "lecture des vues")
        vues, dits = self._vues(request)
        avertissements += dits

        progress(40, "échantillonnage du tronçon")
        tronçon = self._tronçon(consigne, état, vues, plan)

        progress(80, "domaine et écriture")
        contrôle = domaine(tronçon, self.bornes)
        avertissements += contrôle.pop("warnings")
        # L'avertissement qui ne dépend d'aucune condition, et le seul du parc dans
        # ce cas. Il n'est pas là pour signaler un défaut : il est là parce que
        # personne ne doit pouvoir lire ces nombres sans lire en même temps le nom
        # de la machine pour laquelle ils ont été calculés.
        avertissements.append(
            f"tronçon calculé pour {self.incarnation['embodiment']}, en "
            f"{self.incarnation['space']} et unités {self.incarnation['units']} : aucun "
            "robot du parc ne l'exécute, et rien ici ne dit si le geste est le bon — "
            "seulement s'il est bien formé, dans le domaine et reproductible"
        )

        sortie = self._ecrire(request.output_dir, tronçon, consigne, plan, contrôle, avertissements)

        métriques: dict[str, Any] = {
            "device": "mps",
            "steps": plan.steps,
            "seed": plan.seed,
            "cameras": len(vues),
            "dof": self.dof,
            "horizon": len(tronçon),
            "state_dim": len(état),
            "embodiment": self.incarnation["embodiment"],
            "space": self.incarnation["space"],
            "units": self.incarnation["units"],
            # L'empreinte porte deux des quatre contrôles possibles sans robot :
            # deux graines identiques doivent la répéter, deux consignes
            # différentes doivent en donner deux. Elle est en métriques et pas
            # seulement dans le fichier parce que c'est la ligne de télémétrie
            # que le banc conserve, et qu'un banc qui ne garderait que des durées
            # ne saurait rien dire de la reproductibilité.
            "action_sha256": empreinte(tronçon),
            **contrôle,
            **self.mps_counters(),
            "peak_memory_bytes": self.peak_memory_bytes(),
        }
        if avertissements:
            métriques["warnings"] = avertissements
        return InferResult(output=sortie, metrics=métriques)

    # --- détails -------------------------------------------------------------

    def _vues(self, request: InferRequest) -> tuple[dict[str, Any], list[str]]:
        """Les images du job vers les clés de caméra que le modèle déclare.

        L'appariement est **positionnel** et lu sur les poids : `image` alimente
        la première caméra déclarée, `image2` la deuxième. Les noms de clés ne
        sont écrits nulle part ici — le titulaire les appelle
        `observation.images.camera1/2/3`, un autre membre de la famille les
        appellera autrement, et cet adaptateur n'a pas à le savoir.

        Une vue de trop n'est pas silencieusement jetée : `empty_cameras` vaut 0
        sur les checkpoints vérifiés, les caméras absentes ne sont donc pas
        remplies de zéros masqués mais simplement omises — et une image que
        l'utilisateur a fournie et que le modèle ne regardera pas est une
        information qui se dit.
        """
        torch = self.torch
        avertissements: list[str] = []
        fournies = [
            (champ, request.get(champ))
            for champ in CHAMPS_IMAGES
            if str(request.get(champ) or "").strip()
        ]
        if not fournies:
            raise WorkerError("aucune vue en entrée : le champ `image` est requis")
        if len(fournies) > len(self.cameras):
            surplus = ", ".join(champ for champ, _ in fournies[len(self.cameras) :])
            avertissements.append(
                f"{len(fournies)} vue(s) fournies pour {len(self.cameras)} caméra(s) "
                f"déclarées par ce modèle : {surplus} n'entre(nt) pas dans le calcul"
            )
        if len(fournies) < len(self.cameras):
            avertissements.append(
                f"{len(fournies)} vue(s) fournies pour {len(self.cameras)} caméra(s) "
                "déclarées : les caméras absentes sont omises et non remplies de noir "
                "(`empty_cameras` vaut 0), ce que ce modèle n'a pas vu à l'entraînement"
            )

        vues: dict[str, Any] = {}
        for (champ, valeur), clé in zip(fournies, self.cameras, strict=False):
            chemin = resolve_image(valeur, request.output_dir, champ)
            vues[clé] = self._image(chemin, torch)
        return vues, avertissements

    def _image(self, chemin: Path, torch: Any) -> Any:
        """Une image de fichier vers un tenseur (3, H, W) en flottants [0, 1].

        [0, 1] et non [-1, 1] : c'est `prepare_images` qui fait le passage en
        [-1, 1] qu'attend SigLIP, et le faire ici le ferait deux fois. Le
        rééchantillonnage est laissé au modèle aussi, pour la même raison — il
        conserve les proportions et bourre à la définition de son encodeur, et le
        refaire en amont ajouterait une interpolation qu'il n'a pas vue.
        """
        try:
            from PIL import Image
        except ImportError as exc:
            raise WorkerError(f"Pillow absent de l'environnement ({exc}) — `{REPAIR}`") from exc
        try:
            import numpy as np
        except ImportError as exc:
            raise WorkerError(f"numpy absent de l'environnement ({exc}) — `{REPAIR}`") from exc

        try:
            with Image.open(chemin) as ouverte:
                tableau = np.asarray(ouverte.convert("RGB"), dtype="float32") / 255.0
        except Exception as exc:  # noqa: BLE001 — remonte traduit
            raise WorkerError(
                f"image illisible ({chemin.name}) : {type(exc).__name__}: {exc}"
            ) from exc
        return torch.from_numpy(tableau).permute(2, 0, 1).contiguous()

    def _tronçon(
        self, consigne: str, état: list[float], vues: dict[str, Any], plan: Demande
    ) -> list[list[float]]:
        """L'appel au modèle, et les deux précautions qui l'encadrent.

        *Le bruit est tiré sur le processeur, à graine explicite.* Sa forme est
        `(1, chunk_size, max_action_dim)` — 32 et non 7 : c'est celle que
        `sample_actions` construit quand on ne lui en donne pas, et un bruit à la
        dimension de l'action est refusé par l'intégrateur. Tiré sur le processeur
        plutôt que sur Metal parce qu'un générateur de périphérique ne promet pas
        la même suite d'une version de torch à l'autre, et que la reproductibilité
        de cette capacité est ce que le banc mesure.

        *Le batch est copié, et la politique remise à zéro.* `predict_action_chunk`
        écrit dans le dictionnaire qu'on lui passe — `populate_queues` y range les
        observations, puis `_get_action_chunk` remplace les entrées par des piles.
        Sans copie, deux jobs successifs d'un même worker dériveraient en silence ;
        sans `reset`, les files d'observations garderaient celles du job d'avant.
        """
        torch = self.torch
        policy = self.policy
        # `num_steps` vit dans la configuration que la politique et son modèle
        # partagent : le poser ici suffit, et il est reposé à chaque job plutôt
        # que restauré — aucun job ne doit hériter du réglage du précédent.
        policy.config.num_steps = plan.steps

        générateur = torch.Generator(device="cpu").manual_seed(plan.seed)
        bruit = torch.randn(
            (1, policy.config.chunk_size, policy.config.max_action_dim),
            generator=générateur,
            dtype=torch.float32,
        ).to("mps")

        batch: dict[str, Any] = {
            "task": consigne,
            "observation.state": torch.tensor(état, dtype=torch.float32),
            **vues,
        }
        policy.reset()
        try:
            préparé = self.pre(dict(batch))
            brut = policy.predict_action_chunk(dict(préparé), noise=bruit)
            actions = self.post(brut)
        except Exception as exc:  # noqa: BLE001 — code amont : le message importe plus que le type
            raise WorkerError(
                f"échantillonnage impossible : {type(exc).__name__}: {exc}"
            ) from exc
        torch.mps.synchronize()
        self.mps_counters()

        tableau = actions.detach().float().cpu().numpy()
        if tableau.ndim == 3:
            tableau = tableau[0]
        if tableau.ndim != 2:
            raise WorkerError(
                f"tronçon de forme inattendue {tableau.shape} : deux dimensions étaient "
                "attendues après retrait du lot — le temps, puis les degrés de liberté"
            )
        return [[float(v) for v in pas] for pas in tableau]

    def _ecrire(
        self,
        job_dir: Path,
        tronçon: list[list[float]],
        consigne: str,
        plan: Demande,
        contrôle: dict[str, Any],
        avertissements: list[str],
    ) -> dict[str, Any]:
        """Le fichier du tronçon, puis la réponse du job.

        Le fichier porte tout ce qu'il faut pour se relire seul six mois plus
        tard : l'incarnation, la convention, les unités, les bornes du domaine et
        la consigne qui l'a produit. Un tableau de nombres rangé sans eux ne se
        relit pas — et le relire de travers est ce qui casse un bras.
        """
        premier = list(tronçon[0]) if tronçon else []
        index = self.incarnation.get("gripper_index")
        pince = premier[index] if index is not None and index < len(premier) else None

        document = {
            **self.identite,
            "instruction": consigne,
            "steps": plan.steps,
            "seed": plan.seed,
            "deterministic": True,
            "dof": self.dof,
            "horizon": len(tronçon),
            "action_sha256": empreinte(tronçon),
            **contrôle,
            "bounds": self.bornes,
            "gripper": pince,
            "warnings": avertissements,
            "actions": [[round(v, 6) for v in pas] for pas in tronçon],
        }
        (job_dir / SORTIE_JSON).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        sortie: dict[str, Any] = {
            "actions": SORTIE_JSON,
            "first_action": [round(v, 6) for v in premier],
            "dof": self.dof,
            "horizon": len(tronçon),
            "space": self.incarnation["space"],
            "units": self.incarnation["units"],
            "embodiment": self.incarnation["embodiment"],
            "domain_ok": contrôle["domain_ok"],
            "out_of_domain": contrôle["out_of_domain"],
            # Vrai sans condition sur ce runtime : le bruit est posé par la
            # graine, et c'est le seul tirage au sort du chemin. Le champ existe
            # pour les modèles de cette capacité dont ce ne serait pas le cas —
            # un décodeur qui échantillonnerait ses jetons, par exemple.
            "deterministic": True,
        }
        if pince is not None:
            sortie["gripper"] = round(pince, 6)
        return sortie

    # --- mémoire et versions -------------------------------------------------

    def mps_counters(self) -> dict[str, int]:
        """Compteurs MPS instantanés. Aucun n'est un pic — les noms le disent.

        Tout relevé nourrit au passage le maximum retenu pour le profil :
        `driver_allocated_memory` redescend aussi vite qu'il monte, et le maximum
        se tient à chaque relevé plutôt qu'il ne se lit une fois à la fin.
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
        """Le maximum des relevés du pilote Metal et du pic RSS.

        **Et c'est le RSS qui gagne ici, contrairement à la plupart des modèles
        torch du parc.** Mesuré au banc sur cette machine : pilote 1,36 Gio, RSS
        3,20 Gio. La politique ne pèse que 450 millions de paramètres en
        bfloat16, mais le processus porte en plus le mmap des poids, la pile
        torch, transformers et son tokeniseur — invisibles à Metal, et bien réels
        pour le budget unifié. Inscrire le seul chiffre du pilote, comme le fait
        un rapport de veille qui n'a pas relevé le RSS, sous-estimerait le coût
        d'un facteur trois.
        """
        self.mps_counters()
        return max(self._peak_driver, peak_rss_bytes() or 0) or None

    def unload(self) -> None:
        """Rend la mémoire au budget, pas seulement à Python.

        Sans `empty_cache`, l'allocateur MPS garde ses pools : les tenseurs sont
        libérés, le pilote tient toujours les octets, et le résident suivant se
        voit refuser l'admission pour une mémoire que plus personne n'utilise.
        """
        self.policy = None
        self.pre = None
        self.post = None
        if self.torch is not None:
            gc.collect()
            try:
                self.torch.mps.empty_cache()
            except (AttributeError, RuntimeError):
                pass

    def versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for nom in ("torch", "transformers", "lerobot", "numpy"):
            try:
                importé = __import__(nom, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


if __name__ == "__main__":
    # `HF_HUB_OFFLINE` est déjà posé par le superviseur ; on le redit pour le cas
    # où l'adaptateur serait lancé à la main, parce que la moindre fuite réseau
    # ici irait chercher une dorsale de deux gigaoctets hors de toute
    # comptabilité disque.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    raise SystemExit(main(SmolvlaActWorker))
