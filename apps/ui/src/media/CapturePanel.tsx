/**
 * Le panneau de capture — prendre une photo, enregistrer un son, filmer.
 *
 * Il ne dépose rien lui-même : il rend un `Blob` et un nom à son parent, qui
 * sait à quel champ cela s'adresse et ce qu'il faut faire du chemin obtenu. La
 * séparation compte, parce que la même capture servira demain à autre chose
 * qu'un champ de formulaire — un banc de comparaison, une entrée de la
 * Bibliothèque — et qu'un composant qui saurait déjà téléverser aurait figé
 * cette destination-là.
 *
 * Trois faits gouvernent son état, et chacun se voit à l'écran.
 *
 * **Le matériel ne s'allume que sur demande.** Rien ne s'ouvre au montage :
 * `getUserMedia` fait apparaître une autorisation système, et un formulaire qui
 * la réclamerait parce qu'un champ accepte une image serait insupportable. Le
 * premier clic ouvre, le dernier ferme.
 *
 * **Ce qui est ouvert se referme, sur tous les chemins.** Changer de mode,
 * fermer le panneau, démonter l'écran, échouer à enregistrer : la diode de la
 * caméra doit s'éteindre à chaque fois. C'est la faute la plus visible qu'on
 * puisse commettre ici, et la seule qui survive à la page.
 *
 * **Un refus d'autorisation est une phrase, pas un silence.** `getUserMedia`
 * rejette avec un `NotAllowedError` quand l'utilisateur refuse et un
 * `NotFoundError` quand la machine n'a pas le périphérique — deux situations
 * différentes qui appellent deux gestes différents, et qu'un « échec de la
 * caméra » unique confondrait.
 */

import { useEffect, useRef, useState } from "react";
import {
  type Materiel,
  type ModeCapture,
  TYPES_AUDIO,
  TYPES_VIDEO,
  captureDisponible,
  collecter,
  contraintes,
  fermer,
  materielDuNavigateur,
  modesPour,
  nomDeCapture,
  premierTypeSupporte,
  trameEnPng,
  versWav,
} from "./capture";
import { TYPE_WAV } from "./wav";

export interface CapturePanelProps {
  /** Types acceptés par le champ — ce sont eux qui décident des modes offerts. */
  accept: readonly string[];
  /** Reçoit la capture. Le parent en fait ce qu'il veut : ici, un dépôt. */
  onCapture(fichier: Blob, nom: string): void | Promise<void>;
  /** Le champ est-il en lecture seule, ou un dépôt est-il déjà en cours ? */
  disabled?: boolean;
  /** Injecté par les tests : jsdom n'a ni caméra, ni micro, ni `MediaRecorder`. */
  materiel?: Materiel;
}

const LIBELLES: Record<ModeCapture, { ouvrir: string; agir: string }> = {
  photo: { ouvrir: "Caméra", agir: "Prendre la photo" },
  audio: { ouvrir: "Micro", agir: "Enregistrer" },
  video: { ouvrir: "Caméra + micro", agir: "Filmer" },
};

/** Ce que `getUserMedia` refuse, dit en français et avec le geste qui répare. */
export function phraseRefus(cause: unknown, mode: ModeCapture): string {
  const nom = cause instanceof Error ? cause.name : "";
  const quoi = mode === "audio" ? "le micro" : mode === "photo" ? "la caméra" : "la caméra et le micro";
  if (nom === "NotAllowedError" || nom === "SecurityError") {
    return `Accès à ${quoi} refusé. L'autoriser pour ce site dans les réglages du navigateur, puis réessayer.`;
  }
  if (nom === "NotFoundError" || nom === "OverconstrainedError") {
    return `Aucun périphérique correspondant à ${quoi} sur cette machine.`;
  }
  if (nom === "NotReadableError") {
    return `${quoi.charAt(0).toUpperCase()}${quoi.slice(1)} est déjà utilisé par une autre application.`;
  }
  return `Impossible d'ouvrir ${quoi} : ${cause instanceof Error ? cause.message : String(cause)}`;
}

export function CapturePanel({ accept, onCapture, disabled, materiel }: CapturePanelProps) {
  const [mode, setMode] = useState<ModeCapture | null>(null);
  const [enregistre, setEnregistre] = useState(false);
  const [secondes, setSecondes] = useState(0);
  const [erreur, setErreur] = useState<string | null>(null);
  const [occupe, setOccupe] = useState(false);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const toileRef = useRef<HTMLCanvasElement | null>(null);
  const fluxRef = useRef<MediaStream | null>(null);
  const enregistreurRef = useRef<MediaRecorder | null>(null);
  // Figé au premier rendu : reconstruire l'objet à chaque passe ferait de lui une
  // dépendance qui change tout le temps, et l'effet de nettoyage tournerait en boucle.
  const matérielRef = useRef<Materiel | null>(null);
  if (matérielRef.current === null) matérielRef.current = materiel ?? materielDuNavigateur();
  const outils = matérielRef.current;

  const modes = modesPour(accept);

  // Le seul effet du composant, et il ne fait qu'une chose : rendre le matériel
  // quand l'écran disparaît. Un flux ouvert survit au démontage — React ne coupe
  // rien tout seul —, et l'utilisateur verrait sa caméra allumée sur un écran
  // qui ne l'affiche plus.
  useEffect(() => {
    return () => {
      fermer(fluxRef.current);
      fluxRef.current = null;
    };
  }, []);

  // Le chronomètre n'est pas une décoration : sans lui, un enregistrement audio
  // n'a aucun signe visible, et rien ne distingue « j'enregistre » de « j'ai
  // oublié de cliquer ».
  useEffect(() => {
    if (!enregistre) return;
    const début = Date.now();
    setSecondes(0);
    const battement = setInterval(() => setSecondes(Math.floor((Date.now() - début) / 1000)), 500);
    return () => clearInterval(battement);
  }, [enregistre]);

  function refermer() {
    fermer(fluxRef.current);
    fluxRef.current = null;
    enregistreurRef.current = null;
    setEnregistre(false);
    setMode(null);
    if (videoRef.current) videoRef.current.srcObject = null;
  }

  async function ouvrir(voulu: ModeCapture) {
    if (mode === voulu) {
      refermer();
      return;
    }
    fermer(fluxRef.current);
    fluxRef.current = null;
    setErreur(null);
    setEnregistre(false);
    try {
      const flux = await outils.flux(contraintes(voulu));
      fluxRef.current = flux;
      setMode(voulu);
      // Branché après le rendu du mode : l'élément `<video>` n'existe pas tant
      // que `mode` est nul, et lui poser un `srcObject` avant reviendrait à
      // l'écrire sur `null`.
      // La lecture est déclarée sur l'élément (`autoPlay muted`) et non lancée
      // par un `play()` : les deux marchent, mais le second rend une promesse
      // que le navigateur rejette dès que sa politique de lecture automatique
      // s'y oppose, et il faudrait alors distinguer un vrai échec d'un refus
      // sans conséquence — la trame se lit dans le flux, pas dans l'élément.
      queueMicrotask(() => {
        if (videoRef.current) videoRef.current.srcObject = flux;
      });
    } catch (cause) {
      setErreur(phraseRefus(cause, voulu));
      setMode(null);
    }
  }

  async function rendre(fichier: Blob, nom: string) {
    setOccupe(true);
    try {
      await onCapture(fichier, nom);
      refermer();
    } catch (cause) {
      // Le dépôt a échoué : on garde le matériel ouvert. Refermer obligerait à
      // reprendre la photo qu'on vient de réussir.
      setErreur(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setOccupe(false);
    }
  }

  async function prendreLaPhoto() {
    const video = videoRef.current;
    const toile = toileRef.current;
    if (!video || !toile) return;
    setErreur(null);
    try {
      const image = await trameEnPng(video, toile, outils);
      await rendre(image, nomDeCapture("photo", image.type, new Date()));
    } catch (cause) {
      setErreur(cause instanceof Error ? cause.message : String(cause));
    }
  }

  function demarrerEnregistrement(voulu: ModeCapture) {
    const flux = fluxRef.current;
    if (!flux) return;
    setErreur(null);
    const candidats = voulu === "audio" ? TYPES_AUDIO : TYPES_VIDEO;
    try {
      const enregistreur = outils.enregistreur(
        flux,
        premierTypeSupporte(candidats, outils.typeSupporte),
      );
      enregistreurRef.current = enregistreur;
      const attendu = collecter(enregistreur);
      enregistreur.start();
      setEnregistre(true);
      void attendu
        .then(async (brut) => {
          // Le son repart en WAV : `MediaRecorder` ne sait pas l'écrire, et
          // l'env `mlx-audio` du parc ne sait pas lire de l'opus (voir `wav.ts`).
          const fichier = voulu === "audio" ? await versWav(brut, outils) : brut;
          const type = voulu === "audio" ? TYPE_WAV : fichier.type;
          await rendre(fichier, nomDeCapture(voulu, type, new Date()));
        })
        .catch((cause) => {
          setErreur(cause instanceof Error ? cause.message : String(cause));
          setEnregistre(false);
        });
    } catch (cause) {
      setErreur(
        `Le navigateur a refusé de démarrer l'enregistrement : ` +
          `${cause instanceof Error ? cause.message : String(cause)}`,
      );
    }
  }

  function arreterEnregistrement() {
    setEnregistre(false);
    try {
      enregistreurRef.current?.stop();
    } catch {
      // Déjà arrêté : le blob est en route, rien à signaler.
    }
  }

  if (modes.length === 0) return null;
  if (!materiel && !captureDisponible()) {
    return (
      <p className="ecurie-etat-champ">
        Ce navigateur n'expose ni caméra ni micro à cette page. La capture demande une
        origine sécurisée — <code>localhost</code> en fait partie.
      </p>
    );
  }

  return (
    <div className="ecurie-capture">
      <div className="ecurie-capture-modes">
        {modes.map((m) => (
          <button
            key={m}
            type="button"
            aria-pressed={mode === m}
            disabled={disabled || occupe || enregistre}
            onClick={() => void ouvrir(m)}
          >
            {LIBELLES[m].ouvrir}
          </button>
        ))}
        {mode ? (
          <button type="button" onClick={refermer} disabled={occupe}>
            Fermer
          </button>
        ) : null}
      </div>

      {mode ? (
        <div className="ecurie-capture-scene">
          {mode === "audio" ? (
            <p className="ecurie-etat-champ" role="status">
              {enregistre ? `Enregistrement — ${secondes} s` : "Micro ouvert, prêt à enregistrer."}
            </p>
          ) : (
            <>
              {/* Muet et sans commande : c'est un viseur, pas un lecteur. Le son
                  du micro rejoué dans les haut-parleurs ferait un larsen — et
                  `muted` est aussi ce qui autorise `autoPlay` sans geste de
                  l'utilisateur, sur tous les navigateurs. */}
              <video ref={videoRef} autoPlay muted playsInline aria-label="Aperçu de la caméra" />
              {enregistre ? (
                <p className="ecurie-etat-champ" role="status">
                  Enregistrement — {secondes} s
                </p>
              ) : null}
            </>
          )}
          <canvas ref={toileRef} hidden />
          <div className="ecurie-capture-actions">
            {mode === "photo" ? (
              <button
                type="button"
                className="ecurie-primaire"
                disabled={disabled || occupe}
                onClick={() => void prendreLaPhoto()}
              >
                {occupe ? "dépôt…" : LIBELLES.photo.agir}
              </button>
            ) : enregistre ? (
              <button type="button" className="ecurie-primaire" onClick={arreterEnregistrement}>
                Arrêter
              </button>
            ) : (
              <button
                type="button"
                className="ecurie-primaire"
                disabled={disabled || occupe}
                onClick={() => demarrerEnregistrement(mode)}
              >
                {occupe ? "dépôt…" : LIBELLES[mode].agir}
              </button>
            )}
          </div>
        </div>
      ) : null}

      {erreur ? <p className="text-danger">{erreur}</p> : null}
    </div>
  );
}
