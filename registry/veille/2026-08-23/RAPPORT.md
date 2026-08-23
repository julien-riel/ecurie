# Veille — 2026-08-23

Cycle ciblé, déclenché par une demande explicite : évaluer **Qwen 3.6 27B** et
l'ajouter au parc pour les capacités qu'il supporte. Aucun balayage général des
sources n'a été mené ; ce rapport ne dit rien de ce qui a bougé ailleurs.

## 1. Verdict

**Ce qui change** : quatre manifestes `status: candidate` entrent au registre —
`qwen36-27b-describe`, `-ocr`, `-detect`, `-video` —, tous en `tier: absent`,
tous sans profil. Le modèle existe, sa licence est Apache-2.0, et son chemin
d'exécution est réel : `mlx-vlm` 0.6.15, déjà installé, porte l'architecture
`qwen3_5`.

**Ce qui ne change pas** : rien n'est téléchargé, aucun titulaire n'est déplacé,
aucun manifeste `active` n'est touché. Et le modèle **ne tient pas dans le budget
mémoire** : 15,2 Go de poids pour le variant le plus léger, pic estimé entre 17,4
et 19,2 Go, contre un plafond praticable de 17 Go. Sur les sept capacités qu'il
sait remplir, trois n'ont par ailleurs aucun adaptateur pour les servir.

**Action demandée** : trancher entre éprouver quand même le variant `mxfp4`
(15,2 Go de téléchargement, avec une chance sérieuse que la mesure conclue au
rejet) et classer le dossier en attendant un déclencheur. La règle de filtrage du
projet dit rejet d'office ; la marge est assez faible pour que la question mérite
d'être posée plutôt que tranchée par un tableur.

## 2. Recommandations de remplacement

**Aucune.** Qwen3.6-27B est un challenger crédible de `qwen3-vl-8b-*` sur les
quatre capacités visuelles, mais aucune recommandation ne peut être formulée :

| Capacité | Titulaire de fait | Challenger | Gain | Coût disque | Coût mémoire |
|---|---|---|---|---|---|
| `image-to-text` | qwen3-vl-8b-describe (5,78 Go, pic 6,60) | qwen36-27b-describe | **inconnu** | +9,46 Go | pic 17,4–19,2 Go |
| `document-to-text` | qwen3-vl-8b-ocr (5,78 Go, pic 6,71) | qwen36-27b-ocr | **inconnu** | +9,46 Go | pic 17,4–19,2 Go |
| `image-detect` | qwen3-vl-8b-detect (5,78 Go, pic 6,60) | qwen36-27b-detect | **inconnu** | +9,46 Go | pic 17,4–19,2 Go |
| `video-to-text` | qwen3-vl-8b-video (5,78 Go, pic 7,26) | qwen36-27b-video | **inconnu** | +9,46 Go | pic 17,4–19,2 Go |

Le gain est marqué inconnu et non « probable » : la fiche amont ne compare
Qwen3.6-27B qu'à Qwen3.5-27B, à Gemma4-31B et à des modèles fermés. **Qwen3-VL
8B n'apparaît dans aucune de ses tables.** Un modèle trois fois plus gros est
plus souvent meilleur qu'il n'est pire, mais « plus souvent » n'est pas une
mesure, et le golden set n'a pas tourné. Une recommandation sans gain chiffré
est refusée par la procédure — celle-ci l'est donc aussi.

Le coût, lui, est chiffré : **+9,46 Go de disque** pour les quatre capacités
réunies (poids partagés, un seul téléchargement), et un pic qui passe de ~7 Go à
~18 Go, c'est-à-dire d'un modèle qui cohabite avec d'autres à un modèle qui
occupe la machine à lui seul.

## 3. Candidats à éprouver

Un seul mérite le téléchargement, et sous réserve.

| Variant | Dépôt | Disque | Pic estimé | Budget |
|---|---|---|---|---|
| **`mxfp4`** | `mlx-community/Qwen3.6-27B-mxfp4` | **15,24 Go** | 17,4 – 19,2 Go | dépassé |
| `4bit` | `mlx-community/Qwen3.6-27B-4bit` | 16,08 Go | 18,3 – 20,3 Go | dépassé |

Le pic est estimé en appliquant aux poids le facteur pic/disque **mesuré** sur
les quatre Qwen3-VL 4 bits du parc — 1,142 · 1,142 · 1,162 · 1,257. C'est une
estimation de filtrage : elle n'entre dans aucun manifeste, et le banc d'essai
reste seul habilité à écrire un profil.

Espace disque : 534 Gio libres sur 926 (57 %). Le téléchargement ne pose aucun
problème de place — le seuil des 15 % est loin. **Le blocage est la mémoire, pas
le disque.**

Si l'épreuve est décidée, elle doit commencer par `mxfp4` : à quantification
équivalente il est plus léger de 840 Mo, et `mlx` 0.32.1 connaît son mode
(`group_size` 32, 4 bits — vérifié dans `mlx/nn/layers/quantized.py`). Le variant
`4bit` n'a d'intérêt que pour comparer les deux quantifications, et il est dominé
sur le seul critère qui bloque.

## 4. Rejets motivés

| Objet | Motif |
|---|---|
| `Qwen3.6-27B-6bit` (22,8 Go), `-8bit` (29,5 Go), `-bf16` | Pic très au-delà de 17 Go. |
| `Qwen3.6-27B-nvfp4` (16,08 Go) | Aucun gain de taille sur `mxfp4`, mode moins courant. |
| `Qwen3.6-27B-OptiQ-4bit` (20,0 Go) | Plus lourd que le 4 bits standard, et **sans tour de vision** — inutilisable pour les quatre capacités visées. |
| `Qwen3.6-27B-MTP-4bit` (0,26 Go) | Tête de prédiction multi-jetons seule, pas un modèle complet. |
| AWQ / GPTQ-Int4 (cyankiwi, QuantTrio, Intel, palmfuture…) | Formats CUDA/vLLM. Aucun chemin d'exécution Apple Silicon. |
| Dérivés *abliterated* / *uncensored* / finetunes tiers | Ne sont pas les poids de référence ; hors périmètre d'une veille de parc. |
| `text-generation`, `tool-use`, `translation` | Supportées par le modèle, **aucun adaptateur pour les servir** — voir §5. |
| `audio-to-text`, `speech-to-text` | Aucun encodeur audio dans la configuration. Qwen3.6-27B est vision+vidéo, pas omni. |
| `image-segment`, `text-to-image` | Le modèle ne rend ni masques ni images. |

## 5. Ce que le modèle sait faire et que le parc ne peut pas lui demander

Le modèle remplit **sept** capacités du registre. Quatre ont un adaptateur, et
ce sont les quatre qui reçoivent un manifeste. Les trois autres n'en ont pas :

- **`text-generation`, `translation`, `tool-use`** — leurs workers
  (`mlx_lm`, `mlx_lm_translate`, `mlx_lm_tools`) sont enregistrés pour le runtime
  `mlx-lm`, or **`mlx-lm` ne charge pas `qwen3_5`** : le module n'existe pas dans
  la version installée. Seul `mlx-vlm` sait charger ces poids, et
  `WORKER_MODULES_BY_CAPABILITY` ne déclare aucun couple `("mlx-vlm", …)` pour
  ces trois capacités.
- **`tool-use` a un second blocage, indépendant du premier** : Qwen3.6 émet ses
  appels en XML imbriqué — `<tool_call><function=nom><parameter=clé>valeur</parameter></function></tool_call>`.
  L'extracteur de `mlx_lm_tools` capture bien la balise, puis tente un
  `json.loads` sur son contenu, qui n'est pas du JSON. Il rendrait zéro appel sur
  un modèle ayant parfaitement choisi son outil — soit exactement le défaut que
  l'en-tête de ce worker dit vouloir éviter. Une cinquième stratégie
  d'extraction serait à écrire.

Écrire des manifestes pour ces trois capacités aurait produit des fichiers
valides au schéma et cassés au lancement. Ils sont donc laissés de côté, et
consignés ici comme travail identifié.

Un troisième point touche les **quatre** capacités retenues : Qwen3.6 raisonne à
voix haute par défaut (`<think>…</think>`), le gabarit accepte un drapeau
`enable_thinking`, et **aucun adaptateur du parc ne le transmet**. Sur la
description c'est une gêne ; sur l'OCR le préambule fausse la comparaison au
texte attendu ; sur la détection il menace de faire tomber l'extraction des
boîtes à zéro objet. Rendre ce drapeau transmissible est le préalable à toute
mesure honnête, avant même la question de la mémoire.

## 6. Déclencheurs

Aucun déclencheur levé ce cycle. Trois posés sur ce dossier :

1. Une conversion MLX du modèle de référence à **3 bits ou moins, tour de vision
   comprise**, sous ~13 Go de poids. Aucune n'existe aujourd'hui : les seules
   variantes basse précision publiées sont des finetunes tiers.
2. Une machine cible à **plus de 24 Go** de mémoire unifiée.
3. Un adaptateur `mlx-vlm` transmettant **`enable_thinking`**.

**À surveiller** — `Qwen/Qwen3.8-27B` est paru le 2026-08-14, neuf jours avant ce
cycle. Sa conversion `mlx-community/Qwen3.8-27B-4bit` pèse 16,08 Go, soit
exactement le même dépassement, et aucune variante `mxfp4` n'existe encore. Le
signaler n'est pas une recommandation d'en changer : **le mur est la taille de
la classe 27B sur 24 Go, pas le numéro de version.**

## 7. Plan de GC

**Sans objet.** Rien n'a été téléchargé, aucun poids n'est devenu orphelin, et
aucun variant n'a changé de statut. Le parc occupe ce qu'il occupait hier.

Pour mémoire, si l'épreuve du §3 était décidée : 15,24 Go entreraient en
quarantaine, à retirer d'un seul geste si la mesure conclut au rejet — les quatre
manifestes partagent un unique jeu de poids.
