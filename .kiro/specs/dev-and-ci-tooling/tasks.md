# Implementation Plan

## Overview

Objet : outillage de développement et d'intégration continue — observations #5
(script `test-db`), #12 (E2E Playwright multi-poste) et #13 (garde-fou de perf) de
la revue d'architecture.

Plan conçu selon `design.md`, satisfaisant `requirements.md`. Trois axes
**indépendants** et additifs (aucun code applicatif ni test existant modifié) :

- **#5** — commande `test-db` dans `dme.sh`/`dme.bat` (provisionne un PostGIS
  jetable isolé sur le port 5433, lance la suite complète, nettoie).
- **#12** — pile Playwright + scénario de convergence multi-poste (ADR-1 : API
  simulée déterministe, app servie via la build de prod).
- **#13** — budget de bundle **bloquant** (`size-limit`) + Lighthouse **informatif**
  (ADR-2).

Point transverse : la CI gagne un **job frontend distinct** (parallèle au job
backend inchangé), introduit par la tâche 4 et étendu par #12/#13.

Principes d'ordonnancement :
1. **#5 en premier** : petit, à faible risque, et débloque l'exécution locale de
   l'intégration pour tout le reste du travail.
2. **Job frontend CI (tâche 4) avant #12/#13** : il n'existe aucun job frontend ;
   on le crée (vitest + build) puis on y greffe le budget de bundle (#13) et l'E2E
   (#12).
3. **E2E en dernier** : le plus gros et le plus à risque de non-déterminisme ;
   livré avec sa stratégie anti-flaky (nightly si instable).

Chaque tâche indique sa Definition of Done (DoD).

## Tasks

- [x] 1. [#5] Commande `test-db` (bash)
  - [x] 1.1 Ajouter une branche `test-db` au dispatch `case` de `dme.sh` : vérifier
    Docker (réutiliser les contrôles existants) ; `docker rm -f dme_test_pg` silencieux
    (idempotence) ; **détecter si le port est déjà occupé** → message clair + code non
    nul (M3) ; port **surchargeable** via `DME_TEST_DB_PORT` (défaut 5433) ; démarrer
    `postgis/postgis:16-3.4` en `--rm`, nom `dme_test_pg`, creds de test éphémères ;
    attendre `healthy` ; exporter
    `TEST_DATABASE_URL=postgresql+asyncpg://dme:dme@localhost:<port>/dme` ; `cd backend
    && pytest -q`. Nettoyage **inconditionnel** via `trap EXIT` (le conteneur est
    supprimé même en cas d'échec), en **propageant le code de sortie de pytest**.
  - DoD : `./dme.sh test-db` exécute les 56 tests d'intégration ; conteneur supprimé
    après succès ET échec ; code de sortie non nul si un test échoue ; n'interfère pas
    avec `start` (port/nom distincts).
  - _Requirements: 1 (critères 1, 2, 3) ; NFR-1, NFR-12_

- [x] 2. [#5] Commande `test-db` (Windows)
  - [x] 2.1 Ajouter la branche `test-db` au dispatch `goto` de `dme.bat`, comportement
    équivalent à 1.1 : contrôles Docker existants ; suppression idempotente préalable ;
    conteneur `postgis` jetable sur 5433 ; attente `healthy` ; `set
    TEST_DATABASE_URL=…` ; `pytest` depuis `backend` ; bloc de fin avec `docker rm -f`
    inconditionnel et propagation du code d'erreur (`exit /b`).
  - DoD : `dme test-db` sous Windows a le même comportement observable que la version
    bash (tests exécutés, conteneur nettoyé, code de sortie correct).
  - _Requirements: 1 (critères 1, 2, 3, 4) ; NFR-12_

- [x] 3. [#5] Documentation de `test-db`
  - [x] 3.1 Référencer `test-db` comme la façon recommandée de lancer l'intégration en
    local dans `docs/E2E_VERIFICATION.md` §7 et dans l'aide des scripts (liste des
    actions valides) + une mention au `README`.
  - DoD : la commande figure dans la doc et dans l'usage des scripts ; la procédure
    manuelle `docker run` reste mentionnée comme équivalent bas niveau.
  - _Requirements: 1 (critère 5) ; NFR-12_

- [x] 4. [#13] Job CI frontend + exécution de Vitest
  - [x] 4.1 Ajouter un job `frontend` à `.github/workflows/ci.yml`, **parallèle** au
    job `backend` (inchangé) : checkout, setup Node, `npm ci` (dans `frontend/`),
    `npm run test` (Vitest), `npm run build`. Ce job apporte enfin la couverture
    frontend en CI (aujourd'hui absente).
  - DoD : le job frontend s'exécute en parallèle du backend ; vitest et build verts ;
    job backend inchangé.
  - _Requirements: 4 (critères 1, 4) ; NFR-12_

- [x] 5. [#13] Budget de bundle bloquant (`size-limit`)
  - [x] 5.1 Ajouter `size-limit` (devDependency, **version épinglée**) + config
    (`.size-limit.json` ou champ `package.json`) avec **deux budgets distincts**
    (M2) : (i) app shell (premier rendu hors carte, métrique la plus liée à NFR-11),
    (ii) chunk carte MapLibre. Seuils gzip **calibrés sur l'état actuel + marge
    (~10 %)** et documentés. **Préalable recommandé** : code-splitter MapLibre
    (import dynamique dans `TogoMap.tsx`/`CenterPinPicker.tsx` — aujourd'hui importé
    statiquement, donc dans le bundle d'entrée) pour que la carte ne pèse pas sur le
    premier rendu et que les deux budgets soient séparables. Si le split n'est pas
    fait, documenter explicitement que MapLibre est dans l'entrée et fixer un seuil
    global.
  - [x] 5.2 Ajouter une étape `size-limit` au job frontend, **bloquante** (échec si un
    budget est dépassé).
  - DoD : `size-limit` passe sur l'état actuel ; une augmentation artificielle de
    taille le fait échouer (vérifié puis rétabli) ; seuils documentés.
  - _Requirements: 3 (critères 1, 2, 3, 5) ; NFR-11_

- [x] 6. [#13] Lighthouse CI informatif
  - [x] 6.1 Ajouter Lighthouse CI (`.lighthouserc.*`) exécuté avec throttling mobile
    fixe sur la build de prod servie (`vite preview`), ciblant l'accueil (app shell +
    carte). **Informatif** : publie le rapport (artefact/commentaire), **ne bloque
    PAS** la PR ; seuils tolérants à la variance. Exécution en étape dédiée ou job
    nightly selon le coût mesuré.
  - DoD : le rapport Lighthouse est produit et publié ; aucune PR n'échoue sur la
    variance de la métrique ; throttling mobile fixe appliqué.
  - _Requirements: 3 (critères 3, 4) ; NFR-11_

- [x] 7. [#12] Pile Playwright
  - [x] 7.1 Ajouter `@playwright/test` (devDependency, **version épinglée**, L1) +
    `playwright.config.ts` (Chromium d'abord ; `baseURL` = `vite preview` sur `dist/`,
    port 4173) + script `test:e2e` dans `frontend/package.json`. Injecter `VITE_API_URL`
    vers l'origine API mock au build E2E (H1 : l'API est cross-origin par défaut,
    `localhost:8000`). Documenter `npx playwright install` et prévoir le **cache des
    navigateurs Playwright** en CI (L2, obligatoire, clé = version Playwright).
  - DoD : `npm run test:e2e` démarre Playwright contre la build de prod servie ; config
    minimale fonctionnelle ; version épinglée ; cache navigateurs prévu pour la CI.
  - _Requirements: 2 (critère 1) ; NFR-6_

- [x] 8. [#12] Scénario E2E de convergence multi-poste
  - [x] 8.1 Créer `frontend/e2e/sync-convergence.spec.ts` : **deux `BrowserContext`**
    isolés (poste A, poste B) — PAS deux `page` d'un même contexte (M1 : la base RxDB
    a un nom fixe `dme_local` et serait partagée). API interceptée sur l'**origine
    absolue** de l'API (H1, `page.route('http://<origine-api>/api/**', …)`), scriptée
    et déterministe ; **SW bloqué** (`serviceWorkers: 'block'`) pour isoler le
    comportement du cache ; connectivité pilotée via `context.setOffline(true|false)`.
  - [x] 8.2 Scénario : travail hors ligne des deux côtés (créer/éditer patients et
    visites, dont une **édition concurrente de la même note de visite**) ; reconnexion ;
    vérifier la **convergence sans perte ni doublon** (idempotence `mutation_id`) et que
    le **conflit de note de visite est marqué pour revue** (bannière de conflit), non
    écrasé. Attentes basées sur des conditions observables (pas de `sleep`).
  - [x] 8.3 Éprouver le **déterminisme** : lancer le scénario plusieurs fois ; s'il est
    instable, le déplacer en **nightly** (hors chemin bloquant) plutôt que de le laisser
    bloquer les PR (exigence 4.2).
  - [x] 8.4 (Recommandé) Créer `frontend/e2e/offline-reload.spec.ts` : SW **réel
    activé** (pas de `serviceWorkers: 'block'`), vérifier que l'app se recharge hors
    ligne (app shell + carte servis depuis le cache). Séparé de 8.1 (H1 : ne jamais
    mélanger « tester le SW » et « mocker l'API » dans le même test).
  - [x] 8.5 Greffer les E2E au job frontend (bloquant si stable, nightly sinon).
  - DoD : scénario deux-postes vert et reproductible ; bannière de conflit vérifiée ;
    aucune dépendance à une temporisation arbitraire ; placement CI (bloquant/nightly)
    décidé selon la stabilité observée.
  - _Requirements: 2 (critères 2, 3, 4, 5), 4 (critère 2) ; NFR-6_

- [x] 9. [#5, #12, #13] Non-régression et coût CI
  - [x] 9.1 Vérifier que le job backend est inchangé et parallèle ; mesurer et
    documenter le temps de CI ajouté par le job frontend (+ stratégie pour le contenir :
    parallélisme, cache npm, nightly). Confirmer que les suites existantes (backend
    unitaire + intégration, frontend Vitest) restent vertes.
  - DoD : CI verte de bout en bout ; coût ajouté documenté ; aucun job existant
    régressé ni ralenti.
  - _Requirements: 4 (tous) ; NFR-1, NFR-11, NFR-12_

## Task Dependency Graph

```
#5 :  1 (test-db bash) ─┐
      2 (test-db bat)  ─┼─► 3 (doc test-db)
#13:  4 (job CI frontend) ─► 5 (budget bundle, bloquant)
                          └─► 6 (Lighthouse, informatif)
#12:  7 (pile Playwright) ─► 8 (scénario convergence) ──┐
                                                        │
      3, 5, 6, 8 ──────────────────────────────────────┴─► 9 (non-régression + coût CI)
```

Les trois axes (#5 : 1–3 ; #13 : 4–6 ; #12 : 7–8) sont **indépendants** et
parallélisables. #12 et #13 partagent le job frontend créé en tâche 4 (donc 4 avant
5, 6 et le branchement CI de 8). Chemin critique : **4 → 8 → 9** (l'E2E est le plus
long).

```json
{
  "tasks": [
    { "id": "1", "obs": "#5",            "dependsOn": [],         "wave": 1, "severity": "low" },
    { "id": "2", "obs": "#5",            "dependsOn": [],         "wave": 1, "severity": "low" },
    { "id": "4", "obs": "#13",           "dependsOn": [],         "wave": 1, "severity": "medium" },
    { "id": "7", "obs": "#12",           "dependsOn": [],         "wave": 1, "severity": "medium" },
    { "id": "3", "obs": "#5",            "dependsOn": ["1", "2"], "wave": 2, "severity": "low" },
    { "id": "5", "obs": "#13",           "dependsOn": ["4"],      "wave": 2, "severity": "medium" },
    { "id": "6", "obs": "#13",           "dependsOn": ["4"],      "wave": 2, "severity": "low" },
    { "id": "8", "obs": "#12",           "dependsOn": ["4", "7"], "wave": 2, "severity": "high" },
    { "id": "9", "obs": "#5, #12, #13",  "dependsOn": ["3", "5", "6", "8"], "wave": 3, "severity": "medium" }
  ],
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "4", "7"] },
    { "wave": 2, "tasks": ["3", "5", "6", "8"] },
    { "wave": 3, "tasks": ["9"] }
  ],
  "criticalPath": ["4", "8", "9"]
}
```

## Notes

- ADR-1 (API simulée déterministe pour l'E2E) et ADR-2 (budget de bundle bloquant +
  Lighthouse informatif) sont **tranchés** dans `design.md` ; les tâches en appliquent
  le choix.
- Contrainte anti-flaky (exigence 4.2) : un E2E instable est **déplacé en nightly**,
  jamais laissé bloquer les PR (tâche 8.3).
- Le job backend de `ci.yml` reste **inchangé** ; le job frontend est additif et
  parallèle.
- La convergence serveur réelle reste couverte par les tests d'intégration backend
  (lancés par `test-db`/CI) ; l'E2E (#12) couvre le comportement **navigateur** que
  jsdom ne peut pas exercer.
