import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// Configuration Vite — serveur de dev exposé pour Docker Compose + PWA.
//
// PWA (tâche 14.2, NFR-5 / NFR-11) : stratégie `injectManifest` pour un service
// worker personnalisé écrit en TypeScript (`src/sw.ts`, Workbox). Le manifeste
// (`manifest.webmanifest`) est émis par le plugin et l'app devient installable.
// Le service worker précache l'« app shell » et met en cache à l'exécution les
// ressources de carte afin d'un rechargement/fonctionnement hors ligne.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      registerType: "prompt",
      injectRegister: null, // enregistrement géré manuellement dans main.tsx
      // Permet de tester le service worker en `vite dev` (type module pour TS).
      devOptions: {
        enabled: false,
        type: "module",
      },
      injectManifest: {
        // Précacher l'app shell et les icônes ; GeoJSON embarqué si présent.
        globPatterns: ["**/*.{js,css,html,ico,png,svg,webmanifest,geojson}"],
        maximumFileSizeToCacheInBytes: 5 * 1024 * 1024,
      },
      includeAssets: ["favicon.svg", "icons/apple-touch-icon.png"],
      manifest: {
        // Noms et description en français (NFR-10).
        name: "Centres médicaux du Togo",
        short_name: "Centres Togo",
        description:
          "Annuaire et carte des centres médicaux du Togo — installable et utilisable hors ligne.",
        lang: "fr",
        dir: "ltr",
        start_url: "/",
        scope: "/",
        display: "standalone",
        orientation: "portrait",
        theme_color: "#0369a1",
        background_color: "#ffffff",
        categories: ["medical", "health", "navigation"],
        icons: [
          {
            src: "icons/icon-192.png",
            sizes: "192x192",
            type: "image/png",
            purpose: "any",
          },
          {
            src: "icons/icon-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "any",
          },
          {
            src: "icons/maskable-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
    }),
  ],
  server: {
    host: "0.0.0.0",
    port: 5173,
  },
});
