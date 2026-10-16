"""Service d'authentification `app_user` (tâche 7.1).

Logique **cœur** de vérification des identifiants : rechercher un compte par
e-mail, vérifier le mot de passe contre l'empreinte Argon2id stockée, et renvoyer
l'utilisateur authentifié — ou lever une erreur d'authentification générique.

Périmètre de la tâche 7.1 uniquement. N'implémente PAS :
- l'émission de JWT / login / refresh (tâche 7.2) ;
- la limitation de débit et le verrouillage après échecs (tâche 7.3) : ce service
  ne mute donc PAS `failed_login_count` / `locked_until` ;
- les dépendances de cadrage par rôle (tâche 7.5).

Le **flux de jeton de configuration** (tâche 7.4, `consume_setup_token`) y est en
revanche implémenté : il valide un jeton à usage unique et expirant contre
l'empreinte stockée, fixe `password_hash` (Argon2id) et consomme le jeton (il
efface `setup_token_hash` / `setup_token_expires_at`), le tout de façon
indistincte en cas d'échec (non-fuite d'existence, P3 ; la couche HTTP traduit en
400/410, exigence 3.2).

Conception (design, section « Error handling » / « Échecs d'authentification ») :

- **Erreur générique, pas de fuite d'existence (P3).** Qu'un e-mail soit inconnu,
  que le mot de passe soit faux, ou que le compte n'ait pas d'empreinte définie,
  la même `AuthenticationError` est levée. La couche HTTP (tâche 7.2) la traduira
  en **401 générique** ne révélant jamais si l'e-mail existe.
- **Mitigation du canal temporel (énumération).** Lorsqu'aucun compte n'est trouvé,
  on vérifie malgré tout le mot de passe contre une empreinte factice, afin que la
  durée d'une tentative pour un e-mail inexistant soit comparable à celle d'un
  e-mail existant (pas de timing oracle sur l'existence du compte).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    hash_password,
    verify_password,
    verify_setup_token,
)
from app.models.app_user import AppUser
from app.models.tenant import Tenant

# Empreinte factice (d'un mot de passe aléatoire inconnu) utilisée pour niveler le
# temps de réponse quand aucun compte ne correspond : on exécute une vérification
# Argon2id réelle même pour un e-mail inexistant, ce qui empêche de distinguer
# « compte inconnu » de « mauvais mot de passe » par le temps de calcul.
_DUMMY_PASSWORD_HASH: str | None = None


def _dummy_password_hash() -> str:
    """Empreinte factice mémoïsée, calculée au premier usage (params Argon2 actifs)."""
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = hash_password("timing-attack-mitigation-placeholder")
    return _DUMMY_PASSWORD_HASH


class AuthenticationError(Exception):
    """Échec d'authentification générique (identifiants invalides).

    Volontairement sans message distinctif : la cause exacte (e-mail inconnu,
    mauvais mot de passe, identifiants non configurés, compte inactif) ne doit pas
    transparaître pour l'appelant (non-fuite d'existence, P3). La traduction en
    code HTTP (401 générique) relève de la tâche 7.2.
    """


class SetupTokenError(Exception):
    """Échec générique de consommation d'un jeton de configuration (exigence 3.2).

    Volontairement sans cause distinctive : qu'un jeton soit inconnu, expiré, déjà
    consommé, ou qu'il corresponde à un compte inactif, la même erreur est levée.
    La couche HTTP (tâche 7.4) la traduit en 400/410 générique sans révéler l'état
    du compte (non-fuite d'existence, P3).
    """


async def find_user_by_email(
    session: AsyncSession, *, email: str
) -> AppUser | None:
    """Recherche un compte `app_user` par e-mail (ou `None` s'il n'existe pas).

    Lookup pur, sans effet de bord ni décision d'erreur. Le routeur l'utilise pour
    obtenir l'objet `AppUser` sur lequel appliquer le verrouillage (tâche 7.3)
    avant de vérifier les identifiants, tout en conservant `verify_credentials`
    indépendant de la façon dont le compte a été chargé.
    """
    result = await session.execute(select(AppUser).where(AppUser.email == email))
    return result.scalar_one_or_none()


async def is_tenant_active(session: AsyncSession, user: AppUser | None) -> bool:
    """Indique si le tenant du compte est actif (ou non concerné par la désactivation).

    Défense en profondeur pour l'exigence 3.4 : un tenant désactivé doit refuser la
    connexion de ses membres. La désactivation (tâche 8, `set_tenant_status`)
    bascule déjà `app_user.is_active` à False — ce que `verify_credentials` refuse.
    Cette fonction ajoute un contrôle direct au niveau du **tenant** pour que le
    refus ne dépende pas uniquement de la propagation du drapeau membre.

    Renvoie `True` si :
    - le compte est absent/transversal (`tenant_id` NUL, platform_admin) : non
      soumis à la désactivation d'un tenant ;
    - le tenant existe et son `status` vaut `active`.

    Renvoie `False` si le tenant du compte est introuvable ou au statut
    `inactive`. La couche HTTP (login) traduit ce cas en 401 générique, sans
    révéler l'état du tenant (P3).
    """
    if user is None or user.tenant_id is None:
        return True
    tenant = await session.get(Tenant, user.tenant_id)
    return tenant is not None and tenant.status == "active"


def verify_credentials(user: AppUser | None, password: str) -> AppUser:
    """Vérifie un mot de passe contre un compte **déjà chargé** (ou absent).

    Logique cœur, **pure** : ne lit ni ne mute `failed_login_count` /
    `locked_until` (le verrouillage relève de `services.lockout`, tâche 7.3).

    - `user is None` (compte inconnu) : on exécute tout de même une vérification
      Argon2id contre une empreinte factice pour niveler le temps de réponse
      (mitigation du canal temporel), puis on lève l'erreur générique.
    - mot de passe erroné, empreinte absente, ou compte inactif : même
      `AuthenticationError` indistincte (non-fuite d'existence, P3).

    Lève :
        AuthenticationError: dans tous les cas d'échec, de façon indistincte.
    """
    if user is None:
        verify_password(password, _dummy_password_hash())
        raise AuthenticationError

    if not verify_password(password, user.password_hash):
        raise AuthenticationError

    if not user.is_active:
        # Compte désactivé : même erreur générique (la révocation/désactivation est
        # appliquée sans révéler que le compte existe).
        raise AuthenticationError

    return user


async def authenticate_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
) -> AppUser:
    """Authentifie un compte `app_user` par e-mail + mot de passe.

    Recherche le compte par e-mail, vérifie le mot de passe contre l'empreinte
    Argon2id stockée, et renvoie l'`AppUser` authentifié. Reste **pur** : ne mute
    pas l'état de verrouillage (celui-ci est géré par le routeur via
    `services.lockout`, tâche 7.3).

    Lève :
        AuthenticationError: si l'e-mail est inconnu, si l'empreinte n'est pas
            définie, si le mot de passe ne correspond pas, ou si le compte est
            inactif — toujours de façon indistincte (P3).
    """
    user = await find_user_by_email(session, email=email)
    return verify_credentials(user, password)


def _find_user_by_setup_token(users: list[AppUser], setup_token: str) -> AppUser | None:
    """Retrouve, parmi des comptes candidats, celui dont l'empreinte correspond.

    Le jeton de configuration n'est pas un identifiant lisible : on ne peut pas
    l'indexer en base (seule son empreinte Argon2id, à sel aléatoire, est stockée).
    On vérifie donc le jeton présenté contre l'empreinte de chaque candidat ayant
    un `setup_token_hash` non consommé. `verify_setup_token` ne lève jamais : un
    hash absent/corrompu échoue proprement.
    """
    for user in users:
        if verify_setup_token(setup_token, user.setup_token_hash):
            return user
    return None


async def consume_setup_token(
    session: AsyncSession,
    *,
    setup_token: str,
    password: str,
) -> AppUser:
    """Établit le mot de passe d'un compte via son jeton de configuration (REQ-3.2).

    Vérifie le jeton présenté contre l'empreinte stockée (`setup_token_hash`),
    contrôle qu'il n'est ni expiré (`setup_token_expires_at` dans le futur) ni déjà
    consommé (empreinte présente), puis — en cas de succès — fixe `password_hash`
    (Argon2id) et **consomme** le jeton en effaçant `setup_token_hash` et
    `setup_token_expires_at` (usage unique). Ne valide PAS la transaction : le
    `commit` relève de l'appelant (routeur), qui journalise aussi l'événement.

    Reste **indistinct** en cas d'échec (non-fuite d'existence, P3) : jeton
    inconnu, expiré, déjà consommé, ou compte inactif lèvent tous la même
    `SetupTokenError`. La couche HTTP la traduit en 400/410 générique.

    Lève :
        SetupTokenError: dans tous les cas d'échec, de façon indistincte.
    """
    # Ne considérer que les comptes avec un jeton encore présent (non consommé).
    result = await session.execute(
        select(AppUser).where(AppUser.setup_token_hash.is_not(None))
    )
    candidates = list(result.scalars().all())

    user = _find_user_by_setup_token(candidates, setup_token)
    if user is None:
        # Jeton inconnu ou déjà consommé : échec indistinct.
        raise SetupTokenError

    # Expiration : un jeton sans échéance, ou dont l'échéance est passée, est rejeté.
    expires_at = user.setup_token_expires_at
    if expires_at is None or expires_at <= datetime.now(UTC):
        raise SetupTokenError

    if not user.is_active:
        # Compte désactivé : même erreur générique (pas de révélation d'état).
        raise SetupTokenError

    # Succès : fixe l'empreinte du mot de passe et CONSOMME le jeton (usage unique).
    user.password_hash = hash_password(password)
    user.setup_token_hash = None
    user.setup_token_expires_at = None
    return user
