"""Adaptateur mlx-audio, chemin **débruitage de la parole** (DeepFilterNet3).

Huitième emploi du runtime `mlx-audio`, et le seul dont le modèle ne tient pas
tout entier dans son fichier de poids. Le dépôt MLX publie le **réseau**
(2,1 M paramètres, 8 Mo) et son implémentation de référence `dfn3_mlx.py` ; il ne
publie pas le traitement du signal autour, qui reste « the caller's job ». C'est
donc ici, et c'est ce qui fait qu'un adaptateur de 300 lignes sert un modèle de
9 Mo.

Ce que ce module refait, à l'identique de la référence Rust `libDF`
(`transforms.rs`, `lib.rs`) :

- **analyse** — fenêtre glissante de 960 échantillons par pas de 480, fenêtre de
  Vorbis fournie par `auxiliary.npz`, rfft, puis facteur `wnorm = 2·hop/fenêtre²` ;
- **traits ERB** — puissance moyenne par bande (`|X|² @ erb_fb`, la matrice porte
  déjà le `1/largeur`), passage en dB, puis normalisation à moyenne glissante
  `s ← x(1−α) + sα`, `x ← (x−s)/40` ;
- **traits spectraux** — les 96 premiers bins, normalisés en amplitude
  glissante : `s ← |x|(1−α) + sα`, `x ← x/√s` ;
- **application** — le masque ERB (32 gains) est répliqué sur les 481 bins par
  `erb_inv_fb`, puis le filtre profond d'ordre 5 **remplace** les 96 premiers
  bins : `y[t,f] = Σᵢ coefs[i,t,f]·X[t−2+i,f]`, deux trames de retard et deux
  d'anticipation ;
- **synthèse** — irfft non normalisée, fenêtre, addition-recouvrement.

`α = 0.99` : `exp(−hop/(τ·sr))` arrondi à trois décimales, comme
`df.utils.get_norm_alpha`. Les états initiaux des deux normalisations viennent du
`.npz` — les reconstruire par interpolation linéaire, comme le fait la référence
faute d'état, donnerait un premier dixième de seconde différent.

**Cet adaptateur refuse de servir tant que sa qualité n'est pas prouvée, et
voici pourquoi.** Le 24 août 2026, la chaîne ci-dessus a été mesurée sur un
mélange fabriqué (voix de `parole-tts.wav` + bruit blanc, rapport imposé) :

- analyse → synthèse sans réseau : **SI-SDR 139 dB**, écart maximal 2,4·10⁻⁷ —
  la STFT, la fenêtre, l'échelle et l'addition-recouvrement sont exactes ;
- masque ERB seul : 5,00 dB en entrée → **4,86 dB** en sortie, soit un étage qui
  ne fait presque rien ;
- masque + filtre profond : **−4,75 dB**, une dégradation franche ;
- le réseau juge tout comme du bruit — LSNR estimé à −2,35 dB sur une voix
  propre, là où il devrait être largement positif.

Ni l'échelle des traits (quatre conventions essayées), ni l'alignement temporel
des sorties (cinq décalages, avec et sans le décalage interne du modèle) ne
redressent le résultat : le meilleur essai reste sous l'entrée. Le dépôt MLX
publie le réseau **et rien d'autre** — sa propre table de qualité a été obtenue
en branchant ce réseau dans le pipeline PyTorch amont, pas dans une
réimplémentation. Trancher demande la référence exécutable `libdf`, dont la roue
PyPI se compile depuis Rust, absent de cette machine.

Ce qui reste à faire, dans l'ordre : installer une chaîne Rust, `pip install
deepfilterlib`, puis comparer trait par trait `analyser`, `traits_erb` et
`traits_spectre` à `DF.analysis`, `erb`/`erb_norm` et `unit_norm`. L'écart se
verra à la première trame. En attendant, un variant peut lever le refus par
`options: {dsp_non_valide: true}` à son manifeste — c'est ce qui permet de
mesurer sans livrer un débruiteur qui abîme la voix qu'on lui confie.

**Deux paramètres du contrat que ce chemin n'honore pas comme on l'imagine.**
`segment_seconds` ne découpe rien : le réseau est récurrent et le dépôt MLX
n'expose aucun état de GRU à reporter d'un segment au suivant, si bien qu'une
découpe s'entendrait à chaque raccord. Le pic mémoire suit donc la durée de
l'entrée, ce que le profil mesuré doit porter par `peak_scaling`. `sample_rate`
ne change pas le traitement — tout entre et sort du réseau à 48 kHz — mais la
fréquence du fichier écrit, par rééchantillonnage après coup.
"""

import gc
import importlib.util
import math
import sys
import time
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

ENV_NAME = "mlx-audio"
REPAIR = f"ecurie env sync {ENV_NAME}"
OUTPUT_NAME = "audio.wav"

# Le réseau ne connaît que cette fréquence. Le contrat expose `sample_rate` pour
# le fichier produit, pas pour le traitement (caveat du manifeste).
SAMPLE_RATE_MODELE = 48_000

# Ce que le contrat déclare et que le fichier de sortie peut porter.
SAMPLE_RATES_SORTIE = (16_000, 24_000, 44_100, 48_000)

# Le refus décrit en tête de module. Il tient en une option de variant : le jour
# où la comparaison à `libdf` passe, la ligne à retirer est celle-ci et rien
# d'autre — le traitement, lui, est écrit et mesuré.
OPTION_FORCER = "dsp_non_valide"
REFUS_DSP = (
    "débruitage non livré : le traitement du signal de cet adaptateur reproduit "
    "la STFT de la référence à 139 dB de SI-SDR, mais la chaîne complète DÉGRADE "
    "le signal (5,00 dB en entrée → −4,75 dB en sortie sur un mélange mesuré, "
    "24 août 2026). Le dépôt MLX publie le réseau sans son DSP, et la comparaison "
    "à la référence `libdf` demande une chaîne Rust absente de cette machine. "
    "Pour mesurer quand même : ajouter `options: {" + OPTION_FORCER + ": true}` "
    "au variant. Voir l'en-tête de workers/dfn3_denoise.py."
)

# `df.utils.get_norm_alpha` : exp(-hop/(tau*sr)) arrondi à trois décimales.
# Le calcul est refait ici plutôt que la valeur écrite en dur, pour qu'un
# checkpoint à autre `norm_tau` reste juste.
def norm_alpha(hop: int, sample_rate: int, tau: float) -> float:
    """Facteur de décroissance des deux normalisations glissantes."""
    brut = math.exp(-(hop / sample_rate) / tau)
    précision = 3
    alpha = 1.0
    while alpha >= 1.0:
        alpha = round(brut, précision)
        précision += 1
    return alpha


@dataclass(frozen=True)
class Demande:
    """Ce qui a été demandé, résolu, et ce qui n'a pas pu l'être."""

    strength: float
    sample_rate: int
    warnings: tuple[str, ...] = ()


def plan_denoise(
    *,
    entree: Mapping[str, Any],
    params: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> Demande:
    """Traduit une demande du protocole en réglages de débruitage.

    Fonction pure, sans MLX : priorité des trois couches, bornes du contrat, et
    le sort de `segment_seconds`, que ce chemin ne peut pas honorer.
    """
    couches = (entree, params, defaults)

    force = _reglage("strength", *couches)
    force = 1.0 if force is None else float(force)
    if not 0.0 <= force <= 1.0:
        raise WorkerError(f"strength = {force} : le contrat borne ce réglage à [0, 1]")

    fréquence = _reglage("sample_rate", *couches)
    fréquence = SAMPLE_RATE_MODELE if fréquence is None else int(fréquence)
    if fréquence not in SAMPLE_RATES_SORTIE:
        raise WorkerError(
            f"sample_rate = {fréquence} : le contrat n'accepte que "
            f"{', '.join(str(f) for f in SAMPLE_RATES_SORTIE)}"
        )

    avertissements = []
    segment = _reglage("segment_seconds", *couches)
    if segment is not None:
        avertissements.append(
            f"segment_seconds = {segment} ignoré : DeepFilterNet3 est récurrent et le "
            "portage MLX n'expose pas l'état de ses GRU — découper s'entendrait à "
            "chaque raccord. L'enregistrement est traité d'un seul tenant, et le pic "
            "mémoire suit sa durée."
        )
    if force < 1.0:
        avertissements.append(
            f"strength = {force} : fondu de l'adaptateur entre l'original et le "
            "résultat, pas un réglage du réseau (caveat du manifeste)"
        )

    return Demande(strength=force, sample_rate=fréquence, warnings=tuple(avertissements))


def _reglage(nom: str, *couches: Mapping[str, Any]) -> Any:
    """Première valeur définie, de la plus prioritaire à la moins : entrée, job, manifeste."""
    for couche in couches:
        valeur = couche.get(nom)
        if valeur is not None:
            return valeur
    return None


# --- traitement du signal (numpy, sans MLX) ----------------------------------


def analyser(signal: Any, window: Any, hop: int, np: Any) -> Any:
    """Signal mono → spectrogramme complexe [T, F], échelle de la référence.

    La trame `t` porte les 480 échantillons nouveaux **et** les 480 précédents :
    c'est la fenêtre glissante de `frame_analysis`, dont la mémoire d'analyse
    part à zéro. Le facteur `wnorm` est celui de la référence — la synthèse ne
    normalisant pas, c'est lui qui fixe l'échelle des traits, donc ce que le
    réseau voit.
    """
    taille = int(window.shape[0])
    if taille != 2 * hop:
        raise WorkerError(
            f"fenêtre de {taille} pour un pas de {hop} : cette chaîne suppose un "
            "recouvrement de moitié, comme DeepFilterNet3"
        )
    reste = (-len(signal)) % hop
    rembourré = np.concatenate(
        [np.zeros(hop, dtype=np.float32), signal, np.zeros(reste + hop, dtype=np.float32)]
    )
    nombre = len(rembourré) // hop - 1
    indices = np.arange(nombre)[:, None] * hop + np.arange(taille)[None, :]
    trames = rembourré[indices] * window[None, :]
    wnorm = 2.0 * hop / (taille * taille)
    return np.fft.rfft(trames, axis=-1).astype(np.complex64) * np.float32(wnorm)


def synthetiser(spectre: Any, window: Any, hop: int, np: Any) -> Any:
    """Spectrogramme → signal, par fenêtre et addition-recouvrement.

    `np.fft.irfft` divise par la taille de transformée là où la référence Rust
    ne divise pas : le facteur est rendu ici, sans quoi la sortie serait 960 fois
    trop faible — et le fondu de `strength` mélangerait deux signaux d'échelles
    différentes.
    """
    taille = int(window.shape[0])
    trames = np.fft.irfft(spectre, n=taille, axis=-1).astype(np.float32) * np.float32(taille)
    trames *= window[None, :]
    sortie = np.zeros((trames.shape[0] + 1) * hop, dtype=np.float32)
    for index, trame in enumerate(trames):
        début = index * hop
        sortie[début : début + taille] += trame
    # La première trame d'analyse ne portait que du silence : la sortie commence
    # un pas plus tard que l'entrée, et le rendre aligné est ce qui permet de
    # comparer l'avant et l'après échantillon par échantillon.
    return sortie[hop:]


def traits_erb(spectre: Any, erb_fb: Any, etat: Any, alpha: float, np: Any) -> Any:
    """Puissance par bande, en dB, à moyenne glissante retirée. [T, 32]"""
    puissance = (spectre.real**2 + spectre.imag**2) @ erb_fb
    db = 10.0 * np.log10(puissance + 1e-10)
    return _moyenne_glissante(db, etat.astype(np.float32).copy(), alpha, np) / 40.0


def traits_spectre(spectre: Any, etat: Any, alpha: float, np: Any) -> Any:
    """Les 96 premiers bins, normalisés par amplitude glissante. [T, 96] complexe"""
    bins = spectre[:, : etat.shape[-1]]
    amplitudes = np.abs(bins)
    état = etat.reshape(-1).astype(np.float32).copy()
    sortie = np.empty_like(bins)
    for t in range(bins.shape[0]):
        état = amplitudes[t] * (1.0 - alpha) + état * alpha
        sortie[t] = bins[t] / np.sqrt(état)
    return sortie


def _moyenne_glissante(valeurs: Any, etat: Any, alpha: float, np: Any) -> Any:
    """`s ← x(1−α) + sα ; x ← x − s`, trame après trame — la récurrence de libDF."""
    sortie = np.empty_like(valeurs)
    for t in range(valeurs.shape[0]):
        etat = valeurs[t] * (1.0 - alpha) + etat * alpha
        sortie[t] = valeurs[t] - etat
    return sortie


def filtre_profond(spectre: Any, coefs: Any, ordre: int, lookahead: int, np: Any) -> Any:
    """Filtre profond d'ordre `ordre` sur les bins que couvrent les coefficients.

    `y[t,f] = Σᵢ coefs[i,t,f] · X[t − (ordre−1−lookahead) + i, f]` — le
    rembourrage de la référence (`MultiFrameModule`) place `ordre−1−lookahead`
    trames avant et `lookahead` après, si bien que l'ordre 5 avec 2 d'anticipation
    lit deux trames de passé, la courante, et deux de futur.
    """
    bins = coefs.shape[-1] if coefs.ndim == 3 else coefs.shape[2]
    avant = ordre - 1 - lookahead
    rembourré = np.pad(spectre[:, :bins], ((avant, lookahead), (0, 0)))
    sortie = np.zeros((spectre.shape[0], bins), dtype=np.complex64)
    for i in range(ordre):
        sortie += coefs[i] * rembourré[i : i + spectre.shape[0]]
    return sortie


# --- worker ------------------------------------------------------------------


class Dfn3DenoiseWorker(Worker):
    """Débruitage de la parole : un wav bruité en entrée, un wav propre en sortie."""

    name = "dfn3-denoise"

    def __init__(self) -> None:
        self._mx: Any = None
        self._np: Any = None
        self._model: Any = None
        self._aux: dict[str, Any] = {}
        self._cfg: dict[str, Any] = {}
        self._alpha = 0.99
        self._defaults: dict[str, Any] = {}
        self._peak_load = 0
        self._warned: set[str] = set()

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        # Le refus vient avant le chargement : rien ne sert de payer un warmup
        # pour un job qu'on ne laissera pas écrire son fichier.
        if not (variant.get("options") or {}).get(OPTION_FORCER):
            raise WorkerError(REFUS_DSP)

        mx, np = _import_runtime()
        chemin = Path(str(variant.get("weights_path") or ""))
        if not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                "déjà vérifié, un worker ne télécharge jamais"
            )

        # `dfn3_mlx.py` fait partie des poids, pas de l'environnement : le dépôt
        # publie le réseau ET son implémentation de référence, et le manifeste la
        # télécharge à la révision épinglée. On la charge depuis le dossier des
        # poids plutôt que de la copier ici, pour que le code exécuté soit
        # toujours celui de la révision mesurée.
        module = _charger_reference(chemin)
        self._model = module.DFN3MLX(str(chemin))
        self._cfg = dict(self._model.cfg)

        aux = np.load(chemin / "auxiliary.npz")
        manquants = {"erb_fb", "erb_inv_fb", "window", "mean_norm_state", "unit_norm_state"} - set(
            aux.files
        )
        if manquants:
            raise WorkerError(
                f"auxiliary.npz incomplet : il manque {', '.join(sorted(manquants))} — "
                "ces constantes DSP ne se devinent pas, retélécharger le variant"
            )
        self._aux = {clé: np.asarray(aux[clé], dtype=np.float32) for clé in aux.files}
        self._mx, self._np = mx, np
        self._defaults = dict(variant.get("defaults") or {})
        self._alpha = norm_alpha(
            int(self._cfg["hop_size"]),
            int(self._cfg["sample_rate"]),
            float(self._cfg.get("norm_tau", 1.0)),
        )
        self._peak_load = self._pic_mlx() or 0

        return {
            "sample_rate": int(self._cfg["sample_rate"]),
            "output_sample_rates": list(SAMPLE_RATES_SORTIE),
            "norm_alpha": self._alpha,
            "df_order": int(self._cfg["df_order"]),
            "df_lookahead": int(self._cfg["df_lookahead"]),
            "versions": self._versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._model is None or self._np is None:
            raise WorkerError("modèle non chargé")
        np = self._np

        plan = plan_denoise(
            entree=request.input, params=request.params, defaults=self._defaults
        )
        for message in plan.warnings:
            self._avertir(message)

        progress(5, "lecture de l'entrée")
        bruité = self._lire(self._chemin_entree(request))
        secondes = len(bruité) / SAMPLE_RATE_MODELE

        hop = int(self._cfg["hop_size"])
        window = self._aux["window"]
        self._mx.reset_peak_memory()

        progress(15, "analyse")
        début = time.monotonic()
        spectre = analyser(bruité, window, hop, np)
        feat_erb = traits_erb(spectre, self._aux["erb_fb"], self._aux["mean_norm_state"],
                              self._alpha, np)
        feat_spec = traits_spectre(spectre, self._aux["unit_norm_state"], self._alpha, np)

        progress(35, f"réseau ({spectre.shape[0]} trames)")
        masque, coefs, lsnr = self._reseau(feat_erb, feat_spec)

        progress(75, "application du filtre")
        propre = self._appliquer(spectre, masque, coefs, np)
        signal = synthetiser(propre, window, hop, np)[: len(bruité)]
        calcul = time.monotonic() - début

        if plan.strength < 1.0:
            # Fondu de l'adaptateur, pas un réglage du réseau : à 0, on rend
            # exactement l'entrée, ce que le contrat décrit comme « aucun effet ».
            signal = plan.strength * signal + (1.0 - plan.strength) * bruité

        progress(90, "écriture du wav")
        signal, fréquence = self._reechantillonner(signal, plan.sample_rate)
        chemin = request.output_dir / OUTPUT_NAME
        self._ecrire(chemin, signal, fréquence)

        return InferResult(
            output={"audio": OUTPUT_NAME},
            metrics=self._metriques(plan, secondes, calcul, spectre, lsnr, fréquence),
        )

    def unload(self) -> None:
        self._model = None
        self._aux = {}
        self._peak_load = 0
        # L'ordre compte : tant qu'une référence Python tient les tableaux, leurs
        # buffers ne sont que « cachés » et `clear_cache` ne rend rien au système.
        gc.collect()
        if self._mx is not None:
            self._mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        """Pic MLX, avec le poids résident pour plancher — même règle que la musique."""
        pic = self._pic_mlx()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- détails -------------------------------------------------------------

    def _reseau(self, feat_erb: Any, feat_spec: Any) -> tuple[Any, Any, Any]:
        """Un passage du réseau. Les formes sont celles du contrat de tenseurs du dépôt."""
        mx, np = self._mx, self._np
        erb = mx.array(feat_erb.astype(np.float32)[None, :, :, None])
        spec = mx.array(
            np.stack([feat_spec.real, feat_spec.imag], axis=-1).astype(np.float32)[None]
        )
        try:
            masque, coefs, lsnr = self._model(erb, spec)
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(f"réseau en échec : {type(exc).__name__}: {exc}") from exc
        return (
            np.asarray(masque)[0, :, :, 0],
            np.asarray(coefs)[0],
            np.asarray(lsnr)[0, :, 0],
        )

    def _appliquer(self, spectre: Any, masque: Any, coefs: Any, np: Any) -> Any:
        """Masque ERB partout, filtre profond sur les bins qu'il couvre.

        L'ordre est celui de la référence : le filtre profond travaille sur le
        spectre **non masqué** et remplace les premiers bins ; au-delà, ce sont
        les gains ERB qui font la sortie.
        """
        gains = masque @ self._aux["erb_inv_fb"]
        masqué = spectre * gains.astype(np.float32)
        complexes = coefs[..., 0] + 1j * coefs[..., 1]
        filtré = filtre_profond(
            spectre,
            complexes.astype(np.complex64),
            int(self._cfg["df_order"]),
            int(self._cfg["df_lookahead"]),
            np,
        )
        sortie = masqué.copy()
        sortie[:, : filtré.shape[1]] = filtré
        return sortie

    def _chemin_entree(self, request: InferRequest) -> Path:
        brut = request.get("audio")
        if not brut:
            raise WorkerError("aucun fichier : le contrat audio-denoise exige `audio`")
        chemin = Path(str(brut))
        if not chemin.is_absolute():
            # Le superviseur copie l'entrée dans le dossier du job et transmet un
            # chemin relatif à ce dossier — c'est ce qui rend un job rejouable.
            chemin = request.output_dir / chemin
        if not chemin.is_file():
            raise WorkerError(f"fichier d'entrée introuvable : {chemin}")
        return chemin

    def _lire(self, chemin: Path) -> Any:
        """Lit l'entrée en mono 48 kHz — la seule fréquence que le réseau connaît."""
        from mlx_audio.stt.utils import load_audio

        try:
            audio = load_audio(str(chemin), sr=SAMPLE_RATE_MODELE)
        except Exception as exc:  # noqa: BLE001 — fichier illisible : le dire avec son chemin
            raise WorkerError(f"lecture impossible de {chemin} : {exc}") from exc
        signal = self._np.asarray(audio, dtype=self._np.float32).reshape(-1)
        if signal.size == 0:
            raise WorkerError(f"enregistrement vide : {chemin}")
        return signal

    def _reechantillonner(self, signal: Any, cible: int) -> tuple[Any, int]:
        """Ramène la sortie à la fréquence demandée pour le fichier.

        Le réseau a travaillé à 48 kHz quoi qu'il arrive ; ceci ne concerne que
        ce qui est écrit. `resample_poly` filtre avant de décimer — une décimation
        nue replierait tout ce qui est au-dessus de la nouvelle moitié de bande
        dans la voix.
        """
        if cible == SAMPLE_RATE_MODELE:
            return signal, cible
        from math import gcd

        from scipy.signal import resample_poly

        diviseur = gcd(cible, SAMPLE_RATE_MODELE)
        rééchantillonné = resample_poly(
            signal, cible // diviseur, SAMPLE_RATE_MODELE // diviseur
        )
        return self._np.asarray(rééchantillonné, dtype=self._np.float32), cible

    def _ecrire(self, chemin: Path, signal: Any, fréquence: int) -> None:
        from mlx_audio.audio_io import write

        # Le réseau peut dépasser [-1, 1] sur une attaque : écrêter à l'écriture
        # vaut mieux que laisser miniaudio replier la valeur, ce qui s'entend
        # comme un claquement.
        borné = self._np.clip(signal, -1.0, 1.0).astype(self._np.float32)
        write(str(chemin), borné, fréquence, format="wav")

    def _metriques(
        self,
        plan: Demande,
        secondes: float,
        calcul: float,
        spectre: Any,
        lsnr: Any,
        fréquence: int,
    ) -> dict[str, Any]:
        np = self._np
        métriques: dict[str, Any] = {
            "audio_seconds": round(secondes, 3),
            "frames": int(spectre.shape[0]),
            "strength": plan.strength,
            "sample_rate": fréquence,
            "resampled": fréquence != SAMPLE_RATE_MODELE,
            "processing_sample_rate": SAMPLE_RATE_MODELE,
            "peak_memory_bytes": self.peak_memory_bytes(),
        }
        if secondes > 0:
            # Convention d'Écurie : temps de calcul par seconde traitée.
            métriques["rtf"] = round(calcul / secondes, 4)
        if lsnr is not None and len(lsnr):
            # Le réseau estime le rapport signal/bruit local ; c'est la seule
            # chose qu'il dise de ce qu'il a entendu, et elle vaut d'être lue :
            # un enregistrement déjà propre le montre par un LSNR haut.
            métriques["lsnr_db_mean"] = round(float(np.mean(lsnr)), 2)
            métriques["lsnr_db_min"] = round(float(np.min(lsnr)), 2)
        if plan.warnings:
            métriques["warnings"] = list(plan.warnings)
        return métriques

    def _pic_mlx(self) -> int | None:
        if self._mx is None:
            return None
        try:
            return int(self._mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne doit pas faire échouer un job
            return None

    def _versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for nom, module in (("mlx", "mlx.core"), ("mlx-audio", "mlx_audio")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        versions["dsp"] = "libDF transforms.rs (réimplémenté)"
        return versions

    def _avertir(self, message: str) -> None:
        """Une fois par worker et par sujet : répété à chaque job, un avertissement
        devient du bruit et cesse d'être lu."""
        if message in self._warned:
            return
        self._warned.add(message)
        print(f"[{self.name}] {message}", file=sys.stderr, flush=True)


def _import_runtime() -> tuple[Any, Any]:
    """Importe MLX et numpy, ou explique comment réparer l'environnement."""
    try:
        import mlx.core as mx
        import numpy as np
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-audio indisponible dans cet environnement ({exc}) — `{REPAIR}`"
        ) from exc
    return mx, np


def _charger_reference(chemin: Path) -> Any:
    """Charge `dfn3_mlx.py` depuis le dossier des poids, sans polluer `sys.path`.

    Le README du dépôt fait `sys.path.append(model_dir)` ; un worker qui sert
    plusieurs variants successifs y gagnerait un chemin de plus à chaque
    chargement, et le premier `dfn3_mlx` importé masquerait les suivants.
    """
    fichier = chemin / "dfn3_mlx.py"
    if not fichier.is_file():
        raise WorkerError(
            f"{fichier} absent : l'implémentation de référence fait partie des poids "
            "de ce variant — la retélécharger avec `ecurie pull deepfilternet3-mlx@fp32-mlx`"
        )
    spec = importlib.util.spec_from_file_location(f"dfn3_mlx_{chemin.name}", fichier)
    if spec is None or spec.loader is None:
        raise WorkerError(f"{fichier} illisible comme module Python")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — le fichier vient des poids, pas de l'env
        raise WorkerError(
            f"chargement de {fichier} impossible : {type(exc).__name__}: {exc}"
        ) from exc
    return module


if __name__ == "__main__":
    raise SystemExit(main(Dfn3DenoiseWorker))
