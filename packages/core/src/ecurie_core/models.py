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
    "text-to-video",
    "image-to-video",
    "document-to-text",
    "image-to-mesh",
    "text-to-mesh",
    "audio-separation",
    "audio-denoise",
    "text-generation",
]

Status = Literal["active", "candidate", "deprecated", "retired"]
LicenseClass = Literal["permissive", "restricted", "research-only", "unknown"]
Tier = Literal["hot", "cold", "absent"]
Runtime = Literal[
    "mlx-lm", "mlx-audio", "mlx-vlm", "diffusers-mps", "comfy", "ollama", "llama-cpp", "custom"
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


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disk_bytes: int = Field(ge=0)
    peak_unified_memory_bytes: int = Field(ge=0)
    warmup_ms: int | None = Field(default=None, ge=0)
    latency_ms_p50: int | None = Field(default=None, ge=0)
    rtf: float | None = None
    throughput: str | None = None
    measured_on: str
    measured_at: date
    harness_version: str | None = None


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
