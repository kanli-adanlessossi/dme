"""Verrouillage de compte après échecs répétés à la connexion (tâche 7.3).

Protection anti force brute (NFR-2). Ce service porte la **logique d'état** du
verrouillage, tenue dans les colonnes `app_user.failed_login_count` et
`app_user.locked_until` (voir modèle `app_user`, data model du design) :

- à chaque échec sur un compte **existant**, `failed_login_count` est incrémenté ;
- au moment où le compteur atteint le seuil (`login_max_failed_attempts`), on pose
  `locked_until = now + login_lockout_seconds` ; tant que ce verrou est dans le
  futur, toute tentative — **même avec le bon mot de passe** — doit être refusée
  en **429** (design, « Échecs d'authentification ») ;
- un succès **réinitialise** le compteur et **lève** le verrou.

Séparation des responsabilités (cohérente avec les tâches 7.1/7.2) :

- `authenticate_user` (tâche 7.1) reste **pur** : il ne lit ni ne mute jamais
  `failed_login_count` / `locked_until`. C'est ici qu'on effectue ces mutations.
- Ce service ne décide PAS du code HTTP : il lève `AccountLockedError` quand le
  compte est (ou vient d'être) verrouillé ; le routeur (tâche 7.2) traduit en
  **429**, et l'`AuthenticationError` générique de 7.1 en **401** (non-fuite
  d'existence, P3). Les mutations sont écrites dans la session fournie ; c'est
  l'appelant (le routeur) qui **valide** (commit) la transaction.

Non-fuite d'existence (P3) : le routeur n'appelle le chemin de verrouillage que
pour un compte **effectivement trouvé**. Pour un e-mail inconnu, aucun état n'est
muté (il n'y a pas de ligne à incrémenter) et la mitigation du canal temporel de
7.1 reste la seule défense — on ne distingue donc pas, par le comportement
observable, « compte inexistant » de « mauvais mot de passe sous le seuil ».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.models.app_user import AppUser


class AccountLockedError(Exception):
    """Le compte est temporairement verrouillé (trop d'échecs — NFR-2).

    Porte le nombre de secondes restantes avant expiration du verrou, afin que le
    routeur puisse renseigner un en-tête `Retry-After` sur la réponse 429. La
    cause (seuil atteint) n'est jamais détaillée au client au-delà du 429.
    """

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Compte temporairement verrouillé.")


def _now() -> datetime:
    """Instant courant *aware* (UTC) — isolé pour faciliter les tests."""
    return datetime.now(UTC)


def is_locked(user: AppUser, *, now: datetime | None = None) -> bool:
    """Indique si le compte est actuellement verrouillé (`locked_until` futur)."""
    if user.locked_until is None:
        return False
    current = now or _now()
    locked_until = user.locked_until
    # Tolère un `locked_until` stocké *naïf* (selon le backend) en le traitant
    # comme UTC, pour que la comparaison reste correcte.
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=UTC)
    return locked_until > current


def _remaining_seconds(user: AppUser, *, now: datetime | None = None) -> int:
    """Secondes restantes (arrondies au supérieur, >= 1) avant déverrouillage."""
    current = now or _now()
    locked_until = user.locked_until
    assert locked_until is not None  # appelé uniquement si verrouillé
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=UTC)
    delta = (locked_until - current).total_seconds()
    return max(1, int(delta) + (1 if delta % 1 else 0))


def ensure_not_locked(user: AppUser, *, now: datetime | None = None) -> None:
    """Lève `AccountLockedError` si le compte est verrouillé, sinon ne fait rien.

    À appeler **avant** la vérification du mot de passe : un compte verrouillé est
    refusé même si les identifiants présentés sont corrects (design, NFR-2).
    """
    current = now or _now()
    if is_locked(user, now=current):
        raise AccountLockedError(_remaining_seconds(user, now=current))


def register_failure(user: AppUser, *, now: datetime | None = None) -> bool:
    """Enregistre un échec de connexion sur un compte existant.

    Incrémente `failed_login_count` ; si le seuil configuré
    (`login_max_failed_attempts`) est atteint, pose `locked_until = now +
    login_lockout_seconds`. Mute l'objet `AppUser` fourni (la persistance est à la
    charge de l'appelant, qui valide la session). Renvoie `True` si l'appel a
    déclenché (ou prolongé) un verrou, `False` sinon.
    """
    settings = get_settings()
    current = now or _now()

    user.failed_login_count = (user.failed_login_count or 0) + 1

    if user.failed_login_count >= settings.login_max_failed_attempts:
        user.locked_until = current + timedelta(
            seconds=settings.login_lockout_seconds
        )
        return True
    return False


def register_success(user: AppUser) -> None:
    """Réinitialise l'état de verrouillage après une connexion réussie.

    Remet `failed_login_count` à 0 et efface `locked_until`. Mute l'objet fourni ;
    la persistance (commit) incombe à l'appelant.
    """
    user.failed_login_count = 0
    user.locked_until = None
