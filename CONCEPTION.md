# Écurie — Conception détaillée

> Ce document complète `ARCHITECTURE.md`. L'architecture dit *quoi* et *pourquoi* ;
> ici on fixe *comment* : structures de données, interfaces, algorithmes, formats.
> Le plan d'exécution correspondant est dans `PLAN.md`.
>
> Date : 19 août 2026.

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
| Observé | `~/.ecurie/state.db` (SQLite) | artifacts, locations, cache de hash, télémétrie, résidents mémoire |
| Dérivé | recalculé, jamais committé | classement Elo, trois chiffres d'occupation, plans de GC |

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

Le dépôt actuel est plat et n'est pas encore un dépôt Git. Première tâche du v0.1 :

```
ecurie/                          # git init, monorepo uv workspace
  ARCHITECTURE.md  CONCEPTION.md  PLAN.md
  registry/
    schema/model.schema.json     # ← model.schema.json actuel
    capabilities/*.json
    models/
      qwen3-tts-1.7b.yaml        # ← fichiers actuels
      hunyuan3d-2.1-shape-mlx.yaml
      trellis2.yaml
    measurements/  evals/  veille/
  packages/
    core/     store/    runtime/   api/   veille/     # paquets Python (uv workspace)
  apps/ui/                        # React + Vite + RJSF
  runtimes/                       # envs isolés — .gitignore sur les .venv
    mlx-audio/pyproject.toml
    diffusers/pyproject.toml
    hunyuan3d/{pyproject.toml, run.py}
  .claude/skills/veille-modeles/SKILL.md   # ← SKILL.md actuel
  .github/workflows/{registry-ci.yml, veille.yml}
```

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
  `status: active`.
- **Config machine** : `~/.ecurie/config.toml` — chemins des gestionnaires scannés
  (avec autodétection par défaut), volumes de tiering autorisés, budget mémoire
  (`auto` = `recommendedMaxWorkingSetSize` lu via MLX, ou valeur explicite),
  chemins déclarés manuellement.

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
→ {"op":"unload"}   /   {"op":"ping"} ← {"ev":"pong","rss_bytes":…}
```

Règles : les sorties binaires vont **en fichiers** dans `output_dir`, jamais en
base64 sur stdio ; `stderr` du worker est capturé en log, seul `stdout` porte le
protocole ; un worker qui ne répond pas au ping en 10 s est tué (SIGTERM puis SIGKILL)
et son variant marqué non résident.

### 5.2 Adaptateurs

Un adaptateur = un module worker (`packages/runtime/workers/<runtime>.py`) copié ou
importé dans le venv cible, qui traduit le protocole vers la bibliothèque du runtime.
v0.3 en livre trois : `mlx_audio` (TTS/ASR), `diffusers_mps` (image), `custom`
(délègue à `entrypoint` du manifeste — c'est le chemin de Hunyuan3D). `ollama` et
`comfy` (proxys HTTP vers leurs serveurs) viennent après, ce sont les plus simples.

### 5.3 Environnements isolés

`runtimes/<env>/pyproject.toml` versionné, `.venv` ignoré. `ecurie env sync [env]`
exécute `uv sync` dans chaque env. Le superviseur refuse de lancer un worker si le
venv est absent et affiche la commande de réparation. Aucune dépendance de runtime
dans l'env racine — l'env racine ne connaît que pydantic, typer, FastAPI, PyYAML,
huggingface_hub.

### 5.4 Contrôle d'admission

Le superviseur (dans le processus API) tient la table des résidents
`{variant_ref, peak_bytes (profil mesuré), last_used}`. Avant `load` :

```
budget    = config.memory_budget          # défaut : recommendedMaxWorkingSetSize
résiduel  = budget − Σ peak_bytes(résidents)
tant que résiduel < peak(candidat) :
    décharger le résident LRU non épinglé ; recalculer
si peak(candidat) > budget seul → refus explicite (jamais de swap subi)
```

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
3. Mesure : `warmup_ms` (load → loaded), pic mémoire (`mx.get_peak_memory()` si MLX,
   sinon RSS échantillonné à 100 ms sur toute la vie du worker), `disk_bytes` (somme
   des artifacts du variant), débit/latence/rtf sur la **charge type de la capacité**
   (3 entrées fixes par capacité, versionnées avec les golden sets).
4. Écrit `registry/measurements/<id>@<variant>.json` avec `measured_on`,
   `harness_version`, et affiche le patch `profile:` à committer dans le YAML.
   Le fichier de mesure est l'autorité ; le bloc `profile` du manifeste en est la
   copie committée par un humain.

---

## 9. Évaluation — golden sets, A/B, Elo

- `registry/evals/golden/<capability>/` : entrées figées + `manifest.json`
  (id, entrée, référence attendue s'il y en a une). Règle : **append-only**, jamais
  de modification d'une entrée existante.
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
- **Contrat de worker** : un `fake_worker.py` qui parle le protocole sans charger de
  modèle — teste supervision, timeout, ping, admission, SSE de bout en bout en CI.
- **Intégration réelle** (locale, hors CI, `pytest -m real`) : un job TTS complet sur
  le vrai parc.
- La CI GitHub Actions (runners sans Apple Silicon) n'exécute que unitaires, contrat
  et validation du registre.

---

## 13. Questions ouvertes (à trancher pendant, pas avant)

1. `ollama`/`comfy` en proxy HTTP : v0.3 si besoin réel, sinon repoussés — le parc
   initial tourne avec `mlx-audio`, `diffusers-mps` et `custom`.
2. Format du golden set OCR (vérité terrain par champs) : à définir au v0.5 avec les
   15 pages réelles.
3. `iogpu.wired_limit_mb` : simple page de doc + réglage affiché dans Parc, pas de
   `sudo` lancé par l'outil.
