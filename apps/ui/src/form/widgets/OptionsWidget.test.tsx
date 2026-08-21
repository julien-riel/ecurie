/**
 * Le champ `x-ui: select`, et le fait qui le gouverne : ses options n'existent
 * qu'après le premier chargement du modèle.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, test } from "vitest";
import type { Capability } from "../../api/types";
import { commeCapability, contrat } from "../../schema/__fixtures__/contrats";
import { CapabilityForm } from "../CapabilityForm";

function Banc(props: {
  capability: Capability;
  runtimeOptions?: Record<string, unknown>;
  resident?: boolean;
  onData?: (d: Record<string, unknown>) => void;
}) {
  const [formData, setFormData] = useState<Record<string, unknown>>({});
  return (
    <CapabilityForm
      capability={props.capability}
      variant={null}
      runtimeOptions={props.runtimeOptions}
      resident={props.resident}
      formData={formData}
      onChange={(d) => {
        setFormData(d);
        props.onData?.(d);
      }}
    />
  );
}

describe("le champ à options de runtime", () => {
  test("un select sans options reste utilisable", async () => {
    // Mesuré sur RJSF 6.8 : un ui:widget "select" sur un champ sans enum rend un
    // <select> ne contenant qu'une option, la chaîne vide. Aucun worker n'est
    // résident au premier lancement — c'est-à-dire au moment précis où l'on veut
    // essayer une voix.
    const capability = commeCapability(contrat("text-to-speech"));
    let vu: Record<string, unknown> = {};
    render(<Banc capability={capability} onData={(d) => (vu = d)} />);

    const champ = document.getElementById("root_voice") as HTMLInputElement;
    expect(champ.tagName).toBe("INPUT");
    expect(champ.disabled).toBe(false);

    await userEvent.type(champ, "serena");
    expect(vu["voice"]).toBe("serena");
  });

  test("sans resident le champ annonce pourquoi la liste est vide", () => {
    render(<Banc capability={commeCapability(contrat("text-to-speech"))} />);
    expect(screen.getByText(/après le premier chargement du modèle/)).toBeInTheDocument();
  });

  test("une liste de valeurs vide est un etat normal", () => {
    // Le worker mlx_vlm annonce languages: [] volontairement : il les accepte
    // toutes. Ce n'est ni une panne ni une raison de bloquer la saisie.
    render(
      <Banc
        capability={commeCapability(contrat("document-to-text"))}
        runtimeOptions={{ languages: [] }}
        resident
      />,
    );
    expect(screen.getByText(/n'annonce aucune valeur : il les accepte toutes/)).toBeInTheDocument();
    expect((document.getElementById("root_language") as HTMLInputElement).disabled).toBe(false);
  });

  test("les options d_un resident alimentent les suggestions", () => {
    render(
      <Banc
        capability={commeCapability(contrat("text-to-speech"))}
        runtimeOptions={{ voices: ["serena", "ethan", "chelsie"], max_pages: 12 }}
        resident
      />,
    );
    const liste = document.getElementById("root_voice__suggestions") as HTMLDataListElement;
    expect([...liste.options].map((o) => o.value)).toEqual(["serena", "ethan", "chelsie"]);
    expect(screen.getByText(/3 valeur\(s\) annoncée\(s\)/)).toBeInTheDocument();
  });

  test("un champ requis a options reste soumettable", async () => {
    // translation.target_language est REQUIS et porte x-options-from : un select
    // fermé et vide rendrait le formulaire impossible à remplir.
    const capability = commeCapability(contrat("translation"));
    expect(capability.required).toContain("target_language");

    let vu: Record<string, unknown> = {};
    render(<Banc capability={capability} onData={(d) => (vu = d)} />);
    const champ = document.getElementById("root_target_language") as HTMLInputElement;
    await userEvent.type(champ, "fr");
    expect(vu["target_language"]).toBe("fr");
  });
});
