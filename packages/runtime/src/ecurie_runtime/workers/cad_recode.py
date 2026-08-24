"""Adaptateur `cad-recode` : un nuage de points entre, un **programme** sort.

Quinzième famille du parc, et la seule dont la sortie soit du code. Les autres
capacités 3D rendent une surface figée ; celle-ci rend les quelques lignes de
CadQuery qui la construisent, donc une pièce qu'on peut encore modifier.

Le modèle est un Qwen2-1.5B dont l'entrée a été détournée : les 256 premières
positions du contexte ne portent pas des jetons mais les points du nuage, encodés
en séries de Fourier par une seule couche linéaire ajoutée, et repérées dans le
masque d'attention par la valeur **-1**. Le corps du modèle de langue est
inchangé — c'est pourquoi il écrit du Python correct, il n'a jamais appris autre
chose. Cette valeur -1 est aussi la cause de tout ce qui suit.

**LE CODE D'INFÉRENCE N'EST PAS DANS CE DÉPÔT, ET NE PEUT PAS Y ÊTRE.** La classe
`CADRecode` et son `FourierPointEncoder` sont sous **CC BY-NC 4.0**, comme les
poids : le `LICENSE.md` du dépôt amont est un Attribution-NonCommercial. Ils sont
vendorés sous `runtimes/cad-recode/vendor/`, non versionné, par une commande que
le README de l'env donne — même chemin que `runtimes/hunyuan3d/`, à ceci près que
l'amont ne publie ici aucun module : le code vit dans une cellule de
`demo.ipynb`, d'où `vendorer.py`. Le second chemin envisagé — réécrire la classe
depuis l'article — a été écarté sans hésitation : ce code a été lu pour instruire
le dossier, et affirmer ensuite l'avoir réécrit sans l'avoir regardé aurait été
malhonnête.

**TRANSFORMERS ≥5 NE PLANTE PAS, IL MENT.** C'est le risque numéro un de cette
capacité, et l'adaptateur refuse de charger au-delà plutôt que de le laisser
arriver. Mesuré sur les vrais poids avec 5.15.1 : `from_pretrained` lève d'abord
`AttributeError: all_tied_weights_keys` ; rustiné, `generate()` rend
`import cadquery as cq\\nw0r<|im_start|>import cadquery…` en boucle, parce que
transformers ≥5 calcule `position_ids = attention_mask.cumsum(-1) - 1` sur un
masque qui porte des **-1** — les positions valent -2, -3, … -257. Il n'y a
aucune exception : il y a du Python plausible et faux. Un contrôle de version
est peu de chose ; ici c'est la seule barrière entre un banc au vert et une
capacité silencieusement fausse.

**Trois pièges de lecture, tous mesurés, tous corrigés ici.** `trimesh.load` d'un
GLB rend une `Scene` et non un `Trimesh` — c'est exactement ce que produit
`image-to-mesh`, donc le chaînage naturel tombe dans ce cas ; un `.ply` de points
purs rend un `PointCloud` sans faces, qu'il ne faut surtout pas échantillonner ;
un `.stl` rend un `Trimesh`. Trois branches, pas une. Et `np.random.seed()`, que
le démonstrateur amont emploie pour sa reproductibilité, est **inerte** avec
trimesh 5.0.0 : seul `sample_surface(..., seed=…)` en argument nommé fixe le
tirage. Qui recopie la recette amont obtient une capacité irreproductible sans
qu'aucun message ne le signale.

**Exécuter le programme est une décision, pas une commodité**, et le contrat la
laisse fausse par défaut. Ce que l'adaptateur peut réellement contenir est écrit
dans `EXECUTEUR` et dans les avertissements du job : sur macOS, `RLIMIT_AS`,
`RLIMIT_DATA` et `RLIMIT_RSS` sont **refusées** par le noyau quelle que soit la
valeur demandée (mesuré : `ValueError: current limit exceeds maximum limit`, alors
que la limite dure vaut l'infini). La borne mémoire annoncée par le dossier
d'instruction n'existe donc pas ici, et l'adaptateur le dit au lieu de la
simuler.

Rien de torch, transformers, trimesh, numpy ni cadquery n'est importé au niveau
du module (voir `workers/__init__.py`) : la CI importe tous les adaptateurs sans
Apple Silicon et sans venv de runtime.
"""

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
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

ENV_NAME = "cad-recode"
REPAIR = f"ecurie env sync {ENV_NAME}"

# La seconde étape, que `env sync` ne fait pas. Écrite en toutes lettres partout
# où elle peut manquer : c'est le seul moyen de la découvrir avant d'avoir
# téléchargé trois gigaoctets.
VENDOR_COMMANDES = (
    "git clone --depth 1 https://github.com/filaPro/cad-recode "
    "runtimes/cad-recode/vendor/cad-recode\n"
    "    python3 runtimes/cad-recode/vendorer.py"
)

MODULE_VENDORE = "cad_recode_model"
VAR_VENDOR = "ECURIE_CAD_RECODE_VENDOR"
VAR_DEVICE = "ECURIE_CAD_RECODE_DEVICE"

PROGRAMME_PY = "programme.py"
PIECE_STEP = "piece.step"
PIECE_GLB = "piece.glb"
EXECUTEUR_PY = "executeur.py"
EXECUTION_JSON = "execution.json"

# Ce que trimesh sait ouvrir et que cette capacité sait interpréter. `.glb` en
# premier de la liste par ordre alphabétique, mais surtout par importance : c'est
# ce que rend `image-to-mesh`, et le chaînage est la raison d'être de ce champ.
GEOMETRIES = {".glb", ".gltf", ".obj", ".off", ".ply", ".stl"}

# La valeur d'entraînement. Toute autre est hors distribution — le contrat laisse
# descendre pour mesurer le coût, pas pour améliorer la sortie.
N_POINTS_ENTRAINEMENT = 256
N_POINTS_MIN = 64
N_POINTS_MAX = 512

# Points tirés de la surface avant l'échantillonnage du plus lointain, comme
# l'amont. Assez pour que le tirage ne décide de rien : c'est le second passage
# qui choisit, et il choisit par distance.
N_PRE_POINTS = 8192

JETONS_MIN = 128
JETONS_MAX = 2048
JETONS_DEFAUT = 768

# Les trois jetons spéciaux du tokenizer de Qwen2, vérifiés sur le tokenizer
# réel : `<|im_start|>` ouvre le programme, `<|endoftext|>` le ferme, et
# `<|im_end|>` sert de remplissage aux positions du nuage.
DEBUT = "<|im_start|>"
FIN = "<|endoftext|>"
REMPLISSAGE = "<|im_end|>"

EXECUTIONS = ("non-demandee", "ok", "erreur", "delai-depasse")

# Délai d'horloge du sous-processus. Trois chiffres circulent en amont sans
# qu'aucun ne soit tenu — 3 s recommandées dans le démonstrateur, 10 s dans son
# code, 7 s dans son message d'erreur. On prend large : la construction mesurée
# des pièces simples tient en quelques millisecondes, et un délai serré
# transformerait une pièce compliquée en faux négatif.
DELAI_S = 20.0

# Ce que macOS accepte réellement de borner — voir l'en-tête. RLIMIT_CPU tue par
# SIGXCPU (mesuré), RLIMIT_FSIZE refuse l'écriture par EFBIG (mesuré).
#
# **La borne processeur est plus courte que le délai d'horloge, et cet écart est
# le fruit d'un essai raté.** Aux deux à 20 s, la boucle infinie du jeu d'épreuve
# était toujours coupée par le délai du parent, jamais par SIGXCPU : les deux
# arrivaient ensemble et le parent gagnait. Les cinq secondes d'écart donnent
# leur rôle à chacune — le processeur attrape le calcul emballé, l'horloge
# attrape ce que le processeur ne voit pas, c'est-à-dire l'attente.
CPU_S = 15
FICHIER_MAX = 512 * 1024 * 1024
PROCESSUS_MAX = 64


# --- ce qui se vérifie sans poids ---------------------------------------------


@dataclass(frozen=True)
class Demande:
    """Ce qui a été demandé, résolu, et ce qui n'a pas pu l'être."""

    n_points: int
    seed: int
    max_new_tokens: int
    executer: bool
    warnings: tuple[str, ...] = ()


def plan_cao(
    *,
    entree: Mapping[str, Any],
    params: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> Demande:
    """Traduit une demande du protocole en réglages de reconstruction.

    Fonction pure, sans torch ni trimesh : c'est tout ce qui se vérifie sans les
    poids — la priorité des trois couches, les bornes du contrat revérifiées ici
    parce qu'un worker peut être appelé sans passer par la validation du contrat,
    et l'avertissement sur `n_points`, qui est le seul réglage de cette capacité
    dont une valeur légale puisse dégrader la sortie en silence.
    """
    couches = (entree, params, defaults)

    n_points = _entier("n_points", N_POINTS_ENTRAINEMENT, N_POINTS_MIN, N_POINTS_MAX, couches)
    seed = _entier("seed", 42, 0, 2**32 - 1, couches)
    jetons = _entier("max_new_tokens", JETONS_DEFAUT, JETONS_MIN, JETONS_MAX, couches)

    executer = _reglage("executer_le_code", *couches)
    avertissements: list[str] = []
    if n_points != N_POINTS_ENTRAINEMENT:
        avertissements.append(
            f"n_points = {n_points} : ce modèle a été entraîné à "
            f"{N_POINTS_ENTRAINEMENT} points, et toute autre valeur est hors "
            "distribution. Le programme produit reste syntaxiquement valide, ce "
            "qui est précisément ce qui rend cet écart difficile à voir"
        )

    return Demande(
        n_points=n_points,
        seed=seed,
        max_new_tokens=jetons,
        executer=bool(executer) if executer is not None else False,
        warnings=tuple(avertissements),
    )


def verifier_version_transformers(version: str) -> None:
    """Refuse transformers ≥5, en nommant ce qui arriverait sinon.

    Le seul contrôle de version du parc qui refuse au lieu d'avertir, et il le
    mérite : au-delà de 4.x cette capacité ne tombe pas en panne, elle produit du
    Python plausible et faux. Un job en échec se voit ; un programme CadQuery
    faux passe le banc, passe la revue rapide, et s'exécute en donnant une pièce
    qui n'est pas celle qu'on a soumise.

    Le refus tombe au chargement plutôt qu'au premier job : c'est la seule
    position d'où il protège aussi le banc d'essai.
    """
    majeure = version.strip().split(".")[0]
    try:
        if int(majeure) < 5:
            return
    except ValueError:
        # Une version illisible (« 5.0.0.dev0+abc ») ne doit pas ouvrir la porte
        # par défaut : on ne sait pas, donc on refuse en le disant.
        raise WorkerError(
            f"version de transformers illisible ({version!r}) — cet adaptateur exige "
            f"strictement transformers < 5, voir runtimes/{ENV_NAME}/pyproject.toml"
        ) from None
    raise WorkerError(
        f"transformers {version} : cette capacité exige strictement < 5, et ce n'est pas "
        "une prudence. Mesuré sur les vrais poids en 5.15.1 : le chargement lève "
        "`AttributeError: all_tied_weights_keys`, et une fois rustiné la génération rend "
        "du Python plausible et FAUX — transformers ≥5 calcule "
        "`position_ids = attention_mask.cumsum(-1) - 1` sur un masque qui porte des -1. "
        f"Corriger la borne de runtimes/{ENV_NAME}/pyproject.toml, puis `{REPAIR}`"
    )


def extraire_programme(decode: str) -> tuple[str, list[str]]:
    """Le programme CadQuery seul, et ce qu'il faut penser de la génération.

    La sortie brute porte les 256 jetons de remplissage du nuage, le jeton
    d'ouverture, le programme, puis le jeton de fin. Découper est trivial ; ce qui
    ne l'est pas, c'est de reconnaître les deux façons dont cette découpe peut
    réussir sur une génération ratée :

    *La fin manquante.* Sans `<|endoftext|>`, la génération a buté sur le plafond
    de jetons. Le programme est alors tronqué au milieu d'une ligne et ne
    s'exécutera pas — mais il a toutes les apparences d'un programme.

    *L'ouverture répétée.* Un second `<|im_start|>` **dans** le programme est la
    signature exacte de la génération en boucle qu'on obtient sous
    transformers ≥5. Le contrôle de version en amont devrait l'avoir rendue
    impossible ; ce test-ci est la seconde barrière, et il coûte une ligne.
    """
    avertissements: list[str] = []

    début = decode.find(DEBUT)
    if début < 0:
        raise WorkerError(
            f"la sortie du modèle ne contient pas {DEBUT} : elle ne commence pas là où "
            "un programme commence, et rien n'en serait extrait qu'on puisse relire"
        )
    corps = decode[début + len(DEBUT) :]

    fin = corps.find(FIN)
    if fin < 0:
        avertissements.append(
            "génération arrêtée par le plafond de jetons, sans jeton de fin : le "
            "programme est tronqué et ne s'exécutera pas. Relever `max_new_tokens`, ou "
            "y voir le signe d'une génération partie en boucle"
        )
    else:
        corps = corps[:fin]

    if DEBUT in corps:
        avertissements.append(
            f"le programme contient un second {DEBUT} : c'est la signature d'une "
            "génération en boucle. Vérifier la version de transformers — au-delà de 4.x "
            "cette capacité produit du Python plausible et faux"
        )

    programme = corps.strip("\n")
    if not programme.strip():
        raise WorkerError("le modèle n'a produit aucun programme entre ses jetons de balise")
    if "cadquery" not in programme:
        avertissements.append(
            "le programme ne mentionne pas cadquery : ce n'est pas ce que cette capacité "
            "décrit, et l'exécuter ferait tourner du code dont on ne sait rien"
        )
    return programme + "\n", avertissements


def resolve_geometrie(valeur: Any, job_dir: Path, champ: str = "geometrie") -> Path:
    """Le chemin de la géométrie, relatif au dossier du job quand il l'est.

    Le superviseur copie l'entrée dans le dossier du job et transmet un chemin
    relatif — c'est ce qui rend le job rejouable ailleurs. Un chemin absolu reste
    accepté : le banc d'essai en passe.
    """
    brut = str(valeur or "").strip()
    if not brut:
        raise WorkerError(f"aucune géométrie en entrée : le champ `{champ}` est vide")
    chemin = Path(brut).expanduser()
    if not chemin.is_absolute():
        chemin = job_dir / chemin
    if not chemin.is_file():
        raise WorkerError(f"{champ} introuvable : {chemin}")
    if chemin.suffix.lower() not in GEOMETRIES:
        raise WorkerError(
            f"format non géré : {chemin.suffix or '(sans extension)'} — "
            f"attendu {', '.join(sorted(GEOMETRIES))}"
        )
    return chemin


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


def tokenizer_dir(variant: dict[str, Any]) -> Path:
    """Le tokenizer, qui vient d'un **autre dépôt** que les poids.

    Premier emploi d'`extra_sources` au parc. Le dépôt de CAD-Recode ne publie
    que cinq fichiers et pas le moindre tokenizer : il faut celui de
    `Qwen/Qwen2-1.5B`, sous une autre licence, épinglé à son propre sha. Le
    manifeste le déclare sous `role: tokenizer`, `ecurie pull` le ramène, et le
    superviseur le transmet ici.

    Le message d'échec nomme le rôle et le champ, parce que les deux façons de se
    tromper — un manifeste sans `extra_sources`, un `pull` qui n'a ramené qu'un
    dépôt — ne se réparent pas de la même manière.
    """
    chemins = variant.get("extra_paths") or {}
    brut = str(chemins.get("tokenizer") or "").strip()
    if not brut:
        raise WorkerError(
            "aucun tokenizer transmis : ce variant a besoin d'un second dépôt, déclaré "
            "dans `extra_sources` du manifeste avec `role: tokenizer`. Le dépôt des poids "
            "de CAD-Recode n'en contient aucun"
        )
    chemin = Path(brut)
    if not (chemin / "tokenizer.json").is_file():
        raise WorkerError(
            f"tokenizer.json absent de {chemin} — vérifier les `allow_patterns` de la "
            f"source `tokenizer` du manifeste, puis `ecurie pull {variant.get('ref') or ''}`"
        )
    return chemin


def candidats_vendor(depuis: Path, courant: Path, depuis_env: str | None = None) -> list[Path]:
    """Où chercher le code vendoré, du plus explicite au plus général.

    Trois pistes, et aucune n'est un comptage de `parents[n]` : le premier
    lancement a échoué exactement là-dessus, sur un `parents[4]` qui désignait
    `packages/` et non la racine du dépôt. Un chemin obtenu en comptant des
    niveaux est faux dès qu'un fichier bouge, et le message d'erreur qu'il produit
    envoie chercher au mauvais endroit.

    On remonte donc jusqu'à trouver `runtimes/<env>/`, ce qui est vrai quel que
    soit l'endroit d'où l'adaptateur est importé, et on essaie aussi le répertoire
    courant — le superviseur lance les workers depuis la racine du dépôt.
    """
    if depuis_env:
        return [Path(depuis_env).expanduser()]
    trouvés = [
        parent / "runtimes" / ENV_NAME / "vendor"
        for parent in (depuis.resolve(), *depuis.resolve().parents)
        if (parent / "runtimes" / ENV_NAME).is_dir()
    ]
    return trouvés + [courant / "runtimes" / ENV_NAME / "vendor"]


def vendor_dir() -> Path:
    """Le dossier qui contient le code d'inférence vendoré, ou un refus qui répare.

    Ce code ne peut pas être versionné ici — il est sous CC BY-NC 4.0. Le refus
    porte donc les deux commandes en clair : les découvrir après trois gigaoctets
    de téléchargement et une minute de chargement serait une perte inutile.
    """
    candidats = candidats_vendor(
        Path(__file__), Path.cwd(), os.environ.get(VAR_VENDOR)
    )
    for candidat in candidats:
        if (candidat / f"{MODULE_VENDORE}.py").is_file():
            return candidat
    cherchés = "\n      ".join(str(c) for c in candidats)
    raise WorkerError(
        f"code d'inférence absent : {MODULE_VENDORE}.py introuvable. Cherché dans :\n"
        f"      {cherchés}\n\n"
        "Il est sous CC BY-NC 4.0 et ne peut pas être versionné dans ce dépôt — le poser "
        f"à la main :\n\n    {VENDOR_COMMANDES}\n\n"
        f"Un dossier rangé autrement se déclare par {VAR_VENDOR}. "
        f"Voir runtimes/{ENV_NAME}/README.md"
    )


#: Ce que l'exécution est censée déposer dans le dossier du job. Tout le reste
#: est une trace, et se dit.
DEPOSES = frozenset({PIECE_STEP, PIECE_GLB, EXECUTION_JSON, EXECUTEUR_PY})


def _entrees(dossier: Path) -> set[str]:
    try:
        return {chemin.name for chemin in dossier.iterdir()}
    except OSError:
        return set()


def _traces(avant: set[str], apres: set[str]) -> list[str]:
    """Ce que le programme engendré a laissé en plus de ses sorties déclarées.

    Le dossier de travail du sous-processus est celui du job, et c'est délibéré :
    un programme qui écrit `sortie.stl` doit le déposer là où on peut le voir
    plutôt que n'importe où. Mais confiner sans jamais regarder ce qu'on a
    confiné ne vaut pas grand-chose : un fichier apparu dans le dossier d'un job
    est une information, et elle se dit.

    C'est un cache de polices qui a fait écrire cette fonction, et il a fallu deux
    diagnostics pour l'expliquer. `ezdxf`, tiré par cadquery, déposait 50 Ko dans
    le dossier du job ; sa constante s'appelle `CACHE_DIRECTORY = ".cache"`, ce
    qui donnait à croire à un chemin relatif au dossier de travail. Mesuré, c'est
    faux : il est résolu depuis `HOME`, et poser `HOME` sur un dossier temporaire
    jetable a suffi à laisser le job propre. La fonction reste, parce que ce
    qu'elle surveille n'est pas ce cache-là mais le programme lui-même.
    """
    nouveaux = sorted(apres - avant - DEPOSES)
    if not nouveaux:
        return []
    return [
        f"le programme a laissé {len(nouveaux)} entrée(s) hors de ses sorties déclarées, "
        f"dans le dossier du job : {', '.join(nouveaux)}"
    ]


def _nom_du_signal(code: int) -> str:
    """Un code de retour de sous-processus en clair, signal nommé quand c'en est un.

    `SIGXCPU` dit « la borne processeur a mordu » ; « code -24 » n'apprend rien à
    qui lit un manifeste de job six mois plus tard.
    """
    if code >= 0:
        return f"code {code}"
    import signal

    try:
        return f"signal {signal.Signals(-code).name}"
    except ValueError:
        return f"signal {-code}"


def _reglage(nom: str, *couches: Mapping[str, Any]) -> Any:
    """Première valeur définie, de la plus prioritaire à la moins : entrée, job, manifeste."""
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


# --- l'exécution du programme engendré ----------------------------------------
#
# Écrit dans le dossier du job à chaque exécution plutôt que rangé à côté de
# l'adaptateur, et ce n'est pas un détail : ce qui a réellement tourné reste avec
# le job, relisible des mois plus tard, à côté du programme qu'il a exécuté. Un
# script rangé ailleurs aurait pu changer entre-temps.

EXECUTEUR = '''"""Exécute un programme CadQuery engendré par un modèle. Écrit par l'adaptateur.

    python -I executeur.py <programme.py> <resultat.json> <step> <glb> <cpu_s> <fsize> <nproc>

Ce que ce script contient, et ce qu'il ne contient pas. Il tourne dans un
interpréteur **séparé** et **isolé** (`-I` : ni PYTHONPATH, ni site utilisateur),
avec un environnement vidé et un dossier de travail limité à celui du job. Il
borne le temps processeur et la taille des fichiers écrits.

Il ne coupe **pas** le réseau, et il ne borne **pas** la mémoire. Sur macOS,
`RLIMIT_AS`, `RLIMIT_DATA` et `RLIMIT_RSS` sont refusées par le noyau quelle que
soit la valeur demandée — mesuré, `ValueError: current limit exceeds maximum
limit`, alors que la limite dure vaut l'infini. Le délai d'horloge que le parent
applique reste la seule barrière contre une allocation qui s'emballe. C'est écrit
plutôt que masqué : un garde-fou dont on croit à tort qu'il tient est pire que
pas de garde-fou du tout.
"""

import json
import resource
import sys
import traceback

programme, resultat, step, glb, cpu_s, fsize, nproc = sys.argv[1:8]

# Avant tout import : ce qui suit charge ~300 Mio de code natif, et une limite
# posée après ne protégerait plus de rien.
for nom, valeur in (
    ("RLIMIT_CPU", int(cpu_s)),
    ("RLIMIT_FSIZE", int(fsize)),
    ("RLIMIT_NPROC", int(nproc)),
):
    try:
        resource.setrlimit(getattr(resource, nom), (valeur, valeur))
    except (ValueError, OSError, AttributeError):
        # Une limite refusée par la plateforme est signalée au parent plus bas,
        # jamais avalée : c'est le genre de silence qui fait croire à une
        # protection qui n'existe pas.
        pass

rapport = {"execution": "erreur", "erreur": None, "posees": [], "refusees": []}
for nom in ("RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NPROC", "RLIMIT_AS"):
    borne = getattr(resource, nom, None)
    if borne is None:
        rapport["refusees"].append(nom)
        continue
    douce = resource.getrlimit(borne)[0]
    (rapport["posees"] if douce != resource.RLIM_INFINITY else rapport["refusees"]).append(nom)

try:
    import cadquery as cq
    import trimesh

    source = open(programme, encoding="utf-8").read()
    # Un espace de noms neuf, et non `globals()` comme le démonstrateur amont :
    # le programme ne doit pas voir les variables de ce script, ne serait-ce que
    # pour que `r` soit bien celui qu'il a écrit.
    espace = {}
    exec(compile(source, "programme.py", "exec"), espace)

    # `r` est la convention d'entraînement : c'est le nom que ces poids donnent
    # à leur résultat. Le repli cherche le dernier Workplane défini, ce qui
    # rattrape un programme correct qui aurait nommé sa pièce autrement.
    forme = espace.get("r")
    if forme is None:
        candidats = [v for v in espace.values() if isinstance(v, cq.Workplane)]
        forme = candidats[-1] if candidats else None
    if forme is None:
        raise RuntimeError(
            "le programme ne définit aucun objet cadquery.Workplane — ni `r`, "
            "qui est la convention de ce modèle, ni rien d'autre"
        )

    solide = forme.val()
    cq.exporters.export(solide, step)

    sommets, faces = solide.tessellate(0.001, 0.1)
    maillage = trimesh.Trimesh([(v.x, v.y, v.z) for v in sommets], faces)
    maillage.export(glb)

    rapport.update(
        execution="ok",
        volume=float(solide.Volume()),
        surface=float(solide.Area()),
        etanche=bool(maillage.is_watertight),
        sommets=int(len(maillage.vertices)),
        faces=int(len(maillage.faces)),
    )
except BaseException as exc:  # noqa: BLE001 — tout échec devient une valeur lisible
    rapport["execution"] = "erreur"
    rapport["erreur"] = f"{type(exc).__name__}: {exc}"
    rapport["trace"] = traceback.format_exc(limit=12)

open(resultat, "w", encoding="utf-8").write(json.dumps(rapport, ensure_ascii=False, indent=2))
'''


# --- l'adaptateur -------------------------------------------------------------


class CadRecodeWorker(Worker):
    """Nuage de points vers programme CadQuery, par CAD-Recode v1.5."""

    name = "cad-recode"

    def __init__(self) -> None:
        self._model: Any = None
        self._tokenizer: Any = None
        self._torch: Any = None
        self._trimesh: Any = None
        self._np: Any = None
        self._device = "mps"
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._peak_driver = 0
        self._peak_enfants = 0
        self._prechauffage_ms: int | None = None

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        chemin = weights_dir(variant)
        tok = tokenizer_dir(variant)
        vendor = vendor_dir()

        try:
            import numpy as np
            import torch
            import transformers
            import trimesh
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise WorkerError(
                f"runtime cad-recode indisponible dans cet environnement ({exc}) — `{REPAIR}`"
            ) from exc

        # Avant d'ouvrir trois gigaoctets : au-delà de 4.x la sortie serait
        # fausse sans qu'aucune exception ne le dise.
        verifier_version_transformers(str(transformers.__version__))

        # Le code amont n'est visible qu'ici, et par insertion de chemin plutôt
        # que par installation : `vendor/` n'est pas un paquet, et l'installer
        # ferait entrer du CC BY-NC dans le venv d'un dépôt qui n'en veut pas.
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        try:
            from cad_recode_model import CADRecode
        except ImportError as exc:
            raise WorkerError(
                f"code d'inférence illisible depuis {vendor} ({exc}) — le refaire :\n\n"
                f"    {VENDOR_COMMANDES}"
            ) from exc

        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})
        self._torch, self._trimesh, self._np = torch, trimesh, np
        self._device = self._choisir_device(torch)

        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                str(tok),
                # Les positions du nuage sont remplies par ce jeton, et le masque
                # les marque à -1. `padding_side` est repris de l'amont : le
                # remplissage précède le programme, jamais l'inverse.
                pad_token=REMPLISSAGE,
                padding_side="left",
                local_files_only=True,
            )
        except Exception as exc:  # noqa: BLE001 — remonte avec ce qui répare
            raise WorkerError(
                f"tokenizer illisible depuis {tok} : {type(exc).__name__}: {exc}"
            ) from exc

        try:
            self._model = self._charger(CADRecode, chemin, torch)
        except WorkerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WorkerError(
                f"chargement de CAD-Recode impossible depuis {chemin} : "
                f"{type(exc).__name__}: {exc}. `architectures` du config.json désigne une "
                "classe qui n'existe nulle part et le dépôt ne contient aucun .py : c'est "
                "la classe vendorée qui est instanciée, `trust_remote_code` n'y peut rien"
            ) from exc

        self._prechauffage_ms = self._prechauffer_cadquery()
        self.peak_memory_bytes()

        return {
            "device": self._device,
            "n_points_entrainement": N_POINTS_ENTRAINEMENT,
            "vendor": str(vendor),
            "tokenizer": str(tok),
            "cadquery_prechauffage_ms": self._prechauffage_ms,
            "versions": self._versions(),
        }

    def _charger(self, CADRecode: Any, chemin: Path, torch: Any) -> Any:
        """Les poids sur le périphérique, en gardant leur précision d'origine.

        `dtype` a remplacé `torch_dtype` en cours de route chez transformers, et
        les deux graphies cohabitent selon la version : on tente la neuve, on
        retombe sur l'ancienne. Sans `auto`, les poids bfloat16 seraient
        silencieusement promus en fp32 — deux fois la mémoire, pour rien.

        `attn_implementation` n'est pas passé, et c'est délibéré : mesuré, ne
        rien demander donne `sdpa`, exactement comme `None`. Le piège
        `flash_attention_2` annoncé par la veille n'existe que si on le réclame.
        """
        arguments = dict(local_files_only=True)
        try:
            modèle = CADRecode.from_pretrained(str(chemin), dtype="auto", **arguments)
        except TypeError:
            modèle = CADRecode.from_pretrained(str(chemin), torch_dtype="auto", **arguments)
        return modèle.eval().to(self._device)

    def _choisir_device(self, torch: Any) -> str:
        """MPS, ou ce que la variable d'environnement impose.

        Refusé plutôt que replié en silence : un profil mesuré sur MPS ne décrit
        pas un run CPU, et retomber sans le dire ferait inscrire au manifeste un
        pic qui n'a jamais eu lieu.
        """
        demandé = str(os.environ.get(VAR_DEVICE) or "mps").strip().lower()
        if demandé not in ("mps", "cpu"):
            raise WorkerError(f"{VAR_DEVICE} = {demandé!r} : attendu `mps` ou `cpu`")
        if demandé == "mps" and not torch.backends.mps.is_available():
            raise WorkerError(
                "backend MPS indisponible (torch.backends.mps.is_available() est faux) — "
                f"vérifier que runtimes/{ENV_NAME}/.venv utilise un Python arm64, ou "
                f"forcer le processeur par {VAR_DEVICE}=cpu"
            )
        return demandé

    def _prechauffer_cadquery(self) -> int | None:
        """Paie l'import de cadquery ici, dans un processus jetable.

        Mesuré sur cette machine : **85,6 s** au premier import après
        `env sync`, puis 1,1 s — et 2,97 s de CPU seulement, ce qui dit que
        l'attente est la validation par macOS de ~300 Mio de code natif non
        signé, pas de la compilation. Le cache est **système** : une fois payé
        par n'importe quel processus, il l'est pour les suivants.

        D'où ce préchauffage, et d'où le fait qu'il tourne à part : payer
        l'attente au premier job l'attribuerait à la latence du modèle, et le
        banc l'inscrirait au profil d'un cas qui ne la mérite pas. La payer dans
        le worker lui-même ajouterait ~300 Mio au RSS d'un processus qui n'a
        jamais besoin de cadquery — et fausserait le pic mémoire du profil.
        """
        import time

        début = time.monotonic()
        try:
            issue = subprocess.run(
                [sys.executable, "-I", "-c", "import cadquery"],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return int((time.monotonic() - début) * 1000) if issue.returncode == 0 else None

    # --- exécution -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._model is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")

        plan = plan_cao(
            entree=request.input, params=request.params, defaults=self._defaults
        )
        avertissements = list(plan.warnings)

        progress(5, "lecture de la géométrie")
        source = resolve_geometrie(request.get("geometrie"), request.output_dir)
        points, lu = self._nuage(source, plan)
        avertissements += lu

        progress(25, "génération du programme")
        decode, jetons = self._generer(points, plan)
        programme, extraits = extraire_programme(decode)
        avertissements += extraits

        chemin_programme = request.output_dir / PROGRAMME_PY
        chemin_programme.write_text(programme, encoding="utf-8")

        sortie: dict[str, Any] = {"code": PROGRAMME_PY, "execution": "non-demandee"}
        exécution: dict[str, Any] = {}

        if plan.executer:
            progress(70, "exécution du programme")
            exécution = self._executer(chemin_programme, request.output_dir)
            sortie["execution"] = exécution["execution"]
            if exécution["execution"] == "ok":
                sortie["step"] = PIECE_STEP
                sortie["mesh"] = PIECE_GLB
            if exécution.get("erreur"):
                sortie["erreur"] = str(exécution["erreur"])
            avertissements += exécution.get("warnings", [])
        else:
            avertissements.append(
                "programme non exécuté : `executer_le_code` est faux, ce qui est le "
                "défaut du contrat. Ce code n'a été relu par personne, et l'exécuter est "
                "une décision qui se prend job par job"
            )

        progress(90, "écriture des sorties")
        métriques: dict[str, Any] = {
            "device": self._device,
            "n_points": len(points),
            "n_points_entrainement": N_POINTS_ENTRAINEMENT,
            "tokens_generes": jetons,
            "max_new_tokens": plan.max_new_tokens,
            "plafond_atteint": jetons >= plan.max_new_tokens,
            "lignes_de_code": len(programme.splitlines()),
            "execution": sortie["execution"],
            **{f"execution_{k}": v for k, v in exécution.items() if k in _METRIQUES_EXEC},
            "peak_memory_bytes": self.peak_memory_bytes(),
        }
        if avertissements:
            métriques["warnings"] = avertissements
        return InferResult(output=sortie, metrics=métriques)

    # --- lecture de la géométrie ---------------------------------------------

    def _nuage(self, chemin: Path, plan: Demande) -> tuple[Any, list[str]]:
        """Un fichier vers les `n_points` que le modèle verra, et ce qui a dû être décidé.

        **Trois types de retour pour `trimesh.load`, pas un**, et le chemin qui
        compte le plus est le deuxième : `image-to-mesh` rend du
        `model/gltf-binary`, donc le chaînage naturel du parc arrive ici sous
        forme de `Scene`, où `sample_surface` lèverait
        `AttributeError: 'Scene' object has no attribute 'area_faces'`. Un `.ply`
        de points purs arrive en `PointCloud` sans faces, et il ne faut surtout
        pas l'échantillonner — ses points **sont** la donnée.

        Le cadrage vient ensuite, et il est le même dans les trois cas : centrage
        sur les bornes, puis mise à l'échelle dans un cube de côté 2. C'est ce que
        ces poids ont appris, et c'est ce qui efface l'échelle de la pièce.
        """
        np, trimesh = self._np, self._trimesh
        avertissements: list[str] = []

        try:
            objet = trimesh.load(str(chemin))
        except Exception as exc:  # noqa: BLE001 — remonte traduit
            raise WorkerError(
                f"géométrie illisible ({chemin.name}) : {type(exc).__name__}: {exc}"
            ) from exc

        if isinstance(objet, trimesh.Scene):
            maillage = self._aplatir(objet, chemin)
            avertissements.append(
                f"{chemin.name} contient une scène de {len(objet.geometry)} géométrie(s), "
                "concaténées en un seul maillage avant échantillonnage — c'est la forme "
                "que rend `image-to-mesh`, et trimesh ne la réduit pas tout seul"
            )
            sommets = self._echantillonner_surface(maillage, plan)
        elif isinstance(objet, trimesh.Trimesh):
            sommets = self._echantillonner_surface(objet, plan)
        elif hasattr(objet, "vertices"):
            # Un nuage de points : ses points sont la donnée, il n'y a rien à
            # tirer au sort et la graine n'a donc aucun effet ici.
            sommets = np.asarray(objet.vertices, dtype="float64")
            avertissements.append(
                f"{chemin.name} est un nuage de {len(sommets)} points, pris tels quels : "
                "aucune surface n'est échantillonnée, et `seed` n'a pas d'effet"
            )
        else:
            raise WorkerError(
                f"{chemin.name} : trimesh rend un {type(objet).__name__}, dont cette "
                "capacité ne sait rien faire — attendu un maillage, une scène ou un nuage"
            )

        if len(sommets) == 0:
            raise WorkerError(f"{chemin.name} ne contient aucun point")

        sommets = cadrer(sommets, np)
        if len(sommets) <= plan.n_points:
            avertissements.append(
                f"{len(sommets)} point(s) disponibles pour {plan.n_points} demandés : "
                "tous sont soumis, sans échantillonnage du plus lointain"
            )
            return sommets.astype("float32"), avertissements

        indices = plus_lointains(sommets, plan.n_points, np)
        return sommets[indices].astype("float32"), avertissements

    def _aplatir(self, scene: Any, chemin: Path) -> Any:
        """Une `Scene` vers un `Trimesh` unique, transformations appliquées."""
        trimesh = self._trimesh
        for tentative in (
            lambda: scene.to_mesh(),
            lambda: trimesh.util.concatenate(scene.dump()),
        ):
            try:
                maillage = tentative()
            except (AttributeError, TypeError, ValueError):
                continue
            if maillage is not None and getattr(maillage, "faces", None) is not None:
                return maillage
        raise WorkerError(
            f"{chemin.name} : scène non réductible à un maillage unique — la version de "
            "trimesh installée ne fournit ni `Scene.to_mesh()` ni `dump()` exploitable"
        )

    def _echantillonner_surface(self, maillage: Any, plan: Demande) -> Any:
        """`N_PRE_POINTS` points tirés de la surface, à graine fixée.

        **`np.random.seed()` est inerte avec trimesh 5.0.0** — mesuré, trois
        appels précédés du même `np.random.seed(0)` donnent trois empreintes
        différentes. C'est pourtant la recette de reproductibilité du
        démonstrateur et du Space amont. Seul `seed=` en argument nommé fixe le
        tirage, et sans lui cette capacité serait irreproductible sans qu'aucun
        message ne le signale — y compris pour la charge type figée du banc.
        """
        np, trimesh = self._np, self._trimesh
        if getattr(maillage, "faces", None) is None or len(maillage.faces) == 0:
            raise WorkerError(
                "le maillage n'a aucune face : rien à échantillonner. Un fichier de "
                "points purs doit arriver en nuage, pas en maillage vide"
            )
        try:
            sommets, _ = trimesh.sample.sample_surface(
                maillage, N_PRE_POINTS, seed=plan.seed
            )
        except TypeError as exc:
            raise WorkerError(
                f"cette version de trimesh n'accepte pas `seed=` sur `sample_surface` "
                f"({exc}) : sans lui le tirage n'est pas reproductible, et "
                "`np.random.seed()` n'y peut rien depuis trimesh 5.0.0"
            ) from exc
        return np.asarray(sommets, dtype="float64")

    # --- génération ----------------------------------------------------------

    def _generer(self, points: Any, plan: Demande) -> tuple[str, int]:
        """Le nuage et un jeton d'ouverture entrent, un programme sort.

        Les 256 premières positions du contexte portent le remplissage côté
        `input_ids` et **-1** côté masque : c'est cette valeur qui dit au modèle
        d'y substituer les points encodés. Elle n'a rien d'un détail
        d'implémentation — c'est elle que transformers ≥5 fait passer dans un
        `cumsum`, et c'est de là que vient tout le charabia.

        La génération est **gloutonne**, imposée ici et non laissée au
        `generation_config.json` : `seed` du contrat ne pilote que
        l'échantillonnage de surface, et un décodage qui tirerait au sort rendrait
        deux programmes différents pour le même fichier sans que rien ne le dise.
        """
        torch = self._torch
        tok = self._tokenizer

        début = tok(DEBUT)["input_ids"][0]
        ids = [tok.pad_token_id] * len(points) + [début]
        masque = [-1] * len(points) + [1]

        with torch.no_grad():
            lot = self._model.generate(
                input_ids=torch.tensor(ids).unsqueeze(0).to(self._device),
                attention_mask=torch.tensor(masque).unsqueeze(0).to(self._device),
                point_cloud=torch.tensor(points).unsqueeze(0).to(self._device),
                max_new_tokens=plan.max_new_tokens,
                pad_token_id=tok.pad_token_id,
                do_sample=False,
            )
        if self._device == "mps":
            torch.mps.synchronize()
        self.peak_memory_bytes()

        produits = int(lot.shape[1] - len(ids))
        return tok.batch_decode(lot)[0], produits

    # --- exécution du programme ----------------------------------------------

    def _executer(self, programme: Path, job_dir: Path) -> dict[str, Any]:
        """Le programme dans un interpréteur séparé, et ce qu'il en est revenu.

        Un échec est une **valeur**, jamais une exception : le programme reste
        utile même quand il ne compile pas, et c'est tout ce que cette capacité
        promet de rendre.

        Ce que ce garde-fou fait : un interpréteur isolé (`-I`), un environnement
        vidé, un dossier de travail limité à celui du job, un délai d'horloge, une
        borne de temps processeur et une borne de taille de fichier.

        Ce qu'il ne fait pas, et qui est remonté en avertissement du job plutôt
        que passé sous silence : il ne coupe pas le réseau — le parc n'a pas de
        bac à sable système — et il ne borne pas la mémoire, `RLIMIT_AS`,
        `RLIMIT_DATA` et `RLIMIT_RSS` étant refusées par macOS quelle que soit la
        valeur demandée. Le `multiprocessing.Process` forké du démonstrateur amont
        en fait moins encore : il hérite du système de fichiers, du réseau **et**
        de l'espace mémoire du parent.
        """
        exécuteur = job_dir / EXECUTEUR_PY
        exécuteur.write_text(EXECUTEUR, encoding="utf-8")
        rapport_json = job_dir / EXECUTION_JSON

        commande = [
            sys.executable,
            "-I",
            str(exécuteur),
            str(programme),
            str(rapport_json),
            str(job_dir / PIECE_STEP),
            str(job_dir / PIECE_GLB),
            str(CPU_S),
            str(FICHIER_MAX),
            str(PROCESSUS_MAX),
        ]
        avertissements = [
            "programme exécuté : interpréteur séparé, environnement vidé, dossier de "
            f"travail limité au job, délai {DELAI_S:.0f} s, RLIMIT_CPU et RLIMIT_FSIZE. "
            "Le réseau n'est PAS coupé et la mémoire n'est PAS bornée — macOS refuse "
            "RLIMIT_AS, RLIMIT_DATA et RLIMIT_RSS quelle que soit la valeur demandée"
        ]
        avant = _entrees(job_dir)

        try:
            # Un foyer jetable, et non le dossier du job. Le premier essai posait
            # `HOME` sur le job : `ezdxf`, tiré par cadquery, y déposait 50 Ko de
            # cache de polices, et le dossier du job cessait de ne contenir que ce
            # que le job a produit — or c'est lui que la Bibliothèque archive.
            # Vérifié après coup : le nom `CACHE_DIRECTORY = ".cache"` d'ezdxf
            # laisse croire à un chemin relatif au dossier de travail, mais il est
            # résolu depuis `HOME`. C'est bien cette ligne-ci qui règle la chose,
            # et non le `cwd` en dessous.
            with tempfile.TemporaryDirectory(prefix="cad-recode-") as foyer:
                issue = subprocess.run(
                    commande,
                    cwd=str(job_dir),
                    # Vidé plutôt qu'hérité : ni jeton d'API, ni cache, ni chemin
                    # de bibliothèque du parent ne traversent. `-I` fait le reste.
                    env={"PATH": "/usr/bin:/bin", "HOME": foyer},
                    capture_output=True,
                    text=True,
                    timeout=DELAI_S,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return {
                "execution": "delai-depasse",
                "erreur": f"le programme n'a pas rendu la main en {DELAI_S:.0f} s",
                "warnings": avertissements,
            }
        except OSError as exc:
            return {
                "execution": "erreur",
                "erreur": f"exécuteur non lançable : {exc}",
                "warnings": avertissements,
            }
        finally:
            self.peak_memory_bytes()
            avertissements += _traces(avant, _entrees(job_dir))

        if rapport_json.is_file():
            try:
                rapport = json.loads(rapport_json.read_text(encoding="utf-8"))
            except ValueError:
                rapport = {}
            if rapport.get("execution") in EXECUTIONS:
                refusées = rapport.get("refusees") or []
                if refusées:
                    avertissements.append(
                        "bornes refusées par la plateforme : " + ", ".join(refusées)
                    )
                return {**rapport, "warnings": avertissements}

        # Pas de rapport : l'interpréteur est mort avant de pouvoir en écrire un.
        # Un code de retour négatif est un signal, et c'est presque toujours
        # SIGXCPU — la borne processeur, seule barrière qui morde vraiment ici.
        # Le nommer évite de faire chercher un plantage de code natif là où il n'y
        # a qu'un calcul qui a dépassé son budget.
        détail = (issue.stderr or "").strip().splitlines()[-1:] or ["aucune sortie d'erreur"]
        cause = _nom_du_signal(issue.returncode)
        return {
            "execution": "erreur",
            "erreur": f"l'exécuteur est mort ({cause}) : {détail[0]}",
            "warnings": avertissements,
        }

    # --- mémoire et versions -------------------------------------------------

    def peak_memory_bytes(self) -> int | None:
        """Le plus grand de trois relevés, et les trois comptent.

        Le RSS de ce processus ne voit **pas** la mémoire Metal ; le pilote Metal
        ne voit pas le RSS ; et sur mémoire unifiée les deux occupent le même
        budget. `driver_allocated_memory` redescend en outre aussi vite qu'il
        monte, d'où le maximum tenu à chaque relevé plutôt que lu une fois.

        Le troisième relevé est propre à cette capacité : le programme engendré
        s'exécute dans un **sous-processus**, dont la mémoire est invisible au
        RSS du worker et bien réelle pour la machine. `RUSAGE_CHILDREN` en rend le
        maximum, pas la somme — c'est exactement ce qu'il faut ici.
        """
        rss = peak_rss_bytes() or 0
        enfants = self._pic_enfants()

        if self._device == "mps" and self._torch is not None:
            try:
                self._peak_driver = max(
                    self._peak_driver, int(self._torch.mps.driver_allocated_memory())
                )
            except (AttributeError, RuntimeError):
                pass
        return max(rss, self._peak_driver, enfants) or None

    def _pic_enfants(self) -> int:
        """Pic RSS du plus gros sous-processus terminé — cadquery en fait partie."""
        try:
            import resource

            maxrss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        except (ImportError, OSError, ValueError):
            return self._peak_enfants
        # `ru_maxrss` est en octets sur macOS et en kibioctets sur Linux — le
        # même piège que dans `peak_rss_bytes`, et un facteur 1024 sur un profil
        # mémoire fausse directement le contrôle d'admission.
        octets = maxrss if sys.platform == "darwin" else maxrss * 1024
        self._peak_enfants = max(self._peak_enfants, int(octets))
        return self._peak_enfants

    def unload(self) -> None:
        """Rend la mémoire au budget, pas seulement à Python."""
        import gc

        self._model = None
        self._tokenizer = None
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
            ("transformers", "transformers"),
            ("trimesh", "trimesh"),
            ("numpy", "numpy"),
        ):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


#: Les clés du rapport d'exécution qui ont leur place dans les métriques du job.
#: `etanche` est la plus utile des quatre : un solide non étanche est un solide
#: que le programme décrit mal, et c'est invisible à la lecture du code.
_METRIQUES_EXEC = ("volume", "surface", "etanche", "sommets", "faces")


# --- géométrie, à numpy près --------------------------------------------------
#
# Ces deux fonctions reçoivent numpy en argument plutôt que de l'importer : c'est
# ce qui permet de les décrire ici, à côté de ce qu'elles servent, sans rien
# importer au niveau du module.


def cadrer(points: Any, np: Any) -> Any:
    """Centrage sur les bornes, puis mise à l'échelle dans un cube de côté 2.

    Sur les **bornes** et non sur le centre de masse : c'est le cadrage de
    l'amont, et il ne dépend pas de la densité des points — deux échantillonnages
    du même solide donnent le même cadrage, ce qui est la moitié de la
    reproductibilité de cette capacité.

    Une géométrie plate — un plan, un segment — a une étendue nulle sur un axe et
    ferait une division par zéro. Le facteur est alors laissé à 1 : mieux vaut un
    nuage hors cadrage, que le modèle traitera mal mais visiblement, qu'une
    coordonnée infinie qui traverserait tout jusqu'à la génération.
    """
    minima = points.min(axis=0)
    maxima = points.max(axis=0)
    centré = points - (minima + maxima) / 2.0
    étendue = float(np.max(maxima - minima))
    return centré if étendue <= 0 else centré * (2.0 / étendue)


def plus_lointains(points: Any, k: int, np: Any) -> Any:
    """Échantillonnage du plus lointain : `k` points aussi écartés que possible.

    Réécrit en numpy parce que `pytorch3d.ops.sample_farthest_points`, dont
    l'amont se sert, ne publie **aucune roue arm64** — PyPI s'arrête à 0.7.4, en
    `macosx_10_9_x86_64` et jusqu'à cp310. Le départ est fixé à l'indice 0, comme
    l'amont, dont le défaut `random_start_point=False` a été relu pour en être
    sûr : un départ tiré au sort rendrait la charge type du banc irreproductible.

    Deux cent cinquante-six tours sur huit mille points ; le coût est celui d'une
    lecture de fichier, et il ne vaut pas d'être optimisé.
    """
    n = len(points)
    if k >= n:
        return np.arange(n)
    choisis = np.empty(k, dtype=np.int64)
    choisis[0] = 0
    distances = np.sum((points - points[0]) ** 2, axis=1)
    for rang in range(1, k):
        suivant = int(np.argmax(distances))
        choisis[rang] = suivant
        distances = np.minimum(distances, np.sum((points - points[suivant]) ** 2, axis=1))
    return choisis


if __name__ == "__main__":
    raise SystemExit(main(CadRecodeWorker))
