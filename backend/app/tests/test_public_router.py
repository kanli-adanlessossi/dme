"""Tests du contrat HTTP de l'annuaire public + carte (tâche 12.3) — exigences 7, 8.

Vérifient le comportement HTTP du routeur `public` **sans toucher un vrai
PostgreSQL** (même motif que `test_center_router.py` / `test_clinical_router.py`) :
la session de lecture publique (`public_session`) est remplacée par une session
factice via `app.dependency_overrides`, et la couche service
(`app.services.public`) est stubbée par monkeypatch. On exerce ici les garanties
observables à la **surface HTTP** ; les propriétés dépendant du moteur
(exclusion RLS des tenants inactifs, projection sûre réelle) sont vérifiées contre
un vrai moteur dans `test_public_db.py`.

Couvre (design, « Correctness Properties ») :

- **P4 — Confidentialité des PHI / champs privés.** Les réponses publiques
  n'exposent QUE des champs de profil non sensibles ; aucun `tenant_id`, statut,
  `is_public`, ni donnée PHI ne figure dans le schéma de réponse (exigence 8.3).
- **Région vide → liste vide (exigences 7.3, 8.1).** Lister une région sans centre
  renvoie `[]` (code 200), jamais une erreur.
- **Centre inconnu/inactif → 404 (P8).** Un id invisible côté public donne 404.
- **GeoJSON des régions (exigence 7.6).** `GET /geo/togo-regions.geojson` renvoie
  une `FeatureCollection`.
- **Pas d'auth requise.** Les endpoints publics sont accessibles sans jeton.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.core.tenant_context import public_session
from app.main import app
from app.routers import public as public_router
from app.services.public import (
    CenterNotFoundError,
    PublicCenterData,
    PublicRegionData,
)

_CENTER_ID = uuid.uuid4()
_LOME_LON, _LOME_LAT = 1.2255, 6.1319

# Champs internes/sensibles qui ne doivent JAMAIS apparaître dans une réponse
# publique (P4, exigence 8.3) : identité du tenant, statut, visibilité, PHI.
_FORBIDDEN_PUBLIC_FIELDS = frozenset(
    {
        "tenant_id",
        "status",
        "is_public",
        "created_by",
        "created_at",
        "updated_at",
        "deleted_at",
        "mrn",
        "national_id",
        "given_name",
        "family_name",
        "dob",
        "sex",
        "consent_to_store",
        "patient_id",
        "notes",
        "password_hash",
        "setup_token_hash",
    }
)


class _FakePublicSession:
    """Session factice : absorbe toute exécution, ne touche pas la base."""

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


async def _override_public_session() -> AsyncIterator[_FakePublicSession]:
    yield _FakePublicSession()


def _center(**overrides: object) -> PublicCenterData:
    """Construit une projection publique de centre (valeurs cohérentes)."""
    base: dict = {
        "id": _CENTER_ID,
        "name": "Centre de santé de Lomé",
        "region_code": "MARITIME",
        "address": "12 Rue du Commerce, Lomé",
        "longitude": _LOME_LON,
        "latitude": _LOME_LAT,
        "phone": "+22890000000",
        "email": "contact@centre-lome.tg",
        "opening_hours": [{"day": "lundi", "open": "08:00", "close": "17:00"}],
        "service_types": ["Maternité", "Pharmacie"],
    }
    base.update(overrides)
    return PublicCenterData(**base)


@pytest.fixture
def client() -> TestClient:
    """Client FastAPI : session de lecture publique factice (aucune base)."""
    app.dependency_overrides[public_session] = _override_public_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(public_session, None)


# --------------------------------------------------------------------------- #
# GET /api/public/regions                                                      #
# --------------------------------------------------------------------------- #


def test_list_regions_sans_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /regions : accessible sans auth, renvoie les régions + décompte."""

    async def _fake(_session: object) -> list[PublicRegionData]:
        return [
            PublicRegionData(code="MARITIME", name_fr="Maritime", center_count=3),
            PublicRegionData(code="KARA", name_fr="Kara", center_count=0),
        ]

    monkeypatch.setattr(public_router, "list_public_regions", _fake)

    resp = client.get("/api/public/regions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body[0] == {"code": "MARITIME", "name_fr": "Maritime", "center_count": 3}
    # Région sans centre : décompte 0 (pas d'erreur).
    assert body[1]["center_count"] == 0


# --------------------------------------------------------------------------- #
# GET /api/public/regions/{code}/centers                                       #
# --------------------------------------------------------------------------- #


def test_list_region_centers_projection_sure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /regions/{code}/centers : ne renvoie que des champs publics (P4)."""

    async def _fake(_session: object, _code: str) -> list[PublicCenterData]:
        return [_center()]

    monkeypatch.setattr(public_router, "list_region_centers", _fake)

    resp = client.get("/api/public/regions/MARITIME/centers")
    assert resp.status_code == 200, resp.text
    summary = resp.json()[0]
    assert summary["id"] == str(_CENTER_ID)
    assert summary["region_code"] == "MARITIME"
    assert summary["location"] == {"longitude": _LOME_LON, "latitude": _LOME_LAT}
    # Aucun champ interne/sensible exposé (P4).
    assert not (_FORBIDDEN_PUBLIC_FIELDS & summary.keys())


def test_region_vide_renvoie_liste_vide(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /regions/{code}/centers : région sans centre → [] (exigences 7.3, 8.1)."""

    async def _fake(_session: object, _code: str) -> list[PublicCenterData]:
        return []

    monkeypatch.setattr(public_router, "list_region_centers", _fake)

    resp = client.get("/api/public/regions/KARA/centers")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_region_code_non_canonique_422(client: TestClient) -> None:
    """GET /regions/{code}/centers : un code hors des 5 régions est rejeté (422)."""
    resp = client.get("/api/public/regions/ATLANTIQUE/centers")
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# GET /api/public/centers/{id}                                                 #
# --------------------------------------------------------------------------- #


def test_get_center_projection_sure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /centers/{id} : profil public complet, sans champ sensible (P4, 7.4)."""

    async def _fake(_session: object, _cid: uuid.UUID) -> PublicCenterData:
        return _center()

    monkeypatch.setattr(public_router, "get_public_center", _fake)

    resp = client.get(f"/api/public/centers/{_CENTER_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Champs publics attendus (exigence 7.4).
    assert body["name"] == "Centre de santé de Lomé"
    assert body["phone"] == "+22890000000"
    assert body["email"] == "contact@centre-lome.tg"
    assert body["service_types"] == ["Maternité", "Pharmacie"]
    assert body["location"] == {"longitude": _LOME_LON, "latitude": _LOME_LAT}
    # Aucun champ interne/sensible exposé (P4).
    assert not (_FORBIDDEN_PUBLIC_FIELDS & body.keys())


def test_get_center_inconnu_ou_inactif_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /centers/{id} : centre inconnu/inactif → 404 (indiscernables, P8)."""

    async def _fake(_session: object, _cid: uuid.UUID) -> PublicCenterData:
        raise CenterNotFoundError

    monkeypatch.setattr(public_router, "get_public_center", _fake)

    resp = client.get(f"/api/public/centers/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_center_coordonnees_absentes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /centers/{id} : un centre sans coordonnées renvoie location null."""

    async def _fake(_session: object, _cid: uuid.UUID) -> PublicCenterData:
        return _center(longitude=None, latitude=None, address=None)

    monkeypatch.setattr(public_router, "get_public_center", _fake)

    resp = client.get(f"/api/public/centers/{_CENTER_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["location"] is None
    assert body["address"] is None


# --------------------------------------------------------------------------- #
# GET /api/public/geo/togo-regions.geojson                                     #
# --------------------------------------------------------------------------- #


def test_geojson_des_regions(client: TestClient) -> None:
    """GET /geo/togo-regions.geojson : renvoie une FeatureCollection des 5 régions.

    Sert le vrai fichier livré (`data/togo-regions.geojson`) : donnée publique de
    référence, aucune base. On vérifie la forme GeoJSON standard et la présence
    des 5 régions canoniques (exigence 7.6).
    """
    resp = client.get("/api/public/geo/togo-regions.geojson")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 5
    codes = {f["properties"]["code"] for f in body["features"]}
    assert codes == {"SAVANES", "KARA", "CENTRALE", "PLATEAUX", "MARITIME"}
