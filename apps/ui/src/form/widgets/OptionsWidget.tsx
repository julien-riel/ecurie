/**
 * `x-ui: "select"` — une liste de suggestions, jamais une liste fermée.
 *
 * Le choix du combobox plutôt que du `<select>` vient d'un fait de la surface
 * d'API, pas d'une préférence d'ergonomie : **les options d'un `x-options-from`
 * n'existent qu'après le premier chargement du modèle.** Elles viennent du champ
 * `options` d'un résident (`GET /runtime/residents`), c'est-à-dire de ce que le
 * worker a annoncé en réponse à `loaded`. Tant qu'aucun worker ne tourne, la
 * liste est vide — et c'est l'état exact du parc à l'instant où l'on ouvre
 * l'écran pour la première fois.
 *
 * Mesuré sur RJSF 6.8 : un `ui:widget: "select"` sur un champ sans `enum` rend un
 * `<select>` ne contenant qu'une seule option, la chaîne vide. Le champ est
 * inutilisable, et `translation.target_language` — qui est **requis** — rendrait
 * le formulaire impossible à soumettre.
 *
 * Une liste vide n'est pas davantage un défaut : le worker `mlx_vlm` annonce
 * `languages: []` volontairement, parce qu'il les accepte toutes. Le champ dit
 * donc ce qu'il en sait, et laisse taper.
 */

import type { WidgetProps } from "@rjsf/utils";

export function OptionsWidget(props: WidgetProps) {
  const { id, value, required, disabled, readonly, onChange, onBlur, onFocus, options } = props;
  const suggestions = Array.isArray(options?.suggestions) ? (options.suggestions as string[]) : [];
  const resident = options?.resident === true;
  const listeId = `${id}__suggestions`;

  const état = !resident
    ? "valeurs connues après le premier chargement du modèle — la saisie reste libre"
    : suggestions.length === 0
      ? "ce modèle n'annonce aucune valeur : il les accepte toutes"
      : `${suggestions.length} valeur(s) annoncée(s) par le modèle chargé`;

  return (
    <>
      <input
        id={id}
        className="form-control"
        type="text"
        list={listeId}
        value={value ?? ""}
        required={required}
        disabled={disabled}
        readOnly={readonly}
        autoComplete="off"
        onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
        onBlur={(e) => onBlur(id, e.target.value)}
        onFocus={(e) => onFocus(id, e.target.value)}
      />
      <datalist id={listeId}>
        {suggestions.map((s) => (
          <option key={s} value={s} />
        ))}
      </datalist>
      <p className="ecurie-etat-champ">{état}</p>
    </>
  );
}
