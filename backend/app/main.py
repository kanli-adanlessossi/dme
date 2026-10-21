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

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.config import get_settings
from app.core.db import check_database_ready
from app.core.deps import get_current_tenant_id as auth_get_current_tenant_id
from app.core.rate_limit import limiter
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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Cycle de vie de l'application : garde-fou de démarrage fail-closed (G-1).

    Au démarrage, on refuse un déploiement non sûr AVANT d'accepter la moindre
    requête : `validate_production_safety()` lève `UnsafeProductionConfigError`
    (en nommant le réglage fautif) si, en production, le secret JWT est resté le
    défaut de dev, `debug` est actif, ou le rôle applicatif `APP_DB_*` est absent
    (NFR-1, NFR-2). Hors production (dev, tests), c'est un no-op : la collecte des
    tests qui importe `app.main` avec des valeurs de dev n'est pas cassée — c'est
    pourquoi la conception préfère le `lifespan` à un validateur d'import.
    """
    settings.validate_production_safety()
    yield


app = FastAPI(
    title=settings.app_name,
    description=(
        "Plateforme multi-tenant et local-first de centres médicaux du Togo. "
        "Contrat d'API et protocole de synchronisation figés (tâche 3)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Limitation de débit des endpoints publics — REPLI applicatif (G-4, exigence 4.1).
# La conception place la limitation au reverse proxy (Nginx `limit_req` sur
# `/api/public/*`, 60 req/min/IP → 429 ; artefact sous `deploy/reverse-proxy/`). Ce
# repli SlowAPI rend le 429 reproductible et testable quand aucun proxy n'est en
# façade (dev, tests, déploiement mono-instance), conformément au repli documenté.
#
# Portée bornée par décorateur explicite (jamais globalement) : le limiteur est posé
# via `@limiter.limit(...)` sur `/api/public/*` (`public_rate_limit()`, voir
# `app/routers/public.py`) ET sur `/api/auth/*` (`auth_rate_limit()`, voir
# `app/routers/auth.py` : `login`, `refresh`, `set-credentials`). La limite d'auth est
# un REPLI applicatif, défense par IP complémentaire du verrouillage par compte
# (`app/services/lockout.py`). Aucun `default_limits` n'est configuré et aucun
# middleware global n'est ajouté, de sorte que les autres routes authentifiées
# (`sync`, `center`, `patients`, `visits`, `admin`) et `/health` ne sont JAMAIS
# limitées. Les seuils sont configurables via `PUBLIC_RATE_LIMIT` (défaut
# « 60/minute ») et `AUTH_RATE_LIMIT` (défaut « 10/minute »).
#
# `app.state.limiter` est requis par SlowAPI ; le handler convertit un dépassement
# (`RateLimitExceeded`) en réponse 429 au corps minimal (aucune fuite d'information,
# design Error Handling G-4).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS : autorise le frontend local-first (Vite, http://localhost:5173) à appeler
# l'API depuis le navigateur. Sans ces en-têtes, les requêtes cross-origin — dont
# le chargement du GeoJSON des régions pour la carte — sont bloquées par le
# navigateur. Les origines sont explicites (jamais `*`) et configurables via
# `CORS_ALLOW_ORIGINS`. `allow_credentials=True` autorise l'envoi du jeton Bearer
# et des en-têtes d'auth sur les routes protégées.
#
# Méthodes et en-têtes sont désormais EXPLICITES (jamais `*`), eux aussi pilotés
# par la configuration (`CORS_ALLOW_METHODS` / `CORS_ALLOW_HEADERS`, G-5 / NFR-2) :
# le pré-vol n'annonce que GET/POST/PATCH/DELETE/OPTIONS et les en-têtes
# `Authorization` / `Content-Type` dont le frontend a besoin. Un joker est refusé
# par conception — et, combiné à `allow_credentials=True`, serait de toute façon
# invalide au sens de la spéc CORS. Un pré-vol non autorisé échoue côté navigateur
# comme auparavant, sans code d'erreur serveur supplémentaire.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)

# En-têtes de sécurité côté API (G-2). Middleware maison léger (conception : on
# refuse une dépendance tierce pour une poignée d'en-têtes statiques, cf. design).
# Il n'ajoute/n'altère QUE des en-têtes : il ne touche jamais au corps ni au statut
# de la réponse, et ne peut donc pas transformer une réponse valide en erreur.
#
# - `X-Content-Type-Options: nosniff` : empêche le MIME-sniffing du navigateur.
# - `X-Frame-Options: DENY` : interdit l'inclusion en cadre (anti-clickjacking).
# - `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` : CSP
#   verrouillée adaptée à une API JSON qui ne sert jamais de HTML exécutable. La CSP
#   applicative riche (origines MapLibre, service worker…) est portée par le serveur
#   statique frontend (G-3), pas ici.
#
# L'API ne pose PAS `Strict-Transport-Security` (HSTS) : cet en-tête n'a de sens
# qu'émis par le point de terminaison TLS (reverse proxy en production) ; l'émettre
# depuis l'API serait inopérant ou trompeur (cf. design, répartition des en-têtes).
_API_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


@app.middleware("http")
async def add_security_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Pose les en-têtes de sécurité API sur chaque réponse (G-2).

    Le middleware délègue d'abord au reste de la pile (`call_next`) pour obtenir la
    réponse telle quelle — corps et statut inchangés — puis se contente d'y ajouter
    les en-têtes statiques. Aucune transformation du corps ni du statut : il ne peut
    donc pas convertir une réponse valide en erreur (NFR-2).
    """
    response = await call_next(request)
    for header, value in _API_SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


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
