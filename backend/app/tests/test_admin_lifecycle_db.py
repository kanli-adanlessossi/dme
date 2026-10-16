"""Tests d'intégration du cycle de vie des tenants (tâche 8.3) — exigence 3, NFR-4.

Contrairement à `test_admin_router.py` (comportement HTTP sur session factice),
ce module exerce le **vrai moteur PostgreSQL avec RLS forcée** via le rôle
applicatif non-superuser `dme_app`. C'est la seule façon de prouver que :

- **Chemin admin cadré (P2).** L'intégration d'un tenant écrit dans des tables
  sous RLS forcée (`medical_center`, `app_user`) alors que l'appelant est un
  `platform_admin` (tenant NUL) : le service pose `app.tenant_id` sur le tenant
  cible, et le `WITH CHECK` accepte les lignes (aucune injection inter-tenant).
- **Conservation des données à la désactivation (NFR-4, exigence 3.4).** La
  désactivation d'un tenant passe son `status` à `inactive` SANS supprimer la
  moindre ligne (tenant, centre, comptes toujours présents).
- **Surface publique « actifs seulement » (P8).** Une projection filtrant les
  tenants actifs n'inclut PAS le tenant désactivé (masquage du public/annuaire).
- **Refus de connexion des membres (exigence 3.4).** La désactivation bascule
  `app_user.is_active` à False pour les membres du tenant, condition du refus de
  login (401, tâche 7).

Dispositif (identique à `test_rls_isolation.py`) : migrations réelles en
sous-processus, octroi LOGIN éphémère à `dme_app`, moteur `dme_app` injecté dans
l'app via `app.dependency_overrides[get_session]`, principal `platform_admin`
injecté. Désactivé si `TEST_DATABASE_URL` est absente.
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import get_session
from app.core.deps import AuthPrincipal, get_current_principal
from app.main import app
from app.models.app_user import AppUser
from app.services.auth import is_tenant_active

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : tests d'intégration du cycle de vie "
        "des tenants désactivés (nécessitent un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "admin_lifecycle_test_" + uuid.uuid4().hex

# Région de test et identité du platform_admin (acteur des opérations). Le code
# DOIT être l'un des 5 codes canoniques acceptés par le schéma `RegionCode`
# (sinon la requête est rejetée en 422 avant d'atteindre le service). On amorce
# donc une région MARITIME dédiée au test.
_REGION_ID = uuid.uuid4()
_REGION_CODE = "MARITIME"
_PLATFORM_ADMIN_ID = uuid.uuid4()

# Coordonnées valides dans la boîte englobante du Togo (Lomé).
_LOME_LON, _LOME_LAT = 1.2255, 6.1319


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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def app_engine() -> AsyncIterator[object]:
    """Prépare la base, octroie LOGIN à dme_app, amorce une région, expose le moteur."""
    assert OWNER_URL is not None
    _run_migrations(OWNER_URL)

    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    pw_literal = "'" + _APP_TEST_PASSWORD.replace("'", "''") + "'"
    async with owner_engine.begin() as conn:
        await conn.execute(text(f"ALTER ROLE {_APP_ROLE} LOGIN PASSWORD {pw_literal}"))
        # Région de référence (non cadrée RLS) pour les FK tenant/center.
        await conn.execute(
            text("INSERT INTO region (id, code, name_fr) VALUES (:id, :code, :name)"),
            {"id": _REGION_ID, "code": _REGION_CODE, "name": "Région test"},
        )

    app_url = _app_role_url(OWNER_URL, _APP_TEST_PASSWORD)
    engine = create_async_engine(app_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()
        # Nettoyage : retirer données amorcées + créées, puis révoquer le LOGIN.
        async with owner_engine.begin() as conn:
            # app_user / medical_center sont sous RLS : poser le contexte pour les
            # purger par tenant. On récupère d'abord les tenants de la région test.
            tenant_rows = (
                await conn.execute(
                    text("SELECT id FROM tenant WHERE region_id = :rid"),
                    {"rid": _REGION_ID},
                )
            ).all()
            for (tid,) in tenant_rows:
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, false)"),
                    {"tid": str(tid)},
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
                text("DELETE FROM tenant WHERE region_id = :rid"), {"rid": _REGION_ID}
            )
            await conn.execute(
                text("DELETE FROM region WHERE id = :rid"), {"rid": _REGION_ID}
            )
            await conn.execute(text(f"ALTER ROLE {_APP_ROLE} NOLOGIN PASSWORD NULL"))
        await owner_engine.dispose()


@pytest.fixture
def client(app_engine: object) -> TestClient:
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


def _create_payload(admin_email: str) -> dict:
    return {
        "name": "Centre de santé de Lomé",
        "region_code": _REGION_CODE,
        "address": "12 Rue du Commerce, Lomé",
        "location": {"longitude": _LOME_LON, "latitude": _LOME_LAT},
        "phone": "+22890000000",
        "email": "contact@centre-lome.tg",
        "admin_email": admin_email,
    }


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


async def test_onboarding_ecrit_a_travers_la_rls(
    client: TestClient, app_engine: object
) -> None:
    """L'intégration (platform_admin) écrit tenant + centre + admin malgré la RLS.

    Vérifie P2 : le service pose `app.tenant_id` sur le tenant créé, si bien que le
    `WITH CHECK` accepte le `medical_center` et l'`app_user` cadrés — alors que
    l'appelant n'a aucun tenant (platform_admin).
    """
    resp = client.post(
        "/api/admin/tenants",
        json=_create_payload("admin-onboard@centre-lome.tg"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    tenant_id = uuid.UUID(body["id"])
    assert body["setup_token"]

    # Les lignes cadrées existent bien (lues avec le contexte du tenant).
    centers = await _rows_for_tenant(app_engine, tenant_id, "medical_center")
    users = await _rows_for_tenant(app_engine, tenant_id, "app_user")
    assert len(centers) == 1
    assert len(users) == 1


async def test_desactivation_conserve_les_donnees_et_masque_du_public(
    client: TestClient, app_engine: object
) -> None:
    """Désactivation : données conservées (NFR-4), tenant masqué du public (P8)."""
    # Intégration d'un tenant à désactiver.
    resp = client.post(
        "/api/admin/tenants",
        json=_create_payload("admin-lifecycle@centre-lome.tg"),
    )
    assert resp.status_code == 201, resp.text
    tenant_id = uuid.UUID(resp.json()["id"])

    # Désactivation via l'endpoint admin.
    patch = client.patch(
        f"/api/admin/tenants/{tenant_id}", json={"status": "inactive"}
    )
    assert patch.status_code == 200
    assert patch.json()["status"] == "inactive"

    # NFR-4 : aucune donnée supprimée — tenant, centre et admin toujours présents.
    async with app_engine.connect() as conn:  # type: ignore[attr-defined]
        status = (
            await conn.execute(
                text("SELECT status FROM tenant WHERE id = :id"), {"id": tenant_id}
            )
        ).scalar_one()
        # Projection publique « actifs seulement » (P8) : le tenant inactif en est
        # exclu.
        public_count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM tenant "
                    "WHERE id = :id AND status = 'active'"
                ),
                {"id": tenant_id},
            )
        ).scalar_one()
    assert status == "inactive"
    assert public_count == 0, "Un tenant désactivé ne doit pas apparaître au public"

    # Les données cadrées sont conservées (centre + compte toujours là).
    centers = await _rows_for_tenant(app_engine, tenant_id, "medical_center")
    users = await _rows_for_tenant(app_engine, tenant_id, "app_user")
    assert len(centers) == 1, "Le centre doit être conservé (soft lifecycle)"
    assert len(users) == 1, "Le compte membre doit être conservé"


async def test_desactivation_bascule_is_active_des_membres(
    client: TestClient, app_engine: object
) -> None:
    """Désactivation : les membres passent is_active=False (refus de connexion, 3.4)."""
    resp = client.post(
        "/api/admin/tenants",
        json=_create_payload("admin-members@centre-lome.tg"),
    )
    assert resp.status_code == 201, resp.text
    tenant_id = uuid.UUID(resp.json()["id"])

    client.patch(f"/api/admin/tenants/{tenant_id}", json={"status": "inactive"})

    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        active_flags = (
            await conn.execute(
                text("SELECT is_active FROM app_user WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).scalars().all()
    assert active_flags == [False], "Les membres d'un tenant désactivé sont inactifs"

    # Réactivation : les membres redeviennent actifs.
    client.patch(f"/api/admin/tenants/{tenant_id}", json={"status": "active"})
    async with app_engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        active_flags = (
            await conn.execute(
                text("SELECT is_active FROM app_user WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).scalars().all()
    assert active_flags == [True]


# --- Refus de connexion après désactivation (exigence 3.4, « connexion refusée »)-

# Portée de cette tâche (8.3) : vérifier les DEUX mécanismes serveur que
# l'intégration/cycle de vie des tenants (8.1/8.2) met en place pour REFUSER la
# connexion des membres d'un tenant désactivé :
#   1. le drapeau `app_user.is_active` basculé à False (que `verify_credentials`,
#      tâche 7.1, refuse) ;
#   2. le contrôle de statut au niveau du tenant `is_tenant_active` (tâche 8),
#      défense en profondeur qui refuse même si le drapeau membre n'avait pas été
#      propagé.
# On vérifie ces deux leviers contre le vrai moteur après désactivation via
# l'endpoint, et leur rétablissement à la réactivation.
#
# NB — Le scénario de login HTTP complet n'est PAS exercé ici : le chemin de
# lookup d'auth (`find_user_by_email`) lit `app_user` AVANT qu'un contexte de
# tenant ne soit posé, or `app_user` est sous RLS forcée (migration 0003) et le
# rôle `dme_app` sans `app.tenant_id` n'y voit aucune ligne. Rendre ce lookup
# fonctionnel sous RLS relève de la tâche 7 (auth), pas de l'intégration des
# tenants ; c'est consigné comme constat pour la tâche 7. Les leviers de refus que
# 8.3 doit prouver sont, eux, vérifiés directement ci-dessous.


async def _member_of_tenant(session: object, tenant_id: uuid.UUID) -> AppUser:
    """Charge (contexte posé) l'unique membre `app_user` d'un tenant pour le test."""
    from sqlalchemy import select

    await session.execute(  # type: ignore[attr-defined]
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )
    result = await session.execute(  # type: ignore[attr-defined]
        select(AppUser).where(AppUser.tenant_id == tenant_id)
    )
    return result.scalars().one()


async def test_desactivation_refuse_la_connexion_des_membres(
    client: TestClient, app_engine: object
) -> None:
    """Désactivation → les deux leviers de refus de connexion sont posés (3.4).

    Après désactivation via l'endpoint admin, on vérifie contre le vrai moteur :
    - `app_user.is_active` est False pour le membre (→ `verify_credentials` refuse,
      tâche 7.1) ;
    - `is_tenant_active` renvoie False pour ce membre (→ login refusé au niveau
      tenant, défense en profondeur, tâche 8).
    La réactivation rétablit les deux (connexion de nouveau permise).
    """
    admin_email = "admin-refus-connexion@centre-lome.tg"
    created = client.post("/api/admin/tenants", json=_create_payload(admin_email))
    assert created.status_code == 201, created.text
    tenant_id = uuid.UUID(created.json()["id"])

    session_factory = async_sessionmaker(
        bind=app_engine,  # type: ignore[arg-type]
        expire_on_commit=False,
        autoflush=False,
    )

    # Tenant actif : les deux leviers autorisent la connexion.
    async with session_factory() as session, session.begin():
        member = await _member_of_tenant(session, tenant_id)
        assert member.is_active is True
        assert await is_tenant_active(session, member) is True

    # Désactivation du tenant par le platform_admin.
    patch = client.patch(
        f"/api/admin/tenants/{tenant_id}", json={"status": "inactive"}
    )
    assert patch.status_code == 200
    assert patch.json()["status"] == "inactive"

    # Les deux leviers refusent désormais la connexion du membre.
    async with session_factory() as session, session.begin():
        member = await _member_of_tenant(session, tenant_id)
        assert member.is_active is False, (
            "La désactivation doit refuser la connexion via is_active=False"
        )
        assert await is_tenant_active(session, member) is False, (
            "La désactivation doit refuser la connexion au niveau du tenant"
        )

    # Réactivation : les deux leviers autorisent de nouveau la connexion.
    client.patch(f"/api/admin/tenants/{tenant_id}", json={"status": "active"})
    async with session_factory() as session, session.begin():
        member = await _member_of_tenant(session, tenant_id)
        assert member.is_active is True
        assert await is_tenant_active(session, member) is True


# --- Audit des actions de cycle de vie (exigence 3.5, NFR-3, append-only) -------


async def _audit_rows_for_tenant(
    app_engine: object, tenant_id: uuid.UUID
) -> list[dict]:
    """Lit les lignes d'audit (append-only) rattachées à un tenant.

    `audit_log` ne porte aucune politique RLS par-tenant (migration 0004) : la
    lecture ne requiert pas de contexte. On projette les colonnes vérifiées par
    l'exigence 3.5 (acteur, tenant, action, entité, horodatages).
    """
    async with app_engine.connect() as conn:  # type: ignore[attr-defined]
        rows = (
            await conn.execute(
                text(
                    "SELECT action, actor_user_id, tenant_id, entity, entity_id, "
                    "event_at, received_at FROM audit_log "
                    "WHERE tenant_id = :tid ORDER BY id"
                ),
                {"tid": tenant_id},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def test_actions_de_cycle_de_vie_sont_auditees(
    client: TestClient, app_engine: object
) -> None:
    """Création puis désactivation d'un tenant écrivent des lignes d'audit (3.5, NFR-3).

    Vérifie que les actions transversales de cycle de vie laissent une trace
    append-only capturant l'acteur (platform_admin), le tenant visé, l'action,
    l'entité (`tenant`) et les horodatages (`event_at` client + `received_at`
    serveur). L'audit d'intégration/désactivation partage la transaction de la
    mutation (fail-closed) : il est donc lisible une fois la requête validée.
    """
    admin_email = "admin-audit@centre-lome.tg"
    created = client.post("/api/admin/tenants", json=_create_payload(admin_email))
    assert created.status_code == 201, created.text
    tenant_id = uuid.UUID(created.json()["id"])

    # L'onboarding a produit une ligne d'audit `tenant.onboard`.
    rows = await _audit_rows_for_tenant(app_engine, tenant_id)
    actions = [r["action"] for r in rows]
    assert "tenant.onboard" in actions

    onboard = next(r for r in rows if r["action"] == "tenant.onboard")
    # Acteur = le platform_admin injecté ; tenant = le tenant créé ; entité ciblée.
    assert onboard["actor_user_id"] == _PLATFORM_ADMIN_ID
    assert onboard["tenant_id"] == tenant_id
    assert onboard["entity"] == "tenant"
    assert onboard["entity_id"] == tenant_id
    # Horodatages présents (NFR-3 : event_at client + received_at serveur).
    assert onboard["event_at"] is not None
    assert onboard["received_at"] is not None

    # Désactivation : une ligne `tenant.deactivate` s'ajoute (append-only).
    patch = client.patch(
        f"/api/admin/tenants/{tenant_id}", json={"status": "inactive"}
    )
    assert patch.status_code == 200
    rows_after = await _audit_rows_for_tenant(app_engine, tenant_id)
    actions_after = [r["action"] for r in rows_after]
    assert "tenant.deactivate" in actions_after
    # Append-only : la ligne d'onboarding initiale subsiste (jamais écrasée).
    assert actions_after.count("tenant.onboard") == 1
    assert len(rows_after) == len(rows) + 1

    deact = next(r for r in rows_after if r["action"] == "tenant.deactivate")
    assert deact["actor_user_id"] == _PLATFORM_ADMIN_ID
    assert deact["tenant_id"] == tenant_id
    assert deact["entity"] == "tenant"

    # Réactivation : ligne `tenant.activate` ajoutée (toujours append-only).
    client.patch(f"/api/admin/tenants/{tenant_id}", json={"status": "active"})
    rows_final = await _audit_rows_for_tenant(app_engine, tenant_id)
    assert "tenant.activate" in [r["action"] for r in rows_final]
    assert len(rows_final) == len(rows_after) + 1
