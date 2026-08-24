# Neuf capacités qui ne produisent pas de contenu — 24 août 2026

> Second cycle de la journée. Le matin a soldé le §D du backlog (la famille
> visage, six capacités) ; celui-ci attaque la file d'entrée dans **l'ordre de
> priorité voulu** — séries temporelles, CAO, géospatial, reconstruction
> spatiale, robotique, science — plus les deux pistes du §C que le backlog
> désignait comme les moins coûteuses.

## Verdict en trois lignes

**Ce qui change** : neuf capacités entrent au parc, toutes exécutées et mesurées
sur la machine cible. Le parc passe de 32 à **41 capacités déclarées**, de 54 à
**64 manifestes**, et de 11 à **16 environnements de runtime**. 13,6 Go de poids,
et le plus gros pic du lot occupe 61,7 % du budget.

**Ce qui ne change pas** : aucun titulaire. Les dix manifestes entrent en
`status: candidate`, et aucune confrontation A/B n'a eu lieu — le parc n'a pas de
golden set pour ces domaines, et cinq rangs sur cinq paires connues ne classent
pas des encodeurs.

**L'action demandée** : relire les deux décisions de licence (`cad-recode` en
`research-only`, `smolvla-libero` en `unknown` malgré une déclaration Apache), et
trancher si une capacité sans usage vérifiable de bout en bout — `robot-action`,
faute de robot — a sa place au registre autrement qu'en candidate.

## Ce qui est entré

| Capacité | Modèle | Licence | Disque | Pic | Runtime |
|---|---|---|---|---|---|
| `time-series-forecast` | Chronos-2 | apache-2.0 | 0,48 Go | 0,93 Go | `chronos` (neuf) |
| `audio-align` | Qwen3-ForcedAligner 0.6B | apache-2.0 | 1,28 Go | 2,46 Go | `mlx-audio` |
| `image-embed` | DINOv3 ConvNeXt-S | DINOv3 (restricted) | 0,20 Go | 1,33 Go | `torch-vision` |
| `image-embed` | DINOv2 ViT-B/14 | apache-2.0 | 0,35 Go | 1,53 Go | `torch-vision` |
| `geo-segment` | Prithvi-EO-2.0 Sen1Floods11 | apache-2.0 | 1,28 Go | 3,30 Go | `terratorch` (neuf) |
| `geo-embed` | Prithvi-EO-2.0 300M TL | apache-2.0 | 1,33 Go | 3,28 Go | `terratorch` |
| `protein-embed` | ESM-2 650M | MIT | 2,61 Go | 3,35 Go | `esm-torch` (neuf) |
| `pointcloud-to-cad` | CAD-Recode v1.5 | CC BY-NC 4.0 | 3,09 Go | 4,42 Go | `cad-recode` (neuf) |
| `multiview-to-3d` | Depth Anything 3 Large 1.1 | apache-2.0 | 1,64 Go | 11,77 Go | `depth-anything` |
| `robot-action` | SmolVLA 450M LIBERO | apache-2.0 (voir plus bas) | 0,91 Go | 3,44 Go | `lerobot` (neuf) |

Deux entrées n'ont rien coûté en environnement : `audio-align` tourne sur le venv
`mlx-audio` **sans une dépendance de plus** — son code d'inférence était déjà
livré par la distribution —, et `image-embed` n'a demandé qu'un plancher relevé
sur un paquet déjà installé au-dessus. C'est la cinquième et la sixième fois que
la question « lequel des env déjà là sait le faire ? » paie.

## Recommandations de remplacement

**Aucune.** C'est un résultat, et il est franc : sur les dix manifestes, aucun
n'entre en concurrence avec un titulaire du parc. Neuf des dix ouvrent une
capacité qui n'existait pas ; le dixième (`dinov2`) est le doublon permissif de
`dinov3` sur la même capacité neuve, et il est là pour que le parc puisse choisir
la licence plutôt qu'un point de qualité qu'aucune mesure ne soutient.

Ce qui a été mesuré à titre de comparaison, et ce que cela vaut :

- **DINOv3 ConvNeXt-S contre DINOv2 ViT-B/14** — trois requêtes contre huit
  solides de synthèse : 3/3 contre 2/3 à 256 pixels, 2/3 contre 2/3 à 512. Une
  requête d'écart sur trois ne classe pas des encodeurs, et l'épreuve mesure
  surtout le prétraitement : deux scènes sans rapport rendent 0,79 de cosinus sur
  ces images grises. **La comparaison n'a pas eu lieu**, et les deux manifestes
  le disent.
- **Chronos-2 sur CPU contre MPS** — les deux variants sont mesurés et le CPU
  gagne dès que le contexte dépasse deux mille pas (139 ms contre 190 à 8192).
  Le variant `mps` est livré perdant, délibérément : il rend l'écart vérifiable
  dans le dépôt plutôt que cité de mémoire.
- **Depth Anything 3 en 1.1 contre 1.0** — deux agents ont mesuré l'erreur de
  pose sur deux scènes différentes et obtenu des classements **opposés** (1,90 %
  contre 1,12 % chez l'un, 0,086 % contre 0,223 % chez l'autre). Les deux
  mesures sont vraies sur leur scène et aucune ne tranche. Ce qui a décidé est la
  licence, et le manifeste l'écrit ainsi plutôt que de faire disparaître la
  contradiction.

## Rejets motivés

| Piste | Motif, vérifié |
|---|---|
| **TimesFM 2.5**, **Moirai 2.0** | poids doublés de Chronos-2 pour la même capacité, aucune API DataFrame, pas de covariables. Écartés pour ce tour, pas rejetés |
| **ESM3**, **MatterGen** | licence de recherche, et le score de veille écarte `research-only` d'office |
| **Boltz-2** | demande un serveur de MSA : un parc local ne sort pas sur le réseau, ce qui est éliminatoire |
| **TerraMind 1.0** | jamais exécuté ici. Le §5.2 interdit l'hypothèse non mesurée |
| **VGGT**, **WorldMirror**, **SAM 3D** | non instruits au-delà du dossier ; `multiview-to-3d` est servie |
| **cadrille**, **cadrille-rl** | appellent `self.visual` sur une classe dont l'attribut a disparu en transformers 4.57 ; demanderaient un second venv |
| **Text2CAD** | CC BY-NC-SA 4.0, et aucune alternative permissive n'existe pour la CAO générative |
| **OpenVLA 7B** | 15 Go en bf16 pour 17,76 de budget ; SmolVLA rend la même capacité à un dixième du coût |

## Les deux décisions de licence, à relire

**`cad-recode` est `research-only`, et la restriction atteint le code.** Le
`LICENSE.md` du dépôt amont est un Attribution-NonCommercial 4.0 : les cent
quatorze lignes que l'adaptateur doit employer sont couvertes, pas seulement les
poids. Aucune n'est dans ce dépôt — le code amont se copie à la main dans
`runtimes/cad-recode/vendor/`, non versionné, et un script de découpe versionné
(`vendorer.py`) affiche le sha256 de ce qu'il extrait. Il n'existe **aucune
alternative permissive** pour cette capacité : CAD-Recode v1, cadrille et
cadrille-rl sont toutes en CC BY-NC. Précédent suivi : `arcface`, inscrit comme
référence de comparaison et non comme titulaire.

**`smolvla-libero` déclare apache-2.0 et porte `license_class: unknown`.** Le
publiant le déclare trois fois — champ, étiquette, corps du README — mais le
dépôt porte `base_model: lerobot/smolvla_base`, et ce parent n'a aucune licence
sur aucun canal. `license` enregistre ce que le publiant dit ; `license_class`
est notre jugement, et un dérivé ne transmet pas plus de droits qu'un parent muet
n'en concède. Le coût de ce choix est nul aujourd'hui (candidate, pas titulaire),
et il est à confirmer : si le parc juge qu'une déclaration suffit sans chaîne de
titres, la valeur devient `permissive` et le caveat reste.

## Ce que le lot a coûté au-delà des manifestes

Trois manques du socle sont apparus, et aucun n'était prévisible :

1. **`extra_sources`** — un variant peut avoir besoin de deux dépôts. Réclamé le
   même jour par CAD-Recode (tokenizer publié à part) et SmolVLA (dorsale
   visuelle). Ajouté au schéma, au miroir pydantic, à `pull`, à la comptabilité
   disque et au document transmis au worker.
2. **Les champs à cardinalité variable** — `multiview-to-3d` reçoit entre 2 et 32
   photos. Le type de média d'un tableau vit sur `items`, ce que trois lectures
   distinctes ignoraient. Et un `peak_scaling` sait désormais suivre une
   **longueur** de liste, sans quoi l'admission réservait 11,77 Go à un job qui
   en coûte 4,43.
3. **Le pic de `depth-anything` était mesuré au RSS** alors qu'il tourne sur
   Metal. Corrigé, remesuré : `da3-large@fp32` passe de 3,76 à **6,55 Go**, soit
   42,6 % de sous-déclaration sur le chiffre même dont dépend le contrôle
   d'admission. Le symptôme se lisait dans son profil depuis le début — une pente
   nulle avec un R² de 1,0.

## Plan de GC

**Aucun poste proposé.** Les 13,6 Go entrés ce jour sont tous référencés par un
manifeste mesuré, et le disque reste à 409 Gio libres sur 926. Un seul déchet
identifié, hors comptabilité du parc : un blob `.incomplete` de 134 Mo sous
`models--depth-anything--DA3-LARGE-1.1/blobs/`, reste d'un téléchargement
interrompu pendant l'instruction.

## Ce qui reste ouvert

- **Aucun golden set** pour ces neuf capacités, donc aucune confrontation et
  aucun titulaire. C'est la même dette que le reste du parc, et elle grandit.
- **L'index manque toujours**, et il sert maintenant **trois** capacités
  d'empreinte — `image-embed`, `protein-embed`, `geo-embed` — en plus de
  `face-embed`. Chacune rend un vecteur par job et un cosinus entre deux entrées ;
  « retrouver tout ce qui ressemble à ceci » demande une brique que le parc n'a
  pas. C'était déjà écrit au backlog ; c'est désormais quatre capacités qui
  l'attendent.
- **`robot-action` n'a pas d'usage vérifiable** : le parc n'a ni robot ni
  simulateur. Cinq contrôles restent possibles et sont faits — forme, domaine,
  reproductibilité, pente de coût, sensibilité à la consigne — mais aucun n'est
  sémantique. Le contrat le dit dans sa description au lieu de le masquer.
- **`geo-*` n'a aucune scène réelle** : la charge type est un GeoTIFF à six
  bandes fabriqué par recette. C'est ce que la politique d'assets exige, et cela
  suffit pour un coût ; cela ne suffira pas pour une qualité.
