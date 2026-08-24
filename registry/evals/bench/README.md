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
| `image-to-text` | bref-128, normal-512, detaille-1024 | une seule image, trois longueurs de réponse : l'image est encodée une fois, c'est le nombre de jetons produits qui fait la durée |
| `image-matting` | cote-512, cote-1024, cote-2048 | une seule scène, trois définitions soumises au modèle — `max_side` pilote le coût mémoire, et au carré |
| `image-upscale` | cote-128, cote-256, cote-512 | trois définitions d'entrée au facteur **du variant**. Le facteur n'est pas fixé ici : il appartient aux poids, et l'imposer refuserait la moitié des variants avant de les mesurer |
| `translation` | court, moyen, long | trois textes français vers l'anglais, de longueur croissante — langue d'arrivée fixée pour que la comparaison porte sur le coût et non sur la difficulté d'une paire |
| `tool-use` | outils-3, outils-8, outils-16 | la même demande, trois catalogues : c'est le nombre d'outils déclarés qui gonfle le contexte, la réponse tenant dans les mêmes quelques dizaines de jetons |
| `text-generation` | court-128, moyen-512, long-1536 | trois plafonds de jetons, température nulle partout — une charge qui échantillonnerait ne serait pas reproductible |
| `face-detect` | res-320, res-640, res-1280 | une scène à quatre visages, trois définitions d'entrée — `input_size` pilote le coût au carré, et à 320 les plus petits visages sortent du champ du réseau, ce qui est précisément ce que ce paramètre gouverne |
| `face-landmark`, `face-parse`, `face-embed`, `face-headpose`, `face-gaze` | visages-1, visages-2, visages-4 | la même scène, trois plafonds — ces cinq capacités ne cherchent pas les visages, elles traitent ceux qu'un détecteur leur désigne, et c'est le nombre de passages qui fait la durée |

Les images de `assets/` sont produites par une recette déterministe (silhouette
et dégradé calculés, pas de photo) : elles n'ont ni licence ni provenance à
suivre, et se refabriquent à l'identique si besoin.

**La recette est maintenant committée** — `tools/golden_assets.py`, et les cas
qui en viennent portent un bloc `source` qui dit comment les refaire. Les six
premières images de ce dossier, elles, n'en ont pas : leur recette n'a jamais été
versionnée, et ce sont des données orphelines qu'on ne sait plus expliquer. On ne
recommence pas. Elles sont en **RGBA avec un
fond réellement transparent** — le pipeline Hunyuan3D recadre sur le canal alpha,
et une image opaque le prive de sa seule indication de silhouette. Un fond gris
uniforme n'est pas un détourage.

## Les visages, et pourquoi ils sont calculés

`assets/visages-groupe.png` montre quatre visages. Aucun n'existe : ils sont
rendus par lancer de rayons sur une fonction de distance signée, comme les
solides de `image-to-mesh`, et leur recette est dans `tools/golden_assets.py`.

Ce n'est pas une élégance, c'est la seule issue. Une charge type est versionnée,
publique et figée pour des années — exactement ce qu'on ne fait pas du portrait
de quelqu'un. Un visage calculé n'a ni identité, ni consentement à recueillir, ni
licence à suivre, et il se refabrique à l'identique.

**Il a fallu deux essais.** La première version, sans paupières — le globe
oculaire entier apparent —, était trouvée par RetinaFace MobileNet à 1,00 et par
SCRFD à 0,61, mais **pas du tout par RetinaFace ResNet-50**. Une charge qu'un
variant ne peut pas servir le rend non profilable, donc inadmissible : c'est ce
qui est arrivé à SAM 3 sur `image-segment`. Paupières ajoutées, les neuf
détecteurs essayés trouvent le visage, ResNet-50 à 0,976.

**Ce que cette charge donne et qu'une photographie ne donnerait pas :** les
quatre visages sont rendus à des lacets connus — 0°, +24°, −18°, +8° — puisque
c'est la recette qui les pose. `face-headpose` s'y vérifie donc contre une vérité
terrain qu'on contrôle, sans annotation manuelle.

**Ce qu'elle ne donne pas :** de la texture de peau. `face-parse` place
correctement les régions mais range de larges plages de joue sous `hat`, faute de
grain auquel se raccrocher. La charge mesure un coût, pas une qualité — et c'est
le golden set qui posera la question des photographies, avec celle du
consentement que cette charge-ci évite.

Une capacité sans fichier de charge type reste mesurable — `ecurie bench` déduit
alors une entrée minimale du contrat — mais la mesure porte la mention « non
comparable », et c'est un signal qu'il manque un fichier ici.
