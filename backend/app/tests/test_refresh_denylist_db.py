"""Tests d'intégration de la denylist de refresh (tâche 9.2, G-9) — exigence 9.

**Vérifie le rejet du rejeu** d'un refresh consommé sur le chemin EN LIGNE
`/api/auth/refresh`, contre un **vrai moteur PostgreSQL/PostGIS** via le rôle
applicatif non-superuser `dme_app` (NOBYPASSRLS). Comme les autres modules
`*_db.py`, il exécute les **migrations réelles** (dont `0010_refresh_token`) :
c'est la seule façon de prouver le comportement **du moteur** (UPDATE conditionnel
atomique, invalidation de famille).

Contrairement aux tests cadrés au tenant, le chemin `/refresh` est **pré-auth** :
on surcharge simplement la dépendance `get_session` pour qu'elle ouvre une session
sur le moteur `dme_app` de test (aucun `app.tenant_id` posé — la table
`refresh_token` n'a volontairement pas de RLS par-tenant, cf. migration 0010).

Note : on n'exerce PAS le `login` HTTP complet ici. Le lookup d'auth
(`find_user_by_email`) lit `app_user` AVANT qu'un contexte de tenant ne soit posé,
or `app_user` est sous RLS forcée (0003) et le rôle `dme_app` sans `app.tenant_id`
n'y voit aucune ligne — limitation connue, consignée pour la tâche 7 (cf.
`test_admin_lifecycle_db.py`). On **émet + enregistre donc le refresh initial
directement** (comme la tâche 9.2 l'autorise), puis on exerce le vrai chemin
`/api/auth/refresh` (pré-auth, ne touche que `refresh_token`, sans RLS).

Scénario :
- amorçage d'un `app_user` dans un tenant actif ;
- émission + enregistrement d'un refresh initial (famille `jti`) ;
- `refresh` avec ce refresh → **200** (rotation : nouvelle paire) ;
- `refresh` RÉPÉTÉ avec le MÊME refresh (désormais consommé) → **401** (rejeu
  rejeté) ;
- le refresh **rotatif** issu de la 1re rotation est ensuite **lui aussi rejeté**
  (le rejeu a invalidé toute la famille — défense anti-vol) → **401**.

Garde CI : désactivé (`pytest.skip`) si `TEST_DATABASE_URL` est absente.
"""

from __future__ import annotations

import asyncio
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

from app.core.config import get_settings
from app.core.db import get_session
from app.core.tokens import create_refresh_token
from app.main import app

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

# Tests synchrones (pilotent `TestClient`) ; seul l'amorçage est asynchrone. On
# n'applique PAS de marque `asyncio` globale (cf. test_public_db.py).
pytestmark = pytest.mark.skipif(
    not OWNER_URL,
    reason="TEST_DATABASE_URL non définie : tests d'intégration de la denylist de "
    "refresh désactivés (nécessitent un vrai PostgreSQL/PostGIS).",
)

_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "refresh_test_" + uuid.uuid4().hex

_REGION_ID = uuid.uuid4()
_REGION_CODE = "DENYLIST-" + uuid.uuid4().hex[:6]
TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
_USER_ROLE = "tenant_admin"
_USER_EMAIL = f"refresh-{uuid.uuid4().hex}@centre-lome.tg"


def _app_role_url(owner_url: str, password: str) -> str:
    parts = urlsplit(owner_url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{_APP_ROLE}:{password}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run_migrations(owner_url: str) -> None:
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
    """Amorce 1 région + 1 tenant actif + 1 `app_user` (acteur FK du refresh)."""
    await owner_conn.execute(
        text("INSERT INTO region (id, code, name_fr) VALUES (:id, :code, :name)"),
        {"id": _REGION_ID, "code": _REGION_CODE, "name": "Région test"},
    )
    await owner_conn.execute(
        text(
            "INSERT INTO tenant (id, name, region_id, status) "
            "VALUES (:id, :name, :rid, 'active')"
        ),
        {"id": TENANT_ID, "name": "Centre denylist", "rid": _REGION_ID},
    )
    # `app_user` est sous RLS forcée (0003) : poser le contexte du tenant pour
    # l'insertion (l'owner y est aussi soumis en FORCE RLS).
    await owner_conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, false)"),
        {"tid": str(TENANT_ID)},
    )
    await owner_conn.execute(
        text(
            "INSERT INTO app_user (id, tenant_id, email, role, is_active) "
            "VALUES (:id, :tid, :email, :role, true)"
        ),
        {
            "id": USER_ID,
            "tid": TENANT_ID,
            "email": _USER_EMAIL,
            "role": _USER_ROLE,
        },
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def app_engine() -> AsyncIterator[object]:
    """Prépare la base, octroie LOGIN à dme_app, amorce les données, expose le moteur."""
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
                text("DELETE FROM refresh_token WHERE user_id = :uid"),
                {"uid": USER_ID},
            )
            await conn.execute(
                text("DELETE FROM app_user WHERE id = :uid"), {"uid": USER_ID}
            )
            await conn.execute(
                text("DELETE FROM tenant WHERE id = :tid"), {"tid": TENANT_ID}
            )
            await conn.execute(
                text("DELETE FROM region WHERE id = :rid"), {"rid": _REGION_ID}
            )
            await conn.execute(text(f"ALTER ROLE {_APP_ROLE} NOLOGIN PASSWORD NULL"))
        await owner_engine.dispose()


@pytest.fixture
def client(app_engine: object) -> AsyncIterator[TestClient]:
    """Client FastAPI dont `get_session` ouvre une session sur le moteur `dme_app`.

    Le chemin `/refresh` (comme `/login`) est pré-auth : on surcharge la seule
    dépendance de session, exerçant ainsi exactement le chemin de production (la
    table `refresh_token` n'a pas de RLS par-tenant, cf. 0010).
    """
    session_factory = async_sessionmaker(
        bind=app_engine,  # type: ignore[arg-type]
        expire_on_commit=False,
        autoflush=False,
    )

    async def _override() -> AsyncIterator[object]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _insert_refresh_row(
    *, jti: uuid.UUID, family_id: uuid.UUID, expires_at: datetime
) -> None:
    """Insère une ligne `refresh_token` via un moteur `dme_app` éphémère et dédié.

    Chaque appel crée ET détruit son propre moteur dans la boucle d'événements de
    `asyncio.run` (voir `_issue_initial_refresh`), de sorte qu'aucune connexion ne
    traverse deux boucles (ce qui déclencherait « Event loop is closed » avec le
    client synchrone). La table `refresh_token` n'a pas de RLS → insertion directe.
    """
    assert OWNER_URL is not None
    app_url = _app_role_url(OWNER_URL, _APP_TEST_PASSWORD)
    engine = create_async_engine(app_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO refresh_token (jti, family_id, user_id, expires_at) "
                    "VALUES (:jti, :family_id, :user_id, :expires_at)"
                ),
                {
                    "jti": jti,
                    "family_id": family_id,
                    "user_id": USER_ID,
                    "expires_at": expires_at,
                },
            )
    finally:
        await engine.dispose()


def _issue_initial_refresh() -> str:
    """Émet un refresh initial (famille `jti`) ET l'enregistre dans la denylist.

    Reproduit ce que fait `login` côté serveur (émission JWT +
    `record_issued_refresh`) sans traverser le lookup d'auth cadré RLS. L'insertion
    de la ligne tourne dans sa propre boucle (`asyncio.run`) pour ne pas entrer en
    conflit avec la boucle du `TestClient` synchrone.
    """
    jti = uuid.uuid4()
    family_id = uuid.uuid4()
    token = create_refresh_token(
        sub=USER_ID,
        tenant_id=TENANT_ID,
        role=_USER_ROLE,
        jti=jti,
        family_id=family_id,
    )
    expires_at = datetime.now(UTC) + timedelta(
        seconds=get_settings().jwt_refresh_ttl_seconds
    )
    asyncio.run(
        _insert_refresh_row(jti=jti, family_id=family_id, expires_at=expires_at)
    )
    return token


def test_refresh_rotation_puis_rejeu_rejete(client: TestClient) -> None:
    """Rotation OK une fois ; rejeu du refresh consommé → 401 (exigence 9, NFR-7).

    Vérifie bout à bout, contre un vrai moteur : (a) un refresh consommé ne peut
    être réutilisé, (b) le rejeu est rejeté, (c) le rejeu invalide toute la famille
    (le refresh rotatif légitime devient lui aussi inutilisable).
    """
    refresh_1 = _issue_initial_refresh()

    # 1re rotation : succès, nouvelle paire (refresh_2 porte le MÊME family_id).
    resp_ok = client.post("/api/auth/refresh", json={"refresh": refresh_1})
    assert resp_ok.status_code == 200, resp_ok.text
    refresh_2 = resp_ok.json()["refresh"]
    assert refresh_2 != refresh_1

    # Rejeu du refresh_1 (désormais consommé) → 401 (denylist).
    resp_replay = client.post("/api/auth/refresh", json={"refresh": refresh_1})
    assert resp_replay.status_code == 401, resp_replay.text

    # Le rejeu a invalidé TOUTE la famille : le refresh_2 légitime est lui aussi
    # refusé (défense anti-vol — « un vol invalide la famille »).
    resp_family = client.post("/api/auth/refresh", json={"refresh": refresh_2})
    assert resp_family.status_code == 401, resp_family.text


def test_refresh_rotatif_utilisable_une_fois(client: TestClient) -> None:
    """Le refresh rotatif (non rejoué) est utilisable exactement une fois.

    Chemin heureux de rotation en chaîne : chaque refresh frais fonctionne une
    fois, et son prédécesseur consommé est refusé.
    """
    refresh_1 = _issue_initial_refresh()

    resp_a = client.post("/api/auth/refresh", json={"refresh": refresh_1})
    assert resp_a.status_code == 200, resp_a.text
    refresh_2 = resp_a.json()["refresh"]

    # Le refresh rotatif frais fonctionne une fois (nouvelle rotation).
    resp_b = client.post("/api/auth/refresh", json={"refresh": refresh_2})
    assert resp_b.status_code == 200, resp_b.text
    refresh_3 = resp_b.json()["refresh"]
    assert refresh_3 not in (refresh_1, refresh_2)

    # Le refresh_1 consommé reste refusé (idempotence du rejet).
    resp_old = client.post("/api/auth/refresh", json={"refresh": refresh_1})
    assert resp_old.status_code == 401, resp_old.text
