"""Tests des dépendances d'auth, de cadrage par rôle et de révocation (tâche 7.5).

Couvrent, sans toucher un vrai PostgreSQL (session factice / principal injecté) :

- `get_current_principal` : un access valide donne le principal (sub/tenant/role) ;
  en-tête absent, schéma non-Bearer, jeton mal formé/expiré/mauvais type → 401 ;
- `get_current_tenant_id` : dérive le `tenant_id` du jeton ; `platform_admin`
  (tenant NUL) → 500 fail-closed (chemin RLS interdit au transversal) ;
- `require_roles` : laisse passer un rôle permis, renvoie 403 pour un rôle manquant
  (exigence 2.2), distinct du 404 inter-tenant ;
- `tenant_resource_not_found` : 404 unique couvrant inconnu ET hors-tenant (P3) ;
- `is_access_revoked` : compte inactif / tenant désactivé / compte absent → révoqué ;
- `sync/pull` : renvoie `revoked=true` pour un compte inactif ou un tenant
  désactivé, et un rôle non autorisé reçoit 403 (NFR-7, exigence 2.2).

Le branchement du joint RLS (`tenant_context.get_current_tenant_id` surchargé par
l'auth) est vérifié via `app.dependency_overrides` dans `main`.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import tenant_context
from app.core.db import get_session
from app.core.deps import (
    AuthPrincipal,
    check_access_revoked,
    get_current_principal,
    get_current_tenant_id,
    is_access_revoked,
    require_roles,
    tenant_resource_not_found,
)
from app.core.tokens import create_access_token, create_refresh_token
from app.main import app
from app.models.app_user import AppUser
from app.models.tenant import Tenant
from app.routers.sync import _SYNC_ROLES  # noqa: F401 (importé pour clarté)
from app.schemas.auth import Role

# --- Fabriques de fixtures factices --------------------------------------------


def _make_user(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str = "clinician",
    is_active: bool = True,
) -> AppUser:
    return AppUser(
        id=user_id,
        tenant_id=tenant_id,
        email="user@centre-lome.tg",
        role=role,
        is_active=is_active,
        failed_login_count=0,
    )


def _make_tenant(*, tenant_id: uuid.UUID, status: str = "active") -> Tenant:
    return Tenant(id=tenant_id, name="Centre", region_id=uuid.uuid4(), status=status)


class _FakeGetSession:
    """Session factice exposant `get(Model, pk)` comme `AsyncSession.get`.

    Expose aussi `execute(...)` en no-op : `check_access_revoked` pose désormais le
    contexte RLS (`set_config('app.tenant_id', …)`) avant de lire `app_user`
    (table cadrée au tenant), via `session.execute(...)` (tâche 10). La doublure
    absorbe cet appel sans toucher à un vrai moteur.
    """

    def __init__(self, store: dict[tuple[type, uuid.UUID], object]) -> None:
        self._store = store

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def get(self, model: type, pk: uuid.UUID) -> object | None:
        return self._store.get((model, pk))


# --- get_current_principal : extraction + 401 ----------------------------------


async def test_get_current_principal_access_valide() -> None:
    """Un access valide donne un principal portant sub/tenant_id/role."""
    sub = uuid.uuid4()
    tid = uuid.uuid4()
    token = create_access_token(sub=sub, tenant_id=tid, role="clinician")

    from fastapi.security import HTTPAuthorizationCredentials

    principal = await get_current_principal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )
    assert principal.sub == sub
    assert principal.tenant_id == tid
    assert principal.role == "clinician"
    assert principal.is_platform_admin is False


async def test_get_current_principal_platform_admin_tenant_null() -> None:
    """Un platform_admin a un `tenant_id` NUL et `is_platform_admin` vrai."""
    from fastapi.security import HTTPAuthorizationCredentials

    token = create_access_token(
        sub=uuid.uuid4(), tenant_id=None, role="platform_admin"
    )
    principal = await get_current_principal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )
    assert principal.tenant_id is None
    assert principal.is_platform_admin is True


async def test_get_current_principal_sans_credentials_401() -> None:
    """En-tête d'autorisation absent → 401 générique."""
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(None)
    assert exc.value.status_code == 401


async def test_get_current_principal_jeton_malforme_401() -> None:
    """Un jeton mal formé → 401 générique."""
    from fastapi.security import HTTPAuthorizationCredentials

    with pytest.raises(HTTPException) as exc:
        await get_current_principal(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="pas-un-jwt")
        )
    assert exc.value.status_code == 401


async def test_get_current_principal_refresh_rejete_401() -> None:
    """Un refresh présenté comme access (mauvais type) → 401."""
    from fastapi.security import HTTPAuthorizationCredentials

    refresh = create_refresh_token(
        sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="staff"
    )
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=refresh)
        )
    assert exc.value.status_code == 401


# --- get_current_tenant_id : dérivation + 500 pour platform_admin --------------


async def test_get_current_tenant_id_derive_du_principal() -> None:
    """Le `tenant_id` est dérivé du principal (jamais du corps, P2)."""
    tid = uuid.uuid4()
    principal = AuthPrincipal(sub=uuid.uuid4(), tenant_id=tid, role="tenant_admin")
    assert await get_current_tenant_id(principal) == tid


async def test_get_current_tenant_id_platform_admin_500() -> None:
    """Un platform_admin (tenant NUL) sur le chemin RLS → 500 fail-closed."""
    principal = AuthPrincipal(sub=uuid.uuid4(), tenant_id=None, role="platform_admin")
    with pytest.raises(HTTPException) as exc:
        await get_current_tenant_id(principal)
    assert exc.value.status_code == 500


# --- require_roles : 403 vs autorisé -------------------------------------------


async def test_require_roles_autorise_un_role_permis() -> None:
    """Un rôle figurant dans la liste autorisée passe et renvoie le principal."""
    dep = require_roles(Role.tenant_admin, Role.clinician)
    principal = AuthPrincipal(sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="clinician")
    assert await dep(principal) is principal


async def test_require_roles_refuse_un_role_manquant_403() -> None:
    """Un rôle hors liste → 403 (autorisation refusée, exigence 2.2)."""
    dep = require_roles(Role.tenant_admin)
    principal = AuthPrincipal(sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="staff")
    with pytest.raises(HTTPException) as exc:
        await dep(principal)
    assert exc.value.status_code == 403


async def test_require_roles_accepte_les_chaines() -> None:
    """La fabrique accepte aussi des rôles sous forme de chaîne."""
    dep = require_roles("staff")
    principal = AuthPrincipal(sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="staff")
    assert await dep(principal) is principal


# --- tenant_resource_not_found : contrat P3 ------------------------------------


def test_tenant_resource_not_found_est_404() -> None:
    """Le helper d'absence de ressource encode un 404 (inconnu == hors-tenant, P3)."""
    exc = tenant_resource_not_found()
    assert exc.status_code == 404


# --- is_access_revoked : logique pure de révocation (NFR-7) --------------------


def test_is_access_revoked_compte_actif_tenant_actif_non_revoque() -> None:
    """Compte actif + tenant actif → non révoqué."""
    tid = uuid.uuid4()
    user = _make_user(user_id=uuid.uuid4(), tenant_id=tid, is_active=True)
    tenant = _make_tenant(tenant_id=tid, status="active")
    assert is_access_revoked(user, tenant) is False


def test_is_access_revoked_compte_inactif() -> None:
    """Compte inactif → révoqué, quel que soit l'état du tenant."""
    tid = uuid.uuid4()
    user = _make_user(user_id=uuid.uuid4(), tenant_id=tid, is_active=False)
    tenant = _make_tenant(tenant_id=tid, status="active")
    assert is_access_revoked(user, tenant) is True


def test_is_access_revoked_tenant_desactive() -> None:
    """Tenant au statut `inactive` → révoqué (NFR-4/NFR-7)."""
    tid = uuid.uuid4()
    user = _make_user(user_id=uuid.uuid4(), tenant_id=tid, is_active=True)
    tenant = _make_tenant(tenant_id=tid, status="inactive")
    assert is_access_revoked(user, tenant) is True


def test_is_access_revoked_compte_absent() -> None:
    """Compte introuvable (supprimé/désactivé serveur) → révoqué."""
    assert is_access_revoked(None, None) is True


def test_is_access_revoked_platform_admin_actif() -> None:
    """Un platform_admin actif (tenant NUL) n'est pas révoqué par un tenant."""
    user = _make_user(
        user_id=uuid.uuid4(), tenant_id=None, role="platform_admin", is_active=True
    )
    assert is_access_revoked(user, None) is False


async def test_check_access_revoked_recharge_etat_serveur() -> None:
    """`check_access_revoked` recharge compte+tenant et détecte la désactivation."""
    sub = uuid.uuid4()
    tid = uuid.uuid4()
    # Jeton émis quand tout allait bien, mais le tenant est désormais désactivé.
    user = _make_user(user_id=sub, tenant_id=tid, is_active=True)
    tenant = _make_tenant(tenant_id=tid, status="inactive")
    session = _FakeGetSession({(AppUser, sub): user, (Tenant, tid): tenant})
    principal = AuthPrincipal(sub=sub, tenant_id=tid, role="clinician")

    assert await check_access_revoked(principal, session) is True  # type: ignore[arg-type]


# --- Intégration HTTP : branchement du joint RLS -------------------------------


def test_joint_rls_surcharge_par_lauth() -> None:
    """`main` surcharge le placeholder de tenant_context par la dépendance d'auth."""
    assert (
        app.dependency_overrides.get(tenant_context.get_current_tenant_id)
        is get_current_tenant_id
    )


def test_require_roles_integre_dans_une_app_403_et_200() -> None:
    """En conditions HTTP réelles : rôle permis → 200, rôle manquant → 403."""
    probe = FastAPI()
    # Garde construite une seule fois (hors valeur par défaut d'argument) : motif B008.
    require_admin = require_roles(Role.tenant_admin)

    @probe.get("/admin-only")
    async def _admin_only(
        principal: AuthPrincipal = Depends(require_admin),
    ) -> dict[str, str]:
        return {"role": principal.role}

    client = TestClient(probe)

    good = create_access_token(
        sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="tenant_admin"
    )
    resp_ok = client.get("/admin-only", headers={"Authorization": f"Bearer {good}"})
    assert resp_ok.status_code == 200
    assert resp_ok.json()["role"] == "tenant_admin"

    bad = create_access_token(
        sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="staff"
    )
    resp_forbidden = client.get(
        "/admin-only", headers={"Authorization": f"Bearer {bad}"}
    )
    assert resp_forbidden.status_code == 403

    resp_unauth = client.get("/admin-only")
    assert resp_unauth.status_code == 401


# --- Intégration HTTP : sync/pull expose l'état de révocation (NFR-7) ----------


def _principal_override(principal: AuthPrincipal):
    async def _dep() -> AuthPrincipal:
        return principal

    return _dep


def _session_override(store: dict[tuple[type, uuid.UUID], object]):
    async def _dep():
        yield _FakeGetSession(store)

    return _dep


class _EmptyPullSession:
    """Session tenant factice pour `sync/pull` : aucun delta à renvoyer.

    Le chemin **non révoqué** de `sync/pull` dépend désormais de `tenant_session`
    (tâche 10) : la logique de deltas interroge la base via `execute(select(...))`.
    Cette doublure retourne un résultat vide (aucune ligne) et absorbe l'écriture
    d'audit, afin d'exercer le contrat HTTP sans vrai moteur. Les propriétés
    dépendant du moteur (RLS P11, deltas réels) sont vérifiées dans
    `test_sync_db.py` contre un vrai PostgreSQL.
    """

    async def execute(self, *_args: object, **_kwargs: object) -> _EmptyResult:
        return _EmptyResult()


class _EmptyResult:
    """Résultat SQLAlchemy factice exposant `scalars().all() == []`."""

    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[object]:
        return []


def _tenant_session_override():
    async def _dep():
        yield _EmptyPullSession()

    return _dep


@pytest.fixture
def sync_client() -> TestClient:
    """Client isolant les surcharges auth/session pour `sync/pull`."""
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_principal, None)
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(tenant_context.tenant_session, None)


def test_sync_pull_revoque_pour_compte_inactif(sync_client: TestClient) -> None:
    """`sync/pull` → revoked=true quand le compte est inactif (NFR-7)."""
    sub = uuid.uuid4()
    tid = uuid.uuid4()
    principal = AuthPrincipal(sub=sub, tenant_id=tid, role="clinician")
    user = _make_user(user_id=sub, tenant_id=tid, is_active=False)
    tenant = _make_tenant(tenant_id=tid, status="active")

    app.dependency_overrides[get_current_principal] = _principal_override(principal)
    app.dependency_overrides[get_session] = _session_override(
        {(AppUser, sub): user, (Tenant, tid): tenant}
    )
    app.dependency_overrides[tenant_context.tenant_session] = _tenant_session_override()

    resp = sync_client.get("/api/sync/pull")
    assert resp.status_code == 200
    body = resp.json()
    assert body["revoked"] is True
    assert body["changes"] == []


def test_sync_pull_revoque_pour_tenant_desactive(sync_client: TestClient) -> None:
    """`sync/pull` → revoked=true quand le tenant est désactivé (NFR-4/NFR-7)."""
    sub = uuid.uuid4()
    tid = uuid.uuid4()
    principal = AuthPrincipal(sub=sub, tenant_id=tid, role="tenant_admin")
    user = _make_user(user_id=sub, tenant_id=tid, is_active=True)
    tenant = _make_tenant(tenant_id=tid, status="inactive")

    app.dependency_overrides[get_current_principal] = _principal_override(principal)
    app.dependency_overrides[get_session] = _session_override(
        {(AppUser, sub): user, (Tenant, tid): tenant}
    )
    app.dependency_overrides[tenant_context.tenant_session] = _tenant_session_override()

    resp = sync_client.get("/api/sync/pull")
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True


def test_sync_pull_non_revoque_renvoie_des_deltas(sync_client: TestClient) -> None:
    """Non révoqué : le chemin deltas répond 200 (tâche 10), curseur présent.

    Avec une session tenant vide (aucune ligne), le pull renvoie 200,
    `revoked=false`, aucun changement et un curseur (vide en synchro initiale sans
    delta). Les deltas réels + le cadrage RLS sont vérifiés en base dans
    `test_sync_db.py`.
    """
    sub = uuid.uuid4()
    tid = uuid.uuid4()
    principal = AuthPrincipal(sub=sub, tenant_id=tid, role="clinician")
    user = _make_user(user_id=sub, tenant_id=tid, is_active=True)
    tenant = _make_tenant(tenant_id=tid, status="active")

    app.dependency_overrides[get_current_principal] = _principal_override(principal)
    app.dependency_overrides[get_session] = _session_override(
        {(AppUser, sub): user, (Tenant, tid): tenant}
    )
    app.dependency_overrides[tenant_context.tenant_session] = _tenant_session_override()

    resp = sync_client.get("/api/sync/pull")
    assert resp.status_code == 200
    body = resp.json()
    assert body["revoked"] is False
    assert body["changes"] == []
    assert "cursor" in body


def test_sync_pull_role_non_autorise_403(sync_client: TestClient) -> None:
    """Un rôle hors `_SYNC_ROLES` (p. ex. platform_admin) → 403 (exigence 2.2)."""
    principal = AuthPrincipal(
        sub=uuid.uuid4(), tenant_id=None, role="platform_admin"
    )
    app.dependency_overrides[get_current_principal] = _principal_override(principal)

    resp = sync_client.get("/api/sync/pull")
    assert resp.status_code == 403
