# Écurie — Architecture v0.1

> Registre déclaratif, comptabilité disque et UI générique pour un parc de modèles
> open-weight exécutés localement sur Apple Silicon.
>
> Cible matérielle de référence : MacBook Air M5, 24 Go de mémoire unifiée.
> Date : 19 août 2026.

---

## 0. Critique préalable — ce que ce projet ne doit surtout pas être

Avant de spécifier, il faut éliminer trois pièges qui tueraient le projet.

**Piège 1 : réécrire un runtime.** MLX, `mlx-audio`, `diffusers`, `llama.cpp`, ComfyUI,
`whisper.cpp` existent et sont maintenus par des équipes entières. Écurie n'exécute
jamais un tenseur lui-même. Il orchestre des runtimes existants derrière des adaptateurs.

**Piège 2 : un « Ollama pour tout ».** Ton parc couvre TTS, ASR, musique, image, vidéo,
OCR, 3D, séparation audio, débruitage. Ce sont au bas mot cinq écosystèmes Python
mutuellement incompatibles en versions de `torch`, `numpy` et `transformers`. Prétendre
les faire cohabiter dans un seul environnement est une garantie d'échec au troisième
modèle ajouté. La réponse est l'**isolation par environnement**, pas l'unification.

**Piège 3 : croire que la veille est un problème de découverte.** Trouver de nouveaux
modèles est trivial — l'API Hugging Face te sort 200 candidats par semaine. Le problème
dur est **l'évaluation comparative**, et sans harnais d'évaluation la veille ne produit
que du bruit sous forme de liste. C'est ce qui décide si le module veille a de la valeur
ou non (§7).

**Le vrai trou, celui qui justifie le projet :**

| Douleur | Situation actuelle |
|---|---|
| Comptabilité disque | Cache HF + blobs Ollama + LM Studio + `ComfyUI/models` + caches MLX. Duplication silencieuse de dizaines de Go. Aucun outil ne les voit ensemble. |
| Budget mémoire | Rien ne t'empêche de charger un modèle 14 Go alors qu'un autre en occupe déjà 9. Tu découvres le problème par un swap de 30 s ou un OOM. |
| Contrat d'entrée/sortie | Chaque modèle a son script, ses arguments, sa convention. Aucune surface commune. |
| Reproductibilité | Une sortie générée il y a trois mois : quel modèle, quelle révision, quelle quantification, quels paramètres ? Perdu. |

Écurie attaque ces quatre points. Le reste est délégué.

---

## 1. Nom

**Écurie** — on y garde un parc de modèles, on les entretient, on les sort, on en réforme.
Le mot porte aussi le sens d'équipe d'écurie de course, ce qui colle au volet
benchmark/comparaison.

Alternatives écartées : *Établi* (trop proche de Forge), *Remise*, *Cale sèche*.

---

## 2. Modèle de données — quatre concepts, pas un

La faute d'architecture la plus commune est de confondre « le modèle » et « le fichier
sur le disque ». Il faut quatre niveaux distincts, sinon la comptabilité disque et la
déduplication sont impossibles.

```
Model          Qwen3-TTS 1.7B          identité logique, licence, capacité
  └─ Variant   …-1.7B-8bit-mlx         quantification, format, révision épinglée
       └─ Artifact  model.safetensors  fichier physique, sha256, taille réelle
            └─ Location  ~/.cache/hf/…  emplacement observé (peut être multiple)
```

- **Model** : l'entité conceptuelle. Une capacité, une famille, une licence, une notice.
- **Variant** : ce que tu télécharges réellement. `Q4_K_M`, `8bit-mlx`, `bf16`.
  C'est le niveau qui porte le **profil de ressources mesuré**.
- **Artifact** : un fichier, identifié par son hash de contenu. C'est **ici** que la
  déduplication opère : le même GGUF présent dans Ollama et dans LM Studio est
  un seul Artifact avec deux Locations.
- **Location** : un chemin observé sur un volume, avec son type de lien (fichier plein,
  lien dur, clone APFS, lien symbolique).

Corollaire : `taille_du_parc ≠ Σ tailles_des_fichiers`. Écurie rapporte **trois** chiffres
distincts — occupation apparente, occupation réelle unique, espace récupérable.

---

## 3. Le registre est la source de vérité, et il vit dans Git

```
registry/
  schema/model.schema.json      # JSON Schema, validé en CI
  capabilities/                 # contrats d'E/S par capacité
    text-to-speech.json
    image-to-mesh.json
  models/
    qwen3-tts-1.7b.yaml
    hunyuan3d-2.1-shape-mlx.yaml
    …
  measurements/<ref>/<machine>.json  # profils mesurés, un fichier par machine
    qwen3-tts-1.7b@8bit-mlx.json
  evals/                        # golden sets + résultats + préférences humaines
```

**Data-in-git**, pas une base de données. Raisons :

1. Les agents de veille produisent des **pull requests**, jamais des mutations d'état
   vivant. Toute évolution du parc est révisée par un humain avant d'être appliquée.
   C'est le même patron de validation humaine que tu as retenu pour Calque.
2. La CI valide le schéma, vérifie que les révisions HF épinglées existent encore,
   détecte les licences changées.
3. `git log` sur `models/flux2-klein.yaml` te raconte l'histoire de ton parc.

Un manifeste minimal :

```yaml
id: qwen3-tts-1.7b
capability: text-to-speech
family: qwen3
license: apache-2.0
status: active            # active | candidate | deprecated | retired

variants:
  - id: 8bit-mlx
    source:
      kind: huggingface
      repo: mlx-community/Qwen3-TTS-1.7B-8bit
      revision: 4f2c9a1…        # épinglé, jamais "main"
    runtime: mlx-audio
    profile:                     # MESURÉ, jamais estimé
      disk_bytes: 1_950_000_000
      peak_unified_memory_bytes: 3_100_000_000
      warmup_ms: 2400
      rtf: 0.11                  # real-time factor
      measured_on: "M5-24GB / macOS 26.2 / mlx 0.31"
      measured_at: 2026-08-14
```

Règle non négociable : **`profile` est rempli par le banc d'essai, pas à la main.**
Un profil estimé est un profil faux, et le contrôle d'admission mémoire (§6) en dépend.

---

## 4. Le contrat de capacité — le pivot de toute l'architecture

C'est la décision structurante. Une **capacité** déclare un schéma d'entrée et un schéma
de sortie typés. Tous les modèles d'une même capacité sont interchangeables derrière ce
contrat.

```json
{
  "id": "text-to-speech",
  "input": {
    "type": "object",
    "required": ["text"],
    "properties": {
      "text":   { "type": "string", "x-ui": "textarea" },
      "voice":  { "type": "string", "x-ui": "select", "x-options-from": "runtime.voices" },
      "speed":  { "type": "number", "minimum": 0.5, "maximum": 2.0, "default": 1.0 }
    }
  },
  "output": {
    "type": "object",
    "properties": { "audio": { "type": "string", "contentMediaType": "audio/wav" } }
  }
}
```

Trois conséquences majeures, et c'est ce qui fait tenir le projet :

1. **L'UI générique n'existe pas — elle est générée.** Le front rend un formulaire à
   partir du JSON Schema d'entrée et un visualiseur à partir du type de sortie. Ajouter
   un modèle = ajouter un YAML. Aucune ligne de front à écrire. C'est la réponse directe
   à ton exigence « un UI générique pour interagir avec ces modèles ».
2. **La comparaison A/B est native.** Deux variants, même capacité, même entrée →
   exécution parallèle, sorties côte à côte, préférence enregistrée. C'est le carburant
   de la veille (§7).
3. **Le chaînage devient typable.** `text-to-image` produit `image/*`, `image-to-mesh`
   consomme `image/*` : la composition est vérifiable statiquement. C'est ce qui rend
   le cas texte→3D propre (§10).

Capacités du parc initial : `text-to-speech`, `speech-to-text`, `text-to-music`,
`text-to-image`, `image-to-video`, `text-to-video`, `document-to-text` (OCR),
`image-to-mesh`, `audio-separation`, `audio-denoise`, `text-to-mesh` (composite).

> **Cinq de plus au v0.4**, et c'est la promesse ci-dessus mise à l'épreuve :
> `image-matting` (détourage — le chaînon entre une photo et une reconstruction
> 3D, qui recadre sur l'alpha), `image-upscale`, `image-to-text` (décrire une
> image, à distinguer de transcrire un document), `translation` et `tool-use`.
>
> Deux enseignements en sont sortis. Le premier : **`image-to-text` n'a rien
> coûté** — mêmes poids que la lecture de document, même environnement, un
> contrat et un manifeste. C'est exactement ce que le contrat de capacité
> promettait, et la première fois que le parc le vérifie. Le second : le partage
> de poids entre deux manifestes a révélé que le résolveur du store les indexait
> par dépôt dans un simple dictionnaire, si bien que le second manifeste écrasait
> le premier — plusieurs gigaoctets attribués au mauvais modèle, et le poste
> « jamais utilisé » du plan de récupération qui proposait à la corbeille des
> poids servis tous les jours.
>
> Ce qui **n'a pas** mérité de contrat : `code-generation`. Le contrat aurait été
> celui de `text-generation`, les modèles les mêmes, et la séparation aurait
> divisé le vivier de la confrontation en deux moitiés qui ne se compareraient
> plus. `tool-use`, à l'inverse, porte des schémas en entrée et une sortie
> structurée : c'est un autre contrat, donc une autre capacité.

---

## 5. Couches et arborescence

```
ecurie/
  registry/                 # données (§3)
  packages/
    core/                   # schémas pydantic, résolveur, types de capacité
    store/                  # scan disque, dédup, GC, tiering
    runtime/                # adaptateurs par runtime + gestion des envs isolés
      adapters/mlx_audio.py  mlx_lm.py  diffusers_mps.py  comfy.py  ollama.py
    api/                    # FastAPI : jobs, SSE, ressources
    veille/                 # agents + connecteurs de sources
  apps/
    ui/                     # front, rendu piloté par schéma
  .claude/skills/           # skills Claude Code (veille, banc d'essai, élagage)
  runtimes/                 # environnements isolés, un par famille
    mlx-audio/  .venv
    diffusers/  .venv
```

**Isolation des runtimes** : un `uv`-venv par famille de runtime, jamais un env global.
L'adaptateur lance un sous-processus dans le bon venv et communique en JSON sur stdio.
Coût : ~200 ms de démarrage par job. Bénéfice : tu peux avoir `torch 2.9` pour l'un et
`torch 2.6` pour l'autre sans que ton parc explose. Ce n'est pas négociable avec dix
familles de modèles.

**Pile technique recommandée** — et ici je m'écarte de tes habitudes :

| Couche | Choix | Justification |
|---|---|---|
| Core / store / runtime | Python 3.12 + `uv` | MLX, `huggingface_hub`, `diffusers` sont Python. Une passerelle Node ne rachèterait rien. |
| API | FastAPI + SSE | Progression de job en flux, pas de WebSocket à gérer. |
| Front | React + Vite + RJSF | `react-jsonschema-form` est l'écosystème le plus mûr pour du rendu piloté par schéma. En Angular il faudrait passer par Formly, viable mais plus de plomberie pour un projet perso. |

Si tu tiens à Angular pour rester dans ton axe professionnel, Formly fait le travail —
compte une semaine de plus sur le rendu de schéma et les types de sortie médias.

---

## 6. Gestion du disque — le module qui a le plus de valeur immédiate

### 6.1 Sources scannées

| Gestionnaire | Emplacement | Particularité |
|---|---|---|
| Hugging Face | `~/.cache/huggingface/hub` | `blobs/` + `snapshots/` avec liens symboliques. `scan_cache_dir()` de `huggingface_hub` donne l'inventaire exact, révision par révision. |
| Ollama | `~/.ollama/models/{blobs,manifests}` | Blobs adressés par `sha256-…`. Dédup interne, aucune avec l'extérieur. |
| LM Studio | `~/.lmstudio/models` | Arborescence `éditeur/dépôt/fichier.gguf`. Fichiers pleins. |
| ComfyUI | `<install>/models/{checkpoints,vae,clip,unet,…}` | Le pire cas : copies manuelles, aucun suivi de provenance. |
| MLX / manuel | variable | Déclaré dans la config Écurie. |

### 6.2 Trois chiffres, pas un

- **Apparent** : la somme naïve des tailles de fichiers. C'est ce que `du` te donne, et
  c'est **faux** dès qu'il y a liens durs ou clones APFS.
- **Réel unique** : somme des tailles par `sha256` distinct. La vérité sur l'occupation.
- **Récupérable** : décomposé en quatre postes, car chacun appelle une action différente.

```
Récupérable = duplication_inter_gestionnaires
            + révisions_HF_obsolètes
            + blobs_orphelins           (plus référencés par aucun snapshot)
            + variants_jamais_utilisés  (télémétrie : run_count == 0 depuis N jours)
```

### 6.3 Déduplication sur APFS

Deux mécanismes, et il faut savoir lequel appliquer :

- **Lien dur** (`os.link`) : un seul inode, zéro octet. Effet de bord : modifier un
  chemin modifie l'autre. Sans danger pour des poids de modèles, qui sont immuables.
  À réserver aux artefacts vérifiés par hash.
- **Clone APFS** (`clonefile`, ou `cp -c`) : copie sur écriture, zéro octet à la création,
  mais **divergence à la première écriture**. Utile pour le tiering, pas pour la dédup.

Règle Écurie : dédup par lien dur **uniquement** entre fichiers de `sha256` identique et
sur le même volume. Vérification du hash avant, jamais sur la seule taille. Toute
opération destructive passe par un plan en `--dry-run` affichant le gain avant exécution.

### 6.4 Tiering vers SSD externe

Les modèles vidéo et image lourds (FLUX.2 dev 32B, gros Wan) n'ont pas leur place sur un
SSD interne de portable. Écurie gère une **ferme de liens symboliques** : l'artefact
migre vers `/Volumes/…`, un lien symbolique reste en place, le manifeste marque le variant
`tier: cold`. L'UI grise les modèles froids quand le volume est absent au lieu de planter
au chargement.

---

## 7. Budget mémoire unifiée — spécifique à ton M5 24 Go

Ton relevé Metal (`recommendedMaxWorkingSetSize` ≈ 17,8 GiB) est la valeur par défaut :
macOS réserve environ 75 % de la mémoire unifiée au GPU. Deux leviers :

- **Repousser le plafond** : `sudo sysctl iogpu.wired_limit_mb=20480` monte la limite
  câblée. Gagne quelques Go, au prix d'un risque de pression sur le système. À exposer
  dans Écurie comme un réglage explicite et réversible, jamais appliqué en silence.
- **Contrôle d'admission** : c'est le vrai mécanisme. Avant de lancer un job, Écurie
  compare `profile.peak_unified_memory_bytes` du variant au budget résiduel. Si ça ne
  passe pas, il **décharge** le résident le moins récemment utilisé au lieu de laisser
  macOS partir en swap.

Politique par défaut : **un seul modèle lourd résident à la fois**, plus N modèles
légers (ASR, débruitage, séparation) qui restent chauds. Les petits modèles gagnent
énormément à ne pas être rechargés — un `warmup_ms` de 2,4 s payé à chaque phrase de
TTS rend l'outil désagréable.

> **Seuil de lourdeur : 8 Gio, et non les 6 Go écrits ici avant toute mesure.** Les
> quatre profils relevés au v0.3 le tranchent — voix 7,65 Gio, lecture de document
> 6,25, image 15,95, musique 13,8 à 23,9 selon la durée. À 6 Go les quatre sont
> lourds, donc aucun ne cohabite jamais et la politique ne distingue plus rien ; à
> 8 Gio la voix et la lecture de document restent chaudes ensemble (13,9 Gio sur les
> 17,76 disponibles) et seules l'image et la musique se disputent la place. Recalibré
> le 20 août 2026, dans `Config.heavy_threshold_bytes` et
> `admission.DEFAULT_HEAVY_THRESHOLD`.

Mesure du profil, par le banc d'essai :
- MLX : `mx.get_peak_memory()` après reset, valeur exacte.
- PyTorch/MPS : `torch.mps.driver_allocated_memory()` + RSS du processus via `psutil`.
- Sous-processus opaque : échantillonnage du RSS à 100 ms, retenue du maximum.

---

## 8. Veille technologique — agents et skills

### 8.1 Principe directeur

Les agents **proposent**, ils ne décident jamais. Sortie d'un cycle de veille :
une branche + une PR contenant un rapport daté et des manifestes en `status: candidate`.
Tu arbitres. Rien n'est téléchargé sans ton accord.

### 8.2 Les quatre agents

| Agent | Rôle | Sortie |
|---|---|---|
| `veille-scan` | Balayer les sources depuis le dernier passage | `veille/YYYY-MM-DD/candidats.json` |
| `veille-qualifier` | Filtrer sur budget, licence, disponibilité Apple Silicon | candidats scorés + rejets motivés |
| `veille-eprouver` | Télécharger, mesurer, exécuter le golden set | `measurements/<ref>/<machine>.json` + sorties d'éval |
| `veille-elaguer` | Proposer des retraits selon télémétrie et redondance | plan de GC chiffré en Go |

Sources de `veille-scan` :
- API HF : `list_models(filter=…, sort="lastModified")` par tâche, avec un second
  passage sur `library:mlx` et l'organisation `mlx-community` — c'est le signal le plus
  prédictif de « ça tournera bien sur ton Mac ».
- Releases GitHub des dépôts de runtime (`ml-explore/mlx`, `Blaizzy/mlx-audio`,
  ComfyUI et ses nœuds) : un runtime qui gagne une famille de modèles vaut souvent plus
  qu'un nouveau modèle.
- Flux d'annonces des laboratoires suivis (Qwen, Tencent Hunyuan, Black Forest Labs,
  Lightricks, MiniMax, Baidu/Paddle).

Fonction de score — pondérations à ajuster après trois cycles :

```
score = 0.35 · gain_qualité_relatif      (issu du golden set, vs titulaire en poste)
      + 0.20 · maturité_runtime          (portage MLX existant ? nœud Comfy ? script maison ?)
      + 0.20 · adéquation_budget         (pic mémoire vs 17 Go, disque vs quota du domaine)
      + 0.15 · licence                   (permissive > restreinte > recherche seulement)
      + 0.10 · vélocité                  (téléchargements, activité du dépôt)
```

### 8.3 Le point qui décide de tout : le harnais d'évaluation

Sans lui, la veille est une liste de liens. Avec lui, c'est un système de décision.

Chaque capacité possède un **golden set figé** — 10 à 15 entrées, versionnées dans le
dépôt, qui ne changent jamais (sinon les comparaisons historiques sont détruites) :

| Capacité | Golden set | Métrique |
|---|---|---|
| `speech-to-text` | 12 extraits, dont 4 en français québécois avec bruit de fond | WER, RTF |
| `document-to-text` | 15 pages : PDF municipal, plan scanné, tableau, manuscrit | exactitude champs + structure |
| `text-to-speech` | 10 phrases FR/EN, chiffres, sigles, noms propres | préférence humaine A/B |
| `text-to-image` | 10 prompts couvrant texte, mains, matériaux, composition | préférence humaine A/B |
| `image-to-mesh` | 8 images d'objets | étanchéité, nb de faces, préférence |

Les métriques automatiques (WER, exactitude OCR) tournent en CI. Le reste passe par le
**mode A/B de l'UI** : deux sorties côte à côte, tu cliques, la préférence est écrite
dans `registry/evals/`. C'est un classement Elo local, alimenté par ton usage réel.
Trente comparaisons suffisent à séparer un modèle correct d'un mauvais.

C'est aussi ce qui rend le projet difficile à copier, et ce sur quoi je concentrerais
l'effort après le v0.2.

---

## 9. UI générique

Quatre écrans, pas davantage.

1. **Atelier** — sélection d'une capacité, puis d'un variant. Formulaire rendu depuis le
   JSON Schema. Bandeau de ressources permanent : mémoire résidente, budget restant,
   ce qui sera déchargé si tu lances. Progression en SSE.
2. **Confrontation** — même entrée, deux variants, sorties côte à côte, vote. Alimente
   le classement.
3. **Parc** — inventaire, trois chiffres d'occupation, arbre de duplication, plan de GC
   en `--dry-run` avec gain chiffré, épinglage et tiering.
4. **Bibliothèque** — toutes les sorties produites, indexées par `(modèle, révision,
   variant, paramètres, graine, hash d'entrée)`. Bouton *rejouer*. Un artefact non
   reproductible est un artefact perdu — même contrat de reproductibilité que dans Forge.

L'écran 4 est celui qu'on oublie systématiquement et qu'on regrette au bout de six mois.

---

## 10. Texte → 3D : la case manquante

État réel au 19 août 2026, sans complaisance : **il n'existe pas de modèle open-weight
texte→3D natif qui tourne convenablement sur Apple Silicon.** Trois routes, par ordre de
praticabilité sur ta machine.

### Route A — Pipeline en deux temps (recommandée)

```
prompt → Z-Image Turbo 6B ou FLUX.2 Klein 4B → image → Hunyuan3D 2.1 Shape MLX → maillage
```

C'est la route que je retiendrais. Trois raisons : les deux maillons sont déjà dans ton
parc, l'itération est bon marché (tu régénères l'image jusqu'à ce que la silhouette soit
juste avant de payer la reconstruction), et le contrôle est bien meilleur qu'avec un
texte→3D natif. C'est aussi la validation du chaînage typé de §4 : le contrat
`text-to-mesh` est une capacité composite, pas un modèle.

Limite connue et non contournable : la géométrie est bonne, le PBR complet ne l'est pas.
Prévois une passe de texturation séparée.

### Route B — TRELLIS.2 (natif, mais pas pour ton Mac)

<cite index="7-1">TRELLIS.2 de Microsoft Research, sous licence MIT, est actuellement le meilleur générateur 3D auto-hébergeable</cite>, et <cite index="1-1">il produit notamment de la sortie en gaussian splatting</cite>. Le problème est
l'exécution : la famille TRELLIS repose sur des noyaux CUDA pour ses opérations de voxels
épars, sans portage MLX connu. Sur ton M5, attends-toi à un repli CPU inexploitable, voire
à un échec d'installation. À garder en `status: candidate` dans le registre, avec une
alerte de veille sur l'apparition d'un portage Metal — ce serait le signal de bascule.

### Route C — LLM modeleur (celle que je surveillerais de près, vu Forge)

Un LLM de code local (classe Qwen3-Coder) qui écrit du `bpy` Blender ou une composition
paramétrique, plutôt que de cracher un maillage. Le champ bouge : les travaux type
LL3M — <cite index="9-1">*LL3M: Large Language 3D Modelers*</cite> — vont exactement dans cette direction.

Pourquoi ça compte pour toi précisément : la sortie est **paramétrique et éditable**, pas
une soupe de triangles. Un modèle génératif te donne un objet figé de 200 k faces ; un LLM
modeleur te donne un arbre de construction que tu peux rejouer avec d'autres paramètres.
C'est structurellement le même pari que Forge, et c'est la seule route texte→3D qui
produise un artefact réellement réutilisable.

Coût : la fidélité visuelle est très inférieure à Hunyuan3D sur les formes organiques.
Excellent sur le mécanique, le mobilier, l'architectural. Ce qui recouvre une bonne part
de tes cas d'usage.

### Ligne à ajouter à ton tableau

| Domaine | Sur ton Mac | Frontière plus lourde | Verdict 24 Go |
|---|---|---|---|
| Texte → 3D | Pipeline Z-Image Turbo → Hunyuan3D 2.1 Shape MLX | TRELLIS.2 (CUDA), Meshy 6 / Rodin Gen-2.5 (fermés) | 🟡 en pipeline, 🔴 en natif |

---

## 11. Périmètre exclu du v1

À écrire noir sur blanc, sinon le projet dérive :

- Pas de Linux, pas de CUDA. Apple Silicon uniquement. Le jour où ça change, la couche
  d'adaptateurs absorbe le choc.
- Pas d'authentification, pas de multi-utilisateur, pas de déploiement distant.
- Pas de moteur de graphes généraliste type ComfyUI. Une seule composition est câblée
  (texte→3D), les autres capacités restent atomiques.
- Pas de fine-tuning, pas d'entraînement.
- Pas de publication npm/PyPI avant le v0.4. C'est d'abord ton outil.

---

## 12. Jalons

| Version | Contenu | Valeur livrée | Effort |
|---|---|---|---|
| **v0.1** | Schéma de manifeste + scan disque **lecture seule**, les trois chiffres, rapport CLI | Tu vois enfin ce que ton parc occupe vraiment | 1 fin de semaine |
| **v0.2** | Dédup par lien dur, plan de GC en `--dry-run`, tiering externe, télémétrie d'usage | Tu récupères des dizaines de Go | 1 semaine |
| **v0.3** | Adaptateurs de runtime (`mlx-audio`, `diffusers/MPS`), envs isolés, contrôle d'admission mémoire | Exécution unifiée, plus d'OOM surprise | 2 semaines |
| **v0.4** | API + UI générique pilotée par schéma, écrans Atelier et Parc | L'outil devient utilisable au quotidien | 2 semaines |
| **v0.5** | Golden sets, mode Confrontation, classement Elo local, Bibliothèque | Le parc devient mesurable | 2 semaines |
| **v0.6** | Skills de veille, cron GitHub Actions hebdomadaire, PR automatiques | Le parc s'entretient | 1 semaine |
| **v0.7** | Capacité composite texte→3D | La case manquante | 1 semaine |

Le v0.1 est délibérément minuscule et sans exécution de modèle. Il livre de la valeur
immédiatement et il valide le modèle de données avant que tu n'investisses dans les
adaptateurs. Si le v0.1 ne te sert pas dans la semaine qui suit, le projet entier est à
remettre en question — c'est le test à passer.

---

## 13. Risques

| Risque | Probabilité | Mitigation |
|---|---|---|
| L'enfer des dépendances entre familles de runtime | Élevée | Envs isolés dès le v0.3, jamais de venv partagé |
| Les manifestes se périment plus vite qu'on les maintient | Élevée | CI hebdomadaire qui vérifie l'existence des révisions épinglées et les changements de licence |
| La veille produit du bruit | Moyenne | Aucun candidat n'est promu sans passage au golden set |
| Effort du front sous-estimé | Moyenne | RJSF plutôt que des formulaires écrits à la main ; 4 écrans, plafond ferme |
| Dérive vers un clone de ComfyUI | Moyenne | Périmètre §11 relu à chaque jalon |
| Une opération de dédup détruit des poids | Faible mais grave | Hash vérifié avant tout lien, `--dry-run` obligatoire, jamais de suppression sans plan affiché |
