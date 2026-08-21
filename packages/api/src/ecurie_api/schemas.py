"""Formes des réponses — le contrat que l'UI programme contre.

Ce ne sont pas des copies des modèles du registre : ce sont des **projections**.
Un `Variant` du registre dit ce qui est déclaré ; un `VariantOut` y ajoute ce que
le disque en dit (`ready`, `blockers`, `weights_path`) et ce que la politique
mémoire en fait (`heavy`), parce que c'est l'assemblage des trois qui permet à
l'Atelier de proposer un choix honnête. Les projeter explicitement plutôt que de
sérialiser les modèles internes a un second effet : le schéma OpenAPI reste
stable quand une clé purement interne apparaît au registre.

Une exception assumée : `store/summary` transporte `Figures` en dictionnaire
plutôt qu'en miroir pydantic. Ce bloc est calculé par `ecurie_store.figures`,
c'est lui l'autorité, et un miroir de sept dataclasses imbriquées finirait par
diverger sans qu'aucun test ne le voie — exactement le défaut que ce projet
signale ailleurs entre `measurements/` et le bloc `profile:` d'un manifeste.
"""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

Json = dict[str, Any]


# --- constats de validation du registre -----------------------------------------


class IssueOut(BaseModel):
    severity: Literal["error", "warning"]
    file: str
    message: str


# --- registre --------------------------------------------------------------------


class SourceOut(BaseModel):
    kind: str
    repo: str | None = None
    revision: str | None = None
    path: str | None = None
    url: str | None = None
    allow_patterns: list[str] | None = None


class PeakScalingOut(BaseModel):
    parameter: str
    base_bytes: int
    bytes_per_unit: float
    measured_range: list[float]
    r_squared: float | None = None


class ProfileOut(BaseModel):
    disk_bytes: int
    peak_unified_memory_bytes: int
    peak_scaling: PeakScalingOut | None = None
    warmup_ms: int | None = None
    latency_ms_p50: int | None = None
    rtf: float | None = None
    throughput: str | None = None
    measured_on: str
    measured_at: date
    harness_version: str | None = None


class VariantOut(BaseModel):
    ref: str = Field(description="« model@variant » — l'identifiant qu'attendent run et bench.")
    id: str
    quantization: str | None = None
    tier: str = Field(description="Présence déclarée au manifeste : hot, cold ou absent.")
    runtime: str
    env: str = Field(description="Environnement isolé sous runtimes/, runtime par défaut.")
    entrypoint: str | None = None
    source: SourceOut
    defaults: Json | None = None
    options: Json | None = None
    caveats: list[str] | None = None
    profile: ProfileOut | None = None
    ready: bool = Field(description="Exécutable en l'état : poids, environnement et profil.")
    blockers: list[str] = Field(
        default_factory=list,
        description="Ce qui manque, chaque cause portant la commande qui la répare.",
    )
    weights_path: str | None = Field(
        default=None, description="Chemin local des poids, quand ils sont là."
    )
    heavy: bool | None = Field(
        default=None,
        description=(
            "Pic au-dessus de heavy_threshold_bytes : ce variant ne cohabitera "
            "avec aucun autre lourd. Inconnu sans profil mesuré."
        ),
    )


class LinksOut(BaseModel):
    homepage: str | None = None
    paper: str | None = None
    code: str | None = None


class ModelOut(BaseModel):
    id: str
    name: str | None = None
    capability: str
    family: str | None = None
    vendor: str | None = None
    license: str
    license_class: str | None = None
    status: str
    incumbent: bool
    notes: str | None = None
    links: LinksOut | None = None
    variants: list[VariantOut]


class ModelsResponse(BaseModel):
    models: list[ModelOut]
    issues: list[IssueOut] = Field(
        default_factory=list,
        description="Constats du chargement du registre. Un manifeste invalide n'est pas servi.",
    )


class CapabilityOut(BaseModel):
    id: str
    title: str
    description: str | None = None
    composite: bool = False
    steps: list[Json] = Field(default_factory=list)
    input: Json = Field(description="JSON Schema du formulaire. L'UI le rend tel quel.")
    output: Json = Field(description="JSON Schema de la sortie.")
    output_media_types: dict[str, str] = Field(
        default_factory=dict,
        description="Sorties qui sont des fichiers → type de média, qui choisit le visualiseur.",
    )
    required: list[str] = Field(default_factory=list)
    defaults: Json = Field(
        default_factory=dict, description="Valeurs par défaut déclarées par le contrat."
    )
    models: list[str] = Field(
        default_factory=list, description="Modèles du registre qui remplissent cette capacité."
    )
    ready_variants: list[str] = Field(
        default_factory=list,
        description=(
            "Variants exécutables en l'état. Vide avec des modèles présents veut dire "
            "que la capacité est déclarée mais pas encore utilisable."
        ),
    )
    incumbent: str | None = Field(
        default=None, description="Titulaire de la capacité, référence de toute confrontation A/B."
    )


class CapabilitiesResponse(BaseModel):
    capabilities: list[CapabilityOut]
    issues: list[IssueOut] = Field(default_factory=list)


# --- parc disque ------------------------------------------------------------------


class TelemetryOut(BaseModel):
    conclusive: bool = Field(
        description="La télémétrie observe-t-elle depuis assez longtemps pour conclure ?"
    )
    first_run_at: str | None = None
    unused_after_days: int


class StoreSummaryResponse(BaseModel):
    scanned: bool
    last_scan_at: str | None = None
    stale: bool = Field(
        default=False,
        description="Un plan a été appliqué depuis le dernier scan : ces chiffres sont périmés.",
    )
    figures: Json | None = Field(
        default=None,
        description=(
            "Les trois chiffres et leur détail, tels que `ecurie_store.figures` les "
            "calcule. `null` — et non zéro — tant qu'aucun scan n'a eu lieu : "
            "un parc jamais regardé n'est pas un parc vide."
        ),
    )
    telemetry: TelemetryOut | None = None
    hint: str | None = Field(
        default=None, description="Ce qu'il faut faire quand la réponse est incomplète."
    )


# --- exécution ---------------------------------------------------------------------


class PolicyOut(BaseModel):
    budget_bytes: int
    max_heavy_resident: int
    heavy_threshold_bytes: int


class ResidentOut(BaseModel):
    ref: str
    pid: int
    peak_bytes: int
    heavy: bool
    runtime: str
    env: str
    loaded_at: str
    last_used: float
    pinned: bool
    busy: bool
    busy_by: int
    busy_since: float
    warmup_ms: int
    options: Json = Field(
        default_factory=dict,
        description="Ce que le worker a annoncé au chargement — voix, langues, versions.",
    )
    socket: str
    log: str


class StaleWorkerOut(BaseModel):
    """Une entrée du registre des résidents qui ne correspond plus à un worker joignable.

    Distinguer les deux cas est ce qui compte : un processus mort ne laisse
    qu'une ligne à balayer, un processus vivant dont le socket a disparu retient
    toujours ses gigaoctets et n'est plus comptable dans le budget.
    """

    ref: str
    pid: int
    holds_memory: bool
    socket: str


class AdmissionOut(BaseModel):
    ref: str
    admitted: bool
    reason: str
    evict: list[str] = Field(default_factory=list)
    headroom_bytes: int = 0
    already_resident: bool = False
    measure_mode: bool = False
    blockers: list[str] = Field(default_factory=list)
    peak_bytes: int | None = Field(
        default=None, description="Pic attendu pour cette entrée, pas seulement pour ce variant."
    )
    peak_note: str | None = Field(
        default=None, description="Dit quand le pic est extrapolé hors de l'intervalle mesuré."
    )


class ResidentsResponse(BaseModel):
    budget_bytes: int
    budget_source: str
    budget_measured: bool = Field(
        description="Le budget vient de Metal, et non d'une règle de trois sur la mémoire physique."
    )
    used_bytes: int
    free_bytes: int
    policy: PolicyOut
    residents: list[ResidentOut]
    stale: list[StaleWorkerOut] = Field(default_factory=list)
    admission: AdmissionOut | None = None


class AdmissionRequest(BaseModel):
    ref: str = Field(description="« model@variant », ou « model » quand le choix est évident.")
    input: Json = Field(
        default_factory=dict,
        description=(
            "Entrée envisagée, déjà typée. Elle sert au pic attendu quand le profil "
            "déclare une pente : trente secondes de musique coûtent le double de quinze."
        ),
    )
    seed: int | None = None


class AdmissionResponse(BaseModel):
    ref: str
    admission: AdmissionOut
    input: Json = Field(
        default_factory=dict,
        description="Entrée complète, défauts du contrat et du variant résolus.",
    )
    input_errors: list[str] = Field(
        default_factory=list,
        description="Ce que le contrat reproche à l'entrée. Non vide, aucun job ne partirait.",
    )
    ready: bool
    blockers: list[str] = Field(default_factory=list)
