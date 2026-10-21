# Tests E2E Playwright (#12)

Ce dossier contient les tests end-to-end exécutés dans un vrai navigateur
(Playwright), séparés des tests Vitest/jsdom de `src/**`.

La pile Playwright est configurée par [`../playwright.config.ts`](../playwright.config.ts) :
l'app est servie via la **build de prod** (`vite build` → `vite preview` sur le
port 4173) et l'origine de l'API est figée au build via `VITE_API_URL`
(cross-origin par défaut, `http://localhost:8000`). Voir ADR-1 de
`.kiro/specs/dev-and-ci-tooling/design.md`.

## Exécuter

```bash
# Une seule fois (ou après une montée de version Playwright) : télécharge le
# navigateur Chromium utilisé par les tests.
npx playwright install --with-deps chromium

# Lance la suite E2E (build + preview + tests). Aucun serveur à démarrer à la
# main : le `webServer` de la config s'en charge.
npm run test:e2e
```

## Scénarios

- `sync-convergence.spec.ts` — deux `BrowserContext` isolés (deux postes),
  API mockée sur l'origine absolue, SW bloqué, connectivité pilotée
  (`context.setOffline`).
  - **Tâche 8.1 (fait)** : harnais réutilisable — `MockSyncServer` déterministe
    (push/pull idempotent par `mutation_id`, résolution LWW, conflit sensible de
    note de visite), helpers `createWorkstation`/`routeApi`, bascules
    online/offline, et un smoke test prouvant que les deux postes isolés
    chargent l'app.
  - **Tâche 8.2 (fait)** : scénario complet de convergence (travail hors ligne
    des deux côtés, reconnexion, convergence sans perte ni doublon, et bannière
    de conflit de note de visite), bâti sur ce harnais.
- `offline-reload.spec.ts` — SW **réel** activé (PAS de `serviceWorkers: 'block'`),
  vérifie le rechargement **hors ligne**. Séparé de `sync-convergence.spec.ts`
  (piège H1 : on ne mélange jamais « tester le SW » et « mocker l'API »).
  - **Tâche 8.4 (fait)** : première visite en ligne sur la build de prod
    (`vite preview`, où `virtual:pwa-register` enregistre le SW) ; attente du SW
    **activé ET contrôlant la page** via des conditions observables
    (`navigator.serviceWorker.ready` + `controller`/`controllerchange`, aucun
    `sleep`, exigence 2.4) ; passage hors ligne (`context.setOffline(true)`) puis
    `reload` ; assertion que l'**app shell** précaché se recharge hors ligne
    (en-tête « Centres médicaux du Togo » + onglet « Carte », servis depuis le
    cache du SW — `precacheAndRoute(self.__WB_MANIFEST)` + `NavigationRoute`
    renvoyant `index.html`). Fidèle au précache : on n'affirme PAS le
    rechargement des tuiles/GeoJSON de carte (chargés depuis l'origine API
    cross-origin, non précachés).

## Déterminisme & placement CI (tâche 8.3)

**Verdict : STABLE → chemin BLOQUANT (PR).** Le scénario de convergence (8.1/8.2)
a été éprouvé en répétition et s'est révélé **déterministe** : son résultat ne
dépend que du mock de sync scripté et de la connectivité pilotée
(`context.setOffline`), jamais d'une temporisation arbitraire ni d'un réseau
réel (exigence 2.4). Il **reste donc sur le chemin bloquant** des PR ; il n'est
PAS déplacé en nightly (le repli nightly de l'exigence 4.2 n'est déclenché que si
un E2E s'avère instable, ce qui n'est pas le cas ici).

Probe de déterminisme exécuté depuis `frontend/` (Chromium, Playwright 1.64.1) :

| Commande                                                        | Exécutions | Résultat       |
| --------------------------------------------------------------- | ---------- | -------------- |
| `npm run test:e2e` (baseline)                                   | 5          | 5 passed       |
| `npx playwright test --repeat-each=10`                          | 50         | 50 passed      |
| `npx playwright test --grep "convergent" --repeat-each=25 --workers=1` | 25  | 25 passed      |
| `npx playwright test` × 5 (runs indépendants, boot serveur frais) | 25       | 25 passed      |
| **Total**                                                       | **105**    | **0 échec, 0 flaky** |

> Note pratique : passer les options Playwright **directement** à `npx playwright
> test …` (p. ex. `--repeat-each`). Via `npm run test:e2e -- …`, npm n'a PAS
> transmis la partie `--repeat-each` observée ici ; préférer `npx playwright test`
> pour les runs paramétrés.

**Conséquence pour la tâche 8.5 (branchement CI).** Les E2E sont **bloquants** :
le job `frontend` de `.github/workflows/ci.yml` les exécute en étape normale
(code de sortie non nul = échec de job), SANS `continue-on-error` et SANS
`retries` (`playwright.config.ts` fixe `retries: 0` — on ne masque pas un flaky,
on garde le déterminisme). Pas de split nightly ni de tag `@nightly` à prévoir
pour ce scénario. Prérequis CI déjà cadrés par la tâche 7 : cache des navigateurs
Playwright (clé = version Playwright) + `npx playwright install --with-deps
chromium`.

## Typecheck du dossier `e2e/`

Le dossier `e2e/` est exclu du build applicatif (`tsconfig.app.json` ne couvre
que `src`). Son propre `e2e/tsconfig.json` (strict) permet de le typechecker
indépendamment :

```bash
npx tsc --noEmit -p e2e/tsconfig.json
```
