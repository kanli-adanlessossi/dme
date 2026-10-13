# Contribuer à DME

Merci de votre intérêt pour le projet. Ce document explique comment contribuer.

## Approche : spec-driven

Ce projet est piloté par les spécifications. Avant d'écrire du code, lisez les
documents de référence dans
[`.kiro/specs/medical-centers-togo/`](.kiro/specs/medical-centers-togo/) :

- `requirements.md` — ce que le système doit faire (exigences EARS).
- `design.md` — comment il est conçu (architecture, modèles, invariants P1–P13).
- `tasks.md` — l'ordre d'implémentation (plan ordonné par dépendance).

Toute évolution significative commence par une mise à jour des spécifications,
puis l'implémentation suit le plan de tâches.

## Flux de travail Git

1. Créez une branche à partir de la branche de base : `git switch -c
   feat/<description-courte>`.
2. Faites des commits atomiques avec des messages clairs (impératif présent,
   p. ex. « ajoute la validation géo des coordonnées »).
3. Poussez votre branche et ouvrez une *pull request* ; ne poussez jamais
   directement sur `main`.
4. Décrivez dans la PR : le résumé, ce qui a été testé, et l'exigence/tâche
   couverte (p. ex. « REQ-4, tâche 10 »).

Ne modifiez pas la configuration Git du dépôt ; n'utilisez pas de commandes
destructrices (`push --force`, `reset --hard`) sans accord explicite.

## Conventions de code

> L'ossature de code n'étant pas encore en place, ces conventions s'appliqueront
> dès la création des dossiers `backend/` et `frontend/`.

**Backend (Python)**
- Python 3.12, FastAPI, SQLAlchemy 2.x, Pydantic v2.
- Formatage et lint : `ruff` (format + check). Typage : annotations partout.
- Toute table cadrée au tenant porte `tenant_id` ; le serveur l'appose, jamais le
  client. Toute écriture de PHI journalise un événement d'audit.
- Les migrations passent par Alembic (pas de modification de schéma hors
  migration).

**Frontend (TypeScript)**
- React + TypeScript strict + Vite.
- Les pages tenant lisent/écrivent **la base locale** (RxDB), jamais l'API
  directement sur le chemin critique ; seul le moteur de sync parle à l'API
  tenant.
- Styles mobile-first (Tailwind), cibles tactiles ≥44px, interface en français.

## Tests

Les tests sont obligatoires pour les parties sensibles. Priorité aux :

- **Tests d'isolation** (un tenant ne voit jamais les données d'un autre).
- **Tests de synchronisation** (idempotence, reprise après coupure, conflits).
- **Tests de sécurité** (pas de PHI sur les endpoints publics, chiffrement local).

Chaque test devrait se rattacher à une propriété de correction (P1–P13) ou à un
critère d'acceptation d'une exigence. Lancez la suite avant d'ouvrir une PR.

## Signaler un bug ou proposer une fonctionnalité

Ouvrez une *issue* décrivant le comportement attendu vs observé, les étapes de
reproduction, et l'environnement. Pour une **faille de sécurité** (données de
santé), signalez-la en privé au mainteneur plutôt que via une issue publique.

## Licence

En contribuant, vous acceptez que vos contributions soient distribuées sous la
licence **MIT** du projet (voir [`LICENSE`](LICENSE)).
