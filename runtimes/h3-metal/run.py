"""Entrypoint MiniMax-H3 — texte vers vidéo sonorisée (CONCEPTION.md §5.2).

C'est le chemin `runtime: custom`, et le premier du parc dont l'inférence ne se
fait pas en Python. Le superviseur lance ce fichier avec l'interpréteur de
`runtimes/h3-metal/.venv` et ne lui rend visible d'Écurie que
`ecurie_runtime.workers.base`. Ce fichier, lui, ne calcule rien : il compose une
ligne de commande pour le binaire `h3` d'antirez, le lance, écoute ce qu'il dit,
et range ce qu'il a produit.

Quatre choses expliquent sa forme.

1. **Le pic mémoire ne peut pas venir du runtime.** `mx.get_peak_memory()`
   n'existe pas ici et le RSS de ce processus-ci ne mesure que ce processus-ci.
   Deux sources sont donc combinées, et la plus grande retenue : le RSS du
   processus fils, échantillonné pendant qu'il tourne, et les `peak=` que
   `--profile` rend phase par phase. Elles ne mesurent pas la même chose — la
   première compte les pages du processus, la seconde le stockage tenseur suivi
   par le pilote Metal — et selon la phase, l'une ou l'autre l'emporte. Se
   tromper de source sous-déclare le chiffre dont dépend le contrôle
   d'admission.

2. **`--profile` est la seule fenêtre sur ce qui se passe dedans**, et il révèle
   que le pic est sur l'encodeur de texte, pas sur le DiT. Ses lignes sont donc
   analysées, pas seulement journalisées : sans elles, ce worker déclarerait un
   pic faux avec assurance.

3. **`--ssd-streaming` est une condition d'exécution, pas un réglage.** Le DiT
   entièrement résident demande environ 36,5 Gio, le double du budget. Le drapeau
   est donc posé par le manifeste et vérifié ici : un variant qui l'omettrait
   ferait tomber la machine en swap sans que rien ne l'annonce.

4. **H3 quantifie le nombre d'images sur sa propre grille.** Une demande de 8
   images rend 22 images. Le job déclare donc les deux nombres, et la mesure
   porte sur ce qui a été produit, jamais sur ce qui a été demandé.

Le worker ne télécharge rien : `weights_path` est un chemin local déjà vérifié
par le superviseur.
"""

import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
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

RACINE = Path(__file__).resolve().parent
DEPOT_AMONT = "https://github.com/antirez/h3.c"
VENDOR_DEFAUT = RACINE / "vendor" / "h3.c" / "h3"
GIO = 1024**3

# `h3 profile: <phase> <étape>   wall=  11.656s … peak=  3.637GiB alloc= 46.864GiB …`
# Le libellé est complété par des espaces sur une largeur fixe ; on le récupère
# non gourmand jusqu'au premier `wall=`, ce qui évite d'avoir à connaître cette
# largeur — elle a déjà changé une fois en amont.
LIGNE_PROFIL = re.compile(
    r"^h3 profile:\s+(?P<libelle>.*?)\s+wall=\s*(?P<wall>[\d.]+)s"
    r".*?peak=\s*(?P<peak>[\d.]+)GiB"
    r"(?:.*?alloc=\s*(?P<alloc>[\d.]+)GiB)?"
)
# `h3: BF16 SSD stream 115.562 GiB read in 22.859s (5.055 GiB/s), unhidden wait 13.528s`
LIGNE_SSD = re.compile(
    r"^h3: BF16 SSD stream\s+(?P<lu>[\d.]+) GiB read in\s+(?P<duree>[\d.]+)s"
    r"\s+\((?P<debit>[\d.]+) GiB/s\)(?:, unhidden wait\s+(?P<attente>[\d.]+)s)?"
)
# `text encoder                 12/50` — barre de progression, réécrite par \r.
LIGNE_ETAPE = re.compile(r"^(?P<etape>[a-zA-Z][\w \-]*?)\s+(?P<fait>\d+)/(?P<total>\d+)\s*$")

# Inventaire de `--info`, une ligne par composant :
# `  Qwen3-VL encoder   14 files  1058 tensors   62.133 GiB`
LIGNE_INVENTAIRE = re.compile(
    r"^\s+(?P<composant>[\w\- ]+?)\s+(?P<fichiers>\d+) files\s+(?P<tenseurs>\d+) tensors"
    r"\s+(?P<gio>[\d.]+) GiB\s*$"
)

# Jalons de progression : un intervalle par phase, parcouru au rythme du `n/total`
# que le binaire imprime. Les largeurs viennent du banc, pas d'une intuition —
# sur le cas long, le débruitage occupe 87 % du mur et le VAE vidéo 11 %, tout le
# reste tenant dans les 2 % restants.
#
# Des paliers fixes donneraient une barre figée sur un seul chiffre pendant les
# onze minutes du débruitage, ce qui, dans l'Atelier, ne se distingue pas d'un
# job planté. L'ordre compte : « denoise enqueue » doit précéder « denoise »,
# sans quoi le préfixe le plus court capterait les deux.
JALONS = (
    ("tokenizer", 1, 2, "chargement du tokenizer"),
    ("text encoder", 2, 6, "encodage de la consigne"),
    ("refine text", 6, 7, "affinage de la consigne"),
    ("precompute adaln", 7, 9, "précalcul AdaLN"),
    ("load transformer core", 9, 12, "chargement du DiT"),
    ("denoise enqueue", 12, 14, "mise en file du débruitage"),
    ("denoise", 14, 86, "débruitage"),
    ("audio vae", 86, 87, "décodage de la bande-son"),
    ("video vae load", 87, 96, "décodage de l'image"),
    ("ffmpeg", 96, 98, "assemblage du MP4"),
)


# --- localisation du binaire et des outils ----------------------------------


def localiser_binaire() -> Path:
    """Le binaire `h3`, dans l'ordre : variable d'environnement, vendor, PATH.

    La variable existe parce qu'une construction de `h3.c` déjà faite ailleurs
    sur la machine n'a aucune raison d'être refaite sous `vendor/`.
    """
    depuis_env = os.environ.get("ECURIE_H3_BIN", "").strip()
    if depuis_env:
        chemin = Path(depuis_env).expanduser()
        if chemin.is_file() and os.access(chemin, os.X_OK):
            return chemin
        raise WorkerError(
            f"ECURIE_H3_BIN désigne {chemin}, qui n'est pas un exécutable. "
            "Corriger la variable, ou la retirer pour retomber sur "
            f"{VENDOR_DEFAUT.relative_to(RACINE.parent.parent)}."
        )

    if VENDOR_DEFAUT.is_file() and os.access(VENDOR_DEFAUT, os.X_OK):
        return VENDOR_DEFAUT

    sur_le_path = shutil.which("h3")
    if sur_le_path:
        return Path(sur_le_path)

    raise WorkerError(
        "binaire `h3` introuvable — le code amont n'est pas sur PyPI et n'est pas "
        "versionné ici. Le construire :\n"
        f"    git clone {DEPOT_AMONT} runtimes/h3-metal/vendor/h3.c\n"
        "    make -j8 -C runtimes/h3-metal/vendor/h3.c\n"
        "ou pointer une construction existante avec ECURIE_H3_BIN."
    )


def verifier_ffmpeg() -> dict[str, str]:
    """FFmpeg assemble le MP4, FFprobe le vérifie. Sans eux, la panne serait muette.

    Seul le numéro est retenu, pas la ligne entière : ces versions finissent dans
    le `measured_on` du profil, qui est le champ sur lequel une autre machine
    décide si un profil mesuré ailleurs la concerne. Deux bannières de copyright
    de deux cents caractères y rendraient cette comparaison illisible.
    """
    versions: dict[str, str] = {}
    for outil in ("ffmpeg", "ffprobe"):
        chemin = shutil.which(outil)
        if not chemin:
            raise WorkerError(
                f"`{outil}` absent du PATH. Le binaire h3 s'en sert pour écrire la "
                "vidéo, et cet adaptateur pour la relire. `brew install ffmpeg` "
                "fournit les deux."
            )
        sortie = subprocess.run(
            [chemin, "-version"], capture_output=True, text=True, timeout=20, check=False
        )
        première = ((sortie.stdout or "").splitlines() or [""])[0]
        trouve = re.search(rf"{outil} version (\S+)", première)
        versions[outil] = trouve.group(1) if trouve else "?"
    return versions


def localiser_poids(weights_path: Any) -> Path:
    """Racine de l'instantané MiniMax-H3, telle que `-d` l'attend.

    Le manifeste désigne un chemin local. On accepte aussi bien la racine qui
    contient `FL2VA/` que le dossier parent d'un instantané Hugging Face, parce
    que les deux se rencontrent selon la façon dont les poids ont été déposés.
    """
    if not weights_path:
        raise WorkerError(
            "variant sans `weights_path` : le superviseur n'a pas résolu les poids. "
            "Ce runtime ne télécharge rien — le manifeste doit porter un "
            "`source: {kind: local, path: …}` existant."
        )
    base = Path(str(weights_path)).expanduser()
    if not base.is_dir():
        raise WorkerError(f"poids introuvables : {base} n'est pas un dossier.")
    if (base / "FL2VA").is_dir():
        return base
    for candidat in sorted(base.glob("*/FL2VA")):
        return candidat.parent
    raise WorkerError(
        f"{base} ne contient pas de dossier `FL2VA/`. L'instantané attendu est celui "
        "de Hugging Face avec sa structure d'origine — voir le README du runtime."
    )


# --- mesure -----------------------------------------------------------------


class EchantillonneurRss:
    """Pic RSS d'un processus fils, relevé pendant qu'il tourne.

    `resource.getrusage(RUSAGE_CHILDREN)` donnerait le chiffre exact et sans
    surcoût, mais il est cumulatif : sur un worker résident qui enchaîne les
    jobs, il rendrait au second job le pic du premier s'il était plus grand.
    Un échantillonnage attribue le pic au bon job. Les deux sont relevés, et le
    job déclare les deux.
    """

    def __init__(self, pid: int, periode_s: float = 0.1) -> None:
        self._pid = pid
        self._periode = periode_s
        self._pic = 0
        self._stop = threading.Event()
        self._fil = threading.Thread(target=self._boucle, daemon=True)

    def __enter__(self) -> "EchantillonneurRss":
        self._fil.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._fil.join(timeout=2.0)

    def _boucle(self) -> None:
        while not self._stop.is_set():
            valeur = self._rss()
            if valeur > self._pic:
                self._pic = valeur
            self._stop.wait(self._periode)

    def _rss(self) -> int:
        try:
            sortie = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(self._pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return 0
        brut = (sortie.stdout or "").strip()
        # `ps` rend des kibioctets sur macOS comme sur Linux — contrairement à
        # `ru_maxrss`, dont l'unité change selon la plateforme.
        return int(brut) * 1024 if brut.isdigit() else 0

    @property
    def pic_bytes(self) -> int:
        return self._pic


@dataclass
class Phase:
    libelle: str
    wall_s: float
    peak_bytes: int
    alloc_bytes: int | None = None


@dataclass
class Journal:
    """Ce que le binaire a dit de lui-même pendant qu'il travaillait."""

    phases: list[Phase] = field(default_factory=list)
    ssd: dict[str, float] = field(default_factory=dict)
    lignes: list[str] = field(default_factory=list)

    def pic_profil_bytes(self) -> int:
        return max((p.peak_bytes for p in self.phases), default=0)

    def phase(self, motif: str) -> Phase | None:
        motif = motif.lower()
        for p in self.phases:
            if motif in p.libelle.lower():
                return p
        return None


def analyser_ligne(ligne: str, journal: Journal, progress: ProgressFn) -> None:
    """Range une ligne du binaire : profil, streaming SSD, ou barre de progression."""
    if profil := LIGNE_PROFIL.match(ligne):
        alloc = profil.group("alloc")
        journal.phases.append(
            Phase(
                libelle=profil.group("libelle").strip(),
                wall_s=float(profil.group("wall")),
                peak_bytes=int(float(profil.group("peak")) * GIO),
                alloc_bytes=int(float(alloc) * GIO) if alloc else None,
            )
        )
        return

    if ssd := LIGNE_SSD.match(ligne):
        journal.ssd = {
            "read_gib": float(ssd.group("lu")),
            "seconds": float(ssd.group("duree")),
            "gib_per_second": float(ssd.group("debit")),
            "unhidden_wait_seconds": float(ssd.group("attente") or 0.0),
        }
        return

    if etape := LIGNE_ETAPE.match(ligne):
        nom = etape.group("etape").strip().lower()
        for prefixe, début, fin, libelle in JALONS:
            if nom.startswith(prefixe):
                fait, total = int(etape.group("fait")), int(etape.group("total"))
                part = min(max(fait / total, 0.0), 1.0) if total > 0 else 0.0
                détail = f"{libelle} ({fait}/{total})" if total > 1 else libelle
                progress(int(début + (fin - début) * part), détail)
                return


# --- arguments --------------------------------------------------------------


@dataclass
class Arguments:
    prompt: str
    width: int
    height: int
    frames: int
    steps: int
    seed: int | None
    layers: int
    reuse: int
    core_reuse: int | None
    ssd_streaming: bool
    ignores: list[str]


def preparer_arguments(request: InferRequest, defauts: Mapping[str, Any],
                       options: Mapping[str, Any]) -> Arguments:
    """Traduit le contrat de capacité en options de la CLI `h3`.

    Trois champs du contrat n'ont aucun équivalent et sont reçus puis ignorés.
    Ils ressortent dans `ignored_contract_fields` : un réglage sans effet qui ne
    se voit nulle part est pire qu'un réglage absent.
    """
    ignores = [
        champ
        for champ in ("negative_prompt", "guidance_scale", "fps")
        if request.get(champ) is not None
    ]

    prompt = (request.get("prompt") or "").strip()
    if not prompt:
        raise WorkerError("`prompt` vide : le contrat text-to-video l'exige non vide.")

    def entier(nom: str, defaut: int) -> int:
        valeur = request.get(nom, defauts.get(nom, defaut))
        return int(valeur) if valeur is not None else defaut

    graine = request.get("seed", request.seed)

    return Arguments(
        prompt=prompt,
        width=entier("width", 512),
        height=entier("height", 320),
        frames=entier("num_frames", 22),
        steps=entier("steps", 20),
        seed=int(graine) if graine is not None else None,
        # Réglages propres au modèle : hors contrat de capacité, donc pris dans
        # `options` du variant, jamais dans l'entrée du job.
        layers=int(options.get("layers", 50)),
        reuse=int(options.get("reuse", 1)),
        core_reuse=int(options["core_reuse"]) if options.get("core_reuse") else None,
        ssd_streaming=bool(options.get("ssd_streaming", True)),
        ignores=ignores,
    )


def composer_commande(binaire: Path, poids: Path, args: Arguments, sortie: Path) -> list[str]:
    commande = [
        str(binaire),
        "--profile",
        "-d", str(poids),
        "-p", args.prompt,
        "--width", str(args.width),
        "--height", str(args.height),
        "--frames", str(args.frames),
        "--steps", str(args.steps),
        "--layers", str(args.layers),
        "--reuse", str(args.reuse),
        "-o", str(sortie),
    ]
    if args.core_reuse:
        commande += ["--core-reuse", str(args.core_reuse)]
    if args.seed is not None:
        commande += ["--seed", str(args.seed)]
    if args.ssd_streaming:
        commande.append("--ssd-streaming")
    return commande


# --- inspection de la sortie ------------------------------------------------


def sonder(chemin: Path) -> dict[str, Any]:
    """Ce que FFprobe dit du fichier produit. Un MP4 bien formé peut être vide."""
    champs = "stream=codec_type,codec_name,width,height,nb_frames,r_frame_rate,duration,sample_rate,channels"
    sortie = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", champs, "-of", "json", str(chemin)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if sortie.returncode != 0:
        raise WorkerError(f"ffprobe a refusé la vidéo produite : {sortie.stderr.strip()[:400]}")

    import json as _json

    flux = _json.loads(sortie.stdout or "{}").get("streams", [])
    video = next((f for f in flux if f.get("codec_type") == "video"), None)
    audio = next((f for f in flux if f.get("codec_type") == "audio"), None)
    if video is None:
        raise WorkerError(
            "la vidéo produite ne contient aucun flux vidéo — le binaire a rendu un "
            "conteneur vide. Regarder le journal du job avant d'accuser le modèle."
        )

    def cadence(brut: str | None) -> float | None:
        if not brut or "/" not in brut:
            return None
        num, den = brut.split("/", 1)
        return round(int(num) / int(den), 3) if int(den) else None

    infos: dict[str, Any] = {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "frames_produced": int(video["nb_frames"]) if video.get("nb_frames") else None,
        "fps": cadence(video.get("r_frame_rate")),
        "duration_seconds": round(float(video["duration"]), 3) if video.get("duration") else None,
        "video_codec": video.get("codec_name"),
        "has_audio": audio is not None,
    }
    if audio is not None:
        infos["audio_codec"] = audio.get("codec_name")
        infos["audio_sample_rate"] = int(audio["sample_rate"]) if audio.get("sample_rate") else None
        infos["audio_channels"] = int(audio["channels"]) if audio.get("channels") else None
    return infos


def extraire_audio(video: Path, destination: Path) -> bool:
    """Copie la bande-son en WAV à côté de la vidéo.

    La piste est déjà dans le MP4 ; cette copie existe pour que le contrat puisse
    la déclarer et pour qu'on puisse l'écouter sans la regarder. En PCM plutôt
    qu'en AAC recopié : c'est ce que le contrat annonce, et ce que l'Atelier
    joue sans négocier de codec.
    """
    sortie = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-acodec", "pcm_s16le",
         "-y", str(destination)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return sortie.returncode == 0 and destination.is_file() and destination.stat().st_size > 0


# --- worker -----------------------------------------------------------------


class H3Worker(Worker):
    name = "minimax-h3"

    def __init__(self) -> None:
        self.variant: dict[str, Any] = {}
        self.binaire: Path | None = None
        self.poids: Path | None = None
        self.inventaire: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, str] = {}
        self._pic_job = 0

    # -- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        """Vérifie l'outillage et la disposition des poids, sans mapper un octet.

        L'ordre compte. Le binaire d'abord, parce qu'une absence de binaire se
        présenterait sinon comme une panne de poids. FFmpeg ensuite, parce que
        son absence ne se verrait qu'à la toute fin, sur un MP4 jamais écrit.
        L'inventaire en dernier, parce que c'est le seul des trois qui demande
        de lancer quelque chose.
        """
        self.variant = variant
        self.binaire = localiser_binaire()
        self.versions = verifier_ffmpeg()
        self.poids = localiser_poids(variant.get("weights_path"))

        options = variant.get("options") or {}
        if not bool(options.get("ssd_streaming", True)):
            raise WorkerError(
                "`options.ssd_streaming: false` sur ce variant : le DiT entièrement "
                "résident demande environ 36,5 Gio, soit le double du budget de la "
                "machine de référence. Ce n'est pas un réglage de confort — voir le "
                "README du runtime."
            )

        info = subprocess.run(
            [str(self.binaire), "--info", "-d", str(self.poids)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        texte = (info.stdout or "") + (info.stderr or "")
        if info.returncode != 0:
            raise WorkerError(
                f"`h3 --info` a échoué (code {info.returncode}) : {texte.strip()[-400:]}"
            )

        for ligne in texte.splitlines():
            if trouve := LIGNE_INVENTAIRE.match(ligne):
                self.inventaire[trouve.group("composant").strip()] = {
                    "files": int(trouve.group("fichiers")),
                    "tensors": int(trouve.group("tenseurs")),
                    "gib": float(trouve.group("gio")),
                }

        manquants = [
            nom
            for nom, attendu in (("Qwen3-VL encoder", 1), ("FL2VA DiT", 1),
                                 ("video VAE", 1), ("audio VAE", 1))
            if self.inventaire.get(nom, {}).get("files", 0) < attendu
        ]
        if manquants:
            raise WorkerError(
                "instantané incomplet — `h3 --info` ne trouve aucun fichier pour : "
                + ", ".join(manquants)
                + ". Le README du runtime décrit la disposition attendue."
            )

        # `--info` ouvre sur « h3-metal 0.1.0-dev ». On ne garde que le numéro :
        # la clé du dictionnaire porte déjà le nom, et le profil affiche les deux
        # accolés.
        entête = next(
            (l.strip() for l in texte.splitlines() if l.startswith("h3-metal")), ""
        )
        version = entête.split(maxsplit=1)[1] if " " in entête else "?"
        return {
            "versions": {"h3": version, **self.versions},
            "inventory": self.inventaire,
            # Ref2VA n'est pas dans la partition FL2VA : le dire au chargement
            # évite qu'on cherche pourquoi `--ref-image` ne fait rien.
            "ref2va_available": self.inventaire.get("Ref2VA DiT", {}).get("files", 0) > 0,
        }

    # -- inférence -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.binaire is None or self.poids is None:
            raise WorkerError("infer avant load — ni binaire ni poids résolus")

        args = preparer_arguments(
            request, self.variant.get("defaults") or {}, self.variant.get("options") or {}
        )
        video = request.output_dir / "video.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        commande = composer_commande(self.binaire, self.poids, args, video)

        progress(2, "lancement de h3")
        journal = Journal()
        début = time.monotonic()

        processus = subprocess.Popen(
            commande,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # Le binaire compile `h3_shaders.metal` au premier usage et le
            # cherche dans son répertoire courant, pas à côté de lui-même. Sans
            # ce `cwd`, tout job échoue en « cannot compile h3_shaders.metal »
            # après avoir chargé le tokenizer — une panne qui ressemble à un
            # problème de poids et n'en est pas.
            cwd=str(self.binaire.parent),
        )
        # Le binaire réécrit ses barres de progression avec \r plutôt que \n. On
        # lit donc caractère par caractère et on découpe sur les deux : la
        # traduction universelle des fins de ligne ramène le \r à un \n, ce qui
        # convient puisque les deux valent ici « ligne terminée ».
        with EchantillonneurRss(processus.pid) as échantillonneur:
            tampon = ""
            assert processus.stdout is not None
            for morceau in iter(lambda: processus.stdout.read(1), ""):
                if morceau in ("\r", "\n"):
                    if ligne := tampon.strip():
                        journal.lignes.append(ligne)
                        analyser_ligne(ligne, journal, progress)
                    tampon = ""
                else:
                    tampon += morceau
            if ligne := tampon.strip():
                journal.lignes.append(ligne)
                analyser_ligne(ligne, journal, progress)
            code = processus.wait()

        mur_ms = int((time.monotonic() - début) * 1000)

        if code != 0:
            queue = " | ".join(journal.lignes[-6:])
            raise WorkerError(f"h3 a échoué (code {code}). Dernières lignes : {queue}")
        if not video.is_file() or video.stat().st_size == 0:
            raise WorkerError(
                "h3 s'est terminé sans erreur mais n'a écrit aucune vidéo. "
                f"Dernières lignes : {' | '.join(journal.lignes[-6:])}"
            )

        progress(98, "vérification de la sortie")
        infos = sonder(video)

        sortie: dict[str, Any] = {"video": video.name}
        audio = request.output_dir / "audio.wav"
        if infos["has_audio"] and extraire_audio(video, audio):
            sortie["audio"] = audio.name

        # Le pic déclaré est le maximum des deux instruments. Ils ne mesurent pas
        # la même chose et se disputent la première place selon la phase ; n'en
        # retenir qu'un sous-déclarerait le chiffre dont dépend l'admission.
        pic_rss = échantillonneur.pic_bytes
        pic_profil = journal.pic_profil_bytes()
        self._pic_job = max(pic_rss, pic_profil, peak_rss_bytes() or 0)

        metrics: dict[str, Any] = {
            "duration_ms": mur_ms,
            "peak_memory_bytes": self._pic_job,
            "peak_source": "rss-fils" if pic_rss >= pic_profil else "profil-metal",
            "peak_rss_child_bytes": pic_rss,
            "peak_profile_bytes": pic_profil,
            "frames_requested": args.frames,
            "steps": args.steps,
            "layers": args.layers,
            "reuse": args.reuse,
            "ssd_streaming": args.ssd_streaming,
            "seed": args.seed,
            **infos,
        }
        if args.ignores:
            metrics["ignored_contract_fields"] = args.ignores
        if journal.ssd:
            metrics["ssd_stream"] = journal.ssd
        metrics["phases"] = [
            {"label": p.libelle, "wall_s": p.wall_s, "peak_bytes": p.peak_bytes}
            for p in journal.phases
        ]
        if (encodeur := journal.phase("text encoder")) is not None:
            metrics["text_encode_s"] = encodeur.wall_s
        if (dit := journal.phase("denoise")) is not None:
            metrics["denoise_s"] = dit.wall_s

        progress(100, "terminé")
        return InferResult(output=sortie, metrics=metrics)

    # -- mesures -------------------------------------------------------------

    def peak_memory_bytes(self) -> int | None:
        """Pic du dernier job, pas de ce processus-ci.

        Le défaut de `Worker` rendrait le RSS du worker Python, qui ne fait que
        lancer un binaire et lire ses lignes : quelques dizaines de mébioctets,
        soit un chiffre juste sur le mauvais processus. Le contrôle d'admission
        laisserait alors entrer n'importe quoi à côté.
        """
        return self._pic_job or peak_rss_bytes()

    def unload(self) -> None:
        # Rien à rendre : le binaire est mort avec son job, et rien de ce modèle
        # ne reste en mémoire entre deux appels. C'est aussi pourquoi ce variant
        # ne gagne rien à rester résident.
        self._pic_job = 0


if __name__ == "__main__":
    raise SystemExit(main(H3Worker))
