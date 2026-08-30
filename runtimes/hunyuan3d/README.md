# Runtime `hunyuan3d` — Hunyuan3D 2.1 Shape

Environnement isolé du variant `hunyuan3d-2.1-shape-mlx@mlx-bf16`
(`registry/models/hunyuan3d-2.1-shape-mlx.yaml`), capacité **`image-to-mesh`** :
une image en entrée, un maillage `model/gltf-binary` en sortie.

C'était le second maillon de ce qui devait être la capacité composite
`text-to-mesh` (`ARCHITECTURE.md` §10, route A). Ce contrat n'existe pas : le nom
est déclaré dans l'énumération des capacités (`packages/core/src/ecurie_core/models.py`,
`registry/schema/model.schema.json`) mais aucun `registry/capabilities/text-to-mesh.json`
ne le suit — un test fige d'ailleurs cet écart (`test_capabilities.py`, « text-to-mesh
est déclarée sans contrat ») —, et la tâche 7.1 qui devait l'écrire est coupée depuis
le 2026-08-29. Seul `image-to-mesh` existe comme contrat, et c'est celui que ce runtime
sert.

**Hors promesse v1.** La filière 3D est coupée de la v1 (décision 8 du pivot) : le
statut de titulaire de Hunyuan3D est retiré et son manifeste est passé de
`status: active` à `status: candidate`. Ce runtime reste découvrable et exécutable,
sans engagement d'entretien ni garantie que son profil reste à jour.

C'est l'un des trois chemins `runtime: custom` du parc — avec `minimax-h3`, et
avec `trellis2`, qui n'a ni environnement construit ni profil mesuré : le
superviseur lance `run.py` avec le python de `.venv`, et lui parle le protocole
JSON Lines de `CONCEPTION.md` §5.1.

> **Le nom du manifeste dit « MLX ». L'exécution, elle, est en PyTorch sur MPS.**
> Il n'existe pas de portage MLX officiel de la partie *shape* de la 2.1. Le seul
> portage communautaire sérieux a explicitement retiré la 2.1 de ses bancs d'essai
> et ne publie de poids MLX que pour les 2.0 / 2-mini. Le manifeste devra être
> renommé, ou le variant remplacé, quand la question sera tranchée.

---

## Construire l'environnement

Deux étapes, dont la seconde n'est **pas** faite par `ecurie env sync`.

### 1. Les dépendances Python

```sh
ecurie env sync hunyuan3d
```

### 2. Vendorer `hy3dshape`

Le code d'inférence de Tencent n'est publié que dans un dépôt Git, sans `setup.py`
ni `pyproject.toml` : **il n'existe pas sur PyPI** et aucun gestionnaire de paquets
ne peut l'installer. Il faut le copier à la main. Un sous-arbre suffit :

```sh
git clone --depth 1 --filter=blob:none --sparse \
    https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1 \
    runtimes/hunyuan3d/vendor/Hunyuan3D-2.1
git -C runtimes/hunyuan3d/vendor/Hunyuan3D-2.1 sparse-checkout set hy3dshape/hy3dshape
```

Après quoi ce fichier doit exister :

```
runtimes/hunyuan3d/vendor/Hunyuan3D-2.1/hy3dshape/hy3dshape/pipelines.py
```

`run.py` insère `vendor/Hunyuan3D-2.1/hy3dshape` dans `sys.path` au chargement, et
refuse de démarrer avec ces commandes en clair si le fichier manque. Un dossier
`vendor/` rangé autrement se déclare par `ECURIE_HY3DSHAPE_PATH` (le dossier qui
*contient* le paquet `hy3dshape`).

`vendor/` n'est pas versionné : le dépôt amont pèse trop et évolue pour son
compte. Le `.gitignore` du dépôt l'écarte par `runtimes/*/vendor/` (l. 15).

### 3. Les poids

`ecurie pull hunyuan3d-2.1-shape-mlx@mlx-bf16`. La géométrie tient en **deux
fichiers**, `hunyuan3d-dit-v2-1/config.yaml` (2 078 o) et
`hunyuan3d-dit-v2-1/model.fp16.ckpt` (7 366 389 768 o), soit 7 366 391 846 octets
au total. Ce seul checkpoint contient les trois sous-modèles : le DiT MoE 3,3 B, la
ShapeVAE et le conditionneur DINOv2-large — rien d'autre n'est à télécharger.

Le reste du dépôt Hugging Face (environ 7,5 Go de plus) est de la **texturation
PBR** : inutile ici, et inexécutable sur Mac.

Le worker ne télécharge jamais : il reçoit le chemin local déjà vérifié par le
superviseur et recompose l'environnement du chargeur amont pour pointer dessus.

---

## Ce qui est vérifié, et ce qui ne l'est pas

Cette distinction n'est pas de la prudence rhétorique : elle décidait si un profil
mémoire pouvait être écrit au manifeste. Le banc du 24 août 2026 l'a écrit.

### Éprouvé — au banc, une fois

`ecurie bench` a exécuté les trois cas de `registry/evals/bench/image-to-mesh.json`
sur `mps` le 2026-08-24 (Mac17,4 24 Gio / macOS 26.5.2). Les trois sont sortis `ok`,
avec des maillages de 768 104, 608 292 et 1 173 776 faces. Pic de mémoire unifiée
17 693 065 216 o pour un budget de 19 069 665 280 o, chargement 44,7 s, médiane
372,8 s par sortie. Le relevé est
`registry/measurements/hunyuan3d-2.1-shape-mlx@mlx-bf16/mac17-4-24-gio.json`, et
c'est de lui que vient le bloc `profile:` du manifeste.

Une machine, une révision de poids, trois cas d'un banc qui vérifie la forme du
fichier de sortie et non son contenu : c'est tout ce que « éprouvé » veut dire ici.

### Vérifié — par lecture du source amont

- Le chemin géométrie ne réclame **aucun noyau CUDA compilé**. Tous les chemins
  CUDA du code sont optionnels : `mc_algo='dmc'` (paquet `diso`), `USE_SAGEATTN=1`
  (`sageattention`), `torch.cuda.Event` sous `HY3DGEN_DEBUG=1`.
- L'extracteur de surface par défaut est `skimage.measure.marching_cubes`, CPU pur.
- `from_pretrained(...)` accepte `device=` et vaut `'cuda'` par défaut : `run.py`
  passe `'mps'` explicitement.
- Il n'y a **pas** d'argument `seed` : la graine passe par un `torch.Generator` CPU.
- La sortie est une liste de `trimesh.Trimesh`, exportée en GLB par `.export()`.
- Tencent anticipe `mps` dans son propre `gradio_app.py` (`--device`), et le README
  amont annonce macOS — mais ne fournit que des instructions d'installation CUDA.
- Un script tiers instancie l'architecture 2.1, la ShapeVAE et le conditionneur sur
  `torch.device('mps')` en fp16 et exécute un forward et un décodage.

### Non vérifié — et assumé comme tel

- **La tenue du chemin ailleurs qu'ici.** Personne, à notre connaissance, ne publie
  de trace d'un `Hunyuan3DDiTFlowMatchingPipeline` 2.1 exécuté de bout en bout sur
  Apple Silicon, et le banc du 24 août est le seul dont ce dépôt dispose : une
  machine, une classe de machine. Rien ne dit ce que fait ce chemin sur un Mac de
  16 Gio, où le pic mesuré de 16,48 Gio ne tient pas.
- **La portabilité du profil.** Les seules mesures Mac publiées portent sur les
  modèles 2.0 / 2-mini portés en MLX, pas sur la 2.1 en PyTorch ; Tencent annonce
  10 Go de VRAM côté CUDA, chiffre sans rapport avec les 16,48 Gio de mémoire
  unifiée relevés ici. `ecurie bench` reste la seule source légitime d'un profil, et il n'a
  tourné que sur une machine.
- **Les versions récentes des dépendances.** Tencent épingle `transformers==4.46`,
  `diffusers==0.30`, `numpy==1.24.4`, `trimesh==4.4.7`. Les bornes du
  `pyproject.toml` sont plus larges, et la compatibilité des versions hautes n'a
  pas été vérifiée. C'est le risque d'intégration numéro deux, après MPS.
- **La reproductibilité de la graine sur MPS.** Le bruit initial est déterministe
  (généré sur CPU), mais les noyaux Metal ne garantissent pas le déterminisme.
- **La validité du GLB produit** pour un maillage non texturé issu de ce pipeline.
- Les roues macOS arm64 de `pymeshlab` pour la version de Python retenue.

`run.py` remonte trois de ces limites dans `metrics.caveats` à chaque job — l'absence
de trace publique d'un run 2.1 complet sur MPS, la géométrie sans PBR, et le fait que
`peak_memory_bytes` est une borne inférieure du pic réel —, pour qu'elles apparaissent
au manifeste du run et pas seulement ici. Le relevé du 24 août les porte toutes trois.

---

## Pièges désamorcés par `run.py`

| Piège amont | Ce que fait `run.py` |
|---|---|
| `HY3DGEN_DEBUG=1` fait passer le chronomètre par `torch.cuda.Event` | la variable est **retirée** de l'environnement au chargement |
| `USE_SAGEATTN` charge un noyau CUDA | idem |
| `hunyuandit.py` enveloppe chaque SDPA dans `torch.backends.cuda.sdp_kernel(...)`, sans condition de plateforme, sur une API dépréciée annoncée pour suppression | remplacée par un contexte neutre quand il n'y a pas de CUDA (`ECURIE_HY3D_GARDER_SDP_KERNEL=1` restitue le comportement amont) |
| `smart_load_model` cherche sous `~/.cache/hy3dgen` puis télécharge | `HY3DGEN_MODELS` et les arguments sont recomposés pour désigner exactement le chemin vérifié par le superviseur |
| `import hy3dshape` exige `pymeshlab` (via `postprocessors`) | `pymeshlab` est une dépendance déclarée, et son absence donne un message qui le dit |
| `enable_flashvdm()` ne connaît pas la 2.1 dans sa table de VAE | jamais appelé |
| `torch.compile` sur MPS, jamais essayé | jamais appelé |
| barre `tqdm` dans un journal sans terminal | `enable_pbar=False` ; la progression passe par le protocole |

Le pipeline n'expose **aucun callback** de boucle de diffusion. La progression est
donc jalonnée (lecture, diffusion, export) et complétée par un battement toutes les
20 s dont le pourcentage ne bouge pas : il sert à empêcher le superviseur de tuer
un job long pour cause de silence, pas à simuler un avancement qu'on ignore.

## Ce que ce runtime ne fait pas

- **Pas de texturation PBR** : `custom_rasterizer` (extension C++/CUDA),
  `cupy-cuda12x`, `bpy`, `realesrgan` — rien de tout cela n'a de sens ici. La
  géométrie sort nue ; prévoir une passe de texturation séparée.
- **Pas de détourage automatique.** `rembg` télécharge son modèle `u2net`
  (~176 Mo) au premier appel, et un worker ne télécharge rien. Une image dont le
  fond n'est pas détouré donne un maillage qui inclut le décor : `run.py` le
  détecte (mode sans alpha, ou alpha entièrement opaque) et le signale en `caveat`
  plutôt que d'installer une dépendance réseau en douce. Détourer en amont du job.
- **Pas de post-traitement de maillage** (`FaceReducer`, `FloaterRemover`…), même
  si `pymeshlab` est installé : ce serait un choix de qualité à exposer au
  manifeste, pas une décision d'adaptateur.

## Réglages

Ceux du contrat `image-to-mesh` (`registry/capabilities/image-to-mesh.json`) :
`image`, `octree_resolution` ∈ {128, 256, 384, 512}, `num_inference_steps` ∈
[10, 100], `seed`. Les défauts du manifeste (256 / 30) sont plus modestes que ceux
du pipeline amont (384 / 50), qui visent une carte de référence.

Deux réglages amont supplémentaires sont acceptés depuis les `defaults` du variant
ou les `params` du job : `guidance_scale` (défaut 5,0) et `num_chunks` (défaut
8 000, taille des lots de requête de la grille SDF — le levier mémoire du décodage).

Variables d'environnement de mise au point, toutes optionnelles :

| Variable | Effet |
|---|---|
| `ECURIE_HY3DSHAPE_PATH` | dossier contenant le paquet `hy3dshape`, si `vendor/` est rangé autrement |
| `ECURIE_HY3D_DEVICE` | force le device (`mps`, `cpu`, `cuda`) au lieu de la détection |
| `ECURIE_HY3D_GARDER_SDP_KERNEL` | conserve `torch.backends.cuda.sdp_kernel` tel quel |

## Licence — `tencent-hunyuan-community`

Le manifeste porte `license_class: restricted`, et ce n'est pas décoratif. Points
saillants de la *Tencent Hunyuan 3D 2.1 Community License Agreement* (13 juin 2025) :

- **Restriction territoriale** : la licence ne s'applique **pas** dans l'Union
  européenne, au Royaume-Uni ni en Corée du Sud. L'accord interdit l'usage, la
  reproduction, la modification, la distribution et l'affichage du modèle **et de
  ses sorties** hors du territoire couvert. La fiche Hugging Face porte
  `extra_gated_eu_disallowed: true`.
- **Seuil d'usage** : au-delà de 1 million d'utilisateurs actifs mensuels à la date
  de publication, une licence explicite doit être demandée à Tencent.
- **Interdiction** d'utiliser le modèle ou ses sorties pour améliorer un autre
  modèle d'IA.
- **Redistribution** : joindre le fichier `Notice` et la mention de copyright ;
  signaler visiblement tout fichier modifié.
- Tencent ne revendique aucun droit sur les sorties générées. Droit applicable :
  Hong Kong.

À signaler : les en-têtes des fichiers source Python du dépôt annoncent une
« TENCENT HUNYUAN NON-COMMERCIAL LICENSE AGREEMENT », là où le fichier `LICENSE` et
la fiche Hugging Face annoncent la *Community* — laquelle n'interdit pas l'usage
commercial sous le seuil. La contradiction est réelle dans le dépôt amont ; en cas
d'usage professionnel, c'est un point à faire trancher avant, pas après.
