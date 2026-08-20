# Écurie — Plan de réalisation

> Découpage en tâches des jalons du §12 de `ARCHITECTURE.md`, selon la conception de
> `CONCEPTION.md`. Chaque jalon a un critère de sortie unique et vérifiable ; on ne
> passe pas au suivant sans lui.
>
> Rappel du test d'existence du projet : **si le v0.1 ne sert pas dans la semaine qui
> suit sa livraison, on arrête et on réévalue.**

---

## v0.1 — Voir le parc (1 fin de semaine)

Lecture seule. Aucune exécution de modèle, aucune écriture sur le disque scanné.

| # | Tâche | Livrable |
|---|---|---|
| 1.1 | `git init`, arborescence cible, migration des fichiers actuels (`registry/schema/`, `registry/models/`, `.claude/skills/veille-modeles/`), workspace `uv`, `ruff`, `pytest` | dépôt structuré, `uv sync` passe |
| 1.2 | `core` : modèles pydantic, chargement + validation du registre, invariants inter-fichiers | `ecurie registry validate` sur les 3 manifestes existants (le placeholder `0000000` doit être signalé) |
| 1.3 | `core` : config machine `~/.ecurie/config.toml` avec autodétection des chemins | config générée au premier lancement |
| 1.4 | `store` : SQLite (`artifacts`, `locations`, `hash_cache`), scanners `hf`, `ollama`, `lmstudio`, `comfy`, `declared` | `ecurie store scan` remplit la base |
| 1.5 | `store` : hachage niveaux 1–2 (inode + hash annoncé), calcul des trois chiffres, arbre de duplication | `ecurie store status` |
| 1.6 | Résolveur : rattachement Locations ↔ variants du registre | variants inconnus du registre listés à part (« fichiers hors registre ») |
| 1.7 | Tests unitaires sur fixtures synthétiques (chiffres attendus exacts) | `pytest` vert |

**Critère de sortie** : sur la machine réelle, `ecurie store status` affiche apparent /
réel unique / récupérable (2 postes sur 4 : duplication, révisions obsolètes) en
moins de 30 s, et au moins une duplication réelle est découverte ou son absence
confirmée.

## v0.2 — Récupérer des gigaoctets (1 semaine)

| # | Tâche | Livrable |
|---|---|---|
| 2.1 | sha256 complet à la demande + cache ; `ecurie store verify` | hash vérifiés persistés |
| 2.2 | Générateur de plan de GC (4 postes, format JSON de la conception §4.3) | `ecurie store plan` avec gain chiffré par poste |
| 2.3 | Quarantaine `~/.ecurie/trash/` + `trash list/empty` | aucune suppression directe possible dans le code (vérifié par test) |
| 2.4 | `ecurie store apply` : dédup lien dur (re-hash à l'exécution, même volume, remplacement atomique), mise en corbeille | journal d'application |
| 2.5 | Tiering : `ecurie store tier <ref> /Volumes/…`, détection volume absent au scan | patch YAML `tier: cold` affiché |
| 2.6 | Table `runs` (télémétrie, encore vide) branchée au poste « jamais utilisé » | affiché « inconnu » tant que vide |

**Critère de sortie** : un plan appliqué sur le vrai parc récupère l'espace annoncé
(±1 %), et tout ce qui a quitté sa place est dans la corbeille, restaurable à la main.

## v0.3 — Exécuter sans OOM (2 semaines)

| # | Tâche | Livrable |
|---|---|---|
| 3.1 | Contrats de capacité du parc initial (`capabilities/*.json`) + validation croisée au chargement du registre | schémas d'E/S committés |
| 3.2 | Protocole worker + superviseur (spawn dans le venv, ping, timeout, kill) | testé avec `fake_worker.py` |
| 3.3 | `runtimes/` : `pyproject.toml` par env, `ecurie env sync` | envs reconstructibles |
| 3.4 | Adaptateurs `mlx_audio`, `diffusers_mps`, `custom` (entrypoint Hunyuan3D) | 3 workers réels |
| 3.5 | `ecurie pull` (téléchargement à révision épinglée, garde des 15 % de disque libre) ; épingler les vraies révisions des 2 manifestes actifs | placeholders `0000000` éliminés |
| 3.6 | Contrôle d'admission (budget, LRU, mode mesure pour variant sans profil) + `ecurie ps` / `ecurie unload` | simulation testée unitairement |
| 3.7 | `ecurie run <ref> -p k=v` : job complet, sortie fichiers, ligne `runs` | premier `run` TTS réel |
| 3.8 | `ecurie bench <ref>` : mesure du profil, écriture `measurements/`, patch `profile:` | profils mesurés des 2 titulaires committés |

**Critère de sortie** : `ecurie run qwen3-tts-1.7b -p text="…"` produit un wav ;
lancer ensuite un job image décharge le TTS proprement (visible dans `ecurie ps`),
sans swap ni OOM.

## v0.4 — Utilisable au quotidien (2 semaines)

| # | Tâche | Livrable |
|---|---|---|
| 4.1 | FastAPI : registre, jobs + SSE, `store/summary`, résidents | `ecurie serve` |
| 4.2 | Bibliothèque côté serveur : manifeste de job complet, rejeu | reproductibilité effective |
| 4.3 | UI : socle React+Vite+RJSF, mapping `x-ui`, visualiseurs par media type | formulaire généré depuis un contrat, zéro formulaire manuel |
| 4.4 | Écran **Atelier** (capacité → variant → formulaire → progression SSE → sortie) + bandeau de ressources | flux complet dans le navigateur |
| 4.5 | Écran **Parc** (trois chiffres, arbre de duplication, plan de GC dry-run, tiering) | parité avec la CLI |

**Critère de sortie** : une semaine d'usage réel où l'UI est le chemin par défaut
pour lancer TTS et image — sans retomber sur les scripts d'origine.

## v0.5 — Mesurer (2 semaines)

| # | Tâche | Livrable |
|---|---|---|
| 5.1 | Golden sets figés : ASR (12 extraits dont FR-QC), OCR (15 pages), TTS (10 phrases), image (10 prompts), mesh (8 images) — append-only | `registry/evals/golden/` committé |
| 5.2 | `ecurie eval` : métriques automatiques (WER, exactitude OCR) → `evals/results/` | comparables entre variants |
| 5.3 | Exécution A/B (même entrée, deux variants, séquentielle sous admission) | paires générées |
| 5.4 | Écran **Confrontation** + `preferences.jsonl` + Elo dérivé + choix des paires par incertitude | 30 comparaisons TTS faites |
| 5.5 | Écran **Bibliothèque** (index, filtre, rejouer) | quatrième écran, plafond atteint |

**Critère de sortie** : le classement TTS départage titulaire et challenger sur des
préférences réelles, et un artefact de trois semaines est rejoué à l'identique.

## v0.6 — S'entretenir (1 semaine)

| # | Tâche | Livrable |
|---|---|---|
| 6.1 | `registry-ci.yml` : schéma, invariants, révisions épinglées existantes, licences, profil ⇔ mesure | CI verte exigée sur `registry/` |
| 6.2 | Compléments du skill de veille : `last_run.json`, `store status --json`, quarantaine de téléchargement | phases 1–3 du skill exécutables |
| 6.3 | `veille.yml` : cron hebdomadaire → branche `veille/<date>` → PR au format RAPPORT.md | première PR de veille reçue |
| 6.4 | Vérification hebdomadaire des révisions/licences du parc actif | alerte si un dépôt HF disparaît |

**Critère de sortie** : un cycle de veille complet — PR reçue, un candidat éprouvé sur
golden set, décision prise en connaissance de coût — sans rien télécharger avant l'accord.

## v0.7 — Texte → 3D (1 semaine)

| # | Tâche | Livrable |
|---|---|---|
| 7.1 | Contrat composite `text-to-mesh` (steps + checkpoint) + validation du typage inter-étapes | schéma committé |
| 7.2 | Exécuteur composite (jobs chaînés, admission entre étapes : décharger l'image avant le mesh) | pipeline réel Z-Image/FLUX → Hunyuan3D |
| 7.3 | UI : point d'arrêt sur l'image intermédiaire, *régénérer* / *continuer* | boucle d'itération bon marché de la Route A |
| 7.4 | Manifeste composite en Bibliothèque (référence les étapes) | rejeu par étape ou complet |

**Critère de sortie** : `prompt → maillage` dans l'Atelier, avec itération sur l'image
avant de payer la reconstruction.

---

## Ordre et dépendances

```
v0.1 ── v0.2 ── v0.3 ── v0.4 ── v0.5 ── v0.6 ── v0.7
 │                │
 └ modèle de données validé   └ 3.1 (contrats) peut démarrer pendant v0.2
```

Seule parallélisation utile : les contrats de capacité (3.1) et la constitution des
golden sets (5.1) sont de la rédaction, faisables en avance pendant les jalons
précédents. Tout le reste est séquentiel — c'est voulu, chaque jalon valide les
fondations du suivant.

## Points de contrôle

- Fin de chaque jalon : relire le périmètre exclu (§11 de l'architecture) — le risque
  « dérive vers un clone de ComfyUI » se combat là.
- Fin v0.1 : le test d'existence. Fin v0.4 : l'outil a-t-il remplacé les scripts ?
  Sinon, corriger l'Atelier avant d'investir dans l'évaluation.
- Après trois cycles de veille (post-v0.6) : réajuster les pondérations du score.

## Premier pas immédiat

Tâches 1.1 et 1.2 : `git init`, restructurer le dépôt, valider les trois manifestes
existants contre le schéma. Une heure de travail, et tout le reste a un socle.
