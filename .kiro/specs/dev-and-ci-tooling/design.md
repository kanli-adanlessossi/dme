# Design Document

_Spec : outillage de développement et d'intégration continue — `dev-and-ci-tooling`._

## Overview

Trois ajouts d'outillage, **additifs** (aucun code applicatif ni test existant
modifié) :

- **#5 — Commande `test-db`** dans `dme.sh`/`dme.bat` : provisionne un
  PostgreSQL+PostGIS **jetable et isolé**, exporte `TEST_DATABASE_URL`, lance la
  suite backend complète (les 56 tests d'intégration inclus), puis nettoie.
- **#12 — E2E Playwright multi-poste** : nouvelle pile de test (Playwright) + un
  scénario à deux contextes navigateur vérifiant la convergence local-first.
- **#13 — Garde-fou de perf** : budget de bundle (bloquant) + Lighthouse CI
  (informatif/nightly) sur l'app shell + carte, pour NFR-11.

Point transverse : la CI n'a **aucun job frontend** aujourd'hui. #12 et #13 ajoutent
un **job frontend distinct** (parallèle au job backend inchangé).

### État vérifié du code (ancrages)

- **Tests d'intégration** : `pytestmark = skipif(not TEST_DATABASE_URL)` dans les
  modules DB (`test_rls_isolation.py`, etc.). La DSN attendue est **propriétaire**
  async (`postgresql+asyncpg://<owner>:<pw>@host:port/db`) ; les modules
  s'auto-provisionnent (migrations en sous-processus, `CREATE ROLE dme_app`, seed).
  `test-db` n'a donc qu'à **fournir la base + la variable**, rien d'autre.
- **Scripts** : `dme.sh` (dispatch `case`), `dme.bat` (dispatch `goto :label` +
  `if errorlevel`). Les deux posent déjà le binaire compose et vérifient le démon
  Docker — réutilisable. Port DB de dev = **5432** (compose) ; on isole la base de
  test sur **5433** (comme `docs/E2E_VERIFICATION.md` §7) pour ne jamais toucher la
  pile de dev.
- **Frontend** : Vitest + jsdom ; Playwright absent ; MapLibre/WebGL stubés sous
  jsdom (donc #12 exige un vrai navigateur). `vite build` produit `dist/`.
- **CI** : `.github/workflows/ci.yml` = un seul job `backend`.

## Architecture

```
#5  dme.sh / dme.bat  test-db
      └─ docker run --rm -d  postgis/postgis:16-3.4  (port 5433, nom/volume dédiés)
         wait healthy → TEST_DATABASE_URL=postgresql+asyncpg://…:5433/dme_test
         (cd backend) pytest -q        # 56 tests d'intégration s'exécutent
         trap/finally → docker rm -f   # nettoyage inconditionnel

#12 frontend/e2e/*.spec.ts  (Playwright)
      ├─ deux BrowserContext isolés (poste A, poste B)
      ├─ API déterministe (voir ADR-1) ; connectivité pilotée (context.setOffline)
      └─ assertions : convergence sans perte/doublon ; conflit visite marqué

#13 CI job "frontend"
      ├─ vitest run           (déjà existant, pas encore en CI)
      ├─ vite build → size-limit (budget gzip, BLOQUANT)
      └─ lighthouse-ci         (perf/PWA, INFORMATIF / nightly)
```

## Decision Record (ADR-1) — API du test E2E : réelle vs simulée

**Contexte.** Le test de convergence (#12) a besoin d'un serveur de sync. Deux voies :
- (a) **Pile Docker complète réelle** (api + db + web) : fidèle, mais lente à
  démarrer, et la convergence réelle dépend d'horloges/timings → risque de flaky.
- (b) **API simulée déterministe** côté Playwright (route interception / mock
  serveur) : rapide, déterministe, pleinement contrôlable (offline/online, ordre
  des deltas), au prix de ne pas exercer le vrai backend.

**Décision : (b) API simulée déterministe pour le scénario de convergence.** La
convergence serveur réelle est **déjà** couverte par les tests d'intégration backend
(`test_sync_db.py`, lancés par #5). Ce que jsdom ne peut PAS couvrir et que #12 doit
vérifier, c'est le **comportement navigateur** : RxDB/IndexedDB réel, service worker,
bascule online/offline, écriture outbox, application des deltas, affichage de la
bannière de conflit. Une API simulée déterministe exerce tout cela sans introduire le
non-déterminisme d'une vraie pile. Un test E2E « full stack » réel pourra être ajouté
plus tard en **nightly** (hors chemin bloquant) si on veut la fidélité de bout en
bout ; il n'est pas requis pour fermer #12.

**Conséquence et pièges tranchés (H1).** Deux pièges « E2E + PWA » vérifiés dans le
code, à traiter explicitement sinon le test est non fonctionnel ou flaky :

1. **Origine cross-domain de l'API.** Vérifié : `API_BASE_URL` par défaut =
   `http://localhost:8000` (`src/lib/api.ts`), tandis que `vite preview` sert sur
   `http://localhost:4173`. Les appels de sync sont donc **cross-origin**. Décision :
   le test **injecte `VITE_API_URL`** vers une origine mock contrôlée **au moment du
   build E2E** (p. ex. `http://localhost:8000` servi par un faux serveur Playwright,
   OU une origine same-origin dédiée), et l'interception cible cette **origine
   absolue** (`page.route('http://<origine-api>/api/**', …)`), jamais un chemin
   relatif `/api/**` qui raterait les requêtes.
2. **Service worker actif.** Vérifié : le SW (`src/sw.ts`) précache l'app shell et a
   `denylist: [/^\/api\//]` sur la navigation (il n'intercepte donc pas `/api/`, ce
   qui est favorable). **Mais** un SW actif entre deux `BrowserContext` peut servir
   depuis son cache et introduire du non-déterminisme. Décision : **séparer les
   responsabilités en deux tests** —
   - `sync-convergence.spec.ts` : scénario de convergence multi-poste, SW
     **désactivé/contourné** (`serviceWorkers: 'block'` au niveau du contexte
     Playwright) pour que seul le mock d'API et IndexedDB déterminent le résultat ;
   - `offline-reload.spec.ts` (léger, optionnel mais recommandé) : exerce le **vrai
     SW** (SW activé, `vite preview`) pour vérifier que l'app se recharge hors ligne —
     sans mock d'API complexe.

   Mélanger « tester le SW » et « mocker l'API » dans le **même** test est la source
   classique de flaky ; on les sépare délibérément.

Le faux serveur scripte des séquences de push/pull contrôlées, dont un conflit de
note de visite, et la connectivité est pilotée par `context.setOffline(true|false)`.

## Decision Record (ADR-2) — Garde-fou de perf : bundle bloquant + Lighthouse informatif

**Contexte.** NFR-11 vise « carte < 3 s en 3G ». Deux leviers de garde-fou : taille
de bundle (proxy stable et déterministe de la perf de chargement) et Lighthouse
(mesure directe mais variable).

**Décision.**
- **Budget de bundle = BLOQUANT.** Un outil type `size-limit` mesure la taille gzip
  des artefacts critiques. Déterministe, rapide, sans variance : parfait garde-fou
  bloquant. Seuils calibrés sur l'état actuel + marge (p. ex. +10 %), documentés,
  échec si dépassé.
  - **Cibler la bonne métrique (M2 — fait vérifié).** `maplibre-gl` (4.7.1) est
    importé **statiquement** dans `TogoMap.tsx` et `CenterPinPicker.tsx` (aucun
    `import()` dynamique) → il est aujourd'hui **dans le bundle d'entrée**, pas
    code-split. Un budget unique sur « l'entrée » mélangerait donc app shell et carte,
    et masquerait quelle partie régresse. Décision : **deux budgets distincts** —
    (i) **app shell** (le JS/CSS nécessaire au premier rendu hors carte : c'est le
    chiffre le plus lié à NFR-11 « premier affichage »), (ii) **chunk carte**
    (MapLibre). Cela exige/encourage un **code-splitting de MapLibre** (import
    dynamique dans `TogoMap`/`CenterPinPicker`, chargé à la demande), ce qui sert
    *aussi* NFR-11 (la carte ne pèse plus sur le premier rendu). Si le code-splitting
    n'est pas fait dans cette spec, le budget documente explicitement que MapLibre est
    dans l'entrée et fixe un seuil global — mais la recommandation forte est de
    split, car un budget global peut rester vert tout en cachant une régression de
    perte de carte.
- **Lighthouse CI = INFORMATIF (nightly ou PR non bloquant).** Lighthouse mesure la
  vraie métrique NFR-11 mais varie selon la machine CI → le rendre bloquant
  produirait des faux échecs. On l'exécute avec throttling mobile fixe, on **publie**
  le rapport (artefact/commentaire) sans faire échouer la PR, et on fixe des seuils
  tolérants comme alerte, pas comme barrière.

**Justification.** Un garde-fou bloquant doit être déterministe (budget de bundle) ;
une mesure variable (Lighthouse) informe sans bloquer. Les deux ensemble couvrent
NFR-11 : le bundle empêche la dérive de taille (cause #1 de lenteur au premier
rendu), Lighthouse surveille la métrique réelle.

## Components and Interfaces

### C1 — Commande `test-db` (`dme.sh` + `dme.bat`)

Nouvelle branche dans le dispatch existant. Comportement identique sur les deux
plateformes :
1. vérifier Docker (réutilise les contrôles existants) ;
2. démarrer `postgis/postgis:16-3.4` en conteneur **jetable** (`--rm`), nom dédié
   (`dme_test_pg`), port **5433**, creds de test éphémères, et attendre `healthy`.
   **Robustesse (M3)** : avant de démarrer, (a) `docker rm -f dme_test_pg` silencieux
   (conteneur homonyme d'un run interrompu), et (b) **détecter si le port 5433 est
   déjà occupé** (par un autre conteneur/service) → message clair (« port 5433 déjà
   utilisé ; libérez-le ou ajustez `DME_TEST_DB_PORT` ») et code non nul, plutôt
   qu'une erreur Docker cryptique. Le port est **surchargeable** via une variable
   d'environnement (`DME_TEST_DB_PORT`, défaut 5433) pour éviter la friction si 5433
   est pris ;
3. exporter `TEST_DATABASE_URL=postgresql+asyncpg://dme:dme@localhost:5433/dme` ;
4. `cd backend && pytest -q` (les modules d'intégration détectent la variable et
   s'auto-provisionnent) ;
5. **nettoyage inconditionnel** (`trap EXIT` côté bash ; bloc de fin + `docker rm -f`
   côté bat) : le conteneur est supprimé même si les tests échouent ;
6. **propager le code de sortie de pytest** (non nul si échec) pour l'usage
   pré-commit/CI locale.

Isolation : nom/port/volume distincts de la pile de dev (compose, 5432) → `test-db`
et `start` coexistent sans collision.

### C2 — Pile E2E Playwright (`frontend/`)

- `@playwright/test` en devDependency ; `playwright.config.ts` (projet unique,
  navigateur Chromium d'abord ; baseURL = `vite preview`).
- Script `test:e2e` dans `package.json`.
- `frontend/e2e/sync-convergence.spec.ts` : **deux `BrowserContext`** (poste A,
  poste B), API interceptée par un faux serveur scripté sur l'**origine API absolue**
  (voir H1), connectivité pilotée via `context.setOffline(true|false)`, SW bloqué
  (`serviceWorkers: 'block'`).
  - **Pourquoi deux `BrowserContext` et pas deux `page` (M1).** RxDB/IndexedDB est
    cloisonné par **origine + contexte** ; la base locale a un **nom fixe**
    (`dme_local`, `db.ts`). Deux `page` du **même** contexte partageraient donc la
    même base IndexedDB → les deux « postes » écriraient dans le même stockage (faux).
    Seuls deux `BrowserContext` distincts donnent deux bases locales isolées, fidèles
    à deux postes. Ne PAS « simplifier » en deux pages.
- `frontend/e2e/offline-reload.spec.ts` (recommandé) : SW réel activé, vérifie le
  rechargement hors ligne (voir H1, séparation des responsabilités).

### C3 — Job CI `frontend` + budget de bundle

- `.github/workflows/ci.yml` : nouveau job `frontend` (parallèle à `backend`) :
  install → `vitest run` → `vite build` → `size-limit` (bloquant) → `test:e2e`
  (déterministe, bloquant si stable, sinon nightly) ; Lighthouse en job/étape
  **informatif**.
- `size-limit` configuré (`.size-limit.json` ou champ `package.json`) avec les
  deux budgets (app shell, chunk carte) et leurs seuils.

### C4 — Versions épinglées et coût CI (L1, L2)

- **Versions épinglées (L1).** `@playwright/test`, `size-limit` et Lighthouse CI
  sont ajoutés avec des versions **épinglées** (cohérent avec la discipline du
  projet : `slowapi==0.1.10`, `maplibre-gl 4.7.1`), pour qu'une montée majeure ne
  casse pas le job silencieusement.
- **Cache des navigateurs Playwright (L2 — obligatoire, pas optionnel).**
  `npx playwright install` télécharge des navigateurs (~100+ Mo). Le job CI DOIT
  **mettre en cache** le dossier des navigateurs Playwright (clé = version
  Playwright) pour ne pas re-télécharger à chaque run — faute de quoi chaque exécution
  paie le coût de téléchargement. Mitigation nommée dès le design, condition de la
  proportionnalité exigée (exigence 4.3).

## Data Models

Aucun modèle de données. Nouveaux artefacts de configuration uniquement :
`playwright.config.ts`, `frontend/e2e/*.spec.ts`, config `size-limit`, config
Lighthouse CI (`.lighthouserc.*`), et les branches `test-db` des scripts.

## Correctness Properties

### Property 1: Intégration locale en une commande, isolée et nettoyée (#5)

`test-db` provisionne un PostGIS jetable (port 5433, isolé de la pile de dev),
exécute la suite complète et supprime le conteneur **inconditionnellement** (même en
cas d'échec), en propageant un code de sortie non nul si un test échoue.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

### Property 2: Convergence multi-poste vérifiée end-to-end (#12)

Le test E2E exerce deux postes hors ligne puis leur reconnexion dans de vrais
navigateurs et vérifie la convergence sans perte ni doublon, le conflit de note de
visite étant marqué pour revue (non écrasé).

**Validates: Requirements 2.2, 2.3**

### Property 3: Déterminisme des vérifications ajoutées (#12, #13)

L'E2E ne dépend d'aucune temporisation arbitraire ni réseau externe (connectivité
pilotée, API scriptée) ; le garde-fou bloquant de perf est déterministe (budget de
bundle), Lighthouse (variable) restant informatif.

**Validates: Requirements 2.4, 3.3, 3.4, 4.2**

### Property 4: Garde-fou de perf couvrant NFR-11 (#13)

Un budget de bundle gzip bloquant sur l'app shell + la carte échoue si la taille
dépasse le seuil documenté ; Lighthouse publie la métrique de premier rendu.

**Validates: Requirements 3.1, 3.2, 3.5**

### Property 5: CI non régressive et proportionnée (#5, #12, #13)

Le job backend existant reste inchangé et parallèle ; les suites existantes restent
vertes ; les ajouts n'introduisent pas de « flaky » sur le chemin bloquant.

**Validates: Requirements 4.1, 4.3, 4.4**

## Error Handling

- **Docker absent / démon arrêté** (`test-db`) : réutiliser les contrôles existants
  des scripts (message clair, code non nul) — ne pas laisser un conteneur orphelin.
- **Conteneur de test orphelin** : avant de démarrer, `docker rm -f dme_test_pg`
  silencieux (idempotence si un run précédent a été interrompu) ; nettoyage final
  inconditionnel.
- **Échec des tests** : le conteneur est quand même supprimé ; le code de sortie de
  pytest est propagé (pas masqué par le `docker rm`).
- **E2E flaky** : si un scénario s'avère instable malgré le pilotage déterministe, le
  **déplacer en nightly** plutôt que de le laisser bloquer les PR (décision explicite
  du design, exigence 4.2).
- **Variance Lighthouse** : seuils tolérants + exécution informative ; jamais de
  blocage de PR sur une métrique variable.

## Testing Strategy

1. **`test-db`** : exécution manuelle sur les deux plateformes → les 56 tests
   d'intégration passent ; le conteneur est supprimé après (succès ET échec
   simulé) ; code de sortie correct.
2. **E2E convergence** : le scénario deux-postes passe de façon répétée (lancer N
   fois pour éprouver le déterminisme) ; le conflit de note de visite affiche bien la
   bannière.
3. **Budget de bundle** : `size-limit` passe sur l'état actuel ; une augmentation
   artificielle de taille le fait échouer (vérifié puis rétabli).
4. **CI** : le job frontend s'exécute en parallèle du backend ; le temps ajouté est
   mesuré et documenté ; le job backend est inchangé.
5. **Non-régression** : backend (unitaire + intégration via #5) et frontend (Vitest)
   restent verts.

## Hors périmètre (rappel)

- Optimisation effective des performances (le budget mesure et garde ; il
  n'optimise pas).
- Couverture E2E générale de toutes les pages (seul le scénario de convergence est
  visé).
- Test E2E « full stack » contre la vraie pile Docker : possible en nightly futur,
  non requis pour #12.
