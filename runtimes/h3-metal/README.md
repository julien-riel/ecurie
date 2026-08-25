# Runtime `h3-metal` — MiniMax-H3

Environnement isolé du variant `minimax-h3@bf16-ssd`
(`registry/models/minimax-h3.yaml`), capacité **`text-to-video`** : une consigne
en entrée, une vidéo **sonorisée** en sortie.

C'est le premier runtime du parc dont l'inférence ne se fait pas dans un
processus Python. Elle est faite par **`h3`**, une implémentation C + Metal
écrite par Salvatore Sanfilippo ([antirez/h3.c](https://github.com/antirez/h3.c),
licence MIT). `run.py` ne calcule rien : il compose une ligne de commande, lance
le binaire, échantillonne sa mémoire, lit son `--profile`, et range ce qu'il a
produit.

C'est aussi la première capacité du parc à rendre du son en même temps que de
l'image, ce qui a valu au contrat `text-to-video` un champ de sortie `audio`.

> **LICENCE À RESTRICTIONS TERRITORIALES.** Les poids sont sous *MiniMax H3
> Community License Agreement* (2 août 2026). Le « territoire applicable » est
> le monde **moins l'Union européenne, le Royaume-Uni, la Corée du Sud et les
> États-Unis d'Amérique**. L'article V.4 interdit l'usage, la reproduction, la
> distribution et **l'affichage des sorties** hors de ce territoire : les vidéos
> produites tombent sous la même restriction que le modèle. S'y ajoutent une
> autorisation écrite préalable au-delà de 20 M$ de revenus annuels,
> l'obligation d'afficher « MiniMax H3 » sur toute interface commerciale,
> l'interdiction d'employer les sorties pour améliorer un autre modèle, et des
> garde-fous à implémenter avant toute mise à disposition à des tiers.
> Le code de `h3.c`, lui, est sous MIT et ne porte aucune de ces restrictions.

---

## Construire l'environnement

Trois étapes, dont deux ne sont **pas** faites par `ecurie env sync`.

### 1. L'interpréteur

```sh
uv run ecurie env sync h3-metal
```

Il ne fabrique qu'un interpréteur nu : ce runtime n'a aucune dépendance Python.
Voir l'en-tête de `pyproject.toml` pour la raison.

### 2. FFmpeg et FFprobe sur le PATH

```sh
brew install ffmpeg          # fournit les deux
ffmpeg -version && ffprobe -version
```

Le binaire `h3` appelle FFmpeg pour assembler le MP4 ; l'adaptateur appelle
FFprobe pour vérifier ce qui en est sorti et en extraire la bande-son. Sans eux,
`load` échoue en le disant, plutôt que de laisser la panne se présenter comme un
MP4 vide.

### 3. Le binaire `h3` — vendoré, jamais versionné

Le code amont est publié dans un dépôt Git avec un `Makefile`, pas sur PyPI.
Il n'a donc pas sa place dans ce dépôt-ci, et n'y est pas.

```sh
mkdir -p runtimes/h3-metal/vendor
git clone https://github.com/antirez/h3.c runtimes/h3-metal/vendor/h3.c
make -j8 -C runtimes/h3-metal/vendor/h3.c
runtimes/h3-metal/vendor/h3.c/h3 --help
```

`run.py` cherche le binaire dans cet ordre :

1. la variable d'environnement **`ECURIE_H3_BIN`**, si elle désigne un exécutable ;
2. `runtimes/h3-metal/vendor/h3.c/h3` ;
3. un `h3` sur le PATH.

La variable d'environnement existe pour une raison précise : une construction de
`h3.c` déjà faite ailleurs sur la machine n'a aucune raison d'être refaite ici.
Elle se pose dans `~/.ecurie/config.toml` ou dans l'environnement du superviseur.

---

## Les poids

**Ce runtime ne télécharge rien.** Les 134 Gio de MiniMax-H3 sont désignés par le
manifeste en `source: {kind: local}`, c'est-à-dire un chemin absolu sur la
machine, déclaré dans `[scan] declared` de `~/.ecurie/config.toml` pour que
`ecurie store scan` les compte.

L'instantané attendu est celui de Hugging Face, avec sa structure d'origine :

```
MiniMax-H3/
  model_index.json
  FL2VA/
    model_index.json
    transformer/      13 fichiers   535 tenseurs   61,7 Gio
    text_encoder/     14 fichiers  1058 tenseurs   62,1 Gio   (Qwen3-VL-32B)
    video_vae/         1 fichier    560 tenseurs    9,7 Gio
    audio_vae/         1 fichier   1087 tenseurs    0,56 Gio
    tokenizer/  processor/
```

`./h3 --info -d <chemin>` vérifie cette disposition sans mapper les poids, et
c'est ce que `load` appelle. Un inventaire incomplet y est visible immédiatement.

**Le DiT Ref2VA n'est pas dans cette partition.** `--info` le rend à `0 fichier,
0 tenseur` sur l'instantané FL2VA, et le `model_index.json` ne déclare que
`["t2va", "fl2va"]`. Les options `--ref-image`, `--ref-video` et `--ref-audio`
du binaire sont donc inutilisables ici, et l'adaptateur ne les expose pas.

---

## Pourquoi `--ssd-streaming` n'est pas optionnel

Le DiT en BF16 occupe environ **36,5 Gio** de stockage tenseur suivi quand il est
entièrement résident — le double du budget de la machine de référence (17,76 Gio).
`--ssd-streaming` ne garde que deux blocs en mémoire et lit le suivant pendant
que le GPU travaille sur le courant, ce qui ramène le DiT à environ 2 Gio. C'est
un échange mémoire contre vitesse assumé par l'amont, qui mesure 26 à 84 % de
lenteur supplémentaire selon la définition.

Sur la machine de référence, une sonde à 256×256, 8 images demandées et 4 pas a
lu **115,6 Gio depuis le SSD à 5,05 Gio/s**, dont 13,5 s d'attente non masquée
sur 23,2 s de débruitage. Le SSD est donc le facteur limitant de ce runtime, pas
le GPU.

C'est pourquoi le variant s'appelle `bf16-ssd` : le nom porte la condition
d'exécution, et non un détail de réglage.

---

## Où va le pic mémoire, et comment il est mesuré

Le binaire est un processus séparé : `mx.get_peak_memory()` n'existe pas ici, et
le RSS du worker Python ne mesure que le worker. L'adaptateur combine donc deux
sources, et retient la plus grande — les deux se disputent la première place
selon la phase :

1. **Le RSS du processus fils**, échantillonné toutes les 100 ms par `ps`.
2. **Les `peak=` du `--profile`**, un par phase, dont le maximum est retenu.

Le `--profile` révèle que le pic n'est pas là où on l'attend. Relevé de la sonde
citée plus haut :

| Phase | Mur | Pic GPU suivi | Alloué |
|---|---|---|---|
| Encodeur de texte Qwen | 11,66 s | **3,64 Gio** | 46,86 Gio |
| DiT — chargement | 8,37 s | 1,55 Gio | 27,41 Gio |
| DiT — débruitage Euler | 23,22 s | 1,55 Gio | — |
| VAE audio | 0,30 s | 0,28 Gio | 0,54 Gio |
| VAE vidéo | 5,36 s | 0,59 Gio | 9,37 Gio |

**Sur cette sonde, le maximum est l'encodeur de texte, pas le DiT.** Les phases
se succèdent au lieu de s'additionner : c'est ce qui permet à un modèle de
134 Gio sur le disque de tenir dans une fraction du budget. La colonne
« alloué » est le cumul des allocations d'une phase, pas une occupation
simultanée — la confondre avec le pic ferait refuser un job qui passe très bien.

À la définition du banc (512×320), c'est en revanche le **VAE vidéo** qui domine,
à 9,55 Gio, et il y reste à l'octet près pour 39, 56 ou 107 images produites : il
décode par tuiles à empreinte fixe. Son mur, lui, croît linéairement — 26,6 s,
43,8 s, 88,6 s. Une pente nulle avec un R² parfait ressemble à un instrument
gelé ; ici les deux autres instruments bougent bien (pic du DiT de 2,02 à
2,40 Gio, RSS du fils de 3,47 à 5,26), ce qui écarte l'hypothèse.

### Le RSS de ce runtime n'est pas reproductible

C'est le piège de mesure propre à ce modèle, et il faut le connaître avant de
comparer deux relevés. Le **même job** rendu quatre fois a donné un pic RSS de
3,47, 3,60, 5,45 puis 5,85 Gio, pendant que le profil Metal rendait le même octet
à chaque fois.

La raison est le fichier de poids de 134 Gio : le RSS compte ses pages mappées,
que macOS garde ou évince selon la pression mémoire du moment. C'est un état de
la machine à un instant, pas une propriété du modèle — alors que le
`peak_unified_memory_bytes` d'un profil est précisément le chiffre que le README
du dépôt promet portable d'un Mac à l'autre.

Le maximum des deux reste retenu, par prudence. Mais la métrique `peak_source`
d'un job dit lequel a gagné, et **deux mesures ne se comparent que si leur
`peak_source` est le même**.

---

## Ce que l'adaptateur ne fait pas

- **Il n'expose pas le mode interactif** de `h3` (session Iris, `!first`,
  `!ref-image`…). Un job Écurie est une invocation, pas une session.
- **Il n'expose pas les `--use-slower-*`** ni `--use-int8-row-fc2`. Ce sont des
  leviers de parité numérique et d'optimisation par machine, pas des paramètres
  de capacité ; les figer dans le manifeste vaut mieux que les offrir au job.
- **Il n'expose pas `--show`.** L'aperçu terminal charge un VAE résident et
  ajoute environ 10 Gio de résidence temporaire — sur ce budget, c'est la
  différence entre tenir et ne pas tenir.
- **Il ne fait pas varier la cadence.** H3 produit à 24 im/s, point. Le champ
  `fps` du contrat est reçu et ignoré, ce que le manifeste déclare en caveat.
