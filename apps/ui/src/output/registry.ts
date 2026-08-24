/**
 * Registre 2 — un `contentMediaType` de sortie vers un visualiseur.
 *
 * La seconde table d'aiguillage de la tâche 4.3, gouvernée par les mêmes règles
 * que la première : **totale** — `viewerFor` rend toujours une entrée, jamais
 * `undefined` — et **extensible sans toucher au reste** : ajouter un type de
 * média est une ligne.
 *
 * Elle compte sept lignes là où `CONCEPTION.md §7` en énumère cinq. La sixième
 * est `application/json`, que trois contrats produisent, dont `tool-use.calls`
 * qui est une sortie **requise**. L'oubli de la conception se serait vu au
 * premier appel d'outil, sous la forme d'un fichier tombé sur le repli. La
 * septième est `model/*`, arrivée avec le STEP de `pointcloud-to-cad` : une
 * géométrie d'échange qu'aucun navigateur ne lit et qu'on sait pourtant nommer.
 *
 * L'ordre de résolution va du plus précis au plus général : un type exact avant
 * une famille, une famille avant le repli. Sans cela, `image/*` capturerait
 * `image/svg+xml` avant qu'une entrée dédiée puisse l'attraper.
 */

import type { ComponentType } from "react";
import { AudioViewer } from "./viewers/AudioViewer";
import { GeometryViewer } from "./viewers/GeometryViewer";
import { ImageViewer } from "./viewers/ImageViewer";
import { InlineViewer } from "./viewers/InlineViewer";
import { JsonViewer } from "./viewers/JsonViewer";
import { MeshViewer } from "./viewers/MeshViewer";
import { TextViewer } from "./viewers/TextViewer";
import { UnknownViewer } from "./viewers/UnknownViewer";
import { VideoViewer } from "./viewers/VideoViewer";
import { matches } from "./mediaType";

export interface ViewerProps {
  /** Clé feuille de la sortie, par exemple « vocals ». */
  nom: string;
  /** Chemin pointé complet dans la réponse, par exemple « tracks.vocals ». */
  chemin: string;
  /** Ce que le worker a écrit : un chemin relatif au job, ou une valeur. */
  valeur: unknown;
  /** `null` pour une valeur inline. */
  mediaType: string | null;
  /**
   * URL résolue du fichier, composée par le serveur et lue dans `job.files`.
   * `null` reste un état **normal** — une sortie facultative que le worker n'a
   * pas produite n'en a pas —, et tout visualiseur doit savoir le rendre.
   */
  href: string | null;
}

export interface ViewerEntry {
  readonly id: string;
  accepts(mediaType: string): boolean;
  Component: ComponentType<ViewerProps>;
}

export const VIEWERS: readonly ViewerEntry[] = [
  { id: "audio", accepts: (t) => matches(t, "audio/*"), Component: AudioViewer },
  { id: "image", accepts: (t) => matches(t, "image/*"), Component: ImageViewer },
  { id: "video", accepts: (t) => matches(t, "video/*"), Component: VideoViewer },
  { id: "mesh", accepts: (t) => matches(t, "model/gltf-binary"), Component: MeshViewer },
  // Après `mesh`, et l'ordre fait le sens : un GLB se regardera un jour, un STEP
  // jamais ici. Cette ligne dit « je sais ce que c'est, et cela s'ouvre
  // ailleurs », là où le repli dirait « je ne sais pas ».
  { id: "geometrie", accepts: (t) => matches(t, "model/*"), Component: GeometryViewer },
  { id: "json", accepts: (t) => matches(t, "application/json"), Component: JsonViewer },
  { id: "text", accepts: (t) => matches(t, "text/*"), Component: TextViewer },
];

export const VIEWER_INLINE: ViewerEntry = {
  id: "inline",
  accepts: () => false,
  Component: InlineViewer,
};

export const VIEWER_INCONNU: ViewerEntry = {
  id: "inconnu",
  accepts: () => true,
  Component: UnknownViewer,
};

/** Le visualiseur d'un type de média. Totale : rend toujours une entrée. */
export function viewerFor(mediaType: string | null): ViewerEntry {
  if (mediaType === null) return VIEWER_INLINE;
  return VIEWERS.find((v) => v.accepts(mediaType)) ?? VIEWER_INCONNU;
}
