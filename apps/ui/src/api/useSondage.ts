/**
 * Sonder une lecture — sans empiler les requêtes, ni tourner pour personne.
 *
 * `useResource` ne sonde pas, et c'est le bon défaut : le registre ne bouge que
 * lorsqu'on édite un YAML. La mémoire, elle, bouge sans qu'on touche à rien —
 * un job de la ligne de commande charge un modèle, un worker meurt, un autre
 * onglet décharge. C'est la raison d'être du bandeau de ressources, et la seule
 * lecture du front qui se rafraîchisse toute seule.
 *
 * Trois propriétés qui ne se devinent pas en lisant l'appel :
 *
 * **On replanifie après la réponse, jamais à intervalle fixe.** Un `setInterval`
 * de deux secondes sur un serveur qui met trois secondes à répondre empile les
 * requêtes jusqu'à saturation. Le prochain tour part de la fin du précédent.
 *
 * **Un échec n'efface pas les derniers chiffres.** Le serveur qu'on redémarre
 * pendant une saisie viderait le bandeau, et l'utilisateur perdrait ce qu'il
 * regardait. Les chiffres restent, `vu` dit de quand ils datent, et c'est à
 * l'affichage de le dire aussi : un chiffre de mémoire non daté qui a cessé
 * d'être vrai est pire qu'une absence de chiffre.
 *
 * **Un onglet caché ne sonde pas.** Cet outil vit dans un onglet qu'on laisse
 * ouvert des jours ; à deux secondes, cela fait quarante mille requêtes par
 * jour, dont chacune vérifie l'existence de processus. Le sondage s'arrête
 * quand l'onglet passe en arrière-plan et repart — immédiatement, sans attendre
 * la période — quand il revient. On lit `visibilityState` plutôt que `hidden`
 * parce que les deux disent la même chose et que seul le premier se simule.
 */

import { useCallback, useEffect, useRef, useState } from "react";

/** La cadence que la conception annonce pour le bandeau : deux secondes. */
export const PERIODE_MS = 2000;

export interface Sondage<T> {
  données: T | null;
  erreur: unknown;
  /** Instant du dernier succès, en millisecondes epoch. `null` avant le premier. */
  vu: number | null;
  /** Faux quand l'onglet est en arrière-plan : le sondage dort. */
  actif: boolean;
  rafraichir: () => void;
}

function visible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

/**
 * @param charger  fonction d'appel, qui doit propager le signal d'annulation
 * @param deps     ce dont dépend la requête ; un changement relance le sondage
 * @param periode  millisecondes entre la fin d'un tour et le début du suivant
 */
export function useSondage<T>(
  charger: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  periode: number = PERIODE_MS,
): Sondage<T> {
  const [données, setDonnées] = useState<T | null>(null);
  const [erreur, setErreur] = useState<unknown>(null);
  const [vu, setVu] = useState<number | null>(null);
  const [actif, setActif] = useState(visible);
  const [tour, setTour] = useState(0);

  // La fonction de chargement est une fermeture reconstruite à chaque rendu du
  // parent. La mettre dans les dépendances de l'effet redémarrerait le sondage
  // à chaque frappe du formulaire ; on la lit par référence.
  const chargerRef = useRef(charger);
  chargerRef.current = charger;

  const rafraichir = useCallback(() => setTour((n) => n + 1), []);

  useEffect(() => {
    const surVisibilité = () => setActif(visible());
    document.addEventListener("visibilitychange", surVisibilité);
    return () => document.removeEventListener("visibilitychange", surVisibilité);
  }, []);

  useEffect(() => {
    if (!actif) return;

    let vivant = true;
    let minuterie: ReturnType<typeof setTimeout> | undefined;
    const contrôle = new AbortController();

    const tourDeSondage = async () => {
      try {
        const valeur = await chargerRef.current(contrôle.signal);
        if (!vivant) return;
        setDonnées(valeur);
        setErreur(null);
        setVu(Date.now());
      } catch (cause) {
        if (!vivant || contrôle.signal.aborted) return;
        setErreur(cause);
      } finally {
        if (vivant) minuterie = setTimeout(tourDeSondage, periode);
      }
    };
    void tourDeSondage();

    return () => {
      vivant = false;
      contrôle.abort();
      clearTimeout(minuterie);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tour, actif, periode]);

  return { données, erreur, vu, actif, rafraichir };
}
