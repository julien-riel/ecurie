# Écurie

**Le serveur MCP qui ne swappe jamais, pour Apple Silicon** — des yeux, des
oreilles et une voix locales pour votre agent de code : douze outils
multimodaux, protégés par des profils mémoire mesurés.

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
| Licence | aucune committée — tous droits réservés | Apache-2.0 |

Rien ci-dessous n'est de l'aspiration, sauf ce qui est marqué **v1.0** ou
*prévu*.

## Le problème

Votre agent doit transcrire une entrevue, décrire une capture, générer une
image de couverture. Aujourd'hui : le cloud (coût, confidentialité, latence),
ou un serveur par modalité sur des ports séparés **sans aucune coordination
mémoire entre eux**. Un chargement de 8 Go de trop pendant que l'IDE, le
navigateur et un LLM local occupent déjà la mémoire unifiée, et macOS swappe :
25 tok/s deviennent 2. Et rien ne permet à l'agent de *découvrir* ce qui est
installé.

## Le catalogue v1.0

Douze capacités, toutes pourvues d'un titulaire mesuré, exposées en outils MCP
engendrés de leurs contrats typés (`registry/capabilities/*.json`) :

- **Entendre** — `speech-to-text`, `speaker-diarization`, `audio-separation`
- **Parler** — `text-to-speech`
- **Voir** — `image-to-text`, `depth-estimation`, `image-segment`, `image-matting`
- **Produire** — `text-to-image`, `image-to-image`, `image-upscale`
- **Prévoir** — `time-series-forecast`

Plus trois méta-outils toujours présents : `ecurie_catalog` (découvrir les 41
contrats et les modèles installés), `ecurie_run` (l'échappatoire : n'importe
quelle capacité par son contrat), `ecurie_status` (résidents, budget Metal,
comptabilité disque — lecture seule).

Deux exclusions délibérées : les capacités `face-*` restent hors du catalogue
par défaut (`human_subject` est appliqué, pas décoratif), et les 29 autres
contrats sont **expérimentaux** — découvrables, exécutables, sans promesse
d'entretien. Le catalogue est petit à dessein : le coût mesuré d'un agent est
son inventaire d'outils, pas sa conversation.

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
git clone <dépôt> && cd ecurie
uv sync                    # le socle : core, store, runtime, api
uv run ecurie env sync     # les venvs isolés des runtimes
uv run ecurie store scan   # peuple ~/.ecurie/state.db
uv run ecurie store status # les trois chiffres
uv run ecurie pull qwen3-tts-1.7b@8bit-mlx
uv run ecurie run qwen3-tts-1.7b@8bit-mlx -p text="bonjour"
uv run ecurie serve        # l'API, sur 127.0.0.1:8765
```

`~/.ecurie/config.toml` est généré au premier lancement, avec autodétection des
caches. Le serveur MCP et `claude mcp add ecurie -- ecurie mcp` arrivent avec
la **v1.0**. Le détail des environnements à étape manuelle, des conventions de
mesure à plusieurs et des gardes de développement est dans
[README.md](README.md).

## Feuille de route

**J0 Publiable** (LICENSE Apache-2.0, CI, PyPI) → **J1 MCP servi** → **J2
Profils dignes de confiance** (banc durci, classes de machine, profils
empruntés) → **J3 Lancement** (quickstart rejoué par des tiers, registre MCP).
Prévu ensuite, sans dates : `/v1/audio/*` et `/v1/images/generations`
compatibles OpenAI — **le chat sera délégué, jamais réimplémenté** — puis
`/v1/messages`, puis la Bibliothèque (rejouer tout job passé depuis son
manifeste).

Le critère de repli est publié, pas sous-entendu : sans signal externe dans les
trois mois suivant le lancement, Écurie redevient officiellement un outil
personnel publié. Le pré-engagement complet est dans
[ARCHITECTURE.md](ARCHITECTURE.md).

## Licence

Écurie sera publiée sous **Apache-2.0** — le fichier LICENSE arrive avec le
jalon J0. Tant qu'il n'est pas committé, le régime par défaut « tous droits
réservés » s'applique.
