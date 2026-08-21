/**
 * Charger le contenu d'une sortie textuelle.
 *
 * Les visualiseurs de médias reçoivent une URL et la donnent au navigateur :
 * `<audio src=…>` se débrouille. Le texte et le JSON, eux, doivent être **lus** —
 * afficher l'URL dans un cadre de texte serait afficher le chemin du fichier à
 * la place de ce qu'il contient, et le défaut ne se verrait qu'au premier vrai
 * job.
 *
 * Il a été écrit au 4.3, alors qu'`href` valait toujours `null` et qu'aucune
 * requête n'en partait, pour que la fin du 4.4 n'ait qu'un résolveur à brancher.
 * C'est ce qui est arrivé : la route des fichiers existe, le résolveur lit
 * l'URL que le serveur compose, et pas une ligne d'ici n'a bougé.
 *
 * L'URL est absolue et pointe l'API, non la page : ce `fetch` est donc soumis au
 * CORS, comme toutes les lectures du front, et `credentials: "omit"` est posé
 * pour la même raison qu'ailleurs — le serveur n'autorise pas les requêtes
 * porteuses de cookies, et le navigateur les rejetterait sans message lisible.
 */

import { useEffect, useState } from "react";

export interface Contenu {
  texte: string | null;
  erreur: string | null;
  en_cours: boolean;
}

export function useContenu(href: string | null): Contenu {
  const [texte, setTexte] = useState<string | null>(null);
  const [erreur, setErreur] = useState<string | null>(null);
  const [en_cours, setEnCours] = useState(false);

  useEffect(() => {
    if (!href) {
      setTexte(null);
      setErreur(null);
      // Sans cette remise à zéro, un `href` qui redevient nul pendant une
      // lecture laisse « lecture… » figé pour toujours : le `finally` de la
      // requête précédente est gardé par `vivant`, que le nettoyage de l'effet
      // vient de passer à faux. L'écran afficherait alors « lecture… » juste
      // à côté de « fichier non résolu ».
      setEnCours(false);
      return;
    }
    const controle = new AbortController();
    let vivant = true;
    setEnCours(true);
    fetch(href, { credentials: "omit", signal: controle.signal })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`${r.status} ${r.statusText}`))))
      .then((t) => {
        if (!vivant) return;
        setTexte(t);
        setErreur(null);
      })
      .catch((cause) => {
        if (!vivant || controle.signal.aborted) return;
        setErreur(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (vivant) setEnCours(false);
      });
    return () => {
      vivant = false;
      controle.abort();
    };
  }, [href]);

  return { texte, erreur, en_cours };
}
