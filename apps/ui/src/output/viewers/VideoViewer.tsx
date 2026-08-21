import { Cadre } from "./_Cadre";
import type { ViewerProps } from "../registry";

/** `video/*` — lecteur natif. */
export function VideoViewer(props: ViewerProps) {
  return (
    <Cadre {...props} chemin={String(props.valeur)}>
      <video controls src={props.href ?? undefined} data-viewer="video" />
    </Cadre>
  );
}
