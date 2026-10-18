"""Tests de la fondation backend (tâche 2) : configuration, /health, OpenAPI.

Vérifie l'exigence 9.3 (endpoint de santé + docs auto-générées) et la base de
NFR-13 (contrat OpenAPI exposé). Le test de readiness est résilient à l'absence
de base : la liveness répond 200 et le champ `database` reflète la disponibilité.
"""

from __future__ import annotations

import httpx2
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app

client = TestClient(app)


def test_health_renvoie_200() -> None:
    """GET /health répond 200 avec un statut « ok » (liveness)."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


def test_health_rapporte_etat_base() -> None:
    """GET /health expose un champ `database` de readiness (« ok »/« unavailable »)."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "database" in body
    assert body["database"] in {"ok", "unavailable"}


async def test_health_async_renvoie_200() -> None:
    """GET /health via un client httpx2 asynchrone répond 200 (pytest-asyncio)."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_route_health_enregistree() -> None:
    """La route /health est bien enregistrée sur l'application."""
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/health" in paths


def test_openapi_disponible() -> None:
    """FastAPI expose le contrat OpenAPI par défaut (base de NFR-13)."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"]


def test_docs_disponible() -> None:
    """Swagger UI (/docs) est servie et non désactivée (exigence 9.3)."""
    response = client.get("/docs")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_settings_construit_database_url() -> None:
    """La configuration construit une URL SQLAlchemy async cohérente."""
    settings = Settings(
        postgres_host="db",
        postgres_port=5432,
        postgres_db="dme",
        postgres_user="dme",
        postgres_password="secret",
    )
    assert settings.database_url == "postgresql+asyncpg://dme:secret@db:5432/dme"


def test_get_settings_est_memoise() -> None:
    """get_settings renvoie une instance unique (cache)."""
    assert get_settings() is get_settings()
