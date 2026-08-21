/** Le bandeau d'avis : visible, jamais bloquant. */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test } from "vitest";
import capacités from "../api/__fixtures__/capabilities.json";
import type { Issue } from "../api/types";
import { NoticeBanner } from "./NoticeBanner";
import { avisChamp, fromIssues, uniques } from "./notices";

const ISSUES = (capacités as unknown as { issues: Issue[] }).issues;

describe("les constats du registre", () => {
  test("les issues du registre reel deviennent des avis", () => {
    // Le parc porte aujourd'hui deux avertissements sur trellis2 : révision non
    // épinglée et runtime custom sans entrypoint.
    const avis = fromIssues(ISSUES);
    expect(avis.length).toBe(ISSUES.length);
    for (const a of avis) {
      expect(a.source).toBeTruthy();
      expect(a.message).toBeTruthy();
    }
  });

  test("un registre en erreur n_empeche rien", async () => {
    // Le serveur répond MALGRÉ un registre en erreur : les modèles valides sont
    // servis, `issues` transporte le reste. Une UI qui masquerait tout parce
    // qu'un manifeste sur six est invalide n'aurait aucun moyen d'apprendre
    // lequel.
    const erreur: Issue[] = [
      { severity: "error", file: "registry/models/x.yaml", message: "manifeste invalide" },
    ];
    render(<NoticeBanner notices={fromIssues(erreur)} />);

    const bouton = screen.getByRole("button");
    expect(bouton).toHaveTextContent("1 erreur(s) de registre");

    await userEvent.click(bouton);
    expect(screen.getByText(/manifeste invalide/)).toBeInTheDocument();
    expect(screen.getByText("registry/models/x.yaml")).toBeInTheDocument();
  });

  test("le bandeau est replie par defaut", () => {
    render(<NoticeBanner notices={fromIssues(ISSUES)} />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  test("sans avis rien ne s_affiche", () => {
    const { container } = render(<NoticeBanner notices={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  test("les erreurs passent avant les avertissements", async () => {
    const mélange = fromIssues([
      { severity: "warning", file: "b.yaml", message: "b" },
      { severity: "error", file: "a.yaml", message: "a" },
    ]);
    render(<NoticeBanner notices={mélange} />);
    await userEvent.click(screen.getByRole("button"));
    const lignes = screen.getAllByRole("listitem");
    expect(lignes[0]).toHaveAttribute("data-severite", "error");
  });

  test("le meme avis n_est compte qu_une fois", () => {
    // Compiler dix-sept contrats répète le même reproche autant de fois.
    const répété = [
      avisChamp("a", "champ", "x-ui inconnu"),
      avisChamp("a", "champ", "x-ui inconnu"),
      avisChamp("b", "champ", "x-ui inconnu"),
    ];
    expect(uniques(répété)).toHaveLength(2);
  });

  test("un avis de champ nomme son contrat et son champ", () => {
    const avis = avisChamp("text-to-speech", "voice", "x-ui « carrousel » inconnu");
    expect(avis.source).toBe("text-to-speech.voice");
    expect(avis.severite).toBe("info");
  });
});
