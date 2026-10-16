"""Tests d'intégration du moteur de synchronisation (tâche 10.4) — NFR-6, P10/P11/P12.

Comme `test_clinical_db.py` / `test_rls_isolation.py`, ce module exécute les
**migrations réelles** (dont la `0007_sync_engine` qui crée `sync_mutation` /
`sync_conflict` sous RLS forcée), exerce le rôle applicatif **non-superuser
`dme_app`** (NOBYPASSRLS) et câble l'app FastAPI sur ce moteur. C'est la seule
façon de prouver des propriétés **du moteur** :

- **P10 — synchronisation idempotente et sans perte.** Rejouer tout ou partie de
  l'outbox (coupure en cours de push) ne crée ni doublon ni corruption : chaque
  mutation est appliquée **au plus une fois** (clé = `mutation_id`). Les rejeux
  sont rendus `duplicate`.
- **P11 — isolation maintenue à la sync.** `pull` ne renvoie que les données du
  tenant de l'appelant ; `push` écrit sous `WITH CHECK` RLS → un client ne peut
  ni lire ni injecter les données d'un autre tenant via la sync (le `tenant_id`
  est apposé par le serveur depuis le principal, jamais par le client).
- **P12 — résolution de conflits non destructive.** Un conflit résout en LWW et
  consigne le perdant dans `sync_conflict` (jamais détruit) ; une entité sensible
  (notes de visite) est **marquée pour revue** (`sensitive=true`) et jamais
  écrasée silencieusement.

Dispositif identique aux autres tests d'intégration (voir `test_clinical_db.py`).
Garde CI : désactivé si `TEST_DATABASE_URL` est absente.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.deps import AuthPrincipal, get_current_principal, sync_access_revoked
from app.core.tenant_context import tenant_session
from app.main import app

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : tests d'intégration de sync "
        "désactivés (nécessitent un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "sync_test_" + uuid.uuid4().hex

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
CENTER_A = uuid.uuid4()
CENTER_B = uuid.uuid4()
USER_A = uuid.uuid4()
USER_B = uuid.uuid4()


def _app_role_url(owner_url: str, password: str) -> str:
    """Dérive la DSN du rôle `dme_app` depuis la DSN propriétaire."""
    parts = urlsplit(owner_url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{_APP_ROLE}:{password}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run_migrations(owner_url: str) -> None:
    """Exécute `alembic upgrade head` (sous-processus, connexion owner)."""
    parts = urlsplit(owner_url)
    env = dict(os.environ)
    env.update(
        {
            "POSTGRES_HOST": parts.hostname or "localhost",
            "POSTGRES_PORT": str(parts.port or 5432),
            "POSTGRES_DB": (parts.path or "/").lstrip("/"),
            "POSTGRES_USER": parts.username or "",
            "POSTGRES_PASSWORD": parts.password or "",
            "APP_DB_USER": "",
            "APP_DB_PASSWORD": "",
        }
    )
    backend_dir = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Échec de `alembic upgrade head` sur la base de test :\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


async def _seed(owner_conn: AsyncConnection) -> None:
    """Amorce région + 2 tenants + 1 centre + 1 utilisateur par tenant (owner)."""
    region_id = uuid.uuid4()
    await owner_conn.execute(
        text("INSERT INTO region (id, code, name_fr) VALUES (:id, :code, :name)"),
        {"id": region_id, "code": f"TEST-{uuid.uuid4().hex[:6]}", "name": "Région test"},
    )
    for tenant_id, name in ((TENANT_A, "Tenant A"), (TENANT_B, "Tenant B")):
        await owner_conn.execute(
            text(
                "INSERT INTO tenant (id, name, region_id, status) "
                "VALUES (:id, :name, :region_id, 'active')"
            ),
            {"id": tenant_id, "name": name, "region_id": region_id},
        )
    for tenant_id, center_id, user_id, label in (
        (TENANT_A, CENTER_A, USER_A, "A"),
        (TENANT_B, CENTER_B, USER_B, "B"),
    ):
        await owner_conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, false)"),
            {"tid": str(tenant_id)},
        )
        await owner_conn.execute(
            text(
                "INSERT INTO medical_center (id, tenant_id, name, region_id) "
                "VALUES (:id, :tenant_id, :name, :region_id)"
            ),
            {
                "id": center_id,
                "tenant_id": tenant_id,
                "name": f"Centre {label}",
                "region_id": region_id,
            },
        )
        await owner_conn.execute(
            text(
                "INSERT INTO app_user (id, tenant_id, email, role, is_active) "
                "VALUES (:id, :tenant_id, :email, 'clinician', true)"
            ),
            {
                "id": user_id,
                "tenant_id": tenant_id,
                "email": f"clinician-{label}-{uuid.uuid4().hex}@test.local",
            },
        )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def app_engine() -> AsyncIterator[object]:
    """Prépare la base de test et expose un moteur connecté en rôle `dme_app`."""
    assert OWNER_URL is not None
    _run_migrations(OWNER_URL)

    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    pw_literal = "'" + _APP_TEST_PASSWORD.replace("'", "''") + "'"
    async with owner_engine.begin() as conn:
        await conn.execute(text(f"ALTER ROLE {_APP_ROLE} LOGIN PASSWORD {pw_literal}"))
    async with owner_engine.begin() as conn:
        await _seed(conn)

    app_url = _app_role_url(OWNER_URL, _APP_TEST_PASSWORD)
    engine = create_async_engine(app_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()
        async with owner_engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE audit_log DISABLE TRIGGER "
                    "trg_audit_log_block_mutation"
                )
            )
            await conn.execute(
                text("DELETE FROM audit_log WHERE tenant_id = ANY(:ids)"),
                {"ids": [TENANT_A, TENANT_B]},
            )
            await conn.execute(
                text(
                    "ALTER TABLE audit_log ENABLE TRIGGER "
                    "trg_audit_log_block_mutation"
                )
            )
            for tenant_id in (TENANT_A, TENANT_B):
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, false)"),
                    {"tid": str(tenant_id)},
                )
                await conn.execute(
                    text("DELETE FROM sync_conflict WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM sync_mutation WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM visit WHERE tenant_id = :tid"), {"tid": tenant_id}
                )
                await conn.execute(
                    text("DELETE FROM patient WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM app_user WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM medical_center WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
            await conn.execute(
                text("DELETE FROM tenant WHERE id = ANY(:ids)"),
                {"ids": [TENANT_A, TENANT_B]},
            )
            await conn.execute(
                text(
                    "DELETE FROM region WHERE name_fr = 'Région test' AND id NOT IN "
                    "(SELECT DISTINCT region_id FROM tenant WHERE region_id IS NOT NULL)"
                )
            )
            await conn.execute(text(f"ALTER ROLE {_APP_ROLE} NOLOGIN PASSWORD NULL"))
        await owner_engine.dispose()


_principal: AuthPrincipal | None = None


@pytest.fixture(autouse=True)
def _reset_principal() -> None:
    global _principal
    _principal = None


@pytest.fixture
def client(app_engine: object) -> TestClient:
    """Client FastAPI dont `tenant_session` est câblée sur le moteur `dme_app`.

    On surcharge `tenant_session` (ouvre une transaction, pose `app.tenant_id`
    depuis le principal injecté — comme la vraie dépendance le dérive du JWT),
    `get_current_principal`, et `sync_access_revoked` (jamais révoqué dans ces
    tests, pour exercer le chemin de deltas).
    """
    session_factory = async_sessionmaker(
        bind=app_engine,  # type: ignore[arg-type]
        expire_on_commit=False,
        autoflush=False,
    )

    async def _override_tenant_session() -> AsyncIterator[object]:
        assert _principal is not None and _principal.tenant_id is not None
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(_principal.tenant_id)},
            )
            yield session

    async def _override_principal() -> AuthPrincipal:
        assert _principal is not None
        return _principal

    async def _override_revoked() -> bool:
        return False

    app.dependency_overrides[tenant_session] = _override_tenant_session
    app.dependency_overrides[get_current_principal] = _override_principal
    app.dependency_overrides[sync_access_revoked] = _override_revoked
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(tenant_session, None)
        app.dependency_overrides.pop(get_current_principal, None)
        app.dependency_overrides.pop(sync_access_revoked, None)


def _use_tenant(tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    global _principal
    _principal = AuthPrincipal(sub=user_id, tenant_id=tenant_id, role="clinician")


def _patient_mutation(
    *,
    entity_id: uuid.UUID,
    mutation_id: uuid.UUID | None = None,
    version: int = 1,
    updated_at: datetime | None = None,
    mrn: str | None = None,
    op: str = "upsert",
    **payload_overrides: object,
) -> dict:
    ts = (updated_at or datetime.now(UTC)).isoformat()
    payload = {
        "mrn": mrn or f"MRN-{uuid.uuid4().hex[:8]}",
        "given_name": "Koffi",
        "family_name": "Mensah",
        "sex": "M",
        "phone": "+22890000000",
        "consent_to_store": True,
    }
    payload.update(payload_overrides)
    return {
        "mutation_id": str(mutation_id or uuid.uuid4()),
        "entity": "patient",
        "entity_id": str(entity_id),
        "op": op,
        "payload": {} if op == "delete" else payload,
        "client_version": version,
        "client_updated_at": ts,
        "event_at": ts,
    }


def _visit_mutation(
    *,
    entity_id: uuid.UUID,
    patient_id: uuid.UUID,
    mutation_id: uuid.UUID | None = None,
    version: int = 1,
    updated_at: datetime | None = None,
    notes: str = "Notes initiales",
) -> dict:
    ts = (updated_at or datetime.now(UTC)).isoformat()
    return {
        "mutation_id": str(mutation_id or uuid.uuid4()),
        "entity": "visit",
        "entity_id": str(entity_id),
        "op": "upsert",
        "payload": {
            "patient_id": str(patient_id),
            "visited_at": ts,
            "reason": "Consultation",
            "notes": notes,
        },
        "client_version": version,
        "client_updated_at": ts,
        "event_at": ts,
    }


async def _count(sql: str, params: dict) -> int:
    """Compte des lignes via la connexion owner (contexte tenant posé si `tid`)."""
    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)  # type: ignore[arg-type]
    try:
        async with owner_engine.connect() as conn, conn.begin():
            if "tid" in params:
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(params["tid"])},
                )
            return int((await conn.execute(text(sql), params)).scalar_one())
    finally:
        await owner_engine.dispose()


async def _scalar(sql: str, params: dict) -> object:
    """Lit une valeur scalaire via la connexion owner (contexte tenant posé)."""
    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)  # type: ignore[arg-type]
    try:
        async with owner_engine.connect() as conn, conn.begin():
            if "tid" in params:
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(params["tid"])},
                )
            return (await conn.execute(text(sql), params)).scalar_one_or_none()
    finally:
        await owner_engine.dispose()


async def _count_as_app(
    app_engine: object, sql: str, tenant_id: uuid.UUID, params: dict
) -> int:
    """Compte des lignes via le rôle **`dme_app`** (NOBYPASSRLS) sous contexte tenant.

    Les assertions **d'isolation** (P11) doivent passer par un rôle qui n'échappe
    pas à la RLS : le propriétaire du schéma est superuser et BYPASS la RLS, ce qui
    rendrait tout visible. On exerce donc le vrai rôle runtime `dme_app`, en posant
    `app.tenant_id` sur le tenant cible — exactement comme `test_rls_isolation.py`.
    """
    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        return int((await conn.execute(text(sql), params)).scalar_one())


# --------------------------------------------------------------------------- #
# P10 — push idempotent : pas de doublon après reprise                         #
# --------------------------------------------------------------------------- #


async def test_push_idempotent_rejeu_sans_doublon(client: TestClient) -> None:
    """Rejouer le même lot (reprise après coupure) n'applique qu'une fois (P10).

    Premier push : la mutation est `applied`. Rejeu du **même** lot (même
    `mutation_id`) : `duplicate`, et une seule ligne patient en base (pas de
    doublon, pas de corruption).
    """
    _use_tenant(TENANT_A, USER_A)
    entity_id = uuid.uuid4()
    mutation_id = uuid.uuid4()
    body = {"mutations": [_patient_mutation(entity_id=entity_id, mutation_id=mutation_id)]}

    first = client.post("/api/sync/push", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["results"][0]["status"] == "applied"

    # Reprise : on rejoue exactement le même lot.
    second = client.post("/api/sync/push", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["results"][0]["status"] == "duplicate"

    # Une seule ligne patient : aucun doublon malgré le rejeu (P10).
    total = await _count(
        "SELECT count(*) FROM patient WHERE id = :id",
        {"id": entity_id, "tid": TENANT_A},
    )
    assert total == 1
    # Une seule entrée au registre d'idempotence.
    reg = await _count(
        "SELECT count(*) FROM sync_mutation WHERE mutation_id = :mid",
        {"mid": mutation_id, "tid": TENANT_A},
    )
    assert reg == 1


async def test_push_lot_partiellement_rejoue(client: TestClient) -> None:
    """Un lot repris où seules certaines mutations sont nouvelles reste cohérent (P10).

    On pousse m1, puis un lot {m1 (rejeu), m2 (nouveau)} : m1 => duplicate, m2 =>
    applied. Deux patients distincts, aucune réapplication de m1.
    """
    _use_tenant(TENANT_A, USER_A)
    e1, e2 = uuid.uuid4(), uuid.uuid4()
    m1, m2 = uuid.uuid4(), uuid.uuid4()

    first = client.post(
        "/api/sync/push",
        json={"mutations": [_patient_mutation(entity_id=e1, mutation_id=m1)]},
    )
    assert first.status_code == 200, first.text

    resumed = client.post(
        "/api/sync/push",
        json={
            "mutations": [
                _patient_mutation(entity_id=e1, mutation_id=m1),  # rejeu
                _patient_mutation(entity_id=e2, mutation_id=m2),  # nouveau
            ]
        },
    )
    assert resumed.status_code == 200, resumed.text
    statuses = {r["mutation_id"]: r["status"] for r in resumed.json()["results"]}
    assert statuses[str(m1)] == "duplicate"
    assert statuses[str(m2)] == "applied"

    for eid in (e1, e2):
        assert (
            await _count(
                "SELECT count(*) FROM patient WHERE id = :id",
                {"id": eid, "tid": TENANT_A},
            )
            == 1
        )


# --------------------------------------------------------------------------- #
# P11 — pull cadré au tenant ; injection inter-tenant impossible               #
# --------------------------------------------------------------------------- #


async def test_pull_cadre_au_tenant(client: TestClient) -> None:
    """Un patient poussé par A n'apparaît jamais dans le pull de B (P11)."""
    _use_tenant(TENANT_A, USER_A)
    entity_id = uuid.uuid4()
    pushed = client.post(
        "/api/sync/push",
        json={"mutations": [_patient_mutation(entity_id=entity_id, mrn="PULL-A-1")]},
    )
    assert pushed.status_code == 200, pushed.text

    # A voit son patient dans le pull initial.
    pull_a = client.get("/api/sync/pull")
    assert pull_a.status_code == 200
    ids_a = {c["entity_id"] for c in pull_a.json()["changes"]}
    assert str(entity_id) in ids_a

    # B ne voit jamais le patient de A (RLS cadre le pull, P11).
    _use_tenant(TENANT_B, USER_B)
    pull_b = client.get("/api/sync/pull")
    assert pull_b.status_code == 200
    ids_b = {c["entity_id"] for c in pull_b.json()["changes"]}
    assert str(entity_id) not in ids_b


async def test_push_appose_le_tenant_du_principal_jamais_cross_tenant(
    client: TestClient, app_engine: object
) -> None:
    """Le serveur appose le tenant du principal : la ligne est sous A, invisible à B (P11/P2).

    Le corps d'une mutation ne porte jamais de `tenant_id` ; le serveur l'appose
    depuis le principal. Vérifié via le rôle `dme_app` (NOBYPASSRLS) : la ligne est
    visible sous le contexte A et INVISIBLE sous le contexte B (la RLS cadre la
    sync, P11). Aucune donnée ne traverse la frontière de tenant.
    """
    _use_tenant(TENANT_A, USER_A)
    entity_id = uuid.uuid4()
    pushed = client.post(
        "/api/sync/push",
        json={"mutations": [_patient_mutation(entity_id=entity_id, mrn="OWN-A-1")]},
    )
    assert pushed.status_code == 200, pushed.text

    # Visible sous le contexte A (rôle dme_app, RLS appliquée)…
    seen_a = await _count_as_app(
        app_engine,
        "SELECT count(*) FROM patient WHERE id = :id",
        TENANT_A,
        {"id": entity_id},
    )
    assert seen_a == 1
    # …et invisible sous le contexte B (RLS, P11).
    seen_b = await _count_as_app(
        app_engine,
        "SELECT count(*) FROM patient WHERE id = :id",
        TENANT_B,
        {"id": entity_id},
    )
    assert seen_b == 0, "Un patient de A ne doit jamais être visible sous B (P11)"


# --------------------------------------------------------------------------- #
# P12 — conflit : LWW + journal ; entité sensible marquée                      #
# --------------------------------------------------------------------------- #


async def test_conflit_patient_lww_client_gagne_et_perdant_journalise(
    client: TestClient,
) -> None:
    """Patient (non sensible) : édition concurrente plus récente gagne (LWW), perdant journalisé (P12).

    On crée le patient à t0 (version 1). Une édition concurrente à t1 > t0 avec
    `client_version = 1` (basée sur la même version) entre en conflit : le client
    est plus récent → il gagne, l'ancien état serveur est consigné dans
    `sync_conflict` (non destructif), et la ligne reflète la valeur gagnante.
    """
    _use_tenant(TENANT_A, USER_A)
    entity_id = uuid.uuid4()
    t0 = datetime.now(UTC)

    base = client.post(
        "/api/sync/push",
        json={
            "mutations": [
                _patient_mutation(
                    entity_id=entity_id, version=1, updated_at=t0, phone="+22800000000"
                )
            ]
        },
    )
    assert base.status_code == 200, base.text

    # Édition concurrente : même version de base (1) mais plus récente (t1 > t0).
    t1 = t0 + timedelta(minutes=5)
    concurrent = client.post(
        "/api/sync/push",
        json={
            "mutations": [
                _patient_mutation(
                    entity_id=entity_id, version=1, updated_at=t1, phone="+22899999999"
                )
            ]
        },
    )
    assert concurrent.status_code == 200, concurrent.text
    result = concurrent.json()
    assert result["results"][0]["status"] == "conflict"
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["sensitive"] is False

    # Le client (plus récent) gagne : la valeur serveur reflète son édition.
    phone = await _scalar(
        "SELECT phone FROM patient WHERE id = :id",
        {"id": entity_id, "tid": TENANT_A},
    )
    assert phone == "+22899999999"

    # Le perdant (ancien état serveur) est consigné (jamais détruit, P12).
    logged = await _count(
        "SELECT count(*) FROM sync_conflict WHERE entity = 'patient' AND entity_id = :id",
        {"id": entity_id, "tid": TENANT_A},
    )
    assert logged == 1


async def test_conflit_visite_sensible_marquee_sans_ecrasement(
    client: TestClient,
) -> None:
    """Visite (sensible) : conflit marqué pour revue, notes serveur NON écrasées (P12).

    Les notes de visite sont « cliniquement sensibles » (décision de conception) :
    même si l'édition cliente est plus récente, elle n'écrase PAS les notes
    serveur ; le conflit est consigné `sensitive=true` pour revue humaine.
    """
    _use_tenant(TENANT_A, USER_A)
    # Un patient support pour la visite.
    patient_id = uuid.uuid4()
    client.post(
        "/api/sync/push",
        json={"mutations": [_patient_mutation(entity_id=patient_id, mrn="VP-A-1")]},
    )

    visit_id = uuid.uuid4()
    t0 = datetime.now(UTC)
    base = client.post(
        "/api/sync/push",
        json={
            "mutations": [
                _visit_mutation(
                    entity_id=visit_id,
                    patient_id=patient_id,
                    version=1,
                    updated_at=t0,
                    notes="NOTES SERVEUR À PRÉSERVER",
                )
            ]
        },
    )
    assert base.status_code == 200, base.text

    # Édition concurrente plus récente des notes.
    t1 = t0 + timedelta(minutes=10)
    concurrent = client.post(
        "/api/sync/push",
        json={
            "mutations": [
                _visit_mutation(
                    entity_id=visit_id,
                    patient_id=patient_id,
                    version=1,
                    updated_at=t1,
                    notes="NOTES CLIENTES CONCURRENTES",
                )
            ]
        },
    )
    assert concurrent.status_code == 200, concurrent.text
    result = concurrent.json()
    assert result["results"][0]["status"] == "conflict"
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["sensitive"] is True, (
        "Une entité sensible (visite) doit être marquée pour revue (P12)"
    )

    # Les notes serveur ne sont PAS écrasées (préservation, pas d'écrasement).
    notes = await _scalar(
        "SELECT notes FROM visit WHERE id = :id",
        {"id": visit_id, "tid": TENANT_A},
    )
    assert notes == "NOTES SERVEUR À PRÉSERVER"

    # Le conflit sensible est consigné pour revue.
    logged = await _count(
        "SELECT count(*) FROM sync_conflict "
        "WHERE entity = 'visit' AND entity_id = :id AND sensitive = true",
        {"id": visit_id, "tid": TENANT_A},
    )
    assert logged == 1


async def test_sync_conflict_cadre_au_tenant(
    client: TestClient, app_engine: object
) -> None:
    """Un conflit journalisé pour A n'est pas visible dans le contexte de B (P11).

    Vérifié via le rôle `dme_app` (NOBYPASSRLS) : la ligne `sync_conflict` de A est
    visible sous A et invisible sous B — le journal de conflits est cadré au tenant.
    """
    _use_tenant(TENANT_A, USER_A)
    entity_id = uuid.uuid4()
    t0 = datetime.now(UTC)
    client.post(
        "/api/sync/push",
        json={"mutations": [_patient_mutation(entity_id=entity_id, version=1, updated_at=t0)]},
    )
    t1 = t0 + timedelta(minutes=1)
    client.post(
        "/api/sync/push",
        json={"mutations": [_patient_mutation(entity_id=entity_id, version=1, updated_at=t1)]},
    )

    seen_by_a = await _count_as_app(
        app_engine,
        "SELECT count(*) FROM sync_conflict WHERE entity_id = :id",
        TENANT_A,
        {"id": entity_id},
    )
    assert seen_by_a >= 1
    seen_by_b = await _count_as_app(
        app_engine,
        "SELECT count(*) FROM sync_conflict WHERE entity_id = :id",
        TENANT_B,
        {"id": entity_id},
    )
    assert seen_by_b == 0, "Le journal de conflits est cadré au tenant (RLS, P11)"
