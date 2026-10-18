"""Tests de la configuration CORS côté API (G-5, tâche 5.2).

Vérifie le DoD de la tâche 5.2 : le pré-vol CORS n'annonce JAMAIS de joker
(`*`) pour les origines, méthodes ou en-têtes, et le frontend local-first
(Vite, http://localhost:5173) reste fonctionnel dans son usage réel —
Bearer + JSON en cross-origin (GET/POST/PATCH avec `Authorization` et
`Content-Type`). Un pré-vol émis depuis une origine non autorisée n'obtient
pas d'en-tête `Access-Control-Allow-Origin` qui l'écho : il échoue côté
navigateur comme auparavant.

Les tests sont volontairement SANS base de données et indépendants du
limiteur de débit : on cible `/health` (pas de base requise, hors périmètre
du limiteur) pour la requête simple, et les pré-vols `OPTIONS` ne sont de
toute façon pas limités.

Validates: Requirements 5, NFR-2.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_ALLOWED_ORIGIN = "http://localhost:5173"
_DISALLOWED_ORIGIN = "https://evil.example"


def _preflight(
    method: str,
    *,
    origin: str = _ALLOWED_ORIGIN,
    request_headers: str = "authorization, content-type",
    path: str = "/health",
) -> object:
    """Émet un pré-vol CORS (OPTIONS) et renvoie la réponse."""
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": request_headers,
        },
    )


def test_prevol_echo_origine_sans_joker() -> None:
    """Le pré-vol réussit et écho l'origine précise, sans aucun joker `*` (G-5)."""
    response = _preflight("POST")

    # Starlette renvoie 200 pour un pré-vol autorisé.
    assert response.status_code == 200

    allow_origin = response.headers.get("Access-Control-Allow-Origin")
    allow_methods = response.headers.get("Access-Control-Allow-Methods")
    allow_headers = response.headers.get("Access-Control-Allow-Headers")

    # L'origine est écho explicitement, jamais `*`.
    assert allow_origin == _ALLOWED_ORIGIN

    # Les méthodes du frontend sont annoncées ; au minimum POST, idéalement tout.
    assert allow_methods is not None
    methods = {m.strip().upper() for m in allow_methods.split(",")}
    for expected in ("GET", "POST", "PATCH", "DELETE", "OPTIONS"):
        assert expected in methods

    # Les en-têtes de requête du frontend sont couverts (comparaison insensible
    # à la casse).
    assert allow_headers is not None
    headers_lower = {h.strip().lower() for h in allow_headers.split(",")}
    assert "authorization" in headers_lower
    assert "content-type" in headers_lower

    # Credentials autorisés (Bearer + cookies éventuels).
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"

    # DoD « sans joker » : aucun `*` dans origine / méthodes / en-têtes.
    assert "*" not in allow_origin
    assert "*" not in allow_methods
    assert "*" not in allow_headers


def test_frontend_bearer_json_patch_autorise() -> None:
    """Le pré-vol du PATCH Bearer+JSON du frontend est autorisé (ex. PATCH /api/center/profile)."""
    response = _preflight("PATCH")

    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == _ALLOWED_ORIGIN

    allow_methods = response.headers.get("Access-Control-Allow-Methods")
    assert allow_methods is not None
    methods = {m.strip().upper() for m in allow_methods.split(",")}
    assert "PATCH" in methods
    assert "*" not in allow_methods

    allow_headers = response.headers.get("Access-Control-Allow-Headers")
    assert allow_headers is not None
    headers_lower = {h.strip().lower() for h in allow_headers.split(",")}
    assert "authorization" in headers_lower
    assert "content-type" in headers_lower
    assert "*" not in allow_headers

    assert response.headers.get("Access-Control-Allow-Credentials") == "true"


def test_requete_simple_porte_les_entetes_cors() -> None:
    """Une requête réelle cross-origin obtient l'écho de l'origine + credentials (sans base)."""
    response = client.get("/health", headers={"Origin": _ALLOWED_ORIGIN})

    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == _ALLOWED_ORIGIN
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"

    # La réponse reste exploitable par le frontend (corps intact).
    body = response.json()
    assert body["status"] == "ok"


def test_origine_non_autorisee_non_echoyee() -> None:
    """Un pré-vol depuis une origine non autorisée n'écho pas cette origine (échoue côté navigateur)."""
    response = _preflight("POST", origin=_DISALLOWED_ORIGIN)

    allow_origin = response.headers.get("Access-Control-Allow-Origin")
    # L'origine malveillante n'est jamais écho, et aucun joker ne la couvre.
    assert allow_origin != _DISALLOWED_ORIGIN
    assert allow_origin != "*"
