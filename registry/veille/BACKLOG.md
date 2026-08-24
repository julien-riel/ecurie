# Backlog de veille — pistes à instruire

Proposé le 2026-08-23. **Ce fichier n'est pas une veille** : rien n'y est
vérifié — ni la licence, ni l'existence de poids ouverts, ni le support Apple
Silicon, ni le budget mémoire. C'est la file d'entrée du skill `veille-modeles`,
qui décide, mesure et rend un rapport daté sous `registry/veille/<date>/`.

Ce qu'il apporte par rapport à la liste d'origine : le **croisement avec le parc
réel** (46 modèles, 26 capacités au 23 août 2026 ; 54 et 32 depuis le 24). Une
piste qui double un titulaire ne se traite pas comme une piste qui ouvre une
capacité — la seconde coûte un contrat, un adaptateur, une charge type et
souvent un runtime.

## Ordre de priorité voulu

Julien place en tête **séries temporelles, CAO, géospatial, reconstruction
spatiale, robotique et science**, pour une raison qui vaut d'être gardée : ces
familles ne produisent pas du contenu, elles transforment des données en
**mesures, géométrie, prévisions, programmes ou actions**. C'est aussi ce qui
les rend coûteuses à faire entrer — aucune ne se branche sur un contrat
existant.

Une exception d'opportunité : la famille **visage** (§D) n'est pas dans cette
liste, mais elle contient la seule piste annoncée comme optimisée pour Apple
Silicon (MLX-UniFace). Elle coûterait moins cher à instruire que les six autres,
et c'est un argument d'ordonnancement, pas de valeur.

> **Fait le 2026-08-24.** Le pari était juste : six capacités pour un runtime de
> vingt lignes de `pyproject.toml`, aucun code amont à vendorer. La file revient
> donc à l'ordre voulu ci-dessus — séries temporelles en tête.

---

## A. Concurrents de capacités déjà servies

Le parc a déjà un titulaire ou un candidat mesuré. La question n'est pas « est-ce
que ça marche » mais « est-ce meilleur que ce qu'on a, sur la charge type ».

| Piste | Capacité du parc | Ce que le parc a déjà | Ce qui déciderait |
|---|---|---|---|
| **SAM 3.1** | `image-segment` | `sam3` (mesuré : 1,35 s, 3,87 Go) | Le suivi vidéo, que `sam3` ne fait pas — c'est une capacité de plus, pas un remplacement |
| **Grounding DINO** | `image-detect` | trois VLM (`qwen3-vl-8b`, `gemma4-12b`, `qwen36-27b@mxfp4`) | Un détecteur dédié contre des VLM généralistes : latence et rappel sur la charge `image-detect` |
| **DWPose** | `video-to-motion` | `rtmw3d-x@onnx` (3,7 s, 1,73 Go) | Visage et mains, que RTMW3D ne rend pas ; mais RTMW3D sort de la 3D |
| **Qwen3-ASR** (0.6B / 1.7B) | `speech-to-text` | `moss-transcribe@bf16` (2,7 s) | 52 langues annoncées contre le périmètre de MOSS ; poids MLX disponibles ? |
| **pyannote.audio** | `speaker-diarization` | `moss-transcribe-diarize@bf16` | pyannote est la référence du domaine ; tourne-t-il hors CUDA ? |
| **ACE-Step 1.5** | `text-to-music` | `minimax-music3@4bit` (81 s, 14,34 Go) | Le plus lourd job du parc — un concurrent moins gourmand changerait l'usage |
| **PaddleOCR-VL 1.5**, **DeepSeek-OCR2** | `document-to-text` | 5 variantes VLM | Des OCR dédiés contre des VLM : tableaux et formules, là où les VLM dérapent |
| **Table Transformer**, **LayoutLMv3** | `document-to-text` | — | Sorties structurées (cellules, champs de formulaire) que le contrat actuel ne sait pas porter : contrat à étendre |
| **Depth Anything 3** | `depth-estimation` | `da3-large@fp32` — **c'est déjà lui** | Rien à faire ; la variante *Streaming* relève de la reconstruction (§C) |
| **Demucs** | `audio-separation` | `htdemucs-mlx@fp32-mlx` — **c'est déjà lui** | Adaptateur écrit le 24 août ; manque une charge type musicale |
| **DeepFilterNet** | `audio-denoise` | `deepfilternet3-mlx` — **c'est déjà lui** | Adaptateur écrit mais **bloqué** : DSP non validé (voir §B) |
| **Qwen3-TTS** | `text-to-speech` | `qwen3-tts-1.7b@8bit-mlx` — **titulaire** | Rien à faire |

## B. Dettes ouvertes sur des capacités déjà déclarées

Constaté par la campagne d'essai des 23–24 août, à solder avant d'élargir.

| Capacité | État | Ce qui manque |
|---|---|---|
| `audio-denoise` | adaptateur écrit, **refuse de servir** | Comparer le DSP réimplémenté à `libdf` (demande une chaîne Rust) |
| `audio-separation` | sert | Une charge type **musicale** — l'asset actuel est une voix TTS, hors domaine MUSDB18 |
| `text-to-video`, `image-to-video` | servent | Charges types versionnées, puis profils aux manifestes |
| `image-segment` | sert | Un cas « désignation par le nom » dans la charge type, sans quoi SAM 3 reste non profilable |
| `depth-estimation` | sert | Charge type versionnée |
| `image-to-mesh` | sert (Hunyuan3D mesuré le 24 août) | `trellis2` a une révision `0000000` : à épingler ou à retirer |

## C. Capacités nouvelles — contrat à écrire

Chacune demande, au minimum : un contrat dans `registry/capabilities/`, une
charge type figée, un adaptateur, et souvent un runtime isolé de plus.

### Séries temporelles — `time-series-forecast`
**Chronos-2** (120 M), **TimesFM 2.5** (200 M), **Moirai 2.0**. Petits modèles,
donc un coût mémoire dérisoire à l'échelle du parc. La difficulté n'est pas là :
le contrat doit porter des **séries en entrée et des quantiles en sortie**, ce
qu'aucun contrat actuel ne ressemble. Usages cités : consommation électrique,
trafic, ventes, télémétrie IoT, charge réseau.

### CAO — `pointcloud-to-cad`, `text-to-cad`
**CAD-Recode** (nuage de points → programme CadQuery éditable), **Text2CAD**
(texte → séquence paramétrique), variante agentique qui pilote FreeCAD et
vérifie sa géométrie. Sortie inhabituelle pour le parc : **du code et du STEP**,
pas un fichier binaire opaque. Prolonge naturellement `image-to-mesh`, qui ne
rend qu'un maillage.

### Géospatial — `geo-segment`, `geo-embed`
**TerraMind** (IBM/ESA), **Prithvi EO 2.0** (NASA/IBM, 100–600 M), **DINOv3 Geo**
(hauteur de canopée). Plus le pipeline « SAM + orthophoto » pour vectoriser
bâtiments, routes et plans d'eau. Entrée multi-bandes : le contrat ne peut pas
être `image/png`.

### Reconstruction spatiale — `multiview-to-3d`
**VGGT** (photos → caméras, profondeur, nuage de points), **WorldMirror**
(vidéo → scène), **Depth Anything 3 Streaming** (séquences longues),
**Segment Anything 3D** (masques 2D → nuage). Sorties : poses de caméra, nuages
de points. Ambition affichée : un jumeau numérique plutôt qu'une image.

### Robotique — `robot-action`
**OpenVLA** (image + consigne → commandes articulaires), **V-JEPA 2-AC** (world
model conditionné par l'action), **FAST** (représentation compacte des
mouvements). Suppose un contrat qui décrive un robot ; sans matériel, l'intérêt
est la mesure du modèle, pas l'usage.

### Science — protéines, chimie
**ESM3** (génératif), **ESM-C** (embeddings), **Boltz-2** (structure de
complexes, affinité de liaison), **MatterGen** (cristaux, sortie `.cif`). Les
plus éloignés du parc actuel, et ceux dont les sorties se vérifient le mieux :
une structure prédite se compare à une structure connue.

### Embeddings et similarité — `image-embed`
**DINOv3** (image → vecteur), ~~**ArcFace** (visage → identité)~~, **ESM-C**
(protéine → vecteur). Une capacité qui ne rend ni texte ni fichier mais un
**vecteur** : elle appelle un index, donc une brique que le parc n'a pas.

> **Le volet visage est fait**, sous son propre contrat `face-embed` plutôt que
> fondu ici : redresser un visage sur ses cinq points d'ancrage n'a pas
> d'équivalent pour une pièce mécanique ou une protéine, et le mêler à
> `image-embed` aurait donné un contrat dont la moitié des paramètres ne
> s'appliquent qu'à la moitié des modèles. L'index reste à faire, et il servira
> les trois. Le contournement retenu en attendant — `compare_to`, qui rend un
> cosinus entre deux images — vaut pour vérifier, pas pour chercher. C'est
aussi le socle de « trouver toutes les pièces qui ressemblent à celle-ci » et de
la détection d'anomalies industrielle.

### Mesure — `image-metrology`
**GaugeAnything** : segmentation + métrologie → largeur de fissure, diamètre,
espacement. Sortie chiffrée avec unité, donc un contrat qui doit porter une
**échelle** (calibration), sans quoi le résultat est un nombre de pixels.

### Compréhension vidéo — `video-anticipate`
**V-JEPA 2 / 2.1**, **MotionLLM**. Se distingue de `video-to-text` : décrire ce
qui s'est passé contre anticiper ce qui va se passer.

### Alignement audio — `audio-align`
**Qwen3 ForcedAligner** : texte + audio → horodatages au mot. Complète
`speech-to-text`, dont le contrat ne porte pas d'alignement fin. Petit modèle,
même famille que le TTS titulaire : la piste la moins coûteuse de cette section.

### Édition musicale — `music-inpaint`, `music-accompany`, `stem-extract`
**ACE-Step Repaint** (régénérer quelques secondes), **Vocal2BGM** (voix →
accompagnement), **Extract** (un instrument d'un mix). Trois capacités distinctes
sur les mêmes poids, comme `sdxl-base` sert déjà trois contrats.

### Météo — `weather-forecast`
**Microsoft Aurora**. Entrée : des champs atmosphériques mondiaux, pas un
fichier local. À traiter en dernier : la donnée d'entrée est un projet à elle
seule.

## D. Visage — TRAITÉE LE 2026-08-24

**Cette section est soldée.** Six capacités sont ouvertes et mesurées, un runtime
`uniface` est entré au parc, huit manifestes sont en `status: candidate` — voir
[le rapport du 2026-08-24](2026-08-24/RAPPORT.md). Ce qui suit est le texte
d'origine, conservé pour ce qu'il annonçait ; les écarts constatés sont dans le
rapport, et il y en a trois qui valent d'être connus avant d'instruire les autres
sections :

- **« MLX-UniFace » n'est pas un projet MLX.** C'est UniFace, une couche MIT
  au-dessus d'onnxruntime. La conclusion du backlog tient — c'est bien la piste
  la moins coûteuse —, mais pour une autre raison que celle écrite.
- **Le tri par licence a été fait en amont, et c'est ce qui a décidé.** Le dépôt
  de poids porte un tableau vérifié source par source, jeux d'entraînement
  compris. Trois des six capacités sont `restricted` non par leur code mais par
  leurs données ; deux sont franchement permissives.
- **La question d'usage est tranchée** : `human_subject` sur le contrat de
  capacité, à trois valeurs. `voice-clone`, qui était au parc depuis le v0.3, en
  porte une — la preuve que le champ ne servait pas qu'à cette famille.

Restent hors périmètre, avec leur raison : `face-restore` (vendoring PyTorch),
`image-to-face-mesh` (FLAME sous licence de recherche, §F.3),
`portrait-animate` (non instruit), `face-expression` (licence des poids non
établie, §F.3).

---

### Texte d'origine (2026-08-23)


Assez fournie pour ne pas se diluer dans les sections précédentes, et elle a une
particularité : **MLX-UniFace est annoncé optimisé Apple Silicon**, ce qui en
fait la seule piste de tout ce backlog à passer d'emblée le filtre du §F.1. Elle
mérite d'être instruite en premier, ne serait-ce que pour savoir ce que le parc
gagnerait sans nouveau runtime.

| Sous-famille | Pistes | Capacité visée |
|---|---|---|
| Détection et points clés | **MLX-UniFace**, **SCRFD**, **MediaPipe Face Landmarker / Face Mesh**, **InsightFace** | `face-detect` — boîtes + landmarks ; MediaPipe rend des centaines de points 3D en temps réel |
| Identité | **ArcFace**, **InsightFace** | rejoint `image-embed` (§C) : visage → vecteur, puis un index |
| Expressions | **LibreFace 2.0**, **EMOCA** | `face-expression` — Action Units, direction du regard |
| Reconstruction 3D | **DECA**, **EMICA / INFERNO**, **MICA**, sur la représentation **FLAME** | `image-to-face-mesh` — voisin d'`image-to-mesh`, mais paramétrique : la sortie est un jeu de coefficients FLAME, pas un maillage libre |
| Animation de portrait | **LivePortrait**, **LivePortrait AudioDriven**, **JoyVASA** | `portrait-animate` — proche d'`image-to-video`, mais piloté par une vidéo ou par de l'audio |
| Restauration | **GFPGAN**, **CodeFormer**, **OSDFace** | `face-restore` — à ne pas confondre avec `image-upscale` (`swin2sr`, `seedvr2`) : ces modèles **inventent** un visage plausible plutôt que d'agrandir |
| Segmentation autour du visage | **SAM 3 / 3.1** | déjà au parc (`sam3`) |
| Échange et réanimation | **UniBioTransfer** | face swap, remplacement de tête, reenactment |

Trois remarques avant d'ouvrir quoi que ce soit :

- **FLAME n'est pas un modèle à exécuter**, c'est la représentation commune que
  DECA, EMOCA, EMICA et MICA partagent. S'il entre au parc, c'est comme format
  de sortie du contrat, pas comme variant.
- **`face-restore` et `image-upscale` doivent rester deux contrats.** Le parc
  distingue déjà interpoler (`swin2sr`) et régénérer (`seedvr2`) ; restaurer un
  visage va plus loin — le résultat ressemble à quelqu'un, et pas forcément à la
  bonne personne. Un caveat de manifeste ne suffira pas, la distinction est dans
  le contrat.
- **Deux sous-familles appellent une décision d'usage, pas seulement technique.**
  La reconnaissance faciale identifie des personnes réelles ; le face swap et le
  talking head produisent des images d'une personne faisant ce qu'elle n'a pas
  fait. Le registre a `license_class` pour les restrictions juridiques, il n'a
  rien pour cet usage-là. À trancher au moment du contrat, pas après.

## E. Pipelines, et pourquoi le parc ne sait pas encore les dire

Plusieurs pistes ne sont pas des modèles mais des **chaînes** : Grounding DINO →
SAM (annotation), SAM 3 → Depth Anything 3 (forme + position), DWPose → avatar
3D, Depth Anything 3 → SAM 3 (pièce meublée). Or un job Écurie appelle
aujourd'hui **un** variant sur **un** contrat. Enchaîner suppose une notion de
composition — ce que l'`ARCHITECTURE.md` §10 esquisse pour `text-to-mesh`
(route A, deux temps) et n'a jamais généralisé. À trancher avant d'ajouter des
modèles qui n'ont d'intérêt qu'en chaîne.

## F. Le filtre qui décidera, avant toute autre considération

1. **Apple Silicon.** Le parc n'exécute que MLX, PyTorch/MPS, ONNX et
   `depth-anything`. `trellis2` est déjà bloqué par ses noyaux CUDA ; plusieurs
   pistes ci-dessus le seront aussi. À vérifier **avant** d'écrire un manifeste.
2. **17,76 Gio de budget.** Mesuré : le 27B en 4bit n'y tient pas, en mxfp4 il
   occupe jusqu'à 98 %. Un foundation model d'observation de la Terre ou un
   world model vidéo est à chiffrer avant d'être téléchargé.
3. **Licence.** `license_class` est obligatoire au manifeste. Plusieurs pistes
   sont sous licence de recherche seulement — ESM3 et MatterGen notamment.
4. **Coût d'entrée réel d'une capacité neuve.** Contrat + charge type figée +
   adaptateur + parfois runtime. La campagne du 24 août a montré le prix d'un
   adaptateur absent : la capacité paraît servie, et elle ne l'est pas.
