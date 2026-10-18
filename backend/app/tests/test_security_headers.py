"""Tests des en-têtes de sécurité côté API (G-2, tâche 2.2).

Vérifie que le middleware `add_security_headers` pose les en-têtes de sécurité
attendus sur chaque réponse de l'API (ici `/health`), sans jamais altérer le
corps ni le statut de la réponse (NFR-2). On encode aussi la décision de
conception : l'API n'émet PAS `Strict-Transport-Security` (HSTS), réservé au
point de terminaison TLS.

Validates: Requirements 2.1-2.5, NFR-2.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_EXPECTED_API_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


def test_health_porte_les_entetes_de_securite() -> None:
    """GET /health porte les trois en-têtes de sécurité avec leurs valeurs exactes."""
    response = client.get("/health")
    for header, expected in _EXPECTED_API_SECURITY_HEADERS.items():
        assert response.headers.get(header) == expected


def test_api_n_emet_pas_hsts() -> None:
    """L'API n'émet pas `Strict-Transport-Security` (HSTS réservé au terminateur TLS)."""
    response = client.get("/health")
    assert "Strict-Transport-Security" not in response.headers


def test_entetes_de_securite_ne_modifient_ni_corps_ni_statut() -> None:
    """Le middleware n'altère ni le statut ni le corps de /health (NFR-2)."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "environment" in body
    assert body["database"] in {"ok", "unavailable"}
