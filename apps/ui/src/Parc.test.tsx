/**
 * L'écran Parc monté en entier, sur les trois réponses du serveur — sans serveur.
 *
 * Ce que ces tests gardent tient en une phrase : **l'écran ne conclut jamais à la
 * place du serveur**. Un poste indéterminé reste indéterminé, un plan périmé se
 * dit périmé, une lecture qui échoue n'emporte pas les deux autres, et rien de
 * ce qui est affiché ne déplace un octet. Les chiffres du disque, eux, sont
 * ceux de `packages/api/tests/test_store_endpoint.py` : les deux suites parlent
 * du même parc d'essai.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test } from "vitest";
import { repond, requetes } from "../vitest.setup";
import { App } from "./App";
import { Parc } from "./ecrans/Parc";
import { poserLeDisque, resume, reponsePlan, tiering, figures, plan } from "./__essais__/disque";
import { poserLeParc } from "./__essais__/parc";

beforeEach(() => poserLeDisque());

describe("les trois chiffres", () => {
  test("ils sont ceux de la CLI, en unites de disque", async () => {
    // `ecurie store status` affiche des Go décimaux. Un écran qui dirait « Gio »
    // des mêmes octets ferait douter du chiffre plutôt que de l'unité.
    render(<Parc />);

    const parc = await screen.findByLabelText("Parc");
    expect(await within(parc).findByText("2 ko")).toBeInTheDocument();
    expect(within(parc).getAllByText("1 ko").length).toBeGreaterThan(0);
  });

  test("le poste jamais utilise n_est pas affiche a zero", async () => {
    // Le total récupérable ne le contient pas non plus : ce qui est indéterminé
    // ne s'additionne pas.
    render(<Parc />);

    const poste = await screen.findByText(/variants jamais utilisés/);
    const ligne = poste.closest("li")!;
    expect(ligne).toHaveAttribute("data-connu", "non");
    expect(ligne.textContent).toContain("inconnu");
    expect(ligne.textContent).toContain("aucune exécution notée");
  });

  test("l_arbre de duplication nomme les chemins", async () => {
    // Sans les chemins, il ne reste qu'un nombre sur lequel on ne peut rien
    // décider. La recherche est bornée à l'arbre : les mêmes chemins reviennent
    // dans le détail du plan, qui les propose à la déduplication.
    render(<Parc />);
    await screen.findByText("Trois chiffres");

    const arbre = document.querySelector("ul.ecurie-duplications")!;
    expect(arbre.textContent).toContain("/hub/model.safetensors");
    expect(arbre.textContent).toContain("/ollama/blobs/sha256-aaa");
    expect(arbre.textContent).toContain("1 ko récupérables");
  });

  test("un contenu qui ment sur son hash est signale en rouge", async () => {
    poserLeDisque({
      resume: resume({
        figures: figures({ mismatched: ["/hub/blobs/menteur"] }),
      }),
    });
    render(<Parc />);

    const alerte = await screen.findByText(/ne correspond pas au hash annoncé/);
    expect(alerte.closest(".text-danger")).toBeTruthy();
  });
});

describe("le plan de récupération", () => {
  test("il dit le gain par poste, avec le libelle du serveur", async () => {
    // Le front n'entretient pas sa propre table de traduction : celle de la CLI
    // voyage avec le plan.
    render(<Parc />);

    const total = await screen.findByText(/Total récupérable/);
    expect(total.textContent).toContain("1 ko");
    expect(screen.getAllByRole("rowheader").map((c) => c.textContent)).toContain(
      "duplication inter-gestionnaires",
    );
  });

  test("chaque action se lit, avec ce qu_elle garde et ce qu_elle remplace", async () => {
    render(<Parc />);

    await userEvent.click(await screen.findByText(/action\(s\), une par une/));
    expect(screen.getByText(/lier en dur/)).toBeInTheDocument();
    expect(screen.getByText(/remplace/)).toBeInTheDocument();
  });

  test("ne dedupliquer que sur des hash relus relance la lecture", async () => {
    // C'est la seule décision de l'écran qui change ce que le plan propose :
    // effacer sur la foi d'un nom de blob, ou sur un contenu qu'on a relu.
    repond("/store/plan", (requête) => {
      const relus = new URL(requête.url).searchParams.get("verified_only") === "true";
      return {
        body: relus
          ? reponsePlan({
              plan: plan({
                actions: [],
                by_reason: {},
                total_bytes_reclaimed: 0,
                ignored: [
                  { reason: "hash-annonce-non-verifie", paths: ["/hub/model.safetensors"] },
                ],
              }),
              command: "ecurie store plan --verified-only",
            })
          : reponsePlan(),
      };
    });
    render(<Parc />);
    await screen.findByText(/Total récupérable/);

    await userEvent.click(screen.getByLabelText(/sha256 relus/));

    await waitFor(() => expect(screen.getByText(/Total récupérable/).textContent).toContain("0 o"));
    expect(screen.getByText(/ecurie store plan --verified-only/)).toBeInTheDocument();
  });

  test("la commande qui applique est donnee, jamais un bouton", async () => {
    // Appliquer relit chaque fichier pour prouver son contenu avant d'y toucher,
    // puis déplace en quarantaine. Ce n'est pas un clic dans un onglet resté
    // ouvert depuis la veille.
    render(<Parc />);

    expect(await screen.findByText("ecurie store apply <plan>")).toBeInTheDocument();
    const boutons = screen.getAllByRole("button").map((b) => b.textContent);
    expect(boutons.some((t) => /appliquer|supprimer|effacer/i.test(t ?? ""))).toBe(false);
  });
});

describe("le tiering", () => {
  test("un volume demonte explique les variants indisponibles", async () => {
    poserLeDisque({
      tiering: tiering({
        volumes: [{ path: "/Volumes/Parc", mounted: false, free_bytes: null, total_bytes: null }],
        cold: [
          {
            path: "/parc/sdxl.safetensors",
            target: "/Volumes/Parc/sdxl.safetensors",
            available: false,
            variant_ref: "sdxl-base@fp16",
          },
        ],
      }),
    });
    render(<Parc />);

    expect(await screen.findByText(/démonté/)).toBeInTheDocument();
    expect(screen.getByText("volume absent")).toBeInTheDocument();
  });

  test("un variant qui ne rendrait rien le dit avant qu_on le deporte", async () => {
    // Un lien dur tenu hors du parc retient les octets : le déport copierait
    // des giga-octets sans en libérer un seul.
    poserLeDisque({
      tiering: tiering({
        variants: [
          {
            ref: "sdxl-base@fp16",
            files: 3,
            bytes: 6_900_000_000,
            freed_bytes: 0,
            shared_with: [],
            devices: [1],
            tiered_links: 0,
            tierable: true,
          },
        ],
      }),
    });
    render(<Parc />);

    expect(
      await screen.findByText(/un lien dur hors du parc retient le reste/),
    ).toBeInTheDocument();
    expect(screen.getByText(/ecurie store tier sdxl-base@fp16/)).toBeInTheDocument();
  });

  test("un variant reparti sur deux volumes est ecarte et nomme", async () => {
    poserLeDisque({
      tiering: tiering({
        variants: [
          {
            ref: "eparpille@v1",
            files: 2,
            bytes: 100,
            freed_bytes: 100,
            shared_with: [],
            devices: [1, 2],
            tiered_links: 0,
            tierable: false,
          },
        ],
      }),
    });
    render(<Parc />);

    const alerte = await screen.findByText(/répartis sur plusieurs volumes/);
    expect(alerte.textContent).toContain("eparpille@v1");
  });
});

describe("ce que l'écran dit de lui-même", () => {
  test("sans scan, il ne montre pas des zeros mais la commande a taper", async () => {
    // Des chiffres nuls se liraient « le parc est vide », ce qui décourage
    // précisément la commande qui les remplirait.
    poserLeDisque({
      resume: resume({
        scanned: false,
        figures: null,
        last_scan_at: null,
        telemetry: null,
        hint: "aucun état observé — lancer `ecurie store scan` pour remplir ~/.ecurie/state.db",
      }),
      plan: reponsePlan({ scanned: false, plan: null, command: null, last_scan_at: null }),
      tiering: tiering({ scanned: false, last_scan_at: null }),
    });
    render(<Parc />);

    expect(await screen.findByText(/n'a jamais été regardé/)).toBeInTheDocument();
    expect(screen.getByText(/ecurie store scan/)).toBeInTheDocument();
    expect(screen.queryByText("Trois chiffres")).toBeNull();
  });

  test("des chiffres perimes se disent perimes", async () => {
    // Ces octets ont déjà bougé : planifier dessus proposerait de récupérer ce
    // qui est déjà repris.
    poserLeDisque({
      resume: resume({
        stale: true,
        hint: "un plan a été appliqué depuis le dernier scan (2026-08-22T11:00:00) : ces chiffres décrivent le disque d'avant — relancer `ecurie store scan`",
      }),
    });
    render(<Parc />);

    const avis = await screen.findByText(/décrivent le disque d'avant/);
    expect(avis).toHaveClass("text-danger");
  });

  test("une lecture qui echoue n_emporte pas les deux autres", async () => {
    // Un parc sans volume de tiering déclaré n'a aucune raison de perdre ses
    // trois chiffres.
    poserLeDisque();
    repond("/store/tiering", { status: 503, body: { detail: "base d'état verrouillée" } });
    render(<Parc />);

    expect(await screen.findByText("Trois chiffres")).toBeInTheDocument();
    expect(await screen.findByText(/base d'état verrouillée/)).toBeInTheDocument();
  });

  test("il ne sonde pas : le disque ne bouge qu_apres un scan", async () => {
    // Le bandeau de ressources sonde parce que la mémoire bouge toute seule.
    // Derrière chaque lecture d'ici, il y a une classification de tout le parc
    // par contenu : la répéter en boucle rendrait trois fois le même chiffre.
    render(<Parc />);
    await screen.findByText("Trois chiffres");
    const premier = requetes().filter((r) => new URL(r.url).pathname === "/store/summary").length;

    await new Promise((r) => setTimeout(r, 60));

    expect(requetes().filter((r) => new URL(r.url).pathname === "/store/summary")).toHaveLength(
      premier,
    );
  });

  test("aucun accent grave de Markdown n_arrive jusqu_a l_ecran", async () => {
    // Trouvé sur une capture d'écran, pas par un test : `phraseHorsRegistre`
    // composait « — `ecurie store verify` tranche » par réflexe de Markdown, et
    // le navigateur affichait les accents graves tels quels. Tous les tests
    // cherchaient le texte par sous-chaîne et ne voyaient rien. Les commandes
    // s'écrivent en `<code>`, jamais en balisage de fichier texte.
    render(<Parc />);
    await screen.findByText("Trois chiffres");

    const parc = screen.getByLabelText("Parc");
    expect(parc.textContent).not.toContain("`");
  });

  test("relire redemande les trois lectures", async () => {
    render(<Parc />);
    await screen.findByText("Trois chiffres");

    await userEvent.click(screen.getByRole("button", { name: "Relire" }));

    await waitFor(() => {
      const chemins = requetes().map((r) => new URL(r.url).pathname);
      expect(chemins.filter((c) => c === "/store/summary")).toHaveLength(2);
      expect(chemins.filter((c) => c === "/store/plan")).toHaveLength(2);
      expect(chemins.filter((c) => c === "/store/tiering")).toHaveLength(2);
    });
  });
});

describe("la navigation entre les deux écrans", () => {
  function onglet(nom: string) {
    return within(screen.getByRole("navigation", { name: "Écrans" })).getByRole("button", {
      name: nom,
    });
  }

  test("l_atelier est l_ecran d_ouverture, le parc est a un clic", async () => {
    poserLeParc();
    render(<App />);

    expect(await screen.findByLabelText("Capacité")).toBeInTheDocument();
    expect(screen.queryByText("Trois chiffres")).toBeNull();

    await userEvent.click(onglet("Parc"));

    expect(await screen.findByText("Trois chiffres")).toBeInTheDocument();
    expect(screen.queryByLabelText("Capacité")).toBeNull();
  });

  test("l_ecran qu_on quitte est demonte, pas cache", async () => {
    // Le Parc classe tout le parc par contenu à chaque lecture et l'Atelier
    // sonde la mémoire toutes les deux secondes : les garder tous les deux
    // montés ferait payer en permanence celui qu'on ne regarde pas.
    poserLeParc();
    render(<App />);
    await screen.findByLabelText("Capacité");

    await userEvent.click(onglet("Parc"));
    await screen.findByText("Trois chiffres");
    const résidents = requetes().filter(
      (r) => new URL(r.url).pathname === "/runtime/residents",
    ).length;

    await new Promise((r) => setTimeout(r, 60));

    expect(requetes().filter((r) => new URL(r.url).pathname === "/runtime/residents")).toHaveLength(
      résidents,
    );
  });

  test("l_ecran courant se dit, pour qui ne voit pas le soulignement", async () => {
    poserLeParc();
    render(<App />);

    expect(onglet("Atelier")).toHaveAttribute("aria-current", "page");
    await userEvent.click(onglet("Parc"));
    expect(onglet("Parc")).toHaveAttribute("aria-current", "page");
    expect(onglet("Atelier")).not.toHaveAttribute("aria-current");
  });
});
