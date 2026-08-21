import { useState } from "react";
import { Cadre } from "./_Cadre";
import type { ViewerProps } from "../registry";

/** `image/*` — l'image, et un zoom au clic qui ne coûte aucune bibliothèque. */
export function ImageViewer(props: ViewerProps) {
  const [zoom, setZoom] = useState(false);
  return (
    <Cadre {...props} chemin={String(props.valeur)}>
      <img
        src={props.href ?? undefined}
        alt={props.nom}
        data-viewer="image"
        className={zoom ? "ecurie-zoom" : undefined}
        onClick={() => setZoom((z) => !z)}
      />
    </Cadre>
  );
}
