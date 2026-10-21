"""Tests d'intégration export + effacement par tenant (spec `tenant-data-rights`,
#2 — tâche 8).

Contrairement aux tests unitaires (`test_tenant_erase_service.py` sur session
factice, `test_tenant_data_schema.py` sur la forme), ce module exerce le **vrai
moteur PostgreSQL/PostGIS avec RLS forcée** via le rôle applicatif non-superuser
`dme_app`. C'est la seule façon de prouver les garanties de bout en bout des
endpoints admin `POST /api/admin/tenants/{id}/export` et `.../erase` :

- **Export cadré + sans secret (exigence 1).** Deux tenants peuplés ; l'export de
  l'un ne porte QUE ses données (aucune fuite inter-tenant, exigence 1.3) et
  AUCUN secret (`password_hash`, `setup_token_hash`, `setup_token_selector`,
  jetons de refresh — exigence 1.2) ; une ligne d'audit `tenant.export` est écrite
  (exigence 1.4).
- **Effacement cadré, audité, préservant l'audit (exigence 2).** L'effacement d'un
  tenant **inactif** supprime ses PHI + son identité, **laisse `audit_log` intact**
  (y compris la ligne `tenant.erase` écrite AVANT l'acte), et ne touche PAS l'autre
  tenant (isolation RLS, exigence 2.4). Tentative sur un tenant **actif** → 409
  (exigence 2.6). Tenant inconnu → 404 (exigence 2.7).
- **Append-only prouvé (exigence 4.4, NFR-3).** Aucune ligne d'`audit_log` n'est
  supprimable par le rôle applicatif `dme_app` : le trigger `audit_log_block_mutation`
  lève, et le GRANT ne porte de toute façon pas DELETE.

Dispositif (identique à `test_rls_isolation.py` / `test_admin_lifecycle_db.py`) :
migrations réelles en sous-processus, octroi LOGIN éphémère à `dme_app`, moteur
`dme_app` injecté dans l'app via `app.dependency_overrides[get_session]`, principal
`platform_admin` injecté. Les données des deux tenants sont amorcées via la
connexion **propriétaire** (owner), en posant `app.tenant_id` sur le tenant cible
pour satisfaire le `WITH CHECK` de la RLS forcée. L'audit d'intention d'effacement
passe par `record_audit_standalone`, qui ouvre sa propre session ; on repointe donc
sa fabrique de sessions vers le moteur de test. Désactivé si `TEST_DATABASE_URL`
est absente.
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
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.core.audit as audit_module
from app.core.db import get_session
from app.core.deps import AuthPrincipal, get_current_principal
from app.main import app

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : tests d'intégration export/effacement "
        "par tenant désactivés (nécessitent un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "tenant_data_test_" + uuid.uuid4().hex

# Région de référence (non cadrée RLS) commune aux deux tenants de test.
_REGION_ID = uuid.uuid4()
_REGION_CODE = f"TD-{uuid.uuid4().hex[:6]}"
_PLATFORM_ADMIN_ID = uuid.uuid4()

# Deux tenants peuplés, identités fixes pour des assertions nettes. Le tenant A est
# désactivé (candidat à l'effacement) ; le tenant B reste actif (témoin d'isolation
# + sujet du test 409 sur tenant actif).
TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
CENTER_A = uuid.uuid4()
CENTER_B = uuid.uuid4()
USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
PATIENT_A = uuid.uuid4()
PATIENT_B = uuid.uuid4()
VISIT_A = uuid.uuid4()
VISIT_B = uuid.uuid4()

# Secrets semés sur chaque compte : ils NE DOIVENT JAMAIS apparaître dans l'export
# (exigence 1.2). On vérifie leur absence textuelle dans le corps JSON sérialisé.
_PASSWORD_HASH_SENTINEL = "$argon2id$v=19$m=8,t=1,p=1$SECRETPWHASHSENTINEL"
_SETUP_HASH_SENTINEL = "$argon2id$v=19$m=8,t=1,p=1$SECRETSETUPHASHSENTINEL"
_SETUP_SELECTOR_SENTINEL = "SECRET_SETUP_SELECTOR_SENTINEL"


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
    """Amorce région, deux tenants peuplés (centre, compte avec secrets, PHI).

    `region`/`tenant` ne sont pas cadrés RLS → insertion directe. Les tables
    cadrées (`medical_center`, `app_user`, `patient`, `visit`) le sont (RLS forcée,
    y compris pour l'owner) → on pose `app.tenant_id` sur le tenant cible avant
    chaque insertion pour satisfaire le `WITH CHECK`. Le tenant A est posé en
    `inactive` (candidat à l'effacement), le tenant B en `active`.
    """
    await owner_conn.execute(
        text("INSERT INTO region (id, code, name_fr) VALUES (:id, :code, :name)"),
        {"id": _REGION_ID, "code": _REGION_CODE, "name": "Région test"},
    )
    for tenant_id, name, status in (
        (TENANT_A, "Tenant A", "inactive"),
        (TENANT_B, "Tenant B", "active"),
    ):
        await owner_conn.execute(
            text(
                "INSERT INTO tenant (id, name, region_id, status) "
                "VALUES (:id, :name, :region_id, :status)"
            ),
            {"id": tenant_id, "name": name, "region_id": _REGION_ID, "status": status},
        )

    seeds = (
        (TENANT_A, CENTER_A, USER_A, PATIENT_A, VISIT_A, "A"),
        (TENANT_B, CENTER_B, USER_B, PATIENT_B, VISIT_B, "B"),
    )
    for tenant_id, center_id, user_id, patient_id, visit_id, tag in seeds:
        # Contexte du tenant cible pour toutes les tables cadrées de ce bloc.
        await owner_conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, false)"),
            {"tid": str(tenant_id)},
        )
        # Centre (avec un point PostGIS pour exercer la projection ST_X/ST_Y).
        await owner_conn.execute(
            text(
                "INSERT INTO medical_center "
                "(id, tenant_id, name, region_id, address, location, phone, email) "
                "VALUES (:id, :tid, :name, :rid, :addr, "
                "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :phone, :email)"
            ),
            {
                "id": center_id,
                "tid": tenant_id,
                "name": f"Centre {tag}",
                "rid": _REGION_ID,
                "addr": f"Adresse {tag}",
                "lon": 1.2255,
                "lat": 6.1319,
                "phone": f"+2289000000{tag}",
                "email": f"contact-{tag.lower()}@test.local",
            },
        )
        # Compte applicatif PORTANT DES SECRETS (ne doivent jamais fuiter, 1.2).
        await owner_conn.execute(
            text(
                "INSERT INTO app_user "
                "(id, tenant_id, email, password_hash, setup_token_hash, "
                "setup_token_selector, role, is_active) "
                "VALUES (:id, :tid, :email, :pw, :sh, :sel, 'tenant_admin', true)"
            ),
            {
                "id": user_id,
                "tid": tenant_id,
                "email": f"admin-{tag.lower()}@test.local",
                "pw": _PASSWORD_HASH_SENTINEL + tag,
                "sh": _SETUP_HASH_SENTINEL + tag,
                "sel": _SETUP_SELECTOR_SENTINEL + tag,
            },
        )
        # Patient (PHI).
        await owner_conn.execute(
            text(
                "INSERT INTO patient "
                "(id, tenant_id, mrn, given_name, family_name, consent_to_store, "
                "created_by) "
                "VALUES (:id, :tid, :mrn, :gn, :fn, true, :cb)"
            ),
            {
                "id": patient_id,
                "tid": tenant_id,
                "mrn": f"MRN-{tag}-001",
                "gn": f"Prénom{tag}",
                "fn": f"Nom{tag}",
                "cb": user_id,
            },
        )
        # Visite (PHI), référençant le patient et le compte (FK RESTRICT).
        await owner_conn.execute(
            text(
                "INSERT INTO visit "
                "(id, tenant_id, patient_id, reason, notes, created_by) "
                "VALUES (:id, :tid, :pid, :reason, :notes, :cb)"
            ),
            {
                "id": visit_id,
                "tid": tenant_id,
                "pid": patient_id,
                "reason": f"Consultation {tag}",
                "notes": f"Note clinique {tag}",
                "cb": user_id,
            },
        )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def app_engine(
    _module_monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[object]:
    """Prépare la base, octroie LOGIN à dme_app, amorce deux tenants, expose le moteur.

    Repointe aussi la fabrique de sessions de `record_audit_standalone` (chemin
    d'audit d'intention de l'effacement) vers le moteur de test, afin que la ligne
    `tenant.erase` écrite AVANT l'acte atterrisse dans la base de test et soit
    vérifiable.
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

    # `record_audit_standalone` (audit d'intention d'effacement) ouvre sa propre
    # session via `audit_module.async_session_factory` (liée au moteur de prod).
    # On la repointe vers le moteur de test pour la durée du module.
    test_session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    _module_monkeypatch.setattr(
        audit_module, "async_session_factory", test_session_factory
    )

    try:
        yield engine
    finally:
        await engine.dispose()
        # Nettoyage : purge des données amorcées/créées puis révocation du LOGIN.
        async with owner_engine.begin() as conn:
            for tenant_id in (TENANT_A, TENANT_B):
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, false)"),
                    {"tid": str(tenant_id)},
                )
                # Ordre FK RESTRICT (enfants d'abord). `IF EXISTS` implicite : un
                # tenant déjà effacé par un test n'a plus de lignes (no-op).
                for table in ("visit", "patient", "medical_center", "app_user"):
                    await conn.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :tid"),
                        {"tid": tenant_id},
                    )
            await conn.execute(
                text("DELETE FROM tenant WHERE id = ANY(:ids)"),
                {"ids": [TENANT_A, TENANT_B]},
            )
            # L'audit est append-only : on ne peut (ni ne doit) le purger via owner
            # — le trigger lèverait. Les lignes d'audit des tenants de test (région
            # dédiée) restent dans la base de test, sans impact sur les autres suites.
            await conn.execute(
                text("DELETE FROM region WHERE id = :rid"), {"rid": _REGION_ID}
            )
            await conn.execute(text(f"ALTER ROLE {_APP_ROLE} NOLOGIN PASSWORD NULL"))
        await owner_engine.dispose()


@pytest.fixture(scope="module")
def _module_monkeypatch() -> AsyncIterator[pytest.MonkeyPatch]:
    """`MonkeyPatch` à portée module (le built-in `monkeypatch` est function-scope)."""
    mp = pytest.MonkeyPatch()
    try:
        yield mp
    finally:
        mp.undo()


@pytest.fixture
def client(app_engine: object) -> AsyncIterator[TestClient]:
    """Client FastAPI dont la session est une vraie session `dme_app` (RLS forcée)."""
    session_factory = async_sessionmaker(
        bind=app_engine,  # type: ignore[arg-type]
        expire_on_commit=False,
        autoflush=False,
    )

    async def _override_session() -> AsyncIterator[object]:
        async with session_factory() as session:
            yield session

    async def _override_principal() -> AuthPrincipal:
        return AuthPrincipal(
            sub=_PLATFORM_ADMIN_ID, tenant_id=None, role="platform_admin"
        )

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_principal] = _override_principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_principal, None)


# --- Helpers de lecture directe (connexion dme_app, contexte posé) -------------


async def _rows_for_tenant(
    app_engine: object, tenant_id: uuid.UUID, table: str
) -> list[tuple]:
    """Lit les lignes d'une table cadrée RLS pour un tenant (contexte posé)."""
    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        rows = (
            await conn.execute(
                text(f"SELECT * FROM {table} WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).all()
    return rows


async def _audit_actions_for_tenant(
    app_engine: object, tenant_id: uuid.UUID
) -> list[str]:
    """Liste les actions d'audit rattachées à un tenant (append-only, sans RLS)."""
    async with app_engine.connect() as conn:  # type: ignore[attr-defined]
        rows = (
            await conn.execute(
                text(
                    "SELECT action FROM audit_log WHERE tenant_id = :tid ORDER BY id"
                ),
                {"tid": tenant_id},
            )
        ).scalars().all()
    return list(rows)


async def _tenant_exists(app_engine: object, tenant_id: uuid.UUID) -> bool:
    """Vrai si la ligne `tenant` existe encore (table non cadrée RLS)."""
    async with app_engine.connect() as conn:  # type: ignore[attr-defined]
        count = (
            await conn.execute(
                text("SELECT count(*) FROM tenant WHERE id = :id"), {"id": tenant_id}
            )
        ).scalar_one()
    return count == 1


# --- Export : cadré, sans secret, audité (exigence 1) --------------------------


async def test_export_ne_contient_que_les_donnees_du_tenant(
    client: TestClient,
) -> None:
    """L'export du tenant B ne porte QUE ses données (aucune fuite de A, 1.3)."""
    resp = client.post(f"/api/admin/tenants/{TENANT_B}/export")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["tenant"]["id"] == str(TENANT_B)
    assert body["center"]["id"] == str(CENTER_B)
    # Comptes/PHI : uniquement ceux de B, jamais ceux de A.
    user_ids = {u["id"] for u in body["users"]}
    patient_ids = {p["id"] for p in body["patients"]}
    visit_ids = {v["id"] for v in body["visits"]}
    assert user_ids == {str(USER_B)}
    assert patient_ids == {str(PATIENT_B)}
    assert visit_ids == {str(VISIT_B)}
    # Aucune donnée du tenant A ne doit apparaître dans l'export de B.
    assert str(USER_A) not in user_ids
    assert str(PATIENT_A) not in patient_ids
    assert str(VISIT_A) not in visit_ids
    # Tous les tenant_id projetés pointent vers B.
    assert {u["tenant_id"] for u in body["users"]} == {str(TENANT_B)}
    assert {p["tenant_id"] for p in body["patients"]} == {str(TENANT_B)}


async def test_export_ne_contient_aucun_secret(client: TestClient) -> None:
    """L'export n'expose aucun secret (hash de mdp/setup, selector, refresh — 1.2)."""
    resp = client.post(f"/api/admin/tenants/{TENANT_B}/export")
    assert resp.status_code == 200, resp.text

    # Recherche textuelle sur le JSON brut : les sentinelles secrètes semées ne
    # doivent apparaître nulle part (ni valeur, ni clé).
    raw = resp.text
    assert _PASSWORD_HASH_SENTINEL not in raw
    assert _SETUP_HASH_SENTINEL not in raw
    assert _SETUP_SELECTOR_SENTINEL not in raw
    assert "password_hash" not in raw
    assert "setup_token_hash" not in raw
    assert "setup_token_selector" not in raw
    assert "refresh" not in raw.lower()

    # Les comptes sont bien présents, mais sans aucune clé de secret.
    body = resp.json()
    assert body["users"], "L'export doit porter les comptes (sans secret)"
    for user in body["users"]:
        assert "password_hash" not in user
        assert "setup_token_hash" not in user
        assert "setup_token_selector" not in user


async def test_export_ecrit_une_ligne_audit(
    client: TestClient, app_engine: object
) -> None:
    """Un export produit une ligne d'audit `tenant.export` (exigence 1.4)."""
    before = await _audit_actions_for_tenant(app_engine, TENANT_B)
    resp = client.post(f"/api/admin/tenants/{TENANT_B}/export")
    assert resp.status_code == 200, resp.text
    after = await _audit_actions_for_tenant(app_engine, TENANT_B)

    assert after.count("tenant.export") == before.count("tenant.export") + 1


async def test_export_tenant_inconnu_404(client: TestClient) -> None:
    """Export d'un tenant inexistant → 404, sans effet de bord (exigence 1)."""
    resp = client.post(f"/api/admin/tenants/{uuid.uuid4()}/export")
    assert resp.status_code == 404, resp.text


# --- Effacement : garde-fous, isolation, audit préservé (exigence 2) -----------


async def test_erase_tenant_actif_409(client: TestClient, app_engine: object) -> None:
    """Effacement d'un tenant ACTIF (B) → 409, aucune donnée supprimée (2.6)."""
    resp = client.post(f"/api/admin/tenants/{TENANT_B}/erase")
    assert resp.status_code == 409, resp.text

    # Aucune donnée de B n'a été touchée (le refus survient avant tout DELETE).
    assert await _tenant_exists(app_engine, TENANT_B)
    assert len(await _rows_for_tenant(app_engine, TENANT_B, "patient")) == 1
    assert len(await _rows_for_tenant(app_engine, TENANT_B, "visit")) == 1


async def test_erase_tenant_inconnu_404(client: TestClient) -> None:
    """Effacement d'un tenant inexistant → 404 (exigence 2.7)."""
    resp = client.post(f"/api/admin/tenants/{uuid.uuid4()}/erase")
    assert resp.status_code == 404, resp.text


async def test_erase_tenant_inactif_efface_isole_et_preserve_audit(
    client: TestClient, app_engine: object
) -> None:
    """Effacement d'un tenant INACTIF (A) : PHI + identité supprimées, audit intact,
    tenant B intact (isolation RLS) — exigences 2.3, 2.4, 2.5.

    Ce test est le cœur de la tâche 8.1 : il est ordonné (préfixe `test_erase_`)
    après les tests sur B pour que l'effacement destructif de A survienne une fois
    les assertions d'export faites. A étant un tenant distinct de B, l'ordre entre
    les tests n'affecte pas les autres (B n'est jamais effacé).
    """
    # Pré-état : A est peuplé ; l'audit de A est capturé avant l'acte.
    assert await _tenant_exists(app_engine, TENANT_A)
    assert len(await _rows_for_tenant(app_engine, TENANT_A, "patient")) == 1
    assert len(await _rows_for_tenant(app_engine, TENANT_A, "visit")) == 1
    audit_a_before = await _audit_actions_for_tenant(app_engine, TENANT_A)
    audit_b_before = await _audit_actions_for_tenant(app_engine, TENANT_B)
    b_patients_before = await _rows_for_tenant(app_engine, TENANT_B, "patient")

    # Acte : effacement du tenant inactif A.
    resp = client.post(f"/api/admin/tenants/{TENANT_A}/erase")
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["tenant_id"] == str(TENANT_A)
    assert report["audit_preserved"] is True
    # Portée : au moins la PHI et l'identité ont été supprimées (1 ligne chacune).
    assert report["erased_counts"]["visit"] == 1
    assert report["erased_counts"]["patient"] == 1
    assert report["erased_counts"]["app_user"] == 1
    assert report["erased_counts"]["medical_center"] == 1
    assert report["erased_counts"]["tenant"] == 1

    # PHI + identité de A supprimées.
    assert not await _tenant_exists(app_engine, TENANT_A)
    assert await _rows_for_tenant(app_engine, TENANT_A, "patient") == []
    assert await _rows_for_tenant(app_engine, TENANT_A, "visit") == []
    assert await _rows_for_tenant(app_engine, TENANT_A, "app_user") == []
    assert await _rows_for_tenant(app_engine, TENANT_A, "medical_center") == []

    # Audit de A PRÉSERVÉ et ENRICHI de la ligne `tenant.erase` écrite AVANT l'acte
    # (exigences 2.3, 2.5, NFR-3) : append-only, rien n'est supprimé.
    audit_a_after = await _audit_actions_for_tenant(app_engine, TENANT_A)
    assert "tenant.erase" in audit_a_after
    # Toutes les lignes d'audit antérieures de A subsistent (aucune suppression).
    for action in audit_a_before:
        assert audit_a_after.count(action) >= audit_a_before.count(action)
    assert len(audit_a_after) == len(audit_a_before) + 1

    # Isolation (exigence 2.4) : le tenant B est totalement intact.
    assert await _tenant_exists(app_engine, TENANT_B)
    b_patients_after = await _rows_for_tenant(app_engine, TENANT_B, "patient")
    assert len(b_patients_after) == len(b_patients_before) == 1
    assert len(await _rows_for_tenant(app_engine, TENANT_B, "visit")) == 1
    assert len(await _rows_for_tenant(app_engine, TENANT_B, "app_user")) == 1
    # L'audit de B n'a pas bougé (l'effacement de A ne l'affecte pas).
    assert await _audit_actions_for_tenant(app_engine, TENANT_B) == audit_b_before


# --- Append-only : aucune ligne d'audit supprimable par le rôle applicatif ------


async def test_audit_log_non_supprimable_par_le_role_applicatif(
    app_engine: object,
) -> None:
    """Le rôle `dme_app` ne peut supprimer aucune ligne d'`audit_log` (4.4, NFR-3).

    Prouve l'intégrité append-only de bout en bout : une tentative de `DELETE`
    sur `audit_log` via le rôle applicatif (NOBYPASSRLS, GRANT sans DELETE) est
    bloquée — le trigger `audit_log_block_mutation` lève une exception. On vérifie
    qu'aucune ligne n'est retirée et que l'erreur est bien d'ordre « privilège /
    append-only ».
    """
    count_before = await _count_audit(app_engine)
    assert count_before > 0, "Des lignes d'audit doivent exister (export/effacement)"

    with pytest.raises((DBAPIError, ProgrammingError)) as exc_info:
        async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
            await conn.execute(text("DELETE FROM audit_log"))
    message = str(exc_info.value).lower()
    assert "append-only" in message or "insufficient" in message or "permission" in message

    # Aucune ligne n'a disparu : l'append-only tient.
    assert await _count_audit(app_engine) == count_before


async def _count_audit(app_engine: object) -> int:
    """Nombre total de lignes d'audit visibles par le rôle applicatif."""
    async with app_engine.connect() as conn:  # type: ignore[attr-defined]
        return (
            await conn.execute(text("SELECT count(*) FROM audit_log"))
        ).scalar_one()
