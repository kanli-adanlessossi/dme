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
    GeneratedSetupToken,
    generate_setup_token,
    hash_password,
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


class _FakeSetupSession:
    """Session factice reproduisant le lookup selector + verifier (ADR-2).

    Le service exécute désormais `select(AppUser).where(setup_token_selector == :sel,
    setup_token_hash is not None)` et lit `scalar_one_or_none()`. Cette session émule
    ce comportement en résolvant le candidat par son selector : elle inspecte la
    clause `WHERE` de l'instruction compilée pour en extraire le selector recherché,
    puis renvoie le compte correspondant (ou `None`), sans toucher la base.
    """

    def __init__(self, users: list[AppUser]) -> None:
        self._users = users

    async def execute(self, statement: object) -> _FakeResult:
        selector = _extract_selector(statement)
        match = next(
            (
                user
                for user in self._users
                if user.setup_token_selector == selector
                and user.setup_token_hash is not None
            ),
            None,
        )
        return _FakeResult(match)


def _extract_selector(statement: object) -> str | None:
    """Extrait la valeur du selector liée dans la clause WHERE d'un `select`.

    Le service filtre sur `setup_token_selector == :selector` ; on lit la valeur
    liée de ce paramètre dans l'instruction compilée pour router la fausse session
    exactement comme le ferait la base (lookup par selector indexé).
    """
    params = statement.compile().params  # type: ignore[attr-defined]
    for key, value in params.items():
        if key.startswith("setup_token_selector"):
            return value  # type: ignore[no-any-return]
    return None


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
    generated: GeneratedSetupToken | None,
    expires_at: datetime | None,
    is_active: bool = True,
) -> AppUser:
    """Construit un compte en attente de configuration (sans mot de passe défini).

    Depuis ADR-2, on persiste le selector NON secret EN CLAIR et l'empreinte Argon2id
    du verifier (`generated.verifier_hash`) ; un `generated=None` simule un compte sans
    jeton en cours (déjà consommé).
    """
    return AppUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="admin@centre-lome.tg",
        password_hash=None,
        setup_token_selector=generated.selector if generated else None,
        setup_token_hash=generated.verifier_hash if generated else None,
        setup_token_expires_at=expires_at,
        role="tenant_admin",
        is_active=is_active,
        failed_login_count=0,
    )


async def test_consume_setup_token_fixe_le_mot_de_passe_et_consomme_le_jeton() -> None:
    """Jeton valide → fixe `password_hash` (Argon2id) et efface le jeton (usage unique)."""
    generated = generate_setup_token()
    user = _make_setup_user(
        generated=generated,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = _FakeSetupSession([user])

    result = await consume_setup_token(
        session,  # type: ignore[arg-type]
        setup_token=generated.token,
        password=_NEW_PASSWORD,
    )
    assert result is user
    # Mot de passe désormais défini et vérifiable.
    assert user.password_hash is not None
    assert verify_password(_NEW_PASSWORD, user.password_hash) is True
    # Jeton consommé : selector, empreinte et échéance effacés (usage unique).
    assert user.setup_token_selector is None
    assert user.setup_token_hash is None
    assert user.setup_token_expires_at is None


async def test_consume_setup_token_rejette_un_jeton_expire() -> None:
    """Jeton expiré (`expires_at` passé) → `SetupTokenError`, mot de passe inchangé."""
    generated = generate_setup_token()
    user = _make_setup_user(
        generated=generated,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session = _FakeSetupSession([user])

    with pytest.raises(SetupTokenError):
        await consume_setup_token(
            session,  # type: ignore[arg-type]
            setup_token=generated.token,
            password=_NEW_PASSWORD,
        )
    assert user.password_hash is None
    # Le jeton n'est PAS consommé sur un échec d'expiration.
    assert user.setup_token_hash is not None


async def test_consume_setup_token_rejette_un_jeton_inconnu() -> None:
    """Jeton dont le selector ne correspond à aucun compte → `SetupTokenError`."""
    user = _make_setup_user(
        generated=generate_setup_token(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = _FakeSetupSession([user])

    with pytest.raises(SetupTokenError):
        await consume_setup_token(
            session,  # type: ignore[arg-type]
            setup_token="selector-inconnu.verifier-quelconque",
            password=_NEW_PASSWORD,
        )
    assert user.password_hash is None


async def test_consume_setup_token_rejette_un_jeton_mal_forme() -> None:
    """Jeton sans séparateur `.` (format invalide) → `SetupTokenError` (indistinct)."""
    user = _make_setup_user(
        generated=generate_setup_token(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = _FakeSetupSession([user])

    with pytest.raises(SetupTokenError):
        await consume_setup_token(
            session,  # type: ignore[arg-type]
            setup_token="jeton-sans-separateur",
            password=_NEW_PASSWORD,
        )
    assert user.password_hash is None


async def test_consume_setup_token_rejette_un_jeton_deja_consomme() -> None:
    """Aucun compte avec jeton (déjà consommé) → `SetupTokenError`."""
    generated = generate_setup_token()
    # Simule un compte dont le jeton a déjà été consommé : il n'est plus candidat
    # (le service ne sélectionne que les comptes à `setup_token_hash` non nul).
    session = _FakeSetupSession([])

    with pytest.raises(SetupTokenError):
        await consume_setup_token(
            session,  # type: ignore[arg-type]
            setup_token=generated.token,
            password=_NEW_PASSWORD,
        )


async def test_consume_setup_token_rejette_un_compte_inactif() -> None:
    """Compte désactivé → `SetupTokenError`, jeton non consommé."""
    generated = generate_setup_token()
    user = _make_setup_user(
        generated=generated,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        is_active=False,
    )
    session = _FakeSetupSession([user])

    with pytest.raises(SetupTokenError):
        await consume_setup_token(
            session,  # type: ignore[arg-type]
            setup_token=generated.token,
            password=_NEW_PASSWORD,
        )
    assert user.password_hash is None
