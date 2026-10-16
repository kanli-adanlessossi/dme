# Implementation Plan

## Overview

Ce plan construit la plateforme multi-tenant **et local-first** de centres
médicaux pour le Togo par ordre de dépendance, suivant la revue « systèmes
médicaux » et la contrainte de connectivité intermittente.

Principes d'ordonnancement :
1. **Isolation d'abord** — multi-tenant + RLS et leurs tests précèdent toute
   fonctionnalité stockant des données.
2. **Contrat tôt** — le contrat OpenAPI *et le protocole de synchronisation* sont
   figés juste après la fondation afin que backend et frontend progressent en
   parallèle (NFR-13).
3. **L'audit est transversal, construit avant les PHI** — et conçu
   distribué-compatible (événements hors ligne téléversés à la sync) (NFR-3).
4. **Local-first dès la conception du domaine** — les entités PHI portent les
   champs de synchronisation (UUIDv7, version, updated_at) dès leur création ; la
   sync n'est pas un rétro-ajout.
5. **Suppression logique, consentement, chiffrement local et gestion des
   identifiants font partie de la fonctionnalité qui les introduit.**

Chaque tâche référence les exigences qu'elle satisfait. Vérifier chaque propriété
de correction (P1–P13 du design) à mesure que la tâche associée se termine.

## Tasks

- [x] 1. Mettre en place le dépôt et la pile locale
  - Créer la structure `backend/`, `frontend/`, `data/` et le `docker-compose.yml` racine.
  - Ajouter `db` (postgis/postgis:16-3.4) avec volume + healthcheck, les services `api`, `web`.
  - Ajouter `.env.example` et le chargement de configuration (Pydantic Settings).
  - Vérifier que `docker compose up` lance Postgres et que `/health` renvoie 200.
  - _Requirements: NFR-12, 9.1, 9.3_

- [x] 2. Fondation backend (FastAPI + SQLAlchemy + Alembic)
  - [x] 2.1 Initialiser l'app FastAPI, `pyproject.toml`, moteur/session SQLAlchemy async, `/health`, `/docs`, `/openapi.json`.
    - _Requirements: 9.3, NFR-13_
  - [x] 2.2 Configurer Alembic pour les migrations versionnées ; activer l'extension PostGIS dans la migration initiale.
    - _Requirements: NFR-12_

- [x] 3. Figer le contrat d'API et le protocole de synchronisation (contract-first)
  - Définir les schémas Pydantic requête/réponse et les stubs de routes pour tous
    les points d'accès (auth, admin, center, patients, visits, public), avec le
    contrat d'erreur 404/403.
  - Définir le protocole de sync : `POST /api/sync/push` (lot idempotent de
    mutations), `GET /api/sync/pull?since=<curseur>` (deltas), formats de curseur
    et de conflit, afin que `/openapi.json` soit stable avant le travail frontend.
  - _Requirements: NFR-13, 1.3, 4.3_

- [x] 4. Cœur multi-tenant + Row-Level Security (critique pour la sécurité)
  - [x] 4.1 Créer les tables `tenant`, `app_user`, `medical_center` avec colonnes `tenant_id` ; ajouter un rôle de base applicatif non-superuser.
  - [x] 4.2 Ajouter une migration activant et FORÇANT la RLS avec les politiques `tenant_isolation` (USING + WITH CHECK sur `app.tenant_id`).
  - [x] 4.3 Implémenter la dépendance de contexte de tenant par requête qui exécute `SET LOCAL app.tenant_id` dans une transaction.
  - [x] 4.4 Écrire les tests d'isolation : le tenant A ne peut lire/écrire les lignes du tenant B ; une requête brute sans contexte ne renvoie aucune ligne (vérifie P1, P2).
  - _Requirements: NFR-1, 1.2, 1.3, 5.2_

- [x] 5. Capacité d'audit distribuée (transversale, avant les PHI)
  - [x] 5.1 Créer `audit_log` en append-only (droits insertion seule ; SELECT pour platform_admin ; aucun UPDATE/DELETE) avec un écrivain non contraint par le contexte de tenant ; colonnes `event_at` (client) et `received_at` (serveur).
  - [x] 5.2 Ajouter un helper/middleware d'audit et le câbler afin que le code PHI, auth et sync ultérieur journalise de manière cohérente ; accepter les événements hors ligne téléversés.
  - [x] 5.3 Tests : les événements plateforme à tenant NULL persistent ; lignes immuables ; un événement PHI hors ligne téléversé est conservé sans perte (vérifie P5).
  - _Requirements: NFR-3_

- [x] 6. Modèle de données de référence + régions
  - [x] 6.1 Créer les tables `region` et `service_type` (migration + modèles SQLAlchemy).
  - [x] 6.2 Charger le GeoJSON des régions du Togo (jeu de données ouvert et attribué) dans `data/togo-regions.geojson` et amorcer les 5 régions (Savanes, Kara, Centrale, Plateaux, Maritime) avec leur géométrie.
  - _Requirements: 7.6, 9.2_

- [x] 7. Authentification tolérante à l'hors-ligne, configuration des identifiants et RBAC
  - [x] 7.1 Implémenter le hachage de mot de passe Argon2id et l'auth `app_user`.
  - [x] 7.2 Implémenter `POST /api/auth/login` + `refresh`, émettant des JWT portant `tenant_id` et `role`, avec une durée de vie dimensionnée pour une journée de travail hors ligne ; journaliser les événements d'auth.
  - [x] 7.3 Implémenter la limitation de débit / le verrouillage de connexion après échecs répétés.
  - [x] 7.4 Implémenter le flux de jeton de configuration d'identifiants à usage unique et expirant (`POST /api/auth/set-credentials`).
  - [x] 7.5 Implémenter les dépendances de cadrage par rôle ; 403 si rôle manquant, 404 en inter-tenant (vérifie P3) ; exposer l'état de révocation via `sync/pull`.
  - [x] 7.6 Tests : cadrage par rôle, non-fuite d'existence, verrouillage, expiration du jeton, et révocation propagée à la sync (NFR-7).
  - _Requirements: 1.1, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 3.2, NFR-2, NFR-7_

- [x] 8. Intégration et cycle de vie des tenants (administrateur de plateforme)
  - [x] 8.1 `POST /api/admin/tenants` crée tenant + medical_center + tenant_admin initial + jeton de configuration (audité).
  - [x] 8.2 `GET/PATCH /api/admin/tenants/{id}` liste et active/désactive ; la désactivation conserve les données, masque du public, bloque la connexion.
  - [x] 8.3 Tests : un non-admin ne peut créer/lister des tenants ; un tenant désactivé est masqué + connexion refusée ; actions auditées (vérifie P8).
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, NFR-4_

- [x] 9. Backend du domaine clinique (patient/visite) synchronisable, avec suppression logique, consentement, audit
  - [x] 9.1 Créer les tables `patient`/`visit` avec `tenant_id`, `created_by`, `deleted_at`, consentement, `UNIQUE(tenant_id, mrn)`, et les champs de sync `id` (UUIDv7 client), `version`, `updated_at` ; `national_id` optionnel.
  - [x] 9.2 Implémenter le CRUD cadré au tenant ; le serveur appose `tenant_id` ; DELETE logique ; accepte un `id` client ; jamais exposé publiquement ; PHI auditées.
  - [x] 9.3 Tests : isolation ; soft-delete conservé ; unicité patient ; consentement ; aucune PHI publique ; PHI auditée (vérifie P4, P5, P6).
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, NFR-1, NFR-3, NFR-4_

- [x] 10. Moteur de synchronisation côté serveur (push/pull, conflits)
  - [x] 10.1 Implémenter `POST /api/sync/push` : application idempotente d'un lot de mutations (clé = UUID de mutation) sous contexte RLS ; `WITH CHECK` empêche l'injection inter-tenant.
  - [x] 10.2 Implémenter `GET /api/sync/pull?since=<curseur>` : deltas du tenant depuis le curseur + nouveau curseur, cadrés RLS.
  - [x] 10.3 Implémenter la résolution LWW sur `updated_at`/`version` ; créer la table `sync_conflict` ; marquer les entités sensibles pour revue plutôt que les écraser.
  - [x] 10.4 Tests : push idempotent (pas de doublon après reprise) ; pull cadré au tenant ; injection inter-tenant rejetée ; conflit → LWW + journal, entité sensible marquée (vérifie P10, P11, P12).
  - _Requirements: 4.2, 4.3, 4.4, 4.6, NFR-3, NFR-6, NFR-13_

- [x] 11. Gestion du profil du centre médical + validation géo
  - [x] 11.1 `GET/PATCH /api/center/profile` pour que tenant_admin modifie nom, adresse, coordonnées, contact, horaires, services.
  - [x] 11.2 Valider que les coordonnées se situent dans le Togo (`ST_Within` PostGIS, repli boîte englobante).
  - [x] 11.3 Tests : coordonnées hors Togo rejetées ; mises à jour reflétées dans la projection publique (vérifie P7).
  - _Requirements: 6.1, 6.2, 6.3_

- [x] 12. API annuaire public et carte (projection sûre)
  - [x] 12.1 `GET /api/public/regions`, `/regions/{code}/centers`, `/centers/{id}` ne renvoyant que les champs publics ; tenants actifs seulement.
  - [x] 12.2 `GET /api/public/geo/togo-regions.geojson` servant les limites des régions.
  - [x] 12.3 Tests : aucune fuite de PHI/champs privés ; tenants inactifs exclus ; région vide renvoie une liste vide (vérifie P4, P8).
  - _Requirements: 7.2, 7.3, 7.4, 8.1, 8.2, 8.3_

- [x] 13. Données d'amorçage pour le dev local
  - Amorcer ≥1 centre médical d'exemple par région avec coordonnées togolaises valides, un tenant_admin d'exemple et un platform_admin.
  - Vérifier que l'amorçage s'exécute automatiquement au `docker compose up`.
  - _Requirements: 9.1, 9.2_

- [x] 14. Fondation frontend local-first (PWA + base locale + moteur de sync)
  - [x] 14.1 Mettre en place React + TypeScript + Vite + Tailwind ; styles mobile-first, navigation inférieure ; react-i18next (français par défaut).
  - [x] 14.2 Configurer la PWA : `manifest.webmanifest` + service worker (Workbox) pré-cachant l'app et les ressources de carte (installable, rechargeable hors ligne).
  - [x] 14.3 Mettre en place la base locale RxDB (IndexedDB) **chiffrée** ; schémas `patient`/`visit`/profil + outbox ; génération d'UUIDv7 côté client.
  - [x] 14.4 Implémenter `sync.ts` : push de l'outbox + pull des deltas, idempotence, reprise après coupure, application des conflits/révocations ; `SyncStatusBar` (en ligne/hors ligne + éléments en attente).
  - [x] 14.5 Implémenter la session locale tolérante hors-ligne (`session.ts`) et le verrouillage de la clé de chiffrement à la déconnexion.
  - [x] 14.6 Tests : CRUD hors ligne depuis la base locale ; rejeu idempotent ; PHI illisibles sans clé ; révocation purge la session (vérifie P9, P10, P13).
  - _Requirements: NFR-5, NFR-6, NFR-7, NFR-8, NFR-9, NFR-10, NFR-11, NFR-13, 4.1, 4.2, 4.3, 4.5, 4.7_

- [x] 15. Carte interactive du Togo (accueil public)
  - [x] 15.1 Construire `TogoMap` avec MapLibre GL chargeant le GeoJSON des régions ; régions cliquables et surlignables ; GeoJSON mis en cache par le service worker.
  - [x] 15.2 Au clic sur une région, afficher les centres dans une feuille inférieure mobile ; lier au profil du centre.
  - [x] 15.3 Implémenter le repli en cas d'indisponibilité de WebGL (`RegionFallbackList`).
  - [x] 15.4 Vérifier le rendu mobile, la cliquabilité, et le rechargement hors ligne depuis le cache.
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, NFR-9, NFR-11_

- [x] 16. Pages annuaire public et profil de centre
  - Construire les pages `RegionCenters` (parcourir/filtrer par région) et `CenterProfile` consommant l'API publique (avec repli cache).
  - _Requirements: 8.1, 8.2, 7.4_

- [x] 17. Interface tenant authentifiée (local-first)
  - [x] 17.1 Pages `Login` + `SetCredentials` et gestion de session (en ligne pour la 1ʳᵉ connexion, locale ensuite).
  - [x] 17.2 Éditeur de profil `CenterAdmin` avec sélecteur d'épingle sur carte contraint au Togo.
  - [x] 17.3 Interface CRUD `Patients` et `Visits` lisant/écrivant **la base locale** (jamais l'API directement), consentement à la création ; `ConflictBanner` pour les conflits à revoir.
  - _Requirements: 3.2, 6.1, 4.1, 4.2, 4.4, 4.7, 5.1, 5.6_

- [x] 18. Vérification de bout en bout et revue de sécurité
  - Exécuter la pile complète via Docker ; lancer les suites isolation, audit, auth/verrouillage, soft-delete, géo, projection publique, **hors ligne, synchronisation, conflits, chiffrement local**.
  - Scénario terrain : travailler hors ligne sur deux postes, reconnecter, vérifier convergence sans perte ni doublon et conflits sensibles marqués.
  - Vérification mobile/PWA : carte < 3 s en 3G ; app installable et rechargeable hors ligne ; navigation à une main.
  - Confirmer qu'aucune PHI n'apparaît sur un point d'accès non authentifié ni en clair dans le stockage local ; TLS documenté.
  - _Requirements: NFR-1, NFR-2, NFR-3, NFR-4, NFR-5, NFR-6, NFR-7, NFR-8, NFR-9, NFR-11, NFR-12, 9.1, 9.2, 9.3_

## Task Dependency Graph

```
1 Pile / scaffold
└─> 2 Fondation backend
    └─> 3 Figer contrat API + protocole de sync
        ├─> 4 Multi-tenant + RLS ──────────────┐
        │   └─> 5 Audit distribué                │
        │       ├─> 6 Données de référence/régions
        │       ├─> 7 Auth hors-ligne + RBAC
        │       │   └─> 8 Intégration & cycle de vie des tenants
        │       │       └─> 9 Domaine clinique synchronisable
        │       │           ├─> 10 Moteur de sync serveur (push/pull/conflits)
        │       │           └─> 11 Profil du centre + validation géo
        │       │               └─> 12 API annuaire public & carte
        │       │                   └─> 13 Données d'amorçage
        │       │
        └───────┴─> 14 Fondation frontend local-first (PWA + RxDB + sync)
                     │        (requiert 3 ; 14.4 s'intègre à 10)
                     ├─> 15 Carte interactive        (requiert 12, 14)
                     ├─> 16 Pages annuaire public     (requiert 12, 14)
                     └─> 17 Interface tenant local-first (requiert 9, 10, 11, 14)
                              └─> 18 Vérification E2E & revue sécurité (requiert tout)
```

Chemin critique : **1 → 2 → 3 → 4 → 5 → 7 → 8 → 9 → 10 → 17 → 18.**
La fondation frontend local-first (14) peut démarrer dès que le contrat et le
protocole de sync sont figés (3) et progresser en parallèle de la chaîne backend ;
l'intégration de sync de l'UI (17) dépend toutefois du moteur de sync serveur
(10). Les tâches backend porteuses de PHI (9, 10) dépendent de l'audit (5).

Le JSON suivant définit les vagues d'exécution. Chaque vague est un ensemble de
tâches dont les dépendances sont satisfaites par les vagues précédentes ; les
tâches d'une même vague peuvent s'exécuter en parallèle.

```json
{
  "tasks": [
    { "id": "1",  "dependsOn": [],              "wave": 1 },
    { "id": "2",  "dependsOn": ["1"],           "wave": 2 },
    { "id": "3",  "dependsOn": ["2"],           "wave": 3 },
    { "id": "4",  "dependsOn": ["3"],           "wave": 4 },
    { "id": "14", "dependsOn": ["3"],           "wave": 4 },
    { "id": "5",  "dependsOn": ["4"],           "wave": 5 },
    { "id": "6",  "dependsOn": ["5"],           "wave": 6 },
    { "id": "7",  "dependsOn": ["5"],           "wave": 6 },
    { "id": "8",  "dependsOn": ["7"],           "wave": 7 },
    { "id": "9",  "dependsOn": ["8"],           "wave": 8 },
    { "id": "10", "dependsOn": ["9"],           "wave": 9 },
    { "id": "11", "dependsOn": ["9"],           "wave": 9 },
    { "id": "12", "dependsOn": ["6", "11"],     "wave": 10 },
    { "id": "13", "dependsOn": ["12"],          "wave": 11 },
    { "id": "15", "dependsOn": ["12", "14"],    "wave": 11 },
    { "id": "16", "dependsOn": ["12", "14"],    "wave": 11 },
    { "id": "17", "dependsOn": ["10", "11", "14"], "wave": 11 },
    { "id": "18", "dependsOn": ["13", "15", "16", "17"], "wave": 12 }
  ],
  "waves": [
    { "wave": 1,  "tasks": ["1"] },
    { "wave": 2,  "tasks": ["2"] },
    { "wave": 3,  "tasks": ["3"] },
    { "wave": 4,  "tasks": ["4", "14"] },
    { "wave": 5,  "tasks": ["5"] },
    { "wave": 6,  "tasks": ["6", "7"] },
    { "wave": 7,  "tasks": ["8"] },
    { "wave": 8,  "tasks": ["9"] },
    { "wave": 9,  "tasks": ["10", "11"] },
    { "wave": 10, "tasks": ["12"] },
    { "wave": 11, "tasks": ["13", "15", "16", "17"] },
    { "wave": 12, "tasks": ["18"] }
  ],
  "criticalPath": ["1", "2", "3", "4", "5", "7", "8", "9", "10", "17", "18"]
}
```

## Notes

- **Pourquoi local-first :** la connectivité intermittente hors de Lomé rend une
  architecture online-first inutilisable. L'application de travail lit/écrit une
  base locale chiffrée et synchronise en arrière-plan ; le poste est la source de
  vérité du travail en cours, le serveur la source consolidée.
- **Pourquoi une bibliothèque de sync :** réplication, reprise après coupure,
  détection de conflits et curseurs sont les parties risquées — déléguées à RxDB
  plutôt que réécrites. La logique métier (RLS, audit, conflits sensibles) reste
  côté serveur.
- **Isolation et audit d'abord :** inchangé — multi-tenant (4) et audit (5)
  précèdent toute PHI, désormais aussi l'audit distribué (événements hors ligne).
- **Traçabilité de la correction :** les tâches indiquent la propriété (P1–P13)
  que leurs tests vérifient. La tâche 18 relance la suite complète, y compris les
  scénarios hors ligne/sync/conflits.
- **Parallélisation :** la fondation local-first (14) démarre dès le contrat figé
  (3), en parallèle du backend ; l'UI tenant (17) attend le moteur de sync (10).
- **Décisions ouvertes** (voir Overview du design) à résoudre avant la tâche 9/10 :
  bibliothèque de sync (RxDB vs PouchDB), granularité de conflit, entités
  « sensibles », durée de vie des jetons hors ligne, et politique de consentement.
- **Exécution suggérée :** les tâches critiques (4, 5, 7, 9, 10, 14) — isolation,
  audit, sync, chiffrement local — sont de bons candidats à une boucle
  implémentation-revue déléguée à un workflow.
