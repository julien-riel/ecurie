/** L'éditeur de JSON libre — le seul champ du parc qu'aucun formulaire ne rend. */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, test } from "vitest";
import { commeCapability, contrat } from "../../schema/__fixtures__/contrats";
import { CapabilityForm } from "../CapabilityForm";

function Banc(props: {
  depart?: Record<string, unknown>;
  onData?: (d: Record<string, unknown>) => void;
}) {
  const [formData, setFormData] = useState<Record<string, unknown>>(props.depart ?? {});
  return (
    <CapabilityForm
      capability={commeCapability(contrat("tool-use"))}
      variant={null}
      formData={formData}
      onChange={(d) => {
        setFormData(d);
        props.onData?.(d);
      }}
    />
  );
}

function zone(): HTMLTextAreaElement {
  return document.getElementById("root_tools_0_parameters") as HTMLTextAreaElement;
}

describe("la saisie d'un schéma libre", () => {
  test("on peut taper a l_interieur d_un objet deja valide", async () => {
    // Défaut trouvé en revue, reproduit au caractère près : la valeur remontait
    // dès qu'elle devenait analysable, revenait réindentée par formData, et
    // réécrire un <textarea> contrôlé replace le curseur en fin de texte. Tout
    // ce qui suivait atterrissait après l'accolade fermante — mesuré, partant de
    // {} et tapant "a":1,"b":2 : `{\n  "a": 1\n},"b":2`, seconde clé perdue.
    let vu: Record<string, unknown> = {};
    render(<Banc depart={{ tools: [{ name: "chercher", parameters: {} }] }} onData={(d) => (vu = d)} />);

    const champ = zone();
    await userEvent.type(champ, '"a":1,"b":2', {
      initialSelectionStart: 1,
      initialSelectionEnd: 1,
    });

    const outils = vu["tools"] as { parameters?: unknown }[];
    expect(outils[0]!.parameters).toEqual({ a: 1, b: 2 });
    expect(screen.queryByText(/^JSON invalide :/)).not.toBeInTheDocument();
  });

  test("une valeur venue d_ailleurs resynchronise l_editeur", async () => {
    // Le garde ne doit ignorer QUE la valeur que le champ vient d'émettre : un
    // changement de variant, ou l'ajout d'un élément au tableau, doit toujours
    // rafraîchir la zone, sans quoi elle afficherait l'objet du voisin.
    function Deux() {
      const [data, setData] = useState<Record<string, unknown>>({
        tools: [{ name: "a", parameters: { origine: 1 } }],
      });
      return (
        <>
          <button type="button" onClick={() => setData({ tools: [{ name: "b", parameters: { origine: 2 } }] })}>
            changer
          </button>
          <CapabilityForm
            capability={commeCapability(contrat("tool-use"))}
            variant={null}
            formData={data}
            onChange={setData}
          />
        </>
      );
    }
    render(<Deux />);
    expect(JSON.parse(zone().value)).toEqual({ origine: 1 });

    await userEvent.click(screen.getByRole("button", { name: "changer" }));
    expect(JSON.parse(zone().value)).toEqual({ origine: 2 });
  });

  test("vider le champ efface la valeur", async () => {
    let vu: Record<string, unknown> = {};
    render(
      <Banc
        depart={{ tools: [{ name: "a", parameters: { type: "object" } }] }}
        onData={(d) => (vu = d)}
      />,
    );
    await userEvent.clear(zone());
    const outils = vu["tools"] as { parameters?: unknown }[];
    expect(outils[0]!.parameters).toBeUndefined();
    expect(zone().value).toBe("");
  });
});
