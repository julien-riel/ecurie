"""Banc d'essai : le seul endroit qui a le droit d'écrire un profil.

« `profile` est rempli par le banc d'essai, pas à la main. Un profil estimé est
un profil faux, et le contrôle d'admission mémoire en dépend » (ARCHITECTURE.md
§3). Ce module est l'application de cette phrase.

Déroulé (CONCEPTION.md §8) : le parc est **entièrement déchargé**, le worker est
lancé en mode mesure, on relève le warmup, le pic mémoire, l'occupation disque et
le débit sur la charge type de la capacité — trois entrées fixes, versionnées
avec les golden sets, pour que deux mesures prises à six mois d'écart soient
comparables.

Le fichier de `registry/measurements/` est l'autorité ; le bloc `profile:` du
manifeste en est la copie, que l'outil affiche mais ne commet pas. C'est cohérent
avec la règle du projet : toute évolution du parc passe par Git et par un humain.
"""

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecurie_core.capabilities import CapabilityContract
from ecurie_core.machine import describe_machine, hardware_of, machine_slug
from ecurie_core.models import Model, Variant
from ecurie_store.db import LocationRecord
from ecurie_store.weights import resolve_weights, variant_disk_bytes

from ecurie_runtime import __version__ as HARNESS_VERSION
from ecurie_runtime.runner import ResolvedInput, resolve_input
from ecurie_runtime.supervisor import Supervisor
from ecurie_runtime.worker import ProgressFn

WORKLOADS_DIR = Path("registry/evals/bench")
MEASUREMENTS_DIR = Path("registry/measurements")


@dataclass
class BenchCase:
    id: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Workload:
    capability: str
    version: int
    cases: list[BenchCase]
    source: str  # fichier d'origine, ou « déduit du contrat »
    base_dir: Path | None = None  # racine des chemins de fichiers de la charge
    # Paramètre que la charge fait varier exprès pour mesurer une pente de pic
    # mémoire. Déclaré par la charge et non deviné : corréler au hasard deux
    # colonnes de trois points donnerait une pente à chaque fois.
    scaling_parameter: str | None = None


@dataclass
class CaseResult:
    id: str
    ok: bool
    duration_ms: int
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class BenchReport:
    ref: str
    model: str
    variant: str
    capability: str
    profile: dict[str, Any]
    cases: list[CaseResult]
    workload: dict[str, Any]
    measured_on: str
    measured_at: str
    harness_version: str = HARNESS_VERSION
    budget_bytes: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def machine_slug(self) -> str:
        """Le nom de fichier de ce relevé sous `measurements/<ref>/`."""
        return machine_slug(hardware_of(self.measured_on))

    @property
    def ok(self) -> bool:
        return bool(self.cases) and all(c.ok for c in self.cases)

    def document(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "model": self.model,
            "variant": self.variant,
            "capability": self.capability,
            "harness_version": self.harness_version,
            "measured_on": self.measured_on,
            "measured_at": self.measured_at,
            "budget_bytes": self.budget_bytes,
            "workload": self.workload,
            "profile": self.profile,
            "cases": [asdict(c) for c in self.cases],
            "warnings": self.warnings,
        }


# --- charge type -------------------------------------------------------------


def load_workload(root: Path, contract: CapabilityContract) -> Workload:
    """Trois entrées fixes par capacité. À défaut, une charge déduite du contrat.

    Le fichier versionné est ce qui rend deux mesures comparables dans le temps.
    Quand il manque, on mesure quand même — un profil approximatif vaut mieux
    qu'un variant inexécutable — mais la charge utilisée est nommée dans le
    fichier de mesure, pour qu'on sache ce qu'on compare.
    """
    fichier = root / WORKLOADS_DIR / f"{contract.id}.json"
    if fichier.is_file():
        document = json.loads(fichier.read_text())
        cases = [
            BenchCase(id=str(c.get("id") or f"cas-{i}"), input=c.get("input") or {})
            for i, c in enumerate(document.get("cases") or [])
        ]
        if cases:
            return Workload(
                capability=contract.id,
                version=int(document.get("version") or 1),
                cases=cases,
                source=str(WORKLOADS_DIR / f"{contract.id}.json"),
                base_dir=fichier.parent,
                scaling_parameter=document.get("scaling_parameter"),
            )
    return default_workload(contract)


def default_workload(contract: CapabilityContract) -> Workload:
    """Une entrée minimale construite depuis les champs requis du contrat."""
    entrée: dict[str, Any] = {}
    for nom in contract.required:
        champ = contract.input_properties.get(nom) or {}
        if "default" in champ:
            entrée[nom] = champ["default"]
        elif champ.get("type") == "string":
            entrée[nom] = "Écurie : mesure de référence du banc d'essai."
        elif champ.get("type") in ("integer", "number"):
            entrée[nom] = champ.get("minimum", 1)
        elif champ.get("type") == "boolean":
            entrée[nom] = False
    return Workload(
        capability=contract.id,
        version=0,
        cases=[BenchCase(id="défaut", input=entrée)],
        source="déduit du contrat — aucune charge type versionnée",
    )


# --- exécution ---------------------------------------------------------------


def run_bench(
    supervisor: Supervisor,
    model: Model,
    variant: Variant,
    contract: CapabilityContract,
    *,
    records: list[LocationRecord] | None = None,
    outputs_dir: Path | None = None,
    on_progress: ProgressFn | None = None,
    workload: Workload | None = None,
) -> BenchReport:
    """Mesure un variant, parc vidé, et rend un profil prêt à committer."""
    ref = f"{model.id}@{variant.id}"
    root = supervisor.repo_root
    charge = workload or load_workload(root, contract)
    avertissements: list[str] = []
    if charge.version == 0:
        avertissements.append(
            f"aucune charge type versionnée pour {contract.id} — mesure non comparable "
            f"à d'autres ; créer {WORKLOADS_DIR}/{contract.id}.json"
        )

    # Le mode mesure décharge tout, y compris les résidents épinglés : mesurer un
    # modèle avec d'autres en mémoire ne mesure pas le modèle, mais la machine.
    lease = supervisor.acquire(model, variant, measure=True, on_progress=on_progress)
    résultats: list[CaseResult] = []
    dossier = (outputs_dir or supervisor.config.outputs_dir) / f"bench-{ref.replace('@', '-')}"
    try:
        for index, cas in enumerate(charge.cases):
            job_dir = dossier / cas.id
            job_dir.mkdir(parents=True, exist_ok=True)
            try:
                résolu = _resolve_case(contract, variant, cas, charge.base_dir)
                début = datetime.now(UTC)
                résultat = lease.session.infer(
                    f"bench-{index}",
                    input=résolu.values,
                    params=résolu.params,
                    output_dir=job_dir,
                    seed=résolu.values.get("seed"),
                )
                durée = int((datetime.now(UTC) - début).total_seconds() * 1000)
                résultats.append(
                    CaseResult(
                        id=cas.id,
                        ok=True,
                        duration_ms=int(résultat.metrics.get("duration_ms") or durée),
                        metrics=résultat.metrics,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — un cas raté n'annule pas la mesure des autres
                résultats.append(
                    CaseResult(
                        id=cas.id, ok=False, duration_ms=0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        pic = _peak_bytes(lease, résultats)
        warmup = lease.loaded.warmup_ms if lease.loaded else 0
    finally:
        lease.release()

    disque = _disk_bytes(supervisor, variant, ref, records)
    échelle = fit_peak_scaling(charge, résultats)
    if échelle is not None and échelle.get("r_squared", 1.0) < 0.9:
        avertissements.append(
            f"pente du pic mal ajustée (R² = {échelle['r_squared']:.2f}) sur "
            f"{échelle['parameter']} : la relation n'a pas l'air linéaire, "
            "le profil garde le pire cas mesuré"
        )
        échelle = None
    profil = build_profile(
        disk_bytes=disque,
        peak_bytes=pic,
        warmup_ms=warmup,
        cases=résultats,
        peak_scaling=échelle,
    )
    return BenchReport(
        ref=ref,
        model=model.id,
        variant=variant.id,
        capability=contract.id,
        profile=profil,
        cases=résultats,
        workload={"source": charge.source, "version": charge.version,
                  "cases": [c.id for c in charge.cases]},
        measured_on=describe_machine(lease.options.get("versions") if lease else None),
        measured_at=datetime.now(UTC).date().isoformat(),
        budget_bytes=supervisor.budget.bytes,
        warnings=avertissements + list(lease.warnings if lease else []),
    )


def _resolve_case(
    contract: CapabilityContract, variant: Variant, cas: BenchCase, base_dir: Path | None
) -> ResolvedInput:
    """Une entrée de charge type passe par la même porte que l'entrée d'un job.

    Les chemins de fichiers de la charge sont relatifs à son propre dossier, pas
    au répertoire courant : une mesure doit donner le même résultat qu'on la
    lance depuis la racine du dépôt ou d'ailleurs.
    """
    fichiers = {
        nom
        for nom, champ in contract.input_properties.items()
        if champ.get("x-ui") == "file" or "contentMediaType" in champ
    }
    assignations: dict[str, str] = {}
    for clé, valeur in cas.input.items():
        if clé in fichiers and isinstance(valeur, str) and base_dir is not None:
            chemin = Path(valeur)
            absolu = chemin if chemin.is_absolute() else (base_dir / chemin).resolve()
            assignations[clé] = str(absolu)
        else:
            assignations[clé] = valeur if isinstance(valeur, str) else json.dumps(valeur)
    return resolve_input(contract, variant, assignations)


def _peak_bytes(lease: Any, résultats: list[CaseResult]) -> int:
    """Le pic retenu est le plus grand relevé, chargement et inférences confondus.

    Le contrôle d'admission réserve de la place pour le pire moment, pas pour le
    moment moyen : un modèle qui tient à 3 Go au repos mais monte à 5 Go pendant
    le décodage occupe 5 Go du budget.
    """
    valeurs = [lease.loaded.peak_memory_bytes or 0] if lease and lease.loaded else []
    valeurs += [int(c.metrics.get("peak_memory_bytes") or 0) for c in résultats]
    return max(valeurs) if valeurs else 0


def _disk_bytes(
    supervisor: Supervisor, variant: Variant, ref: str, records: list[LocationRecord] | None
) -> int:
    """Occupation disque du variant : l'instantané épinglé d'abord.

    L'état observé rattache un variant à **tout** son dépôt Hugging Face, toutes
    révisions confondues — c'est ce qu'il faut pour la comptabilité du parc, mais
    pas pour un profil : `disk_bytes` doit décrire ce que pèse la révision qui a
    été mesurée, pas ce que pèsent en plus deux instantanés périmés qu'un
    `store plan` proposera de récupérer. On mesure donc l'instantané lui-même, et
    on ne retombe sur l'état observé que pour un variant dont les poids ne sont
    plus résolvables.
    """
    try:
        weights = resolve_weights(supervisor.config, variant, ref=ref)
    except Exception:  # noqa: BLE001 — poids introuvables : l'état observé, à défaut
        return variant_disk_bytes(records, ref) if records else 0
    mesuré = _tree_bytes(weights.path)
    if mesuré:
        return mesuré
    return variant_disk_bytes(records, ref) if records else 0


def _tree_bytes(path: Path) -> int:
    """Taille réelle d'une arborescence, chaque contenu compté une seule fois.

    Un instantané Hugging Face n'est qu'une forêt de liens vers `blobs/` : suivre
    les liens sans dédupliquer par inode doublerait la taille annoncée.
    """
    vus: set[tuple[int, int]] = set()
    total = 0
    if path.is_file():
        st = path.stat()
        return st.st_size
    for enfant in path.rglob("*"):
        try:
            st = enfant.stat()  # suit les liens symboliques, c'est voulu
        except OSError:
            continue
        if not enfant.is_file():
            continue
        clé = (st.st_dev, st.st_ino)
        if clé in vus:
            continue
        vus.add(clé)
        total += st.st_size
    return total


def fit_peak_scaling(
    workload: Workload, cases: list[CaseResult]
) -> dict[str, Any] | None:
    """Ajuste une droite pic ↔ paramètre, quand la charge en fait varier un.

    Le pic de certains modèles dépend de ce qu'on leur demande : trente secondes
    de musique coûtent le double de quinze. Un profil à un seul chiffre force
    alors un choix perdant — le pire cas refuse des jobs qui passeraient, un cas
    favorable laisse partir la machine en swap. La pente lève le dilemme, à
    condition d'être **mesurée** comme le reste du profil.

    Moindres carrés sur les points de la charge, et le R² est rendu avec : une
    droite ajustée sur une relation qui n'en est pas une vaut moins que rien.
    """
    nom = workload.scaling_parameter
    if not nom:
        return None
    points = []
    for cas, résultat in zip(workload.cases, cases, strict=False):
        if not résultat.ok:
            continue
        valeur = cas.input.get(nom)
        pic = résultat.metrics.get("peak_memory_bytes")
        if isinstance(valeur, (int, float)) and not isinstance(valeur, bool) and pic:
            points.append((float(valeur), float(pic)))
    if len(points) < 2 or len({x for x, _ in points}) < 2:
        return None

    n = len(points)
    somme_x = sum(x for x, _ in points)
    somme_y = sum(y for _, y in points)
    moyenne_x, moyenne_y = somme_x / n, somme_y / n
    variance = sum((x - moyenne_x) ** 2 for x, _ in points)
    if variance == 0:
        return None
    pente = sum((x - moyenne_x) * (y - moyenne_y) for x, y in points) / variance
    origine = moyenne_y - pente * moyenne_x

    résiduelle = sum((y - (origine + pente * x)) ** 2 for x, y in points)
    totale = sum((y - moyenne_y) ** 2 for _, y in points)
    r2 = 1.0 - résiduelle / totale if totale > 0 else 1.0

    return {
        "parameter": nom,
        "base_bytes": max(0, int(origine)),
        "bytes_per_unit": round(pente, 1),
        "measured_range": [min(x for x, _ in points), max(x for x, _ in points)],
        "r_squared": round(r2, 4),
    }


def build_profile(
    *,
    disk_bytes: int,
    peak_bytes: int,
    warmup_ms: int,
    cases: list[CaseResult],
    peak_scaling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    réussis = [c for c in cases if c.ok]
    profil: dict[str, Any] = {
        "disk_bytes": disk_bytes,
        "peak_unified_memory_bytes": peak_bytes,
    }
    if peak_scaling is not None:
        profil["peak_scaling"] = peak_scaling
    profil["warmup_ms"] = warmup_ms
    if réussis:
        profil["latency_ms_p50"] = int(statistics.median(c.duration_ms for c in réussis))
    rtf = _rtf(réussis)
    if rtf is not None:
        profil["rtf"] = rtf
    débit = _throughput(réussis)
    if débit:
        profil["throughput"] = débit
    return profil


def _rtf(cases: list[CaseResult]) -> float | None:
    """Temps de calcul par seconde produite, agrégé sur toute la charge.

    Agrégé, et non moyenné : la moyenne des `rtf` de chaque cas donne le même
    poids à une phrase de deux secondes qu'à un paragraphe de quinze, alors que
    le warmup et les coûts fixes pèsent bien plus lourd dans la première. Elle
    sortait un `rtf` de 0,87 là où la charge entière tournait à 0,54 — et un
    `throughput` qui n'en était plus l'inverse, ce qui rend les deux chiffres
    ininterprétables ensemble.
    """
    audio = sum(float(c.metrics.get("audio_seconds") or 0) for c in cases)
    secondes = sum(c.duration_ms for c in cases) / 1000
    if audio > 0 and secondes > 0:
        # Six décimales : à 4, un modèle rapide (rtf ~0,002) perdait plus d'un
        # pour cent à l'arrondi, et le débit annoncé cessait d'en être l'inverse.
        return round(secondes / audio, 6)
    mesurés = [float(c.metrics["rtf"]) for c in cases if c.metrics.get("rtf") is not None]
    # Sans durée d'audio, on ne peut plus agréger : on reprend ce que l'adaptateur
    # a rapporté, en le disant par la médiane plutôt que par la moyenne.
    return round(statistics.median(mesurés), 4) if mesurés else None


def _throughput(cases: list[CaseResult]) -> str | None:
    if not cases:
        return None
    secondes = sum(c.duration_ms for c in cases) / 1000
    if secondes <= 0:
        return None
    audio = sum(float(c.metrics.get("audio_seconds") or 0) for c in cases)
    if audio:
        return f"{audio / secondes:.2f}× temps réel"
    jetons = sum(float(c.metrics.get("token_count") or 0) for c in cases)
    if jetons:
        return f"{jetons / secondes:.1f} jetons/s"
    return f"1 sortie / {secondes / len(cases):.1f} s"


# --- écriture ----------------------------------------------------------------


def write_measurement(root: Path, report: BenchReport) -> Path:
    """Écrit `registry/measurements/<ref>/<machine>.json`, l'autorité du profil.

    Un fichier par machine, et non un par variant. Le dépôt est partagé et les
    Macs ne le sont pas : deux personnes qui mesurent le même variant écrivaient
    au même endroit, la seconde effaçait la première, et `registry validate`
    reprochait ensuite au manifeste une divergence qui n'était que l'autre
    machine. Rien ne le signalait — c'était le seul défaut multi-machine du dépôt
    à passer en silence.

    La même machine qui remesure, elle, remplace bien son relevé : le nom du
    fichier ne retient que le matériel, et `measured_on` à l'intérieur dit sous
    quelles versions la mesure a été reprise.
    """
    destination = root / MEASUREMENTS_DIR / report.ref / f"{report.machine_slug}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report.document(), ensure_ascii=False, indent=2) + "\n")
    return destination


def yaml_patch(report: BenchReport) -> str:
    """Le bloc `profile:` à coller sous le variant. L'outil ne touche pas au manifeste.

    L'en-tête nomme le fichier et le variant, et l'indentation est celle des
    manifestes du dépôt (les clés d'un variant à quatre espaces). Un bloc collé
    d'un cran trop loin se retrouverait dans `source:` — YAML l'accepterait sans
    broncher, et l'erreur ne se verrait qu'à la validation suivante, sous la
    forme d'un message qui ne parle pas d'indentation.
    """
    lignes = [
        f"# registry/models/{report.model}.yaml — sous variants[] → id: {report.variant}",
        "    profile:",
    ]
    for clé, valeur in report.profile.items():
        if isinstance(valeur, dict):
            # `peak_scaling` : un objet, donc un niveau d'indentation de plus.
            lignes.append(f"      {clé}:")
            for sous_clé, sous_valeur in valeur.items():
                lignes.append(f"        {sous_clé}: {_yaml_valeur(sous_valeur)}")
            continue
        lignes.append(f"      {clé}: {_yaml_valeur(valeur)}")
    lignes.append(f'      measured_on: "{report.measured_on}"')
    # Les guillemets ne sont pas décoratifs : sans eux, YAML lit `2026-08-20`
    # comme une date, et le schéma — qui attend une chaîne au format date —
    # rejette le manifeste. Le patch doit se coller sans rien avoir à retoucher.
    lignes.append(f'      measured_at: "{report.measured_at}"')
    lignes.append(f'      harness_version: "{report.harness_version}"')
    return "\n".join(lignes)


def _yaml_valeur(valeur: Any) -> str:
    """Rendu YAML d'une valeur scalaire ou d'une liste courte."""
    if isinstance(valeur, str):
        return f'"{valeur}"'
    if isinstance(valeur, list):
        return "[" + ", ".join(_yaml_valeur(v) for v in valeur) + "]"
    return str(valeur)
