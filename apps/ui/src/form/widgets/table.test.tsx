/** La table des widgets : ses cinq entrées, et ce que chacune nomme vraiment. */

import Form from "@rjsf/core";
import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { X_UI_CONNUS } from "../../schema/contract";
import { validator } from "../validator";
import { WIDGETS, X_UI_WIDGETS } from "./index";

describe("la table des widgets", () => {
  test("les cinq valeurs du meta_schema ont une entree", () => {
    expect(Object.keys(X_UI_WIDGETS).sort()).toEqual([...X_UI_CONNUS].sort());
  });

  test("chaque widget nomme existe vraiment", () => {
    // Le type `Record<XUi, WidgetEntry>` garantit la PRÉSENCE d'une entrée,
    // jamais la justesse du nom qu'elle porte : « textarea » mal orthographié
    // compilerait, et RJSF rendrait un champ texte sans que rien n'échoue. On
    // monte donc chaque widget sur un schéma qui lui convient, et on vérifie
    // qu'il produit le contrôle attendu.
    const cas: Record<string, { schema: Record<string, unknown>; attendu: (e: Element) => boolean }> =
      {
        textarea: {
          schema: { type: "string" },
          attendu: (e) => e.tagName === "TEXTAREA",
        },
        select: {
          schema: { type: "string" },
          attendu: (e) => e.tagName === "INPUT" && e.getAttribute("list") !== null,
        },
        file: {
          schema: { type: "string", contentMediaType: "image/png" },
          attendu: (e) => e.tagName === "INPUT" && (e as HTMLInputElement).type === "text",
        },
        slider: {
          schema: { type: "integer", minimum: 0, maximum: 10 },
          attendu: (e) => (e as HTMLInputElement).type === "range",
        },
        hidden: {
          schema: { type: "string" },
          attendu: (e) => (e as HTMLInputElement).type === "hidden",
        },
      };

    for (const xui of X_UI_CONNUS) {
      const { schema, attendu } = cas[xui]!;
      const { unmount } = render(
        <Form
          schema={
            {
              type: "object",
              required: ["champ"],
              additionalProperties: false,
              properties: { champ: schema },
            } as never
          }
          validator={validator}
          widgets={WIDGETS}
          uiSchema={{ champ: { "ui:widget": X_UI_WIDGETS[xui].widget } }}
        />,
      );
      const élément = document.getElementById("root_champ");
      expect(élément, `${xui} : aucun contrôle rendu — le widget « ${X_UI_WIDGETS[xui].widget} » n'existe pas`).toBeTruthy();
      expect(attendu(élément!), `${xui} : contrôle inattendu ${élément!.tagName}`).toBe(true);
      unmount();
    }
  });

  test("le curseur derive son pas de multipleOf", () => {
    // C'est la raison de préférer le RangeWidget natif à un curseur écrit à la
    // main : `text-to-image.width` a un pas de 64, `text-to-video` de 16.
    render(
      <Form
        schema={
          {
            type: "object",
            required: ["largeur"],
            additionalProperties: false,
            properties: {
              largeur: { type: "integer", minimum: 256, maximum: 2048, multipleOf: 64 },
            },
          } as never
        }
        validator={validator}
        widgets={WIDGETS}
        uiSchema={{ largeur: { "ui:widget": X_UI_WIDGETS.slider.widget } }}
      />,
    );
    const curseur = document.getElementById("root_largeur") as HTMLInputElement;
    expect(curseur.step).toBe("64");
    expect(curseur.min).toBe("256");
    expect(curseur.max).toBe("2048");
  });

  test("un champ cache garde sa valeur dans l_entree", () => {
    // Le méta-schéma le décrit comme « widget imposé », pas comme un paramètre
    // supprimé : la valeur doit partir avec le job.
    let vu: unknown = null;
    render(
      <Form
        schema={
          {
            type: "object",
            required: ["jeton"],
            additionalProperties: false,
            properties: { jeton: { type: "string", default: "caché" } },
          } as never
        }
        validator={validator}
        widgets={WIDGETS}
        uiSchema={{ jeton: { "ui:widget": X_UI_WIDGETS.hidden.widget } }}
        onChange={(e) => (vu = e.formData)}
      />,
    );
    expect((vu as Record<string, unknown>)["jeton"]).toBe("caché");
  });
});
