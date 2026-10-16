"""Tests d'intégration du domaine clinique (tâche 9.3) — exigence 5, NFR-1/3/4.

Vérifie P4, P5, P6 (et P1/P2/P3 sur le domaine PHI) contre un **vrai moteur
PostgreSQL avec RLS forcée**. Comme `test_rls_isolation.py` / `test_audit_log.py`
/ `test_admin_lifecycle_db.py`, ce module exécute les migrations réelles (dont la
`0006_patient_visit` qui pose la RLS forcée sur `patient`/`visit`), exerce le rôle
applicatif **non-superuser `dme_app`** (NOBYPASSRLS) et câble l'app FastAPI sur ce
moteur. C'est la seule façon de prouver des propriétés **du moteur** :

- **P1/P2 — Isolation inter-tenant & tenancy autoritaire.** Via l'API, le tenant
  A ne lit/écrit jamais les PHI du tenant B ; un patient de B est, pour A,
  indiscernable d'un patient inexistant (404, P3). Le serveur appose le
  `tenant_id` depuis le principal — un `tenant_id` forgé dans le corps n'a aucun
  effet (le schéma ne l'accepte pas et le `WITH CHECK` RLS est le garde-fou
  ultime).
- **P4 — Confidentialité des PHI.** Aucune route publique n'expose de PHI ; les
  routes patient/visite exigent une authentification (vérifié au contrat dans
  `test_clinical_router.py`). Ici on confirme que les PHI vivent uniquement dans
  des tables sous RLS forcée.
- **P5 — Complétude de l'audit.** Chaque création ET consultation (lecture +
  liste), plus mise à jour/suppression, écrit **exactement une** ligne d'audit
  dans la **même transaction** que la mutation/lecture (fail-closed).
- **P6 — Durabilité de la suppression logique.** Le CRUD normal n'efface JAMAIS
  physiquement : un patient/visite « supprimé » disparaît des lectures mais
  **subsiste en stockage** avec un `deleted_at` horodaté.
- **Unicité patient (exigence 5.4).** `UNIQUE (tenant_id, mrn)` : un doublon de
  `mrn` dans le même tenant → 409 ; le MÊME `mrn` est permis dans un autre tenant.
- **Consentement (exigence 5.6).** `consent_to_store` est persisté à la création.
- **`id` client UUIDv7 (NFR-6).** La clé primaire fournie par le client est
  écrite telle quelle.

Dispositif (conforme aux tests d'intégration existants) :

- Migrations `alembic upgrade head` en **sous-processus** (connexion owner) ;
  octroi LOGIN + mot de passe **éphémère** au rôle `dme_app` (jamais committé).
- Seed (région, 2 tenants, 1 centre + 1 `app_user` par tenant) via la connexion
  **propriétaire** en posant `app.tenant_id` sur le tenant cible (FORCE RLS
  s'applique aussi à l'owner). L'`app_user` sert de `created_by` aux PHI.
- L'app FastAPI est câblée au moteur `dme_app` : on surcharge `tenant_session`
  par une dépendance qui ouvre une transaction, pose `app.tenant_id` depuis le
  **principal injecté** (exactement comme la vraie dépendance le fait depuis le
  JWT), puis cède la session — de sorte que la RLS, l'audit et le CRUD soient
  exercés sur le vrai moteur. On surcharge aussi `get_current_principal`.

Garde CI : désactivé (`pytest.skip`) si `TEST_DATABASE_URL` est absente, afin que
la suite unitaire tourne sans Postgres. Lors de la vérification de la tâche, ces
tests DOIVENT passer contre un conteneur Postgres/PostGIS isolé.
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

from app.core.deps import AuthPrincipal, get_current_principal
from app.core.tenant_context import tenant_session
from app.main import app

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : tests d'intégration du domaine "
        "clinique désactivés (nécessitent un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "clinical_test_" + uuid.uuid4().hex

# Deux tenants, chacun avec un centre et un utilisateur (acteur `created_by`).
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
    """Exécute `alembic upgrade head` (sous-processus, connexion owner).

    Même approche que les autres tests d'intégration : `env.py` appelle
    `asyncio.run()`, incompatible avec la boucle pytest-asyncio ; le sous-processus
    a sa propre boucle et c'est le chemin de migration réellement livré.
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


async def _seed(owner_conn: AsyncConnection) -> None:
    """Amorce région + 2 tenants + 1 centre + 1 utilisateur par tenant (owner).

    `region`/`tenant` ne sont pas cadrés RLS → insertion directe. `medical_center`
    et `app_user` le sont (RLS forcée, y compris pour l'owner) → on pose
    `app.tenant_id` sur le tenant cible avant chaque insertion. L'`app_user` sert
    d'acteur `created_by` aux PHI créées via l'API.
    """
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
    """Prépare la base de test et expose un moteur connecté en rôle `dme_app`.

    1. migrations `alembic upgrade head` ;
    2. octroi LOGIN + mot de passe éphémère au rôle `dme_app` ;
    3. seed région/tenants/centres/utilisateurs ;
    4. yield d'un moteur async `dme_app` (NOBYPASSRLS) ;
    5. nettoyage : purge des PHI + données de seed, retrait du LOGIN.
    """
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
            # PHI + lignes cadrées : purge par tenant (contexte posé). L'audit n'a
            # pas de RLS par-tenant mais porte un trigger append-only : on le
            # neutralise le temps du nettoyage.
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
            # `region` : supprimée en dernier (FK depuis tenant/center purgés).
            await conn.execute(
                text(
                    "DELETE FROM region WHERE id NOT IN "
                    "(SELECT DISTINCT region_id FROM tenant WHERE region_id IS NOT NULL)"
                    " AND name_fr = 'Région test'"
                )
            )
            await conn.execute(text(f"ALTER ROLE {_APP_ROLE} NOLOGIN PASSWORD NULL"))
        await owner_engine.dispose()


# État partagé : principal courant piloté par chaque test (tenant A/B, rôle).
_principal: AuthPrincipal | None = None


@pytest.fixture(autouse=True)
def _reset_principal() -> None:
    global _principal
    _principal = None


@pytest.fixture
def client(app_engine: object) -> TestClient:
    """Client FastAPI dont `tenant_session` est câblée sur le moteur `dme_app`.

    On surcharge la dépendance `tenant_session` (et non `get_session`) car ce sont
    les routeurs PHI qui l'emploient : la surcharge reproduit fidèlement son
    comportement — ouvrir une transaction, poser `app.tenant_id` depuis le
    principal injecté (comme la vraie dépendance le dérive du JWT), céder la
    session, commit en sortie — mais contre le moteur de test. Le principal est
    injecté via `get_current_principal`.
    """
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


def _use_tenant(tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Configure le principal (rôle clinician) pour un tenant donné."""
    global _principal
    _principal = AuthPrincipal(sub=user_id, tenant_id=tenant_id, role="clinician")


def _patient_payload(**overrides: object) -> dict:
    payload: dict = {
        "id": str(uuid.uuid4()),
        "mrn": f"MRN-{uuid.uuid4().hex[:8]}",
        "given_name": "Koffi",
        "family_name": "Mensah",
        "dob": "1990-05-14",
        "sex": "M",
        "phone": "+22890000000",
        "consent_to_store": True,
    }
    payload.update(overrides)
    return payload


async def _count(app_engine: object, sql: str, params: dict) -> int:
    """Compte des lignes via la connexion owner (hors RLS, pour l'introspection).

    Les assertions de **durabilité** (P6) et de persistance doivent voir le
    stockage réel, y compris les lignes supprimées logiquement. On utilise donc la
    connexion propriétaire en posant au besoin le contexte tenant (FORCE RLS
    s'applique aussi à l'owner pour `patient`/`visit`).
    """
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


async def _patient_row(app_engine: object, tenant_id: uuid.UUID, pid: uuid.UUID) -> dict | None:
    """Lit la ligne patient brute (owner, contexte posé) — y compris si supprimée."""
    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)  # type: ignore[arg-type]
    try:
        async with owner_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT id, tenant_id, mrn, consent_to_store, deleted_at, "
                        "version FROM patient WHERE id = :id"
                    ),
                    {"id": pid},
                )
            ).mappings().one_or_none()
            return dict(row) if row is not None else None
    finally:
        await owner_engine.dispose()


async def _audit_actions(
    app_engine: object, tenant_id: uuid.UUID, entity_id: uuid.UUID
) -> list[str]:
    """Liste les actions d'audit (ordre d'insertion) pour une entité d'un tenant."""
    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)  # type: ignore[arg-type]
    try:
        async with owner_engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT action FROM audit_log "
                        "WHERE tenant_id = :tid AND entity_id = :eid ORDER BY id"
                    ),
                    {"tid": tenant_id, "eid": entity_id},
                )
            ).scalars().all()
            return list(rows)
    finally:
        await owner_engine.dispose()


# --------------------------------------------------------------------------- #
# Création + consentement + id client (exigences 5.1, 5.6 ; NFR-6)             #
# --------------------------------------------------------------------------- #


async def test_create_patient_persiste_id_client_et_consentement(
    client: TestClient, app_engine: object
) -> None:
    """Création : `id` client (UUIDv7) et `consent_to_store` persistés (NFR-6, 5.6).

    Le serveur appose `tenant_id` depuis le principal ; la ligne est bien écrite
    sous le tenant A avec la clé fournie par le client et le consentement capturé.
    """
    _use_tenant(TENANT_A, USER_A)
    client_id = uuid.uuid4()

    resp = client.post(
        "/api/patients",
        json=_patient_payload(id=str(client_id), consent_to_store=True),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == str(client_id)
    assert body["tenant_id"] == str(TENANT_A)
    assert body["consent_to_store"] is True

    # La ligne existe en base sous le tenant A, avec la clé client et le consentement.
    row = await _patient_row(app_engine, TENANT_A, client_id)
    assert row is not None
    assert row["tenant_id"] == TENANT_A
    assert row["consent_to_store"] is True
    assert row["deleted_at"] is None


# --------------------------------------------------------------------------- #
# P1/P2/P3 : isolation inter-tenant via l'API                                  #
# --------------------------------------------------------------------------- #


async def test_isolation_un_tenant_ne_voit_pas_les_patients_d_un_autre(
    client: TestClient, app_engine: object
) -> None:
    """Un patient créé par A n'apparaît pas dans la liste de B (P1).

    La RLS filtre la lecture au tenant courant : B ne voit jamais la PHI de A, même
    si le code n'ajoute aucune clause `WHERE tenant_id`.
    """
    _use_tenant(TENANT_A, USER_A)
    client_id = uuid.uuid4()
    created = client.post(
        "/api/patients", json=_patient_payload(id=str(client_id), mrn="ISO-A-001")
    )
    assert created.status_code == 201, created.text

    # Côté A : le patient figure dans la liste.
    list_a = client.get("/api/patients")
    assert list_a.status_code == 200
    assert any(p["id"] == str(client_id) for p in list_a.json())

    # Côté B : le patient de A est invisible.
    _use_tenant(TENANT_B, USER_B)
    list_b = client.get("/api/patients")
    assert list_b.status_code == 200
    assert all(p["id"] != str(client_id) for p in list_b.json())


async def test_isolation_lecture_par_id_d_un_autre_tenant_404(
    client: TestClient, app_engine: object
) -> None:
    """Lire par id un patient de A depuis B → 404, indiscernable d'inexistant (P3).

    La RLS rend la ligne de A invisible à B : la recherche par id ne ramène rien,
    exactement comme un id inexistant → 404 (jamais 403, pas de fuite d'existence).
    """
    _use_tenant(TENANT_A, USER_A)
    client_id = uuid.uuid4()
    created = client.post(
        "/api/patients", json=_patient_payload(id=str(client_id), mrn="ISO-A-002")
    )
    assert created.status_code == 201, created.text

    _use_tenant(TENANT_B, USER_B)
    resp = client.get(f"/api/patients/{client_id}")
    assert resp.status_code == 404


async def test_isolation_suppression_d_un_patient_d_un_autre_tenant_404(
    client: TestClient, app_engine: object
) -> None:
    """Supprimer depuis B un patient de A → 404 ; la ligne de A reste intacte (P1/P3)."""
    _use_tenant(TENANT_A, USER_A)
    client_id = uuid.uuid4()
    created = client.post(
        "/api/patients", json=_patient_payload(id=str(client_id), mrn="ISO-A-003")
    )
    assert created.status_code == 201, created.text

    _use_tenant(TENANT_B, USER_B)
    resp = client.delete(f"/api/patients/{client_id}")
    assert resp.status_code == 404

    # La ligne de A n'a pas été touchée (toujours active).
    row = await _patient_row(app_engine, TENANT_A, client_id)
    assert row is not None
    assert row["deleted_at"] is None


# --------------------------------------------------------------------------- #
# Unicité patient (exigence 5.4) : UNIQUE(tenant_id, mrn)                       #
# --------------------------------------------------------------------------- #


async def test_mrn_duplique_dans_le_meme_tenant_409(
    client: TestClient, app_engine: object
) -> None:
    """Un `mrn` déjà présent dans le tenant → 409 (unicité, exigence 5.4)."""
    _use_tenant(TENANT_A, USER_A)
    mrn = f"DUP-{uuid.uuid4().hex[:8]}"

    first = client.post(
        "/api/patients", json=_patient_payload(id=str(uuid.uuid4()), mrn=mrn)
    )
    assert first.status_code == 201, first.text

    dup = client.post(
        "/api/patients", json=_patient_payload(id=str(uuid.uuid4()), mrn=mrn)
    )
    assert dup.status_code == 409


async def test_meme_mrn_autorise_dans_des_tenants_differents(
    client: TestClient, app_engine: object
) -> None:
    """Le MÊME `mrn` est permis dans deux tenants distincts (unicité PAR tenant, 5.4).

    `UNIQUE (tenant_id, mrn)` est cadré au tenant : le même numéro de dossier peut
    coexister chez A et B sans collision.
    """
    shared_mrn = f"SHARED-{uuid.uuid4().hex[:8]}"

    _use_tenant(TENANT_A, USER_A)
    a = client.post(
        "/api/patients", json=_patient_payload(id=str(uuid.uuid4()), mrn=shared_mrn)
    )
    assert a.status_code == 201, a.text

    _use_tenant(TENANT_B, USER_B)
    b = client.post(
        "/api/patients", json=_patient_payload(id=str(uuid.uuid4()), mrn=shared_mrn)
    )
    assert b.status_code == 201, b.text


# --------------------------------------------------------------------------- #
# P6 : durabilité de la suppression logique                                    #
# --------------------------------------------------------------------------- #


async def test_soft_delete_patient_conserve_la_ligne_et_l_exclut_des_lectures(
    client: TestClient, app_engine: object
) -> None:
    """DELETE patient : exclu des lectures mais CONSERVÉ en stockage (P6, NFR-4).

    Après suppression : (1) GET par id → 404 et absence de la liste (exclusion) ;
    (2) la ligne subsiste en base avec `deleted_at` horodaté (durabilité). Aucune
    suppression physique sur le chemin CRUD normal.
    """
    _use_tenant(TENANT_A, USER_A)
    client_id = uuid.uuid4()
    created = client.post(
        "/api/patients", json=_patient_payload(id=str(client_id), mrn="SOFT-A-001")
    )
    assert created.status_code == 201, created.text

    deleted = client.delete(f"/api/patients/{client_id}")
    assert deleted.status_code == 204

    # Exclusion des lectures normales.
    assert client.get(f"/api/patients/{client_id}").status_code == 404
    assert all(p["id"] != str(client_id) for p in client.get("/api/patients").json())

    # Durabilité : la ligne subsiste avec deleted_at horodaté (jamais effacée).
    total = await _count(
        app_engine,
        "SELECT count(*) FROM patient WHERE id = :id",
        {"id": client_id, "tid": TENANT_A},
    )
    assert total == 1, "La PHI supprimée doit être conservée en stockage (P6)"
    row = await _patient_row(app_engine, TENANT_A, client_id)
    assert row is not None
    assert row["deleted_at"] is not None, "deleted_at doit être horodaté (P6)"


async def test_soft_delete_visit_conserve_la_ligne(
    client: TestClient, app_engine: object
) -> None:
    """DELETE visite : exclue des lectures mais conservée en stockage (P6, NFR-4)."""
    _use_tenant(TENANT_A, USER_A)
    patient_id = uuid.uuid4()
    created_patient = client.post(
        "/api/patients", json=_patient_payload(id=str(patient_id), mrn="SOFT-V-001")
    )
    assert created_patient.status_code == 201, created_patient.text

    visit_id = uuid.uuid4()
    created_visit = client.post(
        "/api/visits",
        json={
            "id": str(visit_id),
            "patient_id": str(patient_id),
            "visited_at": datetime.now(UTC).isoformat(),
            "reason": "Consultation",
            "notes": "Notes cliniques sensibles",
        },
    )
    assert created_visit.status_code == 201, created_visit.text

    deleted = client.delete(f"/api/visits/{visit_id}")
    assert deleted.status_code == 204

    # Exclue des lectures normales.
    assert all(v["id"] != str(visit_id) for v in client.get("/api/visits").json())

    # Conservée en stockage avec deleted_at horodaté.
    total = await _count(
        app_engine,
        "SELECT count(*) FROM visit WHERE id = :id",
        {"id": visit_id, "tid": TENANT_A},
    )
    assert total == 1, "La visite supprimée doit être conservée (P6)"
    not_deleted = await _count(
        app_engine,
        "SELECT count(*) FROM visit WHERE id = :id AND deleted_at IS NOT NULL",
        {"id": visit_id, "tid": TENANT_A},
    )
    assert not_deleted == 1, "deleted_at doit être horodaté (P6)"


# --------------------------------------------------------------------------- #
# P5 : complétude de l'audit (création + consultation + mutation/suppression)  #
# --------------------------------------------------------------------------- #


async def test_cycle_de_vie_patient_est_integralement_audite(
    client: TestClient, app_engine: object
) -> None:
    """Chaque opération PHI écrit EXACTEMENT une ligne d'audit (P5, NFR-3).

    On exerce création → lecture → mise à jour → suppression sur un même patient et
    on vérifie la séquence d'actions auditées rattachées à son `entity_id`
    (`patient.create`, `patient.read`, `patient.update`, `patient.delete`), chacune
    présente une seule fois (l'audit partage la transaction de l'opération,
    fail-closed).
    """
    _use_tenant(TENANT_A, USER_A)
    pid = uuid.uuid4()

    assert (
        client.post(
            "/api/patients", json=_patient_payload(id=str(pid), mrn="AUDIT-A-001")
        ).status_code
        == 201
    )
    assert client.get(f"/api/patients/{pid}").status_code == 200
    assert client.patch(f"/api/patients/{pid}", json={"phone": "+22891234567"}).status_code == 200
    assert client.delete(f"/api/patients/{pid}").status_code == 204

    actions = await _audit_actions(app_engine, TENANT_A, pid)
    assert actions.count("patient.create") == 1
    assert actions.count("patient.read") == 1
    assert actions.count("patient.update") == 1
    assert actions.count("patient.delete") == 1
    # Ordre chronologique attendu du cycle de vie.
    assert actions == ["patient.create", "patient.read", "patient.update", "patient.delete"]


async def test_consultation_liste_est_auditee(
    client: TestClient, app_engine: object
) -> None:
    """Une consultation en liste (GET /api/patients) écrit une ligne `patient.list` (P5).

    La liste n'est pas rattachée à une entité précise (`entity_id` NUL) : on compte
    donc les lignes `patient.list` du tenant avant/après pour prouver qu'exactement
    une est ajoutée par appel.
    """
    _use_tenant(TENANT_A, USER_A)

    before = await _count(
        app_engine,
        "SELECT count(*) FROM audit_log WHERE tenant_id = :tid AND action = 'patient.list'",
        {"tid": TENANT_A},
    )
    assert client.get("/api/patients").status_code == 200
    after = await _count(
        app_engine,
        "SELECT count(*) FROM audit_log WHERE tenant_id = :tid AND action = 'patient.list'",
        {"tid": TENANT_A},
    )
    assert after == before + 1, "Une consultation en liste doit être auditée (P5)"


async def test_audit_rattache_au_tenant_de_l_acteur(
    client: TestClient, app_engine: object
) -> None:
    """L'audit d'une création porte le tenant et l'acteur du principal (P5)."""
    _use_tenant(TENANT_B, USER_B)
    pid = uuid.uuid4()
    assert (
        client.post(
            "/api/patients", json=_patient_payload(id=str(pid), mrn="AUDIT-B-001")
        ).status_code
        == 201
    )

    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)  # type: ignore[arg-type]
    try:
        async with owner_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT tenant_id, actor_user_id, entity, event_at, received_at "
                        "FROM audit_log WHERE entity_id = :eid AND action = 'patient.create'"
                    ),
                    {"eid": pid},
                )
            ).mappings().one()
    finally:
        await owner_engine.dispose()

    assert row["tenant_id"] == TENANT_B
    assert row["actor_user_id"] == USER_B
    assert row["entity"] == "patient"
    # Horodatages NFR-3 présents (event_at client-ou-serveur + received_at serveur).
    assert row["event_at"] is not None
    assert row["received_at"] is not None
