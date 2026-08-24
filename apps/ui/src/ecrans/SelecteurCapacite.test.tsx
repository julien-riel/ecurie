/**
 * Le sélecteur de capacité, monté seul — ce que l'Atelier ne montre pas.
 *
 * `App.test.tsx` éprouve le geste complet, du clic au formulaire. Ici on
 * éprouve ce qui n'a rien à voir avec le parc : la fermeture au clavier, le
 * retour du focus, et les deux ou trois phrases que le panneau compose.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import capacités from "../api/__fixtures__/capabilities.json";
import modèles from "../api/__fixtures__/models.json";
import type { Capability, Model } from "../api/types";
import { comptesDe, SelecteurCapacite } from "./SelecteurCapacite";

const CAPACITES = (capacités as unknown as { capabilities: Capability[] }).capabilities;
const MODELES = (modèles as unknown as { models: Model[] }).models;

function monter(valeur: string | null = null, onChoisir = vi.fn()) {
  render(
    <SelecteurCapacite
      capacites={CAPACITES}
      models={MODELES}
      valeur={valeur}
      onChoisir={onChoisir}
    />,
  );
  return onChoisir;
}

const déclencheur = () => screen.getByRole("button", { name: /^Capacité/ });

describe("le déclencheur", () => {
  test("sans choix, il invite et annonce la taille du parc", () => {
    monter();
    expect(déclencheur()).toHaveTextContent("Choisir une capacité");
    expect(déclencheur()).toHaveTextContent(`${CAPACITES.length} au registre`);
  });

  test("avec un choix, il porte le titre et les comptes", () => {
    monter("face-detect");
    expect(déclencheur()).toHaveTextContent("Détection de visages");
    expect(déclencheur()).toHaveTextContent("1 modèle · 3 variants");
  });

  test("il dit au clavier qu_il ouvre un panneau, et s_il est ouvert", async () => {
    monter();
    expect(déclencheur()).toHaveAttribute("aria-haspopup", "dialog");
    expect(déclencheur()).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(déclencheur());
    expect(déclencheur()).toHaveAttribute("aria-expanded", "true");
  });
});

describe("le panneau", () => {
  test("Échap le ferme et rend le focus au declencheur", async () => {
    monter();
    await userEvent.click(déclencheur());
    await screen.findByRole("dialog");

    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).toBeNull();
    // Sans ce retour, fermer au clavier laisse le curseur au début du document
    // et il faut retraverser l'écran pour revenir au choix qu'on vient de faire.
    expect(déclencheur()).toHaveFocus();
  });

  test("le clic hors du panneau le ferme, le clic dedans ne le ferme pas", async () => {
    monter();
    await userEvent.click(déclencheur());
    const panneau = await screen.findByRole("dialog");

    await userEvent.click(within(panneau).getByRole("heading", { level: 2 }));
    expect(screen.queryByRole("dialog")).not.toBeNull();

    await userEvent.click(panneau.parentElement!);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("choisir une capacite la remonte et referme", async () => {
    const onChoisir = monter();
    await userEvent.click(déclencheur());
    const panneau = await screen.findByRole("dialog");

    await userEvent.click(
      within(panneau).getAllByRole("button", { name: /Détection de visages/ })[0]!,
    );

    expect(onChoisir).toHaveBeenCalledWith("face-detect");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("la capacite en cours est marquee dans le panneau", async () => {
    monter("face-detect");
    await userEvent.click(déclencheur());
    const panneau = await screen.findByRole("dialog");

    const carte = within(panneau).getAllByRole("button", { name: /Détection de visages/ })[0]!;
    expect(carte).toHaveAttribute("aria-pressed", "true");
  });

  test("une recherche sans resultat propose de revenir en arriere", async () => {
    monter();
    await userEvent.click(déclencheur());
    const panneau = await screen.findByRole("dialog");

    await userEvent.type(within(panneau).getByRole("searchbox"), "zzzz");

    // Une page vide est une invitation à agir, pas un constat.
    expect(within(panneau).getByText(/Retirer un filtre/)).toBeInTheDocument();
    await userEvent.click(within(panneau).getByRole("button", { name: "Tout afficher" }));
    expect(within(panneau).getAllByText("Détection de visages").length).toBeGreaterThan(0);
  });
});

describe("les comptes d'une capacité", () => {
  test("les variants viennent des modeles, pas du contrat", () => {
    // `models` du contrat ne donne que les modèles ; un modèle porte de un à
    // sept variants, et c'est le variant qu'on lance.
    const detect = CAPACITES.find((c) => c.id === "face-detect")!;
    expect(comptesDe(detect, MODELES)).toEqual({ modeles: 1, variants: 3, prets: 3 });
  });

  test("sans le catalogue, on compte au moins les modeles du contrat", () => {
    const detect = CAPACITES.find((c) => c.id === "face-detect")!;
    expect(comptesDe(detect, []).modeles).toBe(detect.models.length);
  });
});
