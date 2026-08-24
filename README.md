# Écurie

Registre déclaratif, comptabilité disque et UI générique pour un parc de modèles
open-weight exécutés localement sur Apple Silicon.

Écurie **n'exécute jamais un tenseur elle-même** : elle orchestre MLX, `mlx-audio`,
`diffusers`, `mlx-vlm` et les autres derrière des adaptateurs, chacun dans son
environnement isolé. Ce qu'elle apporte est ce qu'aucun de ces runtimes ne fait :
savoir ce que le parc occupe réellement sur le disque, refuser un job qui ferait
partir la machine en swap, et rendre reproductible une sortie produite il y a trois
mois.

Le pourquoi est dans [ARCHITECTURE.md](ARCHITECTURE.md), le comment dans
[CONCEPTION.md](CONCEPTION.md), l'état d'avancement dans [PLAN.md](PLAN.md). Ce
fichier-ci ne couvre que le premier quart d'heure.

## Ce que le dépôt contient, et ce qu'il ne contient pas

C'est la distinction dont tout le reste découle (CONCEPTION.md §1.1).

| | Où | Versionné |
|---|---|---|
| **Déclaré** — manifestes, contrats de capacité, golden sets, profils mesurés | `registry/` | oui |
| **Observé** — artifacts scannés, cache de hachage, télémétrie | `~/.ecurie/state.db` | non |
| **Machine** — chemins scannés, budget mémoire, politique de résidence | `~/.ecurie/config.toml` | non |
| **Poids** — les gigaoctets | cache HF, Ollama, LM Studio, volumes externes | non |
| **Environnements** — le contrat des runtimes | `runtimes/*/pyproject.toml` + `uv.lock` | oui, le `.venv` non |

Autrement dit : **le dépôt décrit un parc, il ne le contient pas.** C'est ce qui
permet à plusieurs personnes, sur plusieurs Macs, de partager le même registre sans
rien se marcher dessus.

## Installer sur un Mac

Prérequis : macOS sur Apple Silicon, [`uv`](https://docs.astral.sh/uv/), et Node 20.19+
(Vite 8) si vous touchez au front.

```bash
git clone <dépôt> && cd ecurie
uv sync                    # le socle : core, store, runtime, api
uv run ecurie env sync     # les venvs isolés des runtimes, depuis les uv.lock
uv run ecurie store scan   # peuple ~/.ecurie/state.db avec ce qui est déjà sur le disque
uv run ecurie store status # les trois chiffres : apparent, réel unique, récupérable
```

`~/.ecurie/config.toml` est **généré au premier lancement**, avec autodétection de
`~/.cache/huggingface/hub`, `~/.ollama/models` et `~/.lmstudio/models`. Il n'y a rien
à saisir ; un chemin absent au scan est ignoré, pas une erreur.

Deux environnements demandent une étape de plus, et le disent — `ecurie env list`
les signale dans sa colonne « À lire » :

- `runtimes/hunyuan3d` — le code d'inférence de Tencent n'existe pas sur PyPI et doit
  être vendoré à la main (deux commandes git dans [son README](runtimes/hunyuan3d/README.md)).
- `runtimes/cad-recode` — même mécanique, autre motif : le code amont est publié sous
  licence **non commerciale**, et il n'a donc pas sa place dans ce dépôt. Un script
  versionné en extrait la partie utile et affiche son empreinte
  ([son README](runtimes/cad-recode/README.md)).
- tout `runtimes/*/vendor/` en général : pas versionné, reconstruit d'après le README
  de l'environnement concerné.

Ensuite, télécharger et lancer :

```bash
uv run ecurie pull qwen3-tts-1.7b@8bit-mlx        # à la révision épinglée du manifeste
uv run ecurie run qwen3-tts-1.7b@8bit-mlx -p text="bonjour"
uv run ecurie ps                                   # résidents, budget, coût du prochain job
uv run ecurie serve                                # l'API et l'Atelier, sur 127.0.0.1:8765
```

Le front se lance à part, en développement :

```bash
cd apps/ui && npm install && npm run dev
```

## Ce qui s'adapte tout seul à votre Mac

**Le budget mémoire.** Sur Apple Silicon, le chiffre qui décide d'un swap n'est pas la
mémoire installée mais le `recommendedMaxWorkingSetSize` de Metal — environ 75 % de la
mémoire unifiée. Écurie le lit en interrogeant le `mlx` d'un environnement de runtime,
avec repli sur une règle de trois puis sur un défaut prudent. **La provenance du chiffre
est toujours affichée à côté de lui** : un budget lu dans Metal et un budget déduit ne
méritent pas la même confiance.

**Le seuil de lourdeur.** La règle du parc — un seul modèle lourd résident à la fois,
les légers restent chauds — a besoin d'un seuil. Il vaut 45 % du budget détecté, ce qui
donne les 8 Gio calibrés sur la machine de référence (24 Go, budget 17,76 Gio) et un
seuil proportionnellement plus bas sur un Mac de 16 Go. Un nombre d'octets explicite
dans `config.toml` remplace la part.

**Ce qui ne s'adapte pas, parce qu'il n'a pas à le faire** : `peak_unified_memory_bytes`
et `disk_bytes` d'un profil mesuré. Ce sont les poids et les activations ; ils ne
dépendent pas du Mac qui les charge. C'est pourquoi un manifeste mesuré chez quelqu'un
d'autre vous sert tel quel.

## À plusieurs, sur plusieurs Macs

Le mode de collaboration est celui de la veille : **on propose par PR, on ne mute jamais
l'état vivant.** Un modèle candidat entre en `status: candidate`, un humain valide.

Trois conventions suffisent.

**1. Les profils sont mesurés par machine, et le nom du fichier le dit.**
`ecurie bench <ref>` écrit `registry/measurements/<ref>/<machine>.json` — un fichier par
Mac. Deux personnes qui mesurent le même variant ne s'écrasent plus ; la même personne
qui remesure remplace bien son propre relevé, le nom du fichier ne retenant que le
matériel. Le bloc `profile:` du manifeste, lui, reste unique : c'est la copie d'**un** de
ces relevés, et `ecurie registry validate` vérifie qu'il correspond à au moins l'un
d'eux sur les chiffres portables.

**2. Ce que vous ne mesurez pas, ne le committez pas.** `warmup_ms`, `latency_ms_p50`,
`throughput` et toute pente de `peak_scaling` décrivent votre machine autant que le
modèle. Ils vivent dans votre relevé ; le manifeste porte ceux du poste qui l'a écrit, et
c'est très bien ainsi.

**3. Le serveur est local, et il le reste.** `ecurie serve` écoute sur `127.0.0.1` et
refuse une adresse non locale sans `--expose`. L'API dit où sont les poids sur le disque,
ce que la machine tient en mémoire, et lance des jobs : la publier sur un réseau donne
tout cela à qui passe. Il n'y a ni authentification ni cloisonnement par utilisateur —
« un Mac serveur pour plusieurs personnes » n'est pas un usage supporté, c'est un autre
projet.

Un dernier point, connu et assumé : les fixtures du front
(`apps/ui/src/api/__fixtures__/`) contiennent des champs tirés du disque de qui les a
régénérées — `ready`, `blockers`, `weights_path`, `ready_variants`. La garde pytest les
ignore, faute de quoi elle échouerait dans tout clone sauf un. Régénérer ces fichiers
depuis un poste dont le parc n'est pas téléchargé les appauvrirait ; c'est à qui a le
parc complet de le faire.

## Développer

```bash
uv run pytest                 # la suite ; les tests marqués `real` en sont exclus
uv run pytest -m real         # les tests d'intégration sur le vrai parc, hors CI
uv run ruff check packages/   # lint, ligne à 100 colonnes
cd apps/ui && npm test        # la suite du front
```

Deux gardes méritent d'être connues avant de s'y heurter :

- `tools/openapi_dump.py` fige le schéma de l'API, et un test le compare. Changer une
  route sans régénérer casse la suite — c'est voulu, le front en dépend.
- `tools/ui_fixtures.py` fige ce que le serveur calcule sur le vrai registre, et le
  front compare sa propre fusion des valeurs par défaut à cet instantané. Éditer
  `registry/` peut donc faire échouer un test d'API : `npm run fixtures` régénère.
