/**
 * Encodage WAV PCM 16 bits — parce que le parc ne sait pas lire ce que le micro produit.
 *
 * `MediaRecorder` n'écrit pas de WAV : aucun navigateur ne l'offre. Chrome rend
 * du `audio/webm;codecs=opus`, Safari du `audio/mp4;codecs=aac`. Or l'env du
 * runtime `mlx-audio` le dit noir sur blanc dans son `pyproject.toml` :
 * *« ffmpeg non plus : l'écriture WAV passe par miniaudio… Il ne redeviendrait
 * nécessaire que pour flac/mp3/ogg/opus. »* Déposer un fichier opus dans le sas
 * produirait un job qui échoue au décodage, plusieurs secondes après le clic, et
 * pour une raison illisible depuis l'écran.
 *
 * Le navigateur sait pourtant décoder ce qu'il vient d'encoder — c'est le même
 * moteur — et `decodeAudioData` rend des échantillons flottants. De là au WAV il
 * n'y a qu'un en-tête de 44 octets et une conversion. C'est ce que fait ce
 * fichier, et rien d'autre : aucune API du navigateur n'y est touchée, ce qui le
 * rend vérifiable octet par octet.
 *
 * **Le mixage en mono est un choix, pas une simplification.** Les modèles de ce
 * parc qui prennent du son — transcription, diarisation, description, débruitage
 * — travaillent tous sur un canal, et lui en donner deux les fait commencer par
 * les moyenner. Le faire ici divise par deux ce qui traverse le disque et le
 * corps de la requête. La séparation de pistes est la seule capacité qui
 * préférerait le stéréo, et elle ne s'alimente pas au micro d'un portable.
 */

/** Le seul format que toutes les bibliothèques audio du parc ouvrent sans ffmpeg. */
export const TYPE_WAV = "audio/wav";

const OCTETS_PAR_ECHANTILLON = 2;
const TAILLE_ENTETE = 44;

/**
 * Mixe les canaux d'un buffer décodé en un seul.
 *
 * La moyenne, et non la somme : deux canaux identiques sommés saturent à la
 * moindre crête, et l'écrêtage s'entend là où l'atténuation ne s'entend pas.
 */
export function versMono(canaux: readonly Float32Array[]): Float32Array {
  if (canaux.length === 0) return new Float32Array(0);
  const premier = canaux[0]!;
  if (canaux.length === 1) return premier;
  const mixé = new Float32Array(premier.length);
  for (let i = 0; i < mixé.length; i += 1) {
    let somme = 0;
    for (const canal of canaux) somme += canal[i] ?? 0;
    mixé[i] = somme / canaux.length;
  }
  return mixé;
}

/**
 * Un flottant de [-1, 1] vers un entier signé 16 bits.
 *
 * Le bornage précède la conversion : `decodeAudioData` peut rendre des valeurs
 * hors de l'intervalle — un filtre ou un gain amont les y pousse — et les
 * laisser déborder ferait boucler l'entier, ce qui s'entend comme un claquement
 * et non comme une saturation.
 */
export function versPcm16(echantillon: number): number {
  const borné = Math.max(-1, Math.min(1, echantillon));
  return Math.round(borné < 0 ? borné * 0x8000 : borné * 0x7fff);
}

/**
 * Un WAV PCM 16 bits mono, prêt à être déposé.
 *
 * La fréquence est celle des échantillons reçus et n'est jamais convertie ici :
 * rééchantillonner est le travail du worker, qui sait à quelle fréquence son
 * modèle veut entendre — 16 kHz pour la transcription, 48 pour le débruitage.
 * Le faire deux fois dégraderait le signal pour rien.
 */
export function encoderWav(echantillons: Float32Array, frequence: number): Blob {
  const octets = new ArrayBuffer(TAILLE_ENTETE + echantillons.length * OCTETS_PAR_ECHANTILLON);
  const vue = new DataView(octets);
  const tailleDonnees = echantillons.length * OCTETS_PAR_ECHANTILLON;

  const ascii = (position: number, texte: string) => {
    for (let i = 0; i < texte.length; i += 1) vue.setUint8(position + i, texte.charCodeAt(i));
  };

  ascii(0, "RIFF");
  vue.setUint32(4, 36 + tailleDonnees, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  vue.setUint32(16, 16, true); // taille du bloc fmt pour du PCM entier
  vue.setUint16(20, 1, true); // 1 = PCM sans compression
  vue.setUint16(22, 1, true); // un canal
  vue.setUint32(24, frequence, true);
  vue.setUint32(28, frequence * OCTETS_PAR_ECHANTILLON, true); // octets par seconde
  vue.setUint16(32, OCTETS_PAR_ECHANTILLON, true); // alignement d'un bloc
  vue.setUint16(34, 8 * OCTETS_PAR_ECHANTILLON, true);
  ascii(36, "data");
  vue.setUint32(40, tailleDonnees, true);

  let position = TAILLE_ENTETE;
  for (const echantillon of echantillons) {
    vue.setInt16(position, versPcm16(echantillon), true);
    position += OCTETS_PAR_ECHANTILLON;
  }

  return new Blob([octets], { type: TYPE_WAV });
}

/** La durée d'un enregistrement, en secondes — ce qu'on affiche sous le bouton. */
export function dureeSecondes(echantillons: Float32Array, frequence: number): number {
  return frequence > 0 ? echantillons.length / frequence : 0;
}
