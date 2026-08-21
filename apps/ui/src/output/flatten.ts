/**
 * Aplatir la sortie d'un job en une liste de choses à montrer.
 *
 * Le parcours porte sur la **réponse**, jamais sur les `properties` du contrat, et
 * c'est la seule décision de ce fichier. `audio-separation` déclare cinq pistes
 * — voix, accompagnement, batterie, basse, autre — et n'en produit que deux ou
 * quatre selon le paramètre `stems`. Itérer le contrat afficherait cinq lecteurs, dont
 * trois pointant vers des fichiers qui n'existent pas.
 *
 * Les clés d'`output_media_types` sont des **chemins pointés** — `tracks.vocals`
 * et non `vocals` — parce que le serveur descend récursivement dans les sorties
 * de type objet. Le chemin est donc construit pendant la descente, et c'est lui
 * qui interroge la table.
 *
 * Enfin, une clé produite hors contrat est affichée quand même : le méta-schéma
 * n'impose `additionalProperties: false` qu'en **entrée**, et une sortie
 * supplémentaire est une information, pas une faute.
 */

export interface RenderedOutput {
  /** Chemin pointé, clé d'`output_media_types` et identité de la sortie. */
  chemin: string;
  /** Dernière composante du chemin. */
  nom: string;
  valeur: unknown;
  /** `null` quand la valeur n'est pas un fichier. */
  mediaType: string | null;
}

function estObjetSimple(valeur: unknown): valeur is Record<string, unknown> {
  return typeof valeur === "object" && valeur !== null && !Array.isArray(valeur);
}

export function flattenOutput(
  sortie: Record<string, unknown> | null | undefined,
  mediaTypes: Record<string, string>,
): RenderedOutput[] {
  const rendus: RenderedOutput[] = [];

  function descendre(noeud: Record<string, unknown>, prefixe: string): void {
    for (const [clé, valeur] of Object.entries(noeud)) {
      const chemin = prefixe ? `${prefixe}.${clé}` : clé;
      const mediaType = mediaTypes[chemin];

      if (mediaType !== undefined) {
        // Un type de média déclaré arrête la descente : la valeur est un chemin
        // de fichier, même si elle est structurée.
        rendus.push({ chemin, nom: clé, valeur, mediaType });
        continue;
      }
      if (estObjetSimple(valeur)) {
        descendre(valeur, chemin);
        continue;
      }
      rendus.push({ chemin, nom: clé, valeur, mediaType: null });
    }
  }

  if (sortie) descendre(sortie, "");
  return rendus;
}
