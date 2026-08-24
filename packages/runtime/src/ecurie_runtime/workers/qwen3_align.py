"""Adaptateur mlx-audio, chemin **quand chaque mot est prononcé**.

Neuvième emploi du runtime `mlx-audio`, et le premier qui reçoit le texte au lieu
de le produire. La différence avec `moss_transcribe`, qui tourne dans le même
environnement, tient en une phrase : celui-là décide des mots, celui-ci n'a le
droit de rien décider du tout. Le texte fourni fait loi — le modèle ne saute pas
un mot qu'on lui donne, n'en ajoute pas un qu'on a oublié, et ne signale pas que
le texte divergeait de l'enregistrement.

**Trois pièges ont été mesurés avant que ce fichier n'existe, et chacun a laissé
du code ici.**

Le premier est l'aiguillage du chargement. `config.json` annonce `model_type:
qwen3_asr` à la racine, si bien que `load_model` passe par la classe ASR de
mlx-audio ; c'est cette classe qui lit `thinker_config.model_type` et instancie
l'aligneur. L'indirection n'est garantie par aucune version, et le chargement se
fait avec `strict=False` : si l'amont la retire, on obtiendrait un modèle de
transcription posé sur des poids d'aligneur, en silence. D'où le contrôle de
surface dans `load()`, qui coûte trois `hasattr` et refuse plutôt que de servir.

Le deuxième est le ré-appariement de la ponctuation. Le modèle ne rend pas les
mots qu'on lui a donnés : il rend sa propre forme nettoyée, où « 12,5 » devient
« 125 », « Marie-Josée » devient « MarieJosée », et où « : », « % » et « — »
disparaissent entièrement. Sur un texte français dur, treize jetons source
deviennent dix unités rendues — un `zip(texte.split(), mots)` naïf se décale dès
le quatrième et produirait des horodatages silencieusement attribués au mauvais
mot. `surfaces_originales` rejoue le nettoyage **de la bibliothèque**, jamais une
approximation maison, et l'adaptateur retombe sur les jetons du modèle en le
disant si les comptes ne tombent pas.

Le troisième est que l'alignement forcé échoue **sans rien casser**. Sur un texte
dont une phrase revient à l'identique, le modèle colle sur la mauvaise occurrence
et tasse le reste sur un instant : mesuré sur `assets/parole-tts.wav`, les huit
premiers mots atterrissent à 10,56 s d'un fichier où la parole commence à 1,28 s.
Aucune exception, aucun fichier vide, un job vert. `span_seconds` est au contrat
pour cela, et l'adaptateur avertit quand l'empan s'effondre sous la durée écoutée.
"""

import gc
import json
import time
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
MOTS_NAME = "mots.json"
SOUS_TITRES = {"srt": "sous-titres.srt", "vtt": "sous-titres.vtt"}
SAMPLE_RATE = 16_000

# Le plafond dur du réseau, calculé et non deviné : `classify_num` 5000 classes ×
# `timestamp_segment_time` 80 ms. Au-delà, aucune valeur d'horodatage n'est
# représentable, quelle que soit la mémoire disponible. Le contrat s'arrête à
# 300 s, borne prudente de la carte Qwen ; ce chiffre-ci est le mur derrière.
PLAFOND_DUR_S = 400.0

# Les deux langues dont le découpage en mots passe par un paquet tiers que l'env
# n'installe pas. Refusées à l'entrée plutôt que laissées lever un `ImportError`
# au milieu d'un job — c'est le même traitement que `task: translate` chez
# `moss_transcribe` : une demande qu'on ne peut pas honorer se refuse tôt.
DECOUPEURS_TIERS = {
    "japanese": "nagisa",
    "korean": "soynlp",
}

# En deçà de cette part de la durée écoutée, l'empan des horodatages est tenu
# pour suspect. Le seuil est bas à dessein : un enregistrement qui contient
# vraiment quelques mots au milieu d'un long silence est légitime, et un
# avertissement de trop coûte moins qu'un effondrement passé inaperçu.
EMPAN_SUSPECT = 0.5

# Regroupement des mots en répliques de sous-titre. Ce sont des conventions de
# lisibilité, pas des mesures : deux lignes de 42 caractères est l'usage courant,
# et un silence d'une demi-seconde marque une coupure naturelle.
CARACTERES_PAR_REPLIQUE = 84
SECONDES_PAR_REPLIQUE = 6.0
SILENCE_COUPANT = 0.5

# Ce qui ferme une réplique quel que soit le compte de caractères. Trouvé en
# relisant la sortie du banc, et pas avant : sans cette règle, une réplique
# enjambait « … sans un mot. On lui a demandé … », c'est-à-dire deux phrases dont
# la seconde commence au milieu d'une ligne.
FINS_DE_PHRASE = (".", "!", "?", "…", ":")


def _import_runtime() -> tuple[Any, Any, Any]:
    try:
        import mlx.core as mx
        from mlx_audio.stt.utils import load_audio, load_model
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-audio indisponible dans cet environnement ({exc}) — "
            f"le reconstruire avec `{REPAIR}`"
        ) from exc
    return mx, load_model, load_audio


# --- ce qui se calcule sans les poids ----------------------------------------


def surfaces_originales(texte: str, nettoyer: Any, decouper: Any) -> list[str]:
    """Les formes du texte source, une par unité que le modèle rendra.

    `nettoyer` et `decouper` sont `clean_token` et `split_segment_with_chinese`
    de la bibliothèque : ils sont passés en argument plutôt qu'importés pour que
    cette fonction se teste sans mlx-audio, et surtout pour qu'on ne soit jamais
    tenté de les réécrire. La règle de découpage du modèle est la sienne ; la
    deviner, c'est se décaler d'un mot le jour où elle bouge.

    Un jeton qui se nettoie à vide — « % », « — », « : » — ne donne aucune unité
    et disparaît donc de la liste, exactement comme il disparaît du côté du
    modèle. Un jeton qui se découpe en plusieurs unités rend ses morceaux, car
    aucune forme originale ne leur correspond une à une.
    """
    surfaces: list[str] = []
    for jeton in texte.split():
        nettoyé = nettoyer(jeton)
        if not nettoyé:
            continue
        morceaux = decouper(nettoyé)
        surfaces.extend([jeton] if len(morceaux) == 1 else morceaux)
    return surfaces


def empan(mots: list[dict[str, Any]]) -> float:
    """Fin de la dernière unité moins début de la première.

    La sonde du contrat. Elle ne dit pas que l'alignement est juste — rien ne le
    dit —, elle dit qu'il ne s'est pas effondré.
    """
    if not mots:
        return 0.0
    return max(0.0, round(float(mots[-1]["end"]) - float(mots[0]["start"]), 3))


def repliques(
    mots: list[dict[str, Any]],
    *,
    caracteres: int = CARACTERES_PAR_REPLIQUE,
    secondes: float = SECONDES_PAR_REPLIQUE,
    silence: float = SILENCE_COUPANT,
) -> list[dict[str, Any]]:
    """Groupe les unités en répliques lisibles.

    Un sous-titre est une **interprétation** de l'alignement, pas l'alignement :
    c'est pourquoi `subtitle_format` vaut `none` par défaut et que la liste des
    mots reste la sortie exacte. Quatre raisons de couper : une phrase qui se
    termine, un silence, une durée, une longueur de ligne. La première prime,
    parce qu'une réplique qui enjambe un point se lit deux fois.

    Une phrase plus longue que `caracteres` se coupe quand même en son milieu :
    c'est la règle de lisibilité qui gagne alors, et la fin de phrase n'est pas
    un droit à une réplique de trois lignes.
    """
    groupes: list[dict[str, Any]] = []
    ferme = False
    for mot in mots:
        texte = str(mot["text"])
        début, fin = float(mot["start"]), float(mot["end"])
        courant = None if ferme else (groupes[-1] if groupes else None)
        if courant is not None and (
            début - float(courant["end"]) > silence
            or fin - float(courant["start"]) > secondes
            or len(courant["text"]) + 1 + len(texte) > caracteres
        ):
            courant = None
        if courant is None:
            groupes.append({"text": texte, "start": début, "end": fin})
        else:
            courant["text"] = f"{courant['text']} {texte}"
            courant["end"] = fin
        ferme = texte.rstrip("»\"')]").endswith(FINS_DE_PHRASE)
    return groupes


def _horodate(secondes: float, virgule: str) -> str:
    total = max(0.0, float(secondes))
    heures, reste = divmod(int(total), 3600)
    minutes, s = divmod(reste, 60)
    millièmes = int(round((total - int(total)) * 1000))
    if millièmes == 1000:  # 3,9996 s arrondirait à « 03,1000 » sans ce report
        millièmes, s = 0, s + 1
        if s == 60:
            s, minutes = 0, minutes + 1
        if minutes == 60:
            minutes, heures = 0, heures + 1
    return f"{heures:02d}:{minutes:02d}:{s:02d}{virgule}{millièmes:03d}"


def en_srt(mots: list[dict[str, Any]]) -> str:
    lignes: list[str] = []
    for index, groupe in enumerate(repliques(mots), start=1):
        début = _horodate(groupe["start"], ",")
        fin = _horodate(groupe["end"], ",")
        lignes += [str(index), f"{début} --> {fin}", groupe["text"], ""]
    return "\n".join(lignes)


def en_vtt(mots: list[dict[str, Any]]) -> str:
    lignes = ["WEBVTT", ""]
    for groupe in repliques(mots):
        début = _horodate(groupe["start"], ".")
        fin = _horodate(groupe["end"], ".")
        lignes += [f"{début} --> {fin}", groupe["text"], ""]
    return "\n".join(lignes)


# --- l'adaptateur ------------------------------------------------------------


class Qwen3AlignWorker(Worker):
    """Alignement forcé : le texte est donné, on rend quand il est dit."""

    name = "qwen3-align"

    def __init__(self) -> None:
        self.mx: Any = None
        self.load_audio: Any = None
        self.model: Any = None
        self.defaults: dict[str, Any] = {}
        self.langues: list[str] = []
        self._peak_load = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        mx, load_model, load_audio = _import_runtime()
        self.mx = mx
        self.load_audio = load_audio
        self.defaults = dict(variant.get("defaults") or {})

        brut = str(variant.get("weights_path") or "").strip()
        ref = variant.get("ref") or "<ref>"
        if not brut or not Path(brut).is_dir():
            raise WorkerError(
                f"poids absents : {brut or '(chemin vide)'} n'est pas un dossier — "
                f"télécharger avec : ecurie pull {ref}"
            )
        try:
            model = load_model(brut)
        except Exception as exc:  # noqa: BLE001 — remonte avec la réparation
            raise WorkerError(f"chargement impossible : {type(exc).__name__}: {exc}") from exc

        # Le contrôle que l'en-tête du module explique : `load_model` passe par
        # la classe ASR, qui aiguille vers l'aligneur d'après le `model_type` du
        # sous-config. Le jour où cette indirection disparaît, on récupère un
        # modèle de transcription chargé en `strict=False` sur des poids
        # d'aligneur — sans exception, et avec une sortie qui n'est pas celle du
        # contrat. Trois attributs suffisent à le voir tout de suite.
        manquants = [
            nom
            for nom in ("generate", "aligner_processor", "get_supported_languages")
            if not hasattr(model, nom)
        ]
        if manquants:
            raise WorkerError(
                f"{ref} : les poids se chargent mais l'objet obtenu n'est pas un aligneur "
                f"(il lui manque {', '.join(manquants)}). mlx-audio a probablement changé "
                "son aiguillage de `model_type` ; ne pas servir de transcription sous le "
                "nom d'un alignement."
            )

        self.model = model
        self.langues = list(model.get_supported_languages() or [])
        self._peak_load = self._pic() or 0
        return {"languages": self.langues, "versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")

        avertissements: list[str] = []

        # L'ordre est celui du coût : ce qui se refuse sans toucher au disque se
        # refuse d'abord.
        texte = str(self._reglage(request, "text", "") or "").strip()
        if not texte:
            raise WorkerError(
                "« text » est obligatoire : cette capacité aligne un texte donné, elle ne "
                "le transcrit pas. Pour obtenir les mots, passer d'abord par speech-to-text."
            )
        langue = str(self._reglage(request, "language", "french") or "french").strip().lower()
        self._exiger_decoupeur(langue)
        if self.langues and langue not in self.langues:
            avertissements.append(
                f"« {langue} » ne figure pas dans les langues annoncées par ce modèle "
                f"({', '.join(self.langues)}) : le texte sera découpé aux espaces. "
                "Ces valeurs sont des noms anglais de langue, pas des codes ISO."
            )

        max_seconds = float(self._reglage(request, "max_seconds", 300))
        if max_seconds > PLAFOND_DUR_S:
            raise WorkerError(
                f"max_seconds={max_seconds:g} dépasse le plafond du réseau "
                f"({PLAFOND_DUR_S:g} s = 5000 classes × 80 ms) : au-delà, aucun horodatage "
                "n'est représentable et la sortie serait saturée sans le dire."
            )

        format_st = str(self._reglage(request, "subtitle_format", "none") or "none").lower()
        if format_st not in ("none", *SOUS_TITRES):
            raise WorkerError(f"subtitle_format={format_st!r} inconnu — attendu none, srt ou vtt")

        audio_path = self._fichier(request, "audio")

        progress(8, "lecture de l'enregistrement")
        signal = self.load_audio(str(audio_path), sr=SAMPLE_RATE)
        échantillons = int(max_seconds * SAMPLE_RATE)
        if signal.shape[0] > échantillons:
            entier = signal.shape[0] / SAMPLE_RATE
            signal = signal[:échantillons]
            avertissements.append(
                f"enregistrement tronqué à {max_seconds:g} s sur {entier:.1f} s : tout mot du "
                "texte prononcé après cette borne n'a plus d'audio où se poser."
            )
        durée = float(signal.shape[0]) / SAMPLE_RATE

        # Remis à zéro par job : `get_peak_memory` compte depuis le début du
        # processus, sinon le pic du chargement masquerait celui de l'alignement.
        self.mx.reset_peak_memory()
        progress(25, f"alignement en cours ({durée:.0f} s, {len(texte.split())} jetons)")
        début = time.monotonic()
        try:
            résultat = self.model.generate(audio=signal, text=texte, language=langue)
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte
            raise WorkerError(f"alignement impossible : {type(exc).__name__}: {exc}") from exc
        calcul = time.monotonic() - début

        mots = [
            {
                "text": str(m.get("text") or ""),
                "start": round(float(m["start"]), 3),
                "end": round(float(m["end"]), 3),
            }
            for m in (getattr(résultat, "segments", None) or [])
        ]
        mots, reproche = self._rendre_les_formes_donnees(texte, mots)
        if reproche:
            avertissements.append(reproche)

        étendue = empan(mots)
        if mots and durée > 0 and étendue < EMPAN_SUSPECT * durée:
            avertissements.append(
                f"empan de {étendue:.2f} s pour {durée:.2f} s écoutées : l'alignement s'est "
                "probablement effondré. C'est ce que produit un texte qui diverge de "
                "l'enregistrement, ou dont une phrase revient mot pour mot — le modèle "
                "colle alors sur une occurrence et tasse le reste sur un instant."
            )

        progress(90, "écriture")
        (request.output_dir / MOTS_NAME).write_text(
            json.dumps(mots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        sortie: dict[str, Any] = {
            "words": MOTS_NAME,
            "units": len(mots),
            "span_seconds": étendue,
            "duration_seconds": round(durée, 3),
        }
        if format_st in SOUS_TITRES:
            nom = SOUS_TITRES[format_st]
            rendu = en_srt(mots) if format_st == "srt" else en_vtt(mots)
            (request.output_dir / nom).write_text(rendu, encoding="utf-8")
            sortie["subtitles"] = nom

        return InferResult(
            output=sortie,
            metrics={
                "units": len(mots),
                "span_seconds": étendue,
                # Le facteur temps réel, comme pour la transcription : temps de
                # calcul par seconde écoutée. C'est le chiffre comparable entre
                # modèles de cette capacité.
                "rtf": round(calcul / durée, 5) if durée > 0 else None,
                "infer_ms": int(calcul * 1000),
                "avertissements": avertissements,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self.model = None
        self.langues = []
        self._peak_load = 0
        gc.collect()
        if self.mx is not None:
            self.mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        """MLX sait exactement ce qu'il a réservé ; le RSS n'est qu'un repli.

        Aucun octet de ce chemin ne passe par Metal sans passer par MLX, donc
        `get_peak_memory()` est la mesure juste et non une approximation.
        """
        pic = self._pic()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- détails -------------------------------------------------------------

    def _rendre_les_formes_donnees(
        self, texte: str, mots: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str]:
        """Remet la ponctuation du texte source sur les unités rendues.

        Le contrat promet « le texte qui a été fourni, ponctuation comprise » :
        rendre « MarieJosée » là où l'utilisateur a écrit « Marie-Josée » serait
        lui livrer une sortie qu'il ne reconnaît pas. Le ré-appariement se fait
        par le nettoyage de la bibliothèque, et il s'abstient dès que les comptes
        ne tombent pas — un décalage silencieux attribuerait chaque horodatage au
        mot suivant, ce qui est pire que de rendre la forme nettoyée.
        """
        processeur = getattr(self.model, "aligner_processor", None)
        if processeur is None or not mots:
            return mots, ""
        try:
            surfaces = surfaces_originales(
                texte, processeur.clean_token, processeur.split_segment_with_chinese
            )
        except Exception as exc:  # noqa: BLE001 — un ré-appariement raté ne perd pas le job
            return mots, f"formes originales non rétablies ({type(exc).__name__}) : {exc}"
        if len(surfaces) != len(mots):
            return mots, (
                f"formes originales non rétablies : {len(surfaces)} jetons reconstruits "
                f"pour {len(mots)} unités rendues. Les unités portent la forme nettoyée du "
                "modèle, sans ponctuation ni traits d'union."
            )
        return [{**mot, "text": surface} for surface, mot in zip(surfaces, mots, strict=True)], ""

    def _exiger_decoupeur(self, langue: str) -> None:
        """Refuse une langue dont le découpeur n'est pas installé.

        Le refus est vérifié et non décrété : la roue cp313 arm64 de `nagisa`
        existe, celle de `soynlp` est universelle, et le jour où l'une des deux
        entre dans l'env, cette porte s'ouvre d'elle-même. Tant qu'elle n'y est
        pas, `encode_timestamp` lèverait un `ImportError` au milieu du job.
        """
        paquet = DECOUPEURS_TIERS.get(langue)
        if paquet is None:
            return
        try:
            __import__(paquet)
        except ImportError as exc:
            raise WorkerError(
                f"« {langue} » demande le découpeur {paquet}, absent de l'environnement "
                f"{ENV_NAME} ({exc}). Le déclarer dans runtimes/{ENV_NAME}/pyproject.toml "
                f"puis `{REPAIR}` — c'est un geste délibéré, cet env porte neuf capacités "
                "déjà mesurées."
            ) from exc

    def _fichier(self, request: InferRequest, nom: str) -> Path:
        brut = str(self._reglage(request, nom, "") or "").strip()
        if not brut:
            raise WorkerError(f"« {nom} » est obligatoire")
        chemin = Path(brut).expanduser()
        if not chemin.is_absolute():
            chemin = request.output_dir / chemin
        if not chemin.is_file():
            raise WorkerError(f"{nom} introuvable : {chemin}")
        return chemin

    def _reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        if self.defaults.get(nom) is not None:
            return self.defaults[nom]
        return defaut

    def _pic(self) -> int | None:
        if self.mx is None:
            return None
        try:
            return int(self.mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne fait pas échouer un job
            return None

    def _versions(self) -> dict[str, str]:
        versions = {}
        for nom, module in (("mlx", "mlx.core"), ("mlx-audio", "mlx_audio")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


if __name__ == "__main__":
    raise SystemExit(main(Qwen3AlignWorker))
