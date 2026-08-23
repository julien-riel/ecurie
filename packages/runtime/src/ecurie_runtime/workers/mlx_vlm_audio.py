"""Écouter, avec le moteur de mlx-vlm — la modalité qui manquait au parc de ce côté.

`audio-to-text` est déjà servi par `workers.qwen2_audio`, sur le runtime
`mlx-audio` et sur d'autres poids. Ce module ne le remplace pas : il ouvre la
capacité aux modèles **omni**, ceux dont un seul jeu de poids voit et entend.
MiniCPM-o 4.5 en est un — sa configuration porte un encodeur `siglip` et un
encodeur `whisper` côte à côte —, et Gemma 4 en est un autre.

**Ce que cela change tient à la mémoire, pas à la qualité.** Entendre et voir
supposaient jusqu'ici deux modèles, donc deux chargements, donc deux fois le
budget — et sur une machine dont le contrôle d'admission n'admet qu'un modèle
lourd à la fois, cela voulait dire décharger l'un pour interroger l'autre. Un
modèle omni fait les deux sans rien décharger.

**L'audio ne passe pas par l'invite, il passe à côté.** `stream_generate` prend
un paramètre `audio` comme il prend `image`, et le gabarit de conversation doit
savoir qu'un son l'accompagne — d'où `num_audios`, sans quoi l'invite ne porte
pas le jeton de remplacement et le modèle décrit un silence sans le dire.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).
"""

import time
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    WorkerError,
    main,
    sans_raisonnement,
)
from ecurie_runtime.workers.mlx_lm import MlxLmBase
from ecurie_runtime.workers.mlx_vlm import REPAIR
from ecurie_runtime.workers.mlx_vlm_lm import SurMlxVlm

OUTPUT_TEXT = "text.txt"

# Les conteneurs que les décodeurs des runtimes ouvrent sans extension. Fermée
# pour la même raison que celle des images : un `.opus` refusé avec la liste des
# formats acceptés vaut mieux qu'un décodage qui rend zéro échantillon et une
# erreur venue de trois couches plus bas.
AUDIOS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".aiff", ".mp4"}

DESCRIPTION = (
    "Écoute cet enregistrement et décris ce qu'on y entend : les sons, les "
    "voix, la musique. Ne rapporte que ce que tu entends."
)
QUESTION = (
    "Réponds à la question suivante en te fondant uniquement sur cet "
    "enregistrement. S'il ne permet pas de répondre, dis-le plutôt que de deviner."
)


def build_prompt(question: str | None, langue: str | None) -> str:
    """La consigne, composée depuis les champs du contrat.

    La langue vient en dernier, comme pour la description d'image et pour la
    même raison observée : une consigne de langue placée avant la tâche se fait
    oublier au bout de quelques dizaines de jetons.
    """
    demande = (question or "").strip()
    consigne = f"{QUESTION}\n\nQuestion : {demande}" if demande else DESCRIPTION
    voulue = str(langue or "").strip()
    if voulue and voulue.lower() not in ("auto", ""):
        consigne += f" Réponds en {voulue}."
    return consigne


def resolve_audio(valeur: Any, job_dir: Path) -> Path:
    """Le chemin de l'enregistrement, relatif au job ou absolu, format vérifié."""
    brut = str(valeur or "").strip()
    if not brut:
        raise WorkerError("aucun enregistrement fourni")
    chemin = Path(brut).expanduser()
    if not chemin.is_absolute():
        chemin = job_dir / chemin
    if not chemin.is_file():
        raise WorkerError(f"enregistrement introuvable : {chemin}")
    if chemin.suffix.lower() not in AUDIOS:
        raise WorkerError(
            f"format non géré : {chemin.suffix or '(sans extension)'} — "
            f"formats acceptés : {', '.join(sorted(AUDIOS))}"
        )
    return chemin


class MlxVlmAudioWorker(SurMlxVlm, MlxLmBase):
    """Question sur un enregistrement, par modèle omni."""

    name = "mlx-vlm-audio"

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._model is None:
            raise WorkerError("modèle non chargé")

        audio = resolve_audio(request.get("audio"), request.output_dir)
        question = self.reglage(request, "question", None)
        langue = self.reglage(request, "language", None)
        max_tokens = int(self.reglage(request, "max_tokens", 512))
        température = float(self.reglage(request, "temperature", 0.2))

        progress(10, "écoute en cours")
        début = time.monotonic()
        réponse, _ = self.engendrer(
            [{"role": "user", "content": build_prompt(question, langue)}],
            progress=progress,
            max_tokens=max_tokens,
            temperature=température,
            seed=request.seed,
            etape="écoute",
            audio=str(audio),
        )
        calcul = time.monotonic() - début

        texte, raisonnement = sans_raisonnement(réponse.text)
        progress(92, "écriture")
        (request.output_dir / OUTPUT_TEXT).write_text(texte, encoding="utf-8")

        return InferResult(
            output={
                "text": OUTPUT_TEXT,
                # `listened_seconds` est ce que le modèle a réellement écouté.
                # Le worker ne décode pas le son lui-même — le processeur du
                # runtime s'en charge —, donc il ne peut pas le mesurer sans
                # ouvrir le fichier une seconde fois pour rien. Zéro serait un
                # chiffre faux ; l'absence est une absence.
                "listened_seconds": _duree(audio),
                "tokens_generated": réponse.generation_tokens,
                "finish_reason": réponse.finish_reason,
            },
            metrics={
                "characters": len(texte),
                "generation_tokens": réponse.generation_tokens,
                "tokens_per_second": réponse.tokens_per_second,
                "reasoning_characters": len(raisonnement),
                "seconds": round(calcul, 3),
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def _flux(
        self,
        invite: Any,
        *,
        max_tokens: int,
        sampler: Any,
        logits_processors: Any = None,
    ) -> Any:
        """Comme `SurMlxVlm._flux`, l'enregistrement en plus.

        L'audio se lit sur l'instance et non dans la signature : `engendrer` est
        hérité de `MlxLmBase`, qui ne connaît pas cette modalité et n'appelle
        donc `_flux` qu'avec ce que les trois autres adaptateurs lui passent. Un
        paramètre ajouté ici resterait à sa valeur par défaut — le son serait
        chargé, jamais transmis, et le modèle décrirait un silence.
        """
        runtime = self._runtime
        if runtime is None:
            raise WorkerError("modèle non chargé")
        audio = self._audio_courant
        return runtime.stream_generate(
            self._model,
            self._processor,
            invite,
            image=None,
            audio=[audio] if audio else None,
            max_tokens=int(max_tokens),
            sampler=sampler,
            **({"logits_processors": logits_processors} if logits_processors else {}),
        )

    def engendrer(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        """Retient l'enregistrement le temps de l'appel.

        `engendrer` est hérité et ne connaît pas l'audio ; `_flux` en a besoin.
        Le passer par un attribut plutôt que d'élargir la signature de la classe
        de base évite d'imposer une modalité à trois adaptateurs qui l'ignorent.
        """
        self._audio_courant = kwargs.pop("audio", None)
        try:
            return super().engendrer(messages, **kwargs)
        finally:
            self._audio_courant = None

    def _invite(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        thinking: bool | None = None,
    ) -> tuple[Any, bool]:
        """Compose l'invite en déclarant l'enregistrement au gabarit.

        Sans `num_audios`, l'invite ne porte pas le jeton de remplacement du son :
        le modèle reçoit bien les échantillons mais aucune place où les lire, et
        il répond en décrivant un silence — sans que rien ne signale l'écart.
        """
        runtime_processor = self._processor
        if runtime_processor is None or self._config is None:
            return super()._invite(messages, tools, thinking=thinking)
        from mlx_vlm import (
            apply_chat_template,  # noqa: PLC0415 — import paresseux, comme le runtime
        )

        extra: dict[str, Any] = {} if thinking is None else {"enable_thinking": bool(thinking)}
        contenu = messages[-1].get("content", "") if messages else ""
        try:
            invite = apply_chat_template(
                runtime_processor,
                self._config,
                contenu,
                num_images=0,
                num_audios=1 if self._audio_courant else 0,
                **extra,
            )
        except Exception:  # noqa: BLE001 — gabarit qui refuse le drapeau : sans lui plutôt que rien
            invite = apply_chat_template(
                runtime_processor,
                self._config,
                contenu,
                num_images=0,
                num_audios=1 if self._audio_courant else 0,
            )
        return invite, False

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        annonce = super().load(variant)
        chemin = str(variant.get("weights_path") or "")
        try:
            from mlx_vlm.utils import load_config  # noqa: PLC0415 — import paresseux
        except ImportError as exc:
            raise WorkerError(f"runtime mlx-vlm indisponible ({exc}) — `{REPAIR}`") from exc
        self._config = load_config(chemin)
        # Liste ouverte : un modèle omni répond dans toutes les langues qu'il
        # connaît, et une liste fermée en refuserait qu'il maîtrise très bien.
        return {**annonce, "languages": []}

    def __init__(self) -> None:
        super().__init__()
        self._config: Any = None
        self._audio_courant: str | None = None


def _duree(chemin: Path) -> float | None:
    """La durée de l'enregistrement, si le conteneur la déclare sans le décoder.

    `wave` lit l'en-tête d'un WAV sans toucher aux échantillons. Pour les autres
    conteneurs il faudrait un décodeur, et rendre `None` vaut mieux qu'un chiffre
    inventé : le contrat déclare la durée écoutée, pas la durée devinée.
    """
    if chemin.suffix.lower() != ".wav":
        return None
    try:
        import wave  # noqa: PLC0415 — bibliothèque standard, chargée au besoin

        with wave.open(str(chemin), "rb") as fichier:
            cadence = fichier.getframerate()
            return round(fichier.getnframes() / cadence, 3) if cadence else None
    except Exception:  # noqa: BLE001 — un en-tête illisible ne fait pas échouer un job
        return None


if __name__ == "__main__":
    raise SystemExit(main(MlxVlmAudioWorker))
