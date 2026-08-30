# Écurie

**L'inférence multimodale qui ne swappe jamais, sur Apple Silicon** — des yeux,
des oreilles et une voix locales pour votre agent de code, protégés par des
profils mémoire mesurés au banc plutôt qu'estimés. Aujourd'hui, c'est une CLI
et une API HTTP locale sur 41 contrats de capacité. Le serveur MCP qui en fera
douze outils d'agent est ce que veut dire **v1.0** : `ecurie mcp` n'est pas
encore une commande.

Écurie **n'exécute jamais un tenseur elle-même, et ne réimplémente jamais un
moteur**. Elle orchestre MLX, `mlx-audio`, `diffusers`, `mlx-vlm`, des binaires
C+Metal et les autres derrière des adaptateurs, chacun dans son environnement
isolé. Ce qu'elle apporte est ce qu'aucun de ces runtimes ne fait : savoir ce
que le parc occupe réellement sur le disque, refuser un job qui ferait partir
la machine en swap, et rendre reproductible une sortie produite il y a trois
mois. **Les moteurs exécutent. Écurie admet.**

Le pourquoi est dans [ARCHITECTURE.md](ARCHITECTURE.md), le comment dans
[CONCEPTION.md](CONCEPTION.md), l'état dans [PLAN.md](PLAN.md). La version de
référence de ce fichier est [README.md](README.md), en anglais — la langue du
produit ; la salle des machines parle encore français, et
[migre](CONTRIBUTING.md).

## État — pré-1.0, pivot en cours (2026-08-29)

| | Aujourd'hui | Avec la v1.0 |
|---|---|---|
| Installation | `git clone` + `uv sync` | `uv tool install ecurie` (PyPI) |
| Surfaces | CLI (16 commandes), API HTTP locale, UI personnelle | **serveur MCP** (`ecurie mcp`) |
| Admission | profils mesurés, sur la machine qui les a mesurés | + classes de machine, profils empruntés — étiquetés, jamais silencieux |
| Langue | sortie CLI et docs internes en français | surface anglaise |

Rien sur cette page — sous-titre compris — n'est de l'aspiration, sauf ce qui
est marqué **v1.0** ou *prévu*.

## Le problème

Votre agent doit transcrire une entrevue, décrire une capture, générer une
image de couverture, isoler une voix d'un enregistrement. Aujourd'hui : le
cloud (coût, confidentialité, latence), ou un serveur par modalité sur des
ports séparés **sans aucune coordination mémoire entre eux**. Un chargement
de 8 Go de trop pendant que l'IDE, le
navigateur et un LLM local occupent déjà la mémoire unifiée, et macOS swappe :
25 tok/s deviennent 2. Et rien ne permet à l'agent de *découvrir* ce qui est
installé.

## Le catalogue v1.0

Douze capacités, chacune servie par un variant au profil mesuré au banc,
exposées en outils MCP engendrés de leurs contrats typés
(`registry/capabilities/*.json`) :

- **Entendre** — `speech-to-text`, `speaker-diarization`, `audio-separation`
- **Parler** — `text-to-speech`
- **Voir** — `image-to-text`, `depth-estimation`, `image-segment`, `image-matting`
- **Produire** — `text-to-image`, `image-to-image`, `image-upscale`
- **Prévoir** — `time-series-forecast`

Plus trois méta-outils toujours présents : `ecurie_catalog` (découvrir les 41
contrats et les modèles installés), `ecurie_run` (l'échappatoire : n'importe
quelle capacité par son contrat), `ecurie_status` (résidents, budget Metal,
comptabilité disque — lecture seule).

Deux exclusions délibérées. Sept contrats déclarent une valeur `human_subject`
— les six `face-*` et `voice-clone` — et aucun n'est parmi les douze : la
**v1.0** tiendra la famille `face-*` hors du catalogue par défaut sur ce champ
plutôt que sur une liste tenue à la main, les familles étant en opt-in
(`ecurie mcp --tools faces`). Aujourd'hui le champ est déclaré et exposé par
l'API ; rien ne filtre encore dessus. Les 29 autres contrats sont
**expérimentaux** — découvrables, exécutables, sans promesse d'entretien ;
l'OCR est de ceux-là, `document-to-text` étant délibérément un contrat séparé
de `image-to-text`. Le catalogue est petit à dessein : le coût d'un agent est
son inventaire d'outils, pas sa conversation. Quarante outils déclarés coûtent
16 690 jetons de catalogue avant le moindre échange, et le modèle local mesuré
y choisissait encore juste ; à soixante-sept, il partait en boucle de
répétition sans que rien ne lève. Relevé en août 2026 sur
`gemma4-12b-chat@4bit`, au harnais d'agents `dsh` que le pivot a retiré depuis :
le montage n'a jamais été committé, c'est le seul chiffre de cette page dont
vous ne trouverez pas le fichier ici. Un client plus gros devrait tenir son
choix plus longtemps ; le coût de catalogue, lui, se paie chez tous.

## L'admission : mesurée, et négociable

La règle que tout le reste sert : **un profil est rempli par le banc d'essai,
jamais à la main. Un profil estimé est un profil faux.** Quand un job ne passe
pas, le refus n'est pas un message d'erreur — c'est une décision avec ses
chiffres et ses issues. L'éviction des résidents inactifs étant automatique
(LRU), un refus ne survient que quand il ne reste rien à évincer — et c'est ce
qu'il raconte. Le serveur MCP (**v1.0**) le rend lisible par la machine : le
pic mesuré, le budget, les résidents en cause, et les options que l'agent peut
exécuter (réessayer quand le job en cours finit, prendre un variant plus
léger) — une épingle posée par l'humain, elle, n'est jamais levée par l'agent :
il relaie la commande.

## La comptabilité disque : un instrument de connaissance

`ecurie store scan` parcourt le cache Hugging Face, les blobs Ollama, LM Studio
et les volumes que vous lui donnez, hache le contenu, et rapporte **trois
chiffres qu'aucun outil ne voit ensemble** : occupation apparente, occupation
réelle dédupliquée, espace récupérable. Hash annoncé ≠ hash vérifié, jamais de
destruction planifiée sur un hash annoncé, re-vérification au moment d'agir,
quarantaine au lieu de `rm`. N'en attendez pas des gigaoctets miracles —
attendez-en de savoir enfin où vit chaque gigaoctet.

## Installer aujourd'hui

Jusqu'à la v1.0, l'installation se fait depuis les sources. Prérequis : macOS
sur Apple Silicon, [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/julien-riel/ecurie.git && cd ecurie
uv sync                           # le socle : core, store, runtime, api
uv run ecurie env sync mlx-audio  # le venv isolé du seul runtime utile ici
uv run ecurie store scan          # peuple ~/.ecurie/state.db
uv run ecurie store status        # les trois chiffres
```

`env sync` prend des noms, et sans argument reconstruit **les dix-huit**
runtimes : 13 Go de venvs sur cette machine, dont 1,9 Go pour `cad-recode`
seul, avant le moindre poids téléchargé. `mlx-audio` est le seul dont le
quickstart ci-dessous a besoin ; `ecurie env list` nomme les autres.

Puis télécharger et exécuter. Seule la première ligne tire des gigaoctets : les
workers tournent avec `HF_HUB_OFFLINE=1`, donc `ecurie run` ne touche jamais au
réseau et `ecurie pull` en est le seul chemin.

```bash
uv run ecurie pull qwen3-tts-1.7b@8bit-mlx
uv run ecurie run qwen3-tts-1.7b@8bit-mlx -p text="bonjour"
uv run ecurie ps           # résidents, budget, coût du prochain job
uv run ecurie serve        # l'API, sur 127.0.0.1:8765
```

Une frontière, connue et tenue : `ecurie serve` écoute sur `127.0.0.1` et
refuse une adresse non locale sans `--expose`. L'API dit où vivent les poids
sur le disque, ce que la machine tient en mémoire, et lance des jobs. Il n'y a
ni authentification ni isolation par utilisateur — « un Mac qui sert plusieurs
personnes » n'est pas un usage supporté, c'est un autre projet.

`~/.ecurie/config.toml` est généré au premier lancement, avec autodétection des
caches. Le serveur MCP et `claude mcp add ecurie -- ecurie mcp` arrivent avec
la **v1.0**. Le détail des environnements à étape manuelle, des conventions de
mesure à plusieurs et des gardes de développement est dans
[README.md](README.md).

## Feuille de route

Quatre jalons jusqu'à la v1.0, sans dates fermes — un seul mainteneur, et
[SUPPORT.md](SUPPORT.md) le dit sans se dérober.

**J0 Publiable** — LICENSE Apache-2.0, CI sur chaque PR et nettoyage du dépôt
sont faits, et datés par le tag `v0.4.0` ; reste à prendre le nom sur PyPI,
sciemment remis à plus tard — publier brûle un nom et un numéro pour de bon, et
rien ici n'en dépend encore. Puis **J1 MCP servi** → **J2 Profils dignes de confiance** (banc durci,
classes de machine, profils empruntés) → **J3 Lancement** (quickstart rejoué
par des tiers, registre MCP).
Prévu ensuite, sans dates : `/v1/audio/*` et `/v1/images/generations`
compatibles OpenAI — **le chat sera délégué, jamais réimplémenté** — puis
`/v1/messages`, puis la Bibliothèque (rejouer tout job passé depuis son
manifeste).

Le critère de repli est publié, pas sous-entendu : sans signal externe dans les
trois mois suivant le lancement, Écurie redevient officiellement un outil
personnel publié. Le pré-engagement complet est dans
[ARCHITECTURE.md](ARCHITECTURE.md).

## Licence

**Apache-2.0**, pour tout le dépôt — les quatre paquets Python, les 72
manifestes de modèles, les 41 contrats de capacité, les mesures et les jeux de
banc.

Les poids, eux, ne le sont pas. Écurie les télécharge depuis leurs hôtes amont
au `ecurie pull` et n'en redistribue aucun ; chaque manifeste porte sa licence
amont telle que déclarée là-bas, non telle que vérifiée indépendamment. Sur les
72, treize sont `restricted` — dont `da3-large`, en CC BY-NC 4.0, non
commerciale —, deux `research-only` et deux `unknown`. Lisez le manifeste avant
de lancer le modèle qu'il décrit ; le détail est dans [NOTICE](NOTICE).
