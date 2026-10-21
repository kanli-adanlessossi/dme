# Requirements Document

_Spec : outillage de développement et d'intégration continue — `dev-and-ci-tooling`._

## Introduction

Cette spec regroupe trois observations de la revue d'architecture visant à
**automatiser les vérifications aujourd'hui manuelles** et à combler les angles
morts de la CI :

- **#5** — *Script `test-db` pour exécuter la suite d'intégration en local.* Les 56
  tests d'intégration (RLS réelle, sync, audit) ne tournent qu'en CI ou via une
  procédure manuelle `docker run postgis` + `TEST_DATABASE_URL=…`. Un dev local qui
  lance `pytest` ne voit PAS s'exécuter la couche la plus critique.
- **#12** — *Test E2E Playwright multi-poste de la convergence de sync.* Le scénario
  « deux postes hors ligne → reconnexion → convergence sans perte ni doublon,
  conflits sensibles marqués » est le cœur de la valeur local-first, mais reste
  **manuel** (rapport E2E §6). Aucun test E2E navigateur n'existe.
- **#13** — *Budget de bundle / Lighthouse CI pour NFR-11.* La cible « premier rendu
  de carte < 3 s en 3G » et la dégradation WebGL sont vérifiées à la main ; aucun
  garde-fou automatisé n'empêche une régression de performance ou de taille de
  bundle.

### État vérifié du code (base de cette spec)

1. **CI (#5, #12, #13).** `.github/workflows/ci.yml` a **un seul job** (`backend` :
   ruff + `alembic upgrade head` + `pytest` contre un service `postgis/postgis:16-3.4`,
   via `TEST_DATABASE_URL`). **Aucun job frontend** (ni vitest, ni build, ni E2E, ni
   perf) n'existe. #12 et #13 impliquent donc d'**ajouter un job frontend**.
2. **Scripts de cycle de vie (#5).** `dme.sh`/`dme.bat` n'exposent que start/stop/
   restart/status/logs/build/down (pile Docker). **Aucune** commande ne lance la
   suite d'intégration backend ; le dev doit reproduire à la main le `docker run`
   PostGIS + `TEST_DATABASE_URL` documenté dans `docs/E2E_VERIFICATION.md` §7.
3. **Frontend (#12).** `frontend/package.json` : tests via **Vitest + jsdom**
   uniquement (`fake-indexeddb`, `fast-check`). **Playwright n'est pas installé** ;
   pas de script `test:e2e`. Les composants MapLibre/WebGL sont stubés sous jsdom
   (impossible à rendre). Un vrai navigateur est requis pour #12.
4. **Mécanique de sync (#12 — faits à réutiliser).** La convergence est déjà prouvée
   **unitairement** (backend `test_sync_db.py`, frontend `sync.idempotent.test.ts`) :
   push idempotent par `mutation_id`, pull delta par curseur, conflits sensibles
   (visite) marqués. #12 ne réinvente pas ces invariants ; il les vérifie
   **end-to-end dans un navigateur réel**, ce que jsdom ne permet pas.

### Tension / contrainte centrale

Les tests E2E navigateur (#12) et les audits de perf (#13) sont **plus lents et plus
fragiles** que les tests unitaires. Mal intégrés, ils ralentissent la CI et
produisent des échecs instables (« flaky ») qui érodent la confiance. Cette spec
exige donc que ces vérifications soient **fiables et proportionnées** : un E2E
déterministe (pas de dépendance réseau réelle, temporisations maîtrisées), et des
budgets de perf avec des seuils réalistes (pas calés sur une machine idéale). Le
design DOIT arbitrer où elles s'exécutent (bloquant sur PR vs informatif/nightly)
pour ne pas transformer un garde-fou en frein.

### Hors périmètre

- La refonte du moteur de sync ou des invariants de convergence (déjà couverts
  unitairement) : #12 les **vérifie** end-to-end, ne les modifie pas.
- L'optimisation effective des performances (réduction de bundle, lazy-loading) :
  #13 **mesure et garde** un seuil ; les optimisations qui en découleraient sont un
  travail distinct.
- Les tests E2E exhaustifs de toutes les pages : #12 cible le **scénario de
  convergence multi-poste**, pas une couverture E2E générale.

## Glossary

- **Suite d'intégration** : les tests backend gardés par `TEST_DATABASE_URL` qui
  exigent un vrai PostgreSQL+PostGIS (RLS, sync, audit) — 56 tests skippés sans base.
- **E2E** (End-to-End) : test exécuté dans un vrai navigateur, exerçant l'app
  complète (UI + stockage local + réseau mocké ou réel).
- **Multi-poste** : deux contextes navigateur isolés (deux `BrowserContext`
  Playwright) simulant deux postes de travail distincts.
- **Budget de bundle** : seuil maximal de taille (octets, gzip) des artefacts de
  build, échouant si dépassé.
- **Lighthouse CI** : exécution automatisée des audits Lighthouse (perf, PWA) avec
  assertions sur des scores/métriques.
- **Flaky** : test au résultat non déterministe (passe/échoue sans changement de
  code) — à éviter absolument en CI bloquante.

## Non-functional Requirements rattachées

- **NFR-1** — Isolation des tenants : couverte par les tests d'intégration RLS (#5).
- **NFR-6** — Synchronisation et conflits : convergence vérifiée end-to-end (#12).
- **NFR-11** — Résilience et performance : premier rendu carte < 3 s en 3G ;
  dégradation WebGL (#13).
- **NFR-12** — Expérience de développement locale : pile et tests exécutables
  localement en une commande (#5).

## Requirements

### Requirement 1: Commande locale d'exécution de la suite d'intégration (#5)

**User Story:** En tant que développeur, je veux lancer la suite d'intégration
backend (RLS, sync, audit) en une seule commande locale, afin de ne pas découvrir en
CI une régression de la couche la plus critique.

#### Acceptance Criteria

1. LES scripts de cycle de vie (`dme.sh` et `dme.bat`) DOIVENT exposer une commande
   (p. ex. `test-db`) qui provisionne un PostgreSQL+PostGIS jetable, applique les
   migrations, exécute la **suite complète** (les 56 tests d'intégration inclus), puis
   nettoie les ressources éphémères.
2. LA commande NE DOIT PAS interférer avec la pile de développement (`start`) : base
   de test **isolée** (port/nom/volume distincts), détruite en fin d'exécution.
3. LA commande DOIT retourner un code de sortie **non nul** si un test échoue
   (utilisable en pré-commit/CI locale).
4. LA commande DOIT fonctionner sur les deux plateformes cibles (bash et Windows),
   de façon cohérente avec les commandes existantes.
5. LA documentation (`docs/E2E_VERIFICATION.md` §7 et/ou `README`) DOIT référencer
   cette commande comme la façon recommandée de lancer l'intégration en local.

_Requirements: observation #5 ; NFR-1, NFR-12_

### Requirement 2: Test E2E Playwright de convergence multi-poste (#12)

**User Story:** En tant qu'ingénieur, je veux un test automatisé qui exerce deux
postes hors ligne puis leur convergence à la reconnexion, afin de garder la promesse
local-first contre toute régression end-to-end.

#### Acceptance Criteria

1. LE projet DOIT introduire Playwright (dépendance + script `test:e2e`) et au moins
   un test exerçant **deux contextes navigateur isolés** (deux postes) contre l'app
   et une API réelle ou simulée de façon déterministe.
2. LE test DOIT reproduire le scénario : travail hors ligne des deux côtés
   (création/édition de patients/visites), reconnexion, puis vérification de la
   **convergence sans perte ni doublon** (idempotence par `mutation_id`).
3. LE test DOIT vérifier qu'un **conflit sur une entité sensible** (note de visite)
   est **marqué pour revue** (bannière de conflit) et non écrasé silencieusement.
4. LE test DOIT être **déterministe** : aucune dépendance à une temporisation
   arbitraire (`sleep`) ni à un réseau externe non contrôlé ; la connectivité est
   pilotée (online/offline simulés) et les attentes sont basées sur des conditions
   observables.
5. LE design DOIT préciser **où** ce test s'exécute (bloquant sur PR, ou job dédié/
   nightly) pour équilibrer couverture et temps de CI, et comment l'app est servie
   (build de prod servie, ou dev server) de façon reproductible.

_Requirements: observation #12 ; NFR-6_

### Requirement 3: Garde-fou de performance automatisé pour NFR-11 (#13)

**User Story:** En tant que mainteneur, je veux un garde-fou automatisé sur la taille
de bundle et/ou les métriques de perf, afin qu'une régression menaçant la cible
« carte < 3 s en 3G » soit détectée avant le déploiement.

#### Acceptance Criteria

1. LE projet DOIT ajouter au moins un garde-fou automatisé parmi : (a) **budget de
   bundle** (seuil de taille gzip sur les artefacts `vite build`, échec si dépassé),
   (b) **Lighthouse CI** (assertions sur des métriques perf/PWA).
2. LES seuils DOIVENT être **réalistes et documentés** (calibrés sur l'état actuel
   avec une marge), pour détecter une régression sans produire de faux échecs.
3. LE garde-fou DOIT s'exécuter en CI (nouveau job frontend ou étape dédiée) et son
   caractère **bloquant ou informatif** DOIT être explicite (le design tranche :
   un budget de bundle est un bon candidat bloquant ; Lighthouse, plus variable,
   peut être informatif/nightly).
4. SI Lighthouse CI est retenu ALORS son exécution DOIT être rendue aussi
   déterministe que possible (profil throttling fixe, mobile) et ses seuils tolérants
   à la variance inhérente.
5. LE garde-fou DOIT couvrir au minimum l'artefact critique du premier rendu (app
   shell + carte) pertinent pour NFR-11.

_Requirements: observation #13 ; NFR-11_

### Requirement 4: Intégration CI fiable et non régressive (#5, #12, #13)

**User Story:** En tant que responsable CI, je veux que ces nouvelles vérifications
s'intègrent sans ralentir ni déstabiliser le pipeline, afin que les garde-fous
restent un atout et non un frein.

#### Acceptance Criteria

1. LE job backend existant (ruff + migrations + pytest sur PostGIS) NE DOIT PAS être
   dégradé ni ralenti par les ajouts frontend ; les nouveaux travaux s'exécutent dans
   un **job distinct** (parallélisable).
2. LES tests E2E (#12) et les audits de perf (#13) NE DOIVENT PAS être « flaky » : un
   test instable DOIT être rendu déterministe ou déplacé hors du chemin bloquant.
3. LE temps de CI ajouté DOIT être proportionné : le design DOIT indiquer le coût
   estimé et la stratégie (parallélisme, cache, nightly) pour le contenir.
4. LES suites existantes (backend 223+ / intégration, frontend Vitest 143) DOIVENT
   rester vertes ; les ajouts sont **additifs**.

_Requirements: observations #5, #12, #13 ; NFR-11, NFR-12, NFR-1_

## Assumptions

- #5 est un ajout d'**outillage** (scripts + doc), sans changement du code applicatif
  ni des tests eux-mêmes — ils existent déjà, seule leur exécution locale est
  facilitée.
- #12 introduit une **nouvelle pile de test** (Playwright) : c'est le plus gros
  morceau ; un unique scénario de convergence multi-poste de qualité vaut mieux
  qu'une large couverture E2E fragile.
- #13 : décision tranchée (design, ADR-2) — **budget de bundle bloquant**
  (déterministe) + **Lighthouse informatif/nightly** (variable, ne bloque pas la PR).
- Les invariants de convergence restent prouvés unitairement ; #12 les vérifie
  end-to-end sans les remplacer.
- La CI gagne un **job frontend** ; le job backend reste inchangé et parallèle.
