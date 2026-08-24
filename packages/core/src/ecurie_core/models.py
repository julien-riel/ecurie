"""Miroir pydantic de registry/schema/model.schema.json.

Le JSON Schema reste l'autorité : c'est lui que la CI et les agents de veille
lisent. Ces classes servent au code Python après validation par le schéma ; un
test de conformité garantit que les deux acceptent les mêmes documents.
"""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Capability = Literal[
    "text-to-speech",
    "speech-to-text",
    "text-to-music",
    "text-to-image",
    "image-to-image",
    "image-matting",
    "image-upscale",
    "image-to-text",
    "text-to-video",
    "image-to-video",
    "document-to-text",
    "image-to-mesh",
    "text-to-mesh",
    "audio-separation",
    "audio-denoise",
    "text-generation",
    "translation",
    "tool-use",
    "video-to-text",
    "video-to-motion",
    "depth-estimation",
    "image-inpaint",
    "image-detect",
    "image-segment",
    "audio-to-text",
    "speaker-diarization",
    "voice-clone",
    # La famille visage. Six contrats plutôt qu'un seul « analyse de visage » :
    # ils ne rendent ni la même chose ni le même type — des boîtes, des points,
    # une carte de régions, un vecteur, des angles —, et une capacité dont la
    # sortie change avec le modèle n'est plus un contrat.
    "face-detect",
    "face-landmark",
    "face-parse",
    "face-embed",
    "face-headpose",
    "face-gaze",
]

Status = Literal["active", "candidate", "deprecated", "retired"]
LicenseClass = Literal["permissive", "restricted", "research-only", "unknown"]
Tier = Literal["hot", "cold", "absent"]
Runtime = Literal[
    "mlx-lm",
    "mlx-audio",
    "mlx-vlm",
    "diffusers-mps",
    "torch-vision",
    "depth-anything",
    "mflux",
    "rtmlib",
    "uniface",
    "comfy",
    "ollama",
    "llama-cpp",
    "custom",
]
Quantization = Literal[
    "fp32",
    "bf16",
    "fp16",
    "8bit",
    "6bit",
    "4bit",
    "Q8_0",
    "Q6_K",
    "Q5_K_M",
    "Q4_K_M",
    "mixed",
    "none",
]


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["huggingface", "ollama", "url", "local"]
    repo: str | None = None
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    allow_patterns: list[str] | None = None
    url: str | None = None
    path: str | None = None


class PeakScaling(BaseModel):
    """Le pic de certains modèles dépend d'un paramètre d'entrée.

    MiniMax Music 3, mesuré : 13,2 Gio pour 15 s d'audio, 18,1 pour 20, 23,9 pour
    30 — environ 0,7 Gio par seconde. Avec un pic unique, il faut choisir entre
    inscrire le pire cas, et refuser des jobs courts qui passeraient largement,
    ou un cas favorable, et laisser une demande longue emporter la machine. Ni
    l'un ni l'autre n'est acceptable pour un contrôle d'admission.
    """

    model_config = ConfigDict(extra="forbid")

    parameter: str
    base_bytes: int = Field(ge=0)
    bytes_per_unit: float
    measured_range: list[float] = Field(min_length=2, max_length=2)
    r_squared: float | None = None

    def expected(self, valeur: float) -> int:
        return max(0, int(self.base_bytes + self.bytes_per_unit * valeur))

    def extrapolates(self, valeur: float) -> bool:
        """Vrai hors des bornes mesurées : la droite y est une conjecture."""
        bas, haut = min(self.measured_range), max(self.measured_range)
        return not (bas <= valeur <= haut)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disk_bytes: int = Field(ge=0)
    peak_unified_memory_bytes: int = Field(ge=0)
    peak_scaling: PeakScaling | None = None
    warmup_ms: int | None = Field(default=None, ge=0)
    latency_ms_p50: int | None = Field(default=None, ge=0)
    rtf: float | None = None
    throughput: str | None = None
    measured_on: str
    measured_at: date
    harness_version: str | None = None

    def expected_peak(self, values: dict[str, Any] | None = None) -> tuple[int, str | None]:
        """Pic attendu pour cette entrée, et ce qu'il faut en penser.

        Sans `peak_scaling`, ou sans le paramètre dans l'entrée, on rend le pic
        mesuré : c'est le pire cas de la charge type, donc le choix prudent.
        """
        mesuré = self.peak_unified_memory_bytes
        échelle = self.peak_scaling
        if échelle is None or not values:
            return mesuré, None
        brut = values.get(échelle.parameter)
        if not isinstance(brut, (int, float)) or isinstance(brut, bool):
            return mesuré, None

        valeur = float(brut)
        attendu = échelle.expected(valeur)
        if échelle.extrapolates(valeur):
            bas, haut = min(échelle.measured_range), max(échelle.measured_range)
            note = (
                f"{échelle.parameter}={valeur:g} sort de l'intervalle mesuré "
                f"[{bas:g}, {haut:g}] : le pic attendu de {attendu} octets est extrapolé"
            )
            # Hors intervalle, on ne descend jamais sous le pic mesuré : la
            # droite pourrait sous-estimer un coût qui n'est linéaire que dans
            # la plage éprouvée.
            return max(attendu, mesuré), note
        return attendu, None


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    quantization: Quantization | None = None
    tier: Tier = "absent"
    source: Source
    runtime: Runtime
    runtime_env: str | None = None
    entrypoint: str | None = None
    profile: Profile | None = None
    defaults: dict[str, Any] | None = None
    options: dict[str, Any] | None = None
    caveats: list[str] | None = None

    @property
    def env_name(self) -> str:
        """Nom de l'environnement isolé sous runtimes/ (défaut : le runtime)."""
        return self.runtime_env or self.runtime


class Links(BaseModel):
    model_config = ConfigDict(extra="forbid")

    homepage: str | None = None
    paper: str | None = None
    code: str | None = None


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str | None = None
    capability: Capability
    family: str | None = None
    vendor: str | None = None
    license: str
    license_class: LicenseClass | None = None
    status: Status
    incumbent: bool = False
    notes: str | None = None
    links: Links | None = None
    variants: list[Variant] = Field(min_length=1)

    def variant(self, variant_id: str) -> Variant:
        for v in self.variants:
            if v.id == variant_id:
                return v
        raise KeyError(f"{self.id}: variant inconnu {variant_id!r}")
