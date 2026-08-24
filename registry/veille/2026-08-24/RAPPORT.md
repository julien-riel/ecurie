# Famille visage — instruction et mise en service

**2026-08-24.** Traite le §D du backlog de veille du 2026-08-23. Six capacités
ouvertes, un runtime neuf, huit manifestes, douze variants mesurés. Un champ
ajouté au schéma de capacité.

Ce n'est pas une veille au sens habituel : le backlog proposait d'instruire,
la demande était d'implémenter. Le rapport garde donc la forme d'une veille —
ce qui a été vérifié, ce qui a été mesuré, ce qui a été écarté et pourquoi —
mais tout ce qu'il décrit est en place et exécutable.

---

## 1. La piste, et ce qu'elle était vraiment

Le backlog annonçait **MLX-UniFace**, « la seule piste optimisée pour Apple
Silicon » du §D. Vérification faite, ce n'est pas un projet MLX : c'est
**UniFace 4.0.0**, une bibliothèque MIT qui n'est qu'une couche de pré et
post-traitement au-dessus d'onnxruntime. Pas de torch, pas de mmcv, pas de code
amont à vendorer, sept dépendances légères.

L'écart de nom n'enlève rien à la conclusion du backlog, il la renforce : ONNX
est l'un des quatre moteurs que le parc exécute déjà (§F.1), et le coût d'entrée
est celui d'un `pyproject.toml` de vingt lignes.

**Ce qui a décidé, et qui n'était pas dans le backlog :** son dépôt de poids
`yakhyo/uniface-weights` porte 64 fichiers ONNX **avec un tableau de licences
vérifié source par source** — licence du dépôt amont, licence du jeu
d'entraînement, et une catégorie « aucune licence trouvée » pour ce qui n'a pas pu
être établi. C'est exactement ce que réclame le filtre §F.3, fait par quelqu'un
d'autre et vérifiable. Aucune autre piste du §D n'offre cela.

## 2. Ce qui est entré

| Capacité | Modèles | Licence la plus permissive |
|---|---|---|
| `face-detect` | `retinaface` (3 variants) | MIT / données WIDER FACE → `restricted` |
| `face-landmark` | `pipnet` (2), `face-mesh` (2) | **Apache-2.0 franc** pour Face Mesh |
| `face-parse` | `bisenet-parsing` (2) | MIT / données CelebAMask-HQ → `restricted` |
| `face-embed` | `edgeface` (2), `arcface` (1) | BSD-3 pour EdgeFace ; ArcFace `research-only` |
| `face-headpose` | `headpose-6drepnet` (2) | MIT franc |
| `face-gaze` | `mobilegaze` (2) | MIT franc |

Douze variants sont téléchargés et mesurés sur cette machine. Les relevés sont
dans `registry/measurements/<ref>/mac17-4-24-gio.json`, les profils reportés aux
manifestes.

**Deux capacités sur six sont franchement permissives** (`face-headpose`,
`face-gaze`), et une troisième l'est par l'un de ses modèles (`face-landmark` via
Face Mesh). Les trois autres portent `restricted` : leur code est sous MIT, mais
WIDER FACE, 300W, WFLW et CelebAMask-HQ sont publiés pour la recherche non
commerciale, et **une restriction posée par un éditeur de données ne s'efface pas
parce que le dépôt qui l'emploie a choisi une licence permissive.**

## 3. Le champ que la famille a fait ajouter au schéma

Le backlog §D posait la question sans la trancher : « la reconnaissance faciale
identifie des personnes réelles […] le registre a `license_class` pour les
restrictions juridiques, il n'a rien pour cet usage-là. À trancher au moment du
contrat, pas après. »

Tranché : `human_subject` sur le contrat de capacité, à trois valeurs.

- **`analyzes`** — mesure quelqu'un déjà présent dans l'entrée, sans le nommer ni
  le reproduire. Cinq des six capacités visage.
- **`identifies`** — rattache l'entrée à une personne nommable. `face-embed`.
- **`synthesizes`** — produit l'image ou la voix d'une personne faisant ce
  qu'elle n'a pas fait. **`voice-clone`**, qui est au parc depuis le v0.3.

Ce dernier point est la preuve que le champ n'est pas un ornement de la famille
visage : la capacité la plus concernée du parc existait déjà et n'avait aucun
moyen de le dire. Un futur face swap ou talking head hérite du marquage sans
qu'on y repense.

Porté par le **contrat** et non par le variant, parce que c'est la capacité qui
décide : tous ses modèles rendent la même chose. Et distinct de `license_class`,
parce que les deux questions ne se recouvrent pas — EdgeFace est sous BSD-3, la
licence la plus permissive de la famille, et cela ne rend pas plus légitime
d'encoder le visage de quelqu'un qui n'a rien demandé.

## 4. Ce que la mesure a démenti

### Le plus gros détecteur est le moins fiable

Sur la charge type — quatre visages à quatre échelles, trois définitions
d'entrée — le nombre de visages trouvés :

| Variant | Taille | 320 | 640 | 1280 |
|---|---|---|---|---|
| `retinaface_mnet025` | 1,7 Mo | **4** | **4** | **4** |
| `retinaface_mnet050` | 6,3 Mo | **4** | **4** | **4** |
| `retinaface_mnet_v1` | 15,9 Mo | 3 | 4 | 4 |
| `retinaface_mnet_v2` | 11,9 Mo | 2 | **1** | 4 |
| `retinaface_r18` | 45,9 Mo | 3 | 4 | 4 |
| `retinaface_r34` | 84,4 Mo | 3 | 1 | 4 |
| `retinaface_r50` | 104,4 Mo | **0** | 2 | 4 |

`mnet_v2` trouve **moins** de visages à 640 qu'à 320 : une réponse non monotone
est inutilisable en amont d'une autre capacité, puisqu'un visage manqué n'aura ni
points clés, ni régions, ni empreinte. `r50`, soixante fois plus lourd que
`mnet025`, n'en trouve aucun à 320.

`retinaface_mnet050` est donc proposé comme titulaire, et posé comme détecteur
par défaut des cinq autres capacités. Ce n'est pas le plus gros ni le plus récent.

**Réserve honnête :** ces visages sont calculés, pas photographiés. Les gros
réseaux sont probablement plus sensibles à l'absence de texture de peau. Le
classement pourrait s'inverser sur des photographies, et c'est au golden set de
le dire — la charge type mesure un coût.

### La vérification croisée que seule une charge calculée permet

Les quatre visages sont rendus à des lacets **connus**, puisque c'est la recette
qui les pose. `headpose-6drepnet@r18` mesure :

| Lacet posé | 0° | +24° | −18° | +8° |
|---|---|---|---|---|
| Lacet mesuré | −0,2° | +20,1° | −13,9° | +5,3° |

Signes justes, amplitudes cohérentes. La convention de signe déclarée par le
contrat n'est donc pas une convention d'espoir : elle est vérifiée contre une
vérité terrain qu'on contrôle, ce qu'aucune photographie ne donnerait sans
annotation manuelle. Le tangage, lui, porte un biais d'environ +13° — à confirmer
sur le golden set.

### L'identité fonctionne, et se vérifie sans index

Le backlog notait qu'une capacité rendant un vecteur « appelle un index, donc une
brique que le parc n'a pas ». Le contrat contourne : `compare_to` prend une
seconde image et rend le cosinus. Deux vues du même visage de synthèse, de face
et de trois quarts, donnent **0,92** avec `edgeface@s-gamma-05`. Un vecteur de
512 nombres ne se relit pas ; un cosinus se lit.

## 5. Un défaut du socle, révélé par ce cas

Le parc avait jusqu'ici **un dépôt Hugging Face par modèle**, si bien que mesurer
le dossier revenait à mesurer le variant. `yakhyo/uniface-weights` rompt
l'équivalence : soixante-quatre modèles y cohabitent, et un variant n'en veut
qu'un ou deux.

Résultat au premier banc d'essai : `retinaface@mnet050`, qui pèse 6,6 Mo,
déclarait **595 Mo** de disque — la taille de l'instantané entier. Les douze
variants de la famille auraient annoncé 7 Go pour 600 Mo réellement partagés.

Corrigé dans `bench.py` : `_tree_bytes` filtre désormais par les `allow_patterns`
du variant, avec le filtre de `huggingface_hub` — celui-là même qui a décidé de
ce qui a été téléchargé. Deux filtres pour une seule question finiraient par
diverger.

## 6. Écarté, et pourquoi

**SCRFD, YOLOv8-Face, CenterFace, BlazeFace.** SCRFD et YOLOv8 ont une entrée
figée dans leur graphe ONNX et échouent sur toute définition autre que la leur ;
la charge type de `face-detect` fait varier ce paramètre, donc ils ne seraient
pas profilables. SCRFD est de toute façon `research-only` (InsightFace).
CenterFace ne trouve aucun des quatre visages de la charge. BlazeFace rend six
points d'ancrage au lieu de cinq et ne peut pas alimenter `face-embed`.

**`face-restore`** (GFPGAN, CodeFormer, RestoreFormer++). Hors du périmètre
retenu. La piste propre existe — RestoreFormer++ est en Apache-2.0 franc, sans
les clauses non commerciales de S-Lab qui grèvent CodeFormer — mais les
conversions MLX publiées visent `mlx-swift`, et le chemin Python suppose de
vendorer le code amont comme `hunyuan3d`, plus un runtime torch de plus.

**`image-to-face-mesh`** (DECA, EMOCA, MICA sur FLAME). Bloqué au §F.3 : FLAME
demande une inscription et une licence de recherche, et ne se télécharge pas
sans accord préalable.

**`portrait-animate`** (LivePortrait, JoyVASA). Non instruit. Relève de
`human_subject: synthesizes`, et le marquage existe désormais pour l'accueillir.

**`face-expression`** (AffectNet via DDAMFN). Les poids sont dans le dépôt
(`affecnet7`, `affecnet8`) mais leur licence n'a pas pu être établie — le README
du miroir le dit explicitement, et « absent un octroi de licence, présumer
qu'aucun droit d'usage n'est accordé ». Écarté par le §F.3, sans manifeste.

Même raison pour **`fairface`** et **age/gender** : licence non établie, et une
capacité qui prédirait l'âge, le sexe ou l'origine d'une personne mériterait sa
propre discussion d'usage avant sa discussion technique.

## 7. Ce qui reste à faire

- **Valider ou refuser.** Les huit manifestes sont en `status: candidate` et
  aucun ne porte `incumbent`. Un titulaire se désigne quand un humain a validé.
- **Golden sets.** Aucune des six capacités n'en a. C'est là que se posera la
  question des photographies — et donc du consentement, que la charge type évite
  en calculant ses visages.
- **Cinq tests du front échouent, indépendamment de ce travail.**
  `src/schema/etat.test.ts`, `src/ecrans/choix.test.ts` et `src/App.test.tsx`
  supposent qu'`image-to-mesh` n'a aucun variant exécutable (« ses 7,37 Go de
  poids ne sont pas téléchargés »). Depuis que `hunyuan3d-2.1-shape-mlx@mlx-bf16`
  est mesuré, ce n'est plus vrai sur cette machine. C'est le piège que le README
  décrit : ces tests sont écrits contre un état du parc qui a changé. À trancher
  — choisir une autre capacité d'exemple, ou construire le cas plutôt que de le
  cueillir dans les fixtures.
