"""Tests unitaires du service d'intégration des tenants (tâche 8).

Isolent la **logique pure** du service, sans base : la validation géographique de
repli (boîte englobante du Togo, P7) et le contrôle de statut du tenant à la
connexion (`is_tenant_active`, exigence 3.4). Les chemins dépendant d'un vrai
moteur (RLS, insertions) sont couverts par `test_admin_router.py` (session
factice) et `test_admin_lifecycle_db.py` (moteur réel).
"""

from __future__ import annotations

import uuid

import pytest

from app.models.app_user import AppUser
from app.models.tenant import Tenant
from app.schemas.admin import GeoPoint
from app.services.admin import (
    CoordinatesOutsideTogoError,
    _validate_coordinates_in_togo,
)
from app.services.auth import is_tenant_active


class _FakeGetSession:
    """Session factice exposant `get(Model, pk)` comme `AsyncSession.get`."""

    def __init__(self, tenant: Tenant | None) -> None:
        self._tenant = tenant

    async def get(self, _model: object, _pk: object) -> Tenant | None:
        return self._tenant


# --- Validation géographique de repli (P7) -------------------------------------


def test_coordonnees_dans_togo_acceptees() -> None:
    """Un point à Lomé (dans la boîte englobante du Togo) est accepté."""
    # Ne lève pas.
    _validate_coordinates_in_togo(GeoPoint(longitude=1.2255, latitude=6.1319))


def test_coordonnees_dans_togo_nord_acceptees() -> None:
    """Un point dans les Savanes (nord) reste dans la boîte englobante."""
    _validate_coordinates_in_togo(GeoPoint(longitude=0.60, latitude=10.85))


@pytest.mark.parametrize(
    "lon,lat",
    [
        (2.35, 48.85),   # Paris : hors Togo
        (1.2255, 5.0),   # au sud du Togo (dans l'océan)
        (-1.0, 7.0),     # à l'ouest du Togo
        (1.2255, 12.0),  # au nord de la frontière
    ],
)
def test_coordonnees_hors_togo_rejetees(lon: float, lat: float) -> None:
    """Des coordonnées hors de la boîte englobante du Togo sont rejetées (P7)."""
    with pytest.raises(CoordinatesOutsideTogoError):
        _validate_coordinates_in_togo(GeoPoint(longitude=lon, latitude=lat))


# --- Contrôle du statut du tenant à la connexion (exigence 3.4) ----------------


def _member(tenant_id: uuid.UUID | None) -> AppUser:
    return AppUser(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        email="user@centre-lome.tg",
        role="clinician" if tenant_id else "platform_admin",
        is_active=True,
        failed_login_count=0,
    )


async def test_is_tenant_active_tenant_actif() -> None:
    """Un membre dont le tenant est actif peut se connecter."""
    tid = uuid.uuid4()
    session = _FakeGetSession(Tenant(id=tid, name="C", region_id=uuid.uuid4(), status="active"))
    assert await is_tenant_active(session, _member(tid)) is True  # type: ignore[arg-type]


async def test_is_tenant_active_tenant_desactive() -> None:
    """Un membre dont le tenant est désactivé est refusé (exigence 3.4)."""
    tid = uuid.uuid4()
    session = _FakeGetSession(
        Tenant(id=tid, name="C", region_id=uuid.uuid4(), status="inactive")
    )
    assert await is_tenant_active(session, _member(tid)) is False  # type: ignore[arg-type]


async def test_is_tenant_active_tenant_absent() -> None:
    """Un tenant introuvable → refus (défense en profondeur)."""
    session = _FakeGetSession(None)
    assert await is_tenant_active(session, _member(uuid.uuid4())) is False  # type: ignore[arg-type]


async def test_is_tenant_active_platform_admin_non_concerne() -> None:
    """Un platform_admin (tenant NUL) n'est pas soumis au statut d'un tenant."""
    session = _FakeGetSession(None)
    assert await is_tenant_active(session, _member(None)) is True  # type: ignore[arg-type]


async def test_is_tenant_active_user_absent() -> None:
    """Pas d'utilisateur → non bloquant ici (le refus vient d'ailleurs)."""
    session = _FakeGetSession(None)
    assert await is_tenant_active(session, None) is True  # type: ignore[arg-type]
