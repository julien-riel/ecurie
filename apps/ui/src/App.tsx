/**
 * La coquille — un titre, une navigation, et l'écran du moment.
 *
 * Elle ne fait presque rien, et c'est voulu : l'architecture pose un **plafond
 * ferme de quatre écrans** — Atelier (4.4), Parc (4.5), Confrontation (5.4),
 * Bibliothèque (5.5) —, et le risque nommé du projet est « dérive vers un clone
 * de ComfyUI ». Une coquille qui gagnerait des barres d'outils, un thème et un
 * état global serait la première marche de cette dérive.
 *
 * Elle porte **un** état, depuis le Parc : l'écran affiché. Elle n'en portera
 * pas d'autre. Le bandeau de ressources reste rendu par l'Atelier, bien que la
 * conception l'appelle « composant global » : le chiffre qui compte est celui du
 * variant sélectionné, et le hisser ici obligerait à y faire remonter la
 * sélection de l'Atelier. Il est global par sa nature — n'importe quel écran
 * peut le poser — et pas par sa place dans l'arbre. Le Parc, lui, ne le pose
 * pas : il parle de disque, pas de mémoire unifiée.
 *
 * **Ni routeur, ni URL.** Deux écrans, un `useState`, aucune dépendance ajoutée.
 * Ce que cela coûte est réel et se dit : rechargez la page et vous revenez à
 * l'Atelier, et un écran ne se met pas en signet. Ce que cela évite l'est aussi
 * — une bibliothèque de routage et son histoire de navigation pour arbitrer
 * entre deux boutons. La question se reposera au quatrième écran, quand un lien
 * vers un job précis de la Bibliothèque commencera à valoir quelque chose.
 *
 * Ce sont des **boutons**, ni des liens ni des `tab` ARIA. Pas des liens, faute
 * d'URL : en annoncer une à un lecteur d'écran promettrait une adresse qu'on ne
 * peut pas donner. Pas des onglets non plus, bien que la forme y ressemble — le
 * motif `tablist` engage à une navigation au clavier par les flèches avec un
 * `tabindex` mobile, et le déclarer sans la tenir vaut moins que de ne pas le
 * déclarer. Deux boutons et `aria-current` disent exactement ce qui est vrai.
 */

import { useState } from "react";
import { Atelier } from "./ecrans/Atelier";
import { Parc } from "./ecrans/Parc";

const ECRANS = [
  {
    id: "atelier",
    titre: "Atelier",
    sous_titre: "le formulaire vient du contrat de capacité, le coût vient du serveur.",
  },
  {
    id: "parc",
    titre: "Parc",
    sous_titre: "ce que le disque contient, et ce qu'on pourrait en reprendre.",
  },
] as const;

type EcranId = (typeof ECRANS)[number]["id"];

export function App() {
  const [écran, setEcran] = useState<EcranId>("atelier");
  const courant = ECRANS.find((e) => e.id === écran) ?? ECRANS[0];

  return (
    <div className="ecurie">
      {/*
        L'en-tête tient sur une ligne : marque, onglets, et la phrase de l'écran
        du moment. Cette phrase ne répète plus le nom de l'onglet qui la précède
        de trois centimètres — un libellé fait un seul travail, et c'est l'onglet
        appuyé qui dit où l'on est.
      */}
      <header className="ecurie-entete">
        <h1 className="ecurie-marque">Écurie</h1>
        <nav className="ecurie-onglets" aria-label="Écrans">
          {ECRANS.map((e) => (
            <button
              key={e.id}
              type="button"
              aria-current={e.id === écran ? "page" : undefined}
              onClick={() => setEcran(e.id)}
            >
              {e.titre}
            </button>
          ))}
        </nav>
        <p className="ecurie-sujet">{courant.sous_titre}</p>
      </header>
      {/*
        L'écran non affiché est **démonté**, pas caché. Le Parc classe tout le
        parc par contenu à chaque lecture et l'Atelier sonde la mémoire toutes
        les deux secondes : les garder tous les deux montés ferait payer en
        permanence le coût de celui qu'on ne regarde pas.
      */}
      <main>{écran === "parc" ? <Parc /> : <Atelier />}</main>
    </div>
  );
}
