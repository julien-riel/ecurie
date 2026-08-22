/**
 * Le champ fichier, et ses trois façons d'obtenir un chemin.
 *
 * Ce qu'il faut prouver tient en une phrase : quelle que soit la source, **ce
 * qui finit dans le champ est le chemin rendu par le serveur**, jamais le nom du
 * fichier, jamais une data-URL. Le `FileWidget` natif de RJSF encode en base64
 * dans `formData` — c'est précisément ce que ce widget existe pour ne pas faire,
 * et ce qu'un job refuserait plusieurs secondes après le clic.
 */

import type { WidgetProps } from "@rjsf/utils";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { repond, requetes } from "../../../vitest.setup";
import type { Materiel } from "../../media/capture";
import { FilePathWidget } from "./FilePathWidget";

/** Les propriétés que RJSF passe à un widget, réduites à ce que celui-ci lit. */
function props(surchages: Record<string, unknown> = {}): WidgetProps {
  return {
    id: "root_image",
    value: undefined,
    required: true,
    disabled: false,
    readonly: false,
    onChange: vi.fn(),
    onBlur: vi.fn(),
    onFocus: vi.fn(),
    options: { accept: ["image/*"] },
    ...surchages,
  } as unknown as WidgetProps;
}

/** Un matériel de papier : voir `CapturePanel.test.tsx` pour le pourquoi. */
function materielFactice(): Materiel {
  return {
    flux: async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream,
    typeSupporte: () => true,
    enregistreur: () => ({}) as MediaRecorder,
    contexteAudio: () => ({}) as AudioContext,
    toileVersBlob: async () => new Blob(["png"], { type: "image/png" }),
  };
}

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: vi.fn(),
  } as unknown as CanvasRenderingContext2D);
});

describe("le chemin saisi", () => {
  test("part tel quel, sans dépôt", () => {
    // La voie la plus rapide quand on a le chemin sous la main, et la seule qui
    // ne copie aucun octet.
    const onChange = vi.fn();
    render(<FilePathWidget {...props({ onChange })} />);

    const champ = screen.getByRole("textbox");
    expect(champ).toHaveAttribute("placeholder", "/chemin/vers/le/fichier");
    expect(requetes()).toHaveLength(0);
  });

  test("les types attendus et les trois sources sont annonces", () => {
    render(<FilePathWidget {...props({ options: { accept: ["application/pdf", "image/*"] } })} />);
    const phrase = screen.getByText(/Chemin sur cette machine/);
    expect(phrase).toHaveTextContent("Types attendus : application/pdf, image/*");
    expect(phrase).toHaveTextContent("glissé ici");
  });
});

describe("le fichier choisi sur le disque", () => {
  test("le chemin rendu par le serveur entre dans le champ", async () => {
    // Et non le nom, ni une data-URL : le worker ouvrira ce chemin.
    repond("/uploads", {
      status: 201,
      body: {
        path: "/Users/x/.ecurie/uploads/20260822-120000-abcdef-objet.png",
        name: "20260822-120000-abcdef-objet.png",
        media_type: "image/png",
        size_bytes: 4096,
      },
    });
    const onChange = vi.fn();
    render(<FilePathWidget {...props({ onChange })} />);

    await userEvent.upload(
      screen.getByLabelText("Choisir un fichier sur cette machine"),
      new File(["\x89PNG"], "objet.png", { type: "image/png" }),
    );

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith(
        "/Users/x/.ecurie/uploads/20260822-120000-abcdef-objet.png",
      ),
    );
  });

  test("le depot part en multipart, sans content-type ecrit a la main", async () => {
    // Le format exige une frontière que seul le navigateur peut engendrer : en
    // poser une à la main donne un en-tête que Starlette refuse d'analyser.
    repond("/uploads", { status: 201, body: { path: "/tmp/a.png", name: "a.png", media_type: "image/png", size_bytes: 3 } });
    render(<FilePathWidget {...props()} />);

    await userEvent.upload(
      screen.getByLabelText("Choisir un fichier sur cette machine"),
      new File(["abc"], "a.png", { type: "image/png" }),
    );

    await waitFor(() => expect(requetes()).toHaveLength(1));
    const requête = requetes()[0]!;
    expect(requête.method).toBe("POST");
    expect(new URL(requête.url).pathname).toBe("/uploads");
    expect(requête.headers.get("content-type")).toMatch(/^multipart\/form-data; boundary=/);
  });

  test("ce qui a ete depose est dit, avec sa taille", async () => {
    repond("/uploads", {
      status: 201,
      body: { path: "/tmp/a.png", name: "20260822-a.png", media_type: "image/png", size_bytes: 2048 },
    });
    render(<FilePathWidget {...props()} />);

    await userEvent.upload(
      screen.getByLabelText("Choisir un fichier sur cette machine"),
      new File(["ab"], "a.png", { type: "image/png" }),
    );

    expect(await screen.findByText(/Déposé : 20260822-a\.png/)).toBeInTheDocument();
  });

  test("un refus du serveur est montre, et le champ n_est pas vide", async () => {
    // Ce qui était dans le champ y était pour une raison ; une panne n'en est
    // pas une de la perdre.
    repond("/uploads", {
      status: 413,
      body: { detail: "dépôt interrompu au-delà de 1073741824 octets" },
    });
    const onChange = vi.fn();
    render(<FilePathWidget {...props({ value: "/deja/la.png", onChange })} />);

    await userEvent.upload(
      screen.getByLabelText("Choisir un fichier sur cette machine"),
      new File(["\x89PNG"], "enorme.png", { type: "image/png" }),
    );

    expect(await screen.findByText(/dépôt interrompu/)).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toHaveValue("/deja/la.png");
  });

  test("le selecteur natif n_est plus inerte", () => {
    // Il l'était jusqu'au dépôt : le navigateur ne donne pas le chemin réel d'un
    // fichier choisi, et le widget se contentait de l'annoncer.
    render(<FilePathWidget {...props()} />);
    const sélecteur = screen.getByLabelText("Choisir un fichier sur cette machine");

    expect(sélecteur).not.toBeDisabled();
    expect(sélecteur).toHaveAttribute("accept", "image/*");
  });
});

describe("le glisser-déposer et le collage", () => {
  const RÉPONSE = {
    status: 201,
    body: { path: "/tmp/glisse.png", name: "glisse.png", media_type: "image/png", size_bytes: 4 },
  };

  function fichier() {
    return new File(["\x89PNG"], "depuis-le-web.png", { type: "image/png" });
  }

  test("une image lachee sur le champ devient un chemin", async () => {
    // Le geste le plus direct pour « prendre une image dans une page web » : le
    // navigateur télécharge lui-même l'image glissée depuis un onglet et la
    // présente comme un fichier.
    repond("/uploads", RÉPONSE);
    const onChange = vi.fn();
    const { container } = render(<FilePathWidget {...props({ onChange })} />);

    const zone = container.querySelector(".ecurie-fichier")!;
    fireEvent.drop(zone, { dataTransfer: { files: [fichier()] } });

    await waitFor(() => expect(onChange).toHaveBeenCalledWith("/tmp/glisse.png"));
  });

  test("le survol se voit, et cesse quand on sort", () => {
    // Sans repère visuel, un fichier lâché au mauvais endroit ouvre un onglet et
    // fait disparaître le formulaire.
    const { container } = render(<FilePathWidget {...props()} />);
    const zone = container.querySelector(".ecurie-fichier")!;

    fireEvent.dragOver(zone, { dataTransfer: { files: [] } });
    expect(zone).toHaveAttribute("data-survol", "oui");

    fireEvent.dragLeave(zone);
    expect(zone).not.toHaveAttribute("data-survol");
  });

  test("un collage depose le fichier du presse-papiers", async () => {
    repond("/uploads", RÉPONSE);
    const onChange = vi.fn();
    const { container } = render(<FilePathWidget {...props({ onChange })} />);

    fireEvent.paste(container.querySelector(".ecurie-fichier")!, {
      clipboardData: { files: [fichier()] },
    });

    await waitFor(() => expect(onChange).toHaveBeenCalledWith("/tmp/glisse.png"));
  });

  test("un collage de texte ne declenche aucun depot", async () => {
    // Coller un chemin dans le champ doit rester un collage de texte.
    const onChange = vi.fn();
    const { container } = render(<FilePathWidget {...props({ onChange })} />);

    fireEvent.paste(container.querySelector(".ecurie-fichier")!, {
      clipboardData: { files: [] },
    });

    expect(requetes()).toHaveLength(0);
  });

  test("un champ en lecture seule n_accepte rien", () => {
    const { container } = render(<FilePathWidget {...props({ readonly: true })} />);
    const zone = container.querySelector(".ecurie-fichier")!;

    fireEvent.drop(zone, { dataTransfer: { files: [fichier()] } });

    expect(requetes()).toHaveLength(0);
  });
});

describe("la capture", () => {
  test("une photo prise a la camera devient un chemin", async () => {
    repond("/uploads", {
      status: 201,
      body: { path: "/tmp/photo.png", name: "photo.png", media_type: "image/png", size_bytes: 3 },
    });
    const onChange = vi.fn();
    render(<FilePathWidget {...props({ onChange, options: { accept: ["image/*"], materiel: materielFactice() } })} />);

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));
    const aperçu = await screen.findByLabelText("Aperçu de la caméra");
    Object.defineProperty(aperçu, "videoWidth", { value: 640, configurable: true });
    Object.defineProperty(aperçu, "videoHeight", { value: 480, configurable: true });

    await userEvent.click(screen.getByRole("button", { name: "Prendre la photo" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith("/tmp/photo.png"));
  });

  test("un champ qui n_accepte pas de media n_offre aucune capture", () => {
    render(<FilePathWidget {...props({ options: { accept: ["application/pdf"] } })} />);
    expect(screen.queryByRole("button", { name: "Caméra" })).not.toBeInTheDocument();
    // Le champ texte et le sélecteur, eux, restent : un PDF se choisit sur le disque.
    expect(screen.getByLabelText("Choisir un fichier sur cette machine")).toBeInTheDocument();
  });
});
