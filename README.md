# DME — Plateforme des centres médicaux du Togo

> Application web multi-tenant, **mobile-first** et **local-first (hors ligne)**,
> pour les centres médicaux des 5 régions du Togo. Chaque centre médical est un
> *tenant*. Le grand public découvre les centres via une carte interactive du
> Togo ; le personnel soignant travaille dans une application qui fonctionne
> **même sans connexion internet** et se synchronise quand le réseau revient.

**État du projet : spécifications terminées, implémentation à venir.** Le code
applicatif n'est pas encore présent dans ce dépôt. Les spécifications complètes
(exigences, conception, plan de tâches) se trouvent dans
[`.kiro/specs/medical-centers-togo/`](.kiro/specs/medical-centers-togo/).

---

## Pourquoi ce projet

La connectivité internet est **intermittente** dans la plupart des établissements
de santé en dehors de Lomé. Une application qui exige une connexion permanente est
inutilisable en pratique. DME est donc conçue **local-first** : le poste de travail
est la source de vérité du travail en cours, le serveur central est la source
consolidée et le point de synchronisation entre postes et centres.

## Fonctionnalités (cibles v1)

- 🗺️ **Carte publique interactive** du Togo avec les 5 régions cliquables ;
  sélectionner une région affiche ses centres médicaux (repli statique si WebGL
  indisponible).
- 🏥 **Multi-tenant** : chaque centre médical est isolé des autres, isolation
  appliquée en base via la Row-Level Security (RLS) de PostgreSQL.
- 📴 **Fonctionnement hors ligne (PWA)** : création/consultation des patients et
  visites sans réseau, stockées localement et chiffrées.
- 🔄 **Synchronisation automatique** à la reconnexion (file d'attente + deltas),
  avec résolution de conflits et marquage des cas cliniques sensibles pour revue.
- 🔐 **Sécurité des données de santé** : TLS, mots de passe Argon2id, piste
  d'audit append-only, suppression logique des PHI, chiffrement local.
- 🇫🇷 **Interface en français** par défaut, conventions togolaises (+228).

> Hors périmètre v1 : DME clinique complet, rendez-vous, facturation, applications
> natives, paiements/SMS. Voir les exigences pour le détail.

## Pile technique

| Couche              | Technologie |
|---------------------|-------------|
| Frontend            | React + TypeScript + Vite, PWA (service worker Workbox) |
| Stockage local      | IndexedDB chiffré via RxDB + file d'attente (outbox) |
| Carte               | MapLibre GL JS + GeoJSON (sans clé d'API) |
| Backend             | FastAPI (Python 3.12), async |
| ORM / migrations    | SQLAlchemy 2.x + Alembic |
| Base de données     | PostgreSQL 16 + PostGIS (RLS pour l'isolation des tenants) |
| Auth                | JWT (access + refresh), Argon2id |
| Exécution locale    | Docker Compose |

Le détail et les justifications sont dans
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) et la conception complète dans
[`.kiro/specs/medical-centers-togo/design.md`](.kiro/specs/medical-centers-togo/design.md).

## Démarrage rapide

> ⚠️ L'ossature de code n'est pas encore créée (voir *État du projet*). Une fois la
> tâche 1 du plan réalisée, le démarrage se fera ainsi :

```bash
# Cloner le dépôt
git clone <url-du-dépôt>
cd dme

# Copier la configuration d'exemple
cp .env.example .env

# Lancer toute la pile (Postgres/PostGIS + backend + frontend)
docker compose up
```

Services attendus une fois l'implémentation démarrée :

| Service   | URL                      | Description |
|-----------|--------------------------|-------------|
| Frontend  | http://localhost:5173    | Application web / PWA |
| API       | http://localhost:8000    | Backend FastAPI |
| API docs  | http://localhost:8000/docs | OpenAPI / Swagger |
| Health    | http://localhost:8000/health | Sonde de disponibilité |

Prérequis : [Docker](https://docs.docker.com/get-docker/) et Docker Compose.

## Structure du dépôt

```
dme/
├── .kiro/specs/medical-centers-togo/   # Spécifications (source de vérité)
│   ├── requirements.md                 # Exigences (EARS)
│   ├── design.md                       # Conception & architecture
│   └── tasks.md                        # Plan d'implémentation
├── docs/
│   └── ARCHITECTURE.md                 # Vue d'ensemble de l'architecture
├── backend/                            # (à venir) API FastAPI
├── frontend/                           # (à venir) PWA React
├── data/                               # (à venir) GeoJSON des régions du Togo
├── docker-compose.yml                  # (à venir) pile locale
├── CONTRIBUTING.md
├── LICENSE
└── README.md
```

## Documentation

- **Exigences** — [`.kiro/specs/.../requirements.md`](.kiro/specs/medical-centers-togo/requirements.md)
- **Conception** — [`.kiro/specs/.../design.md`](.kiro/specs/medical-centers-togo/design.md)
- **Plan de tâches** — [`.kiro/specs/.../tasks.md`](.kiro/specs/medical-centers-togo/tasks.md)
- **Architecture (synthèse)** — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **Contribuer** — [`CONTRIBUTING.md`](CONTRIBUTING.md)

## Feuille de route

L'implémentation suit le plan ordonné par dépendance dans `tasks.md` :

1. Pile locale (Docker) et fondation backend
2. Contrat d'API + protocole de synchronisation (contract-first)
3. Multi-tenant + RLS, puis audit distribué (fondations de sécurité)
4. Domaine clinique synchronisable + moteur de synchronisation serveur
5. Fondation frontend local-first (PWA, base locale chiffrée, sync)
6. Carte publique, annuaire, interface tenant
7. Vérification de bout en bout (isolation, hors ligne, conflits, sécurité)

## Sécurité et données de santé

Ce projet manipule des données de santé (PHI). Les principes appliqués : isolation
stricte des tenants (RLS), chiffrement des données locales au repos, piste d'audit
append-only, suppression logique (jamais de suppression physique des PHI en usage
normal), et capture du consentement. Voir les propriétés de correction (P1–P13)
dans la conception. Signalez toute faille de sécurité en privé plutôt que via une
*issue* publique.

## Licence

Distribué sous licence **MIT**. Voir [`LICENSE`](LICENSE).
