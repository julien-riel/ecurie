"""Adaptateur mlx-audio, chemin **ce qui est dit**.

Sixième emploi du runtime `mlx-audio`, et le second sur les octets de MOSS. Le
manifeste de la diarisation annonçait celui-ci depuis le début : « Le modèle
transcrit aussi ; ce texte n'est pas remonté, il appartient à `speech-to-text` ».
Deux adaptateurs plutôt qu'une sortie de plus, parce qu'un contrat qui rendrait à
la fois le texte et les tours de parole ne pourrait être rempli par aucun modèle
de transcription ordinaire — et la comparaison A/B d'une capacité suppose que
plusieurs modèles puissent y entrer.

**Trois paramètres du contrat ne s'appliquent pas à ce modèle, et il vaut mieux
le dire que le laisser deviner.** `beam_size` et `temperature` supposent un
décodage à faisceau réglable ; `word_timestamps` suppose un alignement au mot.
Ce réseau produit ses segments en même temps que son texte, sans exposer ni l'un
ni l'autre. Ils restent au contrat parce que le contrat sert la famille Whisper
autant que celui-ci, et l'adaptateur inscrit au manifeste du job lesquels ont été
demandés sans effet — les ignorer sans trace ferait croire qu'ils ont agi.

**`task: "translate"` est refusé, et ce refus est différent des trois autres.**
Demander une traduction et recevoir une transcription n'est pas un réglage
inopérant : c'est une autre sortie que celle qu'on a demandée, et le job aurait
l'air d'avoir réussi. Un refus explicite au début coûte une seconde ; une
transcription chinoise là où l'on attendait de l'anglais coûte la relecture.

**La borne de durée vit dans `options` et non dans le contrat.** `speech-to-text`
ne déclare pas de `max_seconds` — la transcription d'une conférence d'une heure
est un usage légitime —, mais l'adaptateur a besoin d'un garde-fou mémoire.
C'est exactement ce que le champ `options:` d'un variant prévoit : un réglage
propre au runtime, hors contrat, que l'UI n'affiche pas.
"""

import gc
import json
import re
import time
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

ENV_NAME = "mlx-audio"
REPAIR = f"ecurie env sync {ENV_NAME}"
TEXTE_NAME = "transcription.txt"
SEGMENTS_NAME = "segments.json"
SAMPLE_RATE = 16_000

# Ce que le contrat déclare et que ce réseau n'honore pas. La valeur est celle
# qui vaut « on n'a rien demandé de particulier » : la signaler quand elle est au
# défaut ferait un avertissement à chaque job.
INOPERANTS = {
    "word_timestamps": (False, "le modèle horodate le segment, pas le mot"),
    "beam_size": (5, "le décodage ne passe pas par un faisceau réglable"),
    "temperature": (0.0, "le décodage de ce modèle est déterministe"),
}


def _import_runtime() -> tuple[Any, Any, Any]:
    try:
        import mlx.core as mx
        from mlx_audio.stt.utils import load_audio, load_model
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-audio indisponible dans cet environnement ({exc}) — "
            f"le reconstruire avec `{REPAIR}`"
        ) from exc
    return mx, load_model, load_audio


# Ce que le modèle préfixe à chaque segment : `[S01] `, son identifiant de
# locuteur. Trouvé au premier job réel, et pas avant — le banc d'essai vérifie
# qu'un fichier de sortie existe, pas ce qu'il contient.
#
# Ces marqueurs n'ont rien à faire ici, et c'est la frontière même entre les deux
# capacités que ces poids servent : `speech-to-text` rend ce qui est dit,
# `speaker-diarization` rend qui l'a dit. Le contrat de la première ne déclare
# aucun locuteur ; les laisser dans le texte livrerait une sortie qu'aucun autre
# modèle de transcription ne produirait, et qu'aucun consommateur n'attend.
# Sans ancre de début : un segment n'en porte qu'un, en tête, mais le texte
# global du modèle en intercale un par tour de parole.
_LOCUTEUR = re.compile(r"\[S\d+\]\s*")

# Le texte global du modèle intercale en plus les bornes de chaque segment :
# « [1.28][S01] … [9.21][10.48][S01] … ». C'est un flux de diarisation aplati,
# pas une transcription — d'où la recomposition depuis les segments.
_HORODATAGE = re.compile(r"\[\d+(?:\.\d+)?\]")


def sans_marqueurs(texte: str) -> str:
    """Retire l'identifiant de locuteur et les bornes que le modèle intercale."""
    return _LOCUTEUR.sub("", _HORODATAGE.sub("", texte)).strip()


def texte_des_segments(segments: list[dict]) -> str:
    """Le texte complet, recomposé des segments.

    Les segments font foi : ce sont eux qui portent les bornes, et un texte
    global qui divergerait d'eux serait une seconde vérité sur le même son. Le
    modèle en rend bien un — mais c'est le flux de diarisation aplati, marqueurs
    compris, et le recomposer d'ici coûte une jointure.
    """
    morceaux = [sans_marqueurs(str(s.get("text") or "")) for s in segments]
    return " ".join(m for m in morceaux if m)


def reproches(demandé: dict[str, Any]) -> list[str]:
    """Les paramètres demandés que ce variant n'honore pas, en phrases.

    Rendus en `warnings` du job plutôt qu'en erreur : ils ne changent pas la
    nature de la sortie, seulement sa finesse. Refuser priverait d'une
    transcription correcte pour un réglage sans effet.
    """
    dits = []
    for nom, (neutre, pourquoi) in INOPERANTS.items():
        valeur = demandé.get(nom)
        if valeur is not None and valeur != neutre:
            dits.append(f"« {nom} » sans effet sur ce variant : {pourquoi}")
    return dits


class MossTranscribeWorker(Worker):
    """Transcription : rend ce qui a été dit, et où."""

    name = "moss-transcribe"

    def __init__(self) -> None:
        self.mx: Any = None
        self.load_audio: Any = None
        self.model: Any = None
        self.defaults: dict[str, Any] = {}
        self._peak_load = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        mx, load_model, load_audio = _import_runtime()
        self.mx = mx
        self.load_audio = load_audio
        self.defaults = dict(variant.get("defaults") or {})

        brut = str(variant.get("weights_path") or "").strip()
        ref = variant.get("ref") or "<ref>"
        if not brut or not Path(brut).is_dir():
            raise WorkerError(
                f"poids absents : {brut or '(chemin vide)'} n'est pas un dossier — "
                f"télécharger avec : ecurie pull {ref}"
            )
        try:
            self.model = load_model(brut)
        except Exception as exc:  # noqa: BLE001 — remonte avec la réparation
            raise WorkerError(f"chargement impossible : {type(exc).__name__}: {exc}") from exc
        self._peak_load = self._pic() or 0
        # Aucune clé `languages` : le contrat expose un `x-options-from:
        # runtime.languages`, et ce modèle ne publie pas la liste de ce qu'il
        # entend. Annoncer une liste devinée ferait proposer dans l'Atelier des
        # langues dont personne n'a vérifié qu'elles marchent ; l'absence de la
        # clé fait dire à l'écran « aucune suggestion », ce qui est exact.
        return {"versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")

        # L'ordre est celui du coût : ce qui se refuse sans toucher au disque se
        # refuse d'abord.
        tâche = str(self._reglage(request, "task", "transcribe") or "transcribe")
        if tâche == "translate":
            raise WorkerError(
                "« task: translate » n'est pas honoré par ce variant : il transcrit dans "
                "la langue entendue et ne traduit pas. Rendre une transcription sous le "
                "nom d'une traduction serait un job qui a l'air d'avoir réussi. Pour "
                "traduire, enchaîner avec la capacité translation."
            )

        audio_path = self._fichier(request, "audio")
        # `InferRequest.get` lit l'entrée typée **puis** les `options:` du
        # variant : le contrat de cette capacité ne déclare pas `max_seconds`, et
        # c'est de là qu'il vient.
        max_seconds = float(self._reglage(request, "max_seconds", 1800))

        progress(8, "lecture de l'enregistrement")
        signal = self.load_audio(str(audio_path), sr=SAMPLE_RATE)
        échantillons = int(max_seconds * SAMPLE_RATE)
        tronqué = signal.shape[0] > échantillons
        if tronqué:
            signal = signal[:échantillons]
        durée = float(signal.shape[0]) / SAMPLE_RATE

        self.mx.reset_peak_memory()
        progress(20, f"transcription en cours ({durée:.0f} s)")
        début = time.monotonic()
        try:
            résultat = self.model.generate(signal, max_tokens=4096, temperature=0.0)
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte
            raise WorkerError(f"transcription impossible : {type(exc).__name__}: {exc}") from exc
        calcul = time.monotonic() - début

        bruts = getattr(résultat, "segments", None) or []
        segments = [
            {
                "start": round(float(s["start"]), 3),
                "end": round(float(s["end"]), 3),
                "text": sans_marqueurs(str(s.get("text") or "")),
            }
            for s in bruts
            if s.get("start") is not None and s.get("end") is not None
        ]
        # Les segments d'abord, le texte global ensuite : l'ordre inverse était
        # celui du premier jet, et il livrait « [1.28][S01] Je peux… » dans un
        # fichier que le contrat annonce en texte brut.
        texte = texte_des_segments(segments) or sans_marqueurs(
            str(getattr(résultat, "text", "") or "")
        )

        progress(90, "écriture")
        (request.output_dir / TEXTE_NAME).write_text(texte + "\n", encoding="utf-8")
        (request.output_dir / SEGMENTS_NAME).write_text(
            json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Ce que le modèle dit de la langue s'il le dit ; sinon ce qui a été
        # demandé ; sinon rien de plus que « détectée ». Inventer un code de
        # langue serait pire que de laisser le champ à sa valeur d'ignorance.
        langue = (
            str(getattr(résultat, "language", "") or "").strip()
            or str(self._reglage(request, "language", "") or "").strip()
            or "auto"
        )

        # Rangés dans les métriques et non dans un champ `warnings` : le
        # protocole worker↔superviseur n'en transporte pas, et les réserves de ce
        # variant sont déjà à l'écran avant le lancement — la fiche du variant
        # affiche ses `caveats`. Ce qui est ajouté ici, c'est la trace au
        # manifeste de ce qui a été demandé pour ce job-là précisément.
        sans_effet = reproches({nom: request.get(nom) for nom in INOPERANTS})

        return InferResult(
            output={
                "text": TEXTE_NAME,
                "segments": SEGMENTS_NAME,
                "language": langue,
                "duration_seconds": round(durée, 3),
            },
            metrics={
                "segments": len(segments),
                "characters": len(texte),
                "truncated": tronqué,
                # Le facteur temps réel : sous 1, la machine transcrit plus vite
                # qu'on ne parle. C'est le chiffre comparable entre modèles de
                # cette capacité, et celui que le banc d'essai inscrit au profil.
                "rtf": round(calcul / durée, 4) if durée > 0 else None,
                "infer_ms": int(calcul * 1000),
                "reglages_sans_effet": sans_effet,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self.model = None
        self._peak_load = 0
        gc.collect()
        if self.mx is not None:
            self.mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        pic = self._pic()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- détails -------------------------------------------------------------

    def _fichier(self, request: InferRequest, nom: str) -> Path:
        brut = str(self._reglage(request, nom, "") or "").strip()
        if not brut:
            raise WorkerError(f"« {nom} » est obligatoire")
        chemin = Path(brut).expanduser()
        if not chemin.is_absolute():
            chemin = request.output_dir / chemin
        if not chemin.is_file():
            raise WorkerError(f"{nom} introuvable : {chemin}")
        return chemin

    def _reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        if self.defaults.get(nom) is not None:
            return self.defaults[nom]
        return defaut

    def _pic(self) -> int | None:
        if self.mx is None:
            return None
        try:
            return int(self.mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne fait pas échouer un job
            return None

    def _versions(self) -> dict[str, str]:
        versions = {}
        for nom, module in (("mlx", "mlx.core"), ("mlx-audio", "mlx_audio")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


if __name__ == "__main__":
    raise SystemExit(main(MossTranscribeWorker))
