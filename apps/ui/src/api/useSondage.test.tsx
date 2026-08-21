/**
 * Le sondage, éprouvé sur ses trois promesses.
 *
 * Elles ne se voient pas en lisant l'appel — c'est justement pourquoi elles ont
 * chacune un test : les requêtes ne s'empilent pas, un échec n'efface pas les
 * derniers chiffres, et un onglet caché ne sonde pas.
 *
 * Les minuteries sont réelles, avec une période de quelques millisecondes. Les
 * fausses minuteries de vitest obligeraient à avancer l'horloge à l'intérieur
 * d'un `act`, tour par tour, pour un hook dont tout l'intérêt est de replanifier
 * lui-même : le test dirait alors quand chaque tour part, ce qui est exactement
 * la décision qu'on veut vérifier.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { useSondage } from "./useSondage";

function Sonde({
  charger,
  periode = 5,
}: {
  charger: (signal: AbortSignal) => Promise<string>;
  periode?: number;
}) {
  const s = useSondage(charger, [], periode);
  return (
    <output>
      <span data-testid="valeur">{s.données ?? "—"}</span>
      <span data-testid="erreur">{s.erreur ? "échec" : "—"}</span>
      <span data-testid="actif">{s.actif ? "actif" : "dort"}</span>
      <span data-testid="vu">{s.vu === null ? "jamais" : "vu"}</span>
    </output>
  );
}

/** Rend l'onglet caché ou visible, et prévient le document comme le ferait le navigateur. */
function visibilite(état: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { value: état, configurable: true });
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

describe("le sondage", () => {
  test("il repete l_appel a la cadence demandee", async () => {
    let tours = 0;
    render(<Sonde charger={async () => `tour ${++tours}`} />);

    await screen.findByText("tour 1");
    await waitFor(() => expect(tours).toBeGreaterThan(3));
  });

  test("il attend la reponse avant de replanifier", async () => {
    // Un `setInterval` de deux secondes sur un serveur qui met trois secondes à
    // répondre empile les requêtes jusqu'à saturation. Ici, un seul appel peut
    // être en vol à la fois, quelle que soit la lenteur du serveur.
    let enVol = 0;
    let maximum = 0;
    let terminés = 0;

    render(
      <Sonde
        periode={1}
        charger={async () => {
          enVol += 1;
          maximum = Math.max(maximum, enVol);
          await new Promise((r) => setTimeout(r, 15));
          enVol -= 1;
          terminés += 1;
          return `tour ${terminés}`;
        }}
      />,
    );

    await waitFor(() => expect(terminés).toBeGreaterThan(2), { timeout: 2000 });
    expect(maximum).toBe(1);
  });

  test("un echec garde les derniers chiffres et leur date", async () => {
    // Le serveur qu'on redémarre pendant une saisie viderait le bandeau, et
    // l'utilisateur perdrait ce qu'il regardait. Les chiffres restent, datés.
    let tours = 0;
    render(
      <Sonde
        charger={async () => {
          tours += 1;
          if (tours > 1) throw new Error("connexion refusée");
          return "17,76 Gio";
        }}
      />,
    );

    await screen.findByText("17,76 Gio");
    await waitFor(() => expect(screen.getByTestId("erreur")).toHaveTextContent("échec"));
    expect(screen.getByTestId("valeur")).toHaveTextContent("17,76 Gio");
    expect(screen.getByTestId("vu")).toHaveTextContent("vu");
  });

  test("une reprise apres echec efface l_erreur", async () => {
    let tours = 0;
    render(
      <Sonde
        charger={async () => {
          tours += 1;
          if (tours === 2) throw new Error("connexion refusée");
          return `tour ${tours}`;
        }}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("erreur")).toHaveTextContent("échec"));
    await waitFor(() => expect(screen.getByTestId("erreur")).toHaveTextContent("—"));
  });

  test("un onglet cache ne sonde pas, et repart au retour", async () => {
    // À deux secondes, un onglet laissé ouvert une journée fait quarante mille
    // requêtes, dont chacune vérifie l'existence de processus.
    let tours = 0;
    render(<Sonde charger={async () => `tour ${++tours}`} />);
    await waitFor(() => expect(tours).toBeGreaterThan(1));

    visibilite("hidden");
    await waitFor(() => expect(screen.getByTestId("actif")).toHaveTextContent("dort"));
    const figé = tours;
    await new Promise((r) => setTimeout(r, 40));
    expect(tours).toBe(figé);

    visibilite("visible");
    await waitFor(() => expect(tours).toBeGreaterThan(figé));
  });

  test("le demontage arrete tout", async () => {
    let tours = 0;
    const { unmount } = render(<Sonde charger={async () => `tour ${++tours}`} />);
    await waitFor(() => expect(tours).toBeGreaterThan(1));

    unmount();
    const figé = tours;
    await new Promise((r) => setTimeout(r, 40));
    expect(tours).toBe(figé);
  });
});
