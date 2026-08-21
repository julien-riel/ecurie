"""Un job, de bout en bout : entrée typée, exécution, artefact reproductible.

Le contrat de capacité fait loi. C'est lui qui dit quels paramètres existent,
avec quels types et quelles bornes ; `-p k=v` n'est qu'une façon de le remplir
depuis un terminal, et une clé qu'il ne déclare pas est refusée avec la liste de
celles qui existent — plutôt que transmise au worker, qui l'ignorerait en
silence.

Chaque job laisse `~/.ecurie/outputs/<job_id>/manifest.json` : capacité, modèle,
variant, **révision épinglée**, paramètres complets défauts résolus inclus,
graine, empreinte de l'entrée, versions, durée, métriques. C'est le contrat de
reproductibilité du §6 de la conception, et la raison d'être de l'écran
Bibliothèque au v0.4 : un artefact non reproductible est un artefact perdu.

L'entrée elle-même est copiée dans le dossier du job. Un fichier d'entrée laissé
en place se fait renommer, éditer ou effacer, et le rejeu d'un job de trois mois
échoue sur autre chose que ce qu'il prétendait vérifier.
"""

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecurie_core.capabilities import CapabilityContract
from ecurie_core.models import Model, Variant
from ecurie_store.db import StateDB
from jsonschema import Draft202012Validator

from ecurie_runtime import __version__ as HARNESS_VERSION
from ecurie_runtime.supervisor import Lease, Supervisor, WaitFn
from ecurie_runtime.worker import JobResult, ProgressFn

INPUTS_DIR = "inputs"


class InputError(ValueError):
    """Entrée refusée avant toute exécution — le message dit quoi corriger."""


@dataclass
class ResolvedInput:
    values: dict[str, Any]  # paramètres du contrat, défauts résolus
    params: dict[str, Any]  # réglages propres au runtime (variant.options)
    files: dict[str, str] = field(default_factory=dict)  # champ → sha256 du contenu
    provided: tuple[str, ...] = ()  # ce que l'utilisateur a réellement fourni
    # Reproches du contrat, quand la résolution a été demandée non stricte. Vide
    # après une résolution stricte : celle-ci lève au lieu de rendre un objet.
    errors: list[str] = field(default_factory=list)

    def hash(self) -> str:
        """Empreinte stable de l'entrée, fichiers remplacés par leur contenu.

        Deux jobs de même empreinte ont reçu la même chose, quels que soient les
        chemins. C'est ce qui appariera les sorties d'une confrontation A/B au
        v0.5, où comparer deux variants suppose de prouver qu'ils ont bien reçu
        la même entrée.
        """
        canonique = {**self.values}
        for champ, digest in self.files.items():
            canonique[champ] = f"sha256:{digest}"
        payload = json.dumps(
            {"input": canonique, "params": self.params}, sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class JobOutcome:
    job_id: str
    job_dir: Path
    ref: str
    ok: bool
    duration_ms: int
    result: JobResult | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    evicted: tuple[str, ...] = ()
    reused: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    file_outputs: tuple[str, ...] = ()  # clés de sortie qui sont des fichiers, chemins pointés

    @property
    def files(self) -> dict[str, Path]:
        """Fichiers réellement produits, y compris ceux d'une sortie imbriquée.

        Le contrat dit lesquelles de ses sorties sont des fichiers ; le reste est
        une métadonnée en ligne (une durée, une langue détectée). Sans cette
        distinction, une séparation audio afficherait `tracks` comme un chemin —
        alors que `tracks` est un objet qui contient les chemins.
        """
        if self.result is None:
            return {}
        trouvés = {}
        for clé in self.file_outputs:
            valeur = _lookup(self.result.output, clé)
            if isinstance(valeur, str):
                trouvés[clé] = self.job_dir / valeur
        return trouvés


# --- entrée ------------------------------------------------------------------


def parse_assignments(assignments: list[str] | None) -> dict[str, str]:
    """`["text=bonjour", "speed=1.2"]` → dict. La valeur peut contenir des `=`."""
    brut: dict[str, str] = {}
    for item in assignments or []:
        clé, séparateur, valeur = item.partition("=")
        if not séparateur or not clé.strip():
            raise InputError(f"paramètre mal formé : {item!r} — attendu clé=valeur")
        brut[clé.strip()] = valeur
    return brut


def coerce(value: str, schema: dict[str, Any], champ: str) -> Any:
    """Convertit une chaîne de la ligne de commande selon le type du contrat.

    Le terminal ne connaît que des chaînes ; le contrat, lui, sait qu'un
    `octree_resolution` est un entier borné. Sans cette conversion, la validation
    JSON Schema rejetterait tout, ou pire, le worker recevrait `"256"` et
    déciderait lui-même quoi en faire.
    """
    attendu = schema.get("type")
    try:
        if attendu == "integer":
            return int(value)
        if attendu == "number":
            return float(value)
        if attendu == "boolean":
            bas = value.strip().lower()
            if bas in ("1", "true", "vrai", "oui", "yes", "on"):
                return True
            if bas in ("0", "false", "faux", "non", "no", "off"):
                return False
            raise ValueError(value)
        if attendu in ("array", "object"):
            return json.loads(value)
    except (ValueError, json.JSONDecodeError) as exc:
        raise InputError(f"{champ} : {value!r} n'est pas un {attendu} valide") from exc
    return value


def merge_defaults(contract: CapabilityContract, variant: Variant) -> dict[str, Any]:
    """Défauts du contrat corrigés par ceux du variant, avant toute saisie.

    Extrait de `resolve_input` parce que l'API en a besoin seule : le bandeau de
    ressources chiffre le pic attendu d'une entrée que l'utilisateur est encore
    en train de remplir, donc avant qu'il y ait un job. Deux façons de composer
    ces deux niveaux donneraient deux pics pour la même saisie.
    """
    propriétés = contract.input_properties
    valeurs: dict[str, Any] = dict(contract.defaults())
    for nom, valeur in (variant.defaults or {}).items():
        if nom in propriétés:
            valeurs[nom] = valeur
    return valeurs


def resolve_input(
    contract: CapabilityContract,
    variant: Variant,
    assignments: dict[str, str],
    *,
    seed: int | None = None,
) -> ResolvedInput:
    """Fusionne défauts du contrat, défauts du variant et saisie, puis valide.

    L'ordre n'est pas arbitraire : le contrat pose la valeur générique, le
    variant la corrige pour ses particularités, l'utilisateur tranche. Les trois
    niveaux sont écrits au manifeste du job, si bien qu'un rejeu ne dépend
    d'aucun défaut qui aurait changé entre-temps.
    """
    return _resolve(contract, variant, assignments, convert=coerce, seed=seed)


def resolve_typed_input(
    contract: CapabilityContract,
    variant: Variant,
    provided: dict[str, Any] | None,
    *,
    seed: int | None = None,
    strict: bool = True,
) -> ResolvedInput:
    """Même résolution, pour une entrée déjà typée — celle qui arrive en JSON.

    Le terminal ne connaît que des chaînes, et `coerce` les convertit d'après le
    contrat ; un client HTTP, lui, envoie déjà un nombre pour un nombre. Faire
    passer sa valeur par la conversion du terminal marcherait par accident sur
    `1.0`, et se tromperait sur `false` — qui deviendrait la chaîne `"False"`,
    puis le booléen vrai.

    `strict=False` rend les reproches du contrat dans `errors` au lieu de lever.
    C'est ce qu'il faut au bandeau de ressources, qui chiffre le coût d'une
    entrée **en cours de saisie** : un texte encore vide n'est pas une erreur du
    client, et refuser de répondre priverait l'utilisateur du chiffre au moment
    précis où il en a besoin. Un paramètre que le contrat ne déclare pas, lui,
    lève dans les deux cas — ce n'est pas une saisie incomplète, c'est un client
    qui parle d'autre chose que la capacité qu'il a nommée.
    """
    return _resolve(contract, variant, provided or {}, convert=_as_is, seed=seed, strict=strict)


def _as_is(value: Any, schema: dict[str, Any], champ: str) -> Any:
    return value


def _resolve(
    contract: CapabilityContract,
    variant: Variant,
    assignments: dict[str, Any],
    *,
    convert: Any,
    seed: int | None = None,
    strict: bool = True,
) -> ResolvedInput:
    propriétés = contract.input_properties
    options = dict(variant.options or {})
    valeurs = merge_defaults(contract, variant)

    fournis: list[str] = []
    for clé, brut in assignments.items():
        if clé in propriétés:
            valeurs[clé] = convert(brut, propriétés[clé], clé)
            fournis.append(clé)
        elif clé in options:
            # Réglage propre au runtime, déjà déclaré par le manifeste : on
            # autorise à le surcharger, jamais à en inventer un.
            options[clé] = convert(brut, {"type": _type_of(options[clé])}, clé)
            fournis.append(clé)
        else:
            connus = ", ".join(sorted(propriétés)) or "aucun"
            supplément = f" ; options du variant : {', '.join(sorted(options))}" if options else ""
            raise InputError(
                f"{clé!r} n'est pas un paramètre de la capacité {contract.id} "
                f"(paramètres : {connus}{supplément})"
            )

    if seed is not None and "seed" in propriétés:
        valeurs["seed"] = seed

    erreurs = validate_input(contract, valeurs)
    if erreurs and strict:
        raise InputError(
            f"entrée refusée par le contrat {contract.id} :\n  " + "\n  ".join(erreurs)
        )
    return ResolvedInput(
        values=valeurs, params=options, provided=tuple(fournis), errors=erreurs
    )


def _type_of(valeur: Any) -> str:
    if isinstance(valeur, bool):
        return "boolean"
    if isinstance(valeur, int):
        return "integer"
    if isinstance(valeur, float):
        return "number"
    if isinstance(valeur, (list, tuple)):
        return "array"
    if isinstance(valeur, dict):
        return "object"
    return "string"


def validate_input(contract: CapabilityContract, valeurs: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(contract.input_schema)
    return [
        f"{'/'.join(str(p) for p in err.absolute_path) or 'entrée'} : {err.message}"
        for err in sorted(validator.iter_errors(valeurs), key=lambda e: list(e.absolute_path))
    ]


def stage_inputs(contract: CapabilityContract, résolu: ResolvedInput, job_dir: Path) -> None:
    """Copie les fichiers d'entrée dans le dossier du job et note leur empreinte."""
    champs_fichiers = {
        nom
        for nom, champ in contract.input_properties.items()
        if champ.get("x-ui") == "file" or "contentMediaType" in champ
    }
    if not champs_fichiers:
        return
    destination = job_dir / INPUTS_DIR
    for nom in sorted(champs_fichiers):
        valeur = résolu.values.get(nom)
        if not isinstance(valeur, str) or not valeur:
            continue
        source = Path(valeur).expanduser()
        if not source.is_file():
            raise InputError(f"{nom} : fichier introuvable — {source}")
        destination.mkdir(parents=True, exist_ok=True)
        cible = destination / source.name
        shutil.copy2(source, cible)
        résolu.files[nom] = _sha256(cible)
        # Le worker reçoit un chemin relatif au dossier du job : le même job
        # rejoué ailleurs retrouve son entrée sans dépendre du disque d'origine.
        résolu.values[nom] = f"{INPUTS_DIR}/{source.name}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while bloc := handle.read(1 << 22):
            digest.update(bloc)
    return digest.hexdigest()


# --- exécution ---------------------------------------------------------------


def new_job_id(now: datetime | None = None) -> str:
    """Horodaté pour se lire et se trier, suffixé pour ne jamais entrer en collision."""
    horodatage = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{horodatage}-{uuid.uuid4().hex[:6]}"


def run_job(
    supervisor: Supervisor,
    model: Model,
    variant: Variant,
    contract: CapabilityContract,
    assignments: dict[str, str],
    *,
    seed: int | None = None,
    db: StateDB | None = None,
    outputs_dir: Path | None = None,
    on_progress: ProgressFn | None = None,
    on_wait: WaitFn | None = None,
    pin: bool = False,
    job_id: str | None = None,
) -> JobOutcome:
    """Exécute un job complet et rend un résultat déjà écrit sur le disque."""
    ref = f"{model.id}@{variant.id}"
    job_id = job_id or new_job_id()
    racine = outputs_dir or supervisor.config.outputs_dir
    job_dir = racine / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    résolu = resolve_input(contract, variant, assignments, seed=seed)
    stage_inputs(contract, résolu, job_dir)
    # La graine transmise au worker est celle du contrat quand il en déclare une,
    # sinon celle de la ligne de commande : un contrat sans champ `seed` ne rend
    # pas le modèle déterministe pour autant, et le manifeste doit dire laquelle
    # a servi, faute de quoi le rejeu ne rejoue rien.
    graine = résolu.values.get("seed", seed)

    démarré = datetime.now(UTC)
    lease: Lease | None = None
    résultat: JobResult | None = None
    erreur: str | None = None
    try:
        lease = supervisor.acquire(
            model,
            variant,
            pin=pin,
            on_progress=on_progress,
            values=résolu.values,
            job_id=job_id,
            on_wait=on_wait,
        )
        résultat = lease.session.infer(
            job_id,
            input=résolu.values,
            params=résolu.params,
            output_dir=job_dir,
            seed=graine,
        )
    except Exception as exc:  # noqa: BLE001 — l'échec est un résultat, il s'écrit comme les autres
        erreur = f"{type(exc).__name__}: {exc}"
    finally:
        if lease is not None:
            lease.release()

    durée_ms = int((datetime.now(UTC) - démarré).total_seconds() * 1000)
    avertissements = list(lease.warnings) if lease else []
    note = variant.profile.expected_peak(résolu.values)[1] if variant.profile else None
    if note:
        avertissements.append(note)
    if résultat is not None:
        manquantes, autres = _check_outputs(contract, résultat, job_dir)
        avertissements.extend(autres)
        if manquantes:
            # Le contrat fait loi jusqu'au bout : un job dont la sortie exigée
            # n'existe pas n'a pas réussi. Le compter « ok » mettrait dans la
            # Bibliothèque un artefact vide, et dans la télémétrie un succès qui
            # n'en est pas un.
            erreur = erreur or "sortie manquante — " + " ; ".join(manquantes)

    manifeste = build_manifest(
        job_id=job_id,
        model=model,
        variant=variant,
        contract=contract,
        résolu=résolu,
        résultat=résultat,
        lease=lease,
        démarré=démarré,
        durée_ms=durée_ms,
        erreur=erreur,
        avertissements=avertissements,
        graine=graine,
    )
    (job_dir / "manifest.json").write_text(
        json.dumps(manifeste, ensure_ascii=False, indent=2, default=str)
    )

    if db is not None and lease is not None:
        # Une ligne de `runs` atteste d'un **usage** : c'est elle qui alimente le
        # poste « variants jamais utilisés » du plan de GC. Un job refusé avant
        # d'atteindre le worker — poids absents, environnement non synchronisé,
        # admission refusée — n'est pas un usage : l'inscrire ferait passer pour
        # servi un variant qui n'a jamais tourné, et le soustrairait
        # définitivement aux propositions de récupération d'espace.
        db.record_run(
            run_id=job_id,
            variant_ref=ref,
            started_at=démarré.isoformat(),
            duration_ms=durée_ms,
            job_dir=str(job_dir),
            ok=erreur is None,
        )

    return JobOutcome(
        job_id=job_id,
        job_dir=job_dir,
        ref=ref,
        ok=erreur is None,
        duration_ms=durée_ms,
        result=résultat,
        manifest=manifeste,
        evicted=lease.evicted if lease else (),
        reused=lease.reused if lease else False,
        error=erreur,
        warnings=avertissements,
        file_outputs=tuple(sorted(contract.file_outputs())),
    )


def _check_outputs(
    contract: CapabilityContract, résultat: JobResult, job_dir: Path
) -> tuple[list[str], list[str]]:
    """Le worker annonce des fichiers : on vérifie qu'ils existent vraiment.

    Rend deux listes, et la distinction compte : ce qui manque au contrat fait
    échouer le job, ce qui ne concerne qu'une sortie facultative l'accompagne
    d'un avertissement. Un `result` qui nomme un fichier absent est plus
    dangereux qu'une erreur franche — le job passerait pour réussi, la
    télémétrie dirait « ok », et l'absence ne se découvrirait qu'à l'ouverture
    de la Bibliothèque, des semaines plus tard.
    """
    requis = set(contract.output_schema.get("required") or [])
    manquantes: list[str] = []
    avertissements: list[str] = []

    for manquant in sorted(requis - set(résultat.output)):
        manquantes.append(f"{manquant!r} est exigée par le contrat mais n'a pas été produite")

    for champ in sorted(contract.file_outputs()):
        valeur = _lookup(résultat.output, champ)
        if valeur is None:
            continue  # sortie facultative non produite : le contrat le permet
        chemin = job_dir / str(valeur)
        if chemin.exists():
            continue
        message = f"{champ!r} annoncée mais introuvable : {chemin.name}"
        (manquantes if champ.split(".")[0] in requis else avertissements).append(message)

    return manquantes, avertissements


def _lookup(output: dict[str, Any], chemin: str) -> Any:
    """Suit un chemin pointé (`tracks.vocals`) dans la sortie du worker."""
    courant: Any = output
    for segment in chemin.split("."):
        if not isinstance(courant, dict) or segment not in courant:
            return None
        courant = courant[segment]
    return courant


def build_manifest(
    *,
    job_id: str,
    model: Model,
    variant: Variant,
    contract: CapabilityContract,
    résolu: ResolvedInput,
    résultat: JobResult | None,
    lease: Lease | None,
    démarré: datetime,
    durée_ms: int,
    erreur: str | None,
    avertissements: list[str],
    graine: int | None = None,
) -> dict[str, Any]:
    versions = {"harness": HARNESS_VERSION, "runtime": variant.runtime}
    if lease is not None and lease.options.get("versions"):
        versions.update(lease.options["versions"])
    return {
        "job_id": job_id,
        "started_at": démarré.isoformat(),
        "duration_ms": durée_ms,
        "ok": erreur is None,
        "capability": model.capability,
        "model": model.id,
        "variant": variant.id,
        "revision": variant.source.revision,
        "repo": variant.source.repo,
        "quantization": variant.quantization,
        "input": résolu.values,
        "input_files": résolu.files,
        "input_hash": résolu.hash(),
        "params": résolu.params,
        "seed": résolu.values.get("seed", graine),
        "provided": list(résolu.provided),
        "output": résultat.output if résultat else {},
        "metrics": résultat.metrics if résultat else {},
        "versions": versions,
        "contract": {"id": contract.id, "outputs": contract.output_media_types()},
        "error": erreur,
        "warnings": avertissements,
    }
