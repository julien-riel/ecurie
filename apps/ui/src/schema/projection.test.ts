/** La projection du formulaire sur le contrat, et ce qu'elle écarte. */

import { describe, expect, test } from "vitest";
import { commeCapability, contrat } from "./__fixtures__/contrats";
import { toContractInput } from "./projection";

const tts = commeCapability(contrat("text-to-speech"));
const toolUse = commeCapability(contrat("tool-use"));

describe("la projection sur le contrat", () => {
  test("les cles hors contrat sont ecartees", () => {
    // Le serveur lève sur un paramètre hors contrat **même en mode non strict** :
    // « ce n'est pas une saisie incomplète, c'est un client qui parle d'autre
    // chose que la capacité qu'il a nommée ». Une clé de travail oubliée dans
    // formData donnerait un 422 à chaque frappe.
    const projeté = toContractInput(
      { text: "bonjour", ref: "qwen3-tts-1.7b@8bit-mlx", variantId: "8bit-mlx", _dirty: true },
      tts,
    );
    expect(projeté).toEqual({ text: "bonjour" });
  });

  test("les valeurs vides ne partent pas", () => {
    // RJSF émet "" pour un champ texte qu'on a vidé. L'envoyer échouerait sur le
    // minLength: 1 du contrat, alors que l'utilisateur n'a rien saisi du tout.
    expect(toContractInput({ text: "", voice: undefined, speed: null }, tts)).toEqual({});
  });

  test("zero et faux sont des choix et partent", () => {
    // temperature: 0 est le défaut de tool-use, et il est motivé : « un appel
    // d'outil est une sortie structurée ». Le jeter enverrait 0.7 à sa place.
    const projeté = toContractInput({ temperature: 0, parallel_calls: false }, toolUse);
    expect(projeté).toEqual({ temperature: 0, parallel_calls: false });
  });

  test("les structures declarees passent intactes", () => {
    const outils = [{ name: "chercher", parameters: { type: "object" } }];
    expect(toContractInput({ task: "trouve", tools: outils }, toolUse)).toEqual({
      task: "trouve",
      tools: outils,
    });
  });

  test("une saisie absente donne une entree vide", () => {
    expect(toContractInput(undefined, tts)).toEqual({});
    expect(toContractInput({}, tts)).toEqual({});
  });

  test("le seed reste dans l_entree et nulle part ailleurs", () => {
    // Le champ racine `seed` d'AdmissionRequest est écrit par le serveur APRÈS
    // la résolution de la saisie : le passer en double ferait partir une valeur
    // que l'utilisateur n'a pas choisie. Le seed est un paramètre du contrat
    // comme un autre.
    const projeté = toContractInput({ text: "bonjour", seed: 7 }, tts);
    expect(projeté).toEqual({ text: "bonjour", seed: 7 });
    expect(Object.keys(projeté)).not.toContain("ref");
  });
});
