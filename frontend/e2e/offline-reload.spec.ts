// Rechargement hors ligne via le VRAI service worker (#12, tâche 8.4).
//
// Ce fichier est DÉLIBÉRÉMENT séparé de `sync-convergence.spec.ts` (piège H1 de
// `.kiro/specs/dev-and-ci-tooling/design.md` §76-98) : on ne mélange JAMAIS
// « tester le SW » et « mocker l'API » dans le même test.
//
//  - `sync-convergence.spec.ts` BLOQUE le SW (`serviceWorkers: 'block'`) et
//    scripte l'API : seuls le mock de sync et IndexedDB déterminent le résultat.
//  - Ici, à l'inverse, on laisse le SW RÉEL s'enregistrer, s'activer et précacher
//    l'app shell, puis on vérifie que l'app se recharge HORS LIGNE depuis le
//    cache. Aucun mock d'API n'est installé (la carte tente de charger son
//    GeoJSON depuis l'origine API cross-origin ; ce chargement n'est PAS requis
//    pour le rechargement de l'app shell, qui est la seule chose précachée —
//    cf. `src/sw.ts` : `precacheAndRoute(self.__WB_MANIFEST)` + `NavigationRoute`
//    servant `index.html` avec `denylist: [/^\/api\//, …]`).
//
// Pourquoi la build de PROD (`vite preview` sur :4173, cf. `playwright.config.ts`
// et `src/pwa.ts`) : `virtual:pwa-register` n'enregistre le SW qu'en build de
// production ; en `vite dev` c'est un no-op. Le `webServer` de la config sert
// précisément cette build, donc le SW réel EST présent ici.
//
// Déterminisme (exigence 2.4) : toutes les attentes reposent sur des conditions
// OBSERVABLES — `navigator.serviceWorker.ready` (activation terminée), présence
// d'un `controller` (le SW contrôle bien la page), et visibilité DOM de l'app
// shell. AUCUNE temporisation arbitraire (`sleep`).

import { test, expect, type Page } from "@playwright/test";

// En-tête de l'app (i18n `app.title`, voir `src/i18n/fr.ts`) et onglet de
// navigation « Carte » (`nav.map`) : ce sont des morceaux de l'APP SHELL
// précaché, donc disponibles hors ligne une fois le SW actif.
const APP_TITLE = "Centres médicaux du Togo";
const NAV_CARTE = "Carte";

/**
 * Attend que le VRAI service worker soit enregistré, ACTIVÉ et qu'il CONTRÔLE la
 * page (condition observable, pas de `sleep`). On s'appuie sur :
 *   - `navigator.serviceWorker.ready` : résout quand un SW est activé pour ce
 *     scope (l'activation inclut le précache de l'app shell via
 *     `precacheAndRoute` dans `src/sw.ts`) ;
 *   - `navigator.serviceWorker.controller` : non nul quand le SW contrôle
 *     effectivement la page (prise de contrôle immédiate par `clientsClaim()` +
 *     `skipWaiting()` dans `src/sw.ts`). S'il n'est pas encore défini au premier
 *     chargement (page non contrôlée avant le claim), on attend l'événement
 *     `controllerchange`.
 *
 * Retourne quand le cache d'app shell est prêt à servir un rechargement hors
 * ligne.
 */
async function waitForActiveController(page: Page): Promise<void> {
  await page.evaluate(async () => {
    // L'API doit exister : la build de prod enregistre le SW (`pwa.ts`).
    if (!("serviceWorker" in navigator)) {
      throw new Error("serviceWorker indisponible : build de prod attendue (vite preview).");
    }
    // Activation terminée (précache de l'app shell inclus).
    await navigator.serviceWorker.ready;
    // Prise de contrôle de la page : nécessaire pour servir le rechargement
    // hors ligne depuis le cache. `clientsClaim()` la provoque, éventuellement
    // juste après `ready` ; on attend donc `controllerchange` si besoin.
    if (!navigator.serviceWorker.controller) {
      await new Promise<void>((resolve) => {
        navigator.serviceWorker.addEventListener(
          "controllerchange",
          () => resolve(),
          { once: true },
        );
      });
    }
  });
}

test.describe("Rechargement hors ligne via le vrai service worker (#12, tâche 8.4)", () => {
  test("l'app shell se recharge hors ligne depuis le cache du SW", async ({ page, context }) => {
    // 1) Première visite EN LIGNE : charge l'app, enregistre et active le SW.
    //    `networkidle` donne une base observable stable avant d'inspecter le SW.
    await page.goto("/", { waitUntil: "networkidle" });

    // L'app shell est rendu à la première visite (preuve que la build de prod
    // est servie et amorcée).
    await expect(page.getByText(APP_TITLE)).toBeVisible();
    await expect(page.getByRole("button", { name: NAV_CARTE })).toBeVisible();

    // 2) Attendre que le VRAI SW soit activé ET contrôle la page (précache de
    //    l'app shell terminé). Condition observable, aucun `sleep`.
    await waitForActiveController(page);

    // Garde-fou : le SW contrôle bien la page (sinon le rechargement hors ligne
    // ne serait pas servi depuis le cache).
    const controlled = await page.evaluate(
      () => navigator.serviceWorker.controller !== null,
    );
    expect(controlled).toBe(true);

    // 3) Passer HORS LIGNE (connectivité pilotée, pas de réseau réel) PUIS
    //    recharger. Le serveur `vite preview` n'est plus joignable ; seul le SW
    //    peut servir la navigation. `NavigationRoute` renvoie `index.html`
    //    précaché, et les JS/CSS de l'app shell proviennent du précache.
    await context.setOffline(true);
    await page.reload({ waitUntil: "commit" });

    // 4) Vérifier que l'APP SHELL s'affiche bien HORS LIGNE, servi par le cache
    //    du SW. On cible ce que le SW précache RÉELLEMENT (app shell), PAS le
    //    GeoJSON/tuiles de carte (chargés depuis l'origine API cross-origin, non
    //    précachés et donc indisponibles hors ligne lors d'un tout premier
    //    chargement). Attentes DOM observables, aucune temporisation arbitraire.
    await expect(page.getByText(APP_TITLE)).toBeVisible();
    await expect(page.getByRole("button", { name: NAV_CARTE })).toBeVisible();

    // Toujours hors ligne au moment des assertions : le rendu vient du cache,
    // pas du réseau (le serveur de preview est injoignable ici).
    const online = await page.evaluate(() => navigator.onLine);
    expect(online).toBe(false);

    // Repasser en ligne pour un nettoyage propre du contexte.
    await context.setOffline(false);
  });
});
