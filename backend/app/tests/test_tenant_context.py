"""Smoke tests du contexte de tenant (tâche 4.3) — NFR-1, 1.2, 1.3, 5.2.

Ces tests ne touchent PAS la base : l'isolation réelle (RLS) est vérifiée par la
tâche 4.4 contre une base PostgreSQL. On vérifie ici que :

- le module s'importe proprement et expose le joint attendu ;
- `get_current_tenant_id` échoue en **fail-closed** tant que l'auth (tâche 7)
  n'est pas câblée (aucune exécution « hors tenant » par accident) ;
- `set_tenant_context` émet bien `set_config('app.tenant_id', :tid, true)` avec
  la valeur **liée comme paramètre** (défense anti-injection), via un faux objet
  session capturant l'appel ;
- `database_url` sélectionne le rôle applicatif runtime quand il est configuré,
  et `owner_database_url` reste sur le compte propriétaire (migrations).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.core import tenant_context
from app.core.config import Settings


async def test_get_current_tenant_id_fail_closed() -> None:
    """Le joint d'auth non câblé lève 501 (fail-closed), jamais de tenant forgé."""
    with pytest.raises(HTTPException) as exc_info:
        await tenant_context.get_current_tenant_id()
    assert exc_info.value.status_code == 501


async def test_set_tenant_context_lie_le_parametre() -> None:
    """`set_tenant_context` utilise set_config(..., true) avec un paramètre lié."""

    captured: dict[str, object] = {}

    class _FakeSession:
        async def execute(self, statement: object, params: dict[str, object]) -> None:
            captured["sql"] = str(statement)
            captured["params"] = params

    tid = uuid.uuid4()
    await tenant_context.set_tenant_context(_FakeSession(), tid)  # type: ignore[arg-type]

    sql = str(captured["sql"])
    assert "set_config('app.tenant_id', :tid, true)" in sql
    # Transaction-local (3ᵉ argument true) et aucune interpolation du UUID.
    assert str(tid) not in sql
    assert captured["params"] == {"tid": str(tid)}


def test_runtime_db_url_prefere_le_role_applicatif() -> None:
    """`database_url` utilise `APP_DB_*` quand il est fourni (rôle non-superuser)."""
    settings = Settings(
        postgres_host="db",
        postgres_port=5432,
        postgres_db="dme",
        postgres_user="owner",
        postgres_password="ownerpw",
        app_db_user="dme_app",
        app_db_password="apppw",
    )
    assert settings.database_url == "postgresql+asyncpg://dme_app:apppw@db:5432/dme"
    # Les migrations restent sur le compte propriétaire.
    assert settings.owner_database_url == "postgresql+asyncpg://owner:ownerpw@db:5432/dme"


def test_runtime_db_url_retombe_sur_le_compte_principal() -> None:
    """Sans `APP_DB_*`, `database_url` retombe sur `POSTGRES_*`."""
    settings = Settings(
        postgres_host="db",
        postgres_port=5432,
        postgres_db="dme",
        postgres_user="owner",
        postgres_password="ownerpw",
    )
    assert settings.database_url == "postgresql+asyncpg://owner:ownerpw@db:5432/dme"
    assert settings.database_url == settings.owner_database_url
