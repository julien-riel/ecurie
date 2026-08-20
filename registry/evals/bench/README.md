# Charges types du banc d'essai

Trois entrées fixes par capacité, lues par `ecurie bench <ref>` pour mesurer le
profil d'un variant (CONCEPTION.md §8). Elles sont **figées** : la comparabilité
d'une mesure prise aujourd'hui avec une mesure d'il y a six mois n'existe que si
l'entrée n'a pas bougé entre-temps.

Même règle que les golden sets d'évaluation (§9) : **append-only**. On ajoute un
cas, on ne corrige jamais un cas existant. Une coquille dans un texte de charge
type est sans conséquence sur la mesure ; la corriger, si.

Ce que ces charges ne sont pas : un jeu d'évaluation de la *qualité*. Elles
mesurent le coût — mémoire, warmup, latence, débit. La qualité relève des golden
sets de `registry/evals/golden/`, au v0.5.

## Choix des cas

| Capacité | Cas | Ce que chacun éprouve |
|---|---|---|
| `text-to-speech` | court, moyen, difficile | latence à vide ; débit sur un paragraphe ; robustesse aux nombres, sigles, noms propres et traits d'union |
| `image-to-mesh` | cube-256, sphere-256, cone-384 | reconstruction d'arêtes vives, de courbure, puis coût d'une résolution d'octree supérieure — c'est ce paramètre qui pilote le pic mémoire |
| `document-to-text` | paragraphe, tableau, dense | transcription courante ; conservation d'un alignement en colonnes ; robustesse aux chiffres, sigles et références — c'est la densité de texte qui fait la durée |
| `text-to-music` | instrumental-15, couplet-20, couplet-refrain-30 | pièce sans voix ; chanson brève ; deux sections — la durée pilote le coût, la présence de voix change la nature du calcul |
| `text-to-image` | vignette-512, carre-1024, paysage-1216 | latence d'une petite image ; le carré natif du modèle ; un format non carré à résolution supérieure — c'est le produit largeur × hauteur × pas qui fait le coût |

Les images de `assets/` sont produites par une recette déterministe (silhouette
et dégradé calculés, pas de photo) : elles n'ont ni licence ni provenance à
suivre, et se refabriquent à l'identique si besoin. Elles sont en **RGBA avec un
fond réellement transparent** — le pipeline Hunyuan3D recadre sur le canal alpha,
et une image opaque le prive de sa seule indication de silhouette. Un fond gris
uniforme n'est pas un détourage.

Une capacité sans fichier de charge type reste mesurable — `ecurie bench` déduit
alors une entrée minimale du contrat — mais la mesure porte la mention « non
comparable », et c'est un signal qu'il manque un fichier ici.
