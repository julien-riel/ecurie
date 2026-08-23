# Veille — 2026-08-23

Cycle ciblé, déclenché par deux demandes explicites : mettre **Qwen3.6-27B en
4 bits** en service pour toutes les capacités qu'il sait remplir, puis faire de
même avec **Gemma**. Aucun balayage général des sources n'a été mené ; ce rapport
ne dit rien de ce qui a bougé ailleurs.

Contrairement à un cycle de veille ordinaire, celui-ci est allé jusqu'à la phase
d'épreuve : les poids ont été téléchargés sur autorisation, les profils mesurés,
et trois adaptateurs écrits. Ce qui suit n'est donc pas une projection.

## 1. Verdict

**Les deux modèles tournent, et un seul est utilisable.** Qwen3.6-27B tient sur
**une** capacité des sept : la génération de texte, à 17,45 Gio de pic pour un
budget de 17,76 — 316 Mio de marge. Partout ailleurs il échoue en
`Insufficient Memory` : les quatre capacités visuelles, l'appel d'outils sur les
trois cas, et la traduction dès que le texte s'allonge. Gemma 4 12B fait tout, à
6,7–7,2 Gio selon la capacité.

**Ces 316 Mio sont le vrai plafond, et ils se mesurent en jetons d'invite.**
C'est le seul enseignement du cycle qu'aucune estimation ne donnait : ce qui
fait basculer ce modèle n'est pas la taille de sa réponse mais la longueur de ce
qu'on lui donne à lire. Déclarer trois outils porte l'invite de 44 à 177 jetons,
et cela suffit.

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
| `qwen36-27b-describe@4bit` | `image-to-text` | 16,08 Go | — | — | **échec Metal, 3 cas sur 3** |
| `qwen36-27b-traduction@4bit` | `translation` | 16,08 Go | 17,78 Gio | — | **échec sur le cas long** |
| `qwen36-27b-outils@4bit` | `tool-use` | 16,08 Go | — | — | **échec Metal, 3 cas sur 3** |
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

Qwen3.6-27B n'est recommandé pour aucune capacité, et six de ses sept manifestes
resteront refusés par l'admission faute de profil — le banc n'en écrit pas quand
un cas échoue. C'est la protection qui joue, et il faut la laisser jouer : la
seule façon de les faire démarrer serait d'inscrire un profil à la main, ce que
le projet s'interdit précisément pour ce genre de raison.

## 4. Rejets motivés

| Objet | Motif |
|---|---|
| Qwen3.6-27B sur 6 de ses 7 capacités | Échec Metal mesuré : les 4 visuelles, l'appel d'outils, et la traduction longue. Ni `--hors-budget` ni le variant `mxfp4` (840 Mo de moins) ne changent la nature du refus. |
| `Qwen3.6-27B-6bit` (22,8 Go), `-8bit` (29,5), `-bf16` | Très au-delà du budget. |
| `Qwen3.6-27B-OptiQ-4bit` (20,0 Go) | Plus lourd que le 4 bits standard, et **sans tour de vision**. |
| `Qwen3.6-27B-MTP-4bit` (0,26 Go) | Tête de prédiction multi-jetons seule, pas un modèle. |
| AWQ / GPTQ-Int4 (cyankiwi, QuantTrio, Intel…) | Formats CUDA/vLLM. Aucun chemin Apple Silicon. |
| Dérivés *abliterated* / *uncensored* | Ne sont pas les poids de référence. |
| `gemma-4-31B-it` (18,44 Go en 4 bits) | Même impasse que Qwen3.6-27B, en pire. Non téléchargé. |
| `gemma-4-26B-A4B-it` (15,37 Go) | MoE à 4 B actifs, donc rapide, mais le pic suivrait celui de Qwen3.6-27B. À éprouver seulement si la vision de la 12B déçoit. |
| `speech-to-text`, `audio-to-text` pour Gemma 4 | L'adaptateur existe désormais. Ce qui manque est l'encodeur : la conversion MLX ne garde que la projection audio, trois tenseurs sans la tour qui les précède. |
| **Qwen-Image-Edit-2511** | Aucune conversion mflux sous 20 Go — les seules compatibles pèsent 37,5 et 151 Go, pour un budget de 17,76 Gio. Les 4 bits disponibles sont au format diffusers, et publiées en CC-BY-NC-SA quand l'officiel est Apache-2.0 : une quantification qui restreint la licence de ce qu'elle quantifie. `mflux-save` permettrait de quantifier soi-même depuis le 8 bits, au prix de 37,5 Go téléchargés pour en produire 19, et d'un résultat qui dépasserait encore. **Déclencheur : une conversion mflux sous 20 Go.** |

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

## 6. SAM 3, et ce qu'il révèle du banc d'essai

Ajouté à la demande, servi par `mlx-vlm` qui porte l'architecture depuis sa 0.6.
1,72 Go en bf16, 690 ms de warmup, aucune tension avec le budget. Il fonctionne :
sur la scène du banc, l'invite « cube » rend une instance à **0,94** de score et
un masque qui ne mord ni sur la sphère ni sur le cône.

**Il désigne par le mot, pas par le clic.** Le titulaire `sam2-hiera-small` suit
un point ou une boîte ; SAM 3 reçoit un concept et rend une instance par objet
qui lui ressemble. C'est la même capacité au sens du contrat — une image entre,
un masque sort, l'utilisateur désigne —, et `image-segment` a gagné un champ
`prompt` pour cette troisième façon de montrer.

**Le concept se donne en anglais**, et c'est mesuré : « cube » trouve l'objet,
« un cube » ne trouve rien. Le silence de l'encodeur ne se distingue pas d'un
objet absent — rien n'échoue, la réponse est vide. C'est le genre de défaut qui
se paie en heures quand il n'est pas écrit.

**Il n'a pas de profil, et cela tient au banc plutôt qu'au modèle.** La charge
type de `image-segment` désigne par point et par boîte, parce qu'un seul modèle
servait cette capacité quand elle a été écrite. Les trois cas échouent donc ici,
le banc n'écrit rien, et l'admission refusera ce variant — la protection joue,
et il faut la laisser jouer.

La sortie n'est pas d'ajouter un cas : y mettre un concept ferait échouer
`sam2-hiera-small` en retour, et modifier les trois existants détruirait la
comparabilité de toutes les mesures antérieures, que le fichier de charge déclare
en toutes lettres. Ce qu'il faut est **un banc qui saute les cas qu'un variant ne
peut pas servir**, et un manifeste qui déclare ce qu'il sait recevoir. C'est le
chantier que ce cycle a mis au jour, et il dépasse le cadre d'un ajout de modèle.

## 7. Déclencheurs et travail identifié

1. **Un banc d'essai qui s'adapte au variant** — préalable à toute mesure de
   SAM 3, et plus généralement à tout modèle qui sert une capacité autrement que
   le premier arrivé. Voir §6.
2. **L'audio de Gemma 4.** Le modèle transcrit la parole et la traduit à la
   volée. Servir `speech-to-text` et `audio-to-text` demande un adaptateur audio
   sur `mlx-vlm` — le pendant de ce qui a été fait ici pour le texte. C'est le
   chantier le plus rentable qui reste : les poids sont déjà sur le disque.
3. **La grille de `gemma4-12b-detect`.** Le manifeste porte le défaut hérité de
   Qwen3-VL, non vérifié. À établir sur une scène aux positions connues, comme
   l'a été celle du titulaire, avant tout usage et toute comparaison.
4. **Le golden set.** Quatre capacités ont un challenger sans qu'aucun gain de
   qualité ne soit établi. Tant qu'il n'a pas tourné, ce rapport ne peut
   recommander aucun remplacement.
5. **`Qwen/Qwen3.8-27B`**, paru le 2026-08-14. Sa conversion 4 bits pèse 16,08 Go,
   soit exactement la même impasse sur la vision. Le mur est la classe 27B sur
   24 Gio, pas le numéro de version.

## 8. Second lot — cinq modèles demandés nommément

Demandés après coup, avec un classement d'usage. Quatre sur cinq sont entrés au
parc ; le cinquième est écarté avec son motif. Deux d'entre eux avaient été mal
jugés au premier examen, et les deux erreurs allaient dans le même sens : une
conversion qui ne marche pas ne dit rien du modèle.

| Modèle | État | Mesuré |
|---|---|---|
| **MiniCPM-V 4.6** | en service | 2,65 Gio · 1,3 s · 64 jetons/s |
| **MiniCPM-o 4.5** | vision en service, écoute muette | 6,26 Gio · 5,7 s |
| **Depth Anything 3 Large** | en service | 1,64 Go · 571 ms/image |
| **SeedVR2 3B** | en service | 7,28 Go · **16,78 Gio** · 9,5 s |
| **Qwen-Image-Edit-2511** | écarté — voir §4 | — |

**Ce que le parc y gagne en propre.** Deux capacités (`depth-estimation`,
et `image-segment` étendue au concept), trois runtimes (`depth-anything`,
`mflux`, plus l'audio ouvert sur `mlx-vlm`), et quatre adaptateurs.

**Demandés en GGUF, servis en MLX.** Le parc n'a pas de runtime GGUF —
`llama-cpp` est hors périmètre, et en ajouter un supposerait Ollama ou LM Studio,
donc un serveur tiers que le superviseur ne contrôle pas. Les conversions MLX
partent des mêmes poids.

**Deux erreurs de jugement, corrigées par la mesure.** SeedVR2 avait été écarté
parce que sa conversion mlx-community est en MLX-Swift ; mflux en porte une
implémentation Python. Et MiniCPM-o semblait irrécupérable sur un « Missing 1203
parameters » qui venait du `sanitize` d'amont, pas des poids — tous présents,
vérifiés tenseur par tenseur.

**Ce que l'écoute a coûté pour rien.** L'adaptateur `mlx_vlm_audio` est écrit et
testé, il charge et transmet le son correctement. Aucun modèle du parc ne peut
l'exercer : la conversion MLX de Gemma 4 ne garde que la projection audio sans
son encodeur, et celle de MiniCPM-o entend sans transcrire — même en 5 bits, où
elle répond « c'est un son de synthèse », ce qui est exact mais s'arrête là. Le
travail reste, il attend un modèle omni dont la conversion soit entière.

## 9. Plan de GC

Trois jeux de poids sont entrés sur le disque : **16,08 Go** pour Qwen3.6-27B,
**6,77 Go** pour Gemma 4 12B et **1,72 Go** pour SAM 3, soit 24,57 Go. Le disque en garde 511 Gio libres
sur 926 — la garde des 15 % n'est pas menacée.

Un seul poste mérite d'être chiffré : si Qwen3.6-27B n'est pas retenu pour la
génération de texte, ses **16,08 Go** partent d'un seul geste, les sept
manifestes de la famille partageant un unique jeu de poids. C'est le plus gros
gain disponible dans le parc actuel, et il ne coûte que quatre capacités qui ne
fonctionnent pas et trois qui sont plus lentement servies qu'ailleurs.
