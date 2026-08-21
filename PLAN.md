# Écurie — Plan de réalisation

> Découpage en tâches des jalons du §12 de `ARCHITECTURE.md`, selon la conception de
> `CONCEPTION.md`. Chaque jalon a un critère de sortie unique et vérifiable ; on ne
> passe pas au suivant sans lui.
>
> Rappel du test d'existence du projet : **si le v0.1 ne sert pas dans la semaine qui
> suit sa livraison, on arrête et on réévalue.**

## État au 20 août 2026

| Jalon | État | Critère de sortie |
|---|---|---|
| v0.1 — Voir le parc | **livré** | atteint |
| v0.2 — Récupérer des gigaoctets | **livré** | atteint |
| v0.3 — Exécuter sans OOM | **livré** | atteint : `run` TTS produit un wav, un job image décharge le TTS proprement, sans swap |
| v0.4 — Utilisable au quotidien | **en cours** | 4.1 : la surface de lecture d'`ecurie serve` répond sur le vrai parc |
| v0.5 → v0.7 | à faire | — |

Le parc réel compte quatre capacités mesurées sur la machine : voix
(qwen3-tts-1.7b), image (sdxl-base), lecture de document (qwen3-vl-8b-ocr),
musique (minimax-music3). Cinq environnements de runtime, six paquets, 638 tests.

Deux choses ont été faites en marge du jalon, et elles n'attendaient personne :
le **recalibrage du seuil de lourdeur** (voir les points de contrôle), et la
**rédaction des golden sets** de la tâche 5.1, qui est du travail de fond dont le
v0.5 dépend entièrement.

### Cinq capacités de plus, hors jalon

Ajoutées le 21 août 2026 : `image-matting`, `image-upscale`, `image-to-text`,
`translation`, `tool-use`. Le parc passe de 4 à **10 capacités exécutables** sur
17 déclarées, avec deux runtimes de plus (`mlx-lm`, `torch-vision`), six
adaptateurs, cinq manifestes et six golden sets. Tous les profils sont mesurés,
et chaque adaptateur a produit une sortie réelle.

Ce que l'exercice a appris, et qui ne se lisait dans aucun plan :

1. **Le contrat de capacité tient sa promesse, et on peut le chiffrer.**
   `image-to-text` a coûté un contrat, un manifeste et un adaptateur — zéro
   téléchargement, zéro environnement, zéro ligne de superviseur, parce que ses
   poids sont ceux de la lecture de document. Son profil, en revanche, a dû être
   mesuré pour lui-même : 6,60 Go contre 6,71 pour la transcription, ce qui suffit
   à condamner l'idée de recopier un profil d'un manifeste à l'autre.
2. **Un dépôt partagé par deux manifestes cassait la comptabilité.** Le résolveur
   du store indexait les dépôts dans un simple dictionnaire : le second manifeste
   chargé écrasait le premier. Conséquence la plus grave, aujourd'hui couverte
   par un test — le poste « jamais utilisé » du plan de récupération proposait à
   la corbeille des poids servis tous les jours par l'autre manifeste.
3. **Les quatre adaptateurs neufs ont tous échoué au premier lancement**, comme
   les trois du v0.3, et sur des hypothèses écrites avant mesure : un
   `requirements.txt` amont incomplet, des poids en demi-précision annoncés fp32,
   un gabarit de conversation qui rend un objet et non une chaîne, un jeton de
   fin de tour qui fuit dans la sortie. Aucun de ces défauts n'était visible en
   relecture ; le dernier ne fait même échouer personne.
4. **Certaines capacités génératives ont une vérité terrain exacte**, à condition
   de fabriquer l'entrée à l'envers. Le masque de détourage est celui qui a servi
   à composer la scène ; l'image d'agrandissement est l'originale dont l'entrée
   est la réduction. Mesuré : MAE 0,0002 et IoU 0,9966 entre le masque produit et
   la référence. Ces jeux se notent sans juge humain, ce que le §9 n'envisageait
   que pour l'ASR et l'OCR.

Ce que le v0.3 a coûté de plus que prévu, et qu'il faut savoir avant de lire la
suite : **deux runtimes non planifiés** (`mlx-vlm`, et un second env `mlx-audio`
pour la musique), et **un ajout au modèle de données** — le profil paramétré
(§3 de la conception), sans lequel un modèle dont le coût dépend de l'entrée est
soit toujours refusé, soit dangereux. Les deux sont venus de l'usage, pas du
plan : c'est le signe que le jalon a fait son travail.

---

## v0.1 — Voir le parc (1 fin de semaine)

Lecture seule. Aucune exécution de modèle, aucune écriture sur le disque scanné.

| # | Tâche | Livrable |
|---|---|---|
| 1.1 | ✓ `git init`, arborescence cible, migration des fichiers actuels (`registry/schema/`, `registry/models/`, `.claude/skills/veille-modeles/`), workspace `uv`, `ruff`, `pytest` | dépôt structuré, `uv sync` passe |
| 1.2 | ✓ `core` : modèles pydantic, chargement + validation du registre, invariants inter-fichiers | `ecurie registry validate` sur les 3 manifestes existants (le placeholder `0000000` doit être signalé) |
| 1.3 | ✓ `core` : config machine `~/.ecurie/config.toml` avec autodétection des chemins | config générée au premier lancement |
| 1.4 | ✓ `store` : SQLite (`artifacts`, `locations`, `hash_cache`), scanners `hf`, `ollama`, `lmstudio`, `comfy`, `declared` | `ecurie store scan` remplit la base |
| 1.5 | ✓ `store` : hachage niveaux 1–2 (inode + hash annoncé), calcul des trois chiffres, arbre de duplication | `ecurie store status` |
| 1.6 | ✓ Résolveur : rattachement Locations ↔ variants du registre | variants inconnus du registre listés à part (« fichiers hors registre ») |
| 1.7 | ✓ Tests unitaires sur fixtures synthétiques (chiffres attendus exacts) | `pytest` vert |

**Critère de sortie** : sur la machine réelle, `ecurie store status` affiche apparent /
réel unique / récupérable (2 postes sur 4 : duplication, révisions obsolètes) en
moins de 30 s, et au moins une duplication réelle est découverte ou son absence
confirmée.

## v0.2 — Récupérer des gigaoctets (1 semaine)

| # | Tâche | Livrable |
|---|---|---|
| 2.1 | ✓ sha256 complet à la demande + cache ; `ecurie store verify` | hash vérifiés persistés |
| 2.2 | ✓ Générateur de plan de GC (4 postes, format JSON de la conception §4.3) | `ecurie store plan` avec gain chiffré par poste |
| 2.3 | ✓ Quarantaine `~/.ecurie/trash/` + `trash list/empty` | aucune suppression directe possible dans le code (vérifié par test) |
| 2.4 | ✓ `ecurie store apply` : dédup lien dur (re-hash à l'exécution, même volume, remplacement atomique), mise en corbeille | journal d'application |
| 2.5 | ✓ Tiering : `ecurie store tier <ref> /Volumes/…`, détection volume absent au scan | patch YAML `tier: cold` affiché |
| 2.6 | ✓ Table `runs` (télémétrie, encore vide) branchée au poste « jamais utilisé » | affiché « inconnu » tant que vide |

**Critère de sortie** : un plan appliqué sur le vrai parc récupère l'espace annoncé
(±1 %), et tout ce qui a quitté sa place est dans la corbeille, restaurable à la main.

## v0.3 — Exécuter sans OOM (2 semaines) — **livré**

| # | Tâche | Livrable |
|---|---|---|
| 3.1 | ✓ Contrats de capacité du parc initial (`capabilities/*.json`) + validation croisée au chargement du registre | schémas d'E/S committés |
| 3.2 | ✓ Protocole worker + superviseur (spawn dans le venv, ping, timeout, kill) | testé avec `fake_worker.py` |
| 3.3 | ✓ `runtimes/` : `pyproject.toml` par env, `ecurie env sync` | envs reconstructibles |
| 3.4 | ✓ Adaptateurs `mlx_audio`, `diffusers_mps`, `custom` (entrypoint Hunyuan3D) | 3 écrits, 2 éprouvés en vrai ; l'entrypoint Hunyuan3D n'a jamais tourné. Deux de plus sont venus après coup : `mlx_vlm` et `mlx_audio_music` |
| 3.5 | ✓ `ecurie pull` (téléchargement à révision épinglée, garde des 15 % de disque libre) ; épingler les vraies révisions des 2 manifestes actifs | placeholders `0000000` éliminés |
| 3.6 | ✓ Contrôle d'admission (budget, LRU, mode mesure pour variant sans profil) + `ecurie ps` / `ecurie unload` | simulation testée unitairement |
| 3.7 | ✓ `ecurie run <ref> -p k=v` : job complet, sortie fichiers, ligne `runs` | premier `run` TTS réel |
| 3.8 | ✓ `ecurie bench <ref>` : mesure du profil, écriture `measurements/`, patch `profile:` | 4 profils mesurés et committés (voix, image, document, musique). Le titulaire `image-to-mesh` manque, faute de poids téléchargés |

**Critère de sortie** : `ecurie run qwen3-tts-1.7b -p text="…"` produit un wav ;
lancer ensuite un job image décharge le TTS proprement (visible dans `ecurie ps`),
sans swap ni OOM. **Atteint le 20 août 2026** — wav de 4,64 s en 3,4 s, puis
`ecurie run sdxl-base` a tué le worker TTS pour prendre sa place.

Reste en dette, à traiter avant le v0.7 qui en dépend : `runtimes/hunyuan3d/run.py`
est écrit mais n'a **jamais été exécuté** — env non synchronisé, `hy3dshape` non
vendoré, 7,37 Go de poids non téléchargés (conception §13.4).

## v0.4 — Utilisable au quotidien (2 semaines)

| # | Tâche | Livrable |
|---|---|---|
| 4.1 | ◑ FastAPI : registre, jobs + SSE, `store/summary`, résidents | `ecurie serve` sert les **lectures** : `/registry/capabilities`, `/registry/models`, `/store/summary`, `/runtime/residents`, plus `/runtime/admission` que le §4 réclamait. Les jobs et le SSE attendent le 4.6 |
| 4.2 | Bibliothèque côté serveur : manifeste de job complet, rejeu | reproductibilité effective |
| 4.3 | UI : socle React+Vite+RJSF, mapping `x-ui`, visualiseurs par media type | formulaire généré depuis un contrat, zéro formulaire manuel |
| 4.4 | Écran **Atelier** (capacité → variant → formulaire → progression SSE → sortie) + bandeau de ressources | flux complet dans le navigateur |
| 4.5 | Écran **Parc** (trois chiffres, arbre de duplication, plan de GC dry-run, tiering) | parité avec la CLI |
| 4.6 | Le superviseur passe dans le processus de l'API : l'occupation des résidents cesse d'être un pid dans un fichier verrouillé et redevient un état en mémoire, et deux jobs sur un même worker se sérialisent au lieu d'attendre dans le backlog du socket | `residents.json` n'est plus qu'un miroir de lecture pour la CLI |
| 4.7 | Bandeau de ressources **calculé sur l'entrée en cours de saisie** : un profil paramétré (§3 conception) change le pic attendu quand l'utilisateur bouge un curseur de durée ou de résolution | « lancer coûtera 17,2 Gio, déchargera X » se met à jour en direct |

**Critère de sortie** : une semaine d'usage réel où l'UI est le chemin par défaut
pour lancer TTS et image — sans retomber sur les scripts d'origine.

Ce que le v0.3 a rendu obligatoire ici : l'admission doit être interrogeable
**avant** de soumettre (l'endpoint existe déjà en CLI sous `ecurie ps --for`), et
l'UI doit rendre un refus lisible — « ce morceau de 30 s demanderait 24,2 Gio,
au-delà des 17,8 disponibles » vaut mieux qu'un bouton grisé.

## v0.5 — Mesurer (2 semaines)

| # | Tâche | Livrable |
|---|---|---|
| 5.1 | ◑ Golden sets figés : ASR (12 extraits dont FR-QC), OCR (15 pages), TTS (10 phrases), image (10 prompts), mesh (8 images) — append-only. **Distincts des charges type du banc** (`registry/evals/bench/`), qui mesurent un coût et non une qualité | `registry/evals/golden/` committé : 16 pages OCR à vérité terrain exacte, 10 phrases, 10 descriptions, 8 solides, plus un méta-schéma et `tools/golden_assets.py` qui refabrique les entrées. **L'ASR est écrit mais sans son** : les douze textes sont figés, les enregistrements restent à faire (`speech-to-text/SOURCING.md`) |
| 5.2 | `ecurie eval` : métriques automatiques (WER, exactitude OCR) → `evals/results/` | comparables entre variants |
| 5.3 | Exécution A/B (même entrée, deux variants, séquentielle sous admission) | paires générées |
| 5.4 | Écran **Confrontation** + `preferences.jsonl` + Elo dérivé + choix des paires par incertitude | 30 comparaisons TTS faites |
| 5.5 | Écran **Bibliothèque** (index, filtre, rejouer) | quatrième écran, plafond atteint |
| 5.6 | Promouvoir en `incumbent` les candidats qui l'ont mérité : les trois modèles ajoutés au v0.3 (image, document, musique) sont mesurés mais jamais comparés, donc aucune de ces capacités n'a de titulaire | trois capacités de plus avec une référence pour l'A/B |

**Critère de sortie** : le classement TTS départage titulaire et challenger sur des
préférences réelles, et un artefact de trois semaines est rejoué à l'identique.

## v0.6 — S'entretenir (1 semaine)

| # | Tâche | Livrable |
|---|---|---|
| 6.1 | `registry-ci.yml` : schéma, invariants, révisions épinglées existantes, licences, profil ⇔ mesure. La validation croisée `defaults:` ↔ contrat et la conformité pydantic ⇔ JSON Schema existent déjà en tests : il s'agit de les câbler, pas de les écrire | CI verte exigée sur `registry/` |
| 6.2 | Compléments du skill de veille : `last_run.json`, `store status --json`, quarantaine de téléchargement | phases 1–3 du skill exécutables |
| 6.3 | `veille.yml` : cron hebdomadaire → branche `veille/<date>` → PR au format RAPPORT.md | première PR de veille reçue |
| 6.4 | Vérification hebdomadaire des révisions/licences du parc actif (`resolve_revision` du v0.3 fait déjà l'appel) | alerte si un dépôt HF disparaît |
| 6.5 | La CI refuse un `peak_scaling` dont le `r_squared` est absent ou sous 0,9, et un `measured_range` qui ne recouvre pas les cas de la charge type | une pente committée est une pente ajustée, pas une estimation |

**Critère de sortie** : un cycle de veille complet — PR reçue, un candidat éprouvé sur
golden set, décision prise en connaissance de coût — sans rien télécharger avant l'accord.

## v0.7 — Texte → 3D (1 semaine)

| # | Tâche | Livrable |
|---|---|---|
| 7.0 | **Éprouver Hunyuan3D**, qui n'a jamais tourné : synchroniser l'env, vendorer `hy3dshape`, télécharger les 7,37 Go, mesurer. Rien ne prouve aujourd'hui que le chemin 2.1 sur MPS fonctionne — c'est le préalable de tout ce jalon, et il peut le condamner | un maillage produit par `ecurie run`, ou la décision de changer de modèle |
| 7.1 | Contrat composite `text-to-mesh` (steps + checkpoint) + validation du typage inter-étapes. Le méta-schéma des capacités exige `input` et `output` : le contrat composite devra les déclarer, ou l'exigence sera relâchée sous `composite: true` — à trancher ici | schéma committé |
| 7.2 | Exécuteur composite (jobs chaînés, admission entre étapes : décharger l'image avant le mesh) | pipeline réel Z-Image/FLUX → Hunyuan3D |
| 7.3 | UI : point d'arrêt sur l'image intermédiaire, *régénérer* / *continuer* | boucle d'itération bon marché de la Route A |
| 7.4 | Manifeste composite en Bibliothèque (référence les étapes) | rejeu par étape ou complet |

**Critère de sortie** : `prompt → maillage` dans l'Atelier, avec itération sur l'image
avant de payer la reconstruction.

Contrainte mémoire connue d'avance : `sdxl-base` occupe 15,95 Gio, soit 90 % du
budget. La composition **devra** décharger l'image avant de charger le mesh —
c'est exactement ce que la tâche 7.2 prévoit, et la mesure confirme qu'il n'y a
pas d'alternative.

---

## Ordre et dépendances

```
v0.1 ── v0.2 ── v0.3 ── v0.4 ── v0.5 ── v0.6 ── v0.7
 ✓       ✓       ✓       ◑       │               │
                         │       └ 5.1 golden sets ✓ (sauf le son de l'ASR)
                         │                       │
                         │                       └ 7.0 (éprouver Hunyuan3D)
                         │                          peut démarrer n'importe quand
                         └ 4.6 (superviseur dans l'API) rend caduc
                            le verrou de fichier du v0.3, et conditionne
                            le reste du 4.1 (POST /jobs, SSE)
```

Seule parallélisation utile : les contrats de capacité (3.1) et la constitution des
golden sets (5.1) sont de la rédaction, faisables en avance pendant les jalons
précédents. Les deux sont faites. Tout le reste est séquentiel — c'est voulu,
chaque jalon valide les fondations du suivant.

## Points de contrôle

- Fin de chaque jalon : relire le périmètre exclu (§11 de l'architecture) — le risque
  « dérive vers un clone de ComfyUI » se combat là.
- Fin v0.1 : le test d'existence. Fin v0.4 : l'outil a-t-il remplacé les scripts ?
  Sinon, corriger l'Atelier avant d'investir dans l'évaluation.
- Après trois cycles de veille (post-v0.6) : réajuster les pondérations du score.
- ~~**Recalibrer `heavy_threshold_bytes`**~~ — **fait le 20 août 2026**, avant le
  v0.4 : les quatre modèles mesurés dépassaient les 6 Go du seuil hérité de
  l'architecture, donc aucun ne cohabitait avec un autre alors que la voix
  (7,65 Gio) et la lecture de document (6,25) tiennent ensemble dans les 17,76
  disponibles. Seuil porté à **8 Gio**, des deux côtés — `Config` et
  `admission.DEFAULT_HEAVY_THRESHOLD`, avec un test qui vérifie qu'ils ne
  divergent pas — et inscrit en clair dans `~/.ecurie/config.toml`.

## Ce que le v0.3 a appris, et qui vaut pour la suite

Quatre leçons de la première exécution réelle, à garder en tête pour les jalons
suivants :

1. **Un adaptateur non exécuté est un adaptateur faux.** Les trois écrits au v0.3
   avaient chacun un défaut sérieux, invisible aux tests : poids fp16 non
   chargés faute d'un kwarg, pic mémoire faux d'un facteur 38 parce que le RSS ne
   compte pas la mémoire Metal, dépendance manquante que l'amont ne déclare pas.
   Tous découverts au premier lancement. Corollaire pour le v0.7 : ne pas croire
   `runtimes/hunyuan3d/run.py` avant de l'avoir vu tourner.
2. **Une mesure vaut mieux qu'une hypothèse d'architecture.** Le seuil de 6 Go,
   la note du manifeste TTS (« reste résident sans entamer le budget des
   lourds »), la sémantique du RSS sur Apple Silicon : trois affirmations
   écrites avant mesure, trois démenties.
3. **Le contrat de capacité est le bon endroit pour absorber la diversité.**
   Ajouter `lyrics` a suffi à accueillir la génération de chanson ; ajouter
   `options:` a suffi à accueillir les réglages propres à un moteur. Aucun des
   deux n'a demandé de toucher au superviseur.
4. **Ce que le modèle de données ne sait pas dire finit par se payer.** Un pic
   unique par variant a tenu pour trois modèles et cassé sur le quatrième. La
   question à se poser au v0.5 et au v0.7 : quelle autre grandeur dépend de
   l'entrée sans que le registre puisse l'exprimer ?

## Prochain pas

Tâche **4.3** : le socle de l'UI — React + Vite + RJSF —, puis **4.4**, l'écran
Atelier. Tout ce qu'ils demandent au serveur existe : le contrat de capacité rend
le formulaire, `output_media_types` choisit le visualiseur, `/runtime/residents`
alimente le bandeau de ressources, et `/runtime/admission` chiffre le coût d'une
saisie en cours. Rien de tout cela ne demande un modèle de plus ni un
téléchargement.

Ce qui reste du 4.1 — `POST /jobs` et le flux SSE — attend délibérément le
**4.6**, le déménagement du superviseur dans le processus de l'API. L'écrire
avant reviendrait à faire vivre l'occupation des résidents dans un fichier
verrouillé pendant toute la durée d'un job, puis à le défaire.

Faisable en parallèle, sans dépendance : la tâche **7.0** (éprouver Hunyuan3D),
seul risque non levé du projet — `runtimes/hunyuan3d/run.py` est écrit d'après le
source amont et n'a jamais tourné, alors que le v0.7 entier repose dessus. Les
trois adaptateurs écrits « proprement » au v0.3 avaient chacun un défaut sérieux
au premier lancement ; celui-ci n'a aucune raison de faire exception, et mieux
vaut le savoir avant d'avoir bâti la composition.

Restent aussi les douze enregistrements du golden set ASR (**5.1**), une
demi-heure de micro décrite dans `registry/evals/golden/speech-to-text/SOURCING.md`.
