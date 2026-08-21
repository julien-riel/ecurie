import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

const racine = document.getElementById("ecurie");
if (!racine) throw new Error("point de montage #ecurie absent de index.html");
createRoot(racine).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
