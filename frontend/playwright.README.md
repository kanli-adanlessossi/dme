# Pile E2E Playwright — #12 / NFR-6

Pile de test **end-to-end** (vrai navigateur) qui couvre ce que Vitest/jsdom ne
peut pas : RxDB/IndexedDB réel, service worker, bascule online/offline, affichage
de la bannière de conflit (ADR-1 de `.kiro/specs/dev-and-ci-tooling/design.md`).

- Config : [`playwright.config.ts`](./playwright.config.ts)
- Specs : [`e2e/`](./e2e/) (scénarios ajoutés par la tâche 8)
- Version **épinglée** (L1) : `@playwright/test` `1.64.1` dans
  [`package.json`](./package.json) (cohérent avec la discipline du projet —
  `maplibre-gl 4.7.1`, `size-limit 11.2.0`), pour qu'une montée majeure ne casse
  pas le job silencieusement.

## Installer les navigateurs

`@playwright/test` installe le **runner** via `npm`, mais PAS les navigateurs
(binaires téléchargés séparément, ~100+ Mo). Après `npm install`, exécuter **une
fois** (et à chaque montée de version Playwright) :

```bash
# Local — Chromium seul (le projet n'active que Chromium pour l'instant) :
npx playwright install chromium

# CI / conteneur Linux — ajoute aussi les dépendances système du navigateur :
npx playwright install --with-deps chromium
```

Playwright stocke les navigateurs dans un cache utilisateur :

| OS | Emplacement par défaut |
|---|---|
| Linux | `~/.cache/ms-playwright` |
| macOS | `~/Library/Caches/ms-playwright` |
| Windows | `%USERPROFILE%\AppData\Local\ms-playwright` |

Surchargeable via la variable d'environnement `PLAYWRIGHT_BROWSERS_PATH`.

## Exécuter les tests

```bash
npm run test:e2e          # == playwright test
```

Aucun serveur à démarrer à la main : le bloc `webServer` de la config lance
`vite build` puis `vite preview --port 4173 --strictPort` et attend que
`http://localhost:4173` réponde. `VITE_API_URL` est **injecté au build** (défaut
`http://localhost:8000`) pour figer l'origine cross-origin de l'API (H1) ; les
specs intercepteront cette origine absolue.

## Cache des navigateurs Playwright en CI (L2 — OBLIGATOIRE)

`npx playwright install` télécharge ~100+ Mo à chaque exécution si rien n'est mis
en cache. Le design (C4, L2) rend ce cache **obligatoire**, condition de la
proportionnalité du coût CI (exigence 4.3). Le câblage CI effectif des E2E est la
**tâche 8.5** ; la stratégie de cache est fixée ici pour y être appliquée telle
quelle.

**Clé de cache = version de Playwright** (pour re-télécharger uniquement à une
montée de version, jamais à chaque run). Esquisse pour le job `frontend` de
`.github/workflows/ci.yml` :

```yaml
# (tâche 8.5) — après `npm ci`, avant `npm run test:e2e`
- name: Résoudre la version de Playwright
  id: pw
  run: echo "version=$(node -p "require('@playwright/test/package.json').version")" >> "$GITHUB_OUTPUT"
  working-directory: frontend

- name: Cache des navigateurs Playwright
  id: pw-cache
  uses: actions/cache@v4
  with:
    path: ~/.cache/ms-playwright
    # Clé = version Playwright : invalidée uniquement à une montée de version.
    key: ${{ runner.os }}-playwright-${{ steps.pw.outputs.version }}

- name: Installer les navigateurs Playwright (si cache absent)
  if: steps.pw-cache.outputs.cache-hit != 'true'
  run: npx playwright install --with-deps chromium
  working-directory: frontend

# Les dépendances système ne sont pas couvertes par le cache du dossier
# navigateurs ; si le cache est touché, réexécuter `install-deps` seul :
- name: Dépendances système du navigateur (sur cache hit)
  if: steps.pw-cache.outputs.cache-hit == 'true'
  run: npx playwright install-deps chromium
  working-directory: frontend
```

Dériver la clé de la version évite à la fois le re-téléchargement inutile (cache
hit tant que la version ne bouge pas) et l'usage d'un binaire périmé (cache miss
forcé à la montée de version, cohérent avec l'épinglage L1).

## Placement CI (bloquant vs nightly)

Décision déléguée à la tâche 8.3/8.5 selon la **stabilité observée** du scénario :
un E2E stable est bloquant sur PR ; un E2E instable est déplacé en **nightly**
plutôt que de bloquer les PR (exigence 4.2, anti-flaky).
