---
name: veille-modeles
description: >
  Effectue un cycle de veille sur les modèles open-weight pour le parc Écurie. Utiliser
  quand l'utilisateur demande une veille, une mise à jour du parc, la recherche de
  remplaçants pour une capacité, ou l'évaluation d'un modèle candidat. Produit une
  branche Git et une PR contenant un rapport daté et des manifestes en status:candidate.
  Ne télécharge ni ne modifie jamais le parc actif.
---

# Veille modèles — Écurie

## Règle cardinale

**Tu proposes, tu ne décides pas.** Aucun poids n'est téléchargé, aucun manifeste
`status: active` n'est modifié, aucun fichier n'est supprimé sans validation humaine
explicite. La sortie d'un cycle est une PR. Point.

## Contexte matériel

Cible : MacBook Air M5, 24 Go de mémoire unifiée. Budget GPU praticable **15–17 Go**
(working set Metal recommandé ≈ 17,8 GiB). Tout candidat dont le pic mémoire estimé
dépasse 17 Go est rejeté d'office, quelle que soit sa qualité.

Estimation grossière avant mesure :
`pic ≈ paramètres × bits/8 × 1,25` (le facteur couvre le KV cache ou les activations).
Cette estimation sert **uniquement au filtrage**. Elle n'entre jamais dans un manifeste.

## Phase 1 — Balayage

Lire `registry/veille/last_run.json` pour la date du dernier passage. Balayer depuis.

Sources, par ordre de valeur du signal :

1. **`mlx-community` sur Hugging Face** — une conversion MLX est le meilleur prédicteur
   qu'un modèle tournera bien. Filtrer par `lastModified > last_run`.
2. **Releases GitHub des runtimes** : `ml-explore/mlx`, `Blaizzy/mlx-audio`,
   `comfyanonymous/ComfyUI` et son registre de nœuds. Un runtime qui gagne le support
   d'une famille vaut souvent plus qu'un nouveau modèle.
3. **API HF par tâche**, `sort=lastModified`, une passe par capacité du registre.
4. **Laboratoires suivis** : Qwen, Tencent Hunyuan, Black Forest Labs, Lightricks,
   MiniMax, Baidu/Paddle, Microsoft Research.
5. **Déclencheurs surveillés** — parcourir les manifestes `status: candidate` et vérifier
   si leur condition de déblocage est levée. Exemple : `trellis2` attend un portage
   Metal des opérations de voxels épars.

Écrire les résultats bruts dans `registry/veille/<date>/candidats.json`.

## Phase 2 — Qualification

Rejeter, en journalisant le motif (un rejet motivé est réutilisable, un rejet silencieux
sera refait au prochain cycle) :

- pic mémoire estimé > 17 Go ;
- `license_class: research-only` sans demande explicite de l'utilisateur ;
- aucun chemin d'exécution Apple Silicon crédible (CUDA seul, noyaux personnalisés sans
  portage) — reclasser en `candidate` avec déclencheur plutôt que rejeter sèchement ;
- dépôt inactif depuis plus de 12 mois sur un domaine qui bouge vite ;
- doublon d'un variant déjà au registre.

Scorer les survivants :

```
score = 0.35 · gain_qualité_relatif   # vs le titulaire (incumbent) de la capacité
      + 0.20 · maturité_runtime       # portage MLX = 1,0 · nœud Comfy = 0,7 · script maison = 0,3
      + 0.20 · adéquation_budget      # marge mémoire et coût disque
      + 0.15 · licence                # permissive 1,0 · restricted 0,6 · research-only 0,0
      + 0.10 · vélocité               # téléchargements, activité du dépôt
```

Tant que le golden set n'est pas passé, `gain_qualité_relatif` est **inconnu** — ne pas
le remplacer par des affirmations marketing du dépôt. Marquer le score comme provisoire.

## Phase 3 — Épreuve (sur demande explicite)

Ne jamais lancer cette phase spontanément : elle télécharge des gigaoctets.

Pour chaque candidat retenu par l'utilisateur :

1. Vérifier l'espace disponible via `ecurie store status`. Si le téléchargement fait
   passer le disque libre sous 15 %, s'arrêter et proposer un plan de GC d'abord.
2. Télécharger dans un emplacement de quarantaine, hors du parc actif.
3. Mesurer le profil réel : `disk_bytes`, `peak_unified_memory_bytes`, `warmup_ms`,
   débit. MLX → `mx.get_peak_memory()`. Sinon → échantillonnage RSS à 100 ms.
4. Exécuter le golden set de la capacité. Métriques automatiques en CI (WER, exactitude
   OCR) ; le reste produit des paires A/B en attente d'arbitrage humain dans l'UI.
5. Écrire `registry/measurements/<id>@<variant>/<machine>.json` (le banc le nomme).

## Phase 4 — Élagage

Proposer des retraits, jamais les exécuter :

- variants avec `run_count == 0` depuis plus de 90 jours ;
- variants dominés : un autre variant du même modèle est meilleur **et** plus léger ;
- révisions HF obsolètes et blobs orphelins ;
- duplication entre gestionnaires, résolvable par lien dur.

Chiffrer le gain en Go par poste. Un plan de GC sans gain chiffré est inutilisable.

## Sortie

Créer la branche `veille/<date>` et écrire :

```
registry/veille/<date>/
  RAPPORT.md          # ci-dessous
  candidats.json
registry/models/<nouveau>.yaml    # status: candidate uniquement
```

Structure de `RAPPORT.md` :

1. **Verdict en trois lignes** — ce qui change, ce qui ne change pas, l'action demandée.
2. **Recommandations de remplacement** — par capacité, titulaire vs challenger, gain
   mesuré ou provisoire, coût en Go et en mémoire, licence. Une recommandation sans
   coût chiffré est refusée.
3. **Candidats à éprouver** — ce qui mérite un téléchargement, avec le budget associé.
4. **Rejets motivés** — table courte, un motif par ligne.
5. **Déclencheurs levés** — candidats précédemment bloqués dont la condition est remplie.
6. **Plan de GC** — gain en Go par poste.

## Ton

Direct et technique. Si aucun candidat ne bat un titulaire en poste, l'écrire en une
phrase et clore le rapport — c'est un résultat valable et fréquent. Un rapport de veille
qui recommande systématiquement un changement est un rapport de veille cassé.
