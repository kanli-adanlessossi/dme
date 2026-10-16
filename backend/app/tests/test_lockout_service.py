"""Tests du service de verrouillage de compte (tâche 7.3) — NFR-2.

Vérifient la **logique d'état** du verrouillage sur un `AppUser` en mémoire (sans
base) : incrément du compteur, pose du verrou au seuil, détection du verrou actif
vs expiré, calcul du délai restant, et réinitialisation au succès. Le seuil et la
durée proviennent de la configuration ; on les surcharge via `monkeypatch` pour
rendre les tests déterministes et indépendants des valeurs par défaut.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core import config
from app.models.app_user import AppUser
from app.services import lockout
from app.services.lockout import (
    AccountLockedError,
    ensure_not_locked,
    is_locked,
    register_failure,
    register_success,
)


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch):
    """Fixe un seuil et une durée de verrou déterministes pour les tests."""
    settings = config.Settings(
        login_max_failed_attempts=3,
        login_lockout_seconds=900,
    )
    monkeypatch.setattr(lockout, "get_settings", lambda: settings)
    return settings


def _make_user(
    *, failed_login_count: int = 0, locked_until: datetime | None = None
) -> AppUser:
    return AppUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="user@centre-lome.tg",
        password_hash="x",
        role="clinician",
        is_active=True,
        failed_login_count=failed_login_count,
        locked_until=locked_until,
    )


def test_register_failure_incremente_sous_le_seuil() -> None:
    """Sous le seuil, un échec incrémente le compteur sans poser de verrou."""
    user = _make_user(failed_login_count=0)
    newly_locked = register_failure(user)
    assert user.failed_login_count == 1
    assert user.locked_until is None
    assert newly_locked is False


def test_register_failure_pose_le_verrou_au_seuil() -> None:
    """Au seuil atteint, `locked_until` est posé dans le futur et True renvoyé."""
    user = _make_user(failed_login_count=2)  # seuil = 3
    now = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    newly_locked = register_failure(user, now=now)
    assert user.failed_login_count == 3
    assert newly_locked is True
    assert user.locked_until == now + timedelta(seconds=900)


def test_is_locked_vrai_si_verrou_futur() -> None:
    """Un `locked_until` dans le futur => compte verrouillé."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    user = _make_user(locked_until=now + timedelta(minutes=5))
    assert is_locked(user, now=now) is True


def test_is_locked_faux_si_verrou_expire() -> None:
    """Un `locked_until` passé => compte non verrouillé."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    user = _make_user(locked_until=now - timedelta(seconds=1))
    assert is_locked(user, now=now) is False


def test_is_locked_faux_sans_verrou() -> None:
    """`locked_until=None` => jamais verrouillé."""
    assert is_locked(_make_user(locked_until=None)) is False


def test_is_locked_tolere_un_horodatage_naif() -> None:
    """Un `locked_until` naïf est interprété comme UTC (pas d'erreur de compare)."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    naive_future = datetime(2024, 1, 1, 12, 5)  # naïf, traité comme UTC
    user = _make_user(locked_until=naive_future)
    assert is_locked(user, now=now) is True


def test_ensure_not_locked_leve_si_verrouille() -> None:
    """`ensure_not_locked` lève `AccountLockedError` avec un délai restant >= 1."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    user = _make_user(locked_until=now + timedelta(seconds=120))
    with pytest.raises(AccountLockedError) as excinfo:
        ensure_not_locked(user, now=now)
    assert excinfo.value.retry_after_seconds >= 1
    assert excinfo.value.retry_after_seconds <= 120


def test_ensure_not_locked_ne_leve_pas_si_non_verrouille() -> None:
    """`ensure_not_locked` ne lève pas quand le compte n'est pas verrouillé."""
    ensure_not_locked(_make_user(locked_until=None))  # ne doit pas lever


def test_register_success_reinitialise_l_etat() -> None:
    """Un succès remet le compteur à 0 et efface le verrou."""
    user = _make_user(
        failed_login_count=5,
        locked_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    register_success(user)
    assert user.failed_login_count == 0
    assert user.locked_until is None
