"""Adaptateur `mlx-lm`, chemin **traduction**.

Même modèle chargé que la génération de texte, autre métier. Ce qui change tient
en trois points, et chacun a un effet mesurable sur la note d'un golden set :

- **l'invite est une consigne de traduction, pas une conversation.** Un modèle à
  qui l'on dit « traduis » répond volontiers « Voici la traduction : … » ; le
  texte rendu porte alors une phrase qui n'est pas dans l'original, et le score
  automatique la compte comme une erreur. La consigne interdit donc explicitement
  tout préambule, et la sortie est nettoyée d'un éventuel reste ;
- **la température est nulle par défaut.** Une traduction n'est pas une création :
  l'échantillonnage n'y ajoute que des occasions de s'écarter de l'original ;
- **le registre du contrat est honoré.** Un moteur de traduction dédié l'ignore ;
  un modèle de langue peut le suivre, et c'est précisément l'écart que la
  confrontation doit faire apparaître entre les deux.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).
"""

from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    WorkerError,
    main,
)
from ecurie_runtime.workers.mlx_lm import OUTPUT_TEXT, Consigne, MlxLmBase

# Noms français des langues les plus employées ici. Un code BCP 47 inconnu est
# transmis tel quel : le modèle en connaît bien plus que cette table, et refuser
# une langue au motif qu'elle n'y figure pas serait une limite inventée.
LANGUES = {
    "fr": "français",
    "fr-ca": "français québécois",
    "en": "anglais",
    "es": "espagnol",
    "de": "allemand",
    "it": "italien",
    "pt": "portugais",
    "nl": "néerlandais",
}

REGISTRES = {
    "neutre": "Emploie un registre neutre et courant.",
    "soutenu": "Emploie un registre soutenu, sans familiarité ni contraction.",
    "familier": "Emploie un registre familier, tel qu'on parlerait à un proche.",
}

SYSTEME = (
    "Tu es un traducteur professionnel. Tu rends UNIQUEMENT la traduction du "
    "texte qu'on te donne : pas de préambule, pas de commentaire, pas de "
    "guillemets ajoutés, pas de note du traducteur. Si le texte contient des "
    "noms propres, des codes ou des nombres, tu les reportes exactement."
)

# Amorces que les modèles ajoutent malgré la consigne. Retirées de la sortie
# plutôt que tolérées : elles ne sont pas dans l'original, donc elles comptent
# comme des insertions au calcul du score.
PREAMBULES = (
    "voici la traduction :",
    "voici la traduction:",
    "traduction :",
    "traduction:",
    "here is the translation:",
    "translation:",
)


def nom_de_langue(code: Any) -> str | None:
    brut = str(code or "").strip()
    if not brut:
        return None
    return LANGUES.get(brut.lower(), brut)


def build_prompt(
    texte: str,
    source: str | None,
    cible: str,
    registre: str,
    preserver: bool,
) -> str:
    """La consigne de traduction, composée depuis les champs du contrat."""
    départ = f"du {source} " if source else ""
    lignes = [f"Traduis le texte suivant {départ}vers le {cible}."]
    lignes.append(REGISTRES.get(registre, REGISTRES["neutre"]))
    if preserver:
        lignes.append(
            "Conserve exactement la mise en forme : retours à la ligne, listes, "
            "titres et balisage léger."
        )
    lignes.append("")
    lignes.append("Texte :")
    lignes.append(texte)
    return "\n".join(lignes)


def nettoyer(texte: str) -> str:
    """Retire un préambule d'amorce, s'il y en a un."""
    dépouillé = texte.strip()
    bas = dépouillé.lower()
    for amorce in PREAMBULES:
        if bas.startswith(amorce):
            return dépouillé[len(amorce) :].strip()
    return dépouillé


class MlxLmTranslateWorker(MlxLmBase):
    """Traduction : une langue d'arrivée obligatoire, une sortie sans préambule."""

    name = "mlx-lm-translate"

    def annonce(self) -> dict[str, Any]:
        # Le contrat déclare `x-options-from: runtime.languages` : on rend les
        # codes qu'on sait nommer, sans prétendre que ce sont les seuls compris.
        return {"languages": sorted(LANGUES)}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        texte = str(request.get("text") or "").strip()
        if not texte:
            raise WorkerError("aucun texte à traduire : le champ `text` est vide")

        cible = nom_de_langue(self.reglage(request, "target_language", None))
        if not cible:
            raise WorkerError(
                "aucune langue d'arrivée : `target_language` est obligatoire, "
                "la deviner serait deviner ce qu'on veut"
            )
        source_code = self.reglage(request, "source_language", None)
        source = nom_de_langue(source_code)
        registre = str(self.reglage(request, "register", "neutre"))
        preserver = bool(self.reglage(request, "preserve_formatting", True))

        progress(5, "préparation")
        consigne = Consigne(
            system=SYSTEME,
            user=build_prompt(texte, source, cible, registre, preserver),
        )

        # Le plafond suit la longueur de l'entrée : une traduction fait rarement
        # plus du double de son original, et un plafond fixe tronquerait les
        # textes longs sans que rien ne l'annonce. Le facteur est large exprès.
        plafond = max(256, min(8192, len(texte) // 2 + 512))

        réponse, _ = self.engendrer(
            consigne.messages(),
            progress=progress,
            max_tokens=plafond,
            temperature=float(self.reglage(request, "temperature", 0.0)),
            top_p=1.0,
            seed=request.seed,
            etape="traduction",
        )

        traduction = nettoyer(réponse.text)
        progress(92, "écriture")
        (request.output_dir / OUTPUT_TEXT).write_text(traduction, encoding="utf-8")

        return InferResult(
            output={
                "text": OUTPUT_TEXT,
                # Ce modèle ne détecte pas : il traduit ce qu'on lui donne. Rendre
                # la langue déclarée est honnête ; inventer une détection ne le
                # serait pas, et l'UI afficherait une certitude sans fondement.
                "detected_source_language": str(source_code).strip() if source_code else "",
                "tokens_generated": réponse.generation_tokens,
            },
            metrics={
                "source_characters": len(texte),
                "target_characters": len(traduction),
                "expansion_ratio": round(len(traduction) / max(len(texte), 1), 3),
                "register": registre,
                "max_tokens": plafond,
                "prompt_tokens": réponse.prompt_tokens,
                "generation_tokens": réponse.generation_tokens,
                "tokens_per_second": réponse.tokens_per_second,
                "truncated": réponse.finish_reason == "length",
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )


if __name__ == "__main__":
    raise SystemExit(main(MlxLmTranslateWorker))
