# Écurie — Plan de réalisation

> Découpage en tâches des jalons du §12 de `ARCHITECTURE.md`, selon la conception de
> `CONCEPTION.md`. Chaque jalon a un critère de sortie unique et vérifiable ; on ne
> passe pas au suivant sans lui.
>
> Rappel du test d'existence du projet : **si le v0.1 ne sert pas dans la semaine qui
> suit sa livraison, on arrête et on réévalue.**

## État au 22 août 2026

| Jalon | État | Critère de sortie |
|---|---|---|
| v0.1 — Voir le parc | **livré** | atteint |
| v0.2 — Récupérer des gigaoctets | **livré** | atteint |
| v0.3 — Exécuter sans OOM | **livré** | atteint : `run` TTS produit un wav, un job image décharge le TTS proprement, sans swap |
| v0.4 — Utilisable au quotidien | **en cours** | 4.1 : `ecurie serve` sert le parc **et lance ses jobs** — un POST, un flux SSE, un wav téléchargeable, éprouvés sur le vrai parc. 4.3 : les contrats engendrent leur formulaire — 25 aujourd'hui —, sans un formulaire écrit à la main, et un champ fichier se remplit au chemin, au sélecteur ou à la caméra. 4.4 : l'Atelier lance, suit et montre — du clic au wav qu'on écoute, éprouvé contre un vrai serveur. 4.5 : le Parc met la comptabilité disque à l'écran — trois chiffres, duplications, plan de GC à blanc, tiering —, et la coquille gagne sa navigation. 4.6 : le superviseur vit dans le processus de l'API, et `residents.json` n'est plus qu'un miroir |
| v0.5 → v0.7 | à faire | — |

Le parc réel compte **vingt capacités exécutables** sur vingt-cinq déclarées, et
**chacune des vingt-cinq a au moins un modèle** — vingt-six manifestes,
vingt-sept variants. Huit environnements de runtime, quatre paquets Python et un front,
**845 tests Python et 404 tests de front**, plus cinq essais sur le vrai parc et
huit contre un vrai serveur, exclus par défaut.

Deux choses ont été faites en marge du jalon, et elles n'attendaient personne :
le **recalibrage du seuil de lourdeur** (voir les points de contrôle), et la
**rédaction des golden sets** de la tâche 5.1, qui est du travail de fond dont le
v0.5 dépend entièrement.

### Le registre au complet, et trois façons de remplir un champ fichier — le 22 août 2026

Trois demandes en une, et elles se sont révélées liées par un même bout : *« un
modèle pour chaque capacité »*, *« utiliser un stream de la caméra ou du micro »*,
*« choisir une image dans le site web »*.

**Les six dernières capacités sans modèle en ont un.** `audio-denoise`,
`audio-separation`, `image-to-image`, `image-to-video`, `speech-to-text`,
`text-to-video` — six manifestes, deux adaptateurs, deux charges type, un test du
registre réel qui exige l'invariant (`test_chaque_capacite_a_au_moins_un_modele`).
Le parc passe de dix-huit à **vingt capacités exécutables**, et le dropdown de
l'Atelier perd son groupe « Aucun modèle au registre » — il ne reste que
« Exécutables » et « Déclarées, rien d'exécutable en l'état ».

Deux des six sont exécutables **le jour de leur ajout, sans un octet
téléchargé** : `sdxl-base-img2img` tourne sur les poids de `sdxl-base`,
`moss-transcribe` sur ceux de `moss-transcribe-diarize`. C'est la cinquième et la
sixième fois que le partage de poids paie dans ce parc, et la question à poser
devant une capacité vide est désormais « lequel des poids déjà là sait le
faire ? » avant « lequel télécharger ? ».

Les quatre autres sont téléchargées mais pas encore servies : DeepFilterNet3-MLX
(8,7 Mo) et HTDemucs-MLX (168 Mo) attendent leur adaptateur, LTX-Video 2B (15,2 Go
en bf16) attend surtout une machine — le calcul est fait avant le téléchargement
et écrit dans ses caveats : 15,2 Go de poids pour 17,76 Gio de budget, le contrôle
d'admission refusera. **Le manifeste existe quand même**, et c'est le point : une
capacité vide n'apprend rien, un candidat avec son pic annoncé dit exactement où
est le mur.

**`POST /uploads` referme une note vieille de trois tâches.** La liste des routes
figées portait ceci : « aucune route de téléversement, alors que dix champs du
registre attendent un fichier. Sans conséquence tant que le navigateur et le
serveur partagent la machine — le champ porte un chemin local — et à reprendre le
jour où ce ne sera plus vrai. » Ce jour n'est pas venu et la route existe :
**ce n'est pas le partage de machine qui a cessé d'être vrai, c'est le
raisonnement.** Une image choisie dans une page, une photo de la caméra, un son
du micro n'ont jamais eu de chemin à saisir, sur aucune machine. Le champ
`x-ui: "file"` a maintenant trois sources — le chemin saisi, un fichier du disque
(le sélecteur natif n'est plus inerte, et le champ accepte le glisser-déposer et
le collage), la caméra ou le micro — et les trois finissent par poser la même
chose : un chemin local que le worker ouvrira.

Le glisser-déposer est ce qui répond le plus directement à « choisir une image
dans le site web » : le navigateur télécharge lui-même l'image lâchée depuis un
onglet et la présente comme un fichier. Quand il ne le fait pas — certaines
sources ne donnent qu'une URL —, rien ne se passe, et c'est délibéré : suivre
l'URL à sa place demanderait au **serveur** de sortir sur le réseau, ce qu'un
parc local n'a aucune raison de faire.

Ce que l'exercice a appris :

1. **Le navigateur n'écrit pas de WAV, et le parc ne lit rien d'autre.**
   `MediaRecorder` rend de l'opus sur Chrome, de l'AAC sur Safari ; le
   `pyproject.toml` de l'env `mlx-audio` dit que ffmpeg « ne redeviendrait
   nécessaire que pour flac/mp3/ogg/opus ». Un dépôt en opus aurait produit un
   job qui échoue au décodage, plusieurs secondes après le clic. La conversion
   passe par `decodeAudioData` — le navigateur relit ce qu'il vient d'encoder,
   c'est le même moteur — puis par un en-tête de 44 octets écrit à la main. Le
   piège du format WAV est son petit-boutisme : une fréquence de 48000 écrite à
   l'envers se lit 130 048 512, et le fichier s'ouvre quand même.
2. **Le seul défaut qui survit à l'écran est une caméra restée allumée.** React
   ne coupe rien tout seul. Changer de mode, fermer le panneau, démonter l'écran,
   échouer à déposer : quatre chemins, un seul `stop()` à ne pas oublier.
3. **`mimetypes` ne sait pas suffixer `audio/wav`.** Le type canonique de l'IANA
   est `audio/vnd.wave`, et aucune table de macOS ne fait le lien — or c'est
   précisément ce que produit la capture du micro. Le même trou que
   `model/gltf-binary` côté sorties, découvert de la même façon : par un test qui
   demandait une extension et n'en a pas eu.
4. **`strength` décide du nombre de pas, et zéro pas est un job réussi qui n'a
   rien fait.** `diffusers` calcule `int(steps × strength)` : à 0,6, un job de 30
   pas n'en exécute que 18 — d'où un pic de 7,9 Gio contre 15,95 pour la
   génération, ce qui fait de `sdxl-base-img2img` le seul des trois chemins SDXL
   que la politique mémoire ne compte pas comme lourd. Mais le pipeline accepte
   aussi zéro et rend l'image d'entrée sans lever ni avertir. Un plancher à un
   pas est posé dans l'adaptateur.
5. **Un refus coûteux et un refus gratuit ne se traitent pas pareil.**
   `moss-transcribe` refuse `task: "translate"` — rendre une transcription sous
   le nom d'une traduction serait une sortie qui n'est pas celle qu'on a demandée
   — mais se contente d'inscrire au manifeste que `beam_size` et
   `word_timestamps` n'ont rien changé. Le premier refus part avant même de
   toucher au disque : l'ordre des vérifications est celui du coût.
6. **Le banc d'essai vérifie qu'un fichier de sortie existe, pas ce qu'il
   contient.** `moss-transcribe` a passé ses trois cas au vert en livrant
   « `[1.28][S01] Je peux faire glisser.[9.21]` » dans un fichier que le contrat
   annonce en texte brut : le modèle préfixe chaque segment de son identifiant de
   locuteur et intercale les bornes. C'est exactement la frontière entre les deux
   capacités que ces poids servent — l'une rend ce qui est dit, l'autre qui l'a
   dit — et seul un job réel, dont on a **lu la sortie**, l'a montrée. Le §8 dit
   que le banc mesure un coût et non une qualité ; il faut y ajouter qu'il ne
   regarde même pas la forme.
7. **Les deux adaptateurs écrits ce jour-là ont *démarré* du premier coup**, ce
   qui n'est pas la même chose que d'avoir produit la bonne sortie — le point
   précédent le montre. C'est tout de même la première fois depuis le v0.3
   qu'aucun ne meurt au chargement, et l'explication n'est pas l'expérience : ce
   sont les deux qui réemploient des poids déjà mesurés, par des pipelines dont
   les voisins avaient déjà payé les surprises — le `variant="fp16"` des
   `allow_patterns`, le `chat_template.jinja` qui exige jinja2. Le taux d'échec
   au premier lancement mesure la nouveauté de l'amont, pas la qualité du code
   qu'on écrit.

### Huit capacités de plus, hors jalon — le 22 août 2026

Ajoutées d'un coup : `video-to-text`, `video-to-motion`, `image-inpaint`,
`image-detect`, `image-segment`, `audio-to-text`, `speaker-diarization`,
`voice-clone`. Le parc passe de dix-sept à **vingt-cinq capacités déclarées** et
de dix à dix-huit exécutables, avec un runtime de plus (`rtmlib`), huit
adaptateurs, huit manifestes et huit charges type.

Le point de départ n'était pas un modèle mais un **trou de forme** : aucune des
dix-sept capacités n'acceptait de vidéo en entrée. Le parc en produisait deux et
n'en lisait aucune.

Ce que l'exercice a appris, et qui ne se lisait dans aucun plan :

1. **Le parc avait un angle mort : ses propres environnements.** Trois des huit
   capacités sont servies par des modèles que `runtimes/mlx-audio/.venv`
   embarquait déjà — `qwen2_audio`, `moss_transcribe_diarize`, `omnivoice` — sans
   qu'aucun manifeste ne les déclare. Le skill de veille balaye Hugging Face et
   les dépôts amont ; il ne regarde pas ce que les bibliothèques synchronisées
   savent faire. Cinq des huit capacités n'ont demandé **aucun octet** de plus ou
   moins de 400 Mo.
2. **Un chemin « natif » annoncé peut ne rien transmettre.** `mlx-vlm` déclare
   Qwen3-VL parmi les modèles à entrée vidéo native, l'invite composée porte bien
   son jeton de remplissage — et le modèle décrit une scène figée, en niant tout
   mouvement, sur une vidéo où un cube traverse le cadre. Trois réponses
   identiques au caractère près pour trois budgets d'images différents : le banc
   le montrait sans le dire. L'adaptateur décode donc lui-même.
3. **Plus d'images ne donne pas une meilleure réponse.** Quatre images décrivent
   juste deux mouvements sur trois ; seize se trompent sur les trois. Le contraire
   de ce qu'on aurait réglé sans mesurer.
4. **Le score d'un modèle peut être juste et notre lecture fausse.** SAM 2.1 note
   la face avant d'un cube (0,879) au-dessus du solide entier : ce score est une
   sortie entraînée, il n'a pas tort. C'est garder seulement le premier masque qui
   aurait eu tort — d'où la sortie `candidates`.
5. **Deux profils paramétrés committables**, les premiers depuis la musique :
   R² = 0,9982 pour la diarisation sur la durée écoutée, R² = 0,9995 pour la pose
   3D sur la cadence. Le §3 de la conception avait raison de les prévoir, et la
   tâche 6.5 aura de quoi mordre.
6. **Cinq des huit adaptateurs ont échoué à leur premier lancement**, comme les
   trois du v0.3 et les quatre du lot suivant. Sur des hypothèses écrites avant
   mesure : un `variant="fp16"` oublié, deux dimensions de tenseur prises l'une
   pour l'autre, un `audio_duration` qui est un horodatage et non un nombre, une
   dépendance `jinja2` que personne ne déclare, un chemin vidéo qui ne transmet
   rien. Le taux ne baisse pas avec l'expérience — c'est le premier lancement qui
   les trouve, pas la relecture.

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
| 4.1 | ✓ FastAPI : registre, jobs + SSE, `store/summary`, résidents | Les **lectures** (`/registry/*`, `/store/summary`, `/runtime/residents`, `/runtime/admission`), puis les **jobs** : `POST /jobs` rend 202 et un identifiant, `GET /jobs/{id}` l'état et son manifeste, `/events` un flux SSE qui rejoue depuis le début et se termine par un `end`, `/files/{chemin}` les fichiers — avec l'URL composée par le serveur et le type de média que le contrat promettait |
| 4.2 | Bibliothèque côté serveur : manifeste de job complet, rejeu | reproductibilité effective |
| 4.3 | ✓ UI : socle React+Vite+RJSF, mapping `x-ui`, visualiseurs par media type | `apps/ui` : deux tables d'aiguillage totales, les **25 contrats** rendus par une suite qui les lit sur le disque. Le typage vient du serveur — schéma OpenAPI figé et fixtures du vrai registre, gardés par deux tests pytest. Le champ fichier a rattrapé son manque de 2026-08-22 : `POST /uploads` lui donne un chemin réel, et ses trois sources sont le clavier, le disque et le matériel (`src/media/`) |
| 4.4 | ✓ Écran **Atelier** (capacité → variant → formulaire → progression SSE → sortie) + bandeau de ressources | `src/ecrans/Atelier.tsx` : capacités groupées par ce qui marche, variant préselectionné sur le titulaire **exécutable**, formulaire engendré, chiffrage de l'entrée, bandeau permanent sondé toutes les 2 s. Puis le **lancement** : un bouton qui n'est jamais grisé pour un variant qu'on croit incapable, un flux d'événements lu par `fetch`, une progression, un bilan qui dit ce qui a été déchargé, et la sortie réelle servie par le résolveur de fichiers. Éprouvé contre un vrai `ecurie serve` : un wav de 2,48 s produit, téléchargé et lu dans l'écran qui l'a demandé |
| 4.5 | ✓ Écran **Parc** (trois chiffres, arbre de duplication, plan de GC dry-run, tiering) | `src/ecrans/Parc.tsx` et `src/parc/` sur trois lectures : `/store/summary` déjà là, plus `GET /store/plan` — le plan **entier**, jamais écrit — et `GET /store/tiering` — volumes déclarés, variants déjà froids, et l'empreinte disque de chaque variant, que `footprints()` calcule. Parité avec la CLI, y compris l'unité : **Go décimaux** pour le disque, quand tout le reste de l'UI compte en Gio. Rien n'y touche au disque : chaque décision rend la commande qui l'exécute. La coquille gagne sa navigation, un `useState` et deux boutons |
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
                         ├ 4.1 serveur ✓    4.3 socle UI ✓
                         │  (lectures,      4.4 Atelier ✓ (lancement compris)
                         │   puis jobs)     4.5 Parc ✓ (et la navigation)
                         │                  4.6 superviseur dans l'API ✓
                         │
                         └ restent 4.2 (Bibliothèque et rejeu, qui s'appuie
                            sur le manifeste déjà écrit par chaque job) et
                            4.7 (le bandeau chiffre l'entrée en cours
                            de saisie)
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

Un écart au plan, assumé sur le moment et **réglé depuis** : le résolveur de
fichiers de sortie n'avait pas été écrit, le point d'injection restait `NO_FILE`.
Ce n'était pas l'effort — c'est une ligne — mais qu'on ne pouvait pas l'écrire
juste : la forme de `GET /jobs/{id}/files/…` se décidait avec la route des jobs,
qui avait un vrai choix à faire entre un nom de fichier et un chemin à plusieurs
segments, `audio-separation` produisant des sorties imbriquées. L'attente était
le bon calcul : la route a choisi le chemin **et** de composer l'URL elle-même,
si bien que le résolveur écrit après n'est plus une construction mais une lecture
dans `files` — moins de code qu'aucune des deux versions qu'on aurait écrites
avant.

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

### Ce que la route des jobs a tranché

Le reste du 4.1 a livré `POST /jobs`, `GET /jobs/{id}`, le flux SSE et les
fichiers de sortie. Quatre points, dont trois étaient des questions ouvertes.

1. **Le serveur compose l'URL des fichiers ; le client ne la fabrique pas.**
   C'était la décision que le 4.4 avait refusé de prendre faute d'un endroit où
   la prendre — un nom de fichier, ou un chemin à plusieurs segments ? Les deux
   existent, `audio-separation` rendant `tracks/vocals.wav` sous la clé pointée
   `tracks.vocals`. La route accepte donc un chemin, et la réponse porte `files`
   déjà composé : le résolveur du front devient une lecture, pas une
   construction. Le type de média suit le même principe — celui que **le
   contrat** promettait, lu dans le manifeste, parce qu'un `.glb` est un
   `model/gltf-binary` et qu'aucune table système ne le dit.
2. **Ce qui se refuse avant, et ce qui ne peut se refuser qu'après.** Un modèle
   inconnu, un variant que le disque contredit, une entrée hors contrat : pas de
   job, un code et la commande qui répare. L'admission, elle, ne se tranche
   qu'au moment de charger — d'ici là un autre job a pu libérer la place — donc
   le job existe et échoue en portant la phrase du contrôle d'admission. Elle
   est préfixée « admission refusée : » plutôt que du nom de la classe : un refus
   est une décision, pas une panne, et « AdmissionRefused » aurait été le seul
   mot anglais de tout le parcours.
3. **Un fil par job, pas un pool.** Un pool borné ferait attendre un job sur un
   modèle libre derrière deux jobs en file sur un modèle occupé : le backlog
   qu'on venait de retirer du socket, réintroduit un cran plus haut. La
   sérialisation a son endroit — le tour de rôle par variant — et ce qui reste
   ici n'est qu'un plafond de nombre.
4. **Un test a failli tuer le processus qui le lançait.** Le nettoyage des
   fixtures déchargeait les résidents ; or celles de l'API en fabriquent avec le
   pid du test — délibérément, pour qu'ils soient vus vivants. `_evict` a donc
   envoyé un SIGTERM à pytest, et la suite s'arrêtait au milieu sans un mot.
   Le correctif vaut pour la production : **un worker est toujours un autre
   processus**, et notre propre pid dans le registre des résidents ne peut venir
   que d'un fichier corrompu ou d'un pid recyclé. Le serveur aurait disparu en
   voulant faire de la place.

Éprouvé contre un vrai `ecurie serve` : un POST, un flux qui va de `queued` à
`done` en passant par la progression du worker, un wav de 3,36 s téléchargé en
`audio/wav`, deux jobs simultanés qui se suivent sur un seul worker, et une
demande de trente secondes de musique refusée par « demande 24,21 Gio, le budget
entier est de 17,76 Gio » — sans que le TTS résident soit touché.

### Ce que brancher le bouton *Lancer* a appris

La fin du 4.4 a donné à l'Atelier `useJob`, un analyseur de flux, un panneau de
job et le résolveur de fichiers que le 4.3 avait laissé en `NO_FILE`. Sept
choses valent d'être retenues, et deux touchent le serveur plutôt que le front —
c'est le propre d'un client : il découvre ce que l'API ne dit pas.

1. **La conception nommait `EventSource`, et il ne pouvait pas servir.** Pas
   pour ses défauts propres, mais pour deux raisons de banc d'essai. Il n'existe
   pas en jsdom : la suite entière aurait tourné sur un double écrit pour
   l'occasion, c'est-à-dire qu'elle aurait éprouvé le double. Et il n'est pas
   `fetch` : le double de `vitest.setup.ts` refuse toute route non déclarée pour
   qu'un test ne parte pas frapper le `ecurie serve` qui tourne sur cette
   machine, et un `EventSource` serait passé à côté de ce filet. Le `end` que le
   serveur émet pour lui garde tout son sens ; l'analyseur SSE tient en trente
   lignes et se teste, lui, morceau par morceau.
2. **Le flux ne portait pas ce qu'il fallait afficher.** `JobOut` avait `outputs`
   — les sorties du contrat qui sont des fichiers — et pas `output`, la réponse
   du worker. Or on aplatit la réponse et non le contrat, et tout ce qui n'est
   pas un fichier n'existe que là : `page_count`, `language`, `call_names`. Un
   client qui n'écoute que le flux ne les aurait jamais vus. Le défaut était
   invisible côté serveur, où les tests lisent le manifeste.
3. **Une commande de réparation voyage désormais dans une erreur**, ce que le
   front affirmait impossible. Tant qu'on ne faisait que lire, un variant non
   exécutable était un état et ses blockers arrivaient dans une réponse 200 ; le
   demander à `POST /jobs` en fait un refus, et le 409 porte un `detail`
   **objet** que `JSON.stringify` rendait illisible au moment précis où il dit
   quoi taper.
4. **Un test écrit pour la forme a trouvé un vrai défaut.** L'analyseur cherchait
   `\n\n` ; un serveur en CRLF émet `\r\n\r\n`, où cette recherche ne trouve
   rien. Il n'aurait rendu aucun événement, sans une erreur pour le dire :
   l'écran serait resté figé sur « en file » pendant que le job finissait.
5. **Un essai réel change la machine qu'il éprouve.** Le job réel charge le
   modèle et le laisse résident ; le test d'admission du même fichier, écrit au
   4.4, attendait « lancer chargera 7,65 Gio » et lit maintenant « déjà
   résident ». Il passait au premier lancement et échouait au second, sans
   qu'une ligne ait bougé. C'est la même leçon que les cinq essais `real` de
   pytest : **l'état du parc fait partie des entrées d'un essai réel.**
6. **Le flux pouvait sauter son dernier événement**, et c'est le second défaut
   serveur que seul un client a pu révéler. `_flux` lisait le journal puis
   demandait si le job était terminé ; entre les deux, le fil du job avait le
   temps d'exécuter `finish()` en entier. La lecture rendait alors « rien de
   neuf », le test de terminaison concluait « plus rien à venir », et le `end`
   partait sans que l'état final soit passé. Les tests serveur ne pouvaient pas
   le voir — ils lisent le flux jusqu'au bout, hors concurrence. L'écran, lui,
   restait figé à 40 % avec un bouton grisé et une sortie jamais montrée. Lire
   l'état terminal **avant** le journal suffit, et le test qui garde la course la
   provoque à l'endroit exact où elle se produit.
7. **Une requête en vol n'a pas de bouton d'annulation, elle a une génération.**
   `AbortController` coupe le flux, mais pas le `POST` de la soumission ni le
   `GET` de la reprise. Les laisser poser leur résultat au retour ouvrait deux
   impasses trouvées en revue : un job soumis puis oublié — on change de
   capacité pendant que le `POST` vole — revenait bloquer *Lancer* **sans** son
   panneau, donc sans le bouton qui l'aurait retiré ; et un job retiré pendant
   une reprise réapparaissait tout seul. Le chiffrage avait la même faille là où
   le bandeau avait déjà sa garde. La règle vaut pour tout l'écran : **une
   réponse qui revient doit prouver qu'on lui a posé la question la plus
   récente.**

Éprouvé contre un vrai `ecurie serve`, depuis l'écran : un job TTS du clic au wav
— 2,48 s d'audio, `rtf` 0,45, fichier servi en `audio/wav` depuis le port de
l'API et chargé par la page servie par Vite —, et un refus d'admission arrivé
**par le flux** en `failed`, « admission refusée : minimax-music3@4bit demande
24.21 Gio, le budget entier est de 17.76 Gio : décharger ne changerait rien ».

### Ce que l'écran Parc a appris — la tâche 4.5

Le deuxième des quatre écrans, et le premier à parler de disque plutôt que de
mémoire. Il a demandé deux routes de plus, une fonction de plus dans
`ecurie_store`, et il a rendu la coquille navigable. Cinq points valent d'être
gardés, et trois n'ont été visibles que sur le vrai parc.

1. **Le verbe du plan était faux, et il l'était depuis l'origine.** La surface
   d'API du §6 portait `POST /store/plan`, par analogie juste avec la CLI :
   `ecurie store plan` **écrit un fichier**, parce que `ecurie store apply` en
   exige un. Mais l'écran pose une question, il ne demande pas un document — un
   `POST` par consultation déposerait un plan à chaque ouverture d'onglet. La
   route est un `GET`, elle rend le plan entier sans le poser nulle part, et
   `command` porte la commande qui l'écrit pour de bon. **La surface d'écriture
   du parc reste donc vide**, et le tiering le confirme : le §4.4 veut que
   l'outil laisse un `tier: cold` à committer, si bien qu'un bouton dans un
   navigateur laisserait le manifeste mentir jusqu'au prochain commit.

2. **Le front n'avait qu'une unité, et elle était fausse pour la moitié de ce
   qu'il affiche.** `formatBytes` rend des Gio binaires — juste pour un budget
   Metal, un pic, un seuil de lourdeur. La CLI du parc, elle, rend des Go
   décimaux, et ce n'est pas une négligence : un disque s'annonce et s'affiche en
   puissances de dix. Réutiliser la fonction existante aurait affiché
   « 43,4 Gio » là où `ecurie store status` dit « 46,58 Go », dans un écran dont
   la tâche demandait la parité avec la CLI. L'unité suit ce qu'on compte, pas le
   composant qui l'affiche.

3. **Le parc réel a déplacé le sujet de l'écran** : 46,58 Go apparents, 11,4 Mo
   récupérables. Le plan de GC de cette machine ne propose rien, et c'est une
   bonne nouvelle qu'aucune fixture n'aurait donnée. Ce que le même écran révèle
   est autrement utile : **14,29 Go, tout Ollama, ne sont rattachés à aucun
   variant du registre**, et 46,56 Go sur 46,58 portent un hash annoncé par leur
   gestionnaire et jamais relu. Le Parc est d'abord un outil de connaissance.

4. **D'où `--verified-only` en case à cocher plutôt qu'en option de CLI.** Sur ce
   parc, elle ramène le gain proposé de 11,4 Mo à zéro : l'unique duplication
   trouvée repose sur un nom de blob, pas sur un contenu relu. Ce n'est pas un
   réglage d'expert, c'est la différence entre « ce qu'on peut reprendre » et
   « ce qu'on croit pouvoir reprendre ».

5. **Décider *quoi* déporter demandait un chiffre que rien ne calculait.**
   `footprints()` rend deux nombres par variant, et leur écart est le sujet :
   `bytes` est ce qu'il occupe, `freed_bytes` ce que le volume rendrait — nuls
   l'un sans l'autre dès qu'un inode a une référence hors du parc scanné. Le
   parc réel a montré le second piège dans la foulée : **les mêmes 5,78 Go de
   Qwen3-VL servent deux variants**, la lecture de document et la description
   d'image. Chacun affiche son poids réel, la somme de la colonne dépasse le
   parc, et `shared_with` est ce qui empêche d'y voir une erreur de calcul.

6. **Une capture d'écran a trouvé deux défauts que 405 tests laissaient
   passer.** Les phrases portaient des accents graves autour des commandes — la
   convention des docstrings du dépôt — et le navigateur les affichait tels
   quels ; aucun test ne pouvait le voir, tous cherchant le texte par
   sous-chaîne. Sur la même image : les motifs d'écart du plan s'affichaient
   sous leur clé brute, « sans-sha256 », faute d'entrée dans `REASON_LABELS`.
   Les deux sont corrigés et gardés. **Une suite de tests vérifie ce qu'un écran
   dit, pas ce qu'il montre** — regarder la page une fois a coûté une minute et
   rapporté plus que la relecture du diff.

La navigation, elle, n'a rien coûté : un `useState` et deux boutons, ni routeur
ni URL. Ce que cela coûte se dit — recharger la page revient à l'Atelier — et la
question se reposera au quatrième écran. L'écran qu'on quitte est **démonté** :
le Parc classe tout le parc par contenu à chaque lecture, l'Atelier sonde la
mémoire toutes les deux secondes, et les garder tous deux montés ferait payer en
permanence celui qu'on ne regarde pas.

## Ce qui reste à faire

*Au 22 août 2026. Dix-huit tâches ouvertes sur quarante-quatre, plus quatre
chantiers qu'aucune tâche ne porte. Les tableaux des jalons font foi sur le détail ; cette
section dit ce qui n'est pas fait, ce qui bloque chacun, et dans quel ordre s'y
prendre.*

### v0.4 — deux tâches, aucune bloquée

| # | Tâche | Ce qu'il faut avant | Effort |
|---|---|---|---|
| **4.2** | **Bibliothèque côté serveur** : index des jobs, filtre, rejeu à partir du manifeste | rien — chaque job écrit déjà son manifeste complet, c'est la lecture qui manque | 2 j |
| **4.7** | Bandeau **calculé sur l'entrée en cours de saisie** | rien — `POST /runtime/admission` fait déjà parler `peak_scaling` | 1 j |

**4.7 est devenue utile en cours de route.** Tant que le job ne partait pas, un
pic qui suit la saisie était un raffinement. Depuis que le bouton *Lancer*
fonctionne, c'est ce qui dit **avant de cliquer** si le curseur qu'on vient de
bouger fera échouer le job — et le parc compte désormais des variants dont le pic
dépasse le budget à coup sûr.

**Critère de sortie du jalon** : une semaine d'usage réel où l'UI est le chemin
par défaut, sans retomber sur les scripts d'origine. Ce qui l'empêchait — la
comptabilité disque en ligne de commande seulement — a été levé par la 4.5 ; ce
qui reste est du temps, pas du code.

### v0.5 — six tâches, une commencée

| # | Tâche | Ce qu'il faut avant |
|---|---|---|
| **5.1** ◑ | Golden sets — **les douze enregistrements ASR** | une demi-heure de micro, décrite dans `registry/evals/golden/speech-to-text/SOURCING.md`. Les textes sont figés ; il manque le son. Devenu plus facile qu'hier : le champ audio accepte maintenant le micro |
| **5.2** | `ecurie eval` : WER, exactitude OCR → `evals/results/` | 5.1 complète pour l'ASR ; les autres jeux sont prêts |
| **5.3** | Exécution A/B — même entrée, deux variants, séquentielle sous admission | 5.2 |
| **5.4** | Écran **Confrontation** + `preferences.jsonl` + Elo dérivé | 5.3. La navigation de 4.5 est là : y ajouter un écran est une entrée dans un tableau |
| **5.5** | Écran **Bibliothèque** — le quatrième écran, plafond atteint | 4.2 |
| **5.6** | Promouvoir en `incumbent` les candidats qui l'ont mérité | 5.2 et 5.4. Vingt-quatre modèles sur vingt-six sont `candidate`, et seules `text-to-speech` et `image-to-mesh` ont un titulaire : **rien n'a jamais été comparé**, et la seconde n'a même pas ses poids |

### v0.6 — cinq tâches, aucune commencée

`registry-ci.yml` (6.1) est celle qui rapporte le plus vite : la validation
croisée `defaults:` ↔ contrat, la conformité pydantic ⇔ JSON Schema et
l'invariant « une capacité, un modèle » existent **déjà en tests** — il s'agit de
les câbler à GitHub Actions, pas de les écrire. Les quatre autres (6.2 à 6.5)
concernent la veille et la garde des pentes de pic.

### v0.7 — cinq tâches, et un préalable qui peut tout condamner

**7.0 — éprouver Hunyuan3D — est le seul risque non levé du projet, et il ne
dépend de rien.** `runtimes/hunyuan3d/run.py` est écrit d'après le source amont
et **n'a jamais tourné** ; les 7,37 Go de poids ne sont pas téléchargés ; le v0.7
entier repose dessus. Chacun des adaptateurs écrits « proprement » avant mesure a
eu un défaut sérieux au premier lancement — c'est la constante la mieux établie
de ce projet. Mieux vaut le savoir avant d'avoir bâti la composition, et 7.1 à
7.4 (contrat composite, exécuteur, point d'arrêt UI, manifeste composite) n'ont
pas de sens tant que 7.0 n'a pas rendu un maillage ou tranché pour un autre
modèle.

### Quatre chantiers qu'aucune tâche ne porte

**Cinq capacités sur vingt-cinq ne sont pas exécutables**, et chacune bute sur
autre chose :

| Capacité | Ce qui manque | Ce que ça coûte |
|---|---|---|
| `audio-denoise` | l'adaptateur. Poids là (8,7 Mo), env `mlx-audio` synchronisé | le DSP hors réseau — STFT, banc ERB, filtre profond — que le dépôt livre en constantes (`auxiliary.npz`) et en implémentation de référence (`dfn3_mlx.py`) |
| `audio-separation` | l'adaptateur, et `ecurie env sync mlx-audiogen`. Poids là (168 Mo), env déclaré | `DemucsPipeline.separate()` rend quatre pistes ; le contrat en veut deux ou quatre, et la somme des trois non vocales fait l'accompagnement |
| `text-to-video` | la **mesure** avant tout le reste. Poids là (15,2 Go) | le pic dépassera probablement les 17,76 Gio. `ecurie bench` le dira, et cette réponse-là décide s'il faut écrire un adaptateur ou changer de modèle |
| `image-to-video` | idem — mêmes octets, autre pipeline | |
| `image-to-mesh` | la tâche **7.0** | |

**Aucun des 169 champs d'entrée du registre n'a de `title`.** L'étiquette affichée
dans le formulaire est la clé brute du contrat — `negative_prompt`,
`guidance_scale`, `octree_resolution`. La bonne place du français est le JSON, pas
une table de traduction dans le front : c'est de la rédaction, faisable en avance
comme les golden sets l'ont été.

**Le visualiseur 3D n'est pas installé.** La dette a changé de nature sans changer
de conclusion : ce n'est plus l'absence d'URL, c'est qu'**aucun maillage
n'existe**. Quarante mégaoctets de composant éprouvés sur un fichier fabriqué
seraient un chemin de code que rien n'exécute. En attendant, le `.glb` se
télécharge.

**Le banc d'essai ne regarde pas la forme de ce qu'il produit.** Découvert le
22 août : `moss-transcribe` a passé ses trois cas au vert en livrant des
marqueurs de locuteur dans un fichier annoncé en texte brut. Le §8 de la
conception dit que le banc mesure un coût et non une qualité ; il faut y ajouter
qu'il ne vérifie même pas que la sortie ressemble à ce que le contrat décrit. Une
garde légère — la sortie d'un `text/plain` ne contient pas de balisage, un JSON
se relit — coûterait peu et aurait trouvé celui-là.

### L'ordre

```
maintenant, en parallèle et sans dépendance
  ├─ 4.2 Bibliothèque ──→ 4.7 bandeau vivant                       ┐ fin du v0.4
  ├─ 7.0 éprouver Hunyuan3D   (le risque, à lever tôt)             │
  ├─ les douze enregistrements ASR (5.1)                           │
  └─ les `title` des 169 champs   (rédaction)                      ┘
                                    │
                        v0.5 ──→ v0.6 ──→ v0.7
```

Le reste est séquentiel, et c'est voulu : chaque jalon valide les fondations du
suivant. Les seules choses parallélisables sont celles qui relèvent de la
rédaction ou de la mesure — elles ne demandent l'accord de personne et lèvent des
inconnues au lieu d'en ajouter.
