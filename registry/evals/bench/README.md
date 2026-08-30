# Charges types du banc d'essai

Trois entrées fixes par capacité, lues par `ecurie bench <ref>` pour mesurer le
profil d'un variant (CONCEPTION.md §8). Elles sont **figées** : la comparabilité
d'une mesure prise aujourd'hui avec une mesure d'il y a six mois n'existe que si
l'entrée n'a pas bougé entre-temps.

Même règle que les golden sets d'évaluation (§9) : **append-only**. On ajoute un
cas, on ne corrige jamais un cas existant. Une coquille dans un texte de charge
type est sans conséquence sur la mesure ; la corriger, si.

Ce que ces charges ne sont pas : un jeu d'évaluation de la *qualité*. Elles
mesurent le coût — mémoire, warmup, latence, débit. La qualité relèvera des golden
sets de `registry/evals/golden/` : au 29 août 2026 ils existent comme données et
rien ne les passe — `ecurie --help` rend neuf commandes de premier niveau, `eval`
n'en est pas une.

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
| `time-series-forecast` | ctx-512, ctx-2048, ctx-8192 | une seule série horaire, trois fenêtres emboîtées — c'est le **contexte** qui pilote le coût, pas l'horizon, ce qui n'allait pas de soi |
| `audio-align` | une-phrase-4s, trois-phrases-18s, six-phrases-32s | le même enregistrement, trois durées écoutées. Le modèle reçoit 13,0 jetons audio par seconde contre 3,7 par mot : la durée l'emporte, et c'est le seul des deux à être un paramètre du contrat, donc le seul que l'admission puisse lire avant de lancer |
| `image-embed` | cote-256, cote-512, cote-1024 | une seule scène, trois définitions — `max_side` pilote le coût, mais par une **marche puis un plateau** et non par une pente : le banc ajuste un R² de 0,57, jette la droite, et le profil garde le pire cas |
| `geo-segment` | tuile-384, tuile-576, tuile-768 | une scène à six bandes, trois tailles de tuile. Les cotes ne sont pas rondes et ce n'est pas un choix : MPS refuse `adaptive_avg_pool2d` non divisible, et le PPM d'UperNet impose un **multiple de 192** — la taille native des chips, 512, ne passe donc pas |
| `geo-embed` | tuile-192, tuile-384, tuile-768 | la même scène, trois tuiles — l'encodeur seul, sans tête de segmentation, et son pic ne bouge pas d'un octet entre les trois |
| `protein-embed` | ubiquitine-76, lysozyme-129, gfp-238 | trois protéines réelles de longueur croissante. **La seule charge du registre sans un octet sous `assets/`** : l'entrée de cette capacité est du texte saisi, pas un fichier. Aucun `scaling_parameter` — mesuré, le pic est plat à seize kibioctets près de 76 à 2048 résidus ; c'est la latence qui suit la longueur |
| `pointcloud-to-cad` | cube, cylindre-perce, piece-en-l | trois familles de construction du dialecte CadQuery. Aucun `scaling_parameter` : le pic dépend bien de `n_points`, mais par une marche (R² = 0,771), et la durée est dominée par le nombre de jetons produits — qui n'est pas un paramètre d'entrée |

Les images de `assets/` sont calculées — silhouette et dégradé rendus, aucune
photo, aucun enregistrement de personne : elles n'ont ni licence ni provenance à
suivre. Trois réserves à cette phrase (le `NOTICE` du dépôt les reprend, et en
ajoute une quatrième qui porte sur les recettes et non sur la provenance) :
`parole-tts.wav` et `parole-fr-32s.wav` ne sont pas des rendus mais des sorties
du modèle `qwen3-tts-1.7b@8bit-mlx` du parc, voix synthétique et texte écrit ici ;
`atelier-mouvement.mp4` déclare une recette `scene-animee` et la commande ffmpeg
qui l'a assemblée, mais aucun script committé ne l'exécute — `grep -rn
"scene-animee"` ne rend que ce bloc `source` et le `NOTICE` ; et les trois
séquences d'acides aminés de `protein-embed.json` sont la seule entrée du dossier
**collectée** plutôt que calculée — PDB 1UBQ, 2LYZ et 1GFL, CC0 1.0, dont le champ
`provenance` de ce fichier dit la lecture du 24 août 2026.

**Les recettes sont maintenant committées** — `tools/golden_assets.py` pour les
images, les pages et le son ; et depuis le 24 août 2026, un module par famille
sous `tools/assets/`, appelé par `tools/bench_assets.py <famille>`. Les capacités
de mesure n'entrent pas dans les cinq recettes du premier script : une série
horaire, un nuage de points, une scène satellite à six bandes et un
enregistrement de parole ne se fabriquent pas comme une page ou un solide. Chaque
module déclare l'environnement dont il a besoin, parce qu'il n'y en a pas un qui
les serve tous — rasterio pour le GeoTIFF, rien du tout pour le PLY.

Les cas qui en viennent portent un bloc `source` qui dit comment les refaire :
**49 des 58 fichiers d'`assets/`** sont cités par au moins un cas qui en porte un.
**Huit images n'en ont aucun** — `cube.png`, `sphere.png`, `cone.png`,
`page-paragraphe.png`, `page-tableau.png` et `page-dense.png`, entrées le 20 août
2026, puis `masque-cone.png` et `masque-fond.png` le 22 : leur recette n'a jamais
été versionnée, et ce sont des données orphelines qu'on ne sait plus expliquer. On
ne recommence pas. Le neuvième fichier sans bloc `source`, `parole-fr-32s.wav`,
n'est pas orphelin pour autant : sa recette est `tools/assets/parole.py`, que la
description d'`audio-align.json` nomme.

Ces huit-là ne sont pas d'un seul tenant, et une inspection des fichiers le dit :
les trois solides sont des 256×256 en **RGBA à fond réellement transparent**, les
deux masques d'`image-inpaint` des 768×768 en niveaux de gris 8 bits, et les trois
pages du RGB sur 1100 pixels de large. Le fond transparent des solides n'est pas
une coquetterie : le pipeline Hunyuan3D recadre sur le canal alpha, et une image
opaque le prive de sa seule indication de silhouette. Un fond gris uniforme n'est
pas un détourage.

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
