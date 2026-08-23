/**
 * Le panneau de capture, avec un matériel de papier.
 *
 * jsdom n'a ni caméra, ni micro, ni `MediaRecorder`, ni `AudioContext` : rien de
 * tout cela ne peut être simulé par l'environnement, ce sont des accès au
 * matériel. C'est pourquoi `CapturePanel` prend son `Materiel` en propriété — et
 * c'est ce qui rend vérifiable la seule chose qui compte vraiment ici : que ce
 * qu'on allume finisse par s'éteindre.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { CapturePanel, phraseRefus } from "./CapturePanel";
import type { Materiel } from "./capture";

// jsdom fournit un `<canvas>` sans contexte de dessin : `getContext("2d")` y rend
// `null` faute du paquet natif `canvas`. Le dessin lui-même n'est pas ce qu'on
// éprouve ici — `capture.test.ts` s'en charge sur une toile de papier —, et sans
// ce double, tout le chemin de la photo s'arrête sur une limite de
// l'environnement de test.
beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: vi.fn(),
  } as unknown as CanvasRenderingContext2D);
});

/** Un matériel de papier : des pistes qu'on peut voir s'arrêter, un enregistreur pilotable. */
function materielFactice() {
  const pistes = [{ stop: vi.fn() }];
  const flux = { getTracks: () => pistes } as unknown as MediaStream;
  const enregistreur = {
    mimeType: "audio/webm",
    start: vi.fn(),
    stop: vi.fn(),
    ondataavailable: null as ((e: { data: Blob }) => void) | null,
    onstop: null as (() => void) | null,
    onerror: null as (() => void) | null,
  };
  const materiel: Materiel = {
    flux: vi.fn(async () => flux),
    typeSupporte: () => true,
    enregistreur: vi.fn(() => enregistreur as unknown as MediaRecorder),
    contexteAudio: () =>
      ({
        close: vi.fn(),
        decodeAudioData: async () => ({
          numberOfChannels: 1,
          sampleRate: 16000,
          getChannelData: () => new Float32Array([0, 0.5]),
        }),
      }) as unknown as AudioContext,
    toileVersBlob: async () => new Blob(["png"], { type: "image/png" }),
  };
  return { materiel, pistes, enregistreur, flux };
}

/** L'élément `<video>` de l'aperçu ne mesure rien dans jsdom : on lui donne une taille. */
function poserLaTailleDuFlux(largeur = 640, hauteur = 480) {
  const video = screen.getByLabelText("Aperçu de la caméra");
  Object.defineProperty(video, "videoWidth", { value: largeur, configurable: true });
  Object.defineProperty(video, "videoHeight", { value: hauteur, configurable: true });
}

describe("les modes offerts", () => {
  test("un champ image ne propose que la camera", () => {
    const { materiel } = materielFactice();
    render(<CapturePanel accept={["image/*"]} onCapture={vi.fn()} materiel={materiel} />);

    expect(screen.getByRole("button", { name: "Caméra" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Micro" })).not.toBeInTheDocument();
  });

  test("un champ qui n_accepte aucun media n_affiche rien du tout", () => {
    const { materiel } = materielFactice();
    const { container } = render(
      <CapturePanel accept={["application/pdf"]} onCapture={vi.fn()} materiel={materiel} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  test("rien ne s_allume avant le premier clic", () => {
    // `getUserMedia` fait apparaître une autorisation système : la réclamer
    // parce qu'un champ accepte une image serait insupportable.
    const { materiel } = materielFactice();
    render(<CapturePanel accept={["image/*"]} onCapture={vi.fn()} materiel={materiel} />);

    expect(materiel.flux).not.toHaveBeenCalled();
  });
});

describe("la photo", () => {
  test("le flux arrive jusqu_a l_element, et pas seulement jusqu_au composant", async () => {
    // Ce que ce test garde a été livré cassé et n'a été vu qu'à l'écran : le
    // flux était branché depuis un `queueMicrotask`, qui s'exécute avant que
    // React ait posé le `<video>`. `videoRef.current` valait `null`,
    // l'affectation partait dans le vide sans une erreur, la caméra s'allumait
    // — la diode aussi — et le viseur restait noir. Vérifier que `flux` a été
    // appelé ne suffit donc pas : c'est l'élément qui doit le porter.
    const { materiel, flux } = materielFactice();
    render(<CapturePanel accept={["image/*"]} onCapture={vi.fn()} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));
    const video = (await screen.findByLabelText("Aperçu de la caméra")) as HTMLVideoElement;

    await waitFor(() => expect(video.srcObject).toBe(flux));
  });

  test("refermer detache le flux de l_element", async () => {
    const { materiel } = materielFactice();
    render(<CapturePanel accept={["image/*"]} onCapture={vi.fn()} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));
    const video = (await screen.findByLabelText("Aperçu de la caméra")) as HTMLVideoElement;
    await waitFor(() => expect(video.srcObject).not.toBeNull());

    await userEvent.click(screen.getByRole("button", { name: "Fermer" }));
    expect(video.srcObject).toBeNull();
  });

  test("le clic ouvre la camera, le second bouton la photographie", async () => {
    const { materiel } = materielFactice();
    const onCapture = vi.fn();
    render(<CapturePanel accept={["image/*"]} onCapture={onCapture} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));
    await screen.findByLabelText("Aperçu de la caméra");
    expect(materiel.flux).toHaveBeenCalledWith({ video: true });

    poserLaTailleDuFlux();
    await userEvent.click(screen.getByRole("button", { name: "Prendre la photo" }));

    await waitFor(() => expect(onCapture).toHaveBeenCalledOnce());
    const [fichier, nom] = onCapture.mock.calls[0]!;
    expect((fichier as Blob).type).toBe("image/png");
    expect(nom).toMatch(/^photo-\d{8}-\d{6}\.png$/);
  });

  test("la camera se referme une fois la photo deposee", async () => {
    const { materiel, pistes } = materielFactice();
    render(<CapturePanel accept={["image/*"]} onCapture={vi.fn()} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));
    await screen.findByLabelText("Aperçu de la caméra");
    poserLaTailleDuFlux();
    await userEvent.click(screen.getByRole("button", { name: "Prendre la photo" }));

    await waitFor(() => expect(pistes[0]!.stop).toHaveBeenCalledOnce());
  });

  test("un depot qui echoue garde la camera ouverte", async () => {
    // Refermer obligerait à reprendre la photo qu'on vient de réussir.
    const { materiel, pistes } = materielFactice();
    const onCapture = vi.fn(() => {
      throw new Error("le serveur n'a pas répondu");
    });
    render(<CapturePanel accept={["image/*"]} onCapture={onCapture} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));
    await screen.findByLabelText("Aperçu de la caméra");
    poserLaTailleDuFlux();
    await userEvent.click(screen.getByRole("button", { name: "Prendre la photo" }));

    expect(await screen.findByText(/le serveur n'a pas répondu/)).toBeInTheDocument();
    expect(pistes[0]!.stop).not.toHaveBeenCalled();
  });
});

describe("l'enregistrement sonore", () => {
  test("le son depose est du wav, pas ce que le navigateur a enregistre", async () => {
    // `MediaRecorder` rend de l'opus ou de l'AAC ; l'env `mlx-audio` du parc ne
    // sait lire ni l'un ni l'autre sans ffmpeg.
    const { materiel, enregistreur } = materielFactice();
    const onCapture = vi.fn();
    render(<CapturePanel accept={["audio/*"]} onCapture={onCapture} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Micro" }));
    await screen.findByText(/Micro ouvert/);
    expect(materiel.flux).toHaveBeenCalledWith({ audio: true });

    await userEvent.click(screen.getByRole("button", { name: "Enregistrer" }));
    expect(enregistreur.start).toHaveBeenCalledOnce();

    enregistreur.ondataavailable!({ data: new Blob(["opus"]) });
    await userEvent.click(screen.getByRole("button", { name: "Arrêter" }));
    enregistreur.onstop!();

    await waitFor(() => expect(onCapture).toHaveBeenCalledOnce());
    const [fichier, nom] = onCapture.mock.calls[0]!;
    expect((fichier as Blob).type).toBe("audio/wav");
    expect(nom).toMatch(/^micro-\d{8}-\d{6}\.wav$/);
  });

  test("l_enregistrement en cours se voit", async () => {
    const { materiel, enregistreur } = materielFactice();
    render(<CapturePanel accept={["audio/*"]} onCapture={vi.fn()} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Micro" }));
    await userEvent.click(screen.getByRole("button", { name: "Enregistrer" }));

    expect(screen.getByRole("status")).toHaveTextContent(/Enregistrement — \d+ s/);
    expect(enregistreur.stop).not.toHaveBeenCalled();
  });
});

describe("la fermeture", () => {
  test("changer de mode referme le precedent", async () => {
    const { materiel, pistes } = materielFactice();
    render(<CapturePanel accept={["image/*", "audio/*"]} onCapture={vi.fn()} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));
    await screen.findByLabelText("Aperçu de la caméra");
    await userEvent.click(screen.getByRole("button", { name: "Micro" }));

    await waitFor(() => expect(pistes[0]!.stop).toHaveBeenCalled());
  });

  test("rouvrir le meme mode le referme", async () => {
    const { materiel, pistes } = materielFactice();
    render(<CapturePanel accept={["image/*"]} onCapture={vi.fn()} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));
    await screen.findByLabelText("Aperçu de la caméra");
    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));

    expect(screen.queryByLabelText("Aperçu de la caméra")).not.toBeInTheDocument();
    expect(pistes[0]!.stop).toHaveBeenCalledOnce();
  });

  test("demonter l_ecran eteint la camera", async () => {
    // React ne coupe rien tout seul : sans l'effet de nettoyage, la diode reste
    // allumée sur un écran qui n'affiche plus l'aperçu.
    const { materiel, pistes } = materielFactice();
    const vue = render(
      <CapturePanel accept={["image/*"]} onCapture={vi.fn()} materiel={materiel} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));
    await screen.findByLabelText("Aperçu de la caméra");
    vue.unmount();

    expect(pistes[0]!.stop).toHaveBeenCalledOnce();
  });
});

describe("les refus du matériel", () => {
  test("un refus d_autorisation dit ou le lever", async () => {
    const { materiel } = materielFactice();
    materiel.flux = vi.fn(async () => {
      throw Object.assign(new Error("Permission denied"), { name: "NotAllowedError" });
    });
    render(<CapturePanel accept={["image/*"]} onCapture={vi.fn()} materiel={materiel} />);

    await userEvent.click(screen.getByRole("button", { name: "Caméra" }));

    expect(await screen.findByText(/réglages du navigateur/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Aperçu de la caméra")).not.toBeInTheDocument();
  });

  test("chaque refus a sa phrase, et elles ne se confondent pas", () => {
    const nommer = (name: string) => Object.assign(new Error("x"), { name });

    expect(phraseRefus(nommer("NotAllowedError"), "photo")).toContain("refusé");
    expect(phraseRefus(nommer("NotFoundError"), "audio")).toContain("Aucun périphérique");
    expect(phraseRefus(nommer("NotReadableError"), "audio")).toContain("autre application");
    expect(phraseRefus(nommer("QuelqueChoseDInconnu"), "video")).toContain("Impossible d'ouvrir");
  });
});
