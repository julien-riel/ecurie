# Produire les douze enregistrements

Les textes de `manifest.json` sont figés. Les fichiers `assets/*.wav`, non : ils
n'existent pas encore, et c'est la seule chose qui manque à ce jeu.

## Pourquoi ils n'ont pas été fabriqués

Deux raccourcis étaient possibles, et aucun des deux ne tient.

**Synthétiser les extraits avec le moteur de voix du parc.** Ce serait immédiat et
la vérité terrain serait exacte, mais on mesurerait alors la transcription de
parole synthétique — sans hésitation réelle, sans coarticulation de locuteur
pressé, et surtout sans accent québécois, puisque aucune voix du parc n'en a un.
Le jeu perdrait précisément ce pour quoi il existe.

**Reprendre un corpus public.** Common Voice offre des extraits en CC0, y compris
de locuteurs québécois, mais on n'importe pas de la donnée dans le registre sans
en suivre la licence et la provenance — et surtout sans décider si l'on veut d'un
jeu dont les modèles évalués ont peut-être vu les phrases à l'entraînement. C'est
une question à trancher, pas à contourner.

Restait donc à enregistrer, ce qui prend une demi-heure et se fait à un moment
choisi.

## Format attendu

- WAV, 16 kHz, mono, PCM 16 bits, sans normalisation ni réduction de bruit ;
- un fichier par cas, nommé exactement comme `input.audio` du manifeste ;
- pas de silence de plus d'une seconde en tête ou en queue ;
- le texte de `reference.text` lu **tel quel**, sans le corriger : s'il porte une
  hésitation, elle se dit.

## Ce que chaque cas demande

| Cas | Locuteur | Conditions |
|---|---|---|
| `qc-quotidien` | québécois, registre familier | calme |
| `qc-accent-marque` | québécois, accent marqué | calme |
| `qc-technique` | québécois | calme |
| `qc-alternance-anglais` | québécois à l'aise en anglais | calme |
| `qc-noms-propres` | québécois | calme |
| `qc-hesitations` | québécois | calme, débit spontané |
| `qc-debit-rapide` | québécois | calme, débit soutenu |
| `fr-standard` | français standard, diction soignée | calme |
| `chiffres-montants` | indifférent | calme |
| `sigles-codes` | indifférent | calme |
| `bruit-de-fond-temoin` | **le même que le suivant** | calme |
| `bruit-de-fond` | **le même que le précédent** | fond de café, rapport signal sur bruit ≈ 10 dB |

Les deux derniers forment une paire : même phrase, même voix, une seule
différence. Enregistrer le témoin d'abord, puis rejouer la même prise avec le
fond ajouté — ou refaire la prise dans le bruit, à condition que ce soit bien le
même locuteur le même jour.

## Une fois les fichiers en place

Retirer la clé `pending` des cas concernés et passer `status` à `complet` quand
les douze y sont. Rien d'autre ne change : ni les textes, ni les identifiants, ni
les chemins. C'est ce qui permet de comparer un résultat obtenu après
l'enregistrement à un jeu figé avant lui.
