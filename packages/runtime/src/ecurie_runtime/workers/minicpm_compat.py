"""Rendre chargeables les conversions MLX de MiniCPM-o. Correctif d'emprunt.

**Ce module n'a pas vocation à durer.** Il corrige, depuis Écurie, un défaut qui
appartient à `mlx-vlm` — et la première chose à faire en le lisant est de
vérifier s'il sert encore.

Le défaut, mesuré. `mlx_vlm.models.minicpmo.Model.sanitize` traduit les noms de
poids du dépôt d'origine d'OpenBMB — `llm.`, `vpm.`, `apm.` — vers ceux du
modèle MLX, et **jette tout ce qui ne commence pas par l'un d'eux** (le `else:
continue` de sa boucle). Or les conversions publiées portent déjà les noms
d'arrivée. Résultat : `Missing 1203 parameters` sur des poids qui sont tous dans
les fichiers — vérifié tenseur par tenseur, les 367 de la tour audio compris.

Trois conversions ont été examinées, aucune n'y échappe, et elles ne s'accordent
même pas entre elles sur le nom de la tour audio :

    mlx-community/MiniCPM-o-4_5-4bit     audio_tower.     langue+vision+audio
    mlx-community/MiniCPM-o-4_5-5bit     audio_tower.     idem
    andrevp/MiniCPM-o-4_5-MLX-4bit       audio_encoder.   idem, plus la tête TTS

Et il n'y a rien à attendre d'une mise à jour : 0.6.15 est la dernière version
publiée sur PyPI au 2026-08-23.

Le correctif fait l'aller-retour : il rend aux clés leurs noms d'origine, puis
laisse le `sanitize` d'amont faire son travail habituel — transposition des
convolutions audio, découpe de l'attention du resampler, tout ce qu'il sait
faire et qu'on ne veut pas recopier. C'est ce qui le rend sûr : on ne remplace
pas sa logique, on lui donne l'entrée qu'il attend.

**Il s'efface tout seul.** Si une version d'amont accepte un jour les deux
formes, la traduction ci-dessous deviendra une identité sans rien casser — les
clés ne commenceront plus par les préfixes traduits. Le jour où l'on veut le
retirer, il suffit de vérifier qu'un `load()` nu passe.
"""

from typing import Any

# Nom d'arrivée → nom d'origine. L'ordre compte : `audio_encoder.` doit être
# essayé avant `audio_` de quoi que ce soit d'autre, et les préfixes les plus
# longs d'abord — un `startswith` naïf sur des préfixes qui se chevauchent
# traduirait la mauvaise moitié de la clé.
RETOUR_AUX_NOMS_D_ORIGINE: tuple[tuple[str, str], ...] = (
    ("language_model.", "llm."),
    ("vision_tower.", "vpm."),
    ("audio_tower.", "apm."),
    ("audio_encoder.", "apm."),
)

# Ce que le `sanitize` d'amont laisse passer tel quel : inutile d'y toucher.
DEJA_ACCEPTES = ("audio_projection_layer.", "resampler.", "tts.", "audio_avg_pooler.")

_POSE = False


def renommer(weights: dict[str, Any]) -> dict[str, Any]:
    """Les poids d'une conversion MLX, sous les noms que le sanitize d'amont attend.

    Fonction pure, éprouvée sans mlx : c'est tout ce que le correctif ajoute, et
    il n'y a aucune raison de devoir charger six gigaoctets pour la vérifier.
    """
    traduits: dict[str, Any] = {}
    for clé, valeur in weights.items():
        if clé.startswith(DEJA_ACCEPTES):
            traduits[clé] = valeur
            continue
        for arrivée, origine in RETOUR_AUX_NOMS_D_ORIGINE:
            if clé.startswith(arrivée):
                traduits[origine + clé[len(arrivée) :]] = valeur
                break
        else:
            # Déjà sous un nom d'origine, ou inconnu des deux côtés : on le passe
            # tel quel et l'on laisse le sanitize d'amont trancher. Décider ici
            # de ce qu'il doit jeter reviendrait à recopier sa politique.
            traduits[clé] = valeur
    return traduits


def poser() -> bool:
    """Installe le correctif sur le module `minicpmo` de mlx-vlm. Idempotent.

    Rend faux quand il n'y a rien à corriger — mlx-vlm absent, ou module
    `minicpmo` disparu d'une version future. Dans les deux cas ce n'est pas une
    erreur : un correctif d'emprunt qui ne trouve plus son emprunteur a fait son
    temps, et le chargement échouera de lui-même avec sa propre phrase s'il doit
    échouer.
    """
    global _POSE
    if _POSE:
        return True
    try:
        from mlx_vlm.models.minicpmo import minicpmo as amont
    except ImportError:
        return False

    original = getattr(amont.Model, "sanitize", None)
    if original is None or getattr(original, "_ecurie_compat", False):
        return False

    def sanitize(self: Any, weights: dict[str, Any]) -> dict[str, Any]:
        return original(self, renommer(weights))

    sanitize._ecurie_compat = True  # noqa: SLF001 — marque d'idempotence, sur notre propre fonction
    amont.Model.sanitize = sanitize
    _POSE = True
    return True
