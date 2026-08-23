"""Adaptateur `mlx-lm` : génération de texte, et socle de ses deux voisins.

Cinquième famille de runtime du parc, et la première dont un seul modèle chargé
sert **trois** capacités : `text-generation` ici, `translation` et `tool-use`
dans les modules voisins. Le chargement, l'échantillonnage et la mesure sont
communs — ils vivent dans ce fichier ; composer une invite de traduction et
extraire un appel d'outil validable ne le sont pas, et vivent à côté.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).

Écrit contre l'API de `mlx-lm` 0.21+ : `load()` rend `(model, tokenizer)`,
`stream_generate()` cède des réponses porteuses de `generation_tokens`,
`finish_reason` et `peak_memory`. Le flux plutôt que `generate()` d'un bloc, pour
deux raisons : la progression devient honnête sur une réponse longue, et les
compteurs de jetons arrivent sans avoir à re-tokeniser la sortie.
"""

import gc
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import (
    FluxRaisonnement,
    InferRequest,
    InferResult,
    ProgressFn,
    Worker,
    WorkerError,
    main,
    peak_rss_bytes,
    sans_raisonnement,
)

ENV_NAME = "mlx-lm"
REPAIR = f"ecurie env sync {ENV_NAME}"
OUTPUT_TEXT = "text.txt"


@dataclass(frozen=True)
class Runtime:
    """Les points d'entrée de mlx-lm dont les adaptateurs dépendent, et eux seuls."""

    mx: Any
    load: Any
    stream_generate: Any
    make_sampler: Any
    make_logits_processors: Any


@dataclass
class Reponse:
    """Ce qu'une génération a produit, et ce qu'elle a coûté."""

    text: str = ""
    generation_tokens: int = 0
    prompt_tokens: int = 0
    finish_reason: str = "stop"
    seconds: float = 0.0
    # Le raisonnement à voix haute, séparé de la réponse plutôt que jeté. Les
    # modèles à mode « thinking » l'émettent entre <think> et </think> ; le
    # laisser dans `text` fausserait une traduction notée au caractère près et
    # ferait échouer l'extraction d'un appel d'outil. Le supprimer sans le garder
    # priverait qui relit le job de la seule trace expliquant une réponse.
    reasoning: str = ""

    @property
    def tokens_per_second(self) -> float | None:
        return round(self.generation_tokens / self.seconds, 2) if self.seconds > 0 else None


def import_runtime() -> Runtime:
    """Importe mlx-lm, ou explique comment réparer l'environnement."""
    try:
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.generate import stream_generate
        from mlx_lm.sample_utils import make_logits_processors, make_sampler
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-lm indisponible dans cet environnement ({exc}) — `{REPAIR}`"
        ) from exc
    return Runtime(
        mx=mx,
        load=load,
        stream_generate=stream_generate,
        make_sampler=make_sampler,
        make_logits_processors=make_logits_processors,
    )


class MlxLmBase(Worker):
    """Chargement, échantillonnage et mesure, communs aux trois capacités."""

    name = "mlx-lm"

    def __init__(self) -> None:
        self._runtime: Runtime | None = None
        self._model: Any = None
        self._tokenizer: Any = None
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._peak_load = 0

    # --- chargement ----------------------------------------------------------

    def _import_runtime(self) -> Runtime:
        """Le moteur d'inférence de ce worker. Surchargé par les adaptateurs mlx-vlm."""
        return import_runtime()

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        runtime = self._import_runtime()
        chemin = Path(str(variant.get("weights_path") or "").strip())
        if not str(chemin) or not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                "déjà vérifié, un worker ne télécharge jamais"
            )

        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})

        model, tokenizer = runtime.load(str(chemin))
        self._runtime = runtime
        self._model = model
        self._tokenizer = tokenizer
        self._peak_load = self._pic_mlx() or 0

        return {**self.annonce(), "versions": self._versions()}

    def annonce(self) -> dict[str, Any]:
        """Ce que le worker déclare savoir faire, par capacité."""
        return {}

    def unload(self) -> None:
        self._model = None
        self._tokenizer = None
        self._peak_load = 0
        # L'ordre compte : tant qu'une référence Python tient les tableaux, leurs
        # buffers ne sont que « cachés » et `clear_cache` ne rend rien au système.
        gc.collect()
        if self._runtime is not None:
            self._runtime.mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        """Pic MLX, plancher au poids résident du modèle.

        `reset_peak_memory()` est appelé à chaque job : sans le plancher, une
        réponse de trois jetons rapporterait un pic inférieur à ce que le modèle
        occupe en permanence, et le contrôle d'admission laisserait entrer un
        second résident que la mémoire ne peut pas tenir.
        """
        pic = self._pic_mlx()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- génération ----------------------------------------------------------

    def engendrer(
        self,
        messages: list[dict[str, Any]],
        *,
        progress: ProgressFn,
        max_tokens: int,
        temperature: float = 0.0,
        top_p: float = 1.0,
        repetition_penalty: float = 1.0,
        seed: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking: bool | None = None,
        etape: str = "génération",
    ) -> tuple[Reponse, bool]:
        """Une génération complète. Rend la réponse et si le gabarit a pris `tools`.

        Le second membre n'est pas un détail d'implémentation : un modèle dont le
        gabarit de conversation ignore les outils reçoit leur description dans le
        message système, ce qui ne mesure plus tout à fait la même chose. Le
        taire ferait passer pour une faiblesse du modèle ce qui est une
        particularité de son gabarit.
        """
        if self._runtime is None or self._model is None:
            raise WorkerError("modèle non chargé")
        runtime = self._runtime

        invite, gabarit_outils = self._invite(messages, tools, thinking=thinking)

        runtime.mx.reset_peak_memory()
        if seed is not None:
            runtime.mx.random.seed(int(seed))

        échantillonneur = runtime.make_sampler(temp=float(temperature), top_p=float(top_p))
        processeurs = (
            runtime.make_logits_processors(repetition_penalty=float(repetition_penalty))
            if repetition_penalty and repetition_penalty > 1.0
            else None
        )

        morceaux: list[str] = []
        aiguilleur = FluxRaisonnement()
        réponse = Reponse()
        début = time.monotonic()
        try:
            flux = self._flux(
                invite,
                max_tokens=int(max_tokens),
                sampler=échantillonneur,
                logits_processors=processeurs,
            )
            for index, morceau in enumerate(flux):
                fragment = getattr(morceau, "text", "") or ""
                morceaux.append(fragment)
                # Le texte part vers qui regarde au moment où il est produit, et
                # non à la fin : c'est tout l'objet du flux. Le résultat, lui, est
                # recomposé plus bas depuis `morceaux` — ce canal ne le remplace
                # pas, et un fragment qui se perdrait ne changerait rien au job.
                for texte, canal in aiguilleur.pousser(fragment):
                    self.stream(texte, canal)
                if index % 32 == 0:
                    # Bornée à 88 : la progression ne doit jamais annoncer la fin
                    # avant que le fichier de sortie ne soit écrit.
                    avancement = 10 + int(78 * min(index / max(int(max_tokens), 1), 1.0))
                    progress(min(avancement, 88), etape)
                réponse.generation_tokens = int(
                    getattr(morceau, "generation_tokens", 0) or réponse.generation_tokens
                )
                réponse.prompt_tokens = int(
                    getattr(morceau, "prompt_tokens", 0) or réponse.prompt_tokens
                )
                raison = getattr(morceau, "finish_reason", None)
                if raison:
                    réponse.finish_reason = "length" if raison == "length" else "stop"
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(f"génération impossible : {type(exc).__name__}: {exc}") from exc

        for texte_restant, canal in aiguilleur.vider():
            self.stream(texte_restant, canal)
        réponse.seconds = time.monotonic() - début
        texte = _sans_marqueur_de_fin("".join(morceaux), self._tokenizer)
        # Après le marqueur de fin, jamais avant : un raisonnement refermé au
        # dernier jeton porte le marqueur collé à son `</think>`.
        réponse.text, réponse.reasoning = sans_raisonnement(texte)
        if not réponse.generation_tokens:
            réponse.generation_tokens = len(morceaux)
        return réponse, gabarit_outils

    def _flux(
        self,
        invite: Any,
        *,
        max_tokens: int,
        sampler: Any,
        logits_processors: Any = None,
    ) -> Any:
        """L'appel au moteur, isolé pour que d'autres runtimes le remplacent.

        `mlx-vlm` sert les mêmes trois capacités avec la même signature à une
        chose près — il attend un processor là où `mlx-lm` attend un tokenizer,
        et veut savoir qu'il n'y a pas d'image. C'est la seule ligne qui les
        sépare ; la surcharger coûte moins que de recopier `engendrer`.
        """
        runtime = self._runtime
        if runtime is None:
            raise WorkerError("modèle non chargé")
        return runtime.stream_generate(
            self._model,
            self._tokenizer,
            invite,
            max_tokens=int(max_tokens),
            sampler=sampler,
            **({"logits_processors": logits_processors} if logits_processors else {}),
        )

    def _invite(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        thinking: bool | None = None,
    ) -> tuple[Any, bool]:
        """Applique le gabarit de conversation, avec les outils quand il les prend.

        `thinking` n'est transmis que s'il est demandé. Les gabarits qui ignorent
        `enable_thinking` n'y verront rien — Jinja ne se plaint pas d'une
        variable inutilisée —, et ceux qui le lisent, comme Qwen3 et Qwen3.6,
        amorceront un `<think>` vide qui coupe le raisonnement à la racine.
        C'est préférable à ne le retirer qu'après coup : les jetons du brouillon
        sont facturés au budget de la réponse, et un `max_tokens` court se
        consomme entièrement en raisonnement sans rien produire.
        """
        tokenizer = self._tokenizer
        if tokenizer is None or not hasattr(tokenizer, "apply_chat_template"):
            raise WorkerError(
                "ce tokenizer n'a pas de gabarit de conversation : l'invite partirait "
                "sans balises de rôle et un modèle instruit répondrait n'importe quoi"
            )
        extra: dict[str, Any] = {} if thinking is None else {"enable_thinking": bool(thinking)}
        for déclaration in _formes_d_outils(tools):
            try:
                brut = tokenizer.apply_chat_template(
                    messages,
                    tools=déclaration,
                    add_generation_prompt=True,
                    tokenize=False,
                    **extra,
                )
                return _normaliser_invite(brut), True
            except Exception:  # noqa: BLE001 — gabarit sans support d'outils : on replie
                continue
        try:
            brut = tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=False, **extra
            )
        except Exception:  # noqa: BLE001 — gabarit qui refuse le drapeau : sans lui plutôt que rien
            if not extra:
                raise
            brut = tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=False
            )
        return _normaliser_invite(brut), False

    # --- réglages ------------------------------------------------------------

    def reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
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
        except Exception:  # noqa: BLE001 — une mesure ratée ne fait pas échouer un job
            return None

    def _versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for nom, module in (("mlx", "mlx.core"), ("mlx-lm", "mlx_lm")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


# Terminateurs de tour des gabarits courants. Le jeton de fin du tokenizer est
# ajouté à cette liste au moment de nettoyer : c'est lui qui varie d'un modèle à
# l'autre, les autres sont assez répandus pour valoir d'être connus d'avance.
MARQUEURS_DE_FIN = ("<|im_end|>", "<|endoftext|>", "<|eot_id|>", "</s>", "<end_of_turn>")


def _sans_marqueur_de_fin(texte: str, tokenizer: Any) -> str:
    """Retire le jeton de fin de tour que la détokenisation laisse dans le texte.

    Il n'a rien à faire dans une sortie : il pollue une traduction notée au
    caractère près, il casse un bloc de code qu'un appelant voudrait exécuter, et
    il se glisse dans le fichier de la Bibliothèque où il restera pour toujours.
    Rien n'échoue pour autant — c'est une faute silencieuse, du genre qui se
    découvre en relisant une sortie six mois plus tard.
    """
    marqueurs = list(MARQUEURS_DE_FIN)
    fin = getattr(tokenizer, "eos_token", None)
    if isinstance(fin, str) and fin:
        marqueurs.append(fin)

    dépouillé = texte.strip()
    encore = True
    while encore:
        encore = False
        for marqueur in marqueurs:
            if dépouillé.endswith(marqueur):
                dépouillé = dépouillé[: -len(marqueur)].rstrip()
                encore = True
    return dépouillé


def _formes_d_outils(tools: list[dict[str, Any]] | None) -> list[list[dict[str, Any]]]:
    """Les façons de déclarer des outils à un gabarit, de la plus simple à l'autre.

    Il n'y a pas de convention commune, et l'écart se paie cher. Qwen3 lit un
    outil plat — `{"name": …, "parameters": …}` ; Gemma 4 lit son gabarit avec
    `tool.function.name` et lève un `UndefinedError` sur la forme plate, ce qui
    faisait replier sur la description en message système. Le modèle appelait
    quand même le bon outil, mais `template_tools` rendait faux : on mesurait un
    repli là où l'appel natif était disponible, et la comparaison avec les autres
    modèles cessait de porter sur la même chose.

    Essayer les deux coûte un rendu de gabarit raté dans le pire cas — quelques
    millisecondes, une fois par job — et évite de conclure qu'un modèle ne sait
    pas faire ce qu'il fait.
    """
    if not tools:
        return []
    plats = [outil for outil in tools if "function" not in outil]
    enveloppés = [
        outil if "function" in outil else {"type": "function", "function": outil}
        for outil in tools
    ]
    return [tools, enveloppés] if plats else [tools]


def _normaliser_invite(brut: Any) -> Any:
    """Ce que `apply_chat_template` rend, ramené à ce que `stream_generate` accepte.

    Le format de retour dépend de la version de `transformers` : une chaîne, une
    liste de jetons, ou — depuis la 5.x — un `BatchEncoding` qui ressemble à un
    dictionnaire. Le passer tel quel donne « Invalid type BatchEncoding received
    in array initialization », un message qui ne parle ni du gabarit ni de la
    conversation et qui coûte cher à relier à sa cause.
    """
    if isinstance(brut, str):
        return brut
    entrées = getattr(brut, "input_ids", None)
    if entrées is None and isinstance(brut, dict):
        entrées = brut.get("input_ids")
    if entrées is not None:
        # Un lot d'une seule conversation : on rend la première séquence.
        premier = entrées[0] if len(entrées) and not isinstance(entrées[0], int) else entrées
        return [int(jeton) for jeton in premier]
    if isinstance(brut, (list, tuple)):
        return [int(jeton) for jeton in brut]
    raise WorkerError(
        f"gabarit de conversation d'un type inattendu ({type(brut).__name__}) : "
        "ni chaîne, ni liste de jetons"
    )


@dataclass
class Consigne:
    """Messages d'une conversation à un tour, système compris."""

    system: str | None = None
    user: str = ""
    extra: list[dict[str, Any]] = field(default_factory=list)

    def messages(self) -> list[dict[str, Any]]:
        composés: list[dict[str, Any]] = []
        if self.system and self.system.strip():
            composés.append({"role": "system", "content": self.system.strip()})
        composés.extend(self.extra)
        composés.append({"role": "user", "content": self.user})
        return composés


class MlxLmWorker(MlxLmBase):
    """Génération de texte : une requête, une réponse, rien de plus."""

    def annonce(self) -> dict[str, Any]:
        return {"languages": []}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        prompt = str(request.get("prompt") or "").strip()
        if not prompt:
            raise WorkerError("aucune requête : le champ `prompt` est vide")

        consigne = Consigne(system=self.reglage(request, "system", None), user=prompt)
        arrêts = self.reglage(request, "stop", None)

        progress(5, "préparation")
        réponse, _ = self.engendrer(
            consigne.messages(),
            progress=progress,
            max_tokens=int(self.reglage(request, "max_tokens", 1024)),
            temperature=float(self.reglage(request, "temperature", 0.7)),
            top_p=float(self.reglage(request, "top_p", 0.95)),
            repetition_penalty=float(self.reglage(request, "repetition_penalty", 1.0)),
            seed=request.seed,
        )

        texte, coupé = _couper(réponse.text, arrêts)
        if coupé:
            réponse.finish_reason = "stop_sequence"

        progress(92, "écriture")
        (request.output_dir / OUTPUT_TEXT).write_text(texte, encoding="utf-8")

        return InferResult(
            output={
                "text": OUTPUT_TEXT,
                "tokens_generated": réponse.generation_tokens,
                "finish_reason": réponse.finish_reason,
            },
            metrics={
                "characters": len(texte),
                "prompt_tokens": réponse.prompt_tokens,
                "generation_tokens": réponse.generation_tokens,
                "tokens_per_second": réponse.tokens_per_second,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )


def _couper(texte: str, arrêts: Any) -> tuple[str, bool]:
    """Tronque à la première séquence d'arrêt rencontrée.

    Fait ici plutôt que dans l'échantillonneur parce que le contrat déclare des
    chaînes, et qu'une chaîne ne correspond pas toujours à une frontière de
    jeton : couper au jeton laisserait passer les séquences à cheval sur deux.
    """
    if not isinstance(arrêts, (list, tuple)):
        return texte, False
    positions = [texte.find(str(a)) for a in arrêts if a and str(a) in texte]
    if not positions:
        return texte, False
    return texte[: min(positions)].rstrip(), True


if __name__ == "__main__":
    raise SystemExit(main(MlxLmWorker))
