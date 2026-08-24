"""Adaptateur `esm-torch` : une suite de lettres entre, un vecteur de 1280 nombres sort.

Le patron de `uniface_embed` et de `dinov3_embed` transposé à du texte qui n'est
pas une langue. Il n'y a ici ni image à composer, ni définition à aligner sur un
pas de réseau : l'entrée est une chaîne, et tout le soin porte sur ce qu'elle
contient réellement.

**Quatre défauts mesurés sur cette machine décident de ce fichier**, et aucun ne
se serait vu sans exécuter.

*Le pooler tiré au sort.* `AutoModel.from_pretrained` sur ce dépôt imprime
« pooler.dense.weight | MISSING » et initialise **au hasard** un pooler que le
checkpoint ne contient pas. Mesuré : deux chargements du même fichier de poids
rendent des `pooler_output` à **−0,038** de cosinus l'un de l'autre, écart absolu
maximum 0,82. Un adaptateur qui aurait implémenté `cls` par `pooler_output`
aurait servi des nombres tirés au sort, différents à chaque démarrage de
processus, et rien dans la sortie ne l'aurait dit — le cosinus serait resté dans
[-1, 1] et aurait paru plausible. D'où `add_pooling_layer=False`, `cls` implémenté
par `last_hidden_state[:, 0]`, et un contrôle après chargement : si une version
future de `transformers` ignorait le mot-clé, le worker refuse au lieu de servir.

*Une lettre hors alphabet raccourcit la séquence en silence.* `EsmTokenizer` est
un tokenizer lent qui regroupe les caractères inconnus : mesuré, `MJQ`, `MJJQ` et
`MJJJQ` rendent **tous les trois** `[<cls>, M, <unk>, Q, <eos>]`. Une suite de
lettres étrangères, quelle que soit sa longueur, devient donc un seul jeton — la
séquence rétrécit, `length` ment, et le vecteur est celui d'autre chose. On
refuse plutôt qu'on encode, en nommant les caractères fautifs et leur position.

*Une séquence d'ADN passe sans un mot.* A, C, G et T sont quatre acides aminés
valides. Mesuré : un fragment d'ADN de 75 bases s'encode sans erreur, rend un
vecteur de norme 1 et un cosinus de 0,531 avec l'ubiquitine — c'est-à-dire dans
la plage exacte où se trouvent deux protéines sans rapport. Aucune vérification
ne peut l'écarter avec certitude ; le worker le **dit** en avertissement, parce
qu'un job qui passe est ici plus dangereux qu'un job qui échoue.

*Le tokenizer ne tronque jamais.* `tokenizer_config.json` déclare
`model_max_length: 1e30`. Sans `truncation=True, max_length=…` explicites, une
séquence de cent mille résidus part telle quelle. `max_length` est le seul
endroit du contrat qui protège quelque chose de réel.

**Ce que ce fichier ne partage PAS avec `torch_vision.py`, et pourquoi.** Le
socle de vision porte les mêmes compteurs MPS et la même mesure du pic, mais ses
messages de réparation nomment `ecurie env sync torch-vision` et son
`ensure_mps` renvoie vers `runtimes/torch-vision/.venv`. Hériter d'ici enverrait
réparer le mauvais environnement, ce qui est la pire forme d'un message d'erreur
juste. Les quarante lignes sont donc redites, comme `chronos_forecast` les redit.

**Le pic est plat, et c'est mesuré.** `driver_allocated_memory()` rend
3 330 752 512 octets après chargement, 3 347 824 640 après une passe à 76
résidus, 3 347 841 024 après une passe à 2048 — seize kibioctets d'écart entre
les deux extrêmes. C'est la latence qui suit la longueur (72, 116 puis 184 ms à
76, 129 et 238 résidus), pas la mémoire. Le pic retenu reste le maximum des
relevés du pilote **et** du RSS : le RSS ne voit pas la mémoire Metal, mais sur
mémoire unifiée les deux comptent.

Rien de torch ni de transformers n'est importé au niveau du module (voir
`workers/__init__.py`) : la CI importe tous les adaptateurs sans Apple Silicon.
"""

import gc
import json
import math
import os
from collections.abc import Sequence
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

ENV_NAME = "esm-torch"
REPAIR = f"ecurie env sync {ENV_NAME}"

SORTIE_JSON = "embedding.json"

AGREGATIONS = ("mean", "cls")

# Défauts du contrat, repris pour le cas où ni le job ni le manifeste ne disent rien.
DEFAUT_MAX_LENGTH = 1024

# L'alphabet de repli, quand le tokenizer ne se laisse pas interroger. Il est
# recopié de `vocab.txt` du dépôt — vingt standards, X pour un résidu inconnu, B
# et Z pour les deux ambiguïtés classiques, U pour la sélénocystéine, O pour la
# pyrrolysine, puis les deux caractères d'alignement. Le worker lit d'abord le
# vocabulaire réel du modèle : cette constante n'est là que pour que les
# fonctions pures se testent sans poids.
ALPHABET_ESM = frozenset("LAGVSERTIDPKQNFYMHWCXBUZO.-")

# Les caractères d'alignement. Ils SONT dans le vocabulaire, donc ils ne sont pas
# refusés — mais leur présence signale qu'on encode une ligne d'alignement et non
# une séquence, ce qui n'est pas la même chose et ne se voit pas dans le vecteur.
ALIGNEMENT = frozenset(".-")

# Les quatre bases de l'ADN et les quatre de l'ARN sont toutes des lettres
# valides d'acides aminés. Une séquence nucléotidique s'encode donc sans la
# moindre erreur ; c'est ce seuil qui décide de le dire.
NUCLEOTIDES = (frozenset("ACGT"), frozenset("ACGU"))
NUCLEOTIDES_MIN = 24


# --- ce qui se vérifie sans poids ---------------------------------------------
#
# Rien ici ne touche torch ni transformers : c'est ce qui rend ces fonctions
# vérifiables en CI, sans Apple Silicon, sans venv de runtime et sans poids.


def lire_sequence(brut: Any, champ: str) -> tuple[str, list[str]]:
    """Le texte saisi vers une séquence en lettres majuscules, et ce qu'on a dû corriger.

    Trois nettoyages, dans cet ordre. Les lignes d'en-tête FASTA — celles qui
    commencent par `>` ou `;` — sont retirées : c'est la forme sous laquelle une
    séquence se copie depuis à peu près n'importe quelle base, et refuser le
    chevron enverrait l'utilisateur éditer à la main ce que l'adaptateur sait
    faire. Les blancs, retours à la ligne et numéros de colonne sont ôtés ensuite
    — un FASTA est découpé en lignes de soixante, et un `\\n` au milieu ferait un
    jeton de plus. La casse est enfin remontée, le vocabulaire d'ESM n'ayant que
    des majuscules.

    Un enregistrement FASTA qui en contient plusieurs n'est pas silencieusement
    concaténé : ce serait fabriquer une protéine qui n'existe pas, et le cosinus
    qui en sortirait serait celui de cette chimère. Seul le premier est encodé,
    et l'avertissement le dit.
    """
    texte = str(brut or "")
    if not texte.strip():
        raise WorkerError(f"aucune séquence en entrée : le champ `{champ}` est vide")

    avertissements: list[str] = []
    enregistrements: list[list[str]] = []
    courant: list[str] = []
    entetes = 0
    for ligne in texte.splitlines():
        dépouillée = ligne.strip()
        if dépouillée.startswith((">", ";")):
            entetes += 1
            if courant:
                enregistrements.append(courant)
                courant = []
            continue
        if dépouillée:
            courant.append(dépouillée)
    if courant:
        enregistrements.append(courant)

    if not enregistrements:
        raise WorkerError(f"`{champ}` ne contient que des lignes d'en-tête FASTA, aucune séquence")
    if len(enregistrements) > 1:
        avertissements.append(
            f"`{champ}` porte {len(enregistrements)} enregistrements FASTA : seul le "
            "premier est encodé. Concaténer les autres fabriquerait une protéine qui "
            "n'existe pas, et son vecteur n'appartiendrait à aucune des deux"
        )

    séquence = "".join(enregistrements[0]).upper()
    # Les blancs internes sont retirés APRÈS le découpage en lignes : le
    # tokenizer d'ESM traite l'espace comme un séparateur de jetons, si bien
    # qu'une séquence recopiée par blocs de dix passerait sans erreur et
    # encoderait autre chose.
    séquence = "".join(séquence.split())
    if not séquence:
        raise WorkerError(f"`{champ}` ne contient aucun résidu après nettoyage")
    if entetes:
        avertissements.append(
            f"`{champ}` lu comme un FASTA : {entetes} ligne(s) d'en-tête retirée(s)"
        )
    return séquence, avertissements


def verifier_alphabet(sequence: str, alphabet: frozenset[str], champ: str) -> list[str]:
    """Refuse ce que le modèle ne sait pas lire, et signale ce qu'il lit mal.

    Refuser plutôt qu'encoder, et c'est LA décision de ce fichier. `EsmTokenizer`
    regroupe les caractères inconnus : mesuré sur les poids livrés, `MJQ`, `MJJQ`
    et `MJJJQ` rendent tous les trois la même suite de cinq jetons. Une lettre
    hors alphabet ne devient donc pas un résidu inconnu — elle **efface** la
    longueur de ce qui l'entoure, sans qu'aucune sortie ne s'en aperçoive. Le
    vecteur reste de norme 1, `length` annonce un nombre plausible, et le cosinus
    est celui d'une autre protéine.

    Ce qui est dans le vocabulaire n'est pas refusé, même quand c'est douteux :
    `.` et `-` en font partie, et une ligne d'alignement s'encode donc — mais
    l'avertissement dit que ce n'est pas la séquence.
    """
    intrus = sorted({c for c in sequence if c not in alphabet})
    if intrus:
        premières = {c: sequence.index(c) + 1 for c in intrus[:5]}
        détail = ", ".join(f"{c!r} en position {p}" for c, p in premières.items())
        raise WorkerError(
            f"`{champ}` : {len(intrus)} caractère(s) hors de l'alphabet du modèle — "
            f"{détail}. Attendu le code à une lettre des acides aminés "
            f"({''.join(sorted(alphabet - ALIGNEMENT))}). Ils ne sont pas encodés en "
            "résidu inconnu : le tokenizer d'ESM regroupe les caractères qu'il ignore, "
            "si bien qu'une suite étrangère de trois lettres devient un seul jeton et "
            "raccourcit la séquence sans rien dire"
        )

    avertissements: list[str] = []
    gaps = sorted(set(sequence) & ALIGNEMENT)
    if gaps:
        avertissements.append(
            f"`{champ}` contient {sum(sequence.count(c) for c in gaps)} caractère(s) "
            f"d'alignement ({', '.join(repr(c) for c in gaps)}) : ils sont dans le "
            "vocabulaire du modèle et seront encodés comme des résidus. Une ligne "
            "d'alignement n'est pas la séquence qu'elle représente"
        )
    if ressemble_a_un_acide_nucleique(sequence):
        avertissements.append(
            f"`{champ}` ne contient que des lettres de nucléotides sur "
            f"{len(sequence)} caractères : c'est peut-être de l'ADN ou de l'ARN, et "
            "A, C, G, T et U sont aussi des acides aminés valides. Une telle entrée "
            "s'encode sans erreur — mesuré, un fragment d'ADN rend un vecteur de "
            "norme 1 à 0,531 de cosinus de l'ubiquitine, soit exactement la plage de "
            "deux protéines sans rapport. Ce modèle n'encode que des protéines"
        )
    return avertissements


def ressemble_a_un_acide_nucleique(sequence: str) -> bool:
    """Vrai quand la séquence pourrait être de l'ADN ou de l'ARN plutôt qu'une protéine.

    Un seuil de longueur, parce qu'un peptide court peut légitimement n'employer
    que ces quatre lettres — `CAT`, `GATTACA` sont des protéines possibles. Passé
    deux douzaines de résidus, la coïncidence cesse d'en être une.
    """
    if len(sequence) < NUCLEOTIDES_MIN:
        return False
    lettres = set(sequence)
    return any(lettres <= alphabet for alphabet in NUCLEOTIDES)


def verifier_agregation(demandée: Any) -> str:
    """L'agrégation du variant, ou un refus qui dit quoi corriger.

    Elle appartient au variant et non au contrat, pour la raison qui l'a mise là
    chez `image-embed` : ce sont deux espaces vectoriels et non deux réglages.
    Mesuré ici sur le chemin livré, la paire ubiquitine / lysozyme rend **0,6649**
    en `mean` et **0,9615** en `cls` — mêmes protéines, mêmes poids, et une
    échelle qui n'a plus rien à voir. Les présenter comme une préférence dans le
    formulaire de l'UI ferait comparer deux nombres qui ne se comparent pas.

    `cls` est ici `last_hidden_state[:, 0]`, jamais `pooler_output` : voir
    l'en-tête du module, le pooler de ce checkpoint est tiré au sort.
    """
    valeur = str(demandée or "mean").strip().lower()
    if valeur not in AGREGATIONS:
        raise WorkerError(
            f"agrégation inconnue : {valeur!r} — attendu {' ou '.join(AGREGATIONS)} "
            "dans `options.pooling` du manifeste"
        )
    return valeur


def plafond_residus(demandé: Any, defaut: int = DEFAUT_MAX_LENGTH) -> int:
    """Le nombre de résidus soumis au réseau, borné à ce que le contrat autorise.

    Redit ici alors que le contrat le borne déjà, et pour la raison que
    `chronos_forecast` a écrite avant : un worker peut être appelé sans passer
    par la validation du contrat. C'est alors ici, et nulle part ailleurs, que
    le tokenizer cesse de recevoir une séquence de cent mille résidus.
    """
    if demandé is None:
        return defaut
    try:
        valeur = int(demandé)
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"max_length : entier attendu, reçu {demandé!r}") from exc
    if not 16 <= valeur <= 2048:
        raise WorkerError(f"max_length = {valeur} : le contrat borne ce paramètre à [16 ; 2048]")
    return valeur


def norme(vecteur: Sequence[float]) -> float:
    return math.sqrt(sum(float(v) * float(v) for v in vecteur))


def normaliser_l2(vecteur: list[float]) -> list[float]:
    """Norme 1, ou le vecteur tel quel s'il est nul — diviser par zéro ne dit rien."""
    n = norme(vecteur)
    return [v / n for v in vecteur] if n > 0 else list(vecteur)


def cosinus(a: list[float], b: list[float]) -> float | None:
    """Cosinus entre deux vecteurs, ou None quand l'un des deux est nul."""
    if len(a) != len(b):
        raise WorkerError(
            f"vecteurs de longueurs différentes ({len(a)} et {len(b)}) : "
            "les deux séquences n'ont pas été encodées par le même modèle"
        )
    dénominateur = norme(a) * norme(b)
    if dénominateur <= 0:
        return None
    return round(sum(x * y for x, y in zip(a, b, strict=True)) / dénominateur, 4)


def weights_dir(variant: dict[str, Any]) -> Path:
    """Le dossier de poids transmis par le superviseur, vérifié avant usage.

    `from_pretrained` ne court-circuite le réseau que sur un **dossier** : sur une
    chaîne quelconque contenant un « / », il la prend pour un identifiant de dépôt
    et échoue par un message qui parle du Hub plutôt que des poids manquants.
    """
    brut = str(variant.get("weights_path") or "").strip()
    if not brut:
        raise WorkerError("aucun chemin de poids transmis par le superviseur")
    chemin = Path(brut)
    if not chemin.is_dir():
        raise WorkerError(
            f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
            "déjà vérifié, un worker ne télécharge jamais"
        )
    return chemin


# --- l'adaptateur -------------------------------------------------------------


class EsmEmbedWorker(Worker):
    """Une séquence d'acides aminés vers un vecteur — l'empreinte protéique du parc."""

    name = "esm-embed"

    def __init__(self) -> None:
        self.torch: Any = None
        self.model: Any = None
        self.tokenizer: Any = None
        self.defaults: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.identite: dict[str, Any] = {}
        self.pooling: str = "mean"
        self.dimensions: int = 0
        self.alphabet: frozenset[str] = ALPHABET_ESM
        self.speciaux: frozenset[int] = frozenset()
        self._peak_driver: int = 0

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        # Avant l'import de transformers, et pas après : `hub_kernels.py` lit
        # cette variable au chargement de son module. `modeling_esm.py` décore
        # son calcul rotatoire de `@use_kernel_forward_from_hub`, un chemin qui
        # irait chercher un noyau sur le Hub. Le paquet `kernels` n'est pas dans
        # l'env — les décorateurs sont donc déjà des identités — mais c'est un
        # extra d'un seul mot qu'une dépendance future pourrait tirer, et un
        # worker du parc n'a pas le droit de sortir sur le réseau.
        os.environ.setdefault("USE_HUB_KERNELS", "NO")

        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise WorkerError(
                f"runtime esm-torch indisponible dans cet environnement ({exc}) — `{REPAIR}`"
            ) from exc

        self.torch = torch
        self._exiger_mps(torch)
        self.defaults = dict(variant.get("defaults") or {})
        self.options = dict(variant.get("options") or {})
        self.pooling = verifier_agregation(self.options.get("pooling"))

        chemin = weights_dir(variant)
        if not (chemin / "vocab.txt").is_file():
            raise WorkerError(
                f"vocab.txt absent de {chemin} — ESM-2 publie un tokenizer lent, sans "
                "`tokenizer.json` : vérifier les `allow_patterns` du manifeste"
            )

        try:
            self.tokenizer = AutoTokenizer.from_pretrained(str(chemin))
            modèle = AutoModel.from_pretrained(
                str(chemin),
                # LE mot-clé de ce fichier. Sans lui, transformers initialise au
                # hasard un `pooler.dense` que le checkpoint ne contient pas :
                # mesuré, deux chargements des mêmes octets rendent des
                # `pooler_output` à −0,038 de cosinus l'un de l'autre.
                add_pooling_layer=False,
            )
        except Exception as exc:  # noqa: BLE001 — code amont : le message importe plus que le type
            raise WorkerError(
                f"chargement d'ESM impossible depuis {chemin} : {type(exc).__name__}: {exc}"
            ) from exc

        if getattr(modèle, "pooler", None) is not None:
            # Le mot-clé a été ignoré — une version de transformers l'aura
            # retiré. Refuser au chargement plutôt qu'au premier job : un pooler
            # présent ici veut dire qu'il a été tiré au sort, et `cls` servirait
            # alors des nombres différents à chaque démarrage du processus.
            raise WorkerError(
                "`add_pooling_layer=False` a été ignoré : ce checkpoint ne contient pas "
                "de pooler et transformers vient d'en initialiser un au hasard. Un "
                "vecteur produit dans cet état serait tiré au sort sans que rien ne le "
                "signale — corriger l'adaptateur avant de servir"
            )

        self.model = modèle.eval().to("mps")
        self.dimensions = int(getattr(modèle.config, "hidden_size", 0) or 0)
        self.alphabet = _alphabet_du_tokenizer(self.tokenizer)
        self.speciaux = frozenset(int(i) for i in (self.tokenizer.all_special_ids or []))
        self.identite = {
            "ref": variant.get("ref"),
            "repo": variant.get("repo"),
            "revision": variant.get("revision"),
            "architecture": str(getattr(modèle.config, "model_type", "") or "esm"),
            "implementation": f"transformers {_version('transformers')} / torch",
        }
        self.mps_counters()

        return {
            "pooling": self.pooling,
            "dimensions": self.dimensions,
            "alphabet": "".join(sorted(self.alphabet)),
            "position_embedding_type": str(
                getattr(modèle.config, "position_embedding_type", "") or ""
            ),
            "attn_implementation": str(getattr(modèle.config, "_attn_implementation", "") or ""),
            "versions": self.versions(),
        }

    def _exiger_mps(self, torch: Any) -> None:
        """MPS ou rien, et ce n'est pas de la rigidité.

        Le repli CPU serait numériquement juste — mesuré sur les trois séquences
        de la charge type, cosinus de 0,9999997 à 1,0000002 avec le chemin Metal,
        écart absolu maximum 1,25e-06 sur un vecteur normalisé — mais il est
        **plus lent** : 0,643 s contre 0,288 s pour ces trois séquences. Retomber
        en silence sur le processeur ferait donc perdre du temps tout en rendant
        faux le profil mesuré, qui est celui du chemin Metal.
        """
        if not torch.backends.mps.is_available():
            raise WorkerError(
                "backend MPS indisponible (torch.backends.mps.is_available() est faux) — "
                "cet adaptateur ne sert que sur Apple Silicon ; vérifier que "
                f"runtimes/{ENV_NAME}/.venv utilise un Python arm64"
            )

    # --- inférence -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None or self.torch is None:
            raise WorkerError("modèle non chargé")

        plafond = plafond_residus(self.reglage(request, "max_length", None))
        normaliser = bool(self.reglage(request, "normalize", True))

        progress(10, "lecture de la séquence")
        séquence, avertissements = self._preparer(request.get("sequence"), "sequence")

        progress(30, "encodage")
        vecteur, longueur, soumis = self._encoder(séquence, plafond)
        tronquée = longueur < len(séquence)
        if tronquée:
            avertissements.append(
                f"séquence tronquée à {longueur} résidus sur {len(séquence)} par "
                f"`max_length` : le vecteur n'encode pas la protéine entière"
            )
        if normaliser:
            vecteur = normaliser_l2(vecteur)

        similarité: float | None = None
        seconde: str | None = None
        comparaison = self.reglage(request, "compare_to", None)
        if comparaison:
            progress(60, "seconde séquence")
            seconde, autres = self._preparer(comparaison, "compare_to")
            avertissements.extend(autres)
            vecteur_b, longueur_b, _ = self._encoder(seconde, plafond)
            if longueur_b < len(seconde):
                avertissements.append(
                    f"`compare_to` tronquée à {longueur_b} résidus sur {len(seconde)} : "
                    "le cosinus porte sur un fragment"
                )
            # `normalize` ne change rien à ce nombre, et c'est voulu : un cosinus
            # est invariant d'échelle. Un job qui aurait rendu deux similarités
            # selon un réglage d'affichage aurait été incomparable avec lui-même.
            similarité = cosinus(vecteur, vecteur_b)

        progress(85, "écriture")
        document = {
            # Ce bloc décide de la lisibilité du fichier, et non de sa politesse.
            # Six largeurs circulent dans cette famille — 480, 640, 1280 chez
            # ESM-2, 960, 1152, 2560 chez ESM-C — et deux modèles peuvent
            # partager la même sans que le cosinus entre leurs vecteurs veuille
            # dire quoi que ce soit. Rien d'autre que ces lignes ne l'empêcherait.
            **self.identite,
            "pooling": self.pooling,
            "normalized": normaliser,
            "dimensions": len(vecteur),
            "length": longueur,
            "submitted_length": len(séquence),
            "truncated": tronquée,
            "max_length": plafond,
            "tokens": soumis,
            "compare_to_length": len(seconde) if seconde is not None else None,
            "similarity": similarité,
            "warnings": avertissements,
            "embedding": [round(float(v), 6) for v in vecteur],
        }
        chemin = request.output_dir / SORTIE_JSON
        chemin.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")

        sortie: dict[str, Any] = {
            "embedding": SORTIE_JSON,
            "dimensions": len(vecteur),
            "length": longueur,
            "truncated": tronquée,
        }
        if similarité is not None:
            sortie["similarity"] = similarité

        compteurs = self.mps_counters()
        métriques: dict[str, Any] = {
            # Le cosinus est répété dans les métriques, et ce n'est pas une
            # redondance : c'est le seul nombre lisible que ce job produise, et
            # la ligne de télémétrie n'affiche pas les sorties. Sans lui, un
            # terminal ne montre d'une empreinte qu'une taille de vecteur.
            **({"similarity": similarité} if similarité is not None else {}),
            "pooling": self.pooling,
            "length": longueur,
            "submitted_length": len(séquence),
            "tokens": soumis,
            "truncated": tronquée,
            "dimensions": len(vecteur),
            "vector_norm": round(norme(vecteur), 6),
            **compteurs,
            "peak_memory_bytes": self.peak_memory_bytes(),
        }
        if avertissements:
            métriques["warnings"] = avertissements
        return InferResult(output=sortie, metrics=métriques)

    # --- détails -------------------------------------------------------------

    def _preparer(self, brut: Any, champ: str) -> tuple[str, list[str]]:
        séquence, avertissements = lire_sequence(brut, champ)
        avertissements.extend(verifier_alphabet(séquence, self.alphabet, champ))
        return séquence, avertissements

    def _encoder(self, séquence: str, plafond: int) -> tuple[list[float], int, int]:
        """Une séquence vers un vecteur brut, sa longueur encodée et ses jetons.

        La troncature est passée **explicitement**, et c'est la seule chose qui
        l'obtienne : `model_max_length` vaut 10^30 dans ce dépôt, si bien que le
        tokenizer laisserait passer n'importe quelle longueur.
        """
        torch = self.torch
        encodage = self.tokenizer(
            séquence,
            return_tensors="pt",
            truncation=True,
            max_length=plafond + 2,  # <cls> et <eos> comptent dans la fenêtre
        )
        entrées = {clé: valeur.to("mps") for clé, valeur in encodage.items()}
        identifiants = encodage["input_ids"][0].tolist()

        with torch.no_grad():
            états = self.model(**entrées).last_hidden_state

        # Le masque exclut `<cls>`, `<eos>` et `<pad>` de la moyenne. L'effet est
        # petit et mesuré : sur les trois paires de la charge type, les laisser
        # entrer déplace les cosinus de 0,6649 à 0,6819, de 0,5834 à 0,6006 et de
        # 0,7783 à 0,7823 — au plus +0,0172, et l'ordre des paires est préservé.
        # Petit parce que ce contrat encode UNE séquence par appel et ne
        # rembourre donc jamais. Il est écrit quand même : `length` compte les
        # résidus et non les marqueurs, et le jour où quelqu'un batchera, le
        # `<pad>` reprendra toute sa place dans la moyenne.
        utiles = [0 if i in self.speciaux else 1 for i in identifiants]
        masque = torch.tensor([utiles], device=états.device, dtype=états.dtype)
        présents = encodage["attention_mask"][0].tolist()
        masque = masque * torch.tensor([présents], device=états.device, dtype=états.dtype)
        longueur = int(masque.sum().item())
        if longueur <= 0:
            raise WorkerError(
                "aucun résidu à encoder après retrait des jetons spéciaux — la séquence "
                "ne contenait que des marqueurs"
            )

        if self.pooling == "cls":
            # `last_hidden_state[:, 0]`, jamais `pooler_output` : voir l'en-tête.
            vecteur = états[:, 0]
        else:
            poids = masque.unsqueeze(-1)
            vecteur = (états * poids).sum(dim=1) / poids.sum(dim=1)

        torch.mps.synchronize()
        self.mps_counters()
        brut = [float(v) for v in vecteur[0].float().cpu().numpy().reshape(-1)]
        return brut, longueur, len(identifiants)

    def reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        """Entrée du job, puis options du variant, puis défauts du manifeste."""
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        for couche in (self.options, self.defaults):
            if couche.get(nom) is not None:
                return couche[nom]
        return defaut

    # --- mémoire et versions -------------------------------------------------

    def mps_counters(self) -> dict[str, int]:
        """Compteurs MPS instantanés. Aucun n'est un pic — les noms le disent.

        Tout relevé nourrit au passage le maximum retenu pour le profil :
        `driver_allocated_memory` redescend aussi vite qu'il monte, et le
        maximum se tient à chaque relevé plutôt qu'il ne se lit une fois à la fin.
        """
        mps = getattr(self.torch, "mps", None) if self.torch is not None else None
        if mps is None:
            return {}
        try:
            compteurs = {
                "mps_current_allocated_bytes": int(mps.current_allocated_memory()),
                "mps_driver_allocated_bytes": int(mps.driver_allocated_memory()),
                "mps_recommended_max_bytes": int(mps.recommended_max_memory()),
            }
        except (AttributeError, RuntimeError):
            return {}
        self._peak_driver = max(self._peak_driver, compteurs["mps_driver_allocated_bytes"])
        return compteurs

    def peak_memory_bytes(self) -> int | None:
        """Le maximum des relevés du pilote Metal et du pic RSS.

        Le RSS ne compte pas la mémoire Metal — relevé ici, 0,44 Gio de RSS
        pendant que le pilote en tenait 3,12 — mais sur mémoire unifiée les deux
        sortent du même budget, et c'est le plus grand des deux qui doit entrer
        au profil du contrôle d'admission.
        """
        self.mps_counters()
        return max(self._peak_driver, peak_rss_bytes() or 0) or None

    def unload(self) -> None:
        """Rend la mémoire au budget, pas seulement à Python.

        Sans `empty_cache`, l'allocateur MPS garde ses pools : les tenseurs sont
        libérés, le pilote tient toujours les octets, et le résident suivant se
        voit refuser l'admission pour une mémoire que plus personne n'utilise.
        """
        self.model = None
        self.tokenizer = None
        if self.torch is not None:
            gc.collect()
            try:
                self.torch.mps.empty_cache()
            except (AttributeError, RuntimeError):
                pass

    def versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for nom in ("torch", "transformers"):
            version = _version(nom)
            if version != "?":
                versions[nom] = version
        return versions


# --- lecture de l'amont ------------------------------------------------------


def _alphabet_du_tokenizer(tokenizer: Any) -> frozenset[str]:
    """Les lettres réellement connues du modèle, lues sur lui plutôt que supposées.

    ESM-2 et ESM-C ne publient pas le même vocabulaire, et une constante écrite
    ici aurait vieilli au premier modèle suivant. Le repli n'est employé que si
    le tokenizer refuse de se laisser lire — auquel cas mieux vaut un alphabet
    approché qu'un refus de servir.
    """
    try:
        vocabulaire = tokenizer.get_vocab()
    except (AttributeError, TypeError, ValueError):
        return ALPHABET_ESM
    lettres = {jeton for jeton in vocabulaire if len(jeton) == 1}
    return frozenset(lettres) if lettres else ALPHABET_ESM


def _version(module: str) -> str:
    try:
        importé = __import__(module, fromlist=["__version__"])
    except ImportError:
        return "?"
    return str(getattr(importé, "__version__", "?"))


if __name__ == "__main__":
    raise SystemExit(main(EsmEmbedWorker))
