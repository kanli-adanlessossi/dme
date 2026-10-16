# Backend DME

Backend FastAPI (Python 3.12) de la plateforme multi-tenant de centres médicaux
du Togo.

Ce dossier est l'ossature posée par la tâche 1 : chargement de configuration via
Pydantic Settings et une application FastAPI minimale exposant `GET /health`.
La fondation complète (SQLAlchemy async, Alembic, migrations) arrive avec la
tâche 2.

## Lancer en local

Via Docker Compose depuis la racine du dépôt :

```bash
docker compose up
```

L'API est alors disponible sur <http://localhost:8000> et la santé sur
<http://localhost:8000/health>.
