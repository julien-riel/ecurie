# Qwen3.8-27B — le mur n'était pas la taille, c'était le format

> Cycle ciblé, déclenché par une demande explicite : « je veux utiliser
> qwen38-27b ». Ouvert le soir du 24 août ; les mesures portent l'horodatage
> UTC du 25, le banc datant en temps universel.

## Verdict en trois lignes

**Ce qui change** : sept manifestes `qwen38-27b-*` entrent au registre en
`status: candidate`, **mesurés sur les sept capacités, vingt et un cas sur vingt
et un au vert**. Là où le variant `4bit` de la génération précédente en ratait
six sur sept en « Insufficient Memory », celui-ci passe partout. Aucun code
écrit, aucun runtime ouvert, aucun contrat créé : le `model_type` est le même
`qwen3_5` et les sept adaptateurs de la famille `qwen36-27b` servent ces poids
sans retouche.

**Ce qui ne change pas** : aucun titulaire. Le gain de qualité reste inconnu, et
il le restera — il n'existe toujours aucun exécuteur de golden set dans le
dépôt. C'est le **troisième cycle consécutif** où ce manque interdit toute
recommandation de remplacement. Ce rapport chiffre un coût, et ce coût est de
2,5 à 6 fois la mémoire des titulaires en poste pour 2,8 à 7,6 fois leur temps.

**L'action demandée** : trancher si une capacité qui vide le parc entier à
chaque job mérite d'y entrer sans preuve de qualité, et arbitrer le sort de la
famille `qwen36-27b`, que ce cycle laisse intacte sur consigne mais qui est
désormais dominée sur les sept capacités à mémoire strictement égale.

---

## 1. La correction qui a rendu ce cycle possible

Le cycle du 2026-08-23 avait classé Qwen3.8-27B en « à surveiller » sur ce
constat, consigné dans son `candidats.json` :

> « mlx-community/Qwen3.8-27B-4bit pèse 16,08 Go, soit le même dépassement de
> budget. **Aucune conversion mxfp4 à ce jour.** » — « changer de génération ne
> débloque rien : **le mur est la taille, pas la version**. »

**Les deux moitiés sont fausses.**

`mlx-community/Qwen3.8-27B-mxfp4` a été publié le **2026-08-14, le jour même du
modèle amont**, et pèse 15,24 Go — exactement le poids du mxfp4 de la 3.6. Il
existait donc déjà quand le cycle précédent a conclu à son absence.

Et le mur n'était pas la taille. Les sept relevés de
`registry/measurements/qwen36-27b-*@mxfp4/` montraient déjà la génération
précédente au vert sur ses sept capacités, avec des pics de 14,55 à 17,43 Gio
pour un budget de 17,76. Ce qui échouait six fois sur sept, c'était le variant
**`4bit` affine**, plus lourd de 840 Mo. **C'est le format de quantification qui
décidait, pas le nombre de paramètres.**

**Cause probable de l'erreur, et la leçon.** Le balayage du 23 a interrogé l'API
Hugging Face en plein texte, `search=Qwen3.8-27B`, avec une limite de
50 résultats. Sur un modèle à 2,6 millions de téléchargements, cette limite est
saturée par les dérivés communautaires — `abliterated`, `uncensored`, GGUF
tiers, quantifications maison. Le dépôt `mlx-community` officiel n'est pas
remonté. **Un `search` borné sur un modèle populaire ne prouve pas l'absence :
interroger `author=mlx-community` en plus du plein texte.**

---

## 2. Ce qui est mesuré

Machine : Mac17,4, 24 Gio de mémoire unifiée, macOS 26.5.2, mlx 0.32.1,
mlx-vlm 0.6.15. Budget 17,76 Gio, lu dans Metal — chiffre confirmé par une
source indépendante ce jour, le `--info` de `h3.c` annonçant « recommended GPU
set 17.8 GiB » sur la même machine.

| Capacité | Pic 3.8 | Pic 3.6 | % budget | Débit 3.8 | Débit 3.6 | Écart |
|---|---|---|---|---|---|---|
| `text-generation` | 14,55 Gio | 14,55 Gio | 81,9 % | 75,1 s | 76,7 s | −2 % |
| `translation` | 15,08 Gio | 15,08 Gio | 84,9 % | 9,5 s | 10,1 s | −6 % |
| `image-to-text` | 15,72 Gio | 15,72 Gio | 88,5 % | 20,6 s | 25,6 s | **−20 %** |
| `image-detect` | 15,79 Gio | 15,79 Gio | 88,9 % | 13,5 s | 14,0 s | −4 % |
| `document-to-text` | 15,89 Gio | 15,89 Gio | 89,5 % | 20,7 s | 20,9 s | −1 % |
| `tool-use` | 16,40 Gio | 16,40 Gio | 92,3 % | 7,6 s | 10,3 s | **−26 %** |
| `video-to-text` | 17,43 Gio | 17,43 Gio | **98,1 %** | 34,8 s | 28,9 s | **+20 %** |

### Le coût mémoire est identique à l'octet, et ce n'est pas un instrument aveugle

Les sept pics égalent ceux de Qwen3.6-27B@mxfp4 **au dernier octet**, capacité
par capacité. Cela méritait vérification avant publication, la signature étant
la même qu'un relevé recopié. Elle ne l'est pas :

- **le pic varie avec la charge** et suit `prompt_tokens` de façon monotone —
  traduction 153 → 15,84 Go, 212 → 15,98, 287 → 16,19 ; outils 382 → 16,32,
  630 → 16,85, 1095 → 17,61 ;
- **les jetons produits diffèrent** entre les deux générations — traduction
  15/59/118 contre 15/56/116, outils 32/31/31 contre 32/32/32 ;
- **`disk_bytes` distingue les deux jeux de poids** — 15 240 954 241 contre
  15 240 953 186.

L'explication est architecturale : le pic est **dominé par le préremplissage**,
et `qwen3_5` est un hybride où une couche sur quatre seulement porte un cache KV
qui croît. À architecture, quantification et invite identiques, l'allocation est
identique. **Le fait utile pour l'admission est donc celui-ci : à longueur
d'invite égale, Qwen3.8 coûte exactement ce que coûtait Qwen3.6.**

### La détection : le seul écart de qualité que ce cycle établisse

Il ne vient pas d'un golden set mais de la forme de la sortie, que le banc ne
regarde pas.

| Cas | Qwen3.8 | Qwen3.6 |
|---|---|---|
| `categories` | **3 objets**, 0 rejeté | 0 objet, **3 rejetés** |
| `langue-naturelle` | **1 objet**, 0 rejeté | 0 objet, **2 rejetés** |
| `libre` | **3 objets**, 0 rejeté | 0 objet, **3 rejetés** |

**Le banc de `qwen36-27b-detect@mxfp4` était au vert sur ses trois cas en ne
rendant aucun objet.** Le modèle générait 111 à 113 jetons, l'extracteur les
rejetait tous, et la mesure — un coût, une latence — était parfaitement valide.
C'est la réussite silencieuse dans sa forme la plus pure : rien n'échoue, tout
est faux. Qwen3.8 rend des objets sur les trois cas.

**Réserve** : la grille de coordonnées de cette famille n'est toujours pas
vérifiée. `options.grid` n'est pas déclaré, donc l'adaptateur applique le défaut
hérité de Qwen3-VL. Trois objets bien nommés à la mauvaise place seraient un
progrès qui n'en est pas — il faut ouvrir l'`overlay.png` d'un job réel avant de
conclure. Le caveat le dit au manifeste.

### Ce qui a été vérifié dans le gabarit avant de mesurer

Le gabarit de conversation change entre les deux générations — 169 lignes contre
153. Quatre différences, toutes établies en le lisant :

1. **`enable_thinking` reste honoré.** `{%- if enable_thinking is undefined or
   enable_thinking is true %}` court-circuite tout le bloc de raisonnement quand
   le drapeau est faux. C'était le premier point à établir : un drapeau renommé
   aurait laissé mesurer un modèle bavard en croyant l'avoir fait taire, et les
   trois capacités à sortie extraite par motif auraient chiffré l'adaptateur.
2. **`reasoning_effort` est nouveau**, défaut `xhigh` — le plus coûteux des
   trois —, et le gabarit lève une exception sur toute valeur hors de
   `xhigh`/`medium`/`low`. Aucun adaptateur du parc ne le transmet.
3. **`preserve_thinking` a changé de défaut** : indéfini valait faux en 3.6,
   vaut vrai en 3.8. Sans effet sur des contrats tous à un seul tour.
4. **Le format d'appel d'outils est inchangé**, mot pour mot. Prédiction
   confirmée par la mesure : `parse_strategy: xml_function`,
   `template_tools: true`, un appel par cas, zéro doléance.

### Deux limites partagées, qui ne sont pas des régressions

- **`document-to-text` sur la page dense** rend 423 caractères contre 421 pour
  la 3.6. Les deux générations lisent aussi peu ; c'est la charge type ou le
  contrat qu'il faut regarder, pas le modèle.
- **`text-generation` sur le cas long** bute exactement sur les 1536 jetons du
  plafond, dans les deux générations. La sortie est tronquée par construction.

### Une correction de méthode

Le premier banc de la série (traduction) a rendu 14,52 s sur son cas court
contre 3,77 s pour la 3.6, les deux autres cas étant comparables — signature
d'un cache de pages froid, les poids venant d'être écrits. Le banc a été
**relancé en fin de série** : le cas court retombe à 4,34 s, le p50 de 14 521 à
8 519 ms. C'est ce second relevé qui est au manifeste.

---

## 3. Recommandations de remplacement

**Aucune.** Ce n'est pas une prudence de forme : le parc n'a aucun moyen de
départager la qualité, et le coût mesuré est écrasant.

| Capacité | Titulaire de fait | Son pic | Son débit | Coût de Qwen3.8 |
|---|---|---|---|---|
| `text-generation` | `qwen25-coder-7b@4bit` | 4,14 Gio | 12,8 s | 3,5× la mémoire, **5,9× le temps** |
| `translation` | `qwen3-4b-traduction@4bit` | 2,57 Gio | 1,6 s | 5,9× la mémoire, **5,9× le temps** |
| `tool-use` | `qwen3-4b-outils@4bit` | 2,73 Gio | 1,0 s | 6,0× la mémoire, **7,6× le temps** |
| `image-to-text` | `qwen3-vl-8b-describe@4bit` | 6,15 Gio | 6,6 s | 2,6× la mémoire, 3,1× le temps |
| `document-to-text` | `qwen3-vl-8b-ocr@4bit` | 6,25 Gio | 7,4 s | 2,5× la mémoire, 2,8× le temps |
| `image-detect` | `qwen3-vl-8b-detect@4bit` | 6,15 Gio | 4,2 s | 2,6× la mémoire, 3,2× le temps |
| `video-to-text` | `qwen3-vl-8b-video@4bit` | 6,77 Gio | 7,4 s | 2,6× la mémoire, 4,7× le temps |

S'y ajoute un coût que le tableau ne montre pas : **un job de cette famille vide
le parc entier**. Aucun autre modèle ne peut être résident pendant qu'il tourne,
et le contrôle d'admission décharge tout à chaque appel.

**La seule capacité où la discussion mérite d'être ouverte est `image-detect`**,
et sur un argument qui n'est pas le gain de Qwen3.8 sur les titulaires mais son
gain sur `qwen36-27b`, qui ne rendait rien. Encore faut-il vérifier la grille.

### `video-to-text` tient à 0,33 Gio du plafond

17,43 Gio pour un budget de 17,76, soit 98,1 %. Ce n'est pas une marge, c'est un
bord de falaise. Le pic suit `max_frames` et non la durée de la vidéo ; le cas
le plus long du banc en échantillonne 16, et le contrat en autorise davantage.
**Toute demande plus longue sortira du budget sans préavis.** Le manifeste le
dit, mais le banc ne l'éprouve pas — il faudrait un cas à 32 images pour établir
la pente, et ce cas ferait échouer les modèles plus légers de la même capacité.
C'est le même besoin de « banc qui s'adapte au variant » que le cycle du 23 août
avait identifié et qui n'est toujours pas comblé.

---

## 4. Rejets motivés

Neuf conversions `mlx-community` du même modèle, plus six familles tierces.

| Variant | Disque | Motif |
|---|---|---|
| `Qwen3.8-27B-4bit` | 16,08 Go | Affine 4 bits — 840 Mo de plus que le mxfp4, et c'est le format qui a échoué six fois sur sept sur la 3.6. Le mesurer coûterait sept bancs pour reproduire un échec connu. |
| `Qwen3.8-27B-nvfp4` | 16,08 Go | Aucun gain sur le mxfp4, et plus lourd de 840 Mo. |
| `Qwen3.8-27B-oQ4` | 16,68 Go | Plus lourd que le mxfp4 de 1,44 Go. |
| `Qwen3.8-27B-OptiQ-4bit` | 20,68 Go | Hors budget, **et sans tour de vision** — perdrait quatre des sept capacités. |
| `Qwen3.8-27B-oQ6` | 23,33 Go | Hors budget. |
| `Qwen3.8-27B-mxfp8` | 28,69 Go | Hors budget. |
| `Qwen3.8-27B-8bit` | 29,53 Go | Hors budget. |
| `Qwen3.8-27B-bf16` | 54,74 Go | Hors budget. |
| `Qwen3.8-27B-MTP-*` | 0,25 Go | Tête de prédiction multi-jetons seule, pas un modèle. Voir §5, où elle revient comme déclencheur. |
| `lmstudio-community/Qwen3.8-27B-MLX-*` | — | Doublons des conversions `mlx-community`, sans gain de poids. |
| `lukaskremla/*-TextOnly`, `majentik/*`, `avlp12/*`, `maglun/*`, `Jundot/*`, `Youssofal/*`, `keXjos/*` | — | Conversions tierces non canoniques ; les `TextOnly` amputent la tour de vision, donc quatre capacités. |
| `abliterated` / `uncensored` / `OBLITERATED` / `Heretic` / `AEON` / `Cold-Fusion` | — | Hors périmètre : ce ne sont pas les poids de référence. |
| GGUF (`unsloth`, `bartowski`, `mradermacher`…) | — | Le parc n'a pas de runtime GGUF ; en ajouter un supposerait un serveur tiers que le superviseur ne contrôle pas. |
| NVFP4 / AWQ / exl3 / FP8 (`RadixArk`, `True2456`, `turboderp`, `Qwen/Qwen3.8-27B-FP8`) | — | Formats CUDA / vLLM / TensorRT — aucun chemin Apple Silicon. |

---

## 5. Déclencheurs — un levé, quatre posés

**Levé.** Le `candidats.json` du 2026-08-23 attendait pour la classe 27B « une
conversion MLX du modèle de référence à 3 bits ou moins, tour de vision
comprise, sous ~13 Go de poids ». La condition était mal posée : ce n'est pas le
poids sur disque qui décidait mais le mode de quantification. Le mxfp4 à
15,24 Go tient là où le 4 bits affine à 16,08 Go échoue.

**Posés par ce cycle :**

1. **Le décodage spéculatif par tête MTP** — le seul levier connu contre le vrai
   défaut de ce gabarit, la latence. Ce qui manque a été délimité :
   - les poids existent — `mlx-community/Qwen3.8-27B-MTP-mxfp4`, 0,25 Go, soit
     1,6 % du modèle ;
   - **le runtime sait s'en servir** — `mlx_vlm/speculative/` (`mtp.py`,
     `eagle3.py`, `dflash.py`, `ddtree.py`) et un drafter dédié à cette
     architecture, `mlx_vlm/speculative/drafters/qwen3_5_mtp/` ; le modèle écarte
     explicitement les clés `mtp.` du checkpoint de base
     (`models/qwen3_5/qwen3_5.py:145`, « The MTP draft shard is separate from the
     base model ») et implémente `chunked_prefill_policy` pour
     `draft_kind == "mtp"` ainsi que `rollback_speculative_cache` ;
   - **le schéma d'Écurie sait où le déclarer** — `extra_sources` et son `role`
     obligatoire, déjà employés par `cad-recode` et `smolvla-libero`.

   Il ne manque que le câblage dans l'adaptateur `mlx_vlm`, qui ne passe
   aujourd'hui aucun `draft_model`.

2. **Qwen3.8-9B** — même génération, gabarit quatre fois plus léger, et le seul
   membre de la famille qui aurait une chance de **cohabiter** avec le reste du
   parc au lieu de le vider. Aucune conversion `mlx-community` à ce jour
   (vérifié : zéro résultat) ; des conversions tierces existent. À instruire en
   priorité si la 27B est jugée bonne mais trop chère.

3. **L'exécuteur de golden set** — troisième cycle bloqué par son absence. Les
   jeux existent pour cinq des sept capacités visées (`text-generation`,
   `translation`, `tool-use`, `image-to-text`, `document-to-text`) et rien ne les
   fait tourner : ni commande CLI — `ecurie` n'expose que
   `ps/unload/pull/run/bench/serve/registry/store/env` — ni route d'API. **C'est
   désormais le chantier qui bloque le plus de décisions du parc.**

4. **Un banc qui s'adapte au variant**, reporté du cycle du 23 août et rendu plus
   urgent par la falaise de `video-to-text` à 98,1 % du budget.

---

## 6. Dette constatée, hors périmètre

Les sept manifestes `registry/models/qwen36-27b-*.yaml` portent sur leur variant
`mxfp4` un bloc `profile:` mesuré **et** un caveat affirmant « Non téléchargé :
c'est le variant 4bit qui a été mesuré ». Les poids sont sur le disque (14 Gio
dans le cache HF) et les sept relevés sont versionnés depuis le commit `981ee83`.
Le commit `3d5b680` a collé les vingt-cinq profils mesurés sans reprendre les
caveats écrits avant la mesure.

**Non corrigé ici** : la famille `qwen36-27b` est explicitement hors du périmètre
de ce cycle.

---

## 7. Plan de GC

| Poste | Gain | Condition |
|---|---|---|
| `mlx-community/Qwen3.6-27B-4bit` | **15 Gio** | Ce variant est **mesuré comme inutilisable** : six capacités sur sept échouent en « Insufficient Memory ». Rien ne justifie de le garder sur le disque, et son retrait n'attend aucune décision de qualité. |
| `mlx-community/Qwen3.6-27B-mxfp4` | **14 Gio** | Dominé sur les sept capacités par Qwen3.8-27B@mxfp4, **à mémoire strictement égale** et pour un temps inférieur sur six d'entre elles. À retirer si la 3.8 est retenue ; à garder tant qu'elle ne l'est pas. |
| Blobs orphelins | 536,5 Mo | `ecurie store trash` — sans condition. |
| Duplication inter-gestionnaires | 95,0 Mo | Résoluble par lien dur. |

Le premier poste est le seul qui soit **sans condition et déjà justifié par une
mesure** : 15 Gio rendus pour un variant dont l'inutilisabilité est établie.

**Comptabilité du parc après ce cycle** : 332,81 Go apparents — 174,47 Go dans
le cache Hugging Face, 144,05 Go de poids déclarés hors gestionnaire,
14,29 Go dans Ollama. Le chiffre bondit par rapport aux 46,58 Go du dernier
rapport pour deux raisons sans rapport entre elles : la base d'état n'avait pas
été rescannée depuis plusieurs jours, et 134 Gio de poids installés à la main
viennent d'être déclarés au scan.
