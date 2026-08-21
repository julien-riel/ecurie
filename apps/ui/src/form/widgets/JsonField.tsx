/**
 * Un `type: "object"` sans `properties` — le JSON Schema qu'aucun formulaire ne rend.
 *
 * Un seul champ du parc est dans ce cas, et c'est le pire endroit possible :
 * `tool-use.tools[].parameters`, **requis**, qui porte le schéma des arguments
 * d'un outil. Son contenu est libre par nature — c'est un JSON Schema, transmis
 * tel quel au modèle et servant à valider l'appel émis.
 *
 * Mesuré sur RJSF 6.8 : un tel champ rend un `<fieldset>` avec sa légende, sa
 * description, et **aucun contrôle**. Le formulaire paraît complet, il ne l'est
 * pas, et la seule capacité du parc à tableau d'objets est cassée de bout en
 * bout sans que rien ne le signale. Mesuré également : `ui:widget` est **ignoré**
 * sur un champ de type objet ; seul `ui:field` le remplace.
 *
 * Le déclenchement passe par une règle de **forme** (`estObjetLibre`) et non par
 * l'identifiant du contrat : le socle ne connaît aucune capacité par son nom,
 * c'est ce qui permet d'en ajouter une sans toucher au front.
 */

import type { FieldProps } from "@rjsf/utils";
import { useEffect, useRef, useState } from "react";

/** Marque « ce champ n'a encore rien émis » — distincte de l'empreinte d'`undefined`. */
const NULLE = "\u0000jamais émis";

export function JsonField(props: FieldProps) {
  const { fieldPathId, formData, onChange, required, disabled, readonly, schema } = props;
  const [texte, setTexte] = useState(() =>
    formData === undefined ? "" : JSON.stringify(formData, null, 2),
  );
  const [faute, setFaute] = useState<string | null>(null);

  // Ce que ce champ vient lui-même de remonter.
  //
  // Sans ce garde, l'éditeur est inutilisable dès la deuxième frappe, et le
  // défaut est traître parce qu'il ne se voit qu'en tapant *à l'intérieur* d'un
  // objet : chaque fois que la saisie devient analysable, la valeur remonte,
  // revient par `formData`, et l'effet de synchronisation réécrit la zone avec
  // sa version réindentée. Écrire `value` sur un `<textarea>` contrôlé replace
  // le curseur en fin de texte — React ne restaure la sélection que si le focus
  // a changé, ce qui n'est pas le cas pendant la frappe. La suite de la saisie
  // atterrit alors après l'accolade fermante. Mesuré : partant de `{}`, taper
  // `"a":1,"b":2` entre les accolades donne `{\n  "a": 1\n},"b":2` et perd la
  // seconde clé.
  // `undefined` y est une valeur possible et non « rien » : c'est ce que
  // `JSON.stringify(undefined)` rend, et donc l'empreinte qui reviendra quand ce
  // champ efface sa valeur. Le drapeau vaut « aucune émission » avant la
  // première frappe, ce qui laisse la synchronisation faire son travail.
  const emis = useRef<string | undefined>(NULLE);

  // La valeur peut changer sans passer par la frappe — changement de variant,
  // ajout ou suppression d'un élément du tableau. Sans cette synchronisation,
  // l'éditeur afficherait l'objet du voisin.
  const empreinte = JSON.stringify(formData);
  useEffect(() => {
    if (emis.current === empreinte) return; // c'est notre propre valeur qui revient
    setTexte(formData === undefined ? "" : JSON.stringify(formData, null, 2));
    setFaute(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fieldPathId.$id, empreinte]);

  function saisir(brut: string) {
    setTexte(brut);
    if (brut.trim() === "") {
      setFaute(null);
      emis.current = JSON.stringify(undefined);
      onChange(undefined, fieldPathId.path);
      return;
    }
    try {
      const valeur = JSON.parse(brut);
      setFaute(null);
      emis.current = JSON.stringify(valeur);
      onChange(valeur, fieldPathId.path);
    } catch (cause) {
      // On garde le texte fautif à l'écran et on ne remonte rien : remonter un
      // objet à moitié tapé ferait clignoter des erreurs de contrat à chaque
      // caractère.
      setFaute(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <div className="form-group rjsf-field ecurie-json-libre">
      <label className="control-label" htmlFor={fieldPathId.$id}>
        {schema.title ?? props.name}
        {required ? <span className="required">*</span> : null}
      </label>
      {schema.description ? <div className="field-description">{schema.description}</div> : null}
      <textarea
        id={fieldPathId.$id}
        className="form-control"
        rows={6}
        spellCheck={false}
        value={texte}
        disabled={disabled}
        readOnly={readonly}
        placeholder='{ "type": "object", "properties": {} }'
        onChange={(e) => saisir(e.target.value)}
      />
      <p className="ecurie-etat-champ">
        JSON libre : ce champ porte un schéma, qu'aucun formulaire ne peut rendre en contrôles.
      </p>
      {faute ? <p className="text-danger">JSON invalide : {faute}</p> : null}
    </div>
  );
}
