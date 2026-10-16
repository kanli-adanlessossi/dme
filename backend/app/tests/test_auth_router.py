"""Tests du routeur d'authentification login/refresh (tâches 7.2 et 7.3).

Vérifient le comportement HTTP sans toucher un vrai PostgreSQL : la dépendance de
session est surchargée par une fausse session qui renvoie (ou non) un `AppUser`
pour le lookup e-mail et absorbe `commit()`. Le mot de passe est haché pour de
vrai, de sorte que `verify_credentials` (tâche 7.1) et le verrouillage (tâche 7.3)
soient exercés sur le vrai chemin ; seul l'écrivain d'audit est remplacé pour
capturer les événements.

Couvre :
- login succès → 200, émet access + refresh portant `tenant_id`/`role`, audite
  `login.success` et réinitialise le compteur d'échecs ;
- login échec → 401 générique, audite `login.failure`, incrémente le compteur ;
- seuil d'échecs atteint → verrou posé + audit `login.lockout` ;
- compte verrouillé → 429 (même avec le bon mot de passe) + `Retry-After` ;
- expiration du verrou → login de nouveau possible ;
- refresh avec un refresh token valide → 200 + nouvel access vérifiable ;
- refresh avec un access token (mauvais type) → 401 ;
- refresh avec un jeton bidon → 401.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import get_session
from app.core.security import (
    generate_setup_token,
    hash_password,
    hash_setup_token,
    verify_password,
)
from app.core.tokens import (
    TOKEN_TYPE_ACCESS,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.main import app
from app.models.app_user import AppUser
from app.models.tenant import Tenant
from app.routers import auth as auth_router
from app.schemas.audit import AuditEvent

_GOOD_PASSWORD = "le-bon-mot-de-passe-1234"


class _FakeResult:
    """Résultat factice exposant `scalar_one_or_none` comme SQLAlchemy."""

    def __init__(self, user: AppUser | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> AppUser | None:
        return self._user


class _FakeSession:
    """Session factice : renvoie l'`AppUser` configuré et compte les commits.

    `execute` sert au lookup e-mail du routeur ; `commit` absorbe les validations
    de l'état de verrouillage (incrément/réinit) sans base réelle. `get` sert au
    contrôle de statut du tenant (exigence 3.4) : par défaut on renvoie un tenant
    **actif** afin que les tests de login existants ne soient pas affectés ; un
    test de désactivation installe un tenant `inactive` explicite.
    """

    def __init__(
        self, user: AppUser | None, tenant: Tenant | None = None
    ) -> None:
        self._user = user
        self._tenant = tenant
        self.commits = 0

    async def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult(self._user)

    async def get(self, _model: object, pk: object) -> Tenant | None:
        # Si un tenant explicite est fourni, on le renvoie ; sinon on fabrique un
        # tenant actif par défaut (statut `active`) pour l'id demandé.
        if self._tenant is not None:
            return self._tenant
        return Tenant(id=pk, name="Centre", region_id=uuid.uuid4(), status="active")

    async def commit(self) -> None:
        self.commits += 1


_current_user: AppUser | None = None
_current_tenant: Tenant | None = None
_current_session: _FakeSession | None = None


async def _override_get_session():
    global _current_session
    _current_session = _FakeSession(_current_user, _current_tenant)
    yield _current_session


@pytest.fixture(autouse=True)
def _reset_user() -> None:
    """Réinitialise l'utilisateur/la session partagés entre chaque test."""
    global _current_user, _current_tenant, _current_session
    _current_user = None
    _current_tenant = None
    _current_session = None


@pytest.fixture
def client() -> TestClient:
    """Client de test avec la dépendance de session factice."""
    app.dependency_overrides[get_session] = _override_get_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def captured_audit(monkeypatch: pytest.MonkeyPatch) -> list[AuditEvent]:
    """Capture les événements d'audit émis par le routeur (sans base)."""
    events: list[AuditEvent] = []

    async def _fake_record(event: AuditEvent) -> None:
        events.append(event)

    monkeypatch.setattr(auth_router, "record_audit_standalone", _fake_record)
    return events


def _install_user(user: AppUser) -> None:
    """Déclare l'`AppUser` que le lookup e-mail du routeur renverra."""
    global _current_user
    _current_user = user


def _install_tenant(tenant: Tenant) -> None:
    """Déclare le `Tenant` que `session.get(Tenant, …)` renverra (statut)."""
    global _current_tenant
    _current_tenant = tenant


def _make_user(
    *,
    tenant_id: uuid.UUID | None = None,
    role: str = "clinician",
    password: str | None = _GOOD_PASSWORD,
    is_active: bool = True,
    failed_login_count: int = 0,
    locked_until: datetime | None = None,
) -> AppUser:
    return AppUser(
        id=uuid.uuid4(),
        tenant_id=tenant_id if tenant_id is not None else uuid.uuid4(),
        email="user@centre-lome.tg",
        password_hash=hash_password(password) if password is not None else None,
        role=role,
        is_active=is_active,
        failed_login_count=failed_login_count,
        locked_until=locked_until,
    )


def test_login_succes_emet_jetons_et_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Login valide → 200, access/refresh portant tenant_id+role, audit succès."""
    user = _make_user(role="clinician")
    _install_user(user)

    resp = client.post(
        "/api/auth/login",
        json={"email": "user@centre-lome.tg", "password": _GOOD_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == "clinician"
    assert body["tenant_id"] == str(user.tenant_id)
    assert body["expires_in"] > 0

    # Les claims du JWT émis portent sub/tenant_id/role (exigence 1.1, P2).
    claims = decode_token(body["access"], expected_type=TOKEN_TYPE_ACCESS)
    assert claims.sub == user.id
    assert claims.tenant_id == user.tenant_id
    assert claims.role == "clinician"

    # Un événement d'audit `login.success` est écrit (NFR-3).
    assert len(captured_audit) == 1
    event = captured_audit[0]
    assert event.action == "login.success"
    assert event.actor_user_id == user.id
    assert event.tenant_id == user.tenant_id


def test_login_platform_admin_tenant_null(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Un platform_admin reçoit un jeton à `tenant_id` nul (transversal)."""
    user = AppUser(
        id=uuid.uuid4(),
        tenant_id=None,
        email="admin@plateforme.tg",
        password_hash=hash_password(_GOOD_PASSWORD),
        role="platform_admin",
        is_active=True,
        failed_login_count=0,
    )
    _install_user(user)

    resp = client.post(
        "/api/auth/login",
        json={"email": "admin@plateforme.tg", "password": _GOOD_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] is None
    assert body["role"] == "platform_admin"


def test_login_echec_401_generique_et_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Login invalide (e-mail inconnu) → 401 générique + audit `login.failure`."""
    # Aucun utilisateur installé : le lookup renvoie None (e-mail inconnu).
    resp = client.post(
        "/api/auth/login",
        json={"email": "inconnu@centre-lome.tg", "password": "mauvais"},
    )
    assert resp.status_code == 401
    # Message générique : ne révèle pas si l'e-mail existe.
    assert "existe" not in resp.json()["detail"].lower()

    assert len(captured_audit) == 1
    assert captured_audit[0].action == "login.failure"
    assert captured_audit[0].actor_user_id is None


def test_login_echec_sur_compte_existant_incremente_le_compteur(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Un échec sur un compte existant incrémente `failed_login_count` + commit."""
    user = _make_user(failed_login_count=0)
    _install_user(user)

    resp = client.post(
        "/api/auth/login",
        json={"email": "user@centre-lome.tg", "password": "mauvais"},
    )
    assert resp.status_code == 401
    assert user.failed_login_count == 1
    # La mutation du compteur est validée en base (commit sur la session).
    assert _current_session is not None and _current_session.commits == 1
    # Audit d'échec portant l'acteur/tenant du compte existant.
    assert any(e.action == "login.failure" for e in captured_audit)
    assert all(e.action != "login.lockout" for e in captured_audit)


def test_login_seuil_atteint_pose_le_verrou_et_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Au dernier échec tolérable, `locked_until` est posé + audit `login.lockout`."""
    settings = get_settings()
    # Compte à un échec du seuil : le prochain échec déclenche le verrou.
    user = _make_user(failed_login_count=settings.login_max_failed_attempts - 1)
    _install_user(user)

    resp = client.post(
        "/api/auth/login",
        json={"email": "user@centre-lome.tg", "password": "mauvais"},
    )
    assert resp.status_code == 401
    assert user.failed_login_count == settings.login_max_failed_attempts
    assert user.locked_until is not None
    assert user.locked_until > datetime.now(UTC)
    # L'atteinte du seuil est journalisée (en plus de l'échec).
    actions = [e.action for e in captured_audit]
    assert "login.lockout" in actions
    assert "login.failure" in actions


def test_login_compte_verrouille_renvoie_429_meme_avec_bon_mdp(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Un compte verrouillé → 429 même avec le bon mot de passe (NFR-2)."""
    settings = get_settings()
    user = _make_user(
        failed_login_count=settings.login_max_failed_attempts,
        locked_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    _install_user(user)

    resp = client.post(
        "/api/auth/login",
        json={"email": "user@centre-lome.tg", "password": _GOOD_PASSWORD},
    )
    assert resp.status_code == 429
    # En-tête indiquant combien de temps attendre avant de réessayer.
    retry_after = int(resp.headers["retry-after"])
    assert retry_after > 0
    # Le verrou est audité ; aucun jeton n'est émis.
    assert any(e.action == "login.lockout" for e in captured_audit)
    assert "access" not in resp.json()


def test_login_apres_expiration_du_verrou_est_de_nouveau_possible(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Un verrou expiré (`locked_until` passé) n'empêche plus la connexion."""
    settings = get_settings()
    user = _make_user(
        failed_login_count=settings.login_max_failed_attempts,
        # Verrou dans le passé : il a expiré.
        locked_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    _install_user(user)

    resp = client.post(
        "/api/auth/login",
        json={"email": "user@centre-lome.tg", "password": _GOOD_PASSWORD},
    )
    assert resp.status_code == 200
    # Succès : le compteur et le verrou sont réinitialisés.
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert any(e.action == "login.success" for e in captured_audit)


def test_login_refuse_membre_d_un_tenant_desactive(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Un membre d'un tenant désactivé → 401 générique, même avec le bon mdp (3.4)."""
    tenant_id = uuid.uuid4()
    user = _make_user(tenant_id=tenant_id, role="clinician")
    _install_user(user)
    # Le tenant du membre est au statut `inactive` (désactivé par le platform_admin).
    _install_tenant(
        Tenant(id=tenant_id, name="Centre", region_id=uuid.uuid4(), status="inactive")
    )

    resp = client.post(
        "/api/auth/login",
        json={"email": "user@centre-lome.tg", "password": _GOOD_PASSWORD},
    )
    # Refus de connexion : 401 générique, aucun jeton émis.
    assert resp.status_code == 401
    assert "access" not in resp.json()
    assert "tenant" not in resp.json()["detail"].lower()
    # L'échec est audité (NFR-3).
    assert any(e.action == "login.failure" for e in captured_audit)


def test_login_succes_reinitialise_le_compteur_d_echecs(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Un login réussi remet `failed_login_count` à 0 et efface `locked_until`."""
    user = _make_user(failed_login_count=3, locked_until=None)
    _install_user(user)

    resp = client.post(
        "/api/auth/login",
        json={"email": "user@centre-lome.tg", "password": _GOOD_PASSWORD},
    )
    assert resp.status_code == 200
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert _current_session is not None and _current_session.commits == 1


def test_refresh_valide_emet_nouvel_access(client: TestClient) -> None:
    """Un refresh valide → 200 et un access vérifiable portant les mêmes claims."""
    sub = uuid.uuid4()
    tenant_id = uuid.uuid4()
    refresh_token = create_refresh_token(
        sub=sub, tenant_id=tenant_id, role="tenant_admin"
    )

    resp = client.post("/api/auth/refresh", json={"refresh": refresh_token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "tenant_admin"
    assert body["tenant_id"] == str(tenant_id)

    claims = decode_token(body["access"], expected_type=TOKEN_TYPE_ACCESS)
    assert claims.sub == sub
    assert claims.tenant_id == tenant_id
    assert claims.role == "tenant_admin"


def test_refresh_rejette_un_access_token(client: TestClient) -> None:
    """Présenter un access comme refresh → 401 (séparation des types)."""
    access = create_access_token(
        sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="clinician"
    )
    resp = client.post("/api/auth/refresh", json={"refresh": access})
    assert resp.status_code == 401


def test_refresh_rejette_un_jeton_bidon(client: TestClient) -> None:
    """Un refresh mal formé → 401."""
    resp = client.post("/api/auth/refresh", json={"refresh": "pas-un-jwt"})
    assert resp.status_code == 401


# --- set-credentials : jeton de configuration à usage unique (tâche 7.4) --------


class _FakeScalars:
    """Expose `all()` comme SQLAlchemy `scalars()`."""

    def __init__(self, users: list[AppUser]) -> None:
        self._users = users

    def all(self) -> list[AppUser]:
        return self._users


class _FakeSetupResult:
    """Résultat factice d'un `select(AppUser)` multi-lignes (candidats au jeton)."""

    def __init__(self, users: list[AppUser]) -> None:
        self._users = users

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._users)


class _FakeSetupSession:
    """Session factice pour set-credentials : renvoie les candidats et compte les commits."""

    def __init__(self, users: list[AppUser]) -> None:
        self._users = users
        self.commits = 0

    async def execute(self, _statement: object) -> _FakeSetupResult:
        return _FakeSetupResult(self._users)

    async def commit(self) -> None:
        self.commits += 1


_setup_session: _FakeSetupSession | None = None


def _make_setup_user(
    *,
    setup_token: str | None,
    expires_at: datetime | None,
    is_active: bool = True,
) -> AppUser:
    return AppUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="admin@centre-lome.tg",
        password_hash=None,
        setup_token_hash=hash_setup_token(setup_token) if setup_token else None,
        setup_token_expires_at=expires_at,
        role="tenant_admin",
        is_active=is_active,
        failed_login_count=0,
    )


@pytest.fixture
def setup_client(
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Client dont la session renvoie les candidats au jeton de configuration."""

    async def _override():
        yield _setup_session

    app.dependency_overrides[get_session] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _install_setup_users(users: list[AppUser]) -> _FakeSetupSession:
    global _setup_session
    _setup_session = _FakeSetupSession(users)
    return _setup_session


def test_set_credentials_valide_fixe_mdp_consomme_jeton_et_audite(
    setup_client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Jeton valide → 200, fixe le mot de passe, consomme le jeton, audite l'événement."""
    token = generate_setup_token()
    user = _make_setup_user(
        setup_token=token,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = _install_setup_users([user])

    resp = setup_client.post(
        "/api/auth/set-credentials",
        json={"setup_token": token, "password": "un-mot-de-passe-robuste-1234"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "admin@centre-lome.tg"
    assert body["status"] == "credentials_set"

    # Le mot de passe est fixé et vérifiable, le jeton est consommé (usage unique).
    assert user.password_hash is not None
    assert verify_password("un-mot-de-passe-robuste-1234", user.password_hash) is True
    assert user.setup_token_hash is None
    assert user.setup_token_expires_at is None
    # La mutation est validée en base.
    assert session.commits == 1
    # L'événement de configuration des identifiants est audité (NFR-3).
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "credentials.set"
    assert captured_audit[0].actor_user_id == user.id
    assert captured_audit[0].tenant_id == user.tenant_id


def test_set_credentials_jeton_reutilise_renvoie_410(
    setup_client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Un jeton déjà consommé (aucun candidat) → 410 générique, aucun audit."""
    token = generate_setup_token()
    # Jeton déjà consommé : plus aucun compte n'a de `setup_token_hash` non nul.
    session = _install_setup_users([])

    resp = setup_client.post(
        "/api/auth/set-credentials",
        json={"setup_token": token, "password": "un-mot-de-passe-robuste-1234"},
    )
    assert resp.status_code == 410
    # Message générique : ne révèle pas l'état du compte/jeton.
    assert "existe" not in resp.json()["detail"].lower()
    assert session.commits == 0
    assert captured_audit == []


def test_set_credentials_jeton_expire_renvoie_410(
    setup_client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Un jeton expiré → 410, mot de passe inchangé, aucun audit."""
    token = generate_setup_token()
    user = _make_setup_user(
        setup_token=token,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session = _install_setup_users([user])

    resp = setup_client.post(
        "/api/auth/set-credentials",
        json={"setup_token": token, "password": "un-mot-de-passe-robuste-1234"},
    )
    assert resp.status_code == 410
    assert user.password_hash is None
    assert session.commits == 0
    assert captured_audit == []


def test_set_credentials_jeton_invalide_renvoie_410(
    setup_client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Un jeton ne correspondant à aucun compte → 410, aucun audit."""
    user = _make_setup_user(
        setup_token=generate_setup_token(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = _install_setup_users([user])

    resp = setup_client.post(
        "/api/auth/set-credentials",
        json={"setup_token": "jeton-invalide", "password": "un-mot-de-passe-robuste-1234"},
    )
    assert resp.status_code == 410
    assert user.password_hash is None
    assert session.commits == 0
    assert captured_audit == []
