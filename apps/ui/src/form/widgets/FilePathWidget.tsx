/**
 * `x-ui: "file"` — un chemin local, pas un téléversement.
 *
 * Le `FileWidget` de RJSF encode le fichier choisi en **data-URL base64** et le
 * place dans `formData` (mesuré sur RJSF 6.8 :
 * `"data:image/png;name=objet.png;base64,…"`). Le contrat, lui, déclare une
 * chaîne qui est un **chemin** : le worker l'ouvre depuis le disque. Envoyer une
 * data-URL ferait échouer le job, et pour une image de plusieurs mégaoctets, la
 * ferait d'abord traverser le corps d'une requête JSON.
 *
 * Il n'y a d'ailleurs aucune route de téléversement dans l'API — ni au 4.3, ni
 * dans les tâches du plan. Ce n'est pas un manque à contourner ici : l'API et le
 * navigateur tournent sur la **même machine**, un chemin local est donc une
 * réponse exacte, et c'est celle que la CLI attend déjà (`ecurie run -p
 * image=…`).
 *
 * Le sélecteur de fichier natif est montré mais inerte, et il le dit : le
 * navigateur ne donne pas le chemin réel d'un fichier choisi — seulement son nom
 * et son contenu. Le faire croire serait pire que de l'annoncer.
 */

import type { WidgetProps } from "@rjsf/utils";

export function FilePathWidget(props: WidgetProps) {
  const { id, value, required, disabled, readonly, onChange, onBlur, onFocus, options } = props;
  const types = Array.isArray(options?.accept) ? (options.accept as string[]) : [];

  return (
    <>
      <input
        id={id}
        className="form-control"
        type="text"
        value={value ?? ""}
        required={required}
        disabled={disabled}
        readOnly={readonly}
        spellCheck={false}
        placeholder="/chemin/vers/le/fichier"
        onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
        onBlur={(e) => onBlur(id, e.target.value)}
        onFocus={(e) => onFocus(id, e.target.value)}
      />
      <p className="ecurie-etat-champ">
        Chemin sur cette machine
        {types.length > 0 ? ` — types attendus : ${types.join(", ")}` : ""}.
      </p>
    </>
  );
}
