"""Point d'entrée de l'application FastAPI.

Fondation backend (tâche 2) : application exposant `GET /health`
(liveness + readiness base de données), ainsi que le contrat OpenAPI via
`/docs` (Swagger UI) et `/openapi.json` (NFR-13). Le moteur et la session
SQLAlchemy asynchrones sont fournis par `app.core.db`.

Le contrat d'API et le protocole de synchronisation sont figés en tâche 3
(routeurs ci-dessous) afin que `/openapi.json` soit stable avant le travail
frontend (NFR-13). Les handlers sont pour l'instant des stubs (501) ; leur
logique arrive dans les tâches 7 à 12. L'audit, le contexte de tenant et la RLS
arrivent dans les tâches 4 et 5.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.db import check_database_ready
from app.core.deps import get_current_tenant_id as auth_get_current_tenant_id
from app.core.tenant_context import get_current_tenant_id as tenant_context_joint
from app.routers import (
    admin,
    auth,
    center,
    patients,
    public,
    sync,
    visits,
)

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description=(
        "Plateforme multi-tenant et local-first de centres médicaux du Togo. "
        "Contrat d'API et protocole de synchronisation figés (tâche 3)."
    ),
    version="0.1.0",
)

# CORS : autorise le frontend local-first (Vite, http://localhost:5173) à appeler
# l'API depuis le navigateur. Sans ces en-têtes, les requêtes cross-origin — dont
# le chargement du GeoJSON des régions pour la carte — sont bloquées par le
# navigateur. Les origines sont explicites (jamais `*`) et configurables via
# `CORS_ALLOW_ORIGINS`. `allow_credentials=True` autorise l'envoi du jeton Bearer
# et des en-têtes d'auth sur les routes protégées.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routeurs métier (contrat figé en tâche 3). L'ordre d'inclusion n'affecte pas
# le routage mais structure l'ordre des sections dans Swagger UI.
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(center.router)
app.include_router(patients.router)
app.include_router(visits.router)
app.include_router(sync.router)
app.include_router(public.router)

# Branchement de l'authentification sur la fondation RLS (tâches 4.3 → 7.5).
# `app.core.tenant_context.get_current_tenant_id` est un placeholder volontaire
# qui échoue en fail-closed (501) tant que l'auth n'est pas câblée. On le
# surcharge ici par la dépendance d'auth (tâche 7.5) qui dérive le `tenant_id` du
# JWT d'accès vérifié : `tenant_session` pilote ainsi `SET LOCAL app.tenant_id`
# depuis la revendication du jeton, sans que les routeurs cadrés au tenant aient à
# connaître le détail de l'authentification (P2, NFR-1).
app.dependency_overrides[tenant_context_joint] = auth_get_current_tenant_id


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Sonde de liveness et de readiness.

    - Liveness : le processus répond, donc le statut global est « ok » (200).
    - Readiness : on tente un `SELECT 1` sur la base ; le champ `database` vaut
      « ok » si la base répond, « unavailable » sinon. Le code reste 200 pour la
      liveness ; la supervision peut s'appuyer sur `database` pour la readiness.

    En conditions nominales (pile `docker compose` avec une base saine), la base
    répond et le healthcheck du conteneur obtient bien un 200.
    """
    database_status = "ok" if await check_database_ready() else "unavailable"

    return {
        "status": "ok",
        "environment": settings.environment,
        "database": database_status,
    }
