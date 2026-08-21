import type { ViewerProps } from "../registry";

/**
 * Une sortie qui n'est pas un fichier : un nombre, un booléen, une liste.
 *
 * Le discriminant n'est jamais le nom de la clé mais la présence d'un
 * `contentMediaType` dans `output_media_types`. `document-to-text.page_count`,
 * `tool-use.call_names` et `speech-to-text.language` sont des valeurs ;
 * `document-to-text.text` est un chemin. Rien dans leur nom ne le dit.
 */
export function InlineViewer({ nom, valeur }: ViewerProps) {
  const texte =
    typeof valeur === "string" ? valeur : JSON.stringify(valeur, null, 2);
  return (
    <div className="ecurie-sortie ecurie-sortie-inline" data-viewer="inline">
      <span className="ecurie-sortie-nom">{nom}</span>
      <code>{texte}</code>
    </div>
  );
}
