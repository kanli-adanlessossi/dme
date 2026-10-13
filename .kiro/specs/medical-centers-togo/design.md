# Design Document

## Overview

Plateforme web multi-tenant, mobile-first **et local-first**, où **chaque centre
médical est un tenant.** La surface publique est une carte interactive des 5
régions du Togo avec des régions cliquables révélant les centres qui s'y
trouvent. La surface authentifiée (application de travail du personnel) est une
**PWA qui fonctionne hors ligne** : elle lit/écrit dans un stockage local chiffré
et se synchronise avec le serveur central quand le réseau est disponible.
L'isolation par tenant reste appliquée en base de données serveur via la
Row-Level Security (RLS) de PostgreSQL.

**Pourquoi local-first.** La connectivité internet est intermittente dans la
plupart des établissements hors de Lomé ; une architecture online-first (un appel
API par action) serait inutilisable. Le patron retenu est local-first : le poste
est la source de vérité pour le travail en cours, le serveur est la source de
vérité consolidée et le point de synchronisation entre postes/centres.

Priorités de conception, dans l'ordre : **exactitude de l'isolation des tenants**,
**fonctionnement hors ligne et synchronisation fiable**, **sûreté des données de
santé (auditabilité, suppression logique, consentement, chiffrement local)**, UX
mobile-first, exactitude géographique, et une ossature propre et extensible.

Le document est organisé pour correspondre aux groupes d'exigences : Fondations →
Multi-tenant et sécurité → Local-first et synchronisation → Domaine clinique →
Expérience publique → Exploitabilité.

### Choix technologiques (et justification)

| Préoccupation        | Choix                                    | Justification |
|----------------------|------------------------------------------|---------------|
| Framework backend    | **FastAPI** (Python 3.12)                | Async, typé, OpenAPI/Swagger auto, excellente DX |
| ORM / migrations     | **SQLAlchemy 2.x** + **Alembic**         | Mature, gère les variables de session RLS, schéma versionné |
| Base de données      | **PostgreSQL 16 + PostGIS**              | RLS pour l'isolation ; PostGIS pour les requêtes géo et la validation des frontières |
| Validation           | **Pydantic v2**                          | Schémas requête/réponse, gestion de configuration |
| Authentification     | Jetons JWT access + refresh ; Argon2id   | API sans état, porte `tenant_id` + rôle ; durée de vie adaptée au hors-ligne (NFR-7) |
| Limitation de débit  | SlowAPI / throttle au reverse-proxy      | Protection anti force brute à la connexion (NFR-2) |
| Frontend             | **React + TypeScript + Vite** (PWA)      | Builds rapides, typage fort ; PWA installable (NFR-5) |
| Hors ligne / cache   | **Service worker** (Workbox)             | Chargement et fonctionnement sans réseau (NFR-5, NFR-11) |
| Stockage local       | **IndexedDB** via **RxDB** (ou PouchDB)  | Base locale par poste + moteur de synchronisation éprouvé (NFR-5, NFR-6) |
| Chiffrement local    | Couche chiffrée sur IndexedDB            | PHI chiffrées au repos sur le poste (NFR-8) |
| Synchronisation      | Outbox + delta-sync (push/pull)          | Rejoue les mutations et récupère les deltas à la reconnexion (NFR-6) |
| Identifiants         | **UUIDv7** générés côté client           | Évite les collisions de clés entre postes hors ligne (NFR-6) |
| Carte                | **MapLibre GL JS** + GeoJSON (mis en cache) | Open-source, sans clé d'API, régions cliquables, tuiles/GeoJSON en cache |
| Styles               | Tailwind CSS (utilitaires mobile-first)  | Mise en page responsive rapide |
| i18n                 | react-i18next (défaut `fr`)              | Français par défaut (NFR-6) |
| Conteneurs           | Docker Compose                           | Pile locale en une commande (NFR-8) |

> Alternatives envisagées : le schéma-par-tenant et la base-par-tenant ont été
> écartés pour la v1. RLS + schéma partagé est le choix par défaut recommandé pour
> le SaaS B2B ; il évite l'engorgement du catalogue et la dérive des migrations
> par tenant tout en appliquant l'isolation dans le moteur. À réévaluer si un
> tenant a besoin d'une résidence physique des données.

### Questions ouvertes / décisions à confirmer

1. « Mobile-first » = application web responsive (hypothèse retenue) ou
   applications natives plus tard ?
2. Le détail de carte au niveau préfecture est-il requis pour la v1, ou le niveau
   région suffit-il (hypothèse : niveau région) ?
3. Un régime de conformité (p. ex. résidence des données par tenant) imposerait-il
   une isolation par schéma/base pour un tenant ?
4. Confirmer le français comme unique locale par défaut pour la v1.
5. Confirmer la durée de conservation des PHI et si la capture du consentement doit
   bloquer le stockage lorsqu'il est absent, ou seulement être enregistrée
   (conception actuelle : enregistrée).
6. Confirmer la bibliothèque de synchronisation (RxDB vs PouchDB/CouchDB) et la
   granularité de résolution (enregistrement vs champ) ; conception actuelle :
   RxDB, résolution au niveau enregistrement en LWW.
7. Confirmer quelles entités sont « cliniquement sensibles » (conflit marqué pour
   revue plutôt qu'écrasé) — a minima les notes de visite.
8. Confirmer la durée de vie des jetons d'accès pour couvrir une journée de
   travail hors ligne (impacte NFR-7 et la fenêtre de révocation).

## Architecture

```
   POSTE DE TRAVAIL (centre médical) — fonctionne hors ligne
   ┌───────────────────────────────────────────────────┐
   │  PWA mobile-first (React + TS)                      │
   │  - Service worker (Workbox) : app + carte en cache │
   │  - UI lit/écrit la base LOCALE (jamais le réseau    │
   │    directement sur le chemin critique)             │
   │  ┌───────────────────────────────────────────────┐ │
   │  │  IndexedDB chiffré (RxDB)                      │ │
   │  │  patient · visit · profil · audit local        │ │
   │  │  + OUTBOX (mutations en attente)               │ │
   │  └───────────────────────────────────────────────┘ │
   │              ▲ sync bidirectionnelle (quand réseau) │
   └──────────────┼──────────────────────────────────────┘
                  │ HTTPS / JSON — push outbox + pull deltas
                  │ (contrat OpenAPI + protocole de sync, NFR-13)
   ┌──────────────▼──────────────┐
   │  FastAPI (Python, async)     │
   │  - Auth / JWT + limite débit │
   │  - Middleware contexte tenant│
   │  - Middleware d'audit        │
   │  - Endpoints de SYNC         │
   │  - Routeurs publics & tenant │
   └──────────────┬──────────────┘
   définit app.tenant_id par requête
                  │
   ┌──────────────▼──────────────┐
   │  PostgreSQL 16 + PostGIS     │  ← source de vérité consolidée
   │  Politiques Row-Level Security│
   └──────────────────────────────┘
                  ▲
   ┌──────────────┴──────────────┐
   │  Visiteur public (internet) │  carte + annuaire (en ligne, cache SW)
   └─────────────────────────────┘
```

Côté serveur, les services s'exécutent sous Docker Compose pour le développement
local. Côté poste, la PWA est autonome : après la première visite, elle n'a plus
besoin du réseau pour fonctionner ; la synchronisation est opportuniste et en
arrière-plan.

### Synchronisation (cœur local-first)

Modèle de flux :
1. **Écriture locale.** Toute mutation clinique est écrite dans IndexedDB (RxDB)
   et inscrite dans l'outbox avec un UUIDv7 et une version/horodatage client.
   L'UI est immédiatement à jour, sans dépendre du réseau.
2. **Push.** Quand le réseau est présent, le moteur de sync envoie l'outbox au
   point d'accès `POST /api/sync/push`. Les requêtes portent une clé
   d'idempotence (l'UUID de la mutation) ; rejouer une mutation déjà appliquée
   est sans effet (NFR-6 : idempotent, reprenable).
3. **Pull.** Le client appelle `GET /api/sync/pull?since=<curseur>` et reçoit les
   changements du tenant survenus depuis son dernier curseur (delta-sync). Le
   serveur applique la RLS : un client ne reçoit jamais que les données de son
   tenant.
4. **Résolution de conflits.** Chaque enregistrement porte un `version`
   (compteur) et un `updated_at`. En cas de divergence, la règle par défaut est
   Last-Write-Wins sur `updated_at` ; le serveur consigne le perdant dans un
   journal de conflits. Les entités marquées « sensibles » (p. ex. notes
   cliniques) ne sont pas écrasées silencieusement : le conflit est signalé pour
   revue humaine (NFR-6).
5. **Audit distribué.** Les événements PHI hors ligne sont journalisés dans un
   `audit local` et poussés avec l'outbox ; le serveur conserve l'horodatage
   d'événement (client) et ajoute l'horodatage de réception (NFR-3).

Pourquoi une bibliothèque de sync (RxDB/PouchDB) plutôt qu'une couche maison : la
réplication, la reprise après coupure, la détection de conflits et les curseurs
sont exactement les parties difficiles et risquées à réécrire. On délègue à une
brique éprouvée et on garde la logique métier (RLS, audit, conflits sensibles)
côté serveur.

### Intégration contract-first (NFR-9)

Le schéma OpenAPI de FastAPI fait référence unique pour le frontend. Le contrat
d'API (chemins, schémas requête/réponse, formes d'erreur) est figé tôt (voir les
tâches) afin que backend et frontend progressent en parallèle contre une interface
stable. Un client typé est généré/dérivé du schéma OpenAPI.

### Conception multi-tenant (cœur)

#### Modèle d'isolation : schéma partagé + `tenant_id` + RLS
- Chaque table cadrée au tenant possède une colonne `tenant_id UUID` non nulle,
  clé étrangère vers `tenant`.
- Une politique RLS sur chaque table cadrée au tenant restreint les lignes à
  `tenant_id = current_setting('app.tenant_id')::uuid`.
- L'application définit `app.tenant_id` une fois par requête/transaction. Même une
  requête qui oublie une clause `WHERE tenant_id = …` ne peut franchir les
  tenants.

```sql
-- appliqué par table cadrée au tenant, p. ex. patient
ALTER TABLE patient ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON patient
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
```

#### Définition du contexte de tenant
- La connexion à la base utilise un **rôle applicatif non-superuser et
  non-BYPASSRLS** afin que les politiques soient toujours appliquées.
- Par requête, une dépendance FastAPI ouvre une transaction et exécute :
  `SET LOCAL app.tenant_id = '<uuid>'`.
- `SET LOCAL` limite le réglage à la transaction en cours ; les connexions
  mutualisées ne divulguent donc jamais de contexte entre requêtes.
- Les points d'accès transversaux de l'administrateur de plateforme utilisent un
  chemin de code distinct et explicitement audité qui définit `app.tenant_id` sur
  le tenant cible, ou un rôle d'administration dédié — jamais de façon ad hoc.

#### Données de référence vs données de tenant
- **Référence (non cadrée au tenant) :** `region` (les 5 régions), `service_type`.
  Lisibles par tous ; aucune restriction RLS (ou politique de lecture
  permissive).
- **Données publiques de tenant :** les champs de profil de `medical_center` sont
  lisibles publiquement mais modifiables uniquement par ce tenant. La lecture
  publique passe par un chemin dédié en lecture seule exposant une projection
  sûre (pas de filtre RLS par tenant, mais une liste blanche de
  colonnes/points d'accès) et excluant les tenants inactifs.
- **Données privées de tenant (PHI) :** `patient`, `visit`, `app_user` — RLS
  appliquée, jamais publiques.

#### Écriture de `tenant_id`
- L'appelant ne fournit jamais de `tenant_id` dans le corps de la requête. Le
  serveur l'appose depuis le contexte authentifié (REQ-4.2). Le `WITH CHECK` de la
  RLS sert de garde-fou.

## Components and Interfaces

### API backend (routeurs FastAPI)

**Auth**
- `POST /api/auth/login` → `{access, refresh}` portant `tenant_id`, rôle ;
  limité en débit avec verrouillage après échecs répétés (NFR-2).
- `POST /api/auth/refresh`.
- `POST /api/auth/set-credentials` → consomme un jeton de configuration à usage
  unique, définit le mot de passe (REQ-3.2).

**Administrateur de plateforme** (rôle=platform_admin, audité)
- `POST /api/admin/tenants` → crée tenant + tenant_admin initial + jeton de
  configuration.
- `GET /api/admin/tenants`, `PATCH /api/admin/tenants/{id}`
  (activer/désactiver).

**Cadré au tenant** (auth requise ; contexte RLS défini ; accès PHI audité)
- `GET/PATCH /api/center/profile` → gérer son propre centre.
- `GET/POST /api/patients`, `GET/PATCH/DELETE /api/patients/{id}`
  (DELETE = logique).
- `GET/POST /api/visits`, `DELETE /api/visits/{id}` (logique).

**Synchronisation** (auth requise ; contexte RLS défini ; audité)
- `POST /api/sync/push` → applique un lot de mutations de l'outbox ; idempotent
  via l'UUID de mutation ; renvoie les résultats et les conflits détectés
  (NFR-6, NFR-13).
- `GET /api/sync/pull?since=<curseur>` → renvoie les deltas du tenant depuis le
  curseur, plus un nouveau curseur (delta-sync, cadré RLS).

**Public** (sans auth, lecture seule, projection sûre, tenants actifs seulement)
- `GET /api/public/regions` → 5 régions (+ géométrie/GeoJSON optionnels).
- `GET /api/public/regions/{code}/centers` → centres publics d'une région.
- `GET /api/public/centers/{id}` → profil public d'un centre.
- `GET /api/public/geo/togo-regions.geojson` → limites des régions pour la carte
  (mis en cache par le service worker).

**Transversal**
- `GET /health` → liveness/readiness.
- `GET /docs` + `GET /openapi.json` → contrat OpenAPI (NFR-9).

### Composants frontend (mobile-first)

```
frontend/src/
  main.tsx, App.tsx
  sw.ts                 # service worker (Workbox) : précache app + carte
  manifest.webmanifest  # PWA installable
  lib/
    api.ts              # client typé dérivé d'OpenAPI (NFR-13)
    db.ts               # base locale RxDB (IndexedDB chiffré)
    sync.ts             # moteur outbox + delta-sync (push/pull)
    session.ts          # session locale, auth tolérante hors-ligne (NFR-7)
  i18n/                 # fr par défaut
  pages/
    MapHome.tsx         # REQ-7 accueil : carte MapLibre du Togo
    RegionCenters.tsx   # REQ-8 liste par région
    CenterProfile.tsx   # détail public d'un centre
    Login.tsx, SetCredentials.tsx
    tenant/CenterAdmin.tsx, Patients.tsx, Visits.tsx
  components/
    TogoMap.tsx         # wrapper MapLibre + régions GeoJSON
    RegionFallbackList.tsx  # repli non-WebGL REQ-7.5
    SyncStatusBar.tsx   # état connectivité + changements en attente (NFR-5)
    ConflictBanner.tsx  # signale les conflits cliniques à revoir (NFR-6)
    BottomNav.tsx       # navigation atteignable au pouce
```

**Patron d'accès aux données (local-first)**
- Les pages tenant (`Patients`, `Visits`, `CenterAdmin`) lisent et écrivent
  **uniquement** la base locale RxDB — jamais directement l'API sur le chemin
  critique. `sync.ts` est le seul composant qui parle à l'API tenant.
- `SyncStatusBar` reflète l'état : en ligne/hors ligne et nombre d'éléments dans
  l'outbox.
- Les lectures publiques (carte, annuaire) passent par `api.ts` mais le service
  worker sert une version en cache en cas de coupure.

**Composant carte (REQ-7)**
- MapLibre GL charge `togo-regions.geojson` ; chaque région est une couche de
  remplissage avec une propriété `code`. Clic → mise en évidence + routage vers
  `RegionCenters`.
- Épingles de centres chargées par région sélectionnée.
- Détection WebGL → rendu de `RegionFallbackList` (REQ-7.5).
- Stylé pour le tactile : grandes zones de clic, feuille inférieure (bottom sheet)
  pour la liste des centres sur mobile.

**Règles responsive**
- Styles de base pour 320–430px ; les points de rupture `md:`/`lg:` enrichissent la
  mise en page.
- Navigation inférieure + feuilles inférieures pour un usage à une main ; cibles
  tactiles minimales de 44px.

### Conception sécurité et audit

- TLS partout (terminé au niveau du proxy en production ; documenté en local).
- JWT : access de courte durée, refresh rotatif ; les jetons portent `sub`,
  `tenant_id`, `role`. Le serveur redérive le contexte de tenant depuis le jeton,
  jamais depuis le corps.
- Hachage de mot de passe Argon2id. Identifiants initiaux livrés via un jeton de
  configuration à usage unique et expirant — jamais un mot de passe en clair
  (REQ-3.2).
- Limitation de débit à la connexion + verrouillage de compte après échecs répétés
  (NFR-2).
- Le rôle applicatif de la base est non-superuser avec `FORCE ROW LEVEL SECURITY`,
  de sorte que la RLS ne peut être contournée par des bogues applicatifs.

**Authentification tolérante à l'hors-ligne (NFR-7)**
- Après une connexion réussie en ligne, le poste conserve une **session locale**
  (jeton valide + profil utilisateur) permettant de travailler pendant une
  coupure, sans réémission de jeton tant qu'il est valide.
- Les jetons d'accès ont une durée de vie dimensionnée pour une journée de travail
  hors ligne ; le rafraîchissement est opportuniste à la reconnexion.
- La révocation (tenant/utilisateur désactivé) est appliquée au plus tard à la
  prochaine synchronisation réussie : `sync/pull` renvoie l'état de révocation,
  et le client purge sa session et ses PHI locales.

**Chiffrement des données locales (NFR-8)**
- La base locale (IndexedDB via RxDB) chiffre les PHI au repos. La clé est dérivée
  des identifiants de session de l'utilisateur et n'est pas persistée en clair.
- À la déconnexion/expiration, la clé est verrouillée : les PHI locales
  redeviennent inaccessibles sans nouvelle authentification.
- Objectif de menace : perte ou vol d'un poste. Hors périmètre : un utilisateur
  légitime malveillant déjà authentifié sur le poste.

**Journal d'audit (append-only, écriture toujours) — NFR-3**
L'audit doit capturer les actions au niveau plateforme (tenant_id NULL) ainsi que
les accès PHI cadrés au tenant ; il NE PEUT donc PAS se trouver derrière une
politique de lecture RLS par tenant qui masquerait les lignes à tenant NULL.
Conception :
- `audit_log` est en **insertion seule** : une politique/des droits autorisent
  INSERT et (pour l'administrateur de plateforme) SELECT, mais aucun
  UPDATE/DELETE pour les rôles applicatifs.
- Les écritures passent par un écrivain d'audit dédié non contraint par
  `app.tenant_id`, de sorte que les événements de niveau plateforme
  (tenant_id NULL) persistent toujours.
- Événements audités : succès/échec de connexion, création/consultation de PHI,
  cycle de vie des tenants, et actions transversales d'administration.

**Accès à la table `tenant`**
Les lignes de `tenant` sont lues par l'administrateur de plateforme (toutes) et
par un membre (uniquement la sienne, dérivée du jeton). Plutôt qu'une RLS par
tenant sur `tenant` elle-même (ce qui serait circulaire), l'accès est cadré dans
la couche applicative/administration ; les membres n'interrogent jamais `tenant`
en dehors de leur propre id.

### Développement local et déploiement

Services de `docker-compose.yml` :
- `db` : `postgis/postgis:16-3.4`, volume persistant, healthcheck.
- `api` : FastAPI (uvicorn), attend que `db` soit sain, exécute les migrations
  Alembic + l'amorçage au démarrage.
- `web` : serveur de dev Vite (ou build statique servi par nginx).

Une commande : `docker compose up` (NFR-8). L'amorçage insère les 5 régions + ≥1
centre d'exemple par région avec des coordonnées togolaises valides (REQ-8.2).

Arborescence du projet :
```
dme/
  docker-compose.yml
  backend/
    pyproject.toml
    alembic/ (migrations)
    app/
      main.py
      core/ (config, security, db, tenant_context, audit)
      models/  schemas/  routers/  services/
      seed/ (régions + centres d'exemple)
      tests/
  frontend/
    package.json, vite.config.ts, tailwind.config.js
    src/ (voir composants frontend)
  data/
    togo-regions.geojson   # jeu de données ouvert et attribué (CC-BY-4.0)
```

## Data Models

```
region (référence)
  id uuid pk
  code text unique            -- SAVANES, KARA, CENTRALE, PLATEAUX, MARITIME
  name_fr text
  geometry geometry(MultiPolygon, 4326)   -- pour la carte / vérifs spatiales

tenant  (= identité organisationnelle du centre médical)
  id uuid pk
  name text
  region_id uuid fk -> region
  status text  -- active | inactive        (cycle de vie logique, NFR-4)
  created_at timestamptz

medical_center  (profil public ; 1:1 avec tenant en v1)
  id uuid pk
  tenant_id uuid fk -> tenant unique
  name text
  region_id uuid fk -> region
  address text
  location geometry(Point, 4326)   -- validé dans la frontière du Togo
  phone text                        -- +228 ...
  email text
  opening_hours jsonb
  is_public boolean default true

service_type (référence)      -- p. ex. maternité, pharmacie, laboratoire
  id uuid pk
  name_fr text

medical_center_service (liaison)
  medical_center_id uuid fk
  service_type_id uuid fk

app_user  (cadré au tenant ; platform_admin a tenant_id nul)
  id uuid pk
  tenant_id uuid null fk -> tenant   -- nul seulement pour platform_admin
  email text unique
  password_hash text null            -- Argon2id ; nul tant que non défini
  setup_token_hash text null         -- usage unique, expirant (REQ-3.2)
  setup_token_expires_at timestamptz null
  failed_login_count int default 0   -- throttle force brute (NFR-2)
  locked_until timestamptz null
  role text  -- platform_admin | tenant_admin | clinician | staff
  is_active boolean

patient  (domaine d'amorce cadré au tenant, PHI ; synchronisable)
  id uuid pk                         -- UUIDv7 généré côté client (NFR-6)
  tenant_id uuid fk -> tenant
  mrn text                           -- numéro de dossier local au tenant
  given_name text, family_name text
  dob date, sex text, phone text
  national_id text null              -- optionnel ; beaucoup n'en ont pas
  consent_to_store boolean not null  -- capture du consentement (NFR-4, REQ-5.6)
  created_by uuid fk -> app_user
  created_at timestamptz
  updated_at timestamptz             -- base de résolution LWW (NFR-6)
  version int not null default 1     -- compteur de version pour conflits
  deleted_at timestamptz null        -- suppression logique (NFR-4)
  UNIQUE (tenant_id, mrn)            -- unicité patient dans le tenant (REQ-5.4)

visit  (domaine d'amorce cadré au tenant, PHI ; synchronisable)
  id uuid pk                         -- UUIDv7 généré côté client (NFR-6)
  tenant_id uuid fk -> tenant
  patient_id uuid fk -> patient
  visited_at timestamptz
  reason text, notes text
  created_by uuid fk -> app_user
  updated_at timestamptz             -- base de résolution LWW (NFR-6)
  version int not null default 1
  deleted_at timestamptz null        -- suppression logique (NFR-4)

audit_log  (append-only ; voir Conception sécurité et audit)
  id bigserial pk
  tenant_id uuid null                -- nul pour les actions de niveau plateforme
  actor_user_id uuid null
  action text, entity text, entity_id uuid null
  event_at timestamptz               -- horodatage côté client (hors ligne)
  received_at timestamptz            -- horodatage de réception serveur (NFR-3)
  ip inet

sync_conflict  (journal des conflits de synchronisation ; NFR-6)
  id bigserial pk
  tenant_id uuid fk -> tenant
  entity text, entity_id uuid
  loser_payload jsonb                -- version perdante conservée (LWW)
  winner_version int
  sensitive boolean                  -- true => à revoir par un humain
  resolved_by uuid null fk -> app_user
  created_at timestamptz
```

Note sur les clés : les PHI utilisent des **UUIDv7 générés côté client** comme
clé primaire, afin que deux postes créant des enregistrements hors ligne ne
produisent pas de collision et que l'id soit stable dès l'écriture locale
(aucune réécriture de clé à la synchronisation). `updated_at` + `version`
fournissent la base de la résolution de conflits.

La RLS est activée + forcée sur les tables cadrées au tenant : `medical_center`
(chemin d'écriture), `app_user`, `patient`, `visit`.
`region`, `service_type` sont des tables de référence en lecture permissive.
`tenant` et `audit_log` sont traitées de façon particulière (voir Conception
sécurité et audit).

### Validation de la frontière du Togo

Les coordonnées sont validées comme se situant à l'intérieur du Togo. Préféré :
`ST_Within` de PostGIS contre la géométrie du pays. Repli léger : une contrainte
CHECK sur la boîte englobante (longitude ≈ [-0.15, 1.80], latitude ≈
[6.10, 11.14]).

## Correctness Properties

Ce sont les invariants que le système doit maintenir ; la stratégie de test existe
pour les vérifier.

- **P1 — Isolation inter-tenant.** Aucune requête authentifiée exécutée dans le
  contexte du tenant A ne peut lire ou écrire une ligne appartenant au tenant B.
  Vrai même si le code applicatif omet un filtre `tenant_id`, car la RLS l'applique
  dans le moteur et le rôle applicatif ne peut la contourner.
- **P2 — Tenancy autoritaire côté serveur.** Le `tenant_id` d'un enregistrement est
  toujours dérivé du jeton authentifié, jamais de l'entrée client ; un `tenant_id`
  falsifié dans un corps de requête ne peut pas prendre effet (le `WITH CHECK` de
  la RLS le rejette).
- **P3 — Pas de fuite d'existence.** Une requête vers une ressource hors du tenant
  de l'appelant est indiscernable d'une requête vers une ressource inexistante
  (toutes deux 404) ; les échecs de permission renvoient 403.
- **P4 — Confidentialité des PHI.** Aucun point d'accès non authentifié/public ne
  peut renvoyer de PHI (`patient`, `visit`), quelle que soit l'entrée.
- **P5 — Complétude de l'audit.** Chaque événement d'authentification, chaque
  création/consultation de PHI, et chaque action de cycle de vie des tenants /
  d'administration transversale produit exactement un enregistrement d'audit, y
  compris les événements de niveau plateforme (tenant_id NULL).
- **P6 — Durabilité des PHI (suppression logique).** Le CRUD normal ne supprime
  jamais physiquement les PHI ; un patient/visite « supprimé » est exclu des
  lectures mais reste en stockage avec un horodatage `deleted_at`.
- **P7 — Validité géographique.** Les coordonnées d'un centre médical se situent
  toujours à l'intérieur de la frontière du Togo ; les coordonnées hors limites
  sont rejetées à l'écriture.
- **P8 — Surface publique « actifs seulement ».** L'annuaire public et la carte
  n'exposent que les tenants actifs (non désactivés).
- **P9 — Disponibilité hors ligne.** Après la première visite, toutes les
  opérations cliniques courantes réussissent sans réseau ; aucune écriture n'est
  perdue en cas de coupure (conservée dans l'outbox).
- **P10 — Synchronisation idempotente et sans perte.** Rejouer tout ou partie de
  l'outbox (après une coupure en cours de push) ne crée ni doublon ni
  corruption ; chaque mutation est appliquée au plus une fois (clé d'idempotence
  = UUID de mutation).
- **P11 — Isolation maintenue à la synchronisation.** `sync/pull` ne renvoie que
  les données du tenant de l'appelant ; `sync/push` applique le `WITH CHECK` RLS,
  si bien qu'un client ne peut ni lire ni injecter des données d'un autre tenant
  via la sync.
- **P12 — Résolution de conflits non destructive.** Un conflit ne détruit jamais
  silencieusement une donnée : le perdant LWW est consigné dans `sync_conflict`,
  et les entités sensibles sont marquées pour revue humaine.
- **P13 — Confidentialité locale.** Les PHI au repos sur le poste sont chiffrées ;
  après déconnexion/expiration, elles sont inaccessibles sans réauthentification.

## Error Handling

- **Contrat d'erreur :** les ressources inconnues et les ressources hors du tenant
  de l'appelant renvoient **404** (pas de fuite d'existence, respecte P3) ; les
  échecs d'autorisation renvoient **403** ; les échecs de validation renvoient
  **422** avec le détail d'erreur Pydantic.
- **Échecs d'authentification :** 401 générique ne révélant pas si l'e-mail
  existe ; les échecs répétés incrémentent `failed_login_count` et positionnent
  `locked_until`, après quoi les tentatives renvoient 429 jusqu'à expiration du
  verrou (NFR-2).
- **Configuration des identifiants :** un jeton de configuration expiré ou déjà
  utilisé renvoie 400/410 sans révéler l'état du compte.
- **Validation géo :** les coordonnées hors du Togo renvoient 422 avec un message
  clair (respecte P7).
- **Échecs de contexte de tenant :** si `app.tenant_id` n'est pas défini pour une
  opération cadrée au tenant, la RLS renvoie zéro ligne plutôt que de divulguer des
  données ; la dépendance traite un contexte manquant comme une erreur interne
  (500) plutôt que de s'exécuter sans cadrage.
- **Échecs d'écriture d'audit :** les mutations auditées sont encapsulées de sorte
  qu'un échec d'écriture d'audit fasse échouer la transaction (fail-closed),
  préservant P5 ; les échecs d'audit en lecture sont journalisés mais ne bloquent
  pas la lecture.
- **Erreurs de base/connectivité :** exposées en 503 avec une sémantique propice au
  réessai ; `/health` reflète la disponibilité de la base.
- **Indisponibilité carte/WebGL :** le frontend se dégrade vers le sélecteur de
  région statique plutôt que d'échouer (NFR-11, REQ-7.5).
- **Perte de réseau pendant le travail :** aucune erreur bloquante ; l'écriture
  va dans la base locale et l'outbox, `SyncStatusBar` passe en « hors ligne »
  (NFR-5, P9).
- **Coupure pendant un push :** le push est repris au prochain cycle ; grâce à la
  clé d'idempotence, les mutations déjà appliquées ne sont pas rejouées (P10).
- **Conflit de synchronisation :** résolu en LWW et consigné ; si l'entité est
  sensible, un `ConflictBanner` invite à la revue (P12). Jamais d'écrasement
  silencieux.
- **Jeton expiré hors ligne :** l'utilisateur peut finir l'opération locale en
  cours ; à la reconnexion, un refresh est tenté, sinon une réauthentification
  est demandée (NFR-7).
- **Révocation détectée à la sync :** le client purge session et PHI locales (P13).

## Testing Strategy

- **Tests d'isolation (priorité la plus haute, P1/P2) :** vérifier que le tenant A
  ne peut lire/écrire le tenant B via l'API et que la RLS bloque les requêtes
  directes sans contexte, y compris les tentatives de falsification du `tenant_id`
  dans les corps de requête.
- **Tests de non-fuite d'existence (P3) :** l'accès inter-tenant renvoie 404 ; les
  échecs de permission renvoient 403.
- **Tests de confidentialité des PHI (P4) :** aucun point d'accès public ne renvoie
  de PHI, quelle que soit l'entrée ; liste blanche de projection appliquée.
- **Tests d'audit (P5) :** chaque création/consultation de PHI et chaque événement
  d'auth produit un enregistrement d'audit ; les événements plateforme à tenant
  NULL persistent ; les lignes d'audit sont immuables pour les rôles applicatifs ;
  un échec d'écriture d'audit annule la mutation.
- **Tests de suppression logique (P6) :** les PHI supprimées sont exclues des
  lectures mais conservées en stockage.
- **Tests auth/verrouillage :** le cadrage par rôle renvoie 403 ; le verrouillage
  de connexion se déclenche après échecs répétés ; l'expiration/réutilisation du
  jeton de configuration est rejetée.
- **Tests de validation géo (P7) :** les coordonnées hors du Togo sont rejetées.
- **Tests « actifs seulement » (P8) :** les tenants désactivés sont exclus de la
  surface publique.
- **Tests hors ligne (P9) :** avec le réseau coupé (service worker actif), les
  CRUD cliniques réussissent depuis la base locale ; aucune écriture perdue.
- **Tests de synchronisation (P10, P11) :** rejeu d'outbox idempotent (pas de
  doublon après coupure en cours de push) ; coupure/reprise sans corruption ;
  `sync/pull` cadré au tenant ; `sync/push` rejette l'injection cross-tenant.
- **Tests de conflits (P12) :** modifications concurrentes sur deux postes →
  LWW appliqué, perdant consigné dans `sync_conflict` ; entité sensible marquée
  pour revue, non écrasée.
- **Tests de chiffrement local (P13) :** PHI illisibles dans IndexedDB sans la
  clé ; verrouillage effectif après déconnexion.
- **Tests d'auth hors ligne (NFR-7) :** travail possible jeton valide sans
  réseau ; révocation appliquée à la sync suivante.
- **Test de fumée :** `docker compose up` → `/health` 200 et amorçage présent (5
  régions + ≥1 centre par région).
- **Vérification mobile et PWA :** la carte se charge sous la cible 3 s/3G ;
  l'app est installable et se recharge hors ligne ; navigation à une main et
  cibles tactiles de 44px vérifiées sur un petit écran.
