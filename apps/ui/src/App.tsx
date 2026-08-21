/**
 * La coquille — un titre, et l'écran du moment.
 *
 * Elle ne fait presque rien, et c'est voulu : l'architecture pose un **plafond
 * ferme de quatre écrans** — Atelier (4.4), Parc (4.5), Confrontation (5.4),
 * Bibliothèque (5.5) —, et le risque nommé du projet est « dérive vers un clone
 * de ComfyUI ». Une coquille qui gagnerait des barres d'outils, un thème et un
 * état global serait la première marche de cette dérive.
 *
 * Elle ne porte donc **aucun état d'écran**. Le bandeau de ressources est rendu
 * par l'Atelier et non ici, bien que la conception l'appelle « composant
 * global » : le chiffre qui compte est celui du variant sélectionné, et le
 * hisser dans la coquille obligerait à y faire remonter la sélection de
 * l'Atelier. Il est global par sa nature — n'importe quel écran peut le poser —
 * et pas par sa place dans l'arbre.
 *
 * Il n'y a pas non plus de navigation : le v0.4 n'a qu'un écran, et un onglet
 * unique est un décor. Elle arrivera avec le Parc, à la tâche 4.5, quand il y
 * aura quelque chose entre quoi choisir.
 */

import { Atelier } from "./ecrans/Atelier";

export function App() {
  return (
    <main className="ecurie">
      <header>
        <h1>Écurie</h1>
        <p className="ecurie-etat-champ">
          Atelier — le formulaire vient du contrat de capacité, le coût vient du serveur.
        </p>
      </header>
      <Atelier />
    </main>
  );
}
