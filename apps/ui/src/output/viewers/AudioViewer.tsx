import { Cadre } from "./_Cadre";
import type { ViewerProps } from "../registry";

/** `audio/*` — le lecteur natif suffit, et il lit le wav de `mlx-audio`. */
export function AudioViewer(props: ViewerProps) {
  return (
    <Cadre {...props} chemin={String(props.valeur)}>
      <audio controls src={props.href ?? undefined} data-viewer="audio" />
    </Cadre>
  );
}
