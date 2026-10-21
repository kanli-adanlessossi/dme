# Implementation Plan

## Overview

Objet : correction des écarts de mise en production.

Plan de correction des écarts recensés dans `requirements.md`, conçu selon
`design.md`. Le numéro de chaque tâche correspond au numéro de l'exigence qu'elle
satisfait (tâche 1 ↔ Requirement 1 ↔ écart G-1, etc.) : la traçabilité est directe
(écart ↔ exigence ↔ conception ↔ tâche ↔ test).

Principes d'ordonnancement :
1. Fail-closed d'abord — refuser un démarrage non sûr (tâche 1) avant tout.
2. Additif, non régressif — les 256 tests existants et la pile locale restent
   intacts ; chaque tâche ajoute ses propres tests ou sa procédure de vérification.
3. Dépendances de déploiement explicites — la CSP applicative (tâche 3) et la
   limitation au proxy (tâche 4) supposent l'introduction du serveur statique /
   reverse proxy ; voir le graphe ci-dessous.

Chaque tâche indique sa Definition of Done (DoD) et référence l'écart + les exigences
associées.

## Tasks

- [x] 1. [G-1] Garde-fou de configuration de production (fail-fast)
  - [x] 1.1 Ajouter `_DEV_JWT_SECRET`, `UnsafeProductionConfigError` et
    `Settings.validate_production_safety()` dans `app/core/config.py` (secret par
    défaut, `debug`, `APP_DB_*` absents → lève en production uniquement). Partir de
    zéro : aucune de ces définitions n'existe actuellement (ébauche revertie,
    vérifié).
  - [x] 1.2 Invoquer la validation dans un gestionnaire `lifespan` de `app/main.py`
    (échec avant d'accepter des requêtes) ; no-op hors production.
  - [x] 1.3 Tests : 5 cas (secret défaut, debug, APP_DB manquant, config saine,
    environnement dev). DoD : les 5 cas passent ; les 256 tests existants restent
    verts ; un démarrage prod non sûr échoue avec un message nommant le réglage
    fautif.
  - _Requirements: 1 ; NFR-1, NFR-2_

- [x] 2. [G-2] En-têtes de sécurité côté API
  - [x] 2.1 Middleware d'en-têtes dans `app/main.py` : `nosniff`,
    `X-Frame-Options: DENY`, CSP d'API verrouillée (`default-src 'none'`,
    `frame-ancestors 'none'`). L'API ne pose PAS HSTS (émis par le proxy, cf. design).
  - [x] 2.2 Test vérifiant la présence des en-têtes sur une réponse (`/health`). DoD :
    en-têtes présents sur toute réponse API ; test vert ; aucun corps/statut modifié.
  - _Requirements: 2 ; NFR-2_

- [x] 3. [G-3] Image Docker frontend de production + CSP applicative
  - [x] 3.1 Convertir `frontend/Dockerfile` en multi-stage (build `vite build` → run
    `nginx:alpine` servant `dist/`).
  - [x] 3.2 Config Nginx : fallback SPA ; CSP applicative concrète (voir design,
    directives MapLibre + service worker + `report-to`) ; en-têtes `nosniff` /
    `X-Frame-Options` ; cache court sur `sw`/manifeste, cache long immuable sur les
    assets horodatés.
  - [x] 3.3 Vérifier : `vite build` passe ; rechargement et fonctionnement hors ligne
    sur l'image Nginx (service worker actif) ; CSP présente et sans joker.
  - [x] 3.4 Conserver la cible de dev (`npm run dev`) pour la pile locale. DoD : image
    de prod sert `dist/` via Nginx avec CSP applicative ; app rechargeable hors ligne ;
    `docker compose up` local inchangé. Dépend de la tâche 2 (valeurs d'en-têtes
    communes).
  - _Requirements: 3 ; NFR-5, NFR-11, NFR-12_

- [x] 4. [G-4] Limitation de débit + cache des endpoints publics
  - [x] 4.1 Limitation de débit sur `/api/public/*` au reverse proxy (Nginx
    `limit_req`), seuil 60 req/min/IP, réponse 429 ; repli middleware applicatif
    documenté si aucun proxy.
  - [x] 4.2 `Cache-Control: public, max-age=300` sur `/api/public/regions` et
    `/api/public/geo/togo-regions.geojson`.
  - [x] 4.3 Vérifier que la projection sûre et la règle « actifs seulement » restent
    inchangées. DoD : au-delà du seuil → 429 reproductible ; `max-age` présent ; test
    de non-régression vert. Dépend de la tâche 3 (introduit le reverse proxy).
  - _Requirements: 4 ; NFR-2_

- [x] 5. [G-5] Restriction CORS en production
  - [x] 5.1 Remplacer `allow_methods=["*"]` / `allow_headers=["*"]` par des listes
    explicites (config) ; conserver les origines explicites.
  - [x] 5.2 Vérifier que le frontend (Bearer + JSON) fonctionne toujours. DoD : pré-vol
    CORS sans joker ; frontend fonctionnel ; test vert.
  - _Requirements: 5 ; NFR-2_

- [x] 6. [G-6] Fin de la fuite d'outbox `center_profile`
  - Décision tranchée (ADR G-6) : **Option 2 — ne plus enfiler `center_profile` dans
    l'outbox** ; le profil s'enregistre en REST direct (`PATCH /api/center/profile`).
  - [x] 6.1 Dans `frontend/src/lib/db.ts`, faire que `upsertCenterProfile`
    n'inscrive plus d'entrée d'outbox ; retirer le `TODO(14.x profil)` de
    `frontend/src/lib/sync.ts`.
  - [x] 6.2 Test : sous éditions répétées du profil, l'outbox ne croît pas.
    DoD : outbox bornée ; aucun contrat de sync conflictuel ; test vert.
  - _Requirements: 6 ; NFR-5, NFR-6_

- [x] 7. [G-7] CI avec Postgres/PostGIS (couverture RLS réelle)
  - [x] 7.1 Job CI avec service `postgis/postgis:16-3.4`, migrations
    `alembic upgrade head`, puis `pytest` complet.
  - [x] 7.2 Confirmer que les 54 tests d'intégration DB ne sont plus ignorés et
    passent. DoD : pipeline CI vert avec les 54 tests exécutés.
  - _Requirements: 7 ; NFR-1_

- [x] 8. [G-8] Résorption des dépréciations
  - [x] 8.1 Monter FastAPI/Starlette/httpx ; remplacer
    `HTTP_422_UNPROCESSABLE_ENTITY` ; migrer l'usage `httpx`/`testclient` déprécié.
    DoD : suite de tests sans avertissement de dépréciation ; 256 tests verts.
  - _Requirements: 8 ; NFR-13_

- [x] 9. [G-9] Durcissement JWT (TTL + denylist de refresh)
  - Décision tranchée (ADR G-9) : **Options 2 + 3** — raccourcir l'access ET ajouter
    une denylist `jti` du refresh consultée en ligne uniquement. Limite assumée : la
    révocation d'un acteur hors ligne reste bornée par le TTL d'access.
  - [x] 9.1 Appliquer immédiatement (configuration, sans modèle) :
    `jwt_access_ttl_seconds = 28800` (8 h) et `jwt_refresh_ttl_seconds = 1209600`
    (14 j) dans `app/core/config.py`. DoD : défauts mis à jour ; 256 tests verts.
  - [x] 9.2 **(Livrée.)** Famille `jti` + denylist de refresh (voir Data Models) :
    migration de la table `refresh_token`, écriture/consommation du `jti` à
    l'émission, et refus du rejeu d'un `jti` consommé sur `/api/auth/refresh` (chemin
    en ligne uniquement ; les postes hors ligne n'y accèdent pas). **Livrée et
    testée** : migration `0010_refresh_token.py`, service `refresh_tokens.py`
    (`record_issued_refresh`, `consume_refresh` — UPDATE conditionnel atomique
    `… RETURNING` + invalidation de famille au rejeu), câblage `auth.py` (login émet
    `jti`+`family_id` et enregistre le refresh ; refresh consomme, refuse les refresh
    hérités sans `jti`, rote avec nouveau `jti`/même `family_id`), tests
    `test_refresh_denylist_db.py` (gardés par `TEST_DATABASE_URL`). DoD atteinte :
    rejeu d'un refresh consommé rejeté ; travail hors ligne non affecté ; tests verts.
  - _Requirements: 9 ; NFR-7, NFR-2_

## Task Dependency Graph

```
1 Garde-fou démarrage ─┐
2 En-têtes API ─────────┼─> (indépendants, backend)
5 Restriction CORS ─────┘
6 Fuite outbox center_profile  (indépendant, frontend)
7 CI avec Postgres             (indépendant, CI)
8 Dépréciations                (indépendant, maintenance)
9 Durcissement JWT             (9.1 config, immédiat ; 9.2 denylist, livrée)

2 ──> 3 Image frontend prod + CSP applicative ──> 4 Rate-limit + cache publics
      (3 réutilise les valeurs d'en-têtes de 2 ;
       4 suppose le reverse proxy introduit par 3)
```

Seules deux dépendances dures existent : **2 → 3 → 4**. Tout le reste est
parallélisable. Chemin critique : **2 → 3 → 4**.

```json
{
  "tasks": [
    { "id": "1", "gap": "G-1", "dependsOn": [],    "wave": 1, "severity": "high" },
    { "id": "2", "gap": "G-2", "dependsOn": [],    "wave": 1, "severity": "high" },
    { "id": "5", "gap": "G-5", "dependsOn": [],    "wave": 1, "severity": "low" },
    { "id": "6", "gap": "G-6", "dependsOn": [],    "wave": 1, "severity": "medium" },
    { "id": "7", "gap": "G-7", "dependsOn": [],    "wave": 1, "severity": "medium" },
    { "id": "8", "gap": "G-8", "dependsOn": [],    "wave": 1, "severity": "low" },
    { "id": "9", "gap": "G-9", "dependsOn": [],    "wave": 1, "severity": "medium" },
    { "id": "3", "gap": "G-3", "dependsOn": ["2"], "wave": 2, "severity": "high" },
    { "id": "4", "gap": "G-4", "dependsOn": ["3"], "wave": 3, "severity": "medium" }
  ],
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "5", "6", "7", "8", "9"] },
    { "wave": 2, "tasks": ["3"] },
    { "wave": 3, "tasks": ["4"] }
  ],
  "criticalPath": ["2", "3", "4"]
}
```

## Notes

- Ordre recommandé : traiter d'abord les tâches 1 et 2 (sécurité à fort impact, bas
  risque, purement additives), puis la chaîne 3 → 4. Les tâches 5 à 9 peuvent avancer
  en parallèle.
- Décisions : les ADR G-6 et G-9 sont **tranchés** (section « Décisions
  d'architecture (ADR) » de `design.md`) ; les tâches 6 et 9 en appliquent le choix.
- Découpage de la tâche 9 : 9.1 (raccourcissement des TTL, configuration) et 9.2
  (table `refresh_token` + denylist `jti` + rotation de famille) sont **toutes deux
  livrées**. 9.2 a bien introduit un modèle de données (migration `0010`), implémenté
  via le service `refresh_tokens.py`, câblé dans `auth.py` et couvert par
  `test_refresh_denylist_db.py`.
- Hors de ce lot : les améliorations facultatives (A-1 chiffrement Web Crypto, A-2
  jetons hors `localStorage`) ne sont pas planifiées ici.
