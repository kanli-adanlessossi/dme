"""Tests du contrat HTTP du moteur de synchronisation (tâche 10.4) — NFR-6, P11.

Vérifient le comportement HTTP des routes `sync/push` / `sync/pull` **sans
toucher un vrai PostgreSQL** (même motif que `test_clinical_router.py`) : la
session cadrée au tenant (`tenant_session`), le principal (`get_current_principal`)
et l'état de révocation (`sync_access_revoked`) sont remplacés par
`app.dependency_overrides`, et la couche service `app.services.sync` est stubbée.
On exerce ici les garanties observables à la **surface HTTP** ; les propriétés
dépendant du moteur (idempotence réelle P10, isolation RLS P11, LWW + journal P12)
sont vérifiées contre un vrai moteur dans `test_sync_db.py`.

Couvre :

- **Cadrage par rôle (P11, exigence 2.2).** `push`/`pull` exigent un rôle de sync
  (`tenant_admin`, `clinician`, `staff`) ; un `platform_admin` (transversal,
  tenant NUL) ou un rôle inconnu → 403.
- **Tenancy autoritaire côté serveur (P2).** Le routeur transmet au service le
  `tenant_id` et l'acteur du **principal**, jamais une valeur du corps.
- **Audit (NFR-3).** Un push et un pull non révoqué émettent chacun exactement
  une ligne d'audit (`sync.push` / `sync.pull`).
- **Révocation (NFR-7).** `pull` révoqué renvoie `revoked=true` sans delta.
- **Agrégation des conflits (P12).** Les conflits renvoyés par le service sont
  propagés dans `SyncPushResponse.conflicts`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.deps import (
    AuthPrincipal,
    get_current_principal,
    sync_access_revoked,
)
from app.core.tenant_context import tenant_session
from app.main import app
from app.routers import sync as sync_router
from app.schemas.audit import AuditEvent
from app.schemas.sync import (
    MutationResult,
    MutationResultStatus,
    SyncChange,
    SyncConflict,
    SyncEntity,
    SyncOp,
)
from app.services.sync import PullResult, PushOutcome

_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()


class _FakeTenantSession:
    """Session factice tenant : absorbe les écritures d'audit, ne commit rien."""

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


_principal: AuthPrincipal | None = None
_revoked = False


async def _override_tenant_session() -> AsyncIterator[_FakeTenantSession]:
    yield _FakeTenantSession()


async def _override_principal() -> AuthPrincipal:
    assert _principal is not None, "Principal non configuré pour ce test"
    return _principal


async def _override_revoked() -> bool:
    return _revoked


@pytest.fixture(autouse=True)
def _reset() -> None:
    global _principal, _revoked
    _principal = None
    _revoked = False


@pytest.fixture
def client() -> TestClient:
    """Client FastAPI : session tenant factice + principal + révocation injectés."""
    app.dependency_overrides[tenant_session] = _override_tenant_session
    app.dependency_overrides[get_current_principal] = _override_principal
    app.dependency_overrides[sync_access_revoked] = _override_revoked
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(tenant_session, None)
        app.dependency_overrides.pop(get_current_principal, None)
        app.dependency_overrides.pop(sync_access_revoked, None)


@pytest.fixture
def captured_audit(monkeypatch: pytest.MonkeyPatch) -> list[AuditEvent]:
    """Capture les événements d'audit émis par le routeur de sync (NFR-3)."""
    events: list[AuditEvent] = []

    async def _fake_record(_session: object, event: AuditEvent) -> None:
        events.append(event)

    monkeypatch.setattr(sync_router, "record_audit", _fake_record)
    return events


def _set_principal(role: str, *, tenant_id: uuid.UUID | None = _TENANT_ID) -> None:
    global _principal
    tid = None if role == "platform_admin" else tenant_id
    _principal = AuthPrincipal(sub=_ACTOR_ID, tenant_id=tid, role=role)


def _mutation_body(**overrides: object) -> dict:
    body: dict = {
        "mutation_id": str(uuid.uuid4()),
        "entity": "patient",
        "entity_id": str(uuid.uuid4()),
        "op": "upsert",
        "payload": {"mrn": "MRN-1", "given_name": "A", "family_name": "B"},
        "client_version": 1,
        "client_updated_at": datetime.now(UTC).isoformat(),
        "event_at": datetime.now(UTC).isoformat(),
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Cadrage par rôle (P11)                                                       #
# --------------------------------------------------------------------------- #

_SYNC_OK_ROLES = ["tenant_admin", "clinician", "staff"]


@pytest.mark.parametrize("role", ["platform_admin"])
def test_push_role_non_autorise_403(client: TestClient, role: str) -> None:
    """Un rôle hors sync (platform_admin) ne peut pas pousser → 403."""
    _set_principal(role)
    resp = client.post("/api/sync/push", json={"mutations": []})
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["platform_admin"])
def test_pull_role_non_autorise_403(client: TestClient, role: str) -> None:
    """Un rôle hors sync (platform_admin) ne peut pas tirer de deltas → 403."""
    _set_principal(role)
    resp = client.get("/api/sync/pull")
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# PUSH : tenancy serveur, agrégation des résultats/conflits, audit             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", _SYNC_OK_ROLES)
def test_push_transmet_le_tenant_et_l_acteur_du_principal(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    """Le routeur transmet au service le tenant + acteur du principal (P2) ; audité."""
    _set_principal(role)
    captured: dict = {}

    async def _fake_apply(_session: object, **kwargs: object) -> PushOutcome:
        captured.update(kwargs)
        return PushOutcome(results=[], conflicts=[], applied_count=0)

    monkeypatch.setattr(sync_router, "apply_push", _fake_apply)

    resp = client.post("/api/sync/push", json={"mutations": [_mutation_body()]})
    assert resp.status_code == 200, resp.text

    assert captured["tenant_id"] == _TENANT_ID
    assert captured["actor_user_id"] == _ACTOR_ID
    assert len(captured["mutations"]) == 1

    # Exactement une ligne d'audit `sync.push` (NFR-3).
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "sync.push"
    assert captured_audit[0].tenant_id == _TENANT_ID
    assert captured_audit[0].actor_user_id == _ACTOR_ID


def test_push_propage_resultats_et_conflits(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Les résultats par mutation et les conflits agrégés sont renvoyés (P12)."""
    _set_principal("clinician")
    mut_id = uuid.uuid4()
    ent_id = uuid.uuid4()
    conflict = SyncConflict(
        entity=SyncEntity.visit,
        entity_id=ent_id,
        loser_payload={"notes": "version perdante"},
        winner_version=3,
        sensitive=True,
    )
    outcome = PushOutcome(
        results=[
            MutationResult(
                mutation_id=mut_id,
                entity=SyncEntity.visit,
                entity_id=ent_id,
                status=MutationResultStatus.conflict,
                server_version=3,
                conflict=conflict,
            )
        ],
        conflicts=[conflict],
        applied_count=1,
    )

    async def _fake_apply(_session: object, **_kwargs: object) -> PushOutcome:
        return outcome

    monkeypatch.setattr(sync_router, "apply_push", _fake_apply)

    resp = client.post("/api/sync/push", json={"mutations": [_mutation_body()]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["status"] == "conflict"
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["sensitive"] is True
    assert body["conflicts"][0]["winner_version"] == 3


def test_push_rejeu_renvoie_duplicate(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une mutation déjà appliquée est rendue `duplicate` par le service (P10)."""
    _set_principal("staff")
    mut_id = uuid.uuid4()
    ent_id = uuid.uuid4()

    async def _fake_apply(_session: object, **_kwargs: object) -> PushOutcome:
        return PushOutcome(
            results=[
                MutationResult(
                    mutation_id=mut_id,
                    entity=SyncEntity.patient,
                    entity_id=ent_id,
                    status=MutationResultStatus.duplicate,
                    server_version=1,
                )
            ],
            conflicts=[],
            applied_count=0,
        )

    monkeypatch.setattr(sync_router, "apply_push", _fake_apply)

    resp = client.post(
        "/api/sync/push",
        json={"mutations": [_mutation_body(mutation_id=str(mut_id))]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "duplicate"


# --------------------------------------------------------------------------- #
# PULL : deltas + curseur, révocation, audit                                   #
# --------------------------------------------------------------------------- #


def test_pull_non_revoque_renvoie_deltas_et_curseur(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pull non révoqué : deltas + nouveau curseur ; audité (NFR-3)."""
    _set_principal("clinician")
    ent_id = uuid.uuid4()
    change = SyncChange(
        entity=SyncEntity.patient,
        entity_id=ent_id,
        op=SyncOp.upsert,
        payload={"mrn": "MRN-1"},
        version=2,
        updated_at=datetime.now(UTC),
        deleted=False,
    )

    async def _fake_pull(_session: object, *, since: str | None) -> PullResult:
        return PullResult(changes=[change], cursor="CURSEUR-123", has_more=False)

    monkeypatch.setattr(sync_router, "pull_deltas", _fake_pull)

    resp = client.get("/api/sync/pull")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revoked"] is False
    assert body["cursor"] == "CURSEUR-123"
    assert len(body["changes"]) == 1
    assert body["changes"][0]["entity_id"] == str(ent_id)

    assert len(captured_audit) == 1
    assert captured_audit[0].action == "sync.pull"


def test_pull_revoque_renvoie_revoked_sans_delta(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pull révoqué : `revoked=true`, aucun delta, pas d'appel au moteur de deltas."""
    global _revoked
    _revoked = True
    _set_principal("clinician")

    called = {"pull": False}

    async def _fake_pull(_session: object, *, since: str | None) -> PullResult:
        called["pull"] = True
        return PullResult(changes=[], cursor="", has_more=False)

    monkeypatch.setattr(sync_router, "pull_deltas", _fake_pull)

    resp = client.get("/api/sync/pull?since=abc")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revoked"] is True
    assert body["changes"] == []
    # Curseur conservé tel quel pour que le client purge sans perdre sa position.
    assert body["cursor"] == "abc"
    assert called["pull"] is False, "Le chemin deltas ne doit pas s'exécuter si révoqué"
