"""Tests du service d'authentification `app_user` (tâche 7.1).

Vérifient la logique cœur de vérification des identifiants sans toucher la base :
une fausse session renvoie (ou non) un `AppUser` pour la requête de lookup, et on
contrôle que :
- un e-mail + mot de passe valides renvoient l'utilisateur authentifié ;
- un mot de passe erroné lève `AuthenticationError` ;
- un e-mail inconnu lève `AuthenticationError` (même erreur générique, P3) ;
- un compte sans empreinte (`password_hash=None`) lève `AuthenticationError` ;
- un compte inactif lève `AuthenticationError`.

La traduction en 401 générique, l'émission de jetons et le verrouillage relèvent
des tâches 7.2/7.3 et ne sont donc pas testés ici.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import (
    generate_setup_token,
    hash_password,
    hash_setup_token,
    verify_password,
)
from app.models.app_user import AppUser
from app.services.auth import (
    AuthenticationError,
    SetupTokenError,
    authenticate_user,
    consume_setup_token,
)


class _FakeResult:
    """Résultat factice exposant `scalar_one_or_none` comme SQLAlchemy."""

    def __init__(self, user: AppUser | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> AppUser | None:
        return self._user


class _FakeSession:
    """Session factice : renvoie l'`AppUser` préconfiguré pour tout `execute`."""

    def __init__(self, user: AppUser | None) -> None:
        self._user = user

    async def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult(self._user)


class _FakeScalars:
    """Expose `all()` sur une liste d'`AppUser` comme SQLAlchemy `scalars()`."""

    def __init__(self, users: list[AppUser]) -> None:
        self._users = users

    def all(self) -> list[AppUser]:
        return self._users


class _FakeSetupResult:
    """Résultat factice d'une requête `select(AppUser)` renvoyant plusieurs lignes."""

    def __init__(self, users: list[AppUser]) -> None:
        self._users = users

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._users)


class _FakeSetupSession:
    """Session factice renvoyant une liste de comptes candidats au jeton.

    Reproduit `select(AppUser).where(setup_token_hash is not None)` du service :
    `execute` renvoie les comptes fournis sans toucher la base.
    """

    def __init__(self, users: list[AppUser]) -> None:
        self._users = users

    async def execute(self, _statement: object) -> _FakeSetupResult:
        return _FakeSetupResult(self._users)


def _make_user(
    *,
    password: str | None = "mot-de-passe-valide-1234",
    is_active: bool = True,
    role: str = "clinician",
) -> AppUser:
    """Construit un `AppUser` en mémoire (sans persistance) pour les tests."""
    return AppUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="user@centre-lome.tg",
        password_hash=hash_password(password) if password is not None else None,
        role=role,
        is_active=is_active,
        failed_login_count=0,
    )


async def test_authenticate_user_renvoie_utilisateur_si_identifiants_valides() -> None:
    """Identifiants valides → renvoie l'`AppUser` authentifié."""
    user = _make_user(password="le-bon-mot-de-passe")
    session = _FakeSession(user)
    result = await authenticate_user(
        session,  # type: ignore[arg-type]
        email=user.email,
        password="le-bon-mot-de-passe",
    )
    assert result is user


async def test_authenticate_user_rejette_mauvais_mot_de_passe() -> None:
    """Mot de passe erroné → `AuthenticationError`."""
    user = _make_user(password="le-bon-mot-de-passe")
    session = _FakeSession(user)
    with pytest.raises(AuthenticationError):
        await authenticate_user(
            session,  # type: ignore[arg-type]
            email=user.email,
            password="mauvais-mot-de-passe",
        )


async def test_authenticate_user_rejette_email_inconnu() -> None:
    """E-mail inconnu → `AuthenticationError` (même erreur générique, P3)."""
    session = _FakeSession(None)
    with pytest.raises(AuthenticationError):
        await authenticate_user(
            session,  # type: ignore[arg-type]
            email="inconnu@centre-lome.tg",
            password="peu-importe",
        )


async def test_authenticate_user_rejette_compte_sans_empreinte() -> None:
    """Compte sans mot de passe défini (`password_hash=None`) → erreur générique."""
    user = _make_user(password=None)
    session = _FakeSession(user)
    with pytest.raises(AuthenticationError):
        await authenticate_user(
            session,  # type: ignore[arg-type]
            email=user.email,
            password="peu-importe",
        )


async def test_authenticate_user_rejette_compte_inactif() -> None:
    """Compte désactivé → `AuthenticationError` même avec le bon mot de passe."""
    user = _make_user(password="le-bon-mot-de-passe", is_active=False)
    session = _FakeSession(user)
    with pytest.raises(AuthenticationError):
        await authenticate_user(
            session,  # type: ignore[arg-type]
            email=user.email,
            password="le-bon-mot-de-passe",
        )


# --- Flux de jeton de configuration (tâche 7.4 / REQ-3.2) -----------------------

_NEW_PASSWORD = "un-nouveau-mot-de-passe-robuste"


def _make_setup_user(
    *,
    setup_token: str | None,
    expires_at: datetime | None,
    is_active: bool = True,
) -> AppUser:
    """Construit un compte en attente de configuration (sans mot de passe défini)."""
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


async def test_consume_setup_token_fixe_le_mot_de_passe_et_consomme_le_jeton() -> None:
    """Jeton valide → fixe `password_hash` (Argon2id) et efface le jeton (usage unique)."""
    token = generate_setup_token()
    user = _make_setup_user(
        setup_token=token,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = _FakeSetupSession([user])

    result = await consume_setup_token(
        session,  # type: ignore[arg-type]
        setup_token=token,
        password=_NEW_PASSWORD,
    )
    assert result is user
    # Mot de passe désormais défini et vérifiable.
    assert user.password_hash is not None
    assert verify_password(_NEW_PASSWORD, user.password_hash) is True
    # Jeton consommé : empreinte et échéance effacées (usage unique).
    assert user.setup_token_hash is None
    assert user.setup_token_expires_at is None


async def test_consume_setup_token_rejette_un_jeton_expire() -> None:
    """Jeton expiré (`expires_at` passé) → `SetupTokenError`, mot de passe inchangé."""
    token = generate_setup_token()
    user = _make_setup_user(
        setup_token=token,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session = _FakeSetupSession([user])

    with pytest.raises(SetupTokenError):
        await consume_setup_token(
            session,  # type: ignore[arg-type]
            setup_token=token,
            password=_NEW_PASSWORD,
        )
    assert user.password_hash is None
    # Le jeton n'est PAS consommé sur un échec d'expiration.
    assert user.setup_token_hash is not None


async def test_consume_setup_token_rejette_un_jeton_inconnu() -> None:
    """Jeton ne correspondant à aucun compte → `SetupTokenError` (indistinct)."""
    user = _make_setup_user(
        setup_token=generate_setup_token(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = _FakeSetupSession([user])

    with pytest.raises(SetupTokenError):
        await consume_setup_token(
            session,  # type: ignore[arg-type]
            setup_token="jeton-qui-ne-correspond-a-rien",
            password=_NEW_PASSWORD,
        )
    assert user.password_hash is None


async def test_consume_setup_token_rejette_un_jeton_deja_consomme() -> None:
    """Aucun compte avec jeton (déjà consommé) → `SetupTokenError`."""
    token = generate_setup_token()
    # Simule un compte dont le jeton a déjà été consommé : il n'est plus candidat
    # (le service ne sélectionne que les comptes à `setup_token_hash` non nul).
    session = _FakeSetupSession([])

    with pytest.raises(SetupTokenError):
        await consume_setup_token(
            session,  # type: ignore[arg-type]
            setup_token=token,
            password=_NEW_PASSWORD,
        )


async def test_consume_setup_token_rejette_un_compte_inactif() -> None:
    """Compte désactivé → `SetupTokenError`, jeton non consommé."""
    token = generate_setup_token()
    user = _make_setup_user(
        setup_token=token,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        is_active=False,
    )
    session = _FakeSetupSession([user])

    with pytest.raises(SetupTokenError):
        await consume_setup_token(
            session,  # type: ignore[arg-type]
            setup_token=token,
            password=_NEW_PASSWORD,
        )
    assert user.password_hash is None
