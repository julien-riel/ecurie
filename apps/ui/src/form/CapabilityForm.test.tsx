/**
 * Le livrable de la tâche 4.3, éprouvé : « formulaire généré depuis un contrat,
 * zéro formulaire manuel ».
 *
 * Les contrats sont lus sur le disque, tous, à chaque exécution. Aucun n'est
 * nommé dans une liste — un dix-huitième entrerait dans cette suite sans qu'une
 * ligne bouge.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";
import type { Capability, Variant } from "../api/types";
import { commeCapability, contrat, tousLesContrats } from "../schema/__fixtures__/contrats";
import { initialValues } from "../schema/defaults";
import { CapabilityForm } from "./CapabilityForm";

function Banc(props: {
  capability: Capability;
  variant?: Variant | null;
  runtimeOptions?: Record<string, unknown>;
  resident?: boolean;
  depart?: Record<string, unknown>;
  onData?: (d: Record<string, unknown>) => void;
}) {
  const [formData, setFormData] = useState<Record<string, unknown>>(props.depart ?? {});
  return (
    <CapabilityForm
      capability={props.capability}
      variant={props.variant ?? null}
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

describe("le formulaire d'une capacité", () => {
  test("rend un formulaire pour chacun des contrats du depot", () => {
    const contrats = tousLesContrats();
    expect(contrats.length).toBeGreaterThanOrEqual(17);

    for (const brut of contrats) {
      const capability = commeCapability(brut);
      const alerte = vi.spyOn(console, "error").mockImplementation(() => {});
      const { container, unmount } = render(<Banc capability={capability} />);

      const contrôles = container.querySelectorAll("input, select, textarea");
      expect(contrôles.length, `${brut.id} ne rend aucun contrôle`).toBeGreaterThan(0);
      expect(alerte, `${brut.id} a produit une erreur de console`).not.toHaveBeenCalled();

      alerte.mockRestore();
      unmount();
    }
  });

  test("chaque champ scalaire declare recoit un controle", () => {
    // Les tableaux font exception : RJSF les rend vides, avec un bouton d'ajout.
    // C'est le comportement attendu, pas un champ manquant.
    for (const brut of tousLesContrats()) {
      const attendus = Object.entries(brut.input.properties).filter(
        ([, champ]) => champ.type !== "array" && champ["x-ui"] !== "hidden",
      );
      const { container, unmount } = render(<Banc capability={commeCapability(brut)} />);
      for (const [nom] of attendus) {
        expect(
          container.querySelector(`#root_${nom}`),
          `${brut.id}.${nom} n'a pas de contrôle`,
        ).toBeTruthy();
      }
      unmount();
    }
  });

  test("un enum entier reste un entier", async () => {
    // Mesuré sur RJSF 6.8 : le SelectWidget encode par index, si bien que la
    // valeur remonte typée. Un widget qui lirait event.target.value enverrait
    // « 44100 », que le validateur Draft 2020-12 du serveur refuserait.
    const brut = contrat("audio-denoise");
    let vu: Record<string, unknown> = {};
    render(<Banc capability={commeCapability(brut)} onData={(d) => (vu = d)} />);

    const select = document.getElementById("root_sample_rate") as HTMLSelectElement;
    const enums = brut.input.properties["sample_rate"]!.enum as number[];
    await userEvent.selectOptions(select, select.options[enums.indexOf(44100)]!.value);

    expect(vu["sample_rate"]).toBe(44100);
    expect(typeof vu["sample_rate"]).toBe("number");
  });

  test("un enum accentue reste intact", () => {
    const brut = tousLesContrats().find((c) =>
      Object.values(c.input.properties).some((p) =>
        (p.enum ?? []).some((v) => typeof v === "string" && /[éèêàçô]/.test(v)),
      ),
    );
    if (!brut) return; // le registre n'en a plus : rien à prouver
    const { container } = render(<Banc capability={commeCapability(brut)} />);
    const options = [...container.querySelectorAll("option")].map((o) => o.textContent);
    expect(options.some((t) => t && /[éèêàçô]/.test(t))).toBe(true);
  });

  test("un champ sans defaut n_est pas prerempli", () => {
    // image-matting.threshold à 0 binariserait le masque ; les onze seed du parc
    // à 0 figeraient l'aléa. Un défaut inventé est un choix qu'on impose.
    for (const brut of tousLesContrats()) {
      const capability = commeCapability(brut);
      const départ = initialValues(capability, null);
      for (const [nom, champ] of Object.entries(brut.input.properties)) {
        if (champ.default === undefined) {
          expect(nom in départ, `${brut.id}.${nom} a été inventé`).toBe(false);
        } else {
          expect(départ[nom], `${brut.id}.${nom}`).toEqual(champ.default);
        }
      }
    }
  });

  test("le json schema libre est editable", async () => {
    // Sans le champ dédié, RJSF rend un fieldset vide pour tool-use.tools[].
    // parameters — pourtant requis —, et la capacité est cassée en silence.
    const brut = contrat("tool-use");
    let vu: Record<string, unknown> = {};
    render(
      <Banc
        capability={commeCapability(brut)}
        depart={{ tools: [{ name: "chercher", parameters: {} }] }}
        onData={(d) => (vu = d)}
      />,
    );

    const zone = document.getElementById("root_tools_0_parameters") as HTMLTextAreaElement;
    expect(zone).toBeTruthy();
    expect(zone.tagName).toBe("TEXTAREA");

    await userEvent.clear(zone);
    await userEvent.type(zone, '{{"type":"object"}');

    const outils = vu["tools"] as { parameters?: unknown }[];
    expect(outils[0]!.parameters).toEqual({ type: "object" });
  });

  test("un json invalide ne remonte rien et le dit", async () => {
    const brut = contrat("tool-use");
    let vu: Record<string, unknown> = {};
    render(
      <Banc
        capability={commeCapability(brut)}
        depart={{ tools: [{ name: "chercher", parameters: { type: "object" } }] }}
        onData={(d) => (vu = d)}
      />,
    );
    // On tape à la suite d'un objet valide, sans effacer : le contenu devient
    // invalide alors que la valeur retenue, elle, ne doit pas bouger.
    const zone = document.getElementById("root_tools_0_parameters") as HTMLTextAreaElement;
    await userEvent.type(zone, "et voilà");

    // La description du champ `max_tokens` de ce même contrat contient les mots
    // « JSON invalide » : on cible le message du champ, pas la prose du registre.
    expect(screen.getByText(/^JSON invalide :/)).toBeInTheDocument();

    // La valeur retenue est la dernière valide : ni la chaîne brute, ni un objet
    // à moitié tapé, qui ferait clignoter les reproches du contrat à chaque
    // caractère.
    const outils = vu["tools"] as { parameters?: unknown }[];
    expect(outils[0]!.parameters).toEqual({ type: "object" });
  });

  test("un champ fichier attend un chemin et non un televersement", () => {
    // Le FileWidget de RJSF encode le fichier en data-url base64 (mesuré). Le
    // contrat déclare un chemin que le worker ouvre sur le disque.
    const brut = contrat("image-to-mesh");
    render(<Banc capability={commeCapability(brut)} />);
    const champ = document.getElementById("root_image") as HTMLInputElement;
    expect(champ.type).toBe("text");
    expect(screen.getByText(/Chemin sur cette machine/)).toBeInTheDocument();
  });

  test("un champ fichier annonce les types attendus", () => {
    const brut = contrat("document-to-text");
    render(<Banc capability={commeCapability(brut)} />);
    expect(screen.getByText(/application\/pdf, image\/\*/)).toBeInTheDocument();
  });

  test("la description francaise du contrat est rendue sous le champ", () => {
    // Aucun champ du registre ne porte de `title` : l'étiquette reste la clé, et
    // la phrase française est la `description`. Une table de traduction dans le
    // front serait le formulaire écrit à la main que la conception interdit.
    const brut = contrat("text-to-speech");
    render(<Banc capability={commeCapability(brut)} />);
    expect(screen.getByText("Texte à synthétiser.")).toBeInTheDocument();
  });
});

describe("la remontée des avis de compilation", () => {
  test("remonter les avis ne provoque pas de boucle de rendu", () => {
    // Défaut trouvé au premier essai contre un vrai serveur, invisible ici tant
    // qu'aucun test ne passait `onNotices` : remonter un tableau neuf PENDANT le
    // rendu fait se rendre le parent, qui recompile, qui remonte un autre
    // tableau neuf. Le navigateur se fige sans qu'aucune exception ne soit levée.
    let appels = 0;

    function Parent() {
      const [, setAvis] = useState<readonly unknown[]>([]);
      const [formData, setFormData] = useState<Record<string, unknown>>({});
      return (
        <CapabilityForm
          capability={commeCapability(contrat("text-to-speech"))}
          variant={null}
          runtimeOptions={{ voices: ["serena"] }}
          formData={formData}
          onChange={setFormData}
          onNotices={(n) => {
            appels += 1;
            setAvis(n); // un tableau neuf à chaque compilation : le piège exact
          }}
        />
      );
    }

    render(<Parent />);
    expect(appels).toBeLessThanOrEqual(2);
  });

  test("un contrat degrade remonte ses avis une fois", () => {
    const capability = commeCapability(contrat("text-to-speech"));
    const entrée = capability.input as { properties: Record<string, Record<string, unknown>> };
    entrée.properties["voice"]!["x-ui"] = "carrousel";

    const vus: unknown[][] = [];
    render(
      <CapabilityForm
        capability={capability}
        variant={null}
        formData={{}}
        onChange={() => {}}
        onNotices={(n) => vus.push([...n])}
      />,
    );
    expect(vus.length).toBeLessThanOrEqual(2);
    expect(vus[vus.length - 1]).toHaveLength(1);
  });
});
