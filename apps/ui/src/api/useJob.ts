/**
 * Lancer un job, et le suivre jusqu'à sa sortie.
 *
 * Ni `useResource` ni `useSondage` ne conviennent, et pas parce qu'il faudrait
 * une troisième variante du même patron : un job **s'écrit** — il n'existe pas
 * avant qu'on clique —, il dure des minutes, et son état arrive par un flux et
 * non par une réponse. Les deux autres chargent une ressource qui existe déjà.
 *
 * Quatre choses gouvernent ce fichier.
 *
 * **Ce qui refuse un job avant sa création, et ce qui ne peut le refuser
 * qu'après.** Un modèle inconnu, un variant que le disque contredit, une entrée
 * hors contrat : la soumission échoue, `erreur` porte la phrase du serveur, et
 * aucun job n'existe. L'**admission**, elle, ne se tranche qu'au moment de
 * charger — d'ici là un autre job a pu libérer la place —, si bien qu'un refus
 * mémoire arrive par le flux, en état `failed`. Un écran qui ne regarderait que
 * le code HTTP croirait tous ses jobs partis.
 *
 * **Le flux rejoue depuis le début.** C'est ce qui rend `reprendre` trivial :
 * il n'y a rien à rattraper, aucun curseur à tenir, aucun risque de trou. Se
 * rabonner après une coupure redonne l'histoire entière.
 *
 * **L'événement `end` ne porte pas un job.** Sa charge est `{id, state}`, un
 * objet à deux clés — le poser dans l'état effacerait tout le reste. Il dit une
 * chose et une seule : il n'y aura plus rien à attendre.
 *
 * **Un job survit à l'écran qui l'a lancé.** Fermer l'onglet n'annule que le
 * suivi ; le serveur poursuit, écrit son manifeste, et `GET /jobs/{id}` le
 * retrouvera. C'est pourquoi le démontage annule le flux sans rien tuer.
 *
 * **Une requête en vol n'a pas de bouton d'annulation, elle a une génération.**
 * `AbortController` coupe le flux, mais deux requêtes courtes lui échappent : le
 * `POST` de la soumission et le `GET` de la reprise. Les laisser poser leur
 * résultat au retour produisait deux défauts observés — un job soumis puis
 * oublié (l'utilisateur change de capacité pendant que le POST vole) revenait
 * bloquer le bouton *Lancer* **sans** son panneau, donc sans le bouton qui
 * l'aurait retiré ; et un job retiré pendant une reprise réapparaissait tout
 * seul. Chaque geste ouvre donc une génération, et une réponse d'une génération
 * périmée est jetée.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./endpoints";
import { messagesErreur } from "./errors";
import type { Job } from "./types";

export interface SuiviJob {
  /** Le dernier état complet reçu. `null` tant qu'aucun job n'a été lancé. */
  job: Job | null;
  /**
   * Ce qui a empêché de lancer ou de suivre — jamais l'échec du job lui-même.
   *
   * Un tableau, et vide plutôt que `null` : un refus de variant porte une phrase
   * par cause, chacune avec la commande qui la répare, et les recoller en une
   * ligne rendrait la seconde illisible.
   */
  erreur: readonly string[];
  /**
   * L'erreur porte sur le **suivi**, non sur la soumission.
   *
   * Les deux ne se réparent pas de la même façon, et les confondre mettrait un
   * bouton « reprendre » sous un 409 qui dit de télécharger des poids. Un job
   * dont le suivi a lâché continue de tourner ; une soumission refusée n'a rien
   * lancé du tout.
   */
  interrompu: boolean;
  /** Une soumission est partie, sa réponse n'est pas revenue. */
  envoi: boolean;
  lancer: (ref: string, input: Record<string, unknown>) => Promise<void>;
  /** Relit l'état du job et rouvre son flux — après une coupure, ou un serveur redémarré. */
  reprendre: () => Promise<void>;
  /** Retire le job de l'écran. Ne l'arrête pas — pour cela, `arreter`. */
  oublier: () => void;
  /**
   * Arrête le job en cours. Le serveur tue le worker ; le job rend `failed`
   * avec `cancelled`, et le texte déjà reçu reste dans l'état.
   */
  arreter: () => Promise<void>;
  /** Un arrêt est parti, sa réponse n'est pas revenue. */
  arretEnCours: boolean;
}

export function useJob(): SuiviJob {
  const [job, setJob] = useState<Job | null>(null);
  const [erreur, setErreur] = useState<readonly string[]>([]);
  const [interrompu, setInterrompu] = useState(false);
  const [envoi, setEnvoi] = useState(false);
  const [arretEnCours, setArret] = useState(false);

  const controle = useRef<AbortController | null>(null);
  const monte = useRef(true);
  // Chaque geste — lancer, reprendre, oublier — ouvre une génération. Une
  // réponse qui revient sous une génération périmée n'a plus de destinataire.
  const generation = useRef(0);

  useEffect(() => {
    // Remis à vrai à chaque montage, et pas seulement déclaré vrai à
    // l'initialisation : `StrictMode` monte, démonte et remonte, si bien qu'un
    // drapeau posé une fois resterait faux pour toute la vie du composant — et
    // l'écran n'afficherait plus jamais rien en développement.
    monte.current = true;
    return () => {
      monte.current = false;
      controle.current?.abort();
    };
  }, []);

  /** Ce résultat a-t-il encore un destinataire ? */
  const perime = useCallback(
    (mienne: number) => !monte.current || mienne !== generation.current,
    [],
  );

  const suivre = useCallback(
    async (id: string, mienne: number) => {
      controle.current?.abort();
      const abandon = new AbortController();
      controle.current = abandon;

      let fin = false;
      try {
        await api.evenementsDuJob(
          id,
          (evenement) => {
            if (evenement.event === "end") {
              fin = true;
              return; // `{id, state}` seulement : le poser effacerait le reste
            }
            if (perime(mienne)) return;
            if (evenement.event === "delta") {
              // Un `delta` ne porte pas un job mais le seul fragment produit.
              // Le poser tel quel remplacerait l'état entier par deux clés —
              // le même piège que `end`, avec cette différence qu'il arrive des
              // centaines de fois par job au lieu d'une.
              setJob((precedent) => appliquerDelta(precedent, JSON.parse(evenement.data)));
              return;
            }
            setJob(JSON.parse(evenement.data) as Job);
          },
          abandon.signal,
        );
      } catch (cause) {
        if (abandon.signal.aborted || perime(mienne)) return;
        setErreur(messagesErreur(cause));
        setInterrompu(true);
        return;
      }
      if (!fin && !abandon.signal.aborted && !perime(mienne)) {
        // Le serveur a fermé sans dire que c'était fini : il s'est arrêté, ou la
        // connexion est tombée. Le job, lui, peut très bien continuer.
        setErreur(["le suivi s'est interrompu avant la fin du job"]);
        setInterrompu(true);
      }
    },
    [perime],
  );

  const lancer = useCallback(
    async (ref: string, input: Record<string, unknown>) => {
      const mienne = (generation.current += 1);
      setErreur([]);
      setInterrompu(false);
      setEnvoi(true);
      try {
        const soumis = await api.lancerJob(ref, input);
        if (perime(mienne)) return;
        setJob(soumis);
        void suivre(soumis.id, mienne);
      } catch (cause) {
        if (!perime(mienne)) setErreur(messagesErreur(cause));
      } finally {
        if (!perime(mienne)) setEnvoi(false);
      }
    },
    [perime, suivre],
  );

  const reprendre = useCallback(async () => {
    const id = job?.id;
    if (!id) return;
    const mienne = (generation.current += 1);
    setErreur([]);
    setInterrompu(false);
    try {
      // La lecture d'abord, le flux ensuite, et dans cet ordre : `GET /jobs/{id}`
      // répond même pour un job d'une session précédente, en lisant le manifeste
      // sur le disque, là où le flux d'un job que la table a perdu est un 404.
      const relu = await api.job(id);
      if (perime(mienne)) return;
      setJob(relu);
      if (relu.state === "queued" || relu.state === "running") await suivre(id, mienne);
    } catch (cause) {
      if (perime(mienne)) return;
      setErreur(messagesErreur(cause));
      setInterrompu(true);
    }
  }, [job?.id, perime, suivre]);

  const arreter = useCallback(async () => {
    const id = job?.id;
    if (!id || !job || job.state === "done" || job.state === "failed") return;
    setArret(true);
    try {
      // La réponse porte l'état d'après l'arrêt, mais on ne la pose pas : le
      // flux est toujours ouvert et va livrer la suite — dont l'état final, qui
      // arrivera de toute façon. Poser celui-ci en plus ferait reculer
      // l'affichage d'un cran si le flux a déjà pris de l'avance.
      await api.arreterJob(id);
    } catch (cause) {
      // L'arrêt a échoué, le job continue : c'est une erreur de commande, pas
      // de suivi. `interrompu` resterait faux, et c'est voulu — il n'y a rien à
      // reprendre.
      setErreur(messagesErreur(cause));
    } finally {
      if (monte.current) setArret(false);
    }
  }, [job]);

  const oublier = useCallback(() => {
    // La génération avance **avant** l'abandon : ce qui vole encore — un `POST`
    // ou un `GET` que rien n'annule — retombera sur une génération périmée et
    // sera jeté, au lieu de faire revenir un job que l'écran vient de retirer.
    generation.current += 1;
    controle.current?.abort();
    setJob(null);
    setErreur([]);
    setInterrompu(false);
    setEnvoi(false);
    setArret(false);
  }, []);

  return { job, erreur, interrompu, envoi, lancer, reprendre, oublier, arreter, arretEnCours };
}

/**
 * Un fragment reçu, rangé dans le canal qui lui revient.
 *
 * Le serveur tient le même cumul de son côté et le remet dans chaque état
 * complet : ce n'est donc pas une reconstruction fragile mais une avance, entre
 * deux états. Le prochain `state` reçu remplacera ce cumul par celui du serveur,
 * et les deux diront la même chose.
 */
export function appliquerDelta(
  precedent: Job | null,
  delta: { text?: string; channel?: string },
): Job | null {
  if (!precedent) return precedent;
  const texte = delta.text ?? "";
  if (!texte) return precedent;
  if (delta.channel === "reasoning") {
    return { ...precedent, stream_reasoning: (precedent.stream_reasoning ?? "") + texte };
  }
  return { ...precedent, stream_text: (precedent.stream_text ?? "") + texte };
}
