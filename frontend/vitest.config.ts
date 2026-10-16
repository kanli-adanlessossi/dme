import { defineConfig } from "vitest/config";

// Configuration Vitest — tâche 14.6 (tests de la fondation local-first).
//
// Volontairement séparée de `vite.config.ts` : la configuration de build charge
// le plugin PWA (service worker, manifeste) qui est inutile — et bruyant — en
// test. Ici on ne veut qu'un environnement de type navigateur fournissant
// `window`, `localStorage`, `navigator.onLine` et `crypto.subtle` (jsdom + le
// webcrypto natif de Node 18+), plus IndexedDB via `fake-indexeddb` injecté
// dans le fichier de setup.
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    // Injecte IndexedDB (fake-indexeddb) et stub le réseau avant chaque suite.
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.ts"],
    // RxDB + Dexie + fake-indexeddb sont plus déterministes dans un seul
    // processus par fichier ; on isole chaque fichier de test.
    isolate: true,
    // Les tests PBT (fast-check) peuvent être un peu plus longs que la valeur
    // par défaut sous RxDB ; marge raisonnable.
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
  // `import.meta.env` est consommé par les modules sous test (api.ts, db.ts).
  define: {
    "import.meta.env.PROD": "false",
  },
});
