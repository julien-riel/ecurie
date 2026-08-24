"""Charge type d'`audio-align` : un paragraphe lu, dont le texte est vrai par construction.

    uv run python tools/bench_assets.py parole

**Pourquoi un asset de plus alors que `parole-tts.wav` existe.** Parce que ce
fichier-là est le cas dégénéré exact de l'alignement forcé, et que ce n'est pas
une supposition : sa transcription répète une phrase mot pour mot, et l'aligneur
placé devant elle met ses huit premiers mots à 10,56 s d'un fichier où la parole
commence à 1,28 s, puis tasse les onze derniers sur 19,84 s. Il reste parfait
pour les quatre autres capacités qui s'en servent ; il ne mesure rien
d'interprétable ici. Le dépôt a par ailleurs déjà consigné qu'un modèle d'écoute
le décrit comme « une chanson pop rock avec une voix féminine », là où la carte
de l'aligneur annonce « Audio Types : Speech » et rien d'autre.

**Pourquoi ce texte-ci est fiable.** Il n'est passé par aucune transcription : il
est **donné au TTS du parc**, et l'enregistrement en découle. Pour cette capacité
le texte est une entrée du contrat, donc il serait versionné de toute façon — et
c'est la seule façon connue d'obtenir une vérité terrain sans annotation
manuelle. Vérifié après coup contre l'enveloppe d'énergie du signal : les
soixante-seize mots sont monotones et chaque groupe de parole tombe dans son
groupe d'énergie.

**LA SYNTHÈSE EST DÉTERMINISTE, ET C'EST UNE CORRECTION AU DOSSIER
D'INSTRUCTION.** Celui-ci affirmait que `generate_custom_voice` ne l'est pas —
deux appels rendant 178 560 puis 207 360 échantillons — et concluait qu'il
faudrait committer un binaire orphelin de plus, ce que le README du banc
interdit. La mesure dit autre chose : passée par `ecurie run --seed`, qui sème le
générateur MLX avant la génération, la même commande rend deux fois le même
sha256. Vérifié sur deux textes différents, deux exécutions chacun.

Cette reproductibilité est **conditionnelle**, et il faut le dire : elle tient à
la révision épinglée du TTS, à la version de MLX, et à la machine. La recette
n'est donc pas une garantie bit à bit pour l'éternité — c'est une explication
exécutable de ce que le fichier contient, ce qui est précisément ce qui manque
aux six images orphelines du dossier.

**Aucune donnée à licencier, personne à qui demander.** La voix est synthétique,
le texte est écrit ici. Un enregistrement de quelqu'un serait versionné, public
et figé pour des années — exactement ce qu'on ne fait pas de la parole d'une
personne réelle. Même raisonnement que les visages calculés de
`tools/golden_assets.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path

#: Rien à installer : la recette pilote la CLI d'Écurie, qui choisit elle-même
#: l'environnement du TTS. Refaire ici l'appel de `mlx_audio.tts` donnerait un
#: second chemin de génération à maintenir, et un asset qui ne serait plus la
#: sortie d'un job du parc.
ENV = None

CIBLE = "parole-fr-32s.wav"
CIBLES = (CIBLE,)

#: Le variant titulaire de `text-to-speech`, à sa révision épinglée.
MODELE = "qwen3-tts-1.7b@8bit-mlx"
GRAINE = 20260824

#: Six phrases, sans aucune répétition littérale — c'est la contrainte qui a fait
#: rejeter l'asset précédent. Longueurs croissantes, et des frontières nettes
#: entre elles : les trois cas de la charge écoutent 3,6 s, 17,5 s et 32,0 s, ce
#: qui tombe dans les silences mesurés à 3,36→3,92, 17,36→17,60 et après 31,84.
PHRASES = (
    "Le quinze mars, la neige tombait encore sur la ville.",
    "Amélie a relu son rapport pendant toute la matinée, sans y trouver la moindre erreur.",
    "Le lendemain, elle l'a présenté devant le conseil de Trois-Rivières, "
    "qui l'a adopté sans un mot.",
    "On lui a demandé combien de temps la mesure tiendrait ; elle a répondu qu'elle l'ignorait.",
    "Personne n'a insisté, et la séance a été levée avant midi.",
    "Le procès-verbal, lui, n'a jamais été relu par quiconque.",
)

TEXTE = " ".join(PHRASES)

#: L'empreinte du fichier committé, relevée sur la machine de référence
#: (Mac17,4 / macOS 26.5.2 / mlx 0.32.1 / mlx-audio 0.5.0). La recette la compare
#: après coup plutôt que de l'imposer : une divergence n'est pas une erreur de la
#: recette, c'est le signe qu'une des trois conditions ci-dessus a bougé, et
#: c'est utile à savoir.
EMPREINTE = "0f3a252a8d57e4125dc622a55e29af3a2b7d4d04f1cbe7beb863d3e01089383c"

RACINE = Path(__file__).resolve().parents[2]


def _dossier_du_job(job_id: str) -> Path:
    """Où `ecurie run` a écrit sa sortie. Voir `ecurie_core.config`.

    Recalculé plutôt qu'importé : cette recette tourne dans l'env racine, mais
    elle n'a aucune raison d'épouser les objets internes d'Écurie pour lire un
    chemin que la conception fixe en une ligne.
    """
    home = Path(os.environ.get("ECURIE_HOME", str(Path.home() / ".ecurie"))).expanduser()
    return home / "outputs" / job_id


def produire(dossier: Path, *, force: bool = False) -> list[Path]:
    cible = dossier / CIBLE
    if cible.exists() and not force:
        print(f"  {CIBLE} : déjà là, laissé tel quel")
        return []

    commande = [
        "uv",
        "run",
        "ecurie",
        "run",
        MODELE,
        "--seed",
        str(GRAINE),
        "--json",
        "-p",
        f"text={TEXTE}",
    ]
    sortie = subprocess.run(commande, cwd=RACINE, capture_output=True, text=True, check=False)
    if sortie.returncode != 0:
        raise SystemExit(
            f"la synthèse a échoué ({sortie.returncode}). Les poids de {MODELE} sont-ils "
            f"téléchargés (`ecurie pull {MODELE}`) et son profil mesuré ?\n{sortie.stderr}"
        )
    manifeste = json.loads(sortie.stdout)
    produit = _dossier_du_job(manifeste["job_id"]) / manifeste["output"]["audio"]
    shutil.copyfile(produit, cible)

    empreinte = sha256(cible.read_bytes()).hexdigest()
    secondes = manifeste["metrics"].get("audio_seconds")
    print(f"  {CIBLE} : {secondes} s, {cible.stat().st_size} octets, sha256 {empreinte[:16]}…")
    if empreinte != EMPREINTE:
        print(
            "  ATTENTION : empreinte différente de celle du fichier committé. La révision "
            "du TTS, la version de MLX ou la machine ont changé — le fichier reste valable "
            "comme parole, mais les bornes de la charge type (3,6 / 17,5 / 32,0 s) ont été "
            "choisies sur les silences de l'autre enregistrement et sont à revérifier."
        )
    return [cible]
