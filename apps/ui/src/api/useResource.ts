/**
 * Charger une ressource de l'API, sans cache et sans sondage.
 *
 * L'absence de cache est un choix, pas une simplification. Le serveur **recharge
 * le registre à chaud** dès qu'un fichier de `registry/` a bougé, précisément
 * pour que « ajouter un modèle = ajouter un YAML » n'oblige pas à redémarrer
 * quoi que ce soit. Un cache côté front annulerait cette propriété, et rendrait
 * un YAML qu'on vient d'écrire invisible jusqu'au rechargement de l'onglet :
 * ici, le cache est le défaut, pas la fonctionnalité.
 *
 * L'absence de sondage en est le pendant : rien ne se rafraîchit tout seul au
 * 4.3. Le bandeau de ressources rafraîchi toutes les deux secondes appartient à
 * l'écran Atelier (4.4) ; l'installer maintenant ferait tourner une requête
 * permanente pour un écran qui n'existe pas.
 */

import { useCallback, useEffect, useState } from "react";

export interface Ressource<T> {
  données: T | null;
  erreur: unknown;
  en_cours: boolean;
  recharger: () => void;
}

/**
 * @param charger  fonction d'appel, qui doit propager le signal d'annulation
 * @param deps     ce dont dépend la requête ; un changement relance l'appel
 */
export function useResource<T>(
  charger: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): Ressource<T> {
  const [données, setDonnées] = useState<T | null>(null);
  const [erreur, setErreur] = useState<unknown>(null);
  const [en_cours, setEnCours] = useState(false);
  const [tour, setTour] = useState(0);

  const recharger = useCallback(() => setTour((n) => n + 1), []);

  useEffect(() => {
    const controle = new AbortController();
    let vivant = true;
    setEnCours(true);
    charger(controle.signal)
      .then((valeur) => {
        if (!vivant) return;
        setDonnées(valeur);
        setErreur(null);
      })
      .catch((cause) => {
        // Une requête annulée n'est pas un échec : c'est un changement de
        // sélection plus rapide que le réseau. L'afficher ferait clignoter une
        // erreur rouge à chaque clic.
        if (!vivant || controle.signal.aborted) return;
        setErreur(cause);
      })
      .finally(() => {
        if (vivant) setEnCours(false);
      });
    return () => {
      vivant = false;
      controle.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tour]);

  return { données, erreur, en_cours, recharger };
}
