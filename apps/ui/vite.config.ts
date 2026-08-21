/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * `strictPort` n'est pas un détail de confort.
 *
 * L'API n'autorise que deux origines, `http://localhost:5173` et
 * `http://127.0.0.1:5173` (`ecurie_api/app.py`). Sans `strictPort`, Vite glisse
 * en 5174 quand 5173 est pris, l'origine sort de la liste, et le navigateur
 * refuse chaque requête avec une erreur que JavaScript ne distingue pas d'un
 * serveur éteint. Échouer bruyamment au démarrage vaut mieux qu'un écran vide
 * qu'on met une heure à diagnostiquer.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
