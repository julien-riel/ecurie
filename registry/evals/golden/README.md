# Golden sets — juger la qualité

Entrées figées, une par capacité, avec leur vérité terrain quand il y en a une.
Elles alimentent `ecurie eval` (métriques automatiques) et l'écran Confrontation
(préférences humaines) du v0.5.

## Ce que ce dossier n'est pas

`registry/evals/bench/` mesure un **coût** — mémoire, warmup, débit — et ses trois
entrées par capacité sont choisies pour être représentatives d'une dépense. Ici,
on mesure une **qualité**, et les entrées sont choisies pour être difficiles.

La différence a une conséquence pratique qu'il vaut mieux avoir en tête avant
d'ajouter un cas : **le banc fige tous les réglages, le golden set n'en fige
aucun.** Le banc doit comparer deux mesures prises à six mois d'écart, donc il
impose trente pas de débruitage et une résolution d'octree. Le golden set doit
comparer deux modèles au mieux de leur forme : imposer trente pas à un modèle
distillé conçu pour en faire quatre le jugerait sur un réglage fait pour un
autre. Chaque cas ne fixe donc que ce qui **définit la question** — le texte à
lire, la description à rendre, l'image à reconstruire, la graine — et laisse le
reste aux `defaults:` du variant.

## La règle qui tient tout : append-only

On ajoute un cas. On n'en corrige jamais un. Une coquille dans une charge type
est sans conséquence sur la mesure ; la corriger, si — et une entrée modifiée
détruit la comparabilité de tous les résultats antérieurs **en silence**, ce qui
est bien pire qu'une erreur franche.

Cela vaut aussi pour les fichiers d'entrée : `tools/golden_assets.py` refuse
d'écraser une image existante sans `--force`.

## État des onze jeux

| Capacité | Cas | Référence | État |
|---|---|---|---|
| `document-to-text` | 16 pages | texte exact + champs | complet |
| `image-matting` | 6 scènes | **masque alpha exact** | complet |
| `image-upscale` | 6 réductions | **image d'origine** | complet |
| `translation` | 10 textes, FR↔EN | texte exact | complet |
| `tool-use` | 8 situations | **appel attendu** (JSON) | complet |
| `image-to-text` | 8 scènes | mentions exigées | complet |
| `text-generation` | 8 demandes de programmation | mentions exigées | complet |
| `text-to-speech` | 10 phrases | aucune (confrontation) | complet |
| `text-to-image` | 10 descriptions | aucune (confrontation) | complet |
| `image-to-mesh` | 8 solides | genre topologique | complet |
| `speech-to-text` | 12 extraits, dont 7 en français québécois | texte exact | **incomplet** — textes figés, enregistrements à produire (`speech-to-text/SOURCING.md`) |

Quatre de ces jeux se notent **entièrement sans juge humain**, ce qui est
inhabituel pour des capacités génératives et tient à la façon dont leurs entrées
sont fabriquées : le masque de détourage est celui qui a servi à composer la
scène, l'image d'agrandissement est l'originale dont l'entrée est la réduction,
la traduction a une référence, l'appel d'outil a un nom et des arguments. Les
autres alimentent la Confrontation.

Un avertissement pour qui lira un classement d'agrandissement : **tous les cas
ne discriminent pas également**. Mesuré le 21 août 2026 avec Swin2SR, l'écart à
une simple interpolation bicubique va de +0,31 dB sur une scène lisse à
+5,48 dB sur un fond rayé. Une scène analytique douce ne contient presque aucune
haute fréquence à restituer — il n'y a donc presque rien à départager, et un
score moyen sur les six cas dilue le seul signal utile.

Le jeu ASR est le seul qui manque de ses fichiers d'entrée, et c'est assumé :
synthétiser les extraits avec la voix du parc mesurerait la transcription de
parole synthétique — sans accent québécois, alors que c'est précisément ce qu'on
veut éprouver. Chaque cas nomme déjà son fichier et porte une clé `pending` ; le
jour où l'enregistrement arrive, rien du cas ne change.

## Forme d'un manifeste

`<capacité>/manifest.json`, validé contre `registry/schema/golden.schema.json` :

```json
{
  "capability": "document-to-text",
  "version": 1,
  "status": "complet",
  "normalization": "whitespace",
  "description": "…",
  "cases": [
    {
      "id": "facture",
      "input": { "document": "assets/facture.png", "format": "text" },
      "reference": {
        "text_file": "reference/facture.txt",
        "fields": { "total": "2 667,42 $" }
      },
      "notes": "Ce que ce cas éprouve, en une phrase.",
      "source": { "recipe": "page", "layout": "tableau" }
    }
  ]
}
```

- `input` doit valider contre le schéma d'entrée du contrat de la capacité — un
  test le vérifie, et c'est ce qui empêche un jeu d'essai de dériver du contrat ;
- `notes` est **obligatoire**. Un cas dont personne ne sait plus ce qu'il testait
  ne se remplace pas, et il ne s'interprète plus non plus ;
- `source` dit comment le fichier d'entrée a été fabriqué. Les images du banc
  d'essai n'en ont pas : leur recette « déterministe » n'a jamais été committée,
  et ce sont aujourd'hui des données orphelines qu'on ne sait plus refaire. On ne
  recommence pas.

### Comparaison des textes

`normalization: "whitespace"` réduit toute suite de blancs — espaces, tabulations,
retours à la ligne — à une seule espace avant de comparer. C'est le défaut, et
c'est ce qui rend la note indépendante de la largeur de page et de la façon dont
un lecteur choisit de rendre un tableau. `"strict"` compare caractère pour
caractère ; un seul cas l'emploie, `code-monospace`, où l'indentation fait partie
du contenu.

### Pourquoi la lecture de document demande `format: "text"` partout

Le contrat `document-to-text` sait rendre du Markdown, et c'est utile au
quotidien. Mais la mise en Markdown d'un tableau a plusieurs formes également
défendables : la noter reviendrait à mesurer la conformité à une préférence
plutôt que l'exactitude de la lecture. La structure se juge donc par
`reference.fields` — une date, un montant, un code de dossier —, qui n'a qu'une
seule bonne réponse. C'est ce que le §9 de la conception appelle l'exactitude par
champs, et cela répond à la question laissée ouverte au §13.2.

## Refabriquer les fichiers d'entrée

```
uv run --project runtimes/mlx-vlm python tools/golden_assets.py [cible…] [--force]
```

Sans argument, l'outil parcourt les golden sets **et** les charges type du banc
d'essai : les deux emploient les mêmes recettes et la même règle append-only,
seule diffère la question qu'elles posent. Une cible est un dossier de golden set
ou un fichier de charge type.

Trois recettes, toutes déterministes et sans réseau :

- **`page`** rend une page de document depuis son texte de référence. Le
  manifeste reste l'autorité — la page et sa vérité terrain ne peuvent donc pas
  diverger. Polices système de macOS, nommées explicitement dans le script ;
- **`solide`** rend un objet en RGBA à fond réellement transparent, par lancer de
  rayons sur une fonction de distance signée, avec suréchantillonnage : sans lui
  l'alpha ne vaudrait que 0 ou 255, ce qui priverait le détourage de toute
  couverture partielle sur les bords ;
- **`scene`** compose des solides sur un fond opaque **et rend l'alpha exact avec**.
  C'est ce qui donne au détourage une vérité terrain qui ne se discute pas : elle
  n'est pas annotée après coup, c'est celle qui a servi à fabriquer l'image. Avec
  `reduire`, l'entrée devient la réduction bicubique de la scène et la scène
  entière devient la référence — le procédé de l'agrandissement.
