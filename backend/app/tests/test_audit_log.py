"""Tests d'intégration du journal d'audit (tâche 5.3) — NFR-3 ; vérifie P5.

L'append-only, l'immutabilité et le comportement de l'écrivain non contraint par
le contexte de tenant sont des propriétés du **moteur PostgreSQL** (GRANT +
trigger + absence de RLS par-tenant) : ils ne peuvent être vérifiés qu'avec une
**vraie base**. Comme `test_rls_isolation.py` (tâche 4.4), ce module exécute les
migrations réelles (dont 0004) et exerce le **rôle non-superuser `dme_app`**,
celui qui écrit l'audit en production.

Propriétés vérifiées (design, « Correctness Properties » P5 et « Tests
d'audit ») :

- **Les événements plateforme à `tenant_id` NUL persistent**, y compris lorsqu'un
  contexte tenant (`app.tenant_id`) est posé — l'écrivain d'audit n'est pas
  contraint par la RLS par-tenant (aucune politique sur `audit_log`), donc la
  ligne à tenant NUL n'est jamais masquée ni rejetée.
- **Les lignes sont immuables** : un `UPDATE` ou un `DELETE` par le rôle
  applicatif est bloqué (GRANT sans UPDATE/DELETE + trigger append-only).
- **Un événement PHI hors ligne téléversé est conservé sans perte** : `event_at`
  (horodatage client) est préservé exactement, et `received_at` (serveur) est
  apposé à la réception (NFR-3).

Garde CI identique aux tests RLS : désactivé si `TEST_DATABASE_URL` est absente.
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
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : tests d'intégration d'audit "
        "désactivés (nécessitent un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "audit_test_" + uuid.uuid4().hex


def _app_role_url(owner_url: str, password: str) -> str:
    """Dérive la DSN du rôle `dme_app` depuis la DSN propriétaire."""
    parts = urlsplit(owner_url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{_APP_ROLE}:{password}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run_migrations(owner_url: str) -> None:
    """Exécute `alembic upgrade head` (sous-processus, connexion owner).

    Même approche que `test_rls_isolation.py` : `env.py` appelle `asyncio.run()`,
    incompatible avec la boucle pytest-asyncio ; le sous-processus a sa propre
    boucle et c'est le chemin de migration réellement livré.
    """
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


# Un tenant arbitraire pour poser un contexte RLS pendant certains tests.
SOME_TENANT = uuid.uuid4()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def app_engine() -> AsyncIterator[object]:
    """Migre la base de test et expose un moteur connecté en rôle `dme_app`.

    1. migrations `alembic upgrade head` (crée `audit_log`, le trigger
       append-only et les GRANT INSERT/SELECT) ;
    2. octroi LOGIN + mot de passe éphémère au rôle `dme_app` ;
    3. yield d'un moteur async `dme_app` (NOBYPASSRLS, droits runtime réels) ;
    4. nettoyage : purge des lignes d'audit insérées + retrait du LOGIN.
    """
    assert OWNER_URL is not None
    _run_migrations(OWNER_URL)

    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    pw_literal = "'" + _APP_TEST_PASSWORD.replace("'", "''") + "'"
    async with owner_engine.begin() as conn:
        await conn.execute(text(f"ALTER ROLE {_APP_ROLE} LOGIN PASSWORD {pw_literal}"))

    app_url = _app_role_url(OWNER_URL, _APP_TEST_PASSWORD)
    engine = create_async_engine(app_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()
        # Nettoyage : les lignes d'audit sont immuables pour `dme_app` (c'est
        # justement ce qu'on teste) ; on les purge donc via le propriétaire, qui
        # reste néanmoins soumis au trigger append-only. On neutralise le trigger
        # le temps du nettoyage puis on le restaure, pour laisser la base propre.
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE audit_log DISABLE TRIGGER trg_audit_log_block_mutation")
            )
            await conn.execute(
                text("DELETE FROM audit_log WHERE action LIKE :p"),
                {"p": "test.%"},
            )
            await conn.execute(
                text("ALTER TABLE audit_log ENABLE TRIGGER trg_audit_log_block_mutation")
            )
            await conn.execute(text(f"ALTER ROLE {_APP_ROLE} NOLOGIN PASSWORD NULL"))
        await owner_engine.dispose()


async def _insert_event(
    conn: AsyncConnection,
    *,
    action: str,
    tenant_id: uuid.UUID | None,
    event_at: datetime,
    entity: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> int:
    """Insère un événement d'audit et renvoie son `id` (received_at = défaut)."""
    row = (
        await conn.execute(
            text(
                "INSERT INTO audit_log "
                "(tenant_id, actor_user_id, action, entity, entity_id, event_at, ip) "
                "VALUES (:tenant_id, NULL, :action, :entity, :entity_id, :event_at, NULL) "
                "RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "action": action,
                "entity": entity,
                "entity_id": entity_id,
                "event_at": event_at,
            },
        )
    ).scalar_one()
    return int(row)


# --- P5 : les événements plateforme (tenant_id NULL) persistent ----------------


async def test_evenement_plateforme_tenant_null_persiste(app_engine: object) -> None:
    """Un événement à `tenant_id` NUL persiste et est relisible — même sous un
    contexte tenant posé (l'écrivain d'audit n'est pas contraint par la RLS).
    """
    now = datetime.now(UTC)
    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        # Poser un contexte tenant : s'il contraignait l'audit, la ligne à tenant
        # NUL serait masquée/rejetée. Elle ne doit pourtant pas l'être.
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(SOME_TENANT)},
        )
        audit_id = await _insert_event(
            conn, action="test.platform.action", tenant_id=None, event_at=now
        )
        row = (
            await conn.execute(
                text("SELECT tenant_id FROM audit_log WHERE id = :id"),
                {"id": audit_id},
            )
        ).one()
    assert row[0] is None, "L'événement plateforme (tenant NUL) doit persister"


# --- P5 : immutabilité (UPDATE / DELETE bloqués pour le rôle applicatif) --------


async def test_update_bloque_pour_le_role_app(app_engine: object) -> None:
    """Un UPDATE d'une ligne d'audit par `dme_app` est refusé (append-only)."""
    now = datetime.now(UTC)
    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        audit_id = await _insert_event(
            conn, action="test.immutable.update", tenant_id=None, event_at=now
        )

    with pytest.raises((DBAPIError, ProgrammingError)):
        async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
            await conn.execute(
                text("UPDATE audit_log SET action = 'falsifie' WHERE id = :id"),
                {"id": audit_id},
            )

    # La ligne est intacte.
    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        action = (
            await conn.execute(
                text("SELECT action FROM audit_log WHERE id = :id"),
                {"id": audit_id},
            )
        ).scalar_one()
    assert action == "test.immutable.update", "La ligne d'audit ne doit pas changer"


async def test_delete_bloque_pour_le_role_app(app_engine: object) -> None:
    """Un DELETE d'une ligne d'audit par `dme_app` est refusé (append-only)."""
    now = datetime.now(UTC)
    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        audit_id = await _insert_event(
            conn, action="test.immutable.delete", tenant_id=None, event_at=now
        )

    with pytest.raises((DBAPIError, ProgrammingError)):
        async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
            await conn.execute(
                text("DELETE FROM audit_log WHERE id = :id"),
                {"id": audit_id},
            )

    # La ligne existe toujours.
    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        count = (
            await conn.execute(
                text("SELECT count(*) FROM audit_log WHERE id = :id"),
                {"id": audit_id},
            )
        ).scalar_one()
    assert count == 1, "La ligne d'audit ne doit pas être supprimable"


# --- P5 : audit distribué — événement PHI hors ligne conservé sans perte --------


async def test_evenement_hors_ligne_conserve_event_at_et_stampe_received_at(
    app_engine: object,
) -> None:
    """Un événement PHI hors ligne téléversé conserve `event_at` ; le serveur
    appose `received_at` (postérieur à l'événement) — aucune perte (NFR-3).
    """
    # L'événement s'est produit hors ligne, dans le passé (p. ex. la veille).
    offline_event_at = datetime.now(UTC) - timedelta(hours=18)
    patient_id = uuid.uuid4()

    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        audit_id = await _insert_event(
            conn,
            action="test.phi.offline.create",
            tenant_id=SOME_TENANT,
            event_at=offline_event_at,
            entity="patient",
            entity_id=patient_id,
        )
        event_at, received_at, got_entity_id = (
            await conn.execute(
                text(
                    "SELECT event_at, received_at, entity_id "
                    "FROM audit_log WHERE id = :id"
                ),
                {"id": audit_id},
            )
        ).one()

    # `event_at` client est préservé exactement (aucune réécriture serveur).
    assert event_at == offline_event_at, "event_at (client) doit être préservé"
    # `received_at` serveur est apposé à la réception et est postérieur.
    assert received_at is not None, "received_at doit être apposé par le serveur"
    assert received_at > event_at, "received_at (serveur) suit event_at (hors ligne)"
    # L'événement PHI est bien conservé avec son entité ciblée.
    assert got_entity_id == patient_id
