# Écurie — Plan de réalisation

> Découpage en tâches des jalons du §12 de `ARCHITECTURE.md`, selon la conception de
> `CONCEPTION.md`. Chaque jalon a un critère de sortie unique et vérifiable ; on ne
> passe pas au suivant sans lui.
>
> Rappel du test d'existence du projet : **si le v0.1 ne sert pas dans la semaine qui
> suit sa livraison, on arrête et on réévalue.**

## État au 21 août 2026

| Jalon | État | Critère de sortie |
|---|---|---|
| v0.1 — Voir le parc | **livré** | atteint |
| v0.2 — Récupérer des gigaoctets | **livré** | atteint |
| v0.3 — Exécuter sans OOM | **livré** | atteint : `run` TTS produit un wav, un job image décharge le TTS proprement, sans swap |
| v0.4 — Utilisable au quotidien | **en cours** | 4.1 : la surface de lecture d'`ecurie serve` répond sur le vrai parc. 4.3 : les 17 contrats engendrent leur formulaire, sans un formulaire écrit à la main. 4.4 : l'Atelier existe, avec son bandeau de ressources, et il est complet sauf son bouton *Lancer*. 4.6 : le superviseur vit dans le processus de l'API, et `residents.json` n'est plus qu'un miroir |
| v0.5 → v0.7 | à faire | — |

Le parc réel compte **dix capacités exécutables** sur dix-sept déclarées, douze
manifestes et treize variants, dont onze prêts. Sept environnements de runtime,
quatre paquets Python et un front, **749 tests Python et 231 tests de front**,
plus quatre essais sur le vrai parc et trois contre un vrai serveur, exclus par
défaut.

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
| 4.1 | ◑ FastAPI : registre, jobs + SSE, `store/summary`, résidents | `ecurie serve` sert les **lectures** : `/registry/capabilities`, `/registry/models`, `/store/summary`, `/runtime/residents`, plus `/runtime/admission` que le §4 réclamait. Les jobs et le SSE restent à écrire — ce qui les retenait, le superviseur hors du processus, est levé depuis le 4.6 |
| 4.2 | Bibliothèque côté serveur : manifeste de job complet, rejeu | reproductibilité effective |
| 4.3 | ✓ UI : socle React+Vite+RJSF, mapping `x-ui`, visualiseurs par media type | `apps/ui` : deux tables d'aiguillage totales, les **17 contrats** rendus par une suite qui les lit sur le disque, 145 tests. Le typage vient du serveur — schéma OpenAPI figé et fixtures du vrai registre, gardés par deux tests pytest |
| 4.4 | ◑ Écran **Atelier** (capacité → variant → formulaire → progression SSE → sortie) + bandeau de ressources | `src/ecrans/Atelier.tsx` : capacités groupées par ce qui marche, variant préselectionné sur le titulaire **exécutable**, formulaire engendré, chiffrage de l'entrée, sorties promises par le contrat. Bandeau permanent sondé toutes les 2 s. **La progression SSE et la sortie réelle attendent la route des jobs**, faute de job à suivre |
| 4.5 | Écran **Parc** (trois chiffres, arbre de duplication, plan de GC dry-run, tiering) | parité avec la CLI |
| 4.6 | ✓ Le superviseur passe dans le processus de l'API : l'occupation des résidents cesse d'être un pid dans un fichier verrouillé et redevient un état en mémoire, et deux jobs sur un même worker se sérialisent au lieu d'attendre dans le backlog du socket | Un superviseur par processus, un verrou par variant tenu de l'admission à la fin du job, l'occupation en mémoire. `residents.json` est écrit par chacun, lu pour les autres. `ecurie unload` refuse un job en cours, `health()` ne fait plus la queue derrière le sien, et l'attente se dit à qui la subit |
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
                         ├ 4.1 lectures ✓   4.3 socle UI ✓
                         │                  4.4 Atelier ✓ (sauf Lancer)
                         │                  4.6 superviseur dans l'API ✓
                         │
                         └ plus rien ne retient le reste du 4.1
                            (POST /jobs, SSE), la fin du 4.4 (progression,
                            sortie réelle, résolveur de fichiers) ni la
                            tâche 4.2 — le superviseur sait désormais
                            qu'un job tourne, et lequel
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

### Ce que le socle de l'UI a appris

Le 4.3 a livré `apps/ui` : deux tables d'aiguillage — `x-ui` vers un widget,
`contentMediaType` vers un visualiseur —, la plomberie qui les relie aux sept
routes de lecture, et 145 tests. Quatre choses valent d'être retenues, aucune ne
se lisait dans le plan :

1. **Un front non exécuté est un front faux**, exactement comme un adaptateur.
   Les 117 tests en jsdom passaient au vert quand le premier essai contre un
   vrai `ecurie serve` a figé le navigateur : les avis de compilation étaient
   remontés *pendant* le rendu, ce qu'aucun test ne faisait faute de passer le
   rappel. Le correctif durable est le test qui garde ce cas ; c'est l'essai réel
   qui l'a révélé, et il reste dans le dépôt, exclu par défaut comme le marqueur
   `real` de pytest.
2. **Le méta-schéma en savait plus que la conception.** Le §7 nommait trois
   valeurs de `x-ui`, il y en a cinq ; il énumérait cinq types de média, il en
   manquait un — `application/json`, que produit `tool-use.calls`, une sortie
   **requise**. Écrire les tables d'après la prose plutôt que d'après
   `capability.schema.json` aurait cassé la capacité la plus riche du parc.
3. **Ce que RJSF fait vraiment ne se devine pas.** Quatre comportements ont été
   mesurés avant d'écrire : les enums encodés par index (`44100` reste un
   entier), le `<select>` réduit à une option vide sans `enum`, le fichier encodé
   en data-URL, et le `type: object` sans `properties` rendu comme un fieldset
   **vide** — sur un champ requis. Chacun aurait donné un formulaire qui paraît
   complet et ne l'est pas.
4. **Un instantané committé se périme en une demi-heure.** Les fixtures capturées
   du vrai registre avaient déjà divergé quand la garde a été écrite. Elle vit
   dans la suite pytest, comme celle du schéma OpenAPI : c'est celui qui édite
   `registry/` qu'il faut avertir, et il ne lance pas `npm test`.

### Ce que l'écran Atelier a appris

Le 4.4 a livré `src/ecrans/Atelier.tsx`, `src/ressources/BandeauRessources.tsx` et
le sondage qui l'alimente. `App.tsx` n'est plus qu'une coquille sans état. Quatre
choses valent d'être retenues, et trois n'ont été vues qu'en exécutant.

1. **Un refus ne se lisait pas, et le défaut datait du v0.3.** Le §4 de ce plan
   exige « ce morceau de 30 s demanderait 24,2 Gio » ; ce que `plan_admission`
   produisait était « demande 25704234348 octets, le budget entier est de
   19070000000 » — rendu tel quel par `ecurie ps --for` depuis deux jalons.
   Personne ne l'avait vu parce que les tests comparaient la phrase à
   `str(20 * GIB)`, c'est-à-dire au calcul qu'ils étaient censés vérifier : **un
   test qui recalcule ce qu'il contrôle ne contrôle rien.**
2. **La mémoire se comptait en unités de disque.** `fmt_bytes` du store est
   décimal, ce qui est juste pour un disque et faux pour un budget écrit
   `8 * (1 << 30)` : le seuil de lourdeur s'affichait « 8.59 Go », un chiffre qui
   n'apparaît ni dans `admission.py`, ni dans `Config`, ni dans
   `~/.ecurie/config.toml`. D'où `ecurie_core.format.fmt_memory`, binaire, pour
   la mémoire seule — deux domaines, deux conventions, dites une fois.
3. **Le même chiffre s'affichait deux fois.** Le bandeau chiffre le *variant* par
   `?for=`, le bouton chiffre l'*entrée* ; pour douze variants sur treize le pic
   ne dépend pas de l'entrée, donc les deux rendent mot pour mot la même phrase
   dans le même écran. Les fixtures ne le montraient pas — elles ne remplissaient
   pas `admission` —, seul l'essai contre un vrai serveur l'a fait. La leçon
   dépasse l'intitulé qui les sépare : **la tâche 4.7 n'est pas un raffinement du
   bandeau**, c'est ce qui rend le second chiffre utile.
4. **Garder les dernières données devient faux quand l'écran change de sujet.**
   `useResource` ne vide pas ses données pendant qu'il recharge, pour ne pas
   faire clignoter l'écran ; entre le clic sur une nouvelle capacité et l'arrivée
   de ses modèles, la liste des variants est donc encore celle de la précédente.
   La préselection y cherchait la bonne référence, ne la trouvait pas, et posait
   un formulaire sans les défauts du manifeste — `voice` vide là où
   `qwen3-tts-1.7b` déclare `serena`.
5. **Ce qui ne se rafraîchit pas se voit à l'usage, pas à la lecture.** Le
   bandeau devait sonder `/runtime/residents` et le formulaire s'en servir pour
   ses `x-options-from` : deux lectures de la même route, dont une seule
   sondait. Les voix d'un modèle chargé après l'ouverture de l'écran ne
   seraient jamais apparues — et c'est le moment exact où l'on veut les voir,
   puisqu'elles n'existent qu'après le premier chargement. Un seul sondage, tenu
   par l'écran et passé au bandeau : deux fois moins de requêtes, et le défaut
   disparaît avec le doublon.

Un écart au plan, assumé : **le résolveur de fichiers de sortie n'a pas été
écrit.** Le point d'injection reste `NO_FILE`. Ce n'est pas l'effort — c'est une
ligne — mais qu'on ne peut pas l'écrire juste : la forme de
`GET /jobs/{id}/files/{name}` se décide avec la route des jobs, qui a un vrai
choix à faire entre un nom de fichier et un chemin à plusieurs segments,
`audio-separation` produisant des sorties imbriquées. Écrite avant, elle serait
réécrite après, et aucun test n'aurait pu dire laquelle des deux formes est la
bonne. À la place, l'écran annonce ce que le contrat promet de produire, sans
prétendre l'avoir produit.

### Ce que le déménagement du superviseur a appris

Le 4.6 a donné au superviseur la durée de vie de son processus, un verrou par
variant, et fait de `residents.json` ce que chacun publie pour les autres. Cinq
choses valent d'être retenues, et quatre ne se lisaient nulle part.

1. **Le défaut visé ne pouvait pas se rencontrer avant qu'un serveur existe.**
   L'occupation était le pid du processus détenteur : un chiffre par processus,
   ce qui suffit tant qu'un processus ne tient qu'un job — le cas d'une commande,
   jamais celui d'un serveur. Deux jobs y inscrivaient le même pid, et le premier
   à finir l'effaçait : le worker redevenait évinçable alors qu'une inférence
   tournait dessus. Le test qui le décrit ne pouvait pas s'écrire avant, faute
   d'un mot pour distinguer deux jobs du même processus.
2. **Le worker a tranché une décision qu'aucun plan ne posait.** Il écoute une
   connexion à la fois. Garder la sienne ouverte entre deux jobs — l'optimisation
   évidente pour un superviseur qui dure — aurait privé tout autre processus de
   l'accès au modèle **et** neutralisé son délai d'inactivité, qui se compte dans
   l'attente d'une connexion : un modèle chargé une fois n'aurait plus jamais été
   rendu de lui-même. Une connexion par job, donc, et le verrou en amont. La
   conception se lisait dans `workers/base.py`, pas dans l'intitulé de la tâche.
3. **Trois défauts du v0.3 sont tombés avec, et pour la même raison** : ils
   demandaient de savoir qu'un job tourne. `ecurie unload` tuait un worker en
   pleine inférence sans un mot ; `ecurie bench` faisait de même, puisqu'il vide
   le parc avant de mesurer — une épingle est une préférence, un job est un
   travail, et seul le premier se passe outre ; `health()` ouvrait une seconde
   connexion sur le socket d'un worker que le processus occupait lui-même, et
   attendait dix secondes pour apprendre ce que sa mémoire disait déjà. Éprouvé
   contre un vrai serveur : « qwen3-tts-1.7b@8bit-mlx : un job est en cours
   dessus (pid 65586) — le décharger détruirait ce travail ». Aucun des trois
   n'était un défaut tant qu'une commande tenait seule le parc.
4. **Une attente qui ne se dit pas est indiscernable d'un blocage.** Le tour de
   rôle fait attendre, c'est son objet ; sans un mot, un `ecurie run` lancé
   pendant un job de l'Atelier paraît avoir cessé de répondre. Trois garde-fous,
   et aucun n'était au plan : l'attente s'annonce en nommant le job qui précède,
   elle est bornée par la durée d'un job entier — au-delà, c'est un bail qu'on
   n'a pas rendu —, et un fil qui redemanderait son propre worker se le voit dire
   au lieu de figer le serveur sans une ligne de journal.
5. **Un essai réel que personne ne lance se périme en silence.**
   `test_un_job_lourd_decharge_le_tts_sans_swap` était rouge depuis l'arrivée du
   profil paramétré : il prenait « le premier variant lourd » du registre et
   tombait sur la musique à 23,94 Gio, que rien n'admet jamais puisqu'elle
   dépasse le budget entier. Le refus était juste, le test faux. Ces quatre
   essais sont hors CI par construction — Apple Silicon, poids téléchargés, venv
   synchronisé — et c'est le prix de ce qu'ils prouvent : ils se lancent à la
   main, donc à chaque fin de jalon.

## Prochain pas

Le reste de la tâche **4.1** : `POST /jobs`, `GET /jobs/{id}`, son flux **SSE** et
`GET /jobs/{id}/files/{name}`. Plus rien ne le retient — le superviseur sait
maintenant qu'un job tourne, lequel, et fait attendre le suivant. C'est ce qui
donne son bouton *Lancer* à l'Atelier (fin du 4.4) et ce dont la Bibliothèque
(4.2) a besoin. La première décision à prendre y est celle qu'on a refusé de
prendre au 4.4 : un nom de fichier ou un chemin à plusieurs segments, sachant
qu'`audio-separation` produit des sorties imbriquées.

L'écran **Parc** (4.5) est faisable en parallèle sans rien attendre : la route
`/store/summary` répond déjà, et le bandeau de ressources lui est réutilisable
tel quel. C'est aussi le moment où la coquille gagnera sa navigation — au 4.4 un
onglet unique aurait été un décor.

Deux dettes que le socle rend visibles, et qu'aucune tâche ne porte : **aucun des
102 champs du registre n'a de `title`** — l'étiquette affichée est la clé du
contrat, et la bonne place du français est le JSON, pas une table de traduction
dans le front —, et **aucune route de téléversement** n'existe pour les dix
champs fichier, ce qui est sans conséquence tant que le navigateur et le serveur
partagent la machine.

Faisable en parallèle, sans dépendance : la tâche **7.0** (éprouver Hunyuan3D),
seul risque non levé du projet — `runtimes/hunyuan3d/run.py` est écrit d'après le
source amont et n'a jamais tourné, alors que le v0.7 entier repose dessus. Les
trois adaptateurs écrits « proprement » au v0.3 avaient chacun un défaut sérieux
au premier lancement ; celui-ci n'a aucune raison de faire exception, et mieux
vaut le savoir avant d'avoir bâti la composition.

Restent aussi les douze enregistrements du golden set ASR (**5.1**), une
demi-heure de micro décrite dans `registry/evals/golden/speech-to-text/SOURCING.md`.
