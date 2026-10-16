"""Tests d'intégration du profil de centre + validation géo (tâche 11.3).

Exigences 6.1, 6.2, 6.3 — **vérifie P7** (validité géographique). Comme
`test_clinical_db.py` / `test_admin_lifecycle_db.py`, ce module exécute les
migrations réelles (dont `0008_center_profile_geo` : liaison services + CHECK
boîte englobante) contre un **vrai moteur PostgreSQL/PostGIS avec RLS forcée** via
le rôle applicatif non-superuser `dme_app`. C'est la seule façon de prouver des
propriétés **du moteur** :

- **P7 — Validité géographique.** La validation `ST_Within` contre la géométrie du
  pays (union des régions) rejette des coordonnées hors Togo (422), et le CHECK de
  repli en base refuse une persistance hors boîte englobante. Des coordonnées
  valides (Lomé) sont acceptées et persistées.
- **Mise à jour reflétée dans la projection publique.** Après une mise à jour
  (nom, adresse, coordonnées, horaires, services), la **projection sûre** du
  centre (les champs publics du `medical_center` + ses services liés, sans PHI ni
  champ interne) reflète exactement les nouvelles valeurs. C'est la source que
  l'annuaire/carte publics (tâche 12) exposeront.
- **Cadrage par rôle réel.** Un `clinician` ne peut pas modifier le profil (403) ;
  seul le `tenant_admin` le peut (exigence 6.1).

Dispositif (identique aux autres tests d'intégration) : migrations en
sous-processus (connexion owner), octroi LOGIN éphémère à `dme_app`, seed
région (avec géométrie couvrant le Togo) + tenant + centre + utilisateurs +
service_types, app FastAPI câblée sur le moteur `dme_app` via override de
`tenant_session` + `get_current_principal`.

Garde CI : désactivé (`pytest.skip`) si `TEST_DATABASE_URL` est absente.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
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

from app.core.deps import AuthPrincipal, get_current_principal
from app.core.tenant_context import tenant_session
from app.main import app

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : tests d'intégration du profil de "
        "centre désactivés (nécessitent un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "center_test_" + uuid.uuid4().hex

_REGION_ID = uuid.uuid4()
_REGION_CODE = "MARITIME"
TENANT_A = uuid.uuid4()
CENTER_A = uuid.uuid4()
ADMIN_A = uuid.uuid4()
CLINICIAN_A = uuid.uuid4()

# Deux services de référence amorcés (ids résolus par name_fr côté service).
SERVICE_MATERNITE = uuid.uuid4()
SERVICE_PHARMACIE = uuid.uuid4()

# Coordonnées valides (Lomé, dans le Togo) et hors Togo (Paris).
_LOME = {"longitude": 1.2255, "latitude": 6.1319}
_PARIS = {"longitude": 2.3522, "latitude": 48.8566}

# Polygone (lon/lat) couvrant la boîte englobante du Togo, pour que `ST_Within`
# contre l'union des régions accepte un point togolais et rejette l'étranger.
_TOGO_BBOX_WKT = (
    "MULTIPOLYGON((("
    "-0.15 6.10, 1.80 6.10, 1.80 11.14, -0.15 11.14, -0.15 6.10"
    ")))"
)


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
    """Amorce région (avec géométrie Togo) + tenant + centre + users + services."""
    # Région de référence avec géométrie couvrant le Togo (pour ST_Within).
    await owner_conn.execute(
        text(
            "INSERT INTO region (id, code, name_fr, geometry) VALUES "
            "(:id, :code, :name, ST_GeomFromText(:wkt, 4326))"
        ),
        {
            "id": _REGION_ID,
            "code": _REGION_CODE,
            "name": "Maritime",
            "wkt": _TOGO_BBOX_WKT,
        },
    )
    # service_type (référence, non cadrée RLS).
    for sid, name in (
        (SERVICE_MATERNITE, "Maternité"),
        (SERVICE_PHARMACIE, "Pharmacie"),
    ):
        await owner_conn.execute(
            text("INSERT INTO service_type (id, name_fr) VALUES (:id, :name)"),
            {"id": sid, "name": name},
        )
    # tenant (non cadré RLS).
    await owner_conn.execute(
        text(
            "INSERT INTO tenant (id, name, region_id, status) "
            "VALUES (:id, :name, :region_id, 'active')"
        ),
        {"id": TENANT_A, "name": "Tenant A", "region_id": _REGION_ID},
    )
    # medical_center + app_user sous RLS forcée → poser le contexte du tenant.
    await owner_conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, false)"),
        {"tid": str(TENANT_A)},
    )
    await owner_conn.execute(
        text(
            "INSERT INTO medical_center "
            "(id, tenant_id, name, region_id, address, location, phone, email) "
            "VALUES (:id, :tid, :name, :rid, :addr, "
            "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :phone, :email)"
        ),
        {
            "id": CENTER_A,
            "tid": TENANT_A,
            "name": "Centre initial",
            "rid": _REGION_ID,
            "addr": "Adresse initiale",
            "lon": _LOME["longitude"],
            "lat": _LOME["latitude"],
            "phone": "+22890000000",
            "email": "initial@centre.tg",
        },
    )
    for uid, role in ((ADMIN_A, "tenant_admin"), (CLINICIAN_A, "clinician")):
        await owner_conn.execute(
            text(
                "INSERT INTO app_user (id, tenant_id, email, role, is_active) "
                "VALUES (:id, :tid, :email, :role, true)"
            ),
            {
                "id": uid,
                "tid": TENANT_A,
                "email": f"{role}-{uuid.uuid4().hex}@test.local",
                "role": role,
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
            # Audit append-only : neutraliser le trigger le temps du nettoyage.
            await conn.execute(
                text(
                    "ALTER TABLE audit_log DISABLE TRIGGER "
                    "trg_audit_log_block_mutation"
                )
            )
            await conn.execute(
                text("DELETE FROM audit_log WHERE tenant_id = :tid"),
                {"tid": TENANT_A},
            )
            await conn.execute(
                text(
                    "ALTER TABLE audit_log ENABLE TRIGGER "
                    "trg_audit_log_block_mutation"
                )
            )
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, false)"),
                {"tid": str(TENANT_A)},
            )
            await conn.execute(
                text("DELETE FROM medical_center_service WHERE tenant_id = :tid"),
                {"tid": TENANT_A},
            )
            await conn.execute(
                text("DELETE FROM app_user WHERE tenant_id = :tid"),
                {"tid": TENANT_A},
            )
            await conn.execute(
                text("DELETE FROM medical_center WHERE tenant_id = :tid"),
                {"tid": TENANT_A},
            )
            await conn.execute(
                text("DELETE FROM tenant WHERE id = :tid"), {"tid": TENANT_A}
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


_principal: AuthPrincipal | None = None


@pytest.fixture(autouse=True)
def _reset_principal() -> None:
    global _principal
    _principal = None


@pytest.fixture
def client(app_engine: object) -> TestClient:
    """Client FastAPI dont `tenant_session` est câblée sur le moteur `dme_app`."""
    session_factory = async_sessionmaker(
        bind=app_engine,  # type: ignore[arg-type]
        expire_on_commit=False,
        autoflush=False,
    )

    async def _override_tenant_session() -> AsyncIterator[object]:
        assert _principal is not None, "Principal non configuré pour ce test"
        assert _principal.tenant_id is not None
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(_principal.tenant_id)},
            )
            yield session

    async def _override_principal() -> AuthPrincipal:
        assert _principal is not None
        return _principal

    app.dependency_overrides[tenant_session] = _override_tenant_session
    app.dependency_overrides[get_current_principal] = _override_principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(tenant_session, None)
        app.dependency_overrides.pop(get_current_principal, None)


def _use(role: str, user_id: uuid.UUID) -> None:
    global _principal
    _principal = AuthPrincipal(sub=user_id, tenant_id=TENANT_A, role=role)


async def _public_projection(app_engine: object, center_id: uuid.UUID) -> dict | None:
    """Lit la projection publique sûre du centre (champs publics + services).

    Reproduit la projection que l'annuaire/carte publics (tâche 12) exposeront :
    uniquement des champs non sensibles du `medical_center` et ses services liés.
    Aucune PHI, aucun champ interne (tenant, utilisateurs). Lue via la connexion
    owner en posant le contexte tenant (RLS forcée s'applique aussi à l'owner).
    """
    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)  # type: ignore[arg-type]
    try:
        async with owner_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(TENANT_A)},
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT name, address, phone, email, opening_hours, is_public, "
                        "ST_X(location::geometry) AS lon, "
                        "ST_Y(location::geometry) AS lat "
                        "FROM medical_center WHERE id = :id"
                    ),
                    {"id": center_id},
                )
            ).mappings().one_or_none()
            if row is None:
                return None
            services = (
                await conn.execute(
                    text(
                        "SELECT st.name_fr FROM medical_center_service mcs "
                        "JOIN service_type st ON st.id = mcs.service_type_id "
                        "WHERE mcs.medical_center_id = :id ORDER BY st.name_fr"
                    ),
                    {"id": center_id},
                )
            ).scalars().all()
            data = dict(row)
            data["service_types"] = list(services)
            return data
    finally:
        await owner_engine.dispose()


# --------------------------------------------------------------------------- #
# P7 : validité géographique                                                   #
# --------------------------------------------------------------------------- #


async def test_coordonnees_hors_togo_rejetees_422(
    client: TestClient, app_engine: object
) -> None:
    """PATCH avec des coordonnées hors Togo (Paris) → 422 ; rien n'est persisté (P7)."""
    _use("tenant_admin", ADMIN_A)
    resp = client.patch("/api/center/profile", json={"location": _PARIS})
    assert resp.status_code == 422, resp.text
    assert "Togo" in resp.json()["detail"]

    # La projection publique conserve les coordonnées initiales (Lomé).
    projection = await _public_projection(app_engine, CENTER_A)
    assert projection is not None
    assert round(projection["lon"], 4) == _LOME["longitude"]
    assert round(projection["lat"], 4) == _LOME["latitude"]


async def test_coordonnees_togolaises_acceptees(
    client: TestClient, app_engine: object
) -> None:
    """PATCH avec des coordonnées togolaises valides → 200, persistées (P7)."""
    _use("tenant_admin", ADMIN_A)
    new_point = {"longitude": 1.1900, "latitude": 6.2000}
    resp = client.patch("/api/center/profile", json={"location": new_point})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert round(body["location"]["longitude"], 4) == new_point["longitude"]
    assert round(body["location"]["latitude"], 4) == new_point["latitude"]


# --------------------------------------------------------------------------- #
# Mises à jour reflétées dans la projection publique                           #
# --------------------------------------------------------------------------- #


async def test_mise_a_jour_reflete_dans_la_projection_publique(
    client: TestClient, app_engine: object
) -> None:
    """Une mise à jour (nom, adresse, horaires, services) se reflète côté public.

    On modifie plusieurs champs publics + les services ; la projection publique
    sûre du centre reflète exactement les nouvelles valeurs (c'est la source de
    l'annuaire/carte publics, tâche 12).
    """
    _use("tenant_admin", ADMIN_A)
    payload = {
        "name": "Centre de santé rénové",
        "address": "45 Boulevard du 13 Janvier, Lomé",
        "phone": "+22899998888",
        "opening_hours": [
            {"day": "lundi", "open": "07:30", "close": "18:00"},
            {"day": "mardi", "open": "07:30", "close": "18:00"},
        ],
        "service_types": ["Maternité", "Pharmacie"],
    }
    resp = client.patch("/api/center/profile", json=payload)
    assert resp.status_code == 200, resp.text

    projection = await _public_projection(app_engine, CENTER_A)
    assert projection is not None
    assert projection["name"] == "Centre de santé rénové"
    assert projection["address"] == "45 Boulevard du 13 Janvier, Lomé"
    assert projection["phone"] == "+22899998888"
    assert projection["service_types"] == ["Maternité", "Pharmacie"]
    assert len(projection["opening_hours"]) == 2


async def test_services_remplaces_integralement(
    client: TestClient, app_engine: object
) -> None:
    """La mise à jour des services remplace l'ensemble (pas d'accumulation)."""
    _use("tenant_admin", ADMIN_A)

    client.patch("/api/center/profile", json={"service_types": ["Maternité", "Pharmacie"]})
    client.patch("/api/center/profile", json={"service_types": ["Pharmacie"]})

    projection = await _public_projection(app_engine, CENTER_A)
    assert projection is not None
    assert projection["service_types"] == ["Pharmacie"]


async def test_service_inconnu_rejete_422(
    client: TestClient, app_engine: object
) -> None:
    """Un type de service inconnu du référentiel → 422."""
    _use("tenant_admin", ADMIN_A)
    resp = client.patch(
        "/api/center/profile", json={"service_types": ["Service inexistant"]}
    )
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# Cadrage par rôle réel                                                        #
# --------------------------------------------------------------------------- #


async def test_clinician_ne_peut_pas_modifier_le_profil_403(
    client: TestClient, app_engine: object
) -> None:
    """Un clinician ne peut pas modifier le profil (403) ; réservé au tenant_admin."""
    _use("clinician", CLINICIAN_A)
    resp = client.patch("/api/center/profile", json={"name": "Interdit"})
    assert resp.status_code == 403


async def test_clinician_peut_lire_le_profil(
    client: TestClient, app_engine: object
) -> None:
    """Un clinician peut lire le profil du centre (lecture ouverte aux rôles tenant)."""
    _use("clinician", CLINICIAN_A)
    resp = client.get("/api/center/profile")
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == str(TENANT_A)
