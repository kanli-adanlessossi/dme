"""Tests d'intégration de l'annuaire public + carte (tâche 12.3) — exigences 7, 8.

**Vérifie P4** (confidentialité des PHI) et **P8** (surface publique « actifs
seulement ») contre un **vrai moteur PostgreSQL/PostGIS avec RLS forcée**, via le
rôle applicatif non-superuser `dme_app` (NOBYPASSRLS). Comme `test_center_db.py`,
ce module exécute les migrations réelles (dont `0009_public_directory`, qui pose
la politique PERMISSIVE `public_read`) : c'est la seule façon de prouver des
propriétés **du moteur**.

Le dispositif câble l'app FastAPI sur le moteur `dme_app` MAIS, contrairement aux
tests cadrés au tenant, **ne surcharge PAS** la dépendance de session : les
endpoints publics empruntent la vraie `public_session`, qui pose
``app.public_read = 'on'`` SANS `app.tenant_id`. On exerce donc exactement le
chemin de production. On surcharge seulement `async_session_factory` (dans
`tenant_context`) pour qu'il pointe sur le moteur `dme_app` de test.

Données amorcées :
- une région ;
- un **tenant actif** avec un centre public (Lomé) + 2 services + un `app_user`
  et un **patient (PHI)** ;
- un **tenant inactif** (status='inactive') avec un centre dans la même région.

Propriétés vérifiées :
- **P8.** Le centre du tenant inactif n'apparaît NI dans la liste par région, NI
  par id (→ 404), et n'est PAS compté dans l'annuaire des régions. Seul le centre
  actif est visible.
- **P4.** La projection publique n'expose aucun champ interne (`tenant_id`,
  `is_public`, …) ni aucune PHI. Le patient amorcé n'est atteignable par AUCUN
  endpoint public (la politique `public_read` n'ouvre rien sur `patient`).
- **Région vide → liste vide** (exigences 7.3, 8.1) pour une région sans centre.

Garde CI : désactivé (`pytest.skip`) si `TEST_DATABASE_URL` est absente.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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

from app.core import tenant_context
from app.main import app

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

# Les tests ci-dessous sont **synchrones** (ils pilotent `TestClient`) ; seul le
# dispositif d'amorçage est asynchrone (fixture `app_engine`). On n'applique donc
# PAS de marque `asyncio` globale (qui, appliquée à une fonction de test sync,
# déclenche un avertissement pytest-asyncio). Le `skipif` suffit à désactiver la
# suite sans base. La fixture asynchrone s'exécute via `asyncio_mode = "auto"`.
pytestmark = pytest.mark.skipif(
    not OWNER_URL,
    reason="TEST_DATABASE_URL non définie : tests d'intégration de l'annuaire "
    "public désactivés (nécessitent un vrai PostgreSQL/PostGIS).",
)

_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "public_test_" + uuid.uuid4().hex

_REGION_ID = uuid.uuid4()
_REGION_CODE = "MARITIME"
# Région sans aucun centre, pour vérifier « région vide → liste vide ».
_EMPTY_REGION_CODE = "KARA"

TENANT_ACTIVE = uuid.uuid4()
TENANT_INACTIVE = uuid.uuid4()
CENTER_ACTIVE = uuid.uuid4()
CENTER_INACTIVE = uuid.uuid4()
USER_ACTIVE = uuid.uuid4()
PATIENT_ACTIVE = uuid.uuid4()

SERVICE_MATERNITE = uuid.uuid4()
SERVICE_PHARMACIE = uuid.uuid4()

# Coordonnées togolaises valides (Lomé) — satisfont le CHECK boîte englobante.
_LOME = {"longitude": 1.2255, "latitude": 6.1319}
_KARA = {"longitude": 1.1900, "latitude": 9.5500}


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
    """Amorce 1 région + tenant actif (centre + services + patient) + tenant inactif."""
    # Région de référence (sans RLS).
    await owner_conn.execute(
        text("INSERT INTO region (id, code, name_fr) VALUES (:id, :code, :name)"),
        {"id": _REGION_ID, "code": _REGION_CODE, "name": "Maritime"},
    )
    # service_type (référence).
    for sid, name in (
        (SERVICE_MATERNITE, "Maternité"),
        (SERVICE_PHARMACIE, "Pharmacie"),
    ):
        await owner_conn.execute(
            text("INSERT INTO service_type (id, name_fr) VALUES (:id, :name)"),
            {"id": sid, "name": name},
        )
    # Tenants : un actif, un inactif (non cadrés RLS).
    await owner_conn.execute(
        text(
            "INSERT INTO tenant (id, name, region_id, status) "
            "VALUES (:id, :name, :rid, 'active')"
        ),
        {"id": TENANT_ACTIVE, "name": "Centre actif", "rid": _REGION_ID},
    )
    await owner_conn.execute(
        text(
            "INSERT INTO tenant (id, name, region_id, status) "
            "VALUES (:id, :name, :rid, 'inactive')"
        ),
        {"id": TENANT_INACTIVE, "name": "Centre désactivé", "rid": _REGION_ID},
    )

    # Centres (RLS forcée → poser le contexte du tenant pour chaque insertion).
    for tid, cid, name, coords in (
        (TENANT_ACTIVE, CENTER_ACTIVE, "Clinique Lomé Active", _LOME),
        (TENANT_INACTIVE, CENTER_INACTIVE, "Clinique Désactivée", _LOME),
    ):
        await owner_conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, false)"),
            {"tid": str(tid)},
        )
        await owner_conn.execute(
            text(
                "INSERT INTO medical_center "
                "(id, tenant_id, name, region_id, address, location, phone, email) "
                "VALUES (:id, :tid, :name, :rid, :addr, "
                "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :phone, :email)"
            ),
            {
                "id": cid,
                "tid": tid,
                "name": name,
                "rid": _REGION_ID,
                "addr": "Boulevard du Mono, Lomé",
                "lon": coords["longitude"],
                "lat": coords["latitude"],
                "phone": "+22890000000",
                "email": "contact@centre.tg",
            },
        )

    # Contexte du tenant actif pour ses lignes cadrées restantes.
    await owner_conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, false)"),
        {"tid": str(TENANT_ACTIVE)},
    )
    # Services liés au centre actif (liaison cadrée au tenant).
    for sid in (SERVICE_MATERNITE, SERVICE_PHARMACIE):
        await owner_conn.execute(
            text(
                "INSERT INTO medical_center_service "
                "(medical_center_id, service_type_id, tenant_id) "
                "VALUES (:cid, :sid, :tid)"
            ),
            {"cid": CENTER_ACTIVE, "sid": sid, "tid": TENANT_ACTIVE},
        )
    # app_user (acteur) + patient (PHI) du tenant actif.
    await owner_conn.execute(
        text(
            "INSERT INTO app_user (id, tenant_id, email, role, is_active) "
            "VALUES (:id, :tid, :email, 'tenant_admin', true)"
        ),
        {
            "id": USER_ACTIVE,
            "tid": TENANT_ACTIVE,
            "email": f"admin-{uuid.uuid4().hex}@test.local",
        },
    )
    await owner_conn.execute(
        text(
            "INSERT INTO patient "
            "(id, tenant_id, mrn, given_name, family_name, consent_to_store, "
            " created_by, updated_at, version) "
            "VALUES (:id, :tid, :mrn, :gn, :fn, true, :cb, :ua, 1)"
        ),
        {
            "id": PATIENT_ACTIVE,
            "tid": TENANT_ACTIVE,
            "mrn": "MRN-PUBLIC-TEST",
            "gn": "Kodjo",
            "fn": "Secret",
            "cb": USER_ACTIVE,
            "ua": datetime.now(UTC),
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
            for tid in (TENANT_ACTIVE, TENANT_INACTIVE):
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, false)"),
                    {"tid": str(tid)},
                )
                await conn.execute(
                    text("DELETE FROM patient WHERE tenant_id = :tid"),
                    {"tid": tid},
                )
                await conn.execute(
                    text(
                        "DELETE FROM medical_center_service WHERE tenant_id = :tid"
                    ),
                    {"tid": tid},
                )
                await conn.execute(
                    text("DELETE FROM app_user WHERE tenant_id = :tid"),
                    {"tid": tid},
                )
                await conn.execute(
                    text("DELETE FROM medical_center WHERE tenant_id = :tid"),
                    {"tid": tid},
                )
            await conn.execute(
                text("DELETE FROM tenant WHERE id = ANY(:ids)"),
                {"ids": [TENANT_ACTIVE, TENANT_INACTIVE]},
            )
            await conn.execute(
                text("DELETE FROM service_type WHERE id = ANY(:ids)"),
                {"ids": [SERVICE_MATERNITE, SERVICE_PHARMACIE]},
            )
            await conn.execute(
                text("DELETE FROM region WHERE id = :rid"), {"rid": _REGION_ID}
            )
            await conn.execute(text(f"ALTER ROLE {_APP_ROLE} NOLOGIN PASSWORD NULL"))
        await owner_engine.dispose()


@pytest.fixture
def client(app_engine: object, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Client FastAPI dont `public_session` tourne sur le vrai moteur `dme_app`.

    On ne surcharge PAS `public_session` : on redirige la fabrique de sessions
    qu'elle utilise (`tenant_context.async_session_factory`) vers le moteur de
    test. L'endpoint exerce ainsi exactement le chemin de production (pose de
    ``app.public_read = 'on'``, aucune pose de `app.tenant_id`), prouvant que la
    politique RLS `public_read` gouverne bien la visibilité.
    """
    session_factory = async_sessionmaker(
        bind=app_engine,  # type: ignore[arg-type]
        expire_on_commit=False,
        autoflush=False,
    )
    monkeypatch.setattr(tenant_context, "async_session_factory", session_factory)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# P8 — surface publique « actifs seulement »                                   #
# --------------------------------------------------------------------------- #


def test_liste_region_exclut_le_tenant_inactif(client: TestClient) -> None:
    """La liste par région ne contient que le centre actif (P8, exigence 8.2)."""
    resp = client.get(f"/api/public/regions/{_REGION_CODE}/centers")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {c["id"] for c in body}
    assert str(CENTER_ACTIVE) in ids
    assert str(CENTER_INACTIVE) not in ids
    assert len(body) == 1


def test_centre_inactif_par_id_renvoie_404(client: TestClient) -> None:
    """Un centre d'un tenant désactivé est invisible par id → 404 (P8)."""
    resp = client.get(f"/api/public/centers/{CENTER_INACTIVE}")
    assert resp.status_code == 404


def test_centre_actif_par_id_visible(client: TestClient) -> None:
    """Le centre actif est accessible par id, avec sa projection publique."""
    resp = client.get(f"/api/public/centers/{CENTER_ACTIVE}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Clinique Lomé Active"
    assert body["service_types"] == ["Maternité", "Pharmacie"]
    assert body["region_code"] == _REGION_CODE


def test_annuaire_regions_compte_actifs_seulement(client: TestClient) -> None:
    """Le décompte par région n'inclut que les centres publics actifs (P8)."""
    resp = client.get("/api/public/regions")
    assert resp.status_code == 200, resp.text
    counts = {r["code"]: r["center_count"] for r in resp.json()}
    # 1 centre actif en Maritime (l'inactif n'est pas compté).
    assert counts[_REGION_CODE] == 1


# --------------------------------------------------------------------------- #
# P4 — confidentialité des PHI / champs privés                                 #
# --------------------------------------------------------------------------- #

_FORBIDDEN = frozenset(
    {"tenant_id", "status", "is_public", "created_by", "created_at", "updated_at"}
)


def test_projection_publique_sans_champ_interne(client: TestClient) -> None:
    """Le profil public n'expose aucun champ interne/sensible (P4, exigence 8.3)."""
    resp = client.get(f"/api/public/centers/{CENTER_ACTIVE}")
    assert resp.status_code == 200, resp.text
    assert not (_FORBIDDEN & resp.json().keys())


def test_aucune_phi_atteignable_publiquement(client: TestClient) -> None:
    """Aucun champ PHI du patient amorcé n'apparaît sur la surface publique (P4).

    On sérialise toutes les réponses publiques exposant le centre actif et on
    vérifie qu'aucune valeur PHI du patient (nom de famille, MRN) n'y figure — la
    politique `public_read` n'ouvre rien sur `patient`.
    """
    bodies = [
        client.get("/api/public/regions").text,
        client.get(f"/api/public/regions/{_REGION_CODE}/centers").text,
        client.get(f"/api/public/centers/{CENTER_ACTIVE}").text,
    ]
    blob = "\n".join(bodies)
    assert "Secret" not in blob
    assert "MRN-PUBLIC-TEST" not in blob


# --------------------------------------------------------------------------- #
# Région vide → liste vide (exigences 7.3, 8.1)                                #
# --------------------------------------------------------------------------- #


def test_region_vide_renvoie_liste_vide(client: TestClient) -> None:
    """Une région sans centre renvoie une liste vide, pas une erreur (7.3, 8.1)."""
    resp = client.get(f"/api/public/regions/{_EMPTY_REGION_CODE}/centers")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
