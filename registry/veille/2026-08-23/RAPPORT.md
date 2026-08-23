# Veille — 2026-08-23

Cycle ciblé, déclenché par deux demandes explicites : mettre **Qwen3.6-27B en
4 bits** en service pour toutes les capacités qu'il sait remplir, puis faire de
même avec **Gemma**. Aucun balayage général des sources n'a été mené ; ce rapport
ne dit rien de ce qui a bougé ailleurs.

Contrairement à un cycle de veille ordinaire, celui-ci est allé jusqu'à la phase
d'épreuve : les poids ont été téléchargés sur autorisation, les profils mesurés,
et trois adaptateurs écrits. Ce qui suit n'est donc pas une projection.

## 1. Verdict

**Les deux modèles tournent, et un seul est utilisable partout.** Qwen3.6-27B
tient en génération de texte — 17,45 Gio de pic pour un budget de 17,76, soit
316 Mio de marge — et **échoue à décrire une image**, où Metal refuse le buffer
d'encodage : `Insufficient Memory`, sur les trois cas de la charge type. Gemma 4
12B fait tout, à 6,81 Gio de pic et quinze fois plus vite sur la vision.

**Le parc gagne sept capacités servies par un modèle qui tient**, et trois
adaptateurs qui manquaient : `text-generation`, `translation` et `tool-use` sur
le runtime `mlx-vlm`. Sans eux, aucun modèle vision-langage ne pouvait servir ces
trois contrats — leurs adaptateurs n'existaient que sous `mlx-lm`, qui ne charge
ni `qwen3_5` ni `gemma4_unified`.

**Action demandée** : arbitrer les titulaires. Gemma 4 12B est en position de
prendre quatre capacités à des titulaires plus légers mais plus faibles, et le
golden set n'a pas encore tourné — aucun gain de qualité n'est établi, seulement
des coûts.

## 2. Ce que la mesure a dit

| Variant | Capacité | Disque | Pic | Débit | État |
|---|---|---|---|---|---|
| `qwen36-27b-texte@4bit` | `text-generation` | 16,08 Go | **17,45 Gio** | 1 / 81,9 s | tient, 316 Mio de marge |
| `qwen36-27b-describe@4bit` | `image-to-text` | 16,08 Go | — | — | **échec Metal, OOM** |
| `gemma4-12b-texte@4bit` | `text-generation` | 6,77 Go | **6,69 Gio** | 1 / 38,7 s | tient largement |
| `gemma4-12b-describe@4bit` | `image-to-text` | 6,77 Go | **6,81 Gio** | 1 / 5,3 s | tient largement |
| `gemma4-12b-traduction@4bit` | `translation` | 6,77 Go | **6,74 Gio** | 1 / 4,3 s | tient largement |
| `gemma4-12b-outils@4bit` | `tool-use` | 6,77 Go | **7,15 Gio** | 1 / 2,3 s | tient, appel natif |

Trois enseignements que l'estimation ne donnait pas :

**L'estimation d'avant-mesure visait juste, le seuil était faux.** Le pic prévu
pour Qwen3.6-27B était de 18,3 à 20,3 Go ; le mesuré est 18,74 Go. Mais il était
comparé à un plafond de « 17 Go » décimaux, quand le budget réel de la machine
est de 17,76 **Gio**, soit 19,07 Go. Le modèle était donc déclaré hors budget
alors qu'il y entre. La confusion Go / Gio a coûté un rejet injustifié : les
budgets de cette procédure sont en gibioctets, et le rapport de veille doit les
écrire ainsi.

**Le mode hors budget ne sauve pas tout, et c'est mesuré.** La pagination
rattrape une croissance graduelle — un KV cache qui s'étend jeton par jeton.
Elle ne rattrape pas une allocation massive d'un seul tenant : l'encodage d'une
image réclame d'un coup un buffer que Metal refuse, sans ralentissement
préalable. `--hors-budget` lève un refus d'**admission** ; il ne lève pas un
refus du pilote, qui survient en aval et qu'aucun contrôle en amont ne peut
prédire depuis un pic global. Le message d'admission le dit désormais en toutes
lettres, et un test le fige.

**Un 27B qui tient reste un 27B qui coûte.** 81,9 s par sortie contre 38,7 s pour
Gemma 4 12B et 12,8 s pour le titulaire `qwen25-coder-7b`. Et à 316 Mio de marge,
il interdit tout autre résident : chaque job vide le parc.

## 3. Recommandations de remplacement

Aucune n'est formulée, faute de gain établi — le golden set n'a pas tourné. Les
coûts, eux, sont chiffrés, et ils vont tous dans le même sens.

| Capacité | Titulaire de fait | Challenger retenu | Coût disque | Coût mémoire | Débit |
|---|---|---|---|---|---|
| `text-generation` | qwen25-coder-7b (4,30 Go / 4,45) | **gemma4-12b-texte** | +2,47 Go | 6,69 Gio | 38,7 s vs 12,8 |
| `image-to-text` | qwen3-vl-8b-describe (5,78 Go / 6,60) | **gemma4-12b-describe** | +0,99 Go | 6,81 Gio | 5,3 s vs 6,6 |
| `translation` | qwen3-4b-traduction (2,28 Go / 2,76) | **gemma4-12b-traduction** | +4,49 Go | 6,74 Gio | 4,3 s vs 1,6 |
| `tool-use` | qwen3-4b-outils (2,28 Go / 2,93) | **gemma4-12b-outils** | +4,49 Go | 7,15 Gio | 2,3 s vs 1,0 |

`gemma4-12b-describe` est le seul candidat de ce cycle qui batte un titulaire sur
un chiffre mesuré : 5,3 s contre 6,6 s par sortie, pour 210 Mio de pic en plus.
C'est mince, et cela ne dit rien de la qualité des descriptions.

Qwen3.6-27B n'est recommandé pour aucune capacité. Il reste au registre en
`candidate` parce qu'il fonctionne en texte et que sa taille peut valoir sur des
tâches longues que le golden set ne couvre pas encore.

## 4. Rejets motivés

| Objet | Motif |
|---|---|
| Qwen3.6-27B sur les 4 capacités visuelles | Échec Metal mesuré. Ni `--hors-budget` ni le variant `mxfp4` (840 Mo de moins) ne changent la nature du refus. |
| `Qwen3.6-27B-6bit` (22,8 Go), `-8bit` (29,5), `-bf16` | Très au-delà du budget. |
| `Qwen3.6-27B-OptiQ-4bit` (20,0 Go) | Plus lourd que le 4 bits standard, et **sans tour de vision**. |
| `Qwen3.6-27B-MTP-4bit` (0,26 Go) | Tête de prédiction multi-jetons seule, pas un modèle. |
| AWQ / GPTQ-Int4 (cyankiwi, QuantTrio, Intel…) | Formats CUDA/vLLM. Aucun chemin Apple Silicon. |
| Dérivés *abliterated* / *uncensored* | Ne sont pas les poids de référence. |
| `gemma-4-31B-it` (18,44 Go en 4 bits) | Même impasse que Qwen3.6-27B, en pire. Non téléchargé. |
| `gemma-4-26B-A4B-it` (15,37 Go) | MoE à 4 B actifs, donc rapide, mais le pic suivrait celui de Qwen3.6-27B. À éprouver seulement si la vision de la 12B déçoit. |
| `speech-to-text`, `audio-to-text` pour Gemma 4 | Le modèle sait transcrire — il a un `audio_config`. Aucun adaptateur audio n'existe sur `mlx-vlm` : ces capacités sont servies par `mlx-audio`, sur d'autres poids. Voir §6. |

## 5. Ce qui a été construit

**Le mode hors budget** (`--hors-budget`, `overcommit` dans l'API). Il lève le
seul refus qui vienne d'un modèle trop gros pour la machine, à trois conditions
tenues par le contrôle d'admission : le parc est vidé **entièrement**, aucun
travail en cours n'est détruit pour faire la place, et la décision est inscrite
en tête des avertissements du manifeste du job. Il ne force ni un profil
manquant — on ne peut pas assumer un dépassement dont on ignore la taille — ni
un épinglé. Neuf tests le figent.

**Trois adaptateurs**, qui ne recopient pas leurs jumeaux : `mlx_vlm_text`,
`mlx_vlm_translate` et `mlx_vlm_tools` héritent de `MlxLmWorker`,
`MlxLmTranslateWorker` et `MlxLmToolsWorker`, et n'en changent que le moteur.
Deux points d'extension ont suffi — `_import_runtime` et `_flux` —, parce que
`mlx_vlm.stream_generate` a la même signature que celui de `mlx-lm` à deux détails
près : un processor au lieu d'un tokenizer, et `image=None`. Les trois modules
font une trentaine de lignes chacun, dont l'essentiel est leur en-tête, et un
test vérifie qu'aucun ne redéfinit `infer`.

**Trois correctifs que ces modèles ont rendus nécessaires :**

- *le mode « thinking »*. Qwen3.6 et Gemma 4 raisonnent à voix haute par défaut.
  Le brouillon coûte deux fois : il consomme le `max_tokens` de la réponse, et il
  s'intercale devant elle. `enable_thinking` est désormais transmis au gabarit
  par les sept adaptateurs `mlx-vlm`, et `sans_raisonnement` sépare ce qui en
  réchapperait — dans `workers/base.py`, parce que ce trait n'appartient ni à une
  famille ni à un runtime ;
- *les formats d'appel d'outils*, et ils ont coûté trois correctifs à eux seuls.
  L'extracteur ne connaissait que le JSON ; Qwen3.6 rend du **XML imbriqué**,
  Gemma 4 rend `<|tool_call>call:nom{clé:<|"|>valeur<|"|>}<tool_call|>` — du JSON
  dont les clés sont nues et les guillemets remplacés par un jeton spécial. Les
  deux auraient rendu zéro appel sur un modèle ayant parfaitement choisi son
  outil. Deux stratégies ont été ajoutées, `xml_function` et `gemma_tool_call`.
  Entre les deux, il a fallu régler la **déclaration** : Gemma lit son gabarit
  avec `tool.function.name` et lève sur la forme plate que Qwen3 accepte, ce qui
  faisait replier sur une description en message système — `template_tools`
  rendait faux, et l'on mesurait un repli là où l'appel natif existait. Les deux
  formes sont désormais proposées au gabarit. Résultat mesuré sur Gemma :
  `template_tools` vrai, un appel juste sur 3, 8 et 16 outils déclarés, aucun
  reproche sur les arguments ;
- *la grille de coordonnées de la détection*. Elle valait 1000 en dur, ce qui se
  défendait tant qu'une seule famille servait la capacité. Dès la deuxième, la
  constante devient un piège silencieux — une grille fausse trace les boîtes à
  une fraction de leur place sans que rien n'échoue. Elle se déclare désormais
  par variant, sous `options.grid`.

## 6. Déclencheurs et travail identifié

1. **L'audio de Gemma 4.** Le modèle transcrit la parole et la traduit à la
   volée. Servir `speech-to-text` et `audio-to-text` demande un adaptateur audio
   sur `mlx-vlm` — le pendant de ce qui a été fait ici pour le texte. C'est le
   chantier le plus rentable qui reste : les poids sont déjà sur le disque.
2. **La grille de `gemma4-12b-detect`.** Le manifeste porte le défaut hérité de
   Qwen3-VL, non vérifié. À établir sur une scène aux positions connues, comme
   l'a été celle du titulaire, avant tout usage et toute comparaison.
3. **Le golden set.** Quatre capacités ont un challenger sans qu'aucun gain de
   qualité ne soit établi. Tant qu'il n'a pas tourné, ce rapport ne peut
   recommander aucun remplacement.
4. **`Qwen/Qwen3.8-27B`**, paru le 2026-08-14. Sa conversion 4 bits pèse 16,08 Go,
   soit exactement la même impasse sur la vision. Le mur est la classe 27B sur
   24 Gio, pas le numéro de version.

## 7. Plan de GC

Deux jeux de poids sont entrés sur le disque : **16,08 Go** pour Qwen3.6-27B,
**6,77 Go** pour Gemma 4 12B, soit 22,85 Go. Le disque en garde 511 Gio libres
sur 926 — la garde des 15 % n'est pas menacée.

Un seul poste mérite d'être chiffré : si Qwen3.6-27B n'est pas retenu pour la
génération de texte, ses **16,08 Go** partent d'un seul geste, les sept
manifestes de la famille partageant un unique jeu de poids. C'est le plus gros
gain disponible dans le parc actuel, et il ne coûte que quatre capacités qui ne
fonctionnent pas et trois qui sont plus lentement servies qu'ailleurs.
