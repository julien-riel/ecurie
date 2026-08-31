"""Le catalogue est éditorial, versionné dans le code, et petit.

Douze outils par défaut, un par capacité promise, plus trois méta-outils. Ce
n'est pas une projection du registre : les quarante et une capacités y restent
découvrables par `ecurie_catalog` et exécutables par `ecurie_run`, sans qu'un
octet de manifeste change. Le registre dit ce que le parc sait faire ; ce
fichier dit ce que le produit promet.

**La taille est une décision, et elle vient d'une mesure.** Quarante outils
déclarés coûtaient 16 690 jetons de catalogue avant qu'un mot soit échangé, et le
choix restait juste ; à soixante-sept, le modèle tombait en boucle de répétition
sans que rien ne lève. Le relevé date d'août 2026, sur `gemma4-12b-chat@4bit`,
pris avec un harnais que le pivot a retiré — c'est le seul chiffre du projet
qu'aucun fichier de ce dépôt ne porte. Les clients tiennent mieux, mais le coût
de contexte, lui, est payé par tous : douze plus trois laisse de la marge.

**Ce que le catalogue exclut, et sur quel critère.** Les capacités qui portent un
`human_subject` — les six `face-*` et `voice-clone` — sont hors du catalogue par
défaut. C'est l'application d'un champ du contrat, pas une opinion du serveur :
le champ dit ce que la capacité **fait** d'une personne réelle, là où
`license_class` ne dit que ce que le droit interdit.

Sur ce point, le §6.3 se contredit d'une phrase à l'autre — il exclut sur le
champ, puis annonce que « les vingt-neuf capacités restantes restent exécutables
par `ecurie_run` », ce qui rouvrirait la porte que le champ vient de fermer.
L'arbitrage retenu, et il est le seul qui donne un sens au champ : `ecurie_run`
refuse lui aussi les capacités à `human_subject`, et l'opt-in les rouvre des deux
côtés à la fois (`--tools faces`, `--tools all`). Une capacité qui identifie
quelqu'un ne devient pas acceptable parce qu'elle est passée par l'échappatoire.
"""

from dataclasses import dataclass, field

# Les douze capacités promises, dans l'ordre où le README les présente : ce que
# le parc entend, ce qu'il dit, ce qu'il voit, ce qu'il fabrique, ce qu'il
# prévoit. L'ordre est éditorial et il compte — c'est celui dans lequel un agent
# lit le catalogue.
DOUZE: tuple[str, ...] = (
    "speech-to-text",
    "speaker-diarization",
    "audio-separation",
    "text-to-speech",
    "image-to-text",
    "depth-estimation",
    "image-segment",
    "image-matting",
    "text-to-image",
    "image-to-image",
    "image-upscale",
    "time-series-forecast",
)

# Les familles qu'un opt-in rouvre. `faces` regroupe les six capacités que le
# champ `human_subject` écarte ; `all` lève le filtre entier. La table vit ici et
# non au registre : regrouper des capacités en familles est une décision de
# produit, et l'inscrire au schéma obligerait chaque manifeste à la connaître.
FAMILLES: dict[str, tuple[str, ...]] = {
    "faces": (
        "face-detect",
        "face-embed",
        "face-gaze",
        "face-headpose",
        "face-landmark",
        "face-parse",
    ),
    # `voice-clone` porte `human_subject: synthesizes` et n'appartient à aucune
    # famille de visages. Sans entrée à lui, son refus renvoyait à
    # `--tools faces` — une commande qui ne l'aurait pas rouvert, donc une
    # promesse fausse dans le seul champ du payload que l'humain va exécuter.
    "voice": ("voice-clone",),
}


def famille_de(capability: str) -> str | None:
    """La famille qui rouvre cette capacité, quand il y en a une.

    Sert au refus d'exclusion : l'option qu'il porte doit être **la commande qui
    répare**, et une commande qui ne répare pas ce refus-là est pire que pas
    d'option du tout.
    """
    for famille, capacités in FAMILLES.items():
        if capability in capacités:
            return famille
    return None

# Les trois méta-outils, toujours présents. Préfixés, quand les douze ne le sont
# pas : le §6.3 les nomme ainsi, et la distinction se lit — un outil qui porte le
# nom d'une capacité fait le travail, un outil qui porte le nom du serveur parle
# du serveur.
CATALOGUE_OUTIL = "ecurie_catalog"
RUN_OUTIL = "ecurie_run"
STATUS_OUTIL = "ecurie_status"


def nom_outil(capability: str) -> str:
    """`speech-to-text` → `speech_to_text`.

    Les noms d'outils MCP admettent le tiret, mais pas les identifiants des
    langages qui les appellent ; le underscore est ce que tout le monde sait
    écrire.
    """
    return capability.replace("-", "_")


@dataclass(frozen=True)
class Outil:
    """Un outil du catalogue : ce qu'il promet, et à quelle capacité il renvoie.

    Les textes sont en anglais et rédigés à la main — c'est la surface produit,
    et elle est lue par des modèles. Les contrats, eux, gardent leur français :
    ils parlent à l'Atelier et à qui écrit un manifeste.
    """

    capability: str
    title: str
    description: str
    champs: dict[str, str] = field(default_factory=dict)

    @property
    def nom(self) -> str:
        return nom_outil(self.capability)


def outils(textes: dict[str, dict]) -> dict[str, Outil]:
    """Les outils du catalogue, montés depuis la rédaction de `textes.py`.

    Ce module porte les décisions — qui entre, qui sort, pourquoi — et l'autre ne
    porte que des phrases. Les tenir séparés évite un cycle d'import, mais ce
    n'est pas la raison : on ne relit pas une décision et une tournure dans le
    même état d'esprit, et mélanger les deux fait qu'on ne relit ni l'une ni
    l'autre.
    """
    montés: dict[str, Outil] = {}
    for capability, texte in textes.items():
        montés[capability] = Outil(
            capability=capability,
            title=texte["title"],
            description=texte["description"],
            champs=dict(texte.get("champs") or {}),
        )
    return montés


def outils_exposes(
    tous: dict[str, Outil], familles: frozenset[str] | None = None
) -> list[Outil]:
    """Les outils déclarés dans `tools/list` — les douze, plus l'opt-in.

    Une famille ouverte **élargit le catalogue** : c'est ce que promet
    `--tools faces`, et c'est distinct de ce qu'`ecurie_run` accepte d'exécuter.
    Une capacité rouverte qui n'a pas de texte rédigé n'entre pas pour autant :
    un outil sans description est un outil qu'aucun agent ne choisira à bon
    escient, et le catalogue n'est pas une projection du registre.
    """
    familles = familles or frozenset()
    ordre = list(DOUZE)
    if "all" in familles:
        ordre += [c for c in sorted(tous) if c not in DOUZE]
    else:
        for famille in sorted(familles):
            ordre += [c for c in FAMILLES.get(famille, ()) if c not in ordre]
    return [tous[capability] for capability in ordre if capability in tous]


def capacites_ouvertes(registry, familles: frozenset[str]) -> set[str]:
    """Les capacités qu'`ecurie_run` accepte d'exécuter.

    Toutes celles du registre, moins celles qui portent un `human_subject` — sauf
    si la famille qui les contient a été explicitement ouverte, ou si l'opt-in
    `all` lève le filtre.
    """
    if "all" in familles:
        return set(registry.capabilities)
    ouvertes: set[str] = set()
    autorisees = {c for f in familles for c in FAMILLES.get(f, ())}
    for identifiant, contract in registry.capabilities.items():
        if contract.human_subject and identifiant not in autorisees:
            continue
        ouvertes.add(identifiant)
    return ouvertes
