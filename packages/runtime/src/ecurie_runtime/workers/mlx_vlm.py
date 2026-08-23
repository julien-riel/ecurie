"""Adaptateur mlx-vlm : lecture de document par modèle vision-langage.

Quatrième runtime du parc, et le premier ajouté après le v0.3 — l'occasion de
vérifier ce que le protocole promettait : servir la capacité `document-to-text`
n'a demandé ni de toucher au superviseur, ni au contrat, ni à la CLI.

Le choix d'un VLM plutôt que d'un OCR classique n'est pas une préférence
technique. Un moteur par segmentation rend des boîtes et des mots ; un modèle
vision-langage rend un texte structuré, respecte l'ordre de lecture d'une mise en
page à colonnes et sait qu'un tableau est un tableau. C'est ce que le contrat
`document-to-text` demande, avec son `format: markdown` et son `detect_layout`.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).

Ce qui suit a été écrit contre l'API de mlx-vlm 0.6.15, relevée dans le paquet
installé : `load()` rend `(model, processor)`, `apply_chat_template()` compose
l'invite, `generate()` rend un `GenerationResult` porteur de son propre
`peak_memory`. Les bornes du pyproject de l'env verrouillent cette hypothèse.
"""

import gc
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
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
    sans_raisonnement,
)

OUTPUT_TEXT = "text.txt"
OUTPUT_LAYOUT = "layout.json"
REPAIR = "ecurie env sync mlx-vlm"

# Un document rendu trop petit devient illisible, trop grand sature la fenêtre
# d'attention du modèle sans rien apporter. 200 ppp est le compromis usuel pour
# du texte imprimé ; le contrat laisse l'utilisateur le régler.
DEFAULT_DPI = 200
MAX_PAGES = 20

# Le nombre de jetons à produire dépend de la densité de la page, pas d'un
# réglage d'utilisateur : une page de roman fait dans les 700 mots, un tableau
# dense bien plus. On plafonne large et on laisse le modèle s'arrêter seul.
MAX_TOKENS_PAR_PAGE = 4096

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}

CONSIGNES = {
    "markdown": (
        "Transcris intégralement le texte de cette page en Markdown. Respecte "
        "l'ordre de lecture, les titres, les listes et les tableaux. N'ajoute "
        "aucun commentaire, aucune introduction : rends uniquement le contenu."
    ),
    "text": (
        "Transcris intégralement le texte de cette page, sans mise en forme. "
        "Respecte l'ordre de lecture et les retours à la ligne. N'ajoute aucun "
        "commentaire : rends uniquement le texte."
    ),
}


@dataclass(frozen=True)
class Runtime:
    """Les points d'entrée de mlx-vlm dont l'adaptateur dépend, et eux seuls."""

    mx: Any
    load: Callable[..., Any]
    generate: Callable[..., Any]
    apply_chat_template: Callable[..., Any]
    load_config: Callable[..., Any]


@dataclass(frozen=True)
class PagePlan:
    """Une page à lire : son image sur le disque, et son rang dans le document."""

    numero: int
    image: Path


def import_runtime() -> Runtime:
    """Importe mlx-vlm, ou explique comment réparer l'environnement."""
    try:
        import mlx.core as mx
        from mlx_vlm import apply_chat_template, generate, load
        from mlx_vlm.utils import load_config
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-vlm indisponible dans cet environnement ({exc}) — "
            f"le reconstruire avec `{REPAIR}`"
        ) from exc
    return Runtime(
        mx=mx,
        load=load,
        generate=generate,
        apply_chat_template=apply_chat_template,
        load_config=load_config,
    )


def thinking_demande(options: Mapping[str, Any], defaults: Mapping[str, Any]) -> bool:
    """Le variant demande-t-il que le modèle raisonne à voix haute ? Non, par défaut.

    Hors du contrat de capacité, donc dans les `options` du variant : le mode
    « thinking » n'est pas un réglage qu'on voudrait voir passer d'un modèle à
    l'autre dans un formulaire — la moitié du parc ne sait pas ce que c'est.
    """
    return bool(options.get("thinking", defaults.get("thinking", False)))


def composer_invite(
    runtime: Runtime,
    processor: Any,
    config: Any,
    consigne: Any,
    *,
    num_images: int = 1,
    thinking: bool = False,
    **extra: Any,
) -> Any:
    """Applique le gabarit de conversation en disant s'il faut raisonner à voix haute.

    Les quatre chemins visuels passent par ici pour une raison qui n'était pas
    prévisible : les familles récentes pensent avant de répondre. Qwen3.6 émet
    un `<think>…</think>` par défaut, et sur ces contrats-là il coûte deux fois
    — il consomme le `max_tokens` de la réponse, et il s'intercale devant elle.
    Sur la détection, où la sortie est extraite par motif, il fait tomber
    l'extraction à zéro objet : le modèle aurait vu juste, le job rendrait vide.

    Le repli n'est pas décoratif. `enable_thinking` traverse `**kwargs` jusqu'au
    gabarit Jinja, qui l'ignore poliment quand il ne le connaît pas — mais rien
    ne garantit que toutes les versions de `transformers` et de `mlx-vlm` s'y
    prêtent. Un gabarit qui refuse le drapeau doit rendre une invite sans lui,
    pas faire échouer le job.
    """
    try:
        return runtime.apply_chat_template(
            processor, config, consigne, num_images=num_images, enable_thinking=thinking, **extra
        )
    except Exception:  # noqa: BLE001 — gabarit qui refuse le drapeau : sans lui plutôt que rien
        return runtime.apply_chat_template(
            processor, config, consigne, num_images=num_images, **extra
        )


# --- préparation du document -------------------------------------------------


def parse_page_range(brut: str | None, total: int) -> list[int]:
    """« 1-5,8 » → [1, 2, 3, 4, 5, 8]. Vide ou absent = tout le document.

    Les bornes hors document sont écartées en silence plutôt que refusées : un
    « 1-999 » sur un document de trois pages exprime « tout », et échouer là-
    dessus n'aiderait personne.
    """
    if not brut or not brut.strip():
        return list(range(1, total + 1))
    pages: list[int] = []
    for morceau in brut.split(","):
        morceau = morceau.strip()
        if not morceau:
            continue
        if "-" in morceau:
            début, _, fin = morceau.partition("-")
            try:
                bornes = range(int(début), int(fin) + 1)
            except ValueError as exc:
                raise WorkerError(f"page_range illisible : {morceau!r}") from exc
            pages.extend(bornes)
        else:
            try:
                pages.append(int(morceau))
            except ValueError as exc:
                raise WorkerError(f"page_range illisible : {morceau!r}") from exc
    retenues = sorted({p for p in pages if 1 <= p <= total})
    if not retenues:
        raise WorkerError(
            f"page_range {brut!r} ne désigne aucune page d'un document qui en compte {total}"
        )
    return retenues


def resolve_document(valeur: Any, job_dir: Path) -> Path:
    """Le chemin du document, relatif au dossier du job quand il l'est.

    Le superviseur copie l'entrée dans le dossier du job et transmet un chemin
    relatif — c'est ce qui rend le job rejouable ailleurs. Un chemin absolu reste
    accepté : le banc d'essai en passe.
    """
    brut = str(valeur or "").strip()
    if not brut:
        raise WorkerError("aucun document à lire : le champ `document` est vide")
    chemin = Path(brut)
    if not chemin.is_absolute():
        chemin = job_dir / chemin
    if not chemin.is_file():
        raise WorkerError(f"document introuvable : {chemin}")
    return chemin


def pdf_to_images(source: Path, dossier: Path, dpi: int, pages: str | None) -> list[PagePlan]:
    """Rend les pages demandées d'un PDF en PNG.

    Un VLM ne lit pas un PDF : il lit une image. Faire ce rendu ici plutôt que
    de l'exiger de l'appelant est ce qui permet au contrat de capacité d'accepter
    les deux formats sans mentir.
    """
    try:
        import pymupdf
    except ImportError as exc:
        raise WorkerError(
            f"lecture de PDF impossible, pymupdf absent ({exc}) — `{REPAIR}`"
        ) from exc

    dossier.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        total = document.page_count
        numeros = parse_page_range(pages, total)
        if len(numeros) > MAX_PAGES:
            raise WorkerError(
                f"{len(numeros)} pages demandées : au-delà de {MAX_PAGES}, un job devient "
                "une file d'attente. Découper avec `page_range`."
            )
        plans = []
        for numero in numeros:
            page = document.load_page(numero - 1)
            rendu = page.get_pixmap(dpi=dpi)
            cible = dossier / f"page-{numero:03d}.png"
            rendu.save(cible)
            plans.append(PagePlan(numero=numero, image=cible))
    return plans


def plan_pages(source: Path, job_dir: Path, *, dpi: int, page_range: str | None) -> list[PagePlan]:
    if source.suffix.lower() == ".pdf":
        return pdf_to_images(source, job_dir / "pages", dpi, page_range)
    if source.suffix.lower() in IMAGES:
        # Une image est une page unique : `page_range` n'a rien à trancher, et le
        # signaler vaut mieux que de l'ignorer.
        if page_range and page_range.strip() not in ("", "1"):
            raise WorkerError(
                f"page_range {page_range!r} sur une image : une image est une page unique"
            )
        return [PagePlan(numero=1, image=source)]
    raise WorkerError(
        f"format non géré : {source.suffix or '(sans extension)'} — "
        f"PDF ou image ({', '.join(sorted(IMAGES))})"
    )


def build_prompt(format_voulu: str, langue: str | None, detect_layout: bool) -> str:
    """La consigne envoyée au modèle, construite depuis les champs du contrat."""
    consigne = CONSIGNES.get(format_voulu, CONSIGNES["markdown"])
    if langue and langue.strip() and langue.strip().lower() not in ("auto", ""):
        consigne += f" Le document est en {langue.strip()}."
    if not detect_layout:
        consigne += " Ignore la mise en page : rends le texte au fil de la lecture."
    return consigne


# --- le worker ---------------------------------------------------------------


class MlxVlmWorker(Worker):
    """Lecture de document par modèle vision-langage : une page, une transcription."""

    name = "mlx-vlm"

    def __init__(self) -> None:
        self._runtime: Runtime | None = None
        self._model: Any = None
        self._processor: Any = None
        self._config: Any = None
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._thinking = False
        self._peak_load = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        runtime = import_runtime()
        chemin = Path(str(variant.get("weights_path") or ""))
        if not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                "déjà vérifié, un worker ne télécharge jamais"
            )

        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})
        self._thinking = thinking_demande(self._options, self._defaults)

        model, processor = runtime.load(str(chemin))
        self._runtime = runtime
        self._model = model
        self._processor = processor
        self._config = runtime.load_config(str(chemin))
        self._peak_load = self._pic_mlx() or 0

        return {
            # Le contrat déclare `x-options-from: runtime.languages` : un VLM les
            # accepte toutes, et prétendre le contraire par une liste fermée
            # ferait refuser une langue qu'il lit très bien.
            "languages": [],
            "formats": sorted(CONSIGNES),
            "max_pages": MAX_PAGES,
            "versions": self._versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._runtime is None or self._model is None:
            raise WorkerError("modèle non chargé")
        runtime = self._runtime

        source = resolve_document(request.get("document"), request.output_dir)
        dpi = int(self._reglage(request, "dpi", DEFAULT_DPI))
        format_voulu = str(self._reglage(request, "format", "markdown"))
        langue = self._reglage(request, "language", None)
        detect_layout = bool(self._reglage(request, "detect_layout", True))

        progress(5, "préparation du document")
        pages = plan_pages(
            source,
            request.output_dir,
            dpi=dpi,
            page_range=self._reglage(request, "page_range", None),
        )
        consigne = build_prompt(format_voulu, langue, detect_layout)

        runtime.mx.reset_peak_memory()
        if request.seed is not None:
            runtime.mx.random.seed(int(request.seed))

        transcriptions: list[str] = []
        structure: list[dict[str, Any]] = []
        jetons = 0
        début = time.monotonic()

        for index, page in enumerate(pages):
            # La progression est par page : c'est la seule granularité honnête,
            # `generate` ne rend la main qu'une fois la page entière produite.
            progress(
                10 + int(80 * index / max(len(pages), 1)),
                f"page {page.numero} sur {len(pages)}",
            )
            texte, résultat = self._lire_page(page, consigne)
            transcriptions.append(texte)
            jetons += int(getattr(résultat, "generation_tokens", 0) or 0)
            structure.append(
                {
                    "page": page.numero,
                    "characters": len(texte),
                    "finish_reason": getattr(résultat, "finish_reason", None),
                    "generation_tokens": getattr(résultat, "generation_tokens", None),
                }
            )

        calcul = time.monotonic() - début
        progress(92, "écriture")

        # Le séparateur de page est explicite : un document recollé sans marque
        # ne se redécoupe plus, et l'utilisateur ne sait plus d'où vient un
        # passage — ce qui compte dès qu'on relit une transcription de 20 pages.
        texte_complet = "\n\n".join(
            t if len(pages) == 1 else f"<!-- page {p.numero} -->\n{t}"
            for p, t in zip(pages, transcriptions, strict=True)
        )
        (request.output_dir / OUTPUT_TEXT).write_text(texte_complet, encoding="utf-8")

        sorties = {"text": OUTPUT_TEXT, "page_count": len(pages)}
        if detect_layout:
            (request.output_dir / OUTPUT_LAYOUT).write_text(
                json.dumps({"pages": structure}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            sorties["layout"] = OUTPUT_LAYOUT

        return InferResult(
            output=sorties,
            metrics={
                "pages": len(pages),
                "characters": len(texte_complet),
                "generation_tokens": jetons,
                "tokens_per_second": round(jetons / calcul, 2) if calcul > 0 else None,
                "seconds_per_page": round(calcul / max(len(pages), 1), 2),
                "dpi": dpi,
                "format": format_voulu,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self._model = None
        self._processor = None
        self._peak_load = 0
        # L'ordre compte : tant qu'une référence Python tient les tableaux, leurs
        # buffers ne sont que « cachés » et `clear_cache` ne rend rien au système.
        gc.collect()
        if self._runtime is not None:
            self._runtime.mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        """Pic MLX en octets — la mesure juste ici, bien plus que le RSS.

        Le plancher du chargement est conservé parce que `reset_peak_memory()`
        est appelé à chaque job : sans lui, une page très courte rapporterait un
        pic inférieur au poids résident du modèle, et le contrôle d'admission
        laisserait entrer un second résident que la mémoire ne peut pas tenir.
        """
        pic = self._pic_mlx()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- détails -------------------------------------------------------------

    def _lire_page(self, page: PagePlan, consigne: str) -> tuple[str, Any]:
        runtime = self._runtime
        assert runtime is not None
        invite = composer_invite(
            runtime, self._processor, self._config, consigne, num_images=1, thinking=self._thinking
        )
        try:
            résultat = runtime.generate(
                self._model,
                self._processor,
                invite,
                image=[str(page.image)],
                max_tokens=MAX_TOKENS_PAR_PAGE,
                # Une transcription n'est pas une création : on veut le texte de
                # la page, pas une variation dessus. La température par défaut
                # de mlx-vlm échantillonne, ce qui invente des mots là où
                # l'image est ambiguë.
                temperature=0.0,
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(
                f"lecture de la page {page.numero} impossible : {type(exc).__name__}: {exc}"
            ) from exc
        texte = getattr(résultat, "text", None)
        if texte is None:
            texte = str(résultat)
        # Page par page : un modèle qui réfléchit le fait à chaque page, et un
        # `<think>` recollé au milieu d'une transcription se lirait comme du
        # texte du document.
        propre, _raisonnement = sans_raisonnement(texte.strip())
        return propre, résultat

    def _reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        """Entrée du job, puis options du variant, puis défauts du manifeste."""
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        for couche in (self._options, self._defaults):
            if couche.get(nom) is not None:
                return couche[nom]
        return defaut

    def _pic_mlx(self) -> int | None:
        if self._runtime is None:
            return None
        try:
            return int(self._runtime.mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne doit pas faire échouer un job
            return None

    def _versions(self) -> dict[str, str]:
        """Ce qui a produit la transcription, pour le manifeste du job."""
        versions = {}
        for nom, module in (("mlx", "mlx.core"), ("mlx-vlm", "mlx_vlm")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


def _avertir(message: str) -> None:
    print(f"[mlx-vlm] {message}", file=sys.stderr, flush=True)


def documents_lisibles() -> Sequence[str]:
    """Extensions acceptées, pour un message d'erreur ou une UI."""
    return (".pdf", *sorted(IMAGES))


def defaults_du_contrat(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Valeurs par défaut déclarées par le contrat — utile aux tests de dérive."""
    return {
        nom: champ["default"]
        for nom, champ in (contract.get("input", {}).get("properties") or {}).items()
        if "default" in champ
    }


if __name__ == "__main__":
    raise SystemExit(main(MlxVlmWorker))
