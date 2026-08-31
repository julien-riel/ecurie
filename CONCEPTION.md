# Écurie — Conception détaillée

> Ce document complète `ARCHITECTURE.md`. L'architecture dit *quoi* et *pourquoi* ;
> ici on fixe *comment* : structures de données, interfaces, algorithmes, formats.
> Le plan d'exécution correspondant est dans `PLAN.md`.
>
> Rédigé le 19 août 2026, révisé le 20 août au terme du v0.3 : les sections §2,
> §3, §5, §8, §9, §12 et §13 portent maintenant ce que l'exécution réelle a
> établi, et non plus seulement ce qui était prévu. Ce qui a été démenti par la
> mesure est signalé comme tel plutôt que réécrit en silence.
>
> Révisé de nouveau le 21 août, au fil des tâches du v0.4 : le §7 dit ce que le
> socle de l'UI a établi, les §1.1 et §5 ce que le déménagement du superviseur
> dans le processus de l'API a changé, et le §6 ce que la route des jobs a
> tranché — la forme des URL de sortie, et ce qui se refuse avant plutôt qu'après.
>
> Révisé le 29 août 2026, au pivot v1.0 — le serveur MCP devient la porte
> d'entrée du produit ; le pourquoi est dans `ARCHITECTURE.md`, la réalisation
> dans `PLAN.md`. Ici : le §5.5 fixe les profils par classe de machine et les
> trois états de l'admission, le §6.3 la conception du serveur MCP, et le §8
> gagne la validation de forme et la garde du profil aveugle. Les §9 et §10
> restent la conception d'écrans et d'une veille aujourd'hui gelés.

---

## 1. Décisions structurantes complémentaires

Trois décisions que l'architecture n'a pas tranchées et qui conditionnent tout le code.

### 1.1 État déclaré vs état observé

Le registre Git est la source de vérité du **déclaré** : manifestes, contrats de
capacité, golden sets, mesures, préférences A/B. Mais le scan disque, la télémétrie
d'usage et le cache de hachage sont de l'**observé** : volatil, propre à la machine,
sans valeur historique. Le committer polluerait le dépôt à chaque scan.

Séparation stricte :

| État | Support | Contenu |
|---|---|---|
| Déclaré | `registry/` en Git | manifestes, capacités, `measurements/` (un fichier par machine), `evals/preferences.jsonl` |
| Observé | `~/.ecurie/state.db` (SQLite) | artifacts, locations, cache de hash, télémétrie |
| Observé | `~/.ecurie/residents.json` | modèles chargés, **miroir** de ce que chaque processus tient en mémoire |
| Dérivé | recalculé, jamais committé | classement Elo, trois chiffres d'occupation, plans de GC |

Les résidents mémoire sont à part, hors SQLite : ils changent à chaque commande,
sont lus et réécrits sous verrou exclusif par le superviseur (§5.4), et une
entrée dont le processus est mort doit disparaître à la lecture. Un fichier JSON
verrouillé dit cela plus simplement qu'une table qu'il faudrait garder en
cohérence avec des PID. Les sockets des workers, eux, vivent dans un répertoire
temporaire court et non dans `~/.ecurie` : `sun_path` est limité à 104 octets.

**Depuis la tâche 4.6, ce fichier n'est plus l'état du superviseur : il en est le
miroir.** Le superviseur vit aussi longtemps que son processus — une commande
dans la CLI, le serveur entier dans `ecurie serve` — et c'est en mémoire que se
tiennent l'occupation de chaque résident et le tour de rôle des jobs sur un même
worker. Ce que le fichier porte encore, et qui ne peut vivre ailleurs : la liste
des workers qu'un **autre** processus a chargés, et le verrou exclusif qui
empêche deux superviseurs de conclure chacun de son côté qu'il reste de la place.
Chacun y publie sa vue et n'y lit que celle des autres.

Le **résolveur** (`packages/core/resolver.py`) fait la jointure : pour chaque variant
déclaré, il retrouve ses artifacts observés et en déduit le `tier` réel (un variant
déclaré `hot` dont les fichiers ont disparu est signalé, pas cru sur parole).

Tables SQLite :

```sql
artifacts (sha256 TEXT PRIMARY KEY, size INTEGER, first_seen TEXT)
locations (path TEXT PRIMARY KEY, sha256 TEXT, size INTEGER, mtime REAL,
           device INTEGER, inode INTEGER, link_kind TEXT,   -- plain|hardlink|symlink|clone
           manager TEXT,                                    -- hf|ollama|lmstudio|comfy|declared
           variant_ref TEXT)                                -- "model@variant" si résolu
hash_cache (path TEXT, size INTEGER, mtime REAL, inode INTEGER,
            quick_hash TEXT, sha256 TEXT, verified_at TEXT)
runs (id TEXT PRIMARY KEY, variant_ref TEXT, started_at TEXT, duration_ms INTEGER,
      job_dir TEXT, ok INTEGER)
```

### 1.2 Hachage à trois niveaux

Un sha256 complet sur 30 Go prend plusieurs minutes ; un scan qui hache tout est
inutilisable. Trois niveaux, du gratuit au coûteux :

1. **Identité d'inode** — `(device, inode)` identiques ⇒ même Artifact, lien dur déjà
   en place. Gratuit via `os.stat`. Détecte la dédup déjà faite.
2. **Hash annoncé** — Ollama nomme ses blobs `sha256-…` ; les blobs HF LFS portent
   leur sha256 dans le nom du fichier `blobs/`. Accepté pour la **comptabilité**
   (trois chiffres, arbre de duplication), marqué `verified_at = NULL`.
3. **sha256 complet** — calculé uniquement : (a) avant toute opération destructive
   (règle §6.3 de l'architecture, non négociable), (b) sur `ecurie store verify`,
   (c) pour les fichiers sans hash annoncé dont la taille coïncide avec un autre
   (candidats à la dédup). Mise en cache par `(path, size, mtime, inode)` —
   invalidé si l'un des quatre change.

Le pré-filtre des doublons potentiels est la **taille exacte en octets** : deux
fichiers de tailles différentes ne sont jamais comparés.

### 1.3 Suppression = quarantaine, jamais `rm`

Aucune action du plan de GC ne supprime directement. `apply` déplace vers
`~/.ecurie/trash/<date>/<sha256-prefix>/` avec un `manifest.json` (origine, motif,
plan source). `ecurie store trash empty` vide après confirmation ; une purge
automatique à 30 jours est proposée, jamais imposée. Coût : nul (rename sur même
volume). Bénéfice : l'erreur de GC cesse d'être irréversible — c'est la mitigation
concrète du dernier risque du §13 de l'architecture.

---

## 2. Arborescence cible et migration du dépôt actuel

État au terme du v0.3, plus la tâche 4.1 et les golden sets du 5.1 — ce qui
existe porte une coche, le reste attend son jalon :

```
ecurie/                          # monorepo uv workspace
  ARCHITECTURE.md  CONCEPTION.md  PLAN.md
  registry/
    schema/{model,capability,golden}.schema.json  ✓
    capabilities/*.json                        ✓ 25 contrats atomiques
    models/*.yaml                              ✓ 26 manifestes, un par capacité au moins
    measurements/<id>@<variant>/<machine>.json ✓ 54 profils mesurés, par machine
    evals/
      bench/<capability>.json + assets/        ✓ charges type du banc d'essai
      golden/<capability>/manifest.json        ✓ 11 jeux (§9), dont l'ASR sans son
      results/  preferences.jsonl                v0.5
    veille/                                      v0.6
  packages/
    core/  store/  runtime/                    ✓
    api/                                       ✓ lectures, puis les jobs (4.1)
    veille/                                      v0.6
  tools/golden_assets.py                       ✓ recette des entrées d'essai
  tools/{openapi_dump,ui_fixtures}.py          ✓ ce que le front fige du serveur
  apps/ui/                                     ✓ socle piloté par schéma (4.3)
  runtimes/                       # envs isolés — .gitignore sur les .venv
    mlx-audio/       pyproject.toml + uv.lock  ✓ TTS
    mlx-audio-music/ pyproject.toml + uv.lock  ✓ chanson (commit épinglé, voir §5.3)
    mlx-vlm/         pyproject.toml + uv.lock  ✓ document + description d'image
    mlx-lm/          pyproject.toml + uv.lock  ✓ texte + traduction + appel d'outils
    diffusers-mps/   pyproject.toml + uv.lock  ✓ image
    torch-vision/    pyproject.toml + uv.lock  ✓ détourage + agrandissement
    hunyuan3d/       pyproject.toml + run.py   ✓ éprouvé au banc le 2026-08-24 (§13.4)
  .claude/skills/veille-modeles/SKILL.md       ✓
  .github/workflows/{registry-ci.yml, veille.yml}  v0.6
```

Le `uv.lock` de chaque environnement est versionné, au même titre que son
`pyproject.toml` : « reconstructible » ne veut rien dire si la reconstruction ne
redonne pas les mêmes versions que celles sur lesquelles le profil a été mesuré.

Outillage : Python 3.12, `uv` workspace (un `pyproject.toml` racine, un par paquet),
`ruff` + `pytest`, CLI construite avec `typer` + `rich`.

---

## 3. `packages/core` — modèles, résolveur, config

- **Modèles pydantic** générés à la main en miroir de `model.schema.json`
  (`Model`, `Variant`, `Source`, `Profile`), plus `Capability` (contrat d'E/S) et
  les quatre niveaux observés (`Artifact`, `Location`). Un test de conformité
  garantit que pydantic et le JSON Schema acceptent/refusent les mêmes documents —
  le schéma JSON reste l'autorité (c'est lui que la CI et les agents de veille lisent).
- **Chargement du registre** : `load_registry(path) -> Registry` lit tous les YAML,
  valide contre le schéma, vérifie les invariants inter-fichiers : un seul
  `incumbent` par capacité, `capability` du manifeste existe dans `capabilities/`,
  `runtime_env` existe sous `runtimes/`, `revision` non-placeholder si
  `status: active`. S'y ajoute la **validation croisée** avec le contrat de
  capacité : chaque clé de `defaults:` doit être un paramètre déclaré par le
  contrat, et sa valeur doit valider le sous-schéma correspondant. Une clé
  inconnue n'est pas une coquille sans suite — elle ne serait jamais transmise au
  worker, et le réglage qu'on croit appliqué ne le serait pas.
- **Deux blocs de réglages, et il faut les distinguer** : `defaults:` porte les
  valeurs par défaut des paramètres *du contrat* pour ce variant, donc
  interchangeables entre modèles d'une même capacité et affichées par l'UI ;
  `options:` porte les réglages *propres au runtime* — la langue forcée d'un
  moteur TTS, un mode de découpe — qui ne sont pas un dénominateur commun de la
  capacité, ne sont pas croisés avec le contrat, et sont transmis tels quels au
  worker en `params`. Sans cette séparation, il faudrait choisir entre relâcher la
  validation croisée et interdire tout réglage spécifique à un moteur.
- **Ce que la capacité fait d'une personne réelle** : `human_subject` sur le
  contrat, à trois valeurs — `analyzes` mesure quelqu'un déjà présent dans
  l'entrée sans le nommer ni le reproduire, `identifies` rattache l'entrée à une
  personne nommable, `synthesizes` produit son image ou sa voix faisant ce
  qu'elle n'a pas fait. Absent pour la grande majorité des capacités, qui ne
  portent pas spécifiquement sur des personnes.

  **Ce champ ne double pas `license_class`, il répond à l'autre question.** Le
  premier dit ce que le droit interdit ; celui-ci dit ce que la capacité fait.
  Les deux ne se recouvrent pas : EdgeFace est sous BSD-3, la licence la plus
  permissive de la famille visage, et cela ne rend pas plus légitime d'encoder le
  visage de quelqu'un qui n'a rien demandé. Un manifeste parfaitement en règle
  peut servir un usage qui ne l'est pas.

  Porté par le **contrat** et non par le variant, parce que c'est la capacité qui
  décide : tous ses modèles rendent la même chose. Le premier bénéficiaire n'est
  d'ailleurs pas la famille qui l'a fait naître — `voice-clone` est au parc
  depuis le v0.3 et n'avait aucun moyen de dire qu'il fabrique la voix de
  quelqu'un.

- **Config machine** : `~/.ecurie/config.toml` — chemins des gestionnaires scannés
  (avec autodétection par défaut), volumes de tiering autorisés, budget mémoire
  (`auto` = `recommendedMaxWorkingSetSize` lu via MLX, ou valeur explicite),
  chemins déclarés manuellement, politique de résidence (`max_heavy_resident`,
  `heavy_threshold_bytes`, `resident_idle_timeout_s`) et garde de disque de
  `pull` (`free_disk_ratio`).

  MLX n'est pas installé dans l'env d'Écurie et ne doit pas l'être : le budget
  `auto` est donc lu en interrogeant le mlx d'un environnement de runtime, avec
  repli sur 75 % de la mémoire physique. La provenance du chiffre est toujours
  affichée à côté de lui — un budget lu dans Metal et un budget déduit d'une
  règle de trois ne méritent pas la même confiance. Relevé sur la machine de
  référence : 17,76 Gio par Metal, ce que le §7 de l'architecture annonçait.

- **Profil dont le pic dépend de l'entrée** : `profile.peak_scaling` déclare un
  paramètre du contrat, une ordonnée à l'origine, une pente en octets par unité,
  et l'intervalle sur lequel elle a été ajustée. Sans lui, un modèle dont le coût
  varie avec ce qu'on lui demande force un choix perdant — inscrire le pire cas
  refuse des jobs qui passeraient, inscrire un cas favorable laisse partir la
  machine en swap. Mesuré, jamais saisi (§8). Le champ est optionnel : un modèle
  à coût fixe n'en a pas, et `peak_unified_memory_bytes` reste le chiffre
  comparable entre variants.

---

## 4. `packages/store` — scan, comptabilité, dédup, GC, tiering

### 4.1 Scanners

Interface commune : `Scanner.scan() -> list[Location]`. Un scanner par gestionnaire :

| Scanner | Méthode | Hash annoncé |
|---|---|---|
| `hf` | `huggingface_hub.scan_cache_dir()` — donne repo, révision, blobs, symlinks | oui (nom de blob LFS) |
| `ollama` | parser `manifests/**` (JSON style OCI) → blobs référencés ; blob présent mais non référencé = orphelin | oui (nom `sha256-…`) |
| `lmstudio` | walk `~/.lmstudio/models` | non |
| `comfy` | walk des dossiers `models/*` de chaque installation déclarée | non |
| `declared` | chemins listés dans la config | non |

Chaque scan est incrémental : une Location dont `(size, mtime, inode)` n'a pas bougé
n'est pas re-résolue. Le lien Location → variant du registre se fait par le résolveur
(repo+révision pour HF, chemin déclaré sinon, hash en dernier recours).

### 4.2 Les trois chiffres

- **Apparent** = Σ `size` des Locations (symlinks exclus).
- **Réel unique** = Σ `size` par `sha256` distinct ; les fichiers sans hash (niveau 1–2
  indisponible) comptent chacun pour eux-mêmes, avec mention « non vérifié » dans le
  rapport.
- **Récupérable** = les quatre postes du §6.2 de l'architecture, chacun calculable :
  duplication inter-gestionnaires (même sha256, inodes distincts, même volume),
  révisions HF obsolètes (snapshots non référencés par un manifeste actif),
  blobs orphelins (HF et Ollama), variants jamais utilisés (`runs` vide depuis N jours
  — poste vide tant que la télémétrie n'existe pas, affiché comme « inconnu », pas 0).

### 4.3 Plan de GC — format et exécution

Un plan est un fichier JSON versionnable, relisible, exécutable :

```json
{ "generated_at": "…", "scan_id": "…",
  "actions": [
    { "kind": "hardlink", "keep": "/path/a.gguf", "replace": ["/path/b.gguf"],
      "sha256": "…", "bytes_reclaimed": 4900000000 },
    { "kind": "trash",    "path": "…", "reason": "hf-stale-revision", "bytes_reclaimed": 0 },
    { "kind": "tier",     "path": "…", "dest_volume": "/Volumes/Parc", "bytes_reclaimed": 0 }
  ],
  "total_bytes_reclaimed": 0 }
```

`ecurie store plan` génère et affiche (gain par poste). `ecurie store apply <plan>`
exécute avec, pour chaque action : re-vérification du sha256 complet **au moment de
l'exécution** (jamais sur le plan seul — le disque a pu bouger), refus si le fichier a
changé depuis le scan, journal d'application. Le lien dur exige même volume
(`st_dev` identique) ; le remplacement est atomique (`link` vers un nom temporaire
puis `rename`).

### 4.4 Tiering

`tier` : migration `copy → fsync → sha256 de la copie → rename de l'original vers
trash → symlink`. Le manifeste n'est pas modifié par l'outil : la commande affiche le
patch YAML (`tier: cold`) à committer — cohérent avec « toute évolution du parc passe
par Git ». Au scan, un symlink dont la cible est absente marque le variant
`cold-unavailable` ; l'API l'expose et l'UI grise.

**Décider *quoi* déporter demande un chiffre que rien ne calculait**, et l'écran
Parc (4.5) l'a fait apparaître : `footprints()` rend l'empreinte disque de chaque
variant observé. Deux nombres par variant, et leur écart est le sujet même du
tiering — `bytes` est ce qu'il occupe, `freed_bytes` ce que le volume de départ
récupérerait. Ils diffèrent dès qu'un inode a une référence hors du parc scanné :
le déport copie alors des giga-octets sans en libérer un seul, et n'afficher que
le premier ferait déporter pour rien. La règle est celle qu'`entierement_couvert`
tenait déjà pour le plan de GC, rendue publique pour l'occasion — les deux
décisions posent la même question au même endroit.

Deux pièges de plus, et ils viennent du parc réel : un même inode à plusieurs
chemins ne pèse qu'une fois — le cache HF en est plein —, et **un fichier peut
appartenir à deux variants**. Les mêmes poids Qwen3-VL servent la lecture de
document et la description d'image ; chacun affiche ses 5,78 Go, si bien que la
somme de la colonne dépasse le parc. `shared_with` est ce qui explique l'écart au
lecteur plutôt que de le laisser conclure à une erreur de calcul.

`cold_links()` est sorti de `compute_figures` pour la même occasion : la route du
tiering n'a besoin que de cette liste, et la lui faire payer une classification
complète du parc reviendrait à parcourir trente mille chemins pour afficher trois
lignes. Ce que la fonction porte de délicat tient en un test : le cache HF est
lui aussi un champ de liens symboliques — chaque `snapshots/<révision>/<fichier>`
pointe vers un blob —, et seul `meta.snapshot` les distingue d'un variant déporté.

---

## 5. `packages/runtime` — workers, adaptateurs, admission

### 5.1 Protocole worker (JSON Lines sur stdio)

Un **worker** est un sous-processus lancé dans le venv de `runtime_env`, qui reste
vivant entre les jobs (le warmup est payé une fois). Protocole, une ligne JSON par
message :

```
→ {"op":"load","variant":{…manifeste résolu…}}
← {"ev":"loaded","warmup_ms":2400,"peak_memory_bytes":3100000000,"options":{"voices":["…"]}}
→ {"op":"infer","job_id":"j1","input":{"text":"…"},"params":{"speed":1.0},
   "output_dir":"/…/outputs/j1","seed":42}
← {"ev":"progress","job_id":"j1","pct":40,"note":"…"}        (0..n)
← {"ev":"result","job_id":"j1","output":{"audio":"audio.wav"},"metrics":{"rtf":0.11}}
← {"ev":"error","job_id":"j1","message":"…","trace":"…"}
→ {"op":"unload"}  ← {"ev":"unloaded","rss_bytes":…}
→ {"op":"ping"}    ← {"ev":"pong","rss_bytes":…}
```

Règles : les sorties binaires vont **en fichiers** dans `output_dir`, jamais en
base64 sur stdio ; `stderr` du worker est capturé en log, seul `stdout` porte le
protocole ; un worker qui ne répond pas au ping en 10 s est tué (SIGTERM puis SIGKILL)
et son variant marqué non résident.

`unloaded` accuse réception du déchargement. Sans lui, le superviseur ne saurait
pas quand la mémoire est rendue et chargerait le modèle suivant par-dessus le
précédent — le swap que le contrôle d'admission (§5.4) existe pour empêcher.

Deux transports pour ce même protocole, selon la durée de vie du worker :

- **stdio**, pour un worker attaché à la commande qui le lance : c'est le mode du
  banc d'essai, qui mesure un modèle seul et ne doit rien laisser derrière lui ;
- **socket Unix** (`--listen`), pour un worker résident qui survit à la commande.
  C'est ce qui permet à `ecurie run` de retrouver un modèle déjà chaud et de ne
  pas repayer le warmup à chaque phrase (§7 de l'architecture). Le worker écoute
  une connexion à la fois ; le dialogue est identique, octet pour octet.

Cette dernière ligne a plus de portée qu'il n'y paraît, et elle a tranché une
décision du 4.6 : **la connexion se ferme entre deux jobs**, elle ne se garde
pas. La garder ouverte pour épargner un `connect()` — une microseconde sur un
socket local — priverait tout autre processus de l'accès au worker, et
neutraliserait son délai d'inactivité, qui se compte dans l'attente d'une
connexion. Un modèle chargé une fois ne serait plus jamais rendu de lui-même.

Côté worker, le descripteur 1 est réservé au protocole dès le démarrage et
remplacé par le descripteur 2 : une barre de progression ou un avertissement de
bibliothèque part alors dans le journal au lieu de couper une ligne JSON en deux.

### 5.2 Adaptateurs

Un adaptateur = un module worker (`packages/runtime/workers/<runtime>.py`) rendu
visible par `PYTHONPATH` dans le venv cible, qui traduit le protocole vers la
bibliothèque du runtime. Rien n'est installé dans le venv du runtime : seuls
`protocol`, `channel` et `workers/base` y sont visibles, tous trois en
bibliothèque standard pure.

Quarante-six sont livrés, plus le chemin `custom` :

| runtime | capacité | adaptateur | poids partagés avec |
|---|---|---|---|
| `mlx-audio` | *défaut du runtime* | `workers/mlx_audio.py` | |
| `mlx-audio` | `text-to-music` | `workers/mlx_audio_music.py` | |
| `mlx-audio` | `speaker-diarization` | `workers/moss_diarize.py` | |
| `mlx-audio` | `speech-to-text` | `workers/moss_transcribe.py` | `moss-transcribe-diarize` |
| `mlx-audio` | `audio-to-text` | `workers/qwen2_audio.py` | |
| `mlx-audio` | `voice-clone` | `workers/omnivoice.py` | |
| `mlx-audio` | `audio-denoise` | `workers/dfn3_denoise.py` | |
| `mlx-audio` | `audio-separation` | `workers/demucs_separate.py` | |
| `mlx-vlm` | *défaut du runtime* | `workers/mlx_vlm.py` | |
| `mlx-vlm` | `image-to-text` | `workers/mlx_vlm_describe.py` | `qwen3-vl-8b-ocr` |
| `mlx-vlm` | `video-to-text` | `workers/mlx_vlm_video.py` | `qwen3-vl-8b-ocr` |
| `mlx-vlm` | `image-detect` | `workers/mlx_vlm_detect.py` | `qwen3-vl-8b-ocr` |
| `mlx-vlm` | `text-generation` | `workers/mlx_vlm_text.py` | `qwen36-27b-*` |
| `mlx-vlm` | `translation` | `workers/mlx_vlm_translate.py` | `qwen36-27b-*` |
| `mlx-vlm` | `tool-use` | `workers/mlx_vlm_tools.py` | `qwen36-27b-*` |
| `mlx-vlm` | `image-segment` | `workers/sam3.py` | |
| `mlx-vlm` | `audio-to-text` | `workers/mlx_vlm_audio.py` | |
| `mlx-lm` | *défaut du runtime* | `workers/mlx_lm.py` | |
| `mlx-lm` | `translation` | `workers/mlx_lm_translate.py` | |
| `mlx-lm` | `tool-use` | `workers/mlx_lm_tools.py` | |
| `torch-vision` | `image-matting` | `workers/birefnet.py` | |
| `torch-vision` | `image-upscale` | `workers/swin2sr.py` | |
| `torch-vision` | `image-segment` | `workers/sam2.py` | |
| `diffusers-mps` | *défaut du runtime* | `workers/diffusers_mps.py` | |
| `diffusers-mps` | `image-inpaint` | `workers/diffusers_inpaint.py` | `sdxl-base` |
| `diffusers-mps` | `image-to-image` | `workers/diffusers_img2img.py` | `sdxl-base` |
| `diffusers-mps` | `text-to-video` | `workers/ltx_video.py` | |
| `diffusers-mps` | `image-to-video` | `workers/ltx_video_i2v.py` | `ltx-video-2b` |
| `depth-anything` | `depth-estimation` | `workers/depth_anything.py` | |
| `mflux` | `image-upscale` | `workers/seedvr2.py` | |
| `rtmlib` | `video-to-motion` | `workers/rtmw3d.py` | |
| `uniface` | `face-detect` | `workers/uniface_detect.py` | |
| `uniface` | `face-landmark` | `workers/uniface_landmark.py` | `retinaface` |
| `uniface` | `face-parse` | `workers/uniface_parse.py` | `retinaface` |
| `uniface` | `face-embed` | `workers/uniface_embed.py` | `retinaface` |
| `uniface` | `face-headpose` | `workers/uniface_headpose.py` | `retinaface` |
| `uniface` | `face-gaze` | `workers/uniface_gaze.py` | `retinaface` |
| `chronos` | `time-series-forecast` | `workers/chronos_forecast.py` | |
| `mlx-audio` | `audio-align` | `workers/qwen3_align.py` | |
| `torch-vision` | `image-embed` | `workers/dinov3_embed.py` | |
| `terratorch` | `geo-segment` | `workers/prithvi_segment.py` | |
| `terratorch` | `geo-embed` | `workers/prithvi_embed.py` | |
| `esm-torch` | `protein-embed` | `workers/esm_embed.py` | |
| `cad-recode` | `pointcloud-to-cad` | `workers/cad_recode.py` | |
| `depth-anything` | `multiview-to-3d` | `workers/da3_multiview.py` | `da3-large` |
| `lerobot` | `robot-action` | `workers/smolvla_act.py` | |
| `custom` | — | l'`entrypoint` du manifeste (chemin de Hunyuan3D) | |

La quatrième colonne est celle qui décide du coût d'une capacité de plus : un
tiers des adaptateurs tournent sur des octets déjà téléchargés pour un autre
contrat. La famille `uniface` en est le cas le plus systématique — cinq de ses
six capacités partagent le **détecteur** de la sixième, parce qu'aucune ne
cherche les visages qu'elle traite. `diffusers_img2img` en est le cas le plus net — il ne diffère de son
voisin `diffusers_inpaint` que par l'absence d'un masque, et il est exécutable le
jour où son manifeste entre au registre.

**Un runtime peut servir plusieurs capacités par des API qui n'ont rien en
commun.** `mlx_audio.tts` et `mlx_audio.music` ne partagent ni le chargement, ni
l'appel, ni la sortie ; les fondre dans un adaptateur donnerait un fichier qui
commence par un aiguillage et ne se relit plus. Le choix se fait donc sur le
couple (runtime, capacité), avec repli sur le runtime seul.

`torch-vision` va au bout de cette logique : il n'a **aucun** adaptateur par
défaut. Détourer et agrandir ne partagent que la pile PyTorch/MPS, et un runtime
est une famille de bibliothèques, pas une promesse d'API commune. `mlx-lm`, à
l'inverse, partage un vrai socle — chargement, échantillonnage, mesure du pic —
que ses trois adaptateurs héritent d'une classe de base commune.

Règles qu'un adaptateur ne peut pas enfreindre, apprises à l'usage :

- **aucun import de la bibliothèque du runtime au niveau du module** — tout dans
  `load()`, dans un try/except qui nomme `ecurie env sync <env>`. C'est ce qui
  permet à la CI, sans Apple Silicon, d'importer tous les adaptateurs ;
- le worker ne télécharge rien (`HF_HUB_OFFLINE=1` est posé par le superviseur) :
  il reçoit un chemin local déjà vérifié, ce qui garantit que la révision
  exécutée est celle qui sera écrite au manifeste du job ;
- ce qu'un adaptateur ne peut pas honorer, il le **dit** plutôt que de le
  simuler. Qwen3-TTS n'a pas de contrôle de vitesse et MiniMax Music ignore
  `guidance_scale` : ces réglages remontent en avertissement dans les métriques
  du job, jamais en rééchantillonnage maison ni en kwarg avalé sans effet ;
- **ce qui vient de l'amont se lit, jamais ne se suppose.** Les quatre
  adaptateurs du v0.4 ont échoué à leur premier lancement, et à chaque fois sur
  une hypothèse écrite avant mesure : le `requirements.txt` publié avec les poids
  de BiRefNet omet trois paquets que son code importe ; ces mêmes poids sont en
  demi-précision alors que le commentaire de l'adaptateur affirmait le contraire ;
  `apply_chat_template` rend un `BatchEncoding` et non une chaîne depuis
  `transformers` 5 ; et le jeton de fin de tour se retrouve **dans le texte**
  rendu, sans que rien n'échoue. Le facteur d'agrandissement, lui, est désormais
  lu dans la config des poids, et la précision dans le type des paramètres.

`ollama` et `comfy` (proxys HTTP vers leurs serveurs) viennent après, ce sont les
plus simples.

### 5.3 Environnements isolés

`runtimes/<env>/{pyproject.toml, uv.lock}` versionnés, `.venv` ignoré.
`ecurie env sync [env]` exécute `uv sync` dans chaque env. Le superviseur refuse de
lancer un worker si le venv est absent et affiche la commande de réparation. Aucune
dépendance de runtime dans l'env racine — l'env racine ne connaît que pydantic,
typer, FastAPI, PyYAML, huggingface_hub.

**Un runtime peut avoir plusieurs environnements**, désignés par `runtime_env` au
manifeste. Le cas s'est présenté dès le premier modèle musical : son support est
mergé en amont mais publié dans aucune version de `mlx-audio`, donc son env
épingle un commit — pendant que celui du TTS reste sur la version publiée sur
laquelle son profil a été mesuré. Faire dépendre un profil qui tient d'une
version qui bouge serait le contraire de ce que l'isolation cherche.

Le `pyproject.toml` d'un env est aussi l'endroit où **rattraper une dépendance
que l'amont oublie de déclarer**. `mlx-vlm` charge `jinja2` pour son gabarit de
conversation sans la déclarer : le modèle se chargeait, puis le premier job
mourait sur un `ImportError`. Une ligne dans l'env, et le problème est réglé sans
attendre l'amont.

**Deux environnements peuvent porter le même moteur d'inférence sans fusionner.**
`rtmlib` et `uniface` servent tous deux de l'ONNX et rien d'autre, et la question
de les fondre s'est posée. La réponse est non, pour la raison qui a fait exister
les autres : `uniface` impose `scikit-image` et `scipy` que `rtmlib` n'a jamais
vus, et `rtmlib` épingle ses propres bornes. Faire entrer une capacité neuve au
prix d'une rétrogradation chez un modèle déjà mesuré est exactement l'arbitrage
que l'isolation existe pour ne plus avoir à faire.

**Quatre des cinq env du 24 août 2026 portent PyTorch, et aucun n'a pu entrer
chez un autre.** Le motif est chaque fois une borne, jamais une préférence :
`chronos` résout numpy 2.5 quand `depth-anything` impose `numpy<2` ; `terratorch`
tire lightning, torchgeo, rasterio et geopandas ; `esm-torch` demande
transformers 5.15 que `torch-vision` rétrograderait sous BiRefNet ; `lerobot`
borne lui-même `torch<2.12` et `transformers<5.6` ; et `cad-recode` a besoin de
`transformers<5`, où la borne n'est pas une prudence mais **la condition de
fonctionnement** — au-delà, le modèle ne lève rien et produit du charabia. Le
prix mesuré de cet isolement est plus faible qu'on ne le croit : le venv d'ESM
pèse 699 Mio, celui de `torch-vision` qu'on aurait réemployé en pèse 756.

Un cinquième cas mérite d'être noté à part, parce qu'il ne coûte rien :
`image-embed` et `audio-align` sont entrées dans des env **existants**, l'une en
relevant un plancher sur un paquet déjà installé au-dessus, l'autre sans toucher
à une ligne. La question à poser devant un runtime neuf reste donc celle de la
couverture complète du registre : lequel des env déjà là sait déjà le faire ?

`cad-recode` est en revanche le **second env dont `ecurie env sync` ne suffit
pas**, après `hunyuan3d`, et le premier pour un motif juridique : son code amont
est sous licence non commerciale et ne peut pas être committé ici. Le README de
l'env porte la commande, et `ecurie env list` le signale dans sa colonne « À
lire » — un env annoncé « prêt » qui ne l'est pas enverrait découvrir le manque
au premier job, plusieurs gigaoctets plus tard.

### 5.4 Contrôle d'admission

Le superviseur (dans le processus API) tient la table des résidents
`{variant_ref, peak_bytes (profil mesuré), last_used}`. Avant `load` :

```
budget    = config.memory_budget          # défaut : recommendedMaxWorkingSetSize
peak      = profil.peak_scaling ? base + pente × entrée[paramètre] : pic mesuré
résiduel  = budget − Σ peak_bytes(résidents)
tant que résiduel < peak(candidat) :
    décharger le résident LRU ni épinglé ni occupé ; recalculer
si peak(candidat) > budget seul → refus explicite (jamais de swap subi)
```

Le pic du candidat se calcule **avec l'entrée du job**, pas seulement avec son
variant : trente secondes de musique coûtent le double de quinze. Hors de
l'intervalle sur lequel la pente a été ajustée, l'admission extrapole, le dit, et
ne descend jamais sous le pire cas mesuré — la droite n'est éprouvée que dans sa
plage. Le job passe alors par le même chemin, avec l'avertissement au manifeste.

Deux résidents ne sont jamais évincés : les **épinglés**, et ceux sur lesquels un
**job tourne**. Le second cas n'est pas une politesse — décharger un worker en
pleine inférence ne libère rien tout de suite, cela détruit un travail en cours,
et la commande qui l'a provoqué n'en sait même rien.

**L'occupation vit dans la mémoire du superviseur** (tâche 4.6), et le pid n'en
est plus que la publication. Elle était le pid du processus détenteur, écrit dans
`residents.json` : un chiffre par processus, ce qui suffit tant qu'un processus
ne tient qu'un job — le cas d'une commande, jamais celui d'un serveur. Deux jobs
du même processus y inscrivaient le même pid, et le premier à finir l'effaçait :
le worker redevenait évinçable alors qu'une inférence tournait dessus. Ce que le
miroir transporte reste un pid, parce qu'il s'adresse aux autres processus et
qu'un pid se vérifie — un détenteur mort ne retient rien, là où un drapeau
resterait posé pour toujours.

**Un modèle sert un job à la fois, et cela se tient en amont du socket.** Le
worker résident écoute une connexion à la fois (`listen(1)`, §5.1) : deux jobs
lancés sur le même modèle attendaient donc dans le backlog, sans que rien ne dise
pourquoi, avec le délai d'inférence du second qui courait déjà. Un verrou par
variant les sérialise, tenu de l'admission à la fin du job. L'attente est
annoncée à qui la subit — « un job occupe déjà ce modèle » vaut mieux qu'une
commande qui semble avoir cessé de répondre —, et elle est bornée : au-delà d'un
job entier, c'est un bail qu'on n'a pas rendu, et cela se dit.

Ce que ce verrou ne fait pas : sérialiser deux **processus**. Un `ecurie run`
lancé pendant qu'un job de l'Atelier occupe le même modèle se retrouve, lui, dans
la file d'écoute du worker — c'est le comportement voulu, et le seul possible
sans donner à un processus autorité sur les jobs de l'autre.

Politique du §7 de l'architecture encodée en config : `max_heavy_resident = 1`
(lourd = peak > `heavy_threshold_bytes`, **8 Gio** sur la machine de référence
depuis le recalibrage du 20 août 2026 — à 6 Go, les quatre profils du parc réel
sont lourds et la règle ne discrimine plus rien), les légers restent chauds. Ces
8 Gio sont **45 % du budget** et non une constante : le défaut vaut `"auto"`, et
`resolve_heavy_threshold` les recalcule sur chaque machine, faute de quoi un Mac
de 16 Go retomberait dans le travers reproché aux 6 Go. Un variant **sans profil mesuré**
n'est exécutable qu'en mode mesure : parc déchargé entièrement, échantillonnage RSS,
le résultat écrit le premier profil. C'est ce qui rend la règle « jamais de profil
estimé » vivable au premier lancement. Cette règle a été écrite pour la machine
qui instruit son parc ; ce qu'elle coûterait à un adoptant — re-mesurer
soixante-douze variants avant le premier `run` — est réglé au §5.5, sans
l'assouplir : un pic qui n'a pas été mesuré ici porte son état sur lui, il ne se
fait jamais passer pour un relevé.

Le mode mesure vide le parc **épinglés compris** — un profil pris en concurrence
mesure la machine et non le modèle —, mais il s'arrête devant un **job en cours** :
une épingle est une préférence, un job est un travail, et l'évincer ne rendrait
pas la mémoire tout de suite. Là encore, le cas ne pouvait pas se poser tant
qu'une commande tenait seule le parc.

**L'admission raisonne par candidat unique, et deux extensions sont annoncées
sans être servies.** La **réservation groupée** (tâche 1.7) admet N candidats
sur la somme de leurs pics, tout ou rien — c'est le même algorithme plié sur
une liste, au service de l'outil `ecurie_fan_out` du §6.3. Les **réservations
durables** (§13.7) épingleraient N modèles pour la durée d'un pipeline — cela
touche à la nature du bail et à la doctrine « une épingle est une préférence
humaine », et se tranche quand le besoin sera réel, pas avant.

**Un refus se lit** — corrigé au 4.4, et le défaut datait du v0.3. La décision
d'admission porte une `reason` rendue telle quelle par `ecurie ps --for` et par
l'Atelier ; elle disait « demande 25704234348 octets, le budget entier est de
19070000000 », là où le §4 du plan exige « ce morceau de 30 s demanderait
24,2 Gio, au-delà des 17,8 disponibles ». Personne ne l'avait vu parce que les
tests comparaient la phrase à `str(20 * GIB)`, c'est-à-dire au calcul qu'ils
étaient censés vérifier — un test qui recalcule ce qu'il contrôle ne contrôle
rien. Les tailles passent maintenant par `ecurie_core.format.fmt_memory`, qui
compte en **unités binaires** : `fmt_bytes` du store est décimal parce qu'un
disque se lit ainsi, mais le budget, le seuil de lourdeur et les profils mesurés
sont des puissances de deux, et un seuil écrit `8 * (1 << 30)` s'affichait
« 8.59 Go » — un chiffre qui n'apparaît dans aucun fichier de configuration.

### 5.5 Profils par classe de machine — les trois états de l'admission

Le §5.4 tient tant que la machine qui exécute est celle qui a mesuré. Pour un
adoptant, cette identité se brise au premier `pull` : les relevés du dépôt
viennent d'un seul poste, et exiger de re-mesurer le parc avant le premier `run`
tuerait les dix minutes du parcours d'adoption — pendant que livrer des
estimations trahirait la règle qui rend l'admission digne de confiance. La
sortie n'est ni l'un ni l'autre : **l'admission garde un seul algorithme, mais
le pic qu'on lui donne porte désormais un état**, et cet état ne se tait jamais.

- **`measured-local`** — le relevé vient de cette machine. C'est le §5.4
  inchangé, et le seul état dont les chiffres finissent dans un bloc `profile:`
  ou un relevé de `registry/measurements/`.
- **`inherited-class`** — aucun relevé local, mais le dépôt en porte un pour la
  **même classe de machine**. Le pic hérité est majoré d'une marge conservatrice
  de **15 %**, et l'étiquette accompagne chaque décision : réponse d'outil MCP,
  `ecurie ps --for`, manifeste du job.
- **`bounded`** — aucun relevé, nulle part. Le pic est borné par le pire cas,
  `disk_bytes` × un facteur par runtime, versionné avec le code et calibré
  pessimiste. Jamais silencieux, et refusé net si la borne dépasse le budget :
  entre un job différé et un swap subi, le choix est déjà fait depuis le §0 de
  l'architecture.

L'ordre de résolution est celui de la liste, et il ne se remonte pas : un état
faible ne se fait jamais passer pour un fort, ni dans une réponse, ni dans un
manifeste, ni dans un fichier committé.

**La classe de machine, c'est famille de puce + mémoire unifiée** — et le layout
l'avait déjà compris : le « fichier par machine » du §8 se nomme
`mac17-4-24-gio.json`, c'est-à-dire par sa classe. On formalise : deux machines
de la même classe partagent le relevé. On n'hérite **jamais d'une classe
voisine** : un pic pris sous 36 Gio de mémoire unifiée ne dit rien de la
pression Metal à 24, et refuser de deviner est précisément ce que le produit
vend.

Deux mécanismes ramènent tout vers `measured-local`, sans jamais barrer la
première valeur :

**Le banc court au pull, en tâche de fond, quand le parc est calme.** Le mode
mesure vide le parc (§5.4) ; on ne le déclenche donc pas au milieu d'une
session. Mais dans le parcours d'un adoptant, après un `pull`, le parc *est*
vide : le banc part tout seul, et le premier `run` trouve souvent un relevé
local déjà écrit. S'il n'a pas fini, le job court quand même, en
`inherited-class` ou `bounded`, étiqueté.

**L'usage remplace le banc.** Chaque job mesure déjà son pic observé — les
métriques du manifeste (§6.2) — et un job complet exécuté en `inherited-class`
promeut son variant en `measured-local`. La promotion porte **le point d'usage,
pas la pente** : la pente de `peak_scaling` reste celle de la classe tant que le
banc n'a pas rejoué la charge type, parce qu'un point unique n'ajuste pas une
droite. Et le patch de `registry/measurements/` que le banc affiche (§8) devient
ici le premier geste communautaire : une PR d'un seul fichier JSON, validée
mécaniquement par la CI, mergeable sans relecture de code.

**Les résidents étrangers comptent.** `ecurie status` et la décision d'admission
lisent, au moment de décider, les modèles chargés d'Ollama (`/api/ps`) et de
LM Studio — en lecture seule — et les déduisent du budget : « Ollama tient
9,2 Gio ; ce job n'entre pas » vaut mieux qu'un swap subi dont Écurie aurait
juré être innocente. Un démon absent ne compte rien. En v1 on ne décharge
jamais ce qu'on n'a pas chargé soi-même : l'éviction négociée est un chantier
d'après, opt-in (`PLAN.md`).

---

## 6. `packages/api` — jobs, SSE, bibliothèque

FastAPI, lancée par `ecurie serve`. Surface v0.4 :

```
GET  /registry/capabilities              GET /registry/models[?capability=]   ✓
GET  /store/summary                      trois chiffres + arbre de duplication ✓
GET  /store/plan[?verified_only=]        plan de récupération, à blanc         ✓
GET  /store/tiering                      volumes, variants froids, empreintes  ✓
GET  /runtime/residents[?for=<ref>]      mémoire, budget, admission simulée    ✓
POST /runtime/admission {ref, input}     pic attendu pour cette entrée         ✓
POST /jobs        {ref, input, seed?}    → 202 {id, state, …}                  ✓
GET  /jobs/{id}   état + manifeste       GET /jobs/{id}/events   (SSE)         ✓
GET  /jobs/{id}/files/{chemin}           fichiers de sortie                    ✓
POST /uploads     multipart              → 201 {path, name, media_type, size}  ✓
POST /evals/preference  {capability, input_hash, a, b, winner}
GET  /library[?capability=&model=]       POST /library/{job_id}/replay
```

**Le plan de récupération est un `GET`, et la surface d'écriture du parc reste
vide** (tâche 4.5). Cette ligne portait `POST /store/plan` depuis l'origine, et
le verbe venait d'un raisonnement juste sur la CLI : `ecurie store plan` **écrit
un fichier**, parce que `ecurie store apply` en exige un — le plan est le
document qu'on relit avant de laisser un outil toucher trente giga-octets. Ce qui
a changé n'est pas ce besoin mais l'usage : l'écran Parc pose une question, il ne
demande pas un document. Un `POST` par consultation déposerait un plan dans
`~/.ecurie/plans/` à chaque ouverture d'onglet, et la relecture avant exécution
perdrait son sens à mesure que les fichiers s'accumuleraient. La route rend donc
le plan **entier** — actions, empreintes, écartés — sans le poser nulle part, et
`command` porte la commande qui l'écrit pour de bon.

Il en va de même du tiering, où la raison est plus forte encore : le §4.4 veut
que l'outil ne touche pas au manifeste et affiche le `tier: cold` à committer.
Un déport lancé depuis un navigateur laisserait donc le registre mentir sur
l'état du disque jusqu'au prochain commit — la moitié de l'opération n'est pas
automatisable par construction. `/store/tiering` montre où déporter, ce qui l'est
déjà, et ce que chaque variant rendrait ; `ecurie store tier` fait le reste.

**La tâche 4.1 a livré les lectures**, cochées ci-dessus. Elles n'engagent rien —
aucun modèle chargé, aucun octet déplacé, aucun manifeste écrit — et elles
suffisent à faire vivre l'Atelier. Quatre décisions les gouvernent :

- **le registre se recharge à chaud**, dès qu'un fichier de `registry/` a bougé.
  « Ajouter un modèle = ajouter un YAML, aucune ligne de front à écrire »
  perdrait beaucoup s'il fallait redémarrer le serveur pour voir le YAML qu'on
  vient d'écrire. Le budget mémoire, lui, est détecté une fois au démarrage : il
  se lit en lançant un sous-processus dans le venv d'un runtime ;
- **le serveur répond malgré un registre en erreur.** La CLI s'arrête, et c'est
  le bon geste avant d'exécuter ; une UI à qui l'on ne rend rien parce qu'un
  manifeste sur six a une révision non épinglée n'a aucun moyen d'apprendre
  lequel. Les modèles valides sont servis, `issues` transporte le reste ;
- **une lecture ne tue rien.** `ecurie ps` appelle `prune()`, qui arrête les
  workers devenus injoignables ; un `GET` rafraîchi toutes les deux secondes ne
  le peut pas. Les entrées périmées sont rapportées dans `stale`, avec
  `holds_memory` — un processus mort ne laisse qu'une ligne à balayer, un
  processus vivant sans socket retient toujours ses gigaoctets ;
- **la boucle locale, et des origines CORS énumérées.** L'API dit où sont les
  poids et ce que la machine a en mémoire : `ecurie serve` refuse une adresse
  non locale sans `--expose`, et n'autorise que les origines de développement
  connues — la boucle locale ne protège pas d'une page ouverte dans le navigateur.

**Le superviseur est unique et vit aussi longtemps que le serveur** (tâche 4.6).
Il en existait un par requête tant qu'il ne portait aucun état : celui des
résidents vivait dans un fichier verrouillé, et un objet jetable suffisait à le
lire. Deux choses l'ont rendu impossible — l'occupation d'un résident et le tour
de rôle des jobs sur un même worker (§5.4) —, et les deux étaient inévitables dès
lors qu'un même processus tient plusieurs jobs à la fois. Ce que l'unicité ne
doit pas figer, en revanche, c'est le registre : le superviseur le **redemande**
plutôt que de le garder, faute de quoi l'admission travaillerait sur un manifeste
que le rechargement à chaud a déjà remplacé.

Trois conséquences visibles, toutes éprouvées contre un vrai serveur :

- `GET /runtime/residents` montre le job d'un `ecurie run` lancé dans un
  terminal, pid à l'appui, et l'admission d'un modèle lourd est refusée pendant
  ce temps — « les résidents restants ne peuvent pas partir (qwen3-tts-1.7b
  (en cours de job)) » ;
- `ecurie unload` **refuse** un worker en plein job sans `--force`, ce qu'il ne
  faisait pas : le défaut datait du v0.3 et n'est devenu visible qu'en donnant au
  superviseur les moyens de savoir qu'un job tournait ;
- l'arrêt du serveur laisse les workers chargés — c'est ce qu'être résident veut
  dire — mais retire l'occupation qu'il publiait.

`/runtime/admission` est un `POST` par la forme et une lecture par l'effet : la
question porte une entrée complète, qu'on n'écrit pas dans une chaîne de requête.
C'est lui qui alimentera le bandeau de la tâche 4.7, parce qu'il fait parler
`peak_scaling` — trente secondes de musique demandent 24,2 Gio là où quinze en
demandent 13,8, et un bandeau qui l'ignorerait annoncerait le même chiffre pour
les deux. Il répond aussi sur une saisie incomplète, en rendant le coût **et**
les reproches du contrat : un champ encore vide n'est pas une erreur du client,
et refuser de répondre priverait l'utilisateur du chiffre au moment précis où il
le regarde.

**`/uploads` est la seconde surface d'écriture, et la dernière prévue.** La liste
des routes figées portait depuis la 4.1 une note qui disait le contraire :
« aucune route de téléversement, alors que dix champs du registre attendent un
fichier. Sans conséquence tant que le navigateur et le serveur partagent la
machine — le champ porte un chemin local — et à reprendre le jour où ce ne sera
plus vrai. » Ce jour n'est pas venu, et la route existe quand même : **ce n'est
pas le partage de machine qui a cessé d'être vrai, c'est le raisonnement qui en
découlait.** Une image choisie dans une page web, une photo prise par la caméra,
un son capté par le micro n'ont **jamais** eu de chemin à saisir, sur aucune
machine. Le serveur écrit le contenu et rend le chemin qu'il vient de créer ; le
champ du formulaire reste ce qu'il était, une chaîne que le worker ouvrira, et la
CLI continue d'accepter la même valeur (`ecurie run -p image=…`).

Quatre décisions la gouvernent, et elles se paient toutes si on les manque :

- **le sas n'est pas une bibliothèque.** `~/.ecurie/uploads/` reçoit ce qui sert
  à lancer un job ; `runner.stage_inputs` en recopie aussitôt le contenu dans le
  dossier du job, avec son sha256, et c'est ce dossier-là qui fait foi pour la
  reproductibilité (§6.2). Les dépôts de plus de sept jours sont balayés au dépôt
  suivant — pas par une tâche de fond, qui serait un fil de plus à surveiller
  pour un `unlink()`. Il n'y a **ni lecture ni liste ni suppression** par HTTP :
  elles feraient de ce dossier une seconde bibliothèque, avec deux vérités sur ce
  qui a servi d'entrée ;
- **le type accepté vient du registre, pas d'une liste écrite dans la route.**
  Les contrats déclarent ce que leurs champs fichier acceptent — `image/*`,
  `audio/*`, `video/*`, `application/pdf` —, et c'est exactement la question
  posée. `CapabilityContract.input_media_types()` est le symétrique
  d'`output_media_types()` et reconnaît un champ fichier comme le fait déjà
  `stage_inputs` : un `contentMediaType`, ou un `x-ui: "file"` ;
- **le nom d'origine ne compose jamais le chemin.** Il est réduit à ce qui tient
  dans un nom de fichier, précédé d'un jeton horodaté comme celui d'un job, et
  jamais employé seul : deux captures s'appellent toutes les deux
  `enregistrement.wav`, et « ../.. » est un nom de fichier acceptable pour qui
  l'envoie. L'extension manquante est déduite du type de média — `audio/wav` est
  justement l'un de ceux que `mimetypes` ne sait pas suffixer, le même trou que
  `model/gltf-binary` côté sorties ;
- **la taille est comptée pendant l'écriture.** `Content-Length` est déclaratif ;
  le seul chiffre fiable est celui des octets déjà écrits. Au-delà d'un gigaoctet
  le fichier partiel est supprimé et la réponse est un 413 — laisser la moitié
  d'une vidéo dans le sas serait une façon coûteuse de refuser.

`python-multipart` entre dans les dépendances de `ecurie-api` pour cette route :
Starlette lui délègue l'analyse du format et ne le déclare pas, si bien que son
absence est une panne **au démarrage**, pas à la requête. C'est la même famille
de manque que le `jinja2` de l'env `mlx-audio`.

### 6.1 Les jobs

`/jobs` est la **première surface d'écriture** de l'API, et la dernière pièce de
la tâche 4.1. Elle a attendu la 4.6 pour une raison de fond : un serveur qui lance
des jobs doit savoir lequel tourne, sur quel worker, et faire attendre le suivant
— ce qu'un superviseur reconstruit à chaque requête ne peut pas.

`POST /jobs` rend **202** et un identifiant : `run_job` est bloquant, il attend
son tour, charge un modèle, exécute — des minutes, parfois. Un fil par job, et
non un pool : un pool borné ferait attendre un job sur un modèle libre derrière
deux jobs en file sur un modèle occupé, c'est-à-dire le backlog qu'on vient de
retirer du socket, réintroduit un cran plus haut. La sérialisation a déjà son
endroit, le tour de rôle par variant ; ce qui reste ici n'est qu'un plafond de
nombre, contre un client qui s'emballe.

Ce qui est refusé **avant** de créer un job, et ce qui ne peut l'être qu'après,
sépare deux familles :

- un modèle inconnu (404), un variant que le disque contredit — poids absents,
  environnement non synchronisé, profil non mesuré (409, avec la commande qui
  répare) —, une entrée que le contrat rejette (422). Créer un job voué à
  l'échec pour le voir échouer trois secondes plus tard n'apprend rien de plus
  et laisse une trace ;
- l'**admission**, elle, ne se tranche qu'au moment de charger, après le tour de
  rôle : d'ici là, un autre job a pu finir et libérer la place. Le job existe
  donc, et il échoue en portant la phrase que le contrôle d'admission compose.
  Elle est préfixée « admission refusée : » et non du nom de la classe — un
  refus est une décision, pas une panne, et « AdmissionRefused » serait le seul
  mot anglais de tout le parcours.

**Le flux d'événements rejoue depuis le début.** Chaque événement porte l'état
complet du job plutôt qu'un fragment : le client remplace ce qu'il affiche, et un
client qui s'abonne en retard voit où l'on en est au lieu d'attendre le
changement suivant. Le flux se termine par un `end` explicite, sans quoi
`EventSource` rouvrirait la connexion indéfiniment sur un job terminé. Le journal
ne grossit pas pour autant : une progression qui répète le même pourcentage et la
même note n'est pas un événement.

**Le serveur compose l'URL des fichiers, le client ne la fabrique pas.** C'est ce
qui tranche la question laissée ouverte au 4.4 — un nom de fichier, ou un chemin
à plusieurs segments ? Les deux existent, `audio-separation` produisant
`tracks/vocals.wav` sous une clé pointée `tracks.vocals`. La route accepte donc un
chemin (`{chemin:path}`), et la réponse porte `files`, déjà composé. Trois
conséquences : le résolveur de fichiers du front est une lecture et non une
construction ; le type de média servi est celui que **le contrat promettait**,
lu dans le manifeste du job — un `.glb` est un `model/gltf-binary`, ce qu'aucune
table système ne dit ; et tout ce qui sort du dossier du job est un 404 et non un
403, la question « ce fichier existe-t-il ailleurs sur cette machine » n'ayant pas
à recevoir de réponse.

**Le flux se suffit à lui-même**, et il a fallu écrire le client pour s'en
apercevoir. `JobOut` portait `outputs` — les sorties du contrat qui sont des
fichiers — mais pas `output`, la réponse du worker telle qu'il l'a rendue. Or le
§7 aplatit **la réponse** et non les `properties` du contrat, et tout ce qui n'est
pas un fichier n'existe que là : `document-to-text.page_count`,
`speech-to-text.language`, `tool-use.call_names`. Sans ce champ, un client qui
n'écoute que le flux ne les verrait jamais et devrait relire le manifeste par une
seconde requête, que les événements ne transportent pas. Les deux clés coexistent
donc, et elles ne disent pas la même chose : `output` est ce qui a été produit,
`outputs` ce qui s'en télécharge.

Enfin, la table des jobs ne survit pas au redémarrage du serveur, **le dossier du
job si** : `GET /jobs/{id}` et les fichiers se replient dessus. C'est le
manifeste, et non la table, qui fait foi sur ce qui a été exécuté.

### 6.2 Bibliothèque et reproductibilité

Chaque job écrit `~/.ecurie/outputs/<job_id>/manifest.json` — capability, model, variant, révision
épinglée, params complets (défauts résolus inclus), seed, sha256 de l'entrée,
versions (harness, runtime, adaptateur), durée, métriques. L'entrée elle-même est
copiée dans le dossier du job (texte inline dans le manifeste, fichiers copiés).
*Rejouer* = re-soumettre le manifeste tel quel ; le serveur refuse si la révision
résolue diffère de celle du manifeste, sauf `--force`.

Un job = une ligne dans `runs` (télémétrie du poste « jamais utilisé » du GC).

### 6.3 Le serveur MCP — `packages/mcp`

La porte d'entrée du produit depuis le pivot du 29 août (`ARCHITECTURE.md`) :
`ecurie mcp`, un serveur MCP stdio dans son propre paquet. Il vit dans ce
chapitre parce qu'il est le deuxième consommateur de la surface du §6, après
l'Atelier — et qu'il n'en exige aucune route nouvelle : `POST /jobs`, le SSE,
`/jobs/{id}/files` et `POST /runtime/admission` suffisent.

**L'outillage est engendré, pas écrit.** Le bloc `input` d'un contrat de
capacité est déjà du JSON Schema 2020-12, et un outil MCP se déclare par un
`inputSchema` du même dialecte : la conversion est mécanique — le pari qui
engendre les formulaires RJSF (§7), appliqué une troisième fois. Les extensions
`x-ui` sont ignorées : elles parlent à un écran, pas à un agent. La description
de l'outil, elle, ne s'engendre pas : c'est du **prompt engineering** — en
anglais, à budget de jetons compté, avec un exemple d'appel dans le texte —
rédigé à la main pour les capacités exposées, comme leurs `title`.

**Le catalogue est éditorial, versionné dans le code, et petit.** Douze outils
par défaut, un par capacité promise : `text_to_speech`, `speech_to_text`,
`speaker_diarization`, `audio_separation`, `text_to_image`, `image_to_image`,
`image_to_text`, `image_upscale`, `image_matting`, `image_segment`,
`depth_estimation`, `time_series_forecast`. Plus trois méta-outils toujours
présents :

- `ecurie_catalog` — la découverte : les quarante et une capacités, les modèles
  installés, l'état de leurs profils ;
- `ecurie_run` — l'échappatoire : n'importe quelle capacité par son contrat,
  paramètres validés comme un `POST /jobs` ;
- `ecurie_status` — résidents (étrangers compris, §5.5), budget Metal, les
  trois chiffres du disque. En lecture seule.

**Un quatrième méta-outil est conçu, pas encore servi** :
`ecurie_fan_out(input, capabilities[])` — la même entrée envoyée à N capacités
du catalogue, N résultats retournés (tâche 1.7 du plan, opportuniste). Sa seule
nouveauté est dans l'admission : une **réservation groupée**, qui décide sur la
somme des pics des N candidats et refuse en bloc, avec les mêmes options
chiffrées qu'un refus simple — retirer une capacité de la liste en est une de
plus. Les sorties suivent la règle commune : des chemins et des ressources,
jamais des blobs. Le cas d'usage qui le motive — une caméra, plusieurs modèles
de vision co-résidents — met les `face-*` dans son périmètre par l'opt-in
`--tools faces`, sans toucher l'exclusion par défaut. La suite de l'idée —
sorties push hors bande, sources gérées déclaratives — est une fiche du backlog
(`registry/veille/BACKLOG.md` §E), post-v1 ; son prérequis, les réservations
durables, est une question ouverte (§13.7).

La contrainte dimensionnante est une mesure — la seule chose qui survive au
harnais dsh : **40 outils déclarés = 16 690 jetons de catalogue** et le choix
encore juste ; **67 = boucle de répétition**, sans que rien ne lève (relevé sur
`gemma4-12b-chat@4bit`, août 2026). Les cerveaux des clients MCP tiennent
mieux, mais le coût de contexte, lui, vaut pour tous : douze plus trois —
seize quand `ecurie_fan_out` entrera — laisse de la marge, et les familles
complètes sont un opt-in (`ecurie mcp --tools faces`, `--tools all`). Le
catalogue est une **liste versionnée dans `packages/mcp`**, pas un champ du
schéma du registre : les vingt-neuf capacités restantes ne changent pas d'un
octet de manifeste et restent découvrables par `ecurie_catalog`.

**Le champ `human_subject` ferme les deux portes, et il a fallu le trancher.**
Ce paragraphe portait jusqu'à J1 deux phrases qui se contredisaient : les
`face-*` exclues « application du champ `human_subject`, pas une opinion du
serveur », et les capacités restantes « exécutables par `ecurie_run` ». Prises
ensemble, elles vident le champ de son sens — l'échappatoire rouvrirait ce que le
catalogue ferme, et il suffirait à un agent de passer par `ecurie_run` pour
identifier quelqu'un. Le champ gagne : `ecurie_run` refuse lui aussi les sept
contrats qui le portent — les six `face-*` et `voice-clone` —, et l'opt-in les
rouvre des deux côtés d'un même geste. Une capacité qui identifie une personne ne
devient pas acceptable parce qu'elle est passée par une autre porte. Le refus
porte alors la commande à relayer à l'humain, comme tous les autres.

**Un refus d'admission est une donnée, pas un message.** La règle de la CLI —
chaque erreur porte la commande qui répare — se transpose : chaque refus porte
les options que l'agent peut exécuter, chiffrées à partir de la même décision
d'admission. L'éviction LRU des résidents ni épinglés ni occupés étant
automatique (§5.4), un refus ne survient que quand il ne reste rien à évincer —
et c'est exactement ce que le payload raconte :

```json
{
  "error": "admission_refused",
  "reason": "sdxl-base@bf16-mps ne tient pas : il manque 4,2 Gio …",
  "requested": {"capability": "text-to-image", "variant": "sdxl-base@bf16-mps",
                "peak_bytes": 14254080000},
  "budget_bytes": 19070000000,
  "residents": [
    {"variant": "qwen3-tts-1.7b@8bit-mlx", "peak_bytes": 3328599654, "pinned": true},
    {"variant": "moss-transcribe@8bit-mlx", "peak_bytes": 6442450944, "busy": true}
  ],
  "options": [
    {"kind": "retry", "when": "the job running on moss-transcribe@8bit-mlx ends",
     "frees_bytes": 6442450944},
    {"kind": "variant", "ref": "sdxl-base@8bit-mps", "peak_bytes": 8912000000,
     "fits_now": true},
    {"kind": "reduce_input", "parameter": "max_seconds", "max_admissible": 18},
    {"kind": "human_command", "command": "ecurie unload qwen3-tts-1.7b@8bit-mlx",
     "why": "pinned by its human — relay this, do not decide it"}
  ]
}
```

**Trois corrections que la réalisation de J1 a imposées à cet exemple**, et elles
disent chacune quelque chose du projet :

- **`basis` a disparu.** Les trois états de l'admission — mesuré-local,
  hérité-de-classe, borné — sont la tâche 2.3, au jalon J2. Aucun champ du code
  ne les porte aujourd'hui : `Profile` ne connaît que `measured_on`, le nom de la
  machine qui a mesuré. Écrire `"measured-local"` en dur aurait été faux pour
  quiconque récupère un profil mesuré ailleurs — exactement la faute que J0 a
  corrigée dans trois documents. Le champ **entrera avec 2.3**, pas avant ;
- **`when` nomme un variant, pas un job.** L'identité du job qui occupe un worker
  n'est pas publiée : `residents.json` ne porte que le pid de qui le tient, et le
  superviseur assume que « quel job précisément occupe le worker ne regarde que
  nous ». Le variant suffit à l'agent, qui ne peut de toute façon agir que sur
  l'attente ;
- **`reduce_input` ne porte pas sur `width`.** L'exemple d'origine réduisait une
  largeur d'image, ce qu'aucun variant du parc ne sait faire : les trois seuls
  `peak_scaling` mesurés portent sur une durée ou une longueur de contexte. Un
  modèle dont le pic ne dépend pas de l'entrée ne se réduit pas, et l'option
  n'est émise que là où une pente existe — **bornée à l'intervalle mesuré**,
  parce qu'extrapoler vers le bas serait admettre sur une estimation.

Une épingle est une préférence humaine : l'agent ne la lève pas, il transmet la
commande à qui de droit. Le refus MCP parle anglais — c'est la surface produit ;
la phrase française du §6.1 suivra la politique de langue du pivot.

**Les sorties sont des chemins et des ressources, jamais des blobs.** Une image
générée revient comme chemin + ressource MCP servie depuis
`/jobs/{id}/files` ; une transcription courte revient en texte, une longue en
fichier. Rien ne transite en base64 dans le contexte de l'agent — le contexte
est un budget, pas un tuyau. La progression des jobs longs passe par les
notifications de progression MCP, alimentées par le SSE existant : pas de
nouveau canal.

**Rien de destructif côté agent.** Le GC, la quarantaine, les suppressions et
le tiering restent des gestes de CLI humaine ; `ecurie_status` lit et ne touche
pas. Un agent qui sait générer une image n'a aucune raison de savoir vider un
cache — et un refus se négocie par les options qu'il transporte, jamais par un
pouvoir général de décharge.

---

## 7. `apps/ui` — rendu piloté par schéma

React + Vite + TypeScript + `react-jsonschema-form`. Aucun formulaire écrit à la main.

**Le socle est livré** (tâche 4.3) : `capability.input` part chez RJSF tel quel,
et tout ce que le front décide vit dans deux tables d'aiguillage, l'une pour les
widgets, l'autre pour les visualiseurs. Les deux sont **totales** — un `x-ui` ou
un type de média inconnu dégrade avec un avis, sans jamais lever ni se taire.
C'est ce qui rend « ajouter un modèle = ajouter un YAML » vérifiable : les 17
contrats du registre sont rendus par une suite qui les lit sur le disque, si bien
qu'un dix-huitième y entre sans qu'une ligne de front bouge.

**Le choix de la capacité n'est plus une liste** (tâche 4.8). Le `<select>`
groupé tenait sur dix-sept contrats ; à trente-deux, il fallait lire une liste de
titres pour en reconnaître un, et rien n'y disait ce qu'une capacité prend, ce
qu'elle rend, ni s'il y avait de quoi la lancer. Le panneau qui le remplace tient
sur quatre décisions.

**La glyphe dit le contrat.** Une forme, une flèche, une forme : l'entrée et la
sortie, qui sont exactement ce qu'une capacité déclare. Seize formes se
recombinent pour trente-deux capacités, donc des formes qu'on reconnaît d'une
carte à l'autre — une icône par capacité aurait demandé d'inventer un symbole
pour « débruitage » et un autre pour « séparation de pistes », que personne
n'aurait distingués. Le vocabulaire est plus fin que les types de média quand il
le faut : quatre capacités rendent « du JSON », et ce JSON est tour à tour des
boîtes, des points, un vecteur et des angles.

**Les filtres se déduisent, la catégorie s'écrit.** Ce qu'une capacité prend et
rend sort du JSON Schema d'entrée et d'`output_media_types` (`schema/modalites.ts`),
si bien qu'un contrat qui entre au registre demain est filtrable le jour même.
La catégorie, elle, ne se déduit pas : `image-detect` et `face-detect` prennent
une image et rendent des boîtes, et on ne les cherche pas dans le même geste.
Elle vit donc dans une table d'écran, avec un test qui refuse qu'une capacité du
registre n'y soit pas — une capacité neuve tombe dans « Divers » à l'écran plutôt
que de disparaître, et fait rougir la CI le lendemain.

**Le rangement par état devient une marque.** Grouper par famille et par état à
la fois donnerait douze sections ; l'état passe donc sur la carte, et à
l'intérieur d'une famille les exécutables viennent d'abord. Ce qui ne change pas :
**une capacité sans variant exécutable reste affichée.** Elle dit ce qu'un
`ecurie pull` rendrait possible, ce qui est la moitié de ce qu'un registre sert à
savoir.

**Douze contrats y ont gagné une description.** Le sélecteur montre une phrase
par capacité, et douze des trente-deux n'en avaient aucune — la carte était alors
un titre et deux chiffres. Elles sont écrites comme les vingt autres : ce que la
capacité fait, et ce qui la sépare de sa voisine la plus proche.

- **Extensions `x-ui`** mappées sur des widgets RJSF. Le méta-schéma en énumère
  **cinq**, pas trois — `textarea`, `select`, `file`, `slider`, `hidden` — et la
  table les couvre toutes. Deux ne sont pas celles qu'on croyait :
  - `select` est un **combobox**, jamais une liste fermée. Ses valeurs viennent
    du champ `options` d'un résident, c'est-à-dire de ce que le worker a annoncé
    au chargement : tant qu'un modèle n'a pas tourné une fois, on ne connaît pas
    ses voix, et `translation.target_language` — qui est requis — serait
    insaisissable. Il n'existe **pas** d'endpoint `/runtime/residents/{ref}/options` :
    la source est `GET /runtime/residents` ;
  - `file` rend un **chemin local**, pas une data-URL — et il l'obtient
    désormais de trois façons. Le `FileWidget` de RJSF encode le fichier choisi
    en base64 dans `formData`, alors que le contrat déclare un chemin que le
    worker ouvre sur le disque : c'est pourquoi le widget est remplacé, et cela
    ne change pas. Ce qui a changé est la conclusion qu'on en tirait. Le
    navigateur ne donne pas le chemin réel d'un fichier choisi — c'est vrai — mais
    **il en a le contenu**, et `POST /uploads` (§6) l'écrit puis rend le chemin
    créé. Le champ porte alors exactement ce qu'on aurait tapé. Trois sources,
    aucune de trop : le chemin saisi, qui reste la voie la plus rapide et la
    seule qui ne copie aucun octet ; un fichier du disque, par le sélecteur natif
    — qui n'est donc plus inerte ; la caméra ou le micro, pour ce qui n'existe
    pas encore du tout. Les deux dernières arrivent au même endroit : un `Blob`
    sans chemin. Le champ accepte aussi le **glisser-déposer** et le **collage**,
    qui sont le geste le plus direct pour prendre une image dans une page web —
    le navigateur la télécharge lui-même et la présente comme un fichier. Quand
    il ne donne qu'une URL, rien ne se passe : la suivre demanderait au serveur
    de sortir sur le réseau, ce qu'un parc local n'a aucune raison de faire. Un
    dépôt qui échoue **n'efface pas** le champ : ce qui s'y trouvait y était pour
    une raison, et une panne de réseau n'en est pas une de la perdre ;
  - un **`type: object` sans `properties`** n'est rendu par aucun widget — RJSF
    produit un fieldset vide. `tool-use.tools[].parameters`, requis, est dans ce
    cas : il reçoit un éditeur JSON, déclenché par la forme du champ et non par
    l'identifiant du contrat.
- **La caméra et le micro sont des sources d'entrée comme le disque.** Un champ
  qui accepte `image/*` propose la caméra, `audio/*` le micro, `video/*` les deux
  ensemble ; un champ qui n'accepte que `application/pdf` ne propose rien —
  aucun bouton plutôt qu'un bouton qui produirait un fichier refusé. La
  mécanique (`src/media/capture.ts`) est séparée du composant qui l'emploie,
  parce que **rien de tout cela n'existe dans jsdom** : `getUserMedia`,
  `MediaRecorder`, `AudioContext`, `canvas.toBlob` sont des accès au matériel, et
  les faire passer par un objet `Materiel` explicite est ce qui rend leur usage
  vérifiable sans caméra. Trois décisions valent d'être écrites :
  - **le son déposé est du WAV, jamais ce que le navigateur a enregistré.**
    Aucun navigateur n'écrit de WAV : Chrome rend de l'opus, Safari de l'AAC. Or
    le `pyproject.toml` de l'env `mlx-audio` le dit noir sur blanc — « ffmpeg…
    ne redeviendrait nécessaire que pour flac/mp3/ogg/opus ». Un dépôt en opus
    produirait un job qui échoue au décodage, plusieurs secondes après le clic.
    Le navigateur sait pourtant relire ce qu'il vient d'encoder — c'est le même
    moteur —, et de `decodeAudioData` au WAV il n'y a qu'un en-tête de 44 octets
    et une conversion, mixée en mono parce que tous les modèles audio du parc
    travaillent sur un canal ;
  - **rien ne s'allume avant le premier clic, et ce qui s'allume se referme.**
    `getUserMedia` fait apparaître une autorisation système : la réclamer parce
    qu'un champ accepte une image serait insupportable. À l'autre bout, une
    caméra qu'on oublie de fermer est la seule faute d'ici qui survive à
    l'écran — changer de mode, fermer le panneau, démonter l'écran et échouer à
    déposer coupent tous les pistes ;
  - **un refus d'autorisation est une phrase, pas un silence.**
    `NotAllowedError` et `NotFoundError` appellent deux gestes différents — lever
    l'interdiction dans le navigateur, ou constater qu'il n'y a pas de
    périphérique —, et un « échec de la caméra » unique les confondrait.
- **Visualiseurs de sortie** par `contentMediaType` : `audio/*` → lecteur,
  `image/*` → image + zoom, `video/*` → vidéo, `model/gltf-binary` →
  `<model-viewer>` (Google, autonome, pas de scène three.js à écrire), `text/*` →
  texte, **`application/json` → arbre**. Cette sixième ligne manquait : trois
  contrats en produisent, dont `tool-use.calls`, qui est une sortie **requise**.
  Table d'aiguillage unique, un composant par type, plus un repli qui montre
  toujours le chemin et le type — une zone blanche est le seul échec que
  l'utilisateur ne peut pas diagnostiquer. Le diff de l'OCR n'est pas ici : un
  diff suppose deux sorties, il appartient à la Confrontation (5.4).
  L'aplatissement parcourt la **réponse** et non les `properties` du contrat,
  parce qu'`audio-separation` déclare cinq pistes et n'en produit que deux ou
  quatre selon `stems`.
- **Quatre écrans**, plafond ferme (§9 de l'architecture) : Atelier, Confrontation,
  Parc, Bibliothèque. **Les deux premiers sont livrés.** Le second est le Parc
  (tâche 4.5), et c'est lui qui a donné sa navigation à la coquille — un onglet
  unique était un décor, à deux il en faut une. Elle tient en un `useState` et
  deux boutons : ni routeur, ni URL. Ce que cela coûte est réel et se dit —
  recharger la page revient à l'Atelier, un écran ne se met pas en signet — et la
  question se reposera au quatrième, quand un lien vers un job précis de la
  Bibliothèque commencera à valoir quelque chose. Les boutons ne sont pas non
  plus des `tab` ARIA, bien que la forme y ressemble : le motif `tablist` engage
  à une navigation par les flèches avec un `tabindex` mobile, et le déclarer sans
  le tenir vaut moins que de ne pas le déclarer.

  **L'écran qu'on quitte est démonté, pas caché** : le Parc classe tout le parc
  par contenu à chaque lecture et l'Atelier sonde la mémoire toutes les deux
  secondes ; les garder tous deux montés ferait payer en permanence celui qu'on
  ne regarde pas.

  Le Parc porte les trois chiffres, l'arbre de duplication, le plan de GC et le
  tiering, et trois décisions le gouvernent :
  - **il ne sonde pas.** Le bandeau de ressources se rafraîchit parce que la
    mémoire bouge sans qu'on touche à l'écran ; le disque observé, lui, ne bouge
    qu'après un `ecurie store scan`, qui est une commande qu'on tape. Un bouton
    *Relire* remplace le sondage ;
  - **rien n'y touche au disque**, et c'est un choix et non une étape suivante —
    les raisons sont au §6, elles tiennent au §4.3 et au §4.4 ;
  - **trois lectures indépendantes**, chacune affichant son propre échec : un
    parc sans volume de tiering déclaré n'a aucune raison de perdre ses trois
    chiffres.

  Le bandeau de ressources n'y figure pas, bien qu'il soit « global par sa
  nature » : il parle de mémoire unifiée quand cet écran parle de disque. Les
  deux ne se comptent même pas dans la même unité — **Gio binaires pour le budget
  Metal, Go décimaux pour le disque**, comme la CLI, parce qu'un disque s'annonce
  et s'affiche en puissances de dix. Un écran Parc qui dirait « 4,56 Gio » de ce
  que `ecurie store status` appelle 4,90 Go ferait douter du chiffre plutôt que
  de l'unité, et la tâche demandait la parité avec la CLI.

  **Le premier exécute** (tâche 4.4) :
  `src/ecrans/Atelier.tsx` remplace le banc de rendu, avec le choix de la capacité
  groupé par ce qui marche, le variant préselectionné sur le titulaire exécutable,
  le formulaire engendré, le chiffrage de l'entrée, le bouton *Lancer*, la
  progression en direct et la sortie réelle. `App.tsx` reste une coquille : elle
  ne porte qu'un état d'écran, et n'en portera pas d'autre.
  Trois décisions gouvernent le lancement, et aucune n'était au plan :
  - **le bouton n'est jamais grisé pour un variant qu'on croit incapable.** « Ce
    morceau de 30 s demanderait 24,2 Gio » vaut mieux qu'un bouton mort ; un
    variant dont les poids manquent part quand même, le serveur refuse en 409, et
    l'écran affiche la commande qui répare. Il ne se grise que pendant qu'un job
    de cet écran tourne, ce qui n'est pas un jugement mais un fait affiché juste
    en dessous ;
  - **un écran, un job.** Le serveur en accepte seize et les sérialise par
    variant ; l'écran n'en suit qu'un, faute d'une seconde zone de sortie où
    montrer le suivant. Ce que cela coûte est ce que la Bibliothèque (5.5) rendra
    visible ; en attendant, `ecurie run` reste là ;
  - **changer de capacité retire le job de l'écran.** Sa sortie s'aiguille sur les
    `output_media_types` du contrat qui l'a produit : la garder l'aplatirait avec
    la mauvaise table. Rien n'est perdu — le dossier du job est sur le disque.
- **Le flux d'événements est lu par `fetch`, non par `EventSource`.** La
  conception nommait le second, et c'est pour lui que le serveur termine par un
  `end` ; deux raisons l'ont écarté au moment d'écrire le client. Il n'existe pas
  en jsdom, si bien que toute la suite tournerait sur un double écrit pour
  l'occasion — on éprouverait le double. Et surtout il n'est pas `fetch` : le
  double de `vitest.setup.ts` refuse toute route non déclarée, précisément pour
  qu'un test ne parte pas frapper le `ecurie serve` qui tourne sur cette machine,
  et un `EventSource` ouvrirait une vraie connexion sans passer par ce filet. Ce
  qu'on perd est ce qu'on ne voulait pas — la reconnexion automatique, qui rouvre
  une connexion sur un job terminé ; ce qu'on gagne est l'annulation par
  `AbortSignal` et un 404 qui arrive comme une erreur lisible. Le `end` garde tout
  son sens : il dit au client qu'il n'y a plus rien à attendre.
- **Le bandeau de ressources** (mémoire résidente, budget restant, « lancer
  déchargera X ») est alimenté par `GET /runtime/residents?for=<ref>`, rafraîchi
  toutes les deux secondes. Il est global par sa nature — n'importe quel écran peut
  le poser — et **pas par sa place dans l'arbre** : le chiffre qui compte est celui
  du variant sélectionné, et le hisser dans la coquille y ferait remonter l'état de
  l'écran. C'est **l'écran qui tient le sondage** et le lui passe, parce que la
  même réponse sert deux fois : les chiffres du bandeau, et les `x-options-from`
  du formulaire, dont la seule source est le champ `options` d'un worker chargé.
  Deux sondages sur la même route doubleraient les requêtes, et une lecture unique
  au montage figerait les voix à « aucune » pour toute la session — y compris après
  un `ecurie run` lancé dans un terminal. Trois décisions gouvernent le sondage, et
  aucune ne se lit dans l'appel :
  on replanifie **après la réponse** (un `setInterval` de 2 s sur un serveur qui
  répond en 3 s empile les requêtes) ; un échec **n'efface pas** les derniers
  chiffres, qui restent affichés datés de l'heure où ils étaient vrais ; et un
  onglet caché **ne sonde pas** — à deux secondes, un onglet laissé ouvert une
  journée fait quarante mille requêtes dont chacune vérifie l'existence de processus.

**La feuille de style est devenue un langage visuel, et le bandeau une barre de
stalles.** Celle du 4.3 ne visait rien de plus que « rendre lisible le formulaire
engendré », et tenait la promesse qui allait avec — ne pas laisser un choix pris
pour un formulaire dicter le langage visuel de quatre écrans. Une fois les deux
écrans montés, ce qu'elle produisait était un document : titres, puces et notes
grises dans une plage de deux dixièmes de rem, sans une surface, sans une couleur
qui décide. On y lisait « lancer refusé » du même œil que « mesuré le
20/08/2026 ». Ce que la refonte change, et qui n'est pas cosmétique :

- **L'Atelier passe à deux colonnes** au-delà de 68 rem — la composition à
  gauche, le box à droite, collant. C'est la réponse à une gêne que le code du
  bandeau nommait sans pouvoir la traiter : « visible pendant qu'on remplit un
  formulaire long, le contrat de `tool-use` fait défiler bien au-delà d'un
  écran ». Un bandeau en haut de page ne le tenait qu'à moitié. L'ordre du
  balisage — choisir, remplir, lancer — ne change pas d'une largeur à l'autre :
  en une colonne le box retombe sous le formulaire, là où l'on attend un bouton
  d'envoi, si bien qu'aucun `order` CSS ne sépare le regard de la tabulation.
  Ce n'est pas la première marche vers un clone de ComfyUI : deux colonnes de
  document ne sont pas un canevas de nœuds, et le plafond de quatre écrans est
  intact.
- **La jauge devient un rang de boxes.** Une part unique et grise répondait à
  « c'est plein aux deux tiers » et à rien d'autre, alors que la question de
  l'écran est **« que faudra-t-il décharger »** — et la réponse tient dans la
  répartition, pas dans le total. Le rail porte une plaque par résident, à sa
  largeur réelle, et la part de l'arrivant hachurée à la suite : les hachures
  distinguent un fait d'une hypothèse. Quand la demande dépasse le budget,
  **l'échelle s'étend** à `occupé + demandé` et un repère marque où tombe le
  budget ; une jauge bornée à 100 % répondait « plein » aussi bien à un
  dépassement de 200 Mio qu'à un dépassement de douze gigaoctets, et ces deux
  situations n'appellent pas le même geste. Le calcul vit dans
  `src/ressources/stalles.ts`, testé sans rendre un composant, comme tout ce qui
  décide dans ce front. `used_bytes` valant exactement `Σ peak_bytes` côté
  serveur, les segments couvrent l'occupé sans reste ni arrondi à cacher.
- **Une couleur par sens, et jamais une couleur seule.** Le laiton est la matière
  occupée et l'action, le pré est ce qui tient, la brique est ce qui refuse. Il
  n'y a **pas de bleu** — c'est la couleur par défaut de toutes les interfaces,
  et une écurie n'en a pas. Chaque état porte aussi son mot, ce qui était déjà la
  règle des modules qui composent les phrases : une stalle nomme son occupant,
  son poids et son état dans la légende, et le repère du budget a perdu son
  étiquette parce qu'elle tombait pile sur le nom peint de la plaque qu'elle
  coupe.
- **Aucune dépendance n'est arrivée avec elle** — ni thème RJSF, ni police
  distante. Une police servie par un tiers sur un outil qui gère des modèles hors
  ligne casserait la page le jour où l'on travaille sans réseau, ce qui est
  précisément le jour où cet outil sert.

Trois défauts ne se voyaient qu'à l'écran, et aucun test ne pouvait les
attraper : le nom peint sur une plaque hachurée devenait illisible une lettre sur
deux tant que les hachures allaient vers le papier plutôt que vers l'encre ; RJSF
émet le champ booléen à l'envers de tous ses autres champs, description d'abord
et nom ensuite, si bien qu'on lisait « Horodater chaque mot » sans savoir de quoi
cela parlait ; et le sélecteur de fichier natif annonce lui-même « Choisir un
fichier », doublant le libellé du composant d'une seconde invitation tronquée au
milieu d'un mot. C'est la même leçon que le reste du dépôt : une capture d'écran
montre en une seconde ce qu'une suite qui cherche du texte par sous-chaîne ne
verra jamais.

**Le quatrième n'était pas cosmétique : la caméra ne montrait rien.** Le viseur
restait vide et *Prendre la photo* répondait « la caméra n'a pas encore envoyé
d'image », alors que le matériel s'ouvrait bel et bien — diode comprise. La cause
est un branchement posé depuis un `queueMicrotask` : `ouvrir` reprend la main
après son `await`, hors du geste de l'utilisateur, si bien que le rendu du
`<video>` est planifié par l'ordonnanceur de React — une macrotâche — quand la
microtâche, elle, s'exécute tout de suite. `videoRef.current` valait `null`, et
`srcObject` partait dans le vide sans une erreur. Le flux se branche désormais
dans un **effet**, qui ne tourne qu'après le commit. Deux enseignements : un
`useRef` sur un élément conditionnel n'est jamais garanti au retour d'un `await`,
et **vérifier qu'on a appelé `getUserMedia` ne prouve rien** — c'est l'élément
qui doit porter le flux, ce que la suite garde maintenant en lisant `srcObject`.
Le défaut a survécu à la tâche qui l'a livré parce que jsdom n'a pas de caméra ;
il s'éprouve avec la mire factice de Chrome
(`--use-fake-device-for-media-stream`), comme un vrai `ecurie serve` éprouve le
reste du front.

Le fond du viseur est noir dans les deux thèmes, et non `--encre` : en thème
sombre l'encre est claire, et le viseur virait au crème — un flux pas encore
arrivé y ressemblait à une panne.

**Le typage vient du serveur.** `tools/openapi_dump.py` fige le schéma OpenAPI
dans `apps/ui/src/api/openapi.json`, dont `openapi-typescript` engendre les
types ; `tools/ui_fixtures.py` capture trois réponses du vrai registre pour la
suite de tests. Les cinq fichiers sont committés, et **deux tests pytest** les
gardent — dans la suite que lance celui qui édite `packages/api` ou `registry/`,
pas dans une suite front qu'il n'ouvrirait pas. Ce n'est pas une précaution
théorique : les fixtures ont divergé du registre en une demi-heure, la première
fois, sans que rien ne le dise.

Ce que le socle a coûté en surprises, toutes découvertes en exécutant plutôt
qu'en relisant : les avis de compilation remontés pendant le rendu figeaient le
navigateur dans une boucle infinie ; l'éditeur JSON renvoyait le curseur en fin
de texte dès que la saisie devenait analysable, rendant impossible toute frappe à
l'intérieur d'un objet ; et une entrée de la table qui aurait nommé un widget
inexistant serait passée à travers le typage sans qu'aucun test ne bronche.

L'Atelier en a coûté deux de plus, l'une et l'autre invisibles en jsdom.

**Le même chiffre s'affichait deux fois.** Le bandeau chiffre le *variant* par
`?for=`, le bouton chiffre l'*entrée* par `POST /runtime/admission` ; comme douze
variants sur treize ont un pic qui ne dépend pas de l'entrée, les deux rendent
mot pour mot la même phrase, dans le même écran. Les fixtures ne le montraient
pas — elles ne remplissaient pas `admission`. L'intitulé « Pour l'entrée saisie »
sépare les deux, mais la leçon porte plus loin : la tâche **4.7 n'est pas un
raffinement du bandeau**, c'est ce qui rend le second chiffre utile.

**Garder les dernières données devient faux quand l'écran change de sujet.**
`useResource` ne vide pas ses données pendant qu'il recharge, pour ne pas faire
clignoter l'écran ; entre le clic sur une nouvelle capacité et l'arrivée de ses
modèles, la liste des variants est donc encore celle de la précédente. La
préselection y cherchait une référence de la bonne capacité, ne la trouvait pas,
et posait un formulaire sans les défauts du manifeste — `voice` vide là où
`qwen3-tts-1.7b` déclare `serena`. Le correctif est de filtrer sur `capability` :
ne jamais faire confiance à une donnée qui n'a pas encore été rechargée.

Brancher le bouton *Lancer* en a coûté quatre autres, et la première touche le
serveur plutôt que le front — c'est le propre d'un client : il découvre ce que
l'API ne dit pas.

**Le flux pouvait sauter son dernier événement.** `_flux` lisait le journal, puis
demandait si le job était terminé ; entre les deux, le fil du job avait le temps
d'exécuter `finish()` en entier — dernier événement compris. La lecture rendait
alors « rien de neuf », le test de terminaison concluait « plus rien à venir », et
le `end` partait sans que l'état final soit jamais passé. Aucun test serveur ne
pouvait le voir : ils lisent le flux jusqu'au bout et trouvent bien l'état final
dans l'avant-dernière trame, en régime non concurrent. C'est l'écran qui en aurait
payé le prix — barre figée à 40 %, bouton *Lancer* grisé, sortie jamais montrée —
et c'est en écrivant l'écran qu'on l'a cherché. Lire l'état terminal **avant** le
journal suffit, et l'inversion est sûre : `finish()` pose l'état puis émet, tous
deux sous le verrou.

**Une commande de réparation voyage maintenant dans une erreur**, ce que
`errors.ts` affirmait impossible. C'était vrai tant qu'on ne faisait que lire :
un variant non exécutable était un état, et ses blockers arrivaient dans une
réponse 200. Le demander à `POST /jobs` en fait un refus, et le 409 porte alors
un `detail` **objet** — une phrase et une liste. `JSON.stringify` le rendait
illisible au moment précis où il dit quoi taper.

**Un test écrit pour la forme a trouvé un vrai défaut.** L'analyseur du flux
cherchait `\n\n` ; un serveur qui écrirait CRLF émet `\r\n\r\n`, où cette
recherche ne trouve rien. Il n'aurait rendu **aucun** événement, sans une erreur
pour le dire — l'écran serait resté figé sur « en file » pendant que le job
finissait. Ce serveur-ci écrit `\n` ; la leçon est que l'analyse d'un format
n'est juste que pour le producteur sur lequel on l'a essayée.

**Un essai réel change la machine qu'il éprouve.** Le job réel de
`App.reel.test.tsx` charge le modèle et le laisse résident ; le test d'admission
du même fichier, écrit au 4.4, attendait « lancer chargera 7,65 Gio » et lit
désormais « déjà résident : lancer ne le rechargera pas ». Il passait au premier
lancement et échouait au second, sans qu'une ligne ait bougé. Cela vaut pour les
cinq essais réels de pytest comme pour celui-ci : **l'ordre des tests et l'état
du parc font partie de leurs entrées.**

**Une requête en vol n'a pas de bouton d'annulation, elle a une génération.**
`AbortController` coupe le flux, mais deux requêtes courtes lui échappent — le
`POST` de la soumission et le `GET` de la reprise. Les laisser poser leur
résultat au retour ouvrait deux impasses : un job soumis puis oublié — l'écran
change de capacité pendant que le `POST` vole — revenait bloquer *Lancer*
**sans** son panneau, donc sans le bouton qui l'aurait retiré ; et un job retiré
pendant une reprise réapparaissait tout seul. Le chiffrage avait la même faille
là où le bandeau avait déjà sa garde. La règle vaut pour tout l'écran : **une
réponse qui revient doit prouver qu'on lui a posé la question la plus récente.**

Le Parc en a coûté trois autres, et deux ne se voyaient que sur le vrai disque.

**Le front n'avait qu'une unité, et elle était fausse pour la moitié de ce
qu'il affiche.** `formatBytes` rend des Gio binaires, ce qui est juste pour tout
ce que l'UI comptait jusqu'ici — le budget Metal, un pic, un seuil de lourdeur.
`ecurie_store.figures.fmt_bytes`, lui, rend des Go décimaux, et ce n'est pas une
négligence de la CLI : un disque s'annonce, se vend et s'affiche dans le Finder
en puissances de dix. Réutiliser la fonction existante aurait affiché « 43,4 Gio »
là où `ecurie store status` dit « 46,58 Go » — pour les mêmes octets, dans un
écran dont la tâche demandait la parité avec la CLI. Deux fonctions, donc, et la
règle est que **l'unité suit ce qu'on compte, pas le composant qui l'affiche**.

**Le parc réel a déplacé le sujet de l'écran.** Sur la machine de référence :
46,58 Go apparents, 11,4 Mo récupérables. Rapporté au total, le plan de GC ne
propose rien — et c'est une bonne nouvelle qu'aucune fixture n'aurait donnée. Ce
que le même écran révèle, en revanche, est autrement utile : **14,29 Go, tout
Ollama, ne sont rattachés à aucun variant du registre**, et 46,56 Go sur 46,58
portent un hash *annoncé par leur gestionnaire et jamais relu*. Le Parc n'est
donc pas d'abord un outil de nettoyage mais un outil de **connaissance** — ce que
le disque contient, et ce qu'on n'en sait pas.

**D'où la case à cocher plutôt qu'une option de CLI.** `--verified-only` avait
l'air d'un réglage d'expert ; sur ce parc, elle ramène le gain proposé de
11,4 Mo à zéro, parce que l'unique duplication trouvée repose sur un nom de blob
et non sur un contenu relu. Ce n'est pas un raffinement, c'est la différence
entre « voici ce qu'on peut reprendre » et « voici ce qu'on croit pouvoir
reprendre ». Une décision de cette portée doit être visible et réversible d'un
clic dans l'écran qui affiche le chiffre, pas enfouie dans un `--help`.

**Une capture d'écran a trouvé deux défauts que quatre cent cinq tests
laissaient passer**, et les deux tiennent au même geste. Les phrases du serveur
et du front étaient écrites avec des accents graves autour des commandes — la
convention des docstrings de ce dépôt —, et le navigateur les affichait tels
quels au milieu de phrases françaises. Aucun test ne pouvait le voir : tous
cherchent le texte par sous-chaîne, et « `ecurie store verify` » contient
« ecurie store verify ». La règle est désormais explicite des deux côtés — les
commandes s'écrivent en `<code>` dans le front, en clair dans les chaînes du
serveur, comme les blockers le font depuis le 4.4 — et deux tests la gardent, le
second en interrogeant le texte rendu de l'écran entier. Second défaut, trouvé
sur la même image : les motifs d'**écart** du plan s'affichaient sous leur clé
brute (« sans-sha256 ») faute d'entrée dans `REASON_LABELS`, qui ne nommait que
les motifs d'action. Deux lignes côté serveur, et l'écran hérite de la
traduction.

La leçon dépasse ces deux-là : **une suite de tests vérifie ce qu'un écran dit,
pas ce qu'il montre.** Regarder la page une fois a coûté une minute et rapporté
plus que la relecture du diff.

---

### Ce que huit capacités ajoutées d'un coup ont appris

Le 22 août 2026, le parc passe de dix-sept à vingt-cinq capacités déclarées. Le
détail est au plan ; trois points touchent la conception elle-même.

**Un runtime est aussi un catalogue qu'on n'a pas lu.** Trois des huit capacités
sont servies par des modèles que le venv de `mlx-audio` embarquait déjà. Le §10
décrit une veille qui balaye Hugging Face et les dépôts amont ; il lui manque une
phase : lire ce que les bibliothèques synchronisées savent faire. Cinq capacités
sur huit n'ont demandé aucun octet de plus ou moins de 400 Mo.

**Le troisième moteur d'inférence est arrivé, et il change une règle.** `rtmlib`
sert des modèles ONNX sur CoreML ou sur le CPU : c'est le premier worker du parc
sans mémoire Metal, donc le seul dont le RSS mesure honnêtement le pic. Le §5.2
disait « le RSS ne compte pas la mémoire Metal » ; il faut lire « sauf quand il
n'y en a pas ».

**Une capacité peut refuser une de ses propres sorties.** `video-to-motion`
déclare un BVH facultatif, et son unique variant fait échouer le job quand on le
demande : aucun modèle de sa chaîne ne rend de rotations. Ce n'est pas un défaut
du contrat — c'est ce qu'un contrat de capacité doit permettre, un variant qui
n'honore qu'une partie de ce que d'autres honoreront. La même mécanique sert à
`speaker-diarization`, dont deux paramètres restent inopérants sur son variant.

---

### Ce qu'une couverture complète du registre a appris

Le 22 août 2026, dans la même journée, les six dernières capacités sans modèle en
reçoivent un — `audio-denoise`, `audio-separation`, `image-to-image`,
`image-to-video`, `speech-to-text`, `text-to-video`. **Les vingt-cinq contrats
ont désormais au moins un manifeste**, et un test du registre réel l'exige
(`test_chaque_capacite_a_au_moins_un_modele`). Trois points touchent la
conception.

**Un contrat sans modèle coûte plus qu'il n'apporte.** L'argument inverse tenait
et il est resté écrit ailleurs : une capacité déclarée dit ce que le parc
*pourrait* faire, et l'Atelier lui réservait un groupe. Ce qu'elle coûte se voit
à l'usage — un formulaire complet dont aucun bouton *Lancer* ne peut partir, et
rien à l'écran qui dise quel modèle irait là. Le registre est aussi une liste de
courses ; une case vide n'en est pas une. L'état `sans-modèle` reste dans le code
du front, et ce n'est pas du code mort : un contrat s'ajoute avant son modèle, et
c'est l'ordre normal du travail.

**Le partage de poids a servi une quatrième et une cinquième fois, et c'est
devenu le patron le plus rentable du parc.** `sdxl-base-img2img` et
`moss-transcribe` sont exécutables **le jour où ils entrent au registre**, sans
un octet téléchargé : mêmes dépôts, mêmes révisions, mêmes caches que
`sdxl-base` et `moss-transcribe-diarize`, un autre pipeline et une autre lecture
du même résultat. Le §5.2 le disait pour deux capacités ; il vaut pour cinq
couples. La question à poser devant chaque capacité vide n'est donc pas « quel
modèle télécharger » mais « lequel des poids déjà là sait le faire ».

**Une capacité peut entrer au registre en sachant qu'elle ne tiendra pas.**
`ltx-video-2b` pèse 15,2 Go en bf16 pour 17,76 Gio de budget : le calcul est fait
avant le téléchargement, écrit dans les caveats, et le contrôle d'admission
refusera. Le manifeste existe quand même, et c'est délibéré — un modèle candidat
avec son pic annoncé apprend exactement où est le mur, là qu'une capacité vide
n'apprenait rien. Sur les vingt-cinq capacités, vingt sont exécutables ; les cinq
autres disent ce qu'il faudrait pour qu'elles le deviennent.

---

### Ce que la famille visage a appris

Le 24 août 2026, six capacités entrent d'un coup — `face-detect`,
`face-landmark`, `face-parse`, `face-embed`, `face-headpose`, `face-gaze` — sur
un runtime neuf. Le détail est au rapport (`registry/veille/2026-08-24/`) ;
quatre points touchent la conception.

**Le registre savait dire ce que le droit interdit, pas ce que la capacité
fait.** `license_class` couvre la licence ; il ne dit rien de ce qu'un modèle
parfaitement en règle permet de faire à quelqu'un. D'où `human_subject` au
contrat (§3). La preuve que le champ manquait vraiment n'est pas dans la famille
qui l'a fait naître : c'est `voice-clone`, au parc depuis le v0.3, qui fabrique
la voix d'une personne réelle et n'avait aucun moyen de le déclarer.

**Une capacité peut en exiger une autre en amont, et le registre ne sait pas
encore le dire.** Cinq de ces six capacités chargent un détecteur avant leur
propre réseau, parce qu'aucune ne cherche les visages qu'elle traite. Aujourd'hui
c'est `options.detector` au manifeste, donc une convention de runtime que rien ne
valide. Le §11 esquisse la composition pour `text-to-mesh` ; ce cas-ci est plus
faible — pas un enchaînement de jobs, une dépendance de chargement — et il
mériterait d'être déclaré plutôt que conventionnel.

**Un dépôt de poids peut servir soixante-quatre modèles, et la comptabilité
disque le supposait unique.** `yakhyo/uniface-weights` a fait déclarer 595 Mo à
un variant qui en pèse 6,6 : `_tree_bytes` mesurait l'instantané entier. Le
manifeste disait pourtant déjà ce qu'il voulait, par les `allow_patterns` sur
lesquels `pull` télécharge. Corrigé en réemployant le filtre de
`huggingface_hub` — deux filtres pour une seule question finiraient par diverger.

**Une charge type peut porter une vérité terrain que la photographie ne donne
pas.** Les visages de `assets/visages-groupe.png` sont calculés, d'abord parce
qu'on ne committe pas le portrait de quelqu'un pour dix ans. Mais comme c'est la
recette qui pose leurs angles, `face-headpose` s'y vérifie contre des lacets
connus — 0°, +24°, −18°, +8° — sans annotation manuelle. Une charge synthétique
n'est pas seulement un pis-aller juridique ; elle sait des choses de son contenu
qu'aucune photo annotée après coup ne saurait.

---

### Ce que les capacités de mesure ont appris

Le 24 août 2026, neuf capacités entrent d'un coup — `time-series-forecast`,
`audio-align`, `image-embed`, `geo-segment`, `geo-embed`, `protein-embed`,
`pointcloud-to-cad`, `multiview-to-3d`, `robot-action` — sur cinq runtimes neufs.
Elles n'ont pas de modalité commune : une série de nombres, un texte à horodater,
une image, une scène satellite à six bandes, une séquence protéique, un nuage de
points, N photos, un état de robot. Ce qui les rassemble est ce qu'elles
**rendent** : une mesure, une prévision, une géométrie, un programme, une action
— jamais du contenu. Le détail est au rapport
(`registry/veille/2026-08-24-mesure/`) ; six points touchent la conception.

**Le modèle de données ne savait pas dire qu'un variant a besoin de deux
dépôts.** Deux capacités l'ont demandé le même jour et pour des raisons
indépendantes : CAD-Recode publie 3,09 Go de poids sans le moindre tokenizer —
il faut celui de Qwen2-1.5B, sous une autre licence — et SmolVLA charge une
dorsale visuelle publiée à part. Les contourner coûtait plus cher que le champ :
un `pull` qui ne ramène qu'une moitié laisse un variant `tier: hot` qui échoue au
chargement, et un second manifeste pour le seul tokenizer déclarerait une
capacité que personne ne peut servir. D'où `extra_sources`, chacune portant un
`role` sous lequel le worker retrouve son chemin. C'est le troisième ajout au
modèle de données venu de l'usage et non du plan, après `runtime_env` et le
profil paramétré — et le troisième à n'avoir été ni prévu ni prévisible.

**Un contrat peut avoir besoin de N fichiers, et trois endroits l'ignoraient
chacun à leur façon.** `multiview-to-3d` reçoit entre deux et trente-deux photos.
Le type de média d'un tableau se déclare sur `items`, ce que ni
`input_media_types`, ni `stage_inputs`, ni `bench._resolve_case` ne regardaient :
la liste était invisible des trois, et le worker recevait des chemins relatifs à
un dossier qu'il n'a jamais vu. La correction a tenu en une fonction partagée —
`CapabilityContract.list_fields()` — parce que la quatrième lecture divergente
était garantie. Deux détails s'y sont ajoutés à la mesure : le rang préfixe le
nom du fichier copié (deux vues peuvent s'appeler `image.png`), et l'empreinte
porte sur la liste **dans l'ordre reçu**, l'ordre des vues changeant la
reconstruction.

**Un `peak_scaling` peut suivre une cardinalité, pas seulement un nombre saisi.**
Le coût de `multiview-to-3d` suit le nombre de vues, qui n'est écrit dans aucun
champ : c'est la taille du champ. Le banc acceptait pourtant le nom du paramètre
et ne produisait aucune pente, **sans un mot** — le pire des deux comportements.
Une liste compte désormais pour sa longueur, des deux côtés (`fit_peak_scaling`
et `expected_peak`), sans quoi l'admission réservait 11,77 Go à un job de deux
vues qui en coûte 4,43.

**Le §8 disait « MLX expose un pic exact, PyTorch/MPS non » ; il faut y ajouter
que le RSS et le pilote Metal se disputent la première place selon le modèle.**
Trois cas mesurés le même jour, trois réponses : `smolvla-libero` a un RSS de
3,20 Gio contre 1,36 au pilote — inscrire le seul chiffre Metal sous-estimerait
de moitié ; `prithvi-sen1floods11` bascule au milieu de sa propre charge, le RSS
menant à 384 et 576 pixels, Metal passant devant à 768 ; `da3-large` était
inscrit sur son seul RSS et **sous-déclarait de 2,79 Go, soit 42,6 % du vrai
chiffre**. La règle qui tient est le maximum des deux, relevé à chaque appel — et
le symptôme d'un instrument aveugle est reconnaissable : une pente nulle avec un
R² de 1,0, c'est-à-dire une consommation qui ne bouge pas quand l'entrée
quadruple.

**Le vrai risque d'une capacité de mesure n'est pas qu'elle échoue, c'est qu'elle
réussisse à côté.** Aucun des neuf adaptateurs n'a produit d'exception sur son
mode de panne principal. `AutoModel` initialise **au hasard** un pooler absent du
checkpoint d'ESM-2 : deux chargements des mêmes octets donnent des vecteurs à
−0,038 de cosinus, et rien n'échoue. Le checkpoint de Prithvi porte ses 381 clés
sous un préfixe `model.` : chargé sans le dépouiller, le réseau tourne et rend du
bruit. Transformers ≥ 5 fait produire du charabia à CAD-Recode par un calcul de
positions sur un masque qui porte des −1. L'aligneur rend des horodatages
plausibles sur un enregistrement qu'il n'a pas compris. Et une prévision demandée
au quantile 0,001 revient étiquetée « 0.001 » avec la valeur du quantile 0,01. Le
§8 dit que le banc mesure un coût et non une qualité ; le 22 août on lui a ajouté
qu'il ne regarde pas la forme. Il faut maintenant dire ce qui en découle : sur
ces capacités, **la seule garde est un job réel dont on a lu la sortie**, et
plusieurs adaptateurs portent désormais leur propre sonde — `span_seconds` pour
l'alignement, `out_of_domain` pour les commandes de robot, le contrôle 0/0 des
clés de poids pour Prithvi.

**Une licence peut décider d'une architecture.** CAD-Recode est sous CC BY-NC
4.0, et pas seulement ses poids : le `LICENSE.md` du dépôt amont couvre les cent
quatorze lignes de code que l'adaptateur devait employer. Aucune n'est entrée
dans ce dépôt — le patron de `hunyuan3d` a servi une seconde fois : le code amont
est copié à la main dans `runtimes/<env>/vendor/`, non versionné, et l'env porte
un README que `ecurie env list` signale. La différence avec le premier cas est
que le motif est ici juridique et non technique, ce qui ne change rien à la
mécanique. Le manifeste suit le précédent d'`arcface` : `research-only`, inscrit
comme référence et non comme titulaire.

---

## 8. Banc d'essai

`ecurie bench <model>@<variant>` :

1. Vérifie que le variant est téléchargé (sinon propose `ecurie pull`, qui télécharge
   à la révision épinglée avec `allow_patterns`).
2. Décharge tout le parc, lance le worker en mode mesure.
3. Mesure : `warmup_ms` (load → loaded), pic mémoire, `disk_bytes`, débit, latence
   et `rtf` sur la **charge type de la capacité** — trois entrées fixes,
   versionnées dans `registry/evals/bench/<capability>.json` avec leurs éventuels
   fichiers sous `assets/`. Append-only, comme les golden sets : une charge
   modifiée détruit la comparabilité de toutes les mesures antérieures.
4. Ajuste, si la charge déclare un `scaling_parameter`, la **pente du pic** par
   moindres carrés sur ses points, et rend le R² avec. Sous 0,9, la pente est
   jetée et le profil garde le pire cas : une droite ajustée sur une relation qui
   n'en est pas une vaut moins que rien.
5. Écrit `registry/measurements/<id>@<variant>/<machine>.json` avec
   `measured_on`, `harness_version`, et affiche le patch `profile:` à committer
   dans le YAML. Les fichiers de mesure sont l'autorité ; le bloc `profile` du
   manifeste en est la copie committée par un humain. **Un fichier par machine** :
   le dépôt est partagé et les Macs ne le sont pas, et un emplacement unique
   faisait s'écraser les relevés de deux postes. Le nom du fichier ne retient que
   le matériel, si bien que la même machine qui remesure remplace bien son
   relevé. Le patch porte un en-tête nommant le fichier et
   le variant : collé d'un cran trop loin, il atterrit dans `source:` et YAML
   l'accepte sans broncher.

**Mesurer le pic est spécifique au runtime, et s'y tromper coûte cher.** MLX
expose `get_peak_memory()`, qui est exact. PyTorch/MPS n'expose aucun maximum, et
surtout le RSS du processus **ne compte pas la mémoire Metal** : mesuré sur SDXL,
452 Mo de RSS pendant que le driver en réservait 15,95 Gio. Un profil écrit sur
le RSS aurait fait cohabiter deux modèles et provoqué l'OOM que tout ceci existe
pour empêcher. `driver_allocated_memory()` redescend aussi vite qu'il monte : le
maximum se tient à chaque relevé, pas une fois à la fin.

Convention de `rtf` : **temps de calcul par seconde produite**, agrégé sur toute
la charge et non moyenné par cas — la moyenne des ratios donne le même poids à
une phrase de deux secondes qu'à un paragraphe de quinze, alors que le warmup
pèse surtout sur la première. `throughput` en est l'inverse ; si les deux cessent
de l'être, l'un des deux est faux.

**Le banc regarde désormais la forme de ce qu'il produit** (jalon J2 du pivot).
Découvert le 22 août : `moss-transcribe` a passé ses trois cas au vert en
livrant des marqueurs de locuteur dans un fichier annoncé `text/plain`. Le banc
mesure un coût, pas une qualité — mais entre les deux il y a la conformité, et
elle est presque gratuite : chaque sortie de cas est validée contre le **schéma
de sortie du contrat** (le validateur est celui des jobs), les fichiers annoncés
existent avec le media type promis, un `text/plain` ne contient pas de balisage,
un JSON se relit. Un cas qui produit n'importe quoi vite ne « passe » plus.

**La garde du profil aveugle** (même jalon). L'affaire `da3-large` : un pic
sous-déclaré de 42,6 % « depuis le début » — le RSS relevé là où Metal payait —
avec un symptôme lisible que personne ne lisait, une pente nulle sous un R² de
1,0. Une mesure réelle a du bruit ; une droite parfaitement plate et
parfaitement ajustée dit que l'instrument ne voit pas la variable. Sur ce
symptôme, le banc refuse d'écrire le relevé et nomme l'instrument à changer, au
lieu de committer le chiffre qui aurait fait cohabiter deux modèles dans une
mémoire qui n'en tenait qu'un.

---

## 9. Évaluation — golden sets, A/B, Elo

- `registry/evals/golden/<capability>/` : entrées figées + `manifest.json`
  (id, entrée, référence attendue s'il y en a une), validé contre
  `registry/schema/golden.schema.json`. Règle : **append-only**, jamais de
  modification d'une entrée existante. **Livré au 5.1** : cinq jeux — 16 pages de
  lecture de document, 10 phrases de synthèse, 10 descriptions d'image, 8 solides
  à reconstruire, 12 extraits de transcription.

  À ne pas confondre avec `registry/evals/bench/`, qui existe depuis le v0.3 :
  la charge type mesure un **coût** (mémoire, warmup, débit), le golden set mesure
  une **qualité**. Les deux sont figés pour la même raison, mais on ne juge pas un
  modèle sur la charge du banc — ses trois entrées sont choisies pour être
  représentatives d'un coût, pas d'un usage.

  Une conséquence de cette différence, qu'il faut avoir en tête avant d'ajouter un
  cas : **le banc fige tous les réglages, le golden set n'en fige aucun.** Le banc
  doit comparer deux mesures prises à six mois d'écart, donc il impose les pas de
  débruitage et la résolution d'octree. Le golden set doit comparer deux modèles
  au mieux de leur forme : imposer trente pas à un modèle distillé conçu pour en
  faire quatre le jugerait sur un réglage fait pour un autre. Chaque cas ne fixe
  donc que ce qui **définit la question**, et laisse le reste aux `defaults:` du
  variant.

  Trois choix de forme, chacun réglant un piège précis. La vérité terrain longue
  vit dans un fichier à part (`reference/<id>.txt`) et non dans le JSON : une page
  entière échappée en une seule ligne ne se relit pas, et une vérité qu'on ne
  relit pas ne se vérifie plus. `notes` est obligatoire — un cas dont personne ne
  sait plus ce qu'il testait ne s'interprète pas davantage qu'il ne se remplace.
  `source` dit comment le fichier d'entrée a été fabriqué, et `tools/golden_assets.py`
  le refabrique : les images du banc d'essai n'ont pas cette recette, et ce sont
  aujourd'hui des données orphelines qu'on ne sait plus refaire.

  Le jeu de transcription est livré **incomplet**, et c'est assumé : ses douze
  textes sont figés, ses enregistrements restent à produire. Les synthétiser avec
  la voix du parc mesurerait la transcription de parole synthétique, sans accent
  québécois — c'est-à-dire sans ce qu'on veut précisément éprouver. Chaque cas
  nomme déjà son fichier et porte une clé `pending` ; le jour où l'enregistrement
  arrive, rien du cas ne change. C'est ce qui rend l'append-only tenable pour un
  jeu dont les entrées se produisent à la main.
- Métriques automatiques (WER via `jiwer`, exactitude OCR par champs) : exécutées par
  `ecurie eval <model>@<variant>`, résultats dans `registry/evals/results/`.
- Préférences humaines : l'écran Confrontation soumet
  `{capability, input_hash, variant_a, variant_b, winner, date}` — append dans
  `registry/evals/preferences.jsonl` (committé, c'est de la donnée). Le classement
  Elo (K = 32, départ 1000) est **dérivé** : recalculé à la volée depuis le jsonl,
  jamais stocké. Le choix des paires suit l'incertitude : on propose en priorité les
  paires les moins départagées.

Le pivot du 29 août 2026 gèle l'écran Confrontation avec le reste de l'UI ; ce
qui précède reste la conception du jour où une évaluation comparative ouvrira —
le même jour qui conditionne la réouverture de la veille (§10).

---

## 10. Veille et CI

**Gelée au pivot du 29 août 2026** : plus de cycle de veille tant qu'aucune
évaluation comparative n'existe — soixante-dix des soixante-douze manifestes
sont des candidats que rien n'a jamais départagés, et chaque cycle en ajoutait.
La CI du registre, elle, ne gèle pas : elle entre au jalon J0 (`PLAN.md`). Ce
qui suit reste la conception du jour où la veille rouvrira.

- Le skill existant (`SKILL.md` → `.claude/skills/veille-modeles/`) est déjà écrit ;
  le v0.6 lui fournit ce qui lui manque : `registry/veille/last_run.json`,
  `ecurie store status --json` (phase 3, garde des 15 %), et le workflow
  `veille.yml` (cron hebdomadaire qui ouvre la PR).
- `registry-ci.yml`, à chaque PR touchant `registry/` :
  1. validation JSON Schema de tous les manifestes ;
  2. invariants inter-fichiers : un incumbent par capacité, capacités connues,
     et **au moins un modèle par capacité déclarée** — un contrat sans manifeste
     propose un formulaire dont aucun bouton *Lancer* ne peut partir, et rien à
     l'écran ne dit quel modèle irait là ;
  3. `revision` : refus des placeholders `0000000` et de `main` sur `status: active` ;
  4. existence des révisions épinglées via l'API HF (job hebdomadaire aussi, pour
     détecter les dépôts disparus et les licences changées) ;
  5. refus d'un bloc `profile:` qui n'est la copie d'aucun relevé de
     `measurements/<ref>/` — c'est l'application mécanique de « jamais estimé à la
     main ». Le relevé dont le `measured_on` est celui que le manifeste annonce est
     comparé à l'égalité, `harness_version` comprise ; à défaut — le manifeste vient
     d'un poste dont ce clone n'a pas le relevé —, il suffit que ses chiffres
     **portables** (`peak_unified_memory_bytes`, `disk_bytes`) s'accordent avec l'un
     des relevés présents. `warmup_ms`, `latency_ms_p50` et `throughput` en sont
     exclus : ils mesurent la machine autant que le modèle, et les comparer d'un
     poste à l'autre déclencherait un avertissement chez tout le monde.

---

## 11. Capacité composite `text-to-mesh`

Une seule composition câblée (périmètre §11 de l'architecture). Le contrat
`capabilities/text-to-mesh.json` déclare des `steps` :

```json
{ "id": "text-to-mesh", "composite": true,
  "steps": [
    { "capability": "text-to-image", "expose": ["prompt", "seed"] },
    { "capability": "image-to-mesh", "input_from": "steps[0].output.image",
      "expose": ["octree_resolution"] } ],
  "checkpoint_after": [0] }
```

L'exécuteur composite enchaîne les jobs atomiques ; `checkpoint_after: [0]` matérialise
la boucle d'itération de la Route A : l'UI affiche l'image intermédiaire et propose
*régénérer* (nouvelle seed) ou *continuer* vers la reconstruction. Chaque étape est un
job normal de la Bibliothèque — la reproductibilité tombe gratuitement, et le manifeste
composite référence les job_ids des étapes. Le typage (`image/*` produit/consommé) est
vérifié au chargement du registre, pas à l'exécution.

---

## 12. Tests

- **Unitaires** : scanners sur des arborescences de fixtures synthétiques (faux cache
  HF, faux blobs Ollama, liens durs réels créés par le test) — les trois chiffres ont
  des valeurs attendues exactes ; plan de GC sur fixtures ; résolveur ; admission
  (simulation pure, aucun modèle réel).
- **Contrat de worker** : un `workers/fake.py` qui parle le protocole sans charger
  de modèle, et dont on demande les pannes par variables d'environnement
  (`ECURIE_FAKE_HANG`, `_FAIL`, `_EXIT`, `_IGNORE_SIGTERM`, `_NOISE`). Il éprouve
  la supervision, le ping, le timeout, l'escalade SIGTERM/SIGKILL, l'éviction, le
  job complet — en CI, sans Apple Silicon ni poids. Les tests du superviseur
  lancent de **vrais** sous-processus détachés sur de **vrais** sockets Unix :
  le mécanisme qui fait survivre un modèle entre deux commandes ne se simule pas.
- **Adaptateurs réels** : ce qui se vérifie sans le runtime — l'import du module
  sans la bibliothèque (c'est la situation de la CI), la préparation pure des
  arguments, le message qui nomme la réparation. Le reste demande le vrai modèle
  et relève du banc d'essai.
- **Intégration réelle** (locale, hors CI, `pytest -m real`) : un job TTS complet
  sur le vrai parc, la non-répétition du warmup au second job, et deux jobs
  concurrents qui se partagent un seul worker — la situation du serveur, qu'aucun
  worker d'essai ne prouve. Ces tests **sautent avec le message qui dit quelle
  commande manque** plutôt que d'échouer : un test rouge parce qu'un modèle de
  deux gigaoctets n'est pas téléchargé n'apprend rien à personne. Ils ne
  s'exécutent qu'à la main, donc à chaque fin de jalon : l'un d'eux avait viré au
  rouge sans que personne le sache, périmé par l'arrivée du profil paramétré.
- La CI GitHub Actions (runners sans Apple Silicon) n'exécute que unitaires, contrat
  et validation du registre.

- **Le front a les mêmes deux étages**, et pour la même raison. La suite en jsdom
  monte l'écran entier sur un double de `fetch` qui refuse toute route non
  déclarée — sans quoi un test oublié partirait frapper le `ecurie serve` qui
  tourne sur cette machine, et passerait au vert chez son auteur seulement. Le
  double sait servir un **flux** poussé morceau par morceau, ce qui rend le
  suivi d'un job éprouvable sans serveur : une barre qui bouge pendant que le
  flux dure, et un abandon qui coupe la connexion. `App.reel.test.tsx` fait le
  reste contre un vrai serveur, `ECURIE_ESSAI_REEL=1`, et c'est lui qui a trouvé
  la boucle de rendu du 4.3.

- **Le matériel de capture est injecté, jamais simulé par l'environnement.**
  `getUserMedia`, `MediaRecorder`, `AudioContext` et `canvas.toBlob` n'existent
  pas en jsdom et ne peuvent pas y exister : ce sont des accès à une caméra et à
  un micro. `CapturePanel` reçoit donc un objet `Materiel` en propriété, et la
  suite lui donne des pistes dont elle vérifie qu'elles s'arrêtent. C'est le même
  patron que le `spec_factory` du superviseur côté serveur : le point d'injection
  est aussi bas que possible, et tout ce qui est au-dessus est le chemin de
  production. L'encodage WAV, lui, n'a besoin d'aucun double — c'est du calcul
  pur, vérifié octet par octet, y compris le petit-boutisme de l'en-tête.

Au terme de la tâche 4.4 : 825 tests Python, plus 5 marqués `real` ; 371 tests de
front, plus 5 contre un vrai serveur. Ils comptent les huit contrats ajoutés le
22 août, puis les six derniers manifestes du même jour : la suite du front
engendre ses cas depuis `registry/capabilities/`, si bien qu'un contrat de plus
s'y rend sans qu'une ligne de test soit écrite.

---

## 13. Questions ouvertes (à trancher pendant, pas avant)

1. ~~`ollama`/`comfy` en proxy HTTP~~ — **repoussés**, comme prévu : le parc tourne
   sans eux. Trois runtimes de plus sont venus à la place, tous MLX ou PyTorch.
2. ~~Format du golden set OCR (vérité terrain par champs)~~ — **tranché au 5.1**.
   Toutes les pages demandent `format: "text"` : la mise en Markdown d'un tableau
   a plusieurs formes également défendables, et la noter reviendrait à mesurer la
   conformité à une préférence plutôt que l'exactitude de la lecture. La
   comparaison du texte se fait à blancs normalisés — toute suite d'espaces, de
   tabulations et de retours à la ligne vaut une espace —, et la structure se
   juge par `reference.fields`, qui n'a qu'une seule bonne réponse : une date, un
   montant, un code de dossier. Une page lue à 98 % dont le montant est faux est
   une page inutilisable, et le taux d'erreur global ne le dit pas.
3. `iogpu.wired_limit_mb` : simple page de doc + réglage affiché dans Parc, pas de
   `sudo` lancé par l'outil.
4. ~~**Hunyuan3D n'a jamais été exécuté.**~~ — **levé le 24 août 2026**, et la
   filière coupée le 29 pour d'autres motifs. `ecurie bench` a rendu les trois cas
   de `registry/evals/bench/image-to-mesh.json` en `ok` sur un Mac17,4 de 24 Gio :
   l'env est synchronisé, `hy3dshape` vendoré, les 7,37 Go téléchargés, le relevé
   committé (`registry/measurements/hunyuan3d-2.1-shape-mlx@mlx-bf16/mac17-4-24-gio.json`).
   Le chemin 2.1 sur MPS fonctionne donc, **sur cette machine-là** — et c'est ce
   qui reste ouvert : un pic de 16,48 Gio ne tient pas dans les ~11,8 Gio d'un Mac
   de 16 Gio, aucune comparaison n'a jamais départagé Hunyuan3D et TRELLIS.2 (dont
   les noyaux CUDA de voxels épars n'ont aucun portage Metal connu), et `hy3dshape`
   n'est publié sur aucun index de paquets — il se vendore à la main. Ces trois
   faits, et non l'inexécution, motivent la coupe de la filière hors v1
   (`ARCHITECTURE.md` §10).
5. ~~**Le seuil « lourd » de 6 Go est à recalibrer.**~~ — **fait le 20 août 2026**,
   avant le v0.4. Il venait du §7 de l'architecture, écrit avant toute mesure, et
   les quatre modèles mesurés étaient tous au-dessus (6,25 à 15,95 Gio) : aucun ne
   pouvait cohabiter avec un autre, alors que la voix et la lecture de document
   tiennent ensemble dans les 17,76 Gio. Seuil porté à **8 Gio**, dans
   `Config.heavy_threshold_bytes` comme dans `admission.DEFAULT_HEAVY_THRESHOLD`,
   avec un test qui vérifie que les deux ne divergent pas — `core` ne pouvant pas
   dépendre de `runtime`, la constante est nécessairement écrite deux fois.
6. **Aucune capacité ajoutée après le v0.1 n'a de titulaire.** Les trois modèles
   du parc réel sont en `status: candidate` : ils fonctionnent et sont mesurés,
   mais rien ne dit encore qu'ils sont les bons. La promotion en `incumbent`
   demande une comparaison, donc le v0.5.
7. **Les réservations durables du fan-out de flux.** `ecurie_fan_out` (1.7) se
   contente d'une réservation groupée le temps d'un appel ; les étapes 2 et 3 de
   la fiche (`registry/veille/BACKLOG.md` §E — sorties push, sources gérées)
   demandent d'épingler N modèles pour la durée d'un pipeline. Qui pose cette
   épingle, qui la lève, ce qu'elle fait au LRU, à `max_heavy_resident` et à la
   doctrine « une épingle est une préférence humaine » — à trancher quand
   `ecurie_fan_out` aura trouvé ses usages, jamais avant.
