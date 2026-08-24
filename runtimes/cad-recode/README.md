# Runtime `cad-recode` — CAD-Recode v1.5

Environnement isolé du variant `cad-recode@bf16` (`registry/models/cad-recode.yaml`),
capacité **`pointcloud-to-cad`** : un nuage de points en entrée, un programme
**CadQuery** en sortie — et, si on le demande, le STEP et le maillage que ce
programme construit.

C'est la seule capacité du parc dont la sortie est du **code**. Le parc rend des
maillages ; celle-ci rend la recette qui les fabrique, donc une pièce qu'on peut
encore modifier.

> **USAGE DE RECHERCHE NON COMMERCIALE UNIQUEMENT.** Les poids sont sous
> CC BY-NC 4.0, et le `LICENSE.md` du dépôt amont est le même Attribution-
> NonCommercial. La restriction porte donc aussi sur le **code d'inférence** —
> c'est la raison de toute l'étape 2 ci-dessous.

---

## Construire l'environnement

Trois étapes, dont la deuxième n'est **pas** faite par `ecurie env sync`.

### 1. Les dépendances Python

```sh
ecurie env sync cad-recode
```

Comptez une bonne minute et **1,9 Gio** : `cadquery` tire `cadquery-ocp`,
`cadquery-ocp-proxy`, `vtk`, `trame`, `numba`, `llvmlite`, `casadi` et `nlopt`,
soit une pile de visualisation entière pour ce qui se réduit ici à « exécuter du
code et écrire un STEP ». Il n'y a pas d'échappatoire : `build123d`, qui
éviterait `vtk`, n'exécute pas le dialecte CadQuery que ces poids ont appris à
écrire. La bibliothèque a été choisie à l'entraînement, pas par nous.

### 2. Vendorer le code d'inférence

**Aucune ligne du code amont ne peut être committée dans Écurie** : elle est sous
CC BY-NC 4.0. Et contrairement à `hy3dshape` chez `runtimes/hunyuan3d/`, ce code
n'existe pas sous forme de module — il vit dans **une cellule de `demo.ipynb`**,
mêlé à des imports d'`open3d`, `skimage`, `matplotlib`, `scipy` et `pytorch3d`
dont aucun n'est installé ici (et dont `pytorch3d` ne publie aucune roue arm64,
ni pour Python 3.12). Un `sparse-checkout` ne suffisait donc pas : il a fallu
découper, et c'est ce que fait `vendorer.py`.

```sh
git clone --depth 1 https://github.com/filaPro/cad-recode \
    runtimes/cad-recode/vendor/cad-recode
python3 runtimes/cad-recode/vendorer.py
```

Après quoi ce fichier doit exister :

```
runtimes/cad-recode/vendor/cad_recode_model.py
```

`vendorer.py` affiche le nombre de lignes et le **sha256** de ce qu'il a écrit,
et prévient quand l'empreinte diffère de celle sur laquelle le profil du
manifeste a été mesuré (111 lignes,
`a810f52b1dde027175240b196b0d4aab67994bb47e6bf8387dca9d45566c6c01`, commit amont
`03e3262119b38939feaa44b8368ad8db99243d47`). Ce n'est pas un contrôle
d'intégrité : `git clone --depth 1` ramène toujours la pointe de la branche, et
savoir que le code a bougé est le seul moyen de savoir que le profil ne décrit
plus ce qu'on exécute.

L'adaptateur insère `vendor/` dans `sys.path` au chargement et **refuse de
démarrer** avec ces deux commandes en clair si le fichier manque. Un dossier
rangé autrement se déclare par `ECURIE_CAD_RECODE_VENDOR` (le dossier qui
*contient* `cad_recode_model.py`).

`vendor/` n'est pas versionné — `.gitignore` l'exclut par `runtimes/*/vendor/`,
la même ligne que pour `hunyuan3d`.

### 3. Les poids

```sh
ecurie pull cad-recode@bf16
```

**Deux dépôts, et c'est voulu.** Le dépôt des poids ne publie que cinq fichiers
et **aucun tokenizer** : il faut celui de `Qwen/Qwen2-1.5B`, sur lequel ces poids
ont été entraînés. Le manifeste le déclare en `extra_sources` avec
`role: tokenizer` — premier emploi du champ au parc — et `ecurie pull` ramène les
deux en un seul appel. L'adaptateur le retrouve sous
`variant["extra_paths"]["tokenizer"]` ; il ne télécharge rien, jamais.

| Dépôt | Rôle | Licence | Poids |
|---|---|---|---|
| `filapro/cad-recode-v1.5` | les poids | cc-by-nc-4.0 | 3 087 787 687 o |
| `Qwen/Qwen2-1.5B` | le tokenizer | apache-2.0 | 11 477 976 o |

---

## Le premier `import cadquery` prend 85 secondes

Mesuré sur cette machine : **85,6 s** au premier import après `env sync`, puis
1,1 s. Ce n'est pas de la compilation de bytecode — 2,97 s de CPU utilisateur
pour 85,6 s d'horloge, soit 3 % d'occupation, et un `python -m compileall` sur
`OCP` et `vtkmodules` prend 0,16 s. C'est la première validation par macOS de
~300 Mio de code natif non signé (`OCP.cpython-312-darwin.so` 141 Mio,
`libvtkCommonCore.dylib` 144 Mio).

Le cache est **système** : une fois payé par n'importe quel processus, il l'est
pour les suivants. D'où le choix de l'adaptateur — au chargement, il lance
`python -I -c "import cadquery"` dans un **processus jetable**. Deux raisons, et
la seconde est la moins évidente :

- payer l'attente au premier job l'attribuerait à la latence du modèle, et le
  banc l'inscrirait au profil d'un cas qui ne la mérite pas ;
- l'importer dans le worker lui-même ajouterait ~300 Mio au RSS d'un processus
  qui n'a **jamais** besoin de cadquery — c'est le sous-processus d'exécution qui
  s'en sert — et fausserait le pic mémoire du profil.

Mesuré sur un parc chaud, ce préchauffage coûte 1,3 à 2,0 s de warmup. À prévoir
donc : une attente unique après `ecurie env sync`, pas un défaut à corriger.

---

## Ce qui est vérifié, et ce qui ne l'est pas

### Vérifié — par exécution sur cette machine, avec les vrais poids

- Le chemin complet sur **MPS** : chargement, génération, exécution du programme,
  export STEP et GLB.
- Les trois pièces de la charge type reviennent en CadQuery dont la **forme**
  est juste, et les trois solides produits sont étanches. Les **cotes**, elles,
  ne le sont pas : le cadrage efface l'échelle. Sur la pièce en L, les six
  sommets du profil reviennent exacts à 0,0033 près une fois recadrés, pendant
  que le volume vaut 1 671 600 mm³ contre 360 000 au solide d'origine.
- `torch.mps.driver_allocated_memory()` et le RSS sont relevés tous les deux, et
  c'est le plus grand qui part au profil : sur mémoire unifiée, la mémoire Metal
  n'apparaît pas dans le RSS et le RSS n'apparaît pas dans le pilote.
- Aucun `not implemented for MPS` : l'affectation masquée
  `inputs_embeds[attention_mask == -1]` en bfloat16 passe.

### Non vérifié — et assumé comme tel

- **Le dialecte CadQuery au-delà des constructions rencontrées.** Le modèle a été
  entraîné contre `cadquery` au commit `e99a15df` avec `cadquery-ocp` 7.7.2 ; on
  sert 2.8.0 avec `ocp` 7.9.3.1.1. Les constructions vues ici passent ; l'amont
  annonce lui-même 0,37 % de code invalide sur DeepCAD, hors dérive de version.
- **Les entrées bruitées ou partielles.** La charge type est faite de nuages
  calculés depuis des solides exacts. Un scan réel a du bruit, des trous et une
  densité inégale, et rien ici ne dit ce que le modèle en fait.
- **`n_points` hors de 256.** Le contrat descend à 64 et monte à 512 pour mesurer
  le coût, pas pour améliorer la sortie : 256 est la valeur d'entraînement.

---

## Pièges désamorcés par l'adaptateur

| Piège | Ce que fait `workers/cad_recode.py` |
|---|---|
| `transformers>=5` charge puis produit du charabia, **sans exception** | la borne `<5` est dans le `pyproject.toml`, et l'adaptateur refuse de charger au-delà en nommant le motif |
| `architectures: [MyQwen2ForCausalLM]` désigne une classe qui n'existe nulle part, et le dépôt n'a aucun `.py` | `trust_remote_code` n'est jamais employé ; la classe vendorée est instanciée directement |
| `trimesh.load` d'un GLB rend une **Scene**, pas un `Trimesh` | trois branches explicites — `Trimesh`, `Scene` (concaténée), `PointCloud` (pris tel quel) |
| `np.random.seed()` est **inerte** avec trimesh 5.0.0 | `sample_surface(..., seed=…)` en argument nommé, et le tirage est vérifié reproductible |
| `pytorch3d.ops.sample_farthest_points` n'a aucune roue arm64 | échantillonnage du plus lointain réécrit en numpy, départ à l'indice 0 comme l'amont |
| le premier `import cadquery` coûte 85 s | préchauffé au chargement dans un processus jetable — ni dans la latence du premier job, ni dans le RSS du worker |
| exécuter du Python engendré par un modèle | interpréteur séparé `-I`, environnement vidé, dossier de travail limité au job, délai d'horloge 20 s, `RLIMIT_CPU` 15 s, `RLIMIT_FSIZE` 512 Mio — et **faux par défaut**. Ni réseau ni mémoire bornés : mesuré, macOS refuse `RLIMIT_AS`, `RLIMIT_DATA` et `RLIMIT_RSS`, ce que le job remonte en avertissement |

## Variables d'environnement

| Variable | Effet |
|---|---|
| `ECURIE_CAD_RECODE_VENDOR` | dossier contenant `cad_recode_model.py`, si `vendor/` est rangé autrement |
| `ECURIE_CAD_RECODE_DEVICE` | force le périphérique (`mps`, `cpu`) au lieu de la détection |
