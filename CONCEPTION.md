# Écurie — Conception détaillée

> Ce document complète `ARCHITECTURE.md`. L'architecture dit *quoi* et *pourquoi* ;
> ici on fixe *comment* : structures de données, interfaces, algorithmes, formats.
> Le plan d'exécution correspondant est dans `PLAN.md`.
>
> Rédigé le 19 août 2026, révisé le 20 août au terme du v0.3 : les sections §2,
> §3, §5, §8, §9, §12 et §13 portent maintenant ce que l'exécution réelle a
> établi, et non plus seulement ce qui était prévu. Ce qui a été démenti par la
> mesure est signalé comme tel plutôt que réécrit en silence.

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
| Déclaré | `registry/` en Git | manifestes, capacités, `measurements/`, `evals/preferences.jsonl` |
| Observé | `~/.ecurie/state.db` (SQLite) | artifacts, locations, cache de hash, télémétrie |
| Observé | `~/.ecurie/residents.json` | modèles chargés en mémoire, sous verrou de fichier |
| Dérivé | recalculé, jamais committé | classement Elo, trois chiffres d'occupation, plans de GC |

Les résidents mémoire sont à part, hors SQLite : ils changent à chaque commande,
sont lus et réécrits sous verrou exclusif par le superviseur (§5.4), et une
entrée dont le processus est mort doit disparaître à la lecture. Un fichier JSON
verrouillé dit cela plus simplement qu'une table qu'il faudrait garder en
cohérence avec des PID. Les sockets des workers, eux, vivent dans un répertoire
temporaire court et non dans `~/.ecurie` : `sun_path` est limité à 104 octets.

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

État au terme du v0.3 — ce qui existe porte une coche, le reste attend son jalon :

```
ecurie/                          # monorepo uv workspace
  ARCHITECTURE.md  CONCEPTION.md  PLAN.md
  registry/
    schema/{model,capability}.schema.json      ✓
    capabilities/*.json                        ✓ 12 contrats atomiques
    models/*.yaml                              ✓ 6 manifestes
    measurements/<id>@<variant>.json           ✓ 4 profils mesurés
    evals/
      bench/<capability>.json + assets/        ✓ charges type du banc d'essai
      golden/  results/  preferences.jsonl       v0.5
    veille/                                      v0.6
  packages/
    core/  store/  runtime/                    ✓
    api/                                         v0.4
    veille/                                      v0.6
  apps/ui/                                       v0.4
  runtimes/                       # envs isolés — .gitignore sur les .venv
    mlx-audio/       pyproject.toml + uv.lock  ✓ TTS
    mlx-audio-music/ pyproject.toml + uv.lock  ✓ chanson (commit épinglé, voir §5.3)
    mlx-vlm/         pyproject.toml + uv.lock  ✓ lecture de document
    diffusers-mps/   pyproject.toml + uv.lock  ✓ image
    hunyuan3d/       pyproject.toml + run.py   ✓ écrit, jamais exécuté (§13.4)
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

Côté worker, le descripteur 1 est réservé au protocole dès le démarrage et
remplacé par le descripteur 2 : une barre de progression ou un avertissement de
bibliothèque part alors dans le journal au lieu de couper une ligne JSON en deux.

### 5.2 Adaptateurs

Un adaptateur = un module worker (`packages/runtime/workers/<runtime>.py`) rendu
visible par `PYTHONPATH` dans le venv cible, qui traduit le protocole vers la
bibliothèque du runtime. Rien n'est installé dans le venv du runtime : seuls
`protocol`, `channel` et `workers/base` y sont visibles, tous trois en
bibliothèque standard pure.

Cinq sont livrés :

| runtime | capacité | adaptateur |
|---|---|---|
| `mlx-audio` | `text-to-speech` | `workers/mlx_audio.py` |
| `mlx-audio` | `text-to-music` | `workers/mlx_audio_music.py` |
| `mlx-vlm` | `document-to-text` | `workers/mlx_vlm.py` |
| `diffusers-mps` | `text-to-image` | `workers/diffusers_mps.py` |
| `custom` | — | l'`entrypoint` du manifeste (chemin de Hunyuan3D) |

**Un runtime peut servir plusieurs capacités par des API qui n'ont rien en
commun.** `mlx_audio.tts` et `mlx_audio.music` ne partagent ni le chargement, ni
l'appel, ni la sortie ; les fondre dans un adaptateur donnerait un fichier qui
commence par un aiguillage et ne se relit plus. Le choix se fait donc sur le
couple (runtime, capacité), avec repli sur le runtime seul.

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
  du job, jamais en rééchantillonnage maison ni en kwarg avalé sans effet.

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
et la commande qui l'a provoqué n'en sait même rien. L'occupation est portée par
le registre des résidents sous la forme du **pid du processus qui tient le
worker** : un drapeau resterait posé pour toujours si la commande était
interrompue, un pid se vérifie.

Politique du §7 de l'architecture encodée en config : `max_heavy_resident = 1`
(lourd = peak > 6 Go), les légers restent chauds. Un variant **sans profil mesuré**
n'est exécutable qu'en mode mesure : parc déchargé entièrement, échantillonnage RSS,
le résultat écrit le premier profil. C'est ce qui rend la règle « jamais de profil
estimé » vivable au premier lancement.

---

## 6. `packages/api` — jobs, SSE, bibliothèque

FastAPI, lancée par `ecurie serve`. Surface v0.4 :

```
GET  /registry/capabilities              GET /registry/models[?capability=]
POST /jobs        {model, variant, input, params, seed?}   → {job_id}
GET  /jobs/{id}   état + manifeste       GET /jobs/{id}/events   (SSE)
GET  /jobs/{id}/files/{name}             fichiers de sortie
GET  /store/summary                      trois chiffres + arbre de duplication
POST /store/plan                         GET /runtime/residents
POST /evals/preference  {capability, input_hash, a, b, winner}
GET  /library[?capability=&model=]       POST /library/{job_id}/replay
```

**Bibliothèque / reproductibilité** : chaque job écrit
`~/.ecurie/outputs/<job_id>/manifest.json` — capability, model, variant, révision
épinglée, params complets (défauts résolus inclus), seed, sha256 de l'entrée,
versions (harness, runtime, adaptateur), durée, métriques. L'entrée elle-même est
copiée dans le dossier du job (texte inline dans le manifeste, fichiers copiés).
*Rejouer* = re-soumettre le manifeste tel quel ; le serveur refuse si la révision
résolue diffère de celle du manifeste, sauf `--force`.

Un job = une ligne dans `runs` (télémétrie du poste « jamais utilisé » du GC).

---

## 7. `apps/ui` — rendu piloté par schéma

React + Vite + TypeScript + `react-jsonschema-form`. Aucun formulaire écrit à la main.

- **Extensions `x-ui`** du contrat de capacité mappées sur des widgets RJSF :
  `textarea`, `select`, `file` (upload → l'API stocke et renvoie une référence),
  `x-options-from: runtime.voices` → options chargées depuis la réponse `loaded` du
  worker (endpoint `/runtime/residents/{ref}/options`).
- **Visualiseurs de sortie** par `contentMediaType` : `audio/*` → lecteur,
  `image/*` → image + zoom, `video/*` → vidéo, `model/gltf-binary` →
  `<model-viewer>` (Google, autonome, pas de scène three.js à écrire), `text/*` →
  texte + diff pour l'OCR. Table d'aiguillage unique, un composant par type.
- **Quatre écrans**, plafond ferme (§9 de l'architecture) : Atelier, Confrontation,
  Parc, Bibliothèque. Le bandeau de ressources (mémoire résidente, budget restant,
  « lancer déchargera X ») est un composant global alimenté par `/runtime/residents`.

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
5. Écrit `registry/measurements/<id>@<variant>.json` avec `measured_on`,
   `harness_version`, et affiche le patch `profile:` à committer dans le YAML.
   Le fichier de mesure est l'autorité ; le bloc `profile` du manifeste en est la
   copie committée par un humain. Le patch porte un en-tête nommant le fichier et
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

---

## 9. Évaluation — golden sets, A/B, Elo

- `registry/evals/golden/<capability>/` : entrées figées + `manifest.json`
  (id, entrée, référence attendue s'il y en a une). Règle : **append-only**, jamais
  de modification d'une entrée existante.

  À ne pas confondre avec `registry/evals/bench/`, qui existe depuis le v0.3 :
  la charge type mesure un **coût** (mémoire, warmup, débit), le golden set mesure
  une **qualité**. Les deux sont figés pour la même raison, mais on ne juge pas un
  modèle sur la charge du banc — ses trois entrées sont choisies pour être
  représentatives d'un coût, pas d'un usage.
- Métriques automatiques (WER via `jiwer`, exactitude OCR par champs) : exécutées par
  `ecurie eval <model>@<variant>`, résultats dans `registry/evals/results/`.
- Préférences humaines : l'écran Confrontation soumet
  `{capability, input_hash, variant_a, variant_b, winner, date}` — append dans
  `registry/evals/preferences.jsonl` (committé, c'est de la donnée). Le classement
  Elo (K = 32, départ 1000) est **dérivé** : recalculé à la volée depuis le jsonl,
  jamais stocké. Le choix des paires suit l'incertitude : on propose en priorité les
  paires les moins départagées.

---

## 10. Veille et CI

- Le skill existant (`SKILL.md` → `.claude/skills/veille-modeles/`) est déjà écrit ;
  le v0.6 lui fournit ce qui lui manque : `registry/veille/last_run.json`,
  `ecurie store status --json` (phase 3, garde des 15 %), et le workflow
  `veille.yml` (cron hebdomadaire qui ouvre la PR).
- `registry-ci.yml`, à chaque PR touchant `registry/` :
  1. validation JSON Schema de tous les manifestes ;
  2. invariants inter-fichiers (un incumbent par capacité, capacités connues) ;
  3. `revision` : refus des placeholders `0000000` et de `main` sur `status: active` ;
  4. existence des révisions épinglées via l'API HF (job hebdomadaire aussi, pour
     détecter les dépôts disparus et les licences changées) ;
  5. refus d'un bloc `profile:` sans fichier `measurements/` correspondant de même
     `harness_version` — c'est l'application mécanique de « jamais estimé à la main ».

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
  sur le vrai parc, et la non-répétition du warmup au second job. Ces tests
  **sautent avec le message qui dit quelle commande manque** plutôt que d'échouer :
  un test rouge parce qu'un modèle de deux gigaoctets n'est pas téléchargé
  n'apprend rien à personne.
- La CI GitHub Actions (runners sans Apple Silicon) n'exécute que unitaires, contrat
  et validation du registre.

Au terme du v0.3 : 556 tests, dont 3 marqués `real`.

---

## 13. Questions ouvertes (à trancher pendant, pas avant)

1. ~~`ollama`/`comfy` en proxy HTTP~~ — **repoussés**, comme prévu : le parc tourne
   sans eux. Trois runtimes de plus sont venus à la place, tous MLX ou PyTorch.
2. Format du golden set OCR (vérité terrain par champs) : à définir au v0.5 avec les
   15 pages réelles. La charge type du banc en donne déjà la forme la plus simple —
   trois pages rendues depuis des textes du dépôt, donc à vérité terrain exacte.
3. `iogpu.wired_limit_mb` : simple page de doc + réglage affiché dans Parc, pas de
   `sudo` lancé par l'outil.
4. **Hunyuan3D n'a jamais été exécuté.** `runtimes/hunyuan3d/run.py` est écrit
   d'après le source amont, son env n'est pas synchronisé, le code `hy3dshape`
   n'est pas vendoré et les 7,37 Go de poids ne sont pas téléchargés. Rien ne
   prouve que le chemin 2.1 sur MPS fonctionne, et aucune trace publique ne
   l'établit. C'est le risque principal du v0.7, qui en dépend.
5. **Le seuil « lourd » de 6 Go est à recalibrer.** Il vient du §7 de
   l'architecture, écrit avant toute mesure. Les quatre modèles mesurés sont
   au-dessus (6,25 à 15,95 Gio), donc aucun ne peut cohabiter avec un autre —
   alors que la voix et la lecture de document tiendraient ensemble dans les
   17,76 Gio. Un seuil à 8 Go rendrait la politique de nouveau discriminante.
6. **Aucune capacité ajoutée après le v0.1 n'a de titulaire.** Les trois modèles
   du parc réel sont en `status: candidate` : ils fonctionnent et sont mesurés,
   mais rien ne dit encore qu'ils sont les bons. La promotion en `incumbent`
   demande une comparaison, donc le v0.5.
