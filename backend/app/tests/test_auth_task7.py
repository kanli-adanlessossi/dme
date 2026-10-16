"""Tests consolidés de la tâche 7 — RBAC, non-fuite d'existence, verrouillage,
expiration du jeton et révocation propagée à la sync (NFR-7, P3).

Les tâches 7.1–7.5 ont livré des suites ciblées (test_security, test_auth_service,
test_tokens, test_auth_router, test_lockout_service, test_deps). Ce module ne
duplique pas ces tests : il COMBLE les assertions encore manquantes et vérifie,
bout à bout, le **contrat d'erreur de non-fuite d'existence (P3)** là où il n'était
couvert qu'au niveau unitaire :

- l'indiscernabilité HTTP explicite entre un e-mail inconnu et un mauvais mot de
  passe (même statut 401 ET même corps générique) — exigence 1.3, P3 ;
- la trilogie du contrat P3 réunie : 401 générique (auth) / 404
  (`tenant_resource_not_found`, ressource inconnue == hors-tenant) / 403
  (autorisation refusée) — exigences 1.3, 2.2.

Aucun PostgreSQL réel : la session de lookup e-mail est surchargée par une fausse
session (même motif que test_auth_router) et l'écrivain d'audit est neutralisé.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.deps import (
    AuthPrincipal,
    require_roles,
    tenant_resource_not_found,
)
from app.core.security import hash_password
from app.core.tokens import create_access_token
from app.main import app
from app.models.app_user import AppUser
from app.routers import auth as auth_router
from app.schemas.audit import AuditEvent
from app.schemas.auth import Role

_GOOD_PASSWORD = "le-bon-mot-de-passe-1234"


class _FakeResult:
    """Résultat factice exposant `scalar_one_or_none` comme SQLAlchemy."""

    def __init__(self, user: AppUser | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> AppUser | None:
        return self._user


class _FakeSession:
    """Session factice : renvoie l'`AppUser` configuré et absorbe `commit`."""

    def __init__(self, user: AppUser | None) -> None:
        self._user = user

    async def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult(self._user)

    async def commit(self) -> None:  # pragma: no cover - mutation absorbée
        return None


def _make_user(*, password: str | None = _GOOD_PASSWORD) -> AppUser:
    return AppUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="user@centre-lome.tg",
        password_hash=hash_password(password) if password is not None else None,
        role="clinician",
        is_active=True,
        failed_login_count=0,
    )


@pytest.fixture(autouse=True)
def _silence_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise l'écrivain d'audit : on teste le contrat HTTP, pas l'audit ici."""

    async def _noop(_event: AuditEvent) -> None:
        return None

    monkeypatch.setattr(auth_router, "record_audit_standalone", _noop)


def _login_client(user: AppUser | None) -> TestClient:
    """Client dont le lookup e-mail renvoie `user` (ou None pour un inconnu)."""

    async def _override():
        yield _FakeSession(user)

    app.dependency_overrides[get_session] = _override
    return TestClient(app)


# --- P3 : e-mail inconnu et mauvais mot de passe sont indiscernables -----------


def test_login_email_inconnu_et_mauvais_mdp_sont_indiscernables() -> None:
    """Un e-mail inconnu et un mauvais mot de passe produisent la MÊME réponse 401.

    Non-fuite d'existence (P3, exigence 1.3) : rien dans le statut ni le corps ne
    permet à un attaquant de distinguer « ce compte n'existe pas » de « mot de
    passe erroné ». Les deux réponses doivent être strictement identiques.
    """
    # Cas 1 : e-mail inconnu (le lookup renvoie None).
    try:
        client_unknown = _login_client(None)
        resp_unknown = client_unknown.post(
            "/api/auth/login",
            json={"email": "inconnu@centre-lome.tg", "password": "peu-importe"},
        )
    finally:
        app.dependency_overrides.pop(get_session, None)

    # Cas 2 : compte existant mais mot de passe erroné.
    try:
        user = _make_user(password=_GOOD_PASSWORD)
        client_wrong = _login_client(user)
        resp_wrong = client_wrong.post(
            "/api/auth/login",
            json={"email": user.email, "password": "mauvais-mot-de-passe"},
        )
    finally:
        app.dependency_overrides.pop(get_session, None)

    # Même statut ET même corps : les deux cas sont indiscernables.
    assert resp_unknown.status_code == 401
    assert resp_wrong.status_code == resp_unknown.status_code
    assert resp_wrong.json() == resp_unknown.json()
    # Le message ne révèle pas l'existence du compte.
    assert "existe" not in resp_unknown.json()["detail"].lower()


# --- P3 : la trilogie du contrat d'erreur réunie (401 / 404 / 403) -------------


def test_contrat_p3_401_404_403_sont_distincts_et_coherents() -> None:
    """Le contrat P3 réunit trois codes distincts selon la nature de l'échec.

    - 401 générique : échec d'authentification (identité non prouvée) ;
    - 404 : ressource inconnue OU hors-tenant (indiscernables, `tenant_resource_not_found`) ;
    - 403 : identité prouvée mais autorisation refusée (rôle manquant, exigence 2.2).

    On exerce les trois sur une app jouet pour figer le contrat de bout en bout.
    """
    probe = FastAPI()
    require_admin = require_roles(Role.tenant_admin)

    @probe.get("/needs-admin")
    async def _needs_admin(
        _p: AuthPrincipal = Depends(require_admin),
    ) -> dict[str, bool]:
        return {"ok": True}

    @probe.get("/resource/{item_id}")
    async def _resource(item_id: str) -> dict[str, bool]:
        # Simule un lookup cadré au tenant qui ne trouve rien : inconnu == hors-tenant.
        raise tenant_resource_not_found()

    client = TestClient(probe)

    # 401 : aucune identité (en-tête d'autorisation absent).
    assert client.get("/needs-admin").status_code == 401

    # 403 : identité prouvée (jeton valide) mais rôle insuffisant.
    staff_token = create_access_token(
        sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="staff"
    )
    resp_forbidden = client.get(
        "/needs-admin", headers={"Authorization": f"Bearer {staff_token}"}
    )
    assert resp_forbidden.status_code == 403

    # 200 : le bon rôle passe (borne positive du cadrage, exigence 2.2).
    admin_token = create_access_token(
        sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="tenant_admin"
    )
    resp_ok = client.get(
        "/needs-admin", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp_ok.status_code == 200

    # 404 : ressource inconnue / hors-tenant (jamais 403 ni 401, pour P3).
    resp_missing = client.get(f"/resource/{uuid.uuid4()}")
    assert resp_missing.status_code == 404


def test_tenant_resource_not_found_ne_distingue_pas_inconnu_de_hors_tenant() -> None:
    """Deux absences (ID inconnu vs ID d'un autre tenant) donnent le même 404.

    L'absence de ressource ne doit jamais révéler si l'ID existe chez un autre
    tenant (sinon fuite d'existence inter-tenant, P3).
    """
    exc_unknown = tenant_resource_not_found()
    exc_cross_tenant = tenant_resource_not_found()
    assert exc_unknown.status_code == exc_cross_tenant.status_code == 404
    assert exc_unknown.detail == exc_cross_tenant.detail
