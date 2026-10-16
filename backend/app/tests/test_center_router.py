"""Tests du contrat HTTP du profil de centre (tâche 11.3) — exigence 6.

Vérifient le comportement HTTP du routeur `center` **sans toucher un vrai
PostgreSQL** (même motif que `test_clinical_router.py` / `test_admin_router.py`) :
la session cadrée au tenant (`tenant_session`) et le principal authentifié
(`get_current_principal`) sont remplacés par `app.dependency_overrides`, et la
couche service (`app.services.center`) est stubbée par monkeypatch. On exerce ici
les garanties observables à la **surface HTTP** ; les propriétés dépendant du
moteur (isolation RLS, `ST_Within` géo réel, projection publique) sont vérifiées
contre un vrai moteur dans `test_center_db.py`.

Couvre (design, « Correctness Properties ») :

- **Cadrage par rôle (exigence 6.1).** La **lecture** est ouverte aux rôles cadrés
  au tenant (`tenant_admin`, `clinician`, `staff`) ; la **mise à jour** est
  réservée au `tenant_admin` — `clinician`/`staff`/`platform_admin` → 403.
- **Validité géographique (P7, exigence 6.2).** Des coordonnées hors Togo
  renvoient **422** avec un message clair (le service lève
  `CoordinatesOutsideTogoError`).
- **Tenancy autoritaire côté serveur (P2).** Le schéma de mise à jour n'accepte
  aucun `tenant_id`/`id` : un corps en contenant est ignoré (champs inconnus) et
  le profil renvoyé reste celui du tenant du principal.
- **Profil absent → 404 (P3).** Un tenant sans centre intégré n'expose rien.
- **Mise à jour auditée (NFR-3).** Un `center.update` est émis à la mise à jour.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.core.deps import AuthPrincipal, get_current_principal
from app.core.tenant_context import tenant_session
from app.main import app
from app.routers import center as center_router
from app.schemas.audit import AuditEvent
from app.services.center import (
    CenterNotFoundError,
    CenterProfileData,
    CoordinatesOutsideTogoError,
    UnknownServiceTypeError,
)

_TENANT_ID = uuid.uuid4()
_OTHER_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_CENTER_ID = uuid.uuid4()

# Coordonnées valides à Lomé (dans la frontière du Togo).
_LOME_LON, _LOME_LAT = 1.2255, 6.1319


class _FakeTenantSession:
    """Session factice : absorbe toute exécution (audit, etc.), ne commit rien."""

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


def _profile(**overrides: object) -> CenterProfileData:
    """Construit une projection de profil (valeurs par défaut cohérentes)."""
    base: dict = {
        "id": _CENTER_ID,
        "tenant_id": _TENANT_ID,
        "name": "Centre de santé de Lomé",
        "region_code": "MARITIME",
        "address": "12 Rue du Commerce, Lomé",
        "longitude": _LOME_LON,
        "latitude": _LOME_LAT,
        "phone": "+22890000000",
        "email": "contact@centre-lome.tg",
        "opening_hours": [{"day": "lundi", "open": "08:00", "close": "17:00"}],
        "is_public": True,
        "service_types": ["Maternité", "Pharmacie"],
    }
    base.update(overrides)
    return CenterProfileData(**base)


_principal: AuthPrincipal | None = None


async def _override_tenant_session() -> AsyncIterator[_FakeTenantSession]:
    yield _FakeTenantSession()


async def _override_principal() -> AuthPrincipal:
    assert _principal is not None, "Principal non configuré pour ce test"
    return _principal


@pytest.fixture(autouse=True)
def _reset() -> None:
    global _principal
    _principal = None


@pytest.fixture
def client() -> TestClient:
    """Client FastAPI : session tenant factice + principal injecté (bypass JWT)."""
    app.dependency_overrides[tenant_session] = _override_tenant_session
    app.dependency_overrides[get_current_principal] = _override_principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(tenant_session, None)
        app.dependency_overrides.pop(get_current_principal, None)


@pytest.fixture
def captured_audit(monkeypatch: pytest.MonkeyPatch) -> list[AuditEvent]:
    """Capture les événements d'audit émis par le routeur center (NFR-3)."""
    events: list[AuditEvent] = []

    async def _fake_record(_session: object, event: AuditEvent) -> None:
        events.append(event)

    monkeypatch.setattr(center_router, "record_audit", _fake_record)
    return events


def _set_principal(role: str, *, tenant_id: uuid.UUID | None = _TENANT_ID) -> None:
    global _principal
    tid = None if role == "platform_admin" else tenant_id
    _principal = AuthPrincipal(sub=_ACTOR_ID, tenant_id=tid, role=role)


# --------------------------------------------------------------------------- #
# Lecture : cadrage par rôle + projection                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ["tenant_admin", "clinician", "staff"])
def test_get_profile_ouvert_aux_roles_tenant(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    """GET /profile : accessible aux rôles cadrés au tenant, renvoie la projection."""

    async def _fake_get(_session: object) -> CenterProfileData:
        return _profile()

    monkeypatch.setattr(center_router, "get_center_profile", _fake_get)
    _set_principal(role)

    resp = client.get("/api/center/profile")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == str(_TENANT_ID)
    assert body["region_code"] == "MARITIME"
    assert body["service_types"] == ["Maternité", "Pharmacie"]
    assert body["location"] == {"longitude": _LOME_LON, "latitude": _LOME_LAT}


def test_get_profile_platform_admin_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /profile : un platform_admin (transversal) n'y a pas accès (403)."""

    async def _fake_get(_session: object) -> CenterProfileData:
        return _profile()

    monkeypatch.setattr(center_router, "get_center_profile", _fake_get)
    _set_principal("platform_admin")

    assert client.get("/api/center/profile").status_code == 403


def test_get_profile_sans_centre_404(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /profile : un tenant sans centre intégré → 404 (P3)."""

    async def _fake_get(_session: object) -> CenterProfileData:
        raise CenterNotFoundError

    monkeypatch.setattr(center_router, "get_center_profile", _fake_get)
    _set_principal("tenant_admin")

    assert client.get("/api/center/profile").status_code == 404


# --------------------------------------------------------------------------- #
# Mise à jour : cadrage par rôle (tenant_admin seulement)                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ["clinician", "staff", "platform_admin"])
def test_patch_profile_role_insuffisant_403(
    client: TestClient, role: str
) -> None:
    """PATCH /profile : seul le tenant_admin peut modifier (exigence 6.1) ; sinon 403."""
    _set_principal(role)
    resp = client.patch("/api/center/profile", json={"name": "Nouveau nom"})
    assert resp.status_code == 403


def test_patch_profile_tenant_admin_ok_et_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH /profile (tenant_admin) : met à jour, renvoie la projection, audite."""
    captured_fields: dict = {}

    async def _fake_update(_session: object, *, fields: dict) -> CenterProfileData:
        captured_fields.update(fields)
        return _profile(name="Centre rénové")

    monkeypatch.setattr(center_router, "update_center_profile", _fake_update)
    _set_principal("tenant_admin")

    resp = client.patch(
        "/api/center/profile",
        json={"name": "Centre rénové", "service_types": ["Maternité"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Centre rénové"
    # Seuls les champs fournis sont transmis au service (exclude_unset).
    assert captured_fields == {"name": "Centre rénové", "service_types": ["Maternité"]}
    # La mise à jour est auditée (NFR-3).
    assert [e.action for e in captured_audit] == ["center.update"]
    assert captured_audit[0].tenant_id == _TENANT_ID


def test_patch_profile_coordonnees_hors_togo_422(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH /profile : coordonnées hors Togo → 422 avec message clair (P7)."""

    async def _fake_update(_session: object, *, fields: dict) -> CenterProfileData:
        raise CoordinatesOutsideTogoError

    monkeypatch.setattr(center_router, "update_center_profile", _fake_update)
    _set_principal("tenant_admin")

    resp = client.patch(
        "/api/center/profile",
        json={"location": {"longitude": 2.5, "latitude": 48.85}},  # Paris
    )
    assert resp.status_code == 422, resp.text
    assert "Togo" in resp.json()["detail"]


def test_patch_profile_service_inconnu_422(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH /profile : un type de service inconnu du référentiel → 422."""

    async def _fake_update(_session: object, *, fields: dict) -> CenterProfileData:
        raise UnknownServiceTypeError("Téléportation")

    monkeypatch.setattr(center_router, "update_center_profile", _fake_update)
    _set_principal("tenant_admin")

    resp = client.patch(
        "/api/center/profile",
        json={"service_types": ["Téléportation"]},
    )
    assert resp.status_code == 422, resp.text
    assert "service" in resp.json()["detail"].lower()


def test_patch_profile_ignore_tenant_id_force_dans_le_corps(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH /profile : un `tenant_id` forgé dans le corps est ignoré (P2).

    Le schéma `CenterProfileUpdateRequest` ne porte pas de `tenant_id` : un corps
    en contenant n'a aucun effet (champ inconnu ignoré), et le service ne le reçoit
    jamais. Le profil renvoyé reste rattaché au tenant du principal.
    """
    captured_fields: dict = {}

    async def _fake_update(_session: object, *, fields: dict) -> CenterProfileData:
        captured_fields.update(fields)
        return _profile()

    monkeypatch.setattr(center_router, "update_center_profile", _fake_update)
    _set_principal("tenant_admin")

    resp = client.patch(
        "/api/center/profile",
        json={"name": "X", "tenant_id": str(_OTHER_TENANT_ID), "id": str(uuid.uuid4())},
    )
    assert resp.status_code == 200, resp.text
    assert "tenant_id" not in captured_fields
    assert "id" not in captured_fields
    assert resp.json()["tenant_id"] == str(_TENANT_ID)
