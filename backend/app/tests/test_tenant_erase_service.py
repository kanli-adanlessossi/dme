"""Tests unitaires des garde-fous de l'effacement par tenant (spec
`tenant-data-rights`, #2 — tâche 6).

Isolent la **logique de garde pure** de `services/tenant_data.erase_tenant`, sans
base : tenant inexistant → `TenantNotFoundError` (→ 404) et tenant encore actif →
`TenantStillActiveError` (→ 409), **tous deux sans aucun effet de bord** (aucun
DELETE exécuté). Le cadrage RLS et l'ordre d'effacement réel (voie (ii) de l'ADR-1)
sous moteur PostGIS sont couverts par les tests d'intégration (tâche 8).

Propriétés visées :

- **Exigence 2.6 / 2.7.** Les deux refus surviennent AVANT toute suppression :
  on prouve ici qu'aucune requête `DELETE`/`set_config` n'est exécutée quand le
  garde-fou s'active.
- Le chemin nominal (tenant inactif) est vérifié jusqu'au rapport : l'ordre des
  DELETE respecte les FK RESTRICT et l'audit n'est jamais ciblé.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.models.tenant import Tenant
from app.services.tenant_data import (
    TenantNotFoundError,
    TenantStillActiveError,
    erase_tenant,
)


class _FakeResult:
    """Résultat factice exposant `rowcount` comme un `CursorResult`."""

    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeEraseSession:
    """Session factice : renvoie un tenant via `get`, capture chaque `execute`.

    `execute` renvoie un `rowcount` fixe (1) par défaut pour permettre au chemin
    nominal de produire un rapport ; chaque instruction SQL est capturée pour
    inspection (ordre d'effacement, non-ciblage de `audit_log`).
    """

    def __init__(self, tenant: Tenant | None) -> None:
        self._tenant = tenant
        self.executed: list[str] = []

    async def get(self, _model: object, _pk: object) -> Tenant | None:
        return self._tenant

    async def execute(self, statement: object, _params: object = None) -> _FakeResult:
        self.executed.append(str(statement))
        return _FakeResult(1)


def _tenant(status: str) -> Tenant:
    return Tenant(
        id=uuid.uuid4(),
        name="Centre test",
        region_id=uuid.uuid4(),
        status=status,
        created_at=datetime.now(UTC),
    )


# --- Garde-fous : aucun effet de bord (exigences 2.6, 2.7) ---------------------


async def test_erase_tenant_inconnu_leve_not_found_sans_effet() -> None:
    """Tenant absent → `TenantNotFoundError`, aucune requête exécutée (2.7)."""
    session = _FakeEraseSession(tenant=None)

    with pytest.raises(TenantNotFoundError):
        await erase_tenant(session, uuid.uuid4())  # type: ignore[arg-type]

    assert session.executed == []


async def test_erase_tenant_actif_leve_still_active_sans_effet() -> None:
    """Tenant encore actif → `TenantStillActiveError`, aucun DELETE (2.6)."""
    session = _FakeEraseSession(tenant=_tenant(status="active"))

    with pytest.raises(TenantStillActiveError):
        await erase_tenant(session, uuid.uuid4())  # type: ignore[arg-type]

    assert session.executed == []


# --- Chemin nominal (tenant inactif) : ordre FK et audit préservé --------------


async def test_erase_tenant_inactif_ordre_et_rapport() -> None:
    """Tenant inactif → effacement dans l'ordre FK imposé ; audit jamais ciblé."""
    tenant = _tenant(status="inactive")
    session = _FakeEraseSession(tenant=tenant)

    report = await erase_tenant(session, tenant.id)  # type: ignore[arg-type]

    # Rapport : invariant ADR-1 + portée par table.
    assert report.tenant_id == tenant.id
    assert report.audit_preserved is True
    assert report.exported_first is False
    assert set(report.erased_counts) == {
        "visit",
        "patient",
        "medical_center_service",
        "sync_conflict",
        "sync_mutation",
        "refresh_token",
        "app_user",
        "medical_center",
        "tenant",
    }

    joined = "\n".join(session.executed).lower()
    # L'audit n'est JAMAIS touché par l'effacement (ADR-1, exigence 2.3).
    assert "audit_log" not in joined
    # Le contexte RLS est posé avant les DELETE (cadrage au tenant, 2.4).
    assert any("set_config" in stmt for stmt in session.executed)

    # Ordre imposé par les FK RESTRICT : les enfants avant les parents. On repère
    # chaque DELETE par le début de son instruction (match exact de table, pour ne
    # pas confondre `medical_center` et `medical_center_service`).
    def _pos(table: str) -> int:
        prefix = f"delete from {table}"
        for i, stmt in enumerate(session.executed):
            head = stmt.lower().strip()
            if head == prefix or head.startswith(prefix + " "):
                return i
        raise AssertionError(f"instruction absente : DELETE FROM {table}")

    assert _pos("visit") < _pos("patient")
    assert _pos("patient") < _pos("app_user")
    assert _pos("refresh_token") < _pos("app_user")
    assert _pos("app_user") < _pos("medical_center")
    assert _pos("medical_center") < _pos("tenant")
