"""Tests du contrat d'API figé (tâche 3) — NFR-13, exigences 1.3, 4.3.

Vérifie que `/openapi.json` est généré sans erreur et documente de façon stable
l'ensemble des chemins et des réponses d'erreur du contrat, avant tout travail
frontend. Les handlers sont des stubs (501) ; on teste ici le **contrat**, pas la
logique métier.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# Ensemble figé des chemins du contrat (tâche 3). Toute dérive casse ce test.
EXPECTED_PATHS: set[str] = {
    # Transversal (tâche 2)
    "/health",
    # Auth
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/set-credentials",
    # Admin plateforme
    "/api/admin/tenants",
    "/api/admin/tenants/{tenant_id}",
    # Centre (cadré tenant)
    "/api/center/profile",
    # Patients (cadré tenant, PHI)
    "/api/patients",
    "/api/patients/{patient_id}",
    # Visites (cadré tenant, PHI)
    "/api/visits",
    "/api/visits/{visit_id}",
    # Synchronisation
    "/api/sync/push",
    "/api/sync/pull",
    # Public (projection sûre)
    "/api/public/regions",
    "/api/public/regions/{code}/centers",
    "/api/public/centers/{center_id}",
    "/api/public/geo/togo-regions.geojson",
}


def _openapi() -> dict:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def test_openapi_genere_sans_erreur() -> None:
    """`/openapi.json` est produit et porte un titre (schéma valide)."""
    schema = _openapi()
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"]


def test_openapi_contient_tous_les_chemins() -> None:
    """Tous les chemins du contrat figé sont présents dans le schéma OpenAPI."""
    paths = set(_openapi()["paths"].keys())
    missing = EXPECTED_PATHS - paths
    assert not missing, f"Chemins manquants dans OpenAPI : {sorted(missing)}"


def test_routes_enregistrees_et_atteignables() -> None:
    """Chaque endpoint du contrat est atteignable (jamais 404 « route absente »).

    Les handlers sont des stubs : un appel renvoie 501 (contrat figé) ou une
    erreur de validation 422, mais jamais le 404 d'une route non enregistrée.
    Depuis la tâche 7.5, les endpoints cadrés au tenant exigent une
    authentification : l'injection de `require_roles` s'exécute avant le stub,
    si bien qu'un appel non authentifié à `POST /api/sync/push` renvoie 401
    (et non le stub 501/422). Un 401 prouve tout autant que la route est
    enregistrée — ce n'est jamais le 404 d'une route absente. La couverture
    exhaustive des chemins est assurée par le schéma OpenAPI servi (test
    ci-dessus), source d'autorité du contrat.
    """
    # Endpoint public (sans auth) ne touchant PAS la base : le GeoJSON des
    # régions est servi depuis un fichier statique. Un 200 prouve que la route est
    # enregistrée et atteignable sans authentification (jamais 404/405). Les
    # endpoints publics lisant la base (`/regions`, `/centers/{id}`) sont couverts
    # par les tests d'intégration (`test_public_db.py`) ; leur enregistrement est
    # garanti par le schéma OpenAPI (test ci-dessus), source d'autorité du contrat.
    resp = client.get("/api/public/geo/togo-regions.geojson")
    assert resp.status_code == 200
    # Endpoint cadré au tenant : un appel non authentifié renvoie 401 (l'auth
    # précède le stub), ce qui prouve que la route existe (jamais 404/405).
    resp = client.post("/api/sync/push", json={})
    assert resp.status_code == 401


def test_contrat_erreur_404_403_sur_endpoints_tenant() -> None:
    """Les endpoints cadrés au tenant documentent 403 et 404 (contrat P3)."""
    paths = _openapi()["paths"]
    tenant_scoped = [
        ("/api/center/profile", "get"),
        ("/api/patients", "get"),
        ("/api/patients/{patient_id}", "get"),
        ("/api/visits", "get"),
        ("/api/sync/push", "post"),
        ("/api/sync/pull", "get"),
    ]
    for path, method in tenant_scoped:
        responses = paths[path][method]["responses"]
        assert "403" in responses, f"{method.upper()} {path} doit documenter 403"
        assert "404" in responses, f"{method.upper()} {path} doit documenter 404"


def test_login_documente_429() -> None:
    """Le login documente 429 (verrouillage après échecs répétés, NFR-2)."""
    responses = _openapi()["paths"]["/api/auth/login"]["post"]["responses"]
    assert "429" in responses


def test_set_credentials_documente_400_et_410() -> None:
    """set-credentials documente 400/410 (jeton invalide/expiré/consommé)."""
    responses = _openapi()["paths"]["/api/auth/set-credentials"]["post"]["responses"]
    assert "400" in responses
    assert "410" in responses


def test_endpoints_valides_documentent_422() -> None:
    """Les endpoints à corps/paramètre validé exposent 422 (détail Pydantic)."""
    paths = _openapi()["paths"]
    assert "422" in paths["/api/auth/login"]["post"]["responses"]
    assert "422" in paths["/api/patients"]["post"]["responses"]
    assert "422" in paths["/api/sync/push"]["post"]["responses"]


def test_tenant_id_absent_des_corps_de_creation() -> None:
    """`tenant_id` n'est jamais un champ accepté en création (P2)."""
    schemas = _openapi()["components"]["schemas"]
    for model in ("PatientCreateRequest", "VisitCreateRequest", "TenantCreateRequest"):
        props = schemas[model].get("properties", {})
        assert "tenant_id" not in props, f"{model} ne doit pas accepter tenant_id"


def test_mutation_envelope_contient_le_contrat_de_sync() -> None:
    """L'enveloppe de mutation fige les champs du protocole de sync (NFR-13)."""
    props = _openapi()["components"]["schemas"]["Mutation"]["properties"]
    for field in (
        "mutation_id",
        "entity",
        "entity_id",
        "op",
        "payload",
        "client_version",
        "client_updated_at",
        "event_at",
    ):
        assert field in props, f"Mutation doit figer le champ {field}"


def test_conflit_de_sync_fige_le_drapeau_sensible() -> None:
    """Le schéma de conflit fige `sensitive` pour la revue humaine (P12, NFR-6)."""
    props = _openapi()["components"]["schemas"]["SyncConflict"]["properties"]
    for field in ("entity", "entity_id", "loser_payload", "winner_version", "sensitive"):
        assert field in props, f"SyncConflict doit figer le champ {field}"


def test_pull_expose_le_curseur() -> None:
    """La réponse de pull fige un curseur opaque (delta-sync, NFR-13)."""
    props = _openapi()["components"]["schemas"]["SyncPullResponse"]["properties"]
    assert "cursor" in props
    assert "changes" in props
