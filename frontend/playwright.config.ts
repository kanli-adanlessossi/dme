import { defineConfig, devices } from "@playwright/test";

// Configuration Playwright — pile E2E (#12, ADR-1 de
// `.kiro/specs/dev-and-ci-tooling/design.md`).
//
// Principes tranchés par le design :
//   - L'app est servie via la BUILD DE PROD (`vite build` → `vite preview`), pas
//     le dev server : c'est la seule façon reproductible d'exercer le service
//     worker et les chunks réels (ADR-1, exigence 2.5).
//   - `baseURL` = `vite preview` sur le port 4173 (cohérent avec la config
//     Lighthouse `.lighthouserc.json`, qui sert la même build sur 4173).
//   - Chromium D'ABORD : un seul projet navigateur pour commencer ; WebKit/
//     Firefox pourront être ajoutés plus tard sans changer l'architecture.
//
// Injection de `VITE_API_URL` (H1 — piège cross-origin vérifié dans le code).
//   `API_BASE_URL` par défaut = `http://localhost:8000` (`src/lib/api.ts`),
//   tandis que la build E2E est servie sur `http://localhost:4173`. Les appels de
//   sync sont donc CROSS-ORIGIN. On FIGE l'origine de l'API au moment du build
//   E2E via `VITE_API_URL` (défaut `http://localhost:8000`, surchargeable par
//   l'environnement) pour que les tests interceptent cette ORIGINE ABSOLUE
//   (`page.route('http://localhost:8000/api/**', …)`), jamais un chemin relatif
//   `/api/**` qui raterait les requêtes. La valeur est injectée dans la commande
//   `vite build` du `webServer` ci-dessous.

// Origine de l'API mock contre laquelle la build E2E est câblée. Fixée à
// `localhost:8000` par défaut (origine par défaut de `api.ts`) ; les specs
// E2E (tâche 8) intercepteront cette même origine absolue.
const E2E_API_ORIGIN = process.env.VITE_API_URL ?? "http://localhost:8000";

// Port de service de la build de prod (aligné sur `.lighthouserc.json`).
const PREVIEW_PORT = 4173;
const BASE_URL = `http://localhost:${PREVIEW_PORT}`;

export default defineConfig({
  // Les specs E2E vivent dans `frontend/e2e/` (séparées des tests Vitest de
  // `src/**`). Le dossier peut être vide pour l'instant (tâche 8 ajoute les specs).
  testDir: "./e2e",

  // Déterminisme (exigence 2.4) : aucune attente arbitraire, timeouts bornés.
  timeout: 30_000,
  expect: { timeout: 10_000 },

  // Interdit `test.only` resté en CI ; pas de nouvelle tentative silencieuse qui
  // masquerait un flaky (exigence 4.2 — un test instable est traité, pas masqué).
  //
  // Déterminisme éprouvé (tâche 8.3) : le scénario de convergence a passé 105
  // exécutions répétées sans un seul échec/flaky (détail et commandes dans
  // `e2e/README.md`). VERDICT : STABLE → chemin BLOQUANT des PR, pas de nightly.
  // On garde donc `retries: 0` : on ne masque pas un flaky, on s'appuie sur le
  // mock scripté + la connectivité pilotée pour le déterminisme (exigence 2.4).
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",

  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  // Démarre la build de PROD servie par `vite preview` avant les tests, et
  // l'arrête après. `VITE_API_URL` est injecté AU BUILD (H1) pour figer
  // l'origine cross-origin de l'API. `--strictPort` échoue vite si 4173 est pris
  // plutôt que de basculer sur un autre port (reproductibilité).
  webServer: {
    command: `npm run build && npx vite preview --port ${PREVIEW_PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      VITE_API_URL: E2E_API_ORIGIN,
    },
  },
});
