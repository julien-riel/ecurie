/**
 * `x-ui: "file"` — un chemin local, obtenu de trois façons.
 *
 * Ce que le contrat déclare n'a pas changé : une chaîne, qui est un **chemin**,
 * que le worker ouvrira depuis le disque. Ce qui a changé, c'est qu'on n'oblige
 * plus à le taper.
 *
 * Le widget disait jusqu'ici : *« Le sélecteur de fichier natif est montré mais
 * inerte, et il le dit : le navigateur ne donne pas le chemin réel d'un fichier
 * choisi — seulement son nom et son contenu. Le faire croire serait pire que de
 * l'annoncer. »* Le constat était juste et il l'est toujours. La conclusion, non :
 * **le contenu suffit**. L'API et le navigateur tournent sur la même machine ;
 * `POST /uploads` écrit ce contenu et rend le chemin qu'il vient de créer. Le
 * champ porte alors exactement ce qu'on aurait tapé.
 *
 * Trois sources, et aucune n'est de trop :
 *
 * - **le chemin saisi**, qui reste la voie la plus rapide quand on l'a sous la
 *   main, et la seule qui ne copie aucun octet ;
 * - **un fichier du disque**, choisi par le sélecteur natif — le glisser-déposer
 *   depuis une page web y arrive aussi, et c'est le seul cas où le fichier n'a
 *   jamais existé sur ce disque ;
 * - **la caméra ou le micro**, pour ce qui n'existe pas encore du tout.
 *
 * Les deux dernières passent par le même dépôt, parce qu'elles arrivent au même
 * endroit : un `Blob` sans chemin.
 *
 * Une chose que ce widget ne fait pas : effacer le chemin quand un dépôt échoue.
 * Ce qui était dans le champ y était pour une raison, et une panne de réseau
 * n'est pas une raison de la perdre.
 */

import { useState } from "react";
import type { WidgetProps } from "@rjsf/utils";
import * as api from "../../api/endpoints";
import { phraseErreur } from "../../api/errors";
import { formatBytes } from "../../format/bytes";
import { CapturePanel } from "../../media/CapturePanel";
import type { Materiel } from "../../media/capture";

/** Ce qui vient d'être déposé — affiché sous le champ, jamais renvoyé au serveur. */
interface Depot {
  nom: string;
  octets: number;
}

export function FilePathWidget(props: WidgetProps) {
  const { id, value, required, disabled, readonly, onChange, onBlur, onFocus, options } = props;
  const types = Array.isArray(options?.accept) ? (options.accept as string[]) : [];
  // Le matériel de capture n'est jamais fourni par un contrat : c'est un point
  // d'injection pour les tests, que `uiSchema.ts` ne pose pas.
  const materiel = options?.materiel as Materiel | undefined;

  const [depot, setDepot] = useState<Depot | null>(null);
  const [envoi, setEnvoi] = useState(false);
  const [erreur, setErreur] = useState<string | null>(null);
  const [survol, setSurvol] = useState(false);

  async function deposer(fichier: Blob, nom: string) {
    setEnvoi(true);
    setErreur(null);
    try {
      const écrit = await api.deposerFichier(fichier, nom);
      onChange(écrit.path);
      setDepot({ nom: écrit.name, octets: écrit.size_bytes });
    } catch (cause) {
      // Relancé, et pas seulement affiché : `CapturePanel` garde la caméra
      // ouverte quand son parent échoue, pour ne pas faire reprendre la photo.
      const phrase = phraseErreur(cause);
      setErreur(phrase);
      throw new Error(phrase);
    } finally {
      setEnvoi(false);
    }
  }

  function choisirSurLeDisque(fichiers: FileList | null | undefined) {
    const fichier = fichiers?.[0];
    if (!fichier) return;
    void deposer(fichier, fichier.name).catch(() => {
      // Déjà affiché par `deposer`. Le `catch` n'est là que pour ne pas laisser
      // une promesse rejetée remonter à la console.
    });
  }

  const inerte = disabled || readonly || envoi;

  return (
    <div
      className="ecurie-fichier"
      data-survol={survol ? "oui" : undefined}
      /*
        Glisser-déposer et collage : le geste le plus direct pour « prendre une
        image dans une page web ». Le navigateur télécharge lui-même l'image
        glissée depuis un onglet et la présente comme un fichier, ce qui la fait
        arriver exactement là où arrivent les deux autres sources — un `Blob`
        sans chemin. Quand il ne le fait pas, `dataTransfer.files` est vide et
        rien ne se passe : suivre l'URL à sa place demanderait au **serveur** de
        sortir sur le réseau, ce qu'un parc local n'a aucune raison de faire.

        `onDragOver` doit appeler `preventDefault` pour que le dépôt soit
        accepté : sans lui, le navigateur ouvre le fichier dans un nouvel
        onglet et le formulaire disparaît.
      */
      onDragOver={(e) => {
        if (inerte) return;
        e.preventDefault();
        setSurvol(true);
      }}
      onDragLeave={() => setSurvol(false)}
      onDrop={(e) => {
        if (inerte) return;
        e.preventDefault();
        setSurvol(false);
        choisirSurLeDisque(e.dataTransfer?.files);
      }}
      onPaste={(e) => {
        if (inerte || !e.clipboardData?.files?.length) return;
        e.preventDefault();
        choisirSurLeDisque(e.clipboardData.files);
      }}
    >
      <input
        id={id}
        className="form-control"
        type="text"
        value={value ?? ""}
        required={required}
        disabled={disabled}
        readOnly={readonly}
        spellCheck={false}
        placeholder="/chemin/vers/le/fichier"
        onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
        onBlur={(e) => onBlur(id, e.target.value)}
        onFocus={(e) => onFocus(id, e.target.value)}
      />
      <p className="ecurie-etat-champ">
        Chemin sur cette machine — ou un fichier glissé ici, collé, ou capté par la caméra
        {types.length > 0 ? `. Types attendus : ${types.join(", ")}` : ""}.
      </p>

      <div className="ecurie-fichier-sources">
        <label className="ecurie-fichier-parcourir">
          <span>{envoi ? "dépôt…" : "Choisir un fichier…"}</span>
          <input
            type="file"
            aria-label="Choisir un fichier sur cette machine"
            accept={types.join(",") || undefined}
            disabled={inerte}
            onChange={(e) => {
              choisirSurLeDisque(e.target.files);
              // Remis à zéro pour que rechoisir le même fichier déclenche à
              // nouveau l'événement : sinon un second essai après un échec de
              // dépôt ne se passe rien du tout.
              e.target.value = "";
            }}
          />
        </label>

        <CapturePanel
          accept={types}
          disabled={inerte}
          materiel={materiel}
          onCapture={deposer}
        />
      </div>

      {depot ? (
        <p className="ecurie-etat-champ">
          Déposé : {depot.nom} ({formatBytes(depot.octets)}). Le job en fera sa propre copie.
        </p>
      ) : null}
      {erreur ? <p className="text-danger">{erreur}</p> : null}
    </div>
  );
}
