/**
 * Le flux de texte : ce qu'on montre pendant qu'un modèle écrit.
 *
 * Ce qui se vérifie ici tient à la séparation des deux canaux et au moment où le
 * raisonnement s'ouvre — le reste (le défilement, la mise en forme) est du CSS.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FluxTexte } from "./FluxTexte";
import type { Job } from "../api/types";

function jobAvec(flux: Partial<Job>): Job {
  return {
    id: "20260823-120000-abcdef",
    ref: "gemma4-12b-texte@4bit",
    model: "gemma4-12b-texte",
    variant: "4bit",
    capability: "text-generation",
    state: "running",
    submitted_at: "2026-08-23T12:00:00Z",
    started_at: null,
    finished_at: null,
    progress: 40,
    note: "",
    error: null,
    reused: false,
    evicted: [],
    warnings: [],
    metrics: {},
    output: {},
    outputs: {},
    files: {},
    input: {},
    seed: null,
    stream_text: "",
    stream_reasoning: "",
    cancelled: false,
    ...flux,
  } as Job;
}

describe("le flux de texte", () => {
  it("n'affiche rien tant qu'aucun fragment n'est arrivé", () => {
    const { container } = render(<FluxTexte job={jobAvec({})} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("montre la réponse au fur et à mesure", () => {
    render(<FluxTexte job={jobAvec({ stream_text: "Le murmure des vagues" })} />);
    expect(screen.getByText("Le murmure des vagues")).toBeInTheDocument();
  });

  it("sépare le raisonnement de la réponse", () => {
    render(
      <FluxTexte job={jobAvec({ stream_reasoning: "je pèse le pour", stream_text: "La réponse." })} />,
    );
    expect(screen.getByText("je pèse le pour")).toBeInTheDocument();
    expect(screen.getByText("La réponse.")).toBeInTheDocument();
  });

  it("ouvre le raisonnement tant que la réponse n'a pas commencé", () => {
    // Sinon l'écran resterait vide sous une barre de progression pendant qu'un
    // modèle réfléchit — l'attente que le flux existe pour supprimer.
    render(<FluxTexte job={jobAvec({ stream_reasoning: "je réfléchis" })} />);
    expect(screen.getByRole("group")).toHaveAttribute("open");
  });

  it("replie le raisonnement dès que la réponse arrive", () => {
    render(
      <FluxTexte job={jobAvec({ stream_reasoning: "je réfléchis", stream_text: "Voici." })} />,
    );
    expect(screen.getByRole("group")).not.toHaveAttribute("open");
  });

  it("compte les caractères du brouillon", () => {
    render(<FluxTexte job={jobAvec({ stream_reasoning: "douze!" })} />);
    expect(screen.getByText(/6 caractères/)).toBeInTheDocument();
  });
});
