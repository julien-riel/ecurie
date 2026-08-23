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


class StorePlanResponse(BaseModel):
    """Le plan de récupération, **jamais écrit** — c'est toute la différence avec la CLI.

    `ecurie store plan` produit un fichier, parce que `ecurie store apply` en
    exige un : le plan est le document qu'on relit avant de laisser un outil
    toucher trente giga-octets. Un `GET` ne fabrique pas de document ; il montre
    ce qu'un plan dirait, et laisse la commande à taper pour l'obtenir pour de
    bon. `command` porte cette commande, parce que l'écran qui affiche un gain de
    quatre giga-octets doit dire par où on le prend.
    """

    scanned: bool
    last_scan_at: str | None = None
    stale: bool = False
    plan: Json | None = Field(
        default=None,
        description=(
            "Le plan tel que `ecurie_store.plan.generate_plan` le produit "
            "(CONCEPTION.md §4.3), ou `null` tant qu'aucun scan n'a eu lieu."
        ),
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Le libellé français de chaque motif d'action, tel que la CLI "
            "l'affiche — le front n'entretient pas sa propre table."
        ),
    )
    command: str | None = Field(
        default=None, description="La commande qui écrit ce plan et permet de l'appliquer."
    )
    hint: str | None = None


class TierVolumeOut(BaseModel):
    """Un volume de `tier_volumes`, et son état au moment de la requête."""

    path: str
    mounted: bool = Field(description="Le point de montage existe et est un dossier.")
    free_bytes: int | None = Field(
        default=None, description="`null` quand le volume est démonté — inconnu, pas zéro."
    )
    total_bytes: int | None = None


class ColdLinkOut(BaseModel):
    path: str
    target: str
    available: bool = Field(description="La cible du lien est atteignable — volume monté.")
    variant_ref: str | None = None


class VariantFootprintOut(BaseModel):
    ref: str
    files: int
    bytes: int
    freed_bytes: int = Field(
        description=(
            "Ce que le volume de départ rendrait vraiment : un inode dont une "
            "référence échappe au parc scanné ne libère rien."
        )
    )
    shared_with: list[str] = Field(default_factory=list)
    devices: list[int] = Field(default_factory=list)
    tiered_links: int = 0
    tierable: bool


class TieringResponse(BaseModel):
    """Ce que le tiering donne à voir : où déporter, ce qui l'est déjà, ce qui pèse.

    Une lecture, et rien d'autre. Déporter copie des giga-octets et met des
    originaux en quarantaine : `ecurie store tier` le fait, avec une
    confirmation, et l'écran donne la commande. Un bouton qui déclencherait cela
    depuis un navigateur mettrait une opération irréversible à un clic d'un
    survol de souris.
    """

    scanned: bool
    last_scan_at: str | None = None
    volumes: list[TierVolumeOut] = Field(default_factory=list)
    cold: list[ColdLinkOut] = Field(default_factory=list)
    variants: list[VariantFootprintOut] = Field(default_factory=list)
    hint: str | None = None


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


class JobRequest(BaseModel):
    ref: str = Field(description="« model@variant », ou « model » quand le choix est évident.")
    input: Json = Field(
        default_factory=dict,
        description=(
            "Entrée du job, déjà typée — un nombre pour un nombre. Les défauts du "
            "contrat et du variant sont résolus par le serveur, et écrits au manifeste."
        ),
    )
    seed: int | None = None


class JobOut(BaseModel):
    """L'état d'un job, tel que le flux d'événements le répète.

    Le même objet sert la lecture et chaque événement : le client remplace son
    état au lieu de recomposer des fragments, et deux chemins ne peuvent pas
    diverger.
    """

    id: str
    ref: str
    model: str
    variant: str
    capability: str
    state: Literal["queued", "running", "done", "failed"]
    submitted_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: int = 0
    note: str = Field(default="", description="Ce qui se passe maintenant, en clair.")
    error: str | None = None
    reused: bool = Field(
        default=False, description="Le modèle était déjà chaud : le warmup n'a pas été repayé."
    )
    evicted: list[str] = Field(
        default_factory=list, description="Ce qu'il a fallu décharger pour faire de la place."
    )
    warnings: list[str] = Field(default_factory=list)
    metrics: Json = Field(default_factory=dict)
    output: Json = Field(
        default_factory=dict,
        description=(
            "Ce que le worker a rendu, tel quel. `outputs` n'en retient que les "
            "fichiers ; il faut aussi ce qui n'en est pas — une langue détectée, un "
            "nombre de pages, une liste d'appels d'outils. Le client aplatit cette "
            "réponse-là, et non les `properties` du contrat : `audio-separation` "
            "déclare cinq pistes et n'en produit que deux ou quatre."
        ),
    )
    outputs: dict[str, str] = Field(
        default_factory=dict,
        description="Sortie du contrat → chemin du fichier dans le dossier du job.",
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Sortie du contrat → URL à télécharger. Composée par le serveur : une "
            "sortie imbriquée est un chemin à plusieurs segments, que le client n'a pas à savoir."
        ),
    )
    input: Json = Field(default_factory=dict, description="Entrée complète, défauts résolus.")
    seed: int | None = None


class JobDetail(JobOut):
    manifest: Json | None = Field(
        default=None,
        description=(
            "Manifeste du job — révision épinglée, paramètres complets, empreinte de "
            "l'entrée, versions, métriques. C'est le contrat de reproductibilité du §6, "
            "écrit dans le dossier du job. `null` tant que le job n'est pas terminé."
        ),
    )


class UploadOut(BaseModel):
    """Ce qu'un dépôt devient : un chemin, que le champ fichier du formulaire porte.

    `path` est absolu et local à la machine du serveur. C'est délibérément la
    même valeur qu'on saisirait à la main dans le champ, et la même que la CLI
    attend (`ecurie run -p image=…`) : le dépôt fabrique un chemin, il n'invente
    pas une seconde façon de désigner une entrée.
    """

    path: str = Field(description="Chemin absolu du fichier écrit, à poser dans le champ.")
    name: str = Field(description="Nom sur le disque : jeton horodaté et nom d'origine assaini.")
    media_type: str
    size_bytes: int


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
