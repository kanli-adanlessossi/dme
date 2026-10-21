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
revanche implémenté selon le schéma **selector + verifier** (ADR-2, observation
#8) : le jeton `"<selector>.<verifier>"` est découpé sur le premier `.` ; le
selector NON secret indexé localise la ligne par un `SELECT` à coût constant, puis
**une seule** vérification Argon2id valide le verifier. En cas de succès, il fixe
`password_hash` (Argon2id) et consomme le jeton (il efface `setup_token_selector`,
`setup_token_hash` et `setup_token_expires_at`), le tout de façon indistincte en
cas d'échec (non-fuite d'existence, P3 ; la couche HTTP traduit en 400/410,
exigence 3.2).

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
    SETUP_TOKEN_SEPARATOR,
    hash_password,
    hash_setup_token,
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


# Empreinte factice d'un verifier de jeton de configuration, utilisée pour niveler le
# temps de réponse quand aucune ligne ne correspond au selector présenté (ou que le
# format du jeton est invalide) : on exécute une vérification Argon2id réelle même dans
# ces cas, afin qu'un selector inexistant ne soit pas distinguable d'un verifier faux
# par le temps de calcul (anti-timing, P3 — symétrique de `_dummy_password_hash`).
_DUMMY_SETUP_TOKEN_HASH: str | None = None


def _dummy_setup_token_hash() -> str:
    """Empreinte factice (verifier) mémoïsée, calculée au premier usage."""
    global _DUMMY_SETUP_TOKEN_HASH
    if _DUMMY_SETUP_TOKEN_HASH is None:
        _DUMMY_SETUP_TOKEN_HASH = hash_setup_token(
            "setup-token-timing-attack-mitigation-placeholder"
        )
    return _DUMMY_SETUP_TOKEN_HASH


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


def _split_setup_token(setup_token: str) -> tuple[str, str] | None:
    """Découpe un jeton `"<selector>.<verifier>"` sur le PREMIER `.` (ADR-2).

    Renvoie `(selector, verifier)` si le format est valide, `None` sinon. Le `.` est
    un séparateur non ambigu : `token_urlsafe` n'en produit jamais (alphabet
    base64url). Le découpage sur le premier `.` reste robuste même si un `.` figurait
    côté verifier (`selector` = avant, `verifier` = tout le reste). Un jeton sans `.`,
    ou avec selector/verifier vide, est un format invalide (→ `None`).
    """
    selector, separator, verifier = setup_token.partition(SETUP_TOKEN_SEPARATOR)
    if not separator or not selector or not verifier:
        return None
    return selector, verifier


async def consume_setup_token(
    session: AsyncSession,
    *,
    setup_token: str,
    password: str,
) -> AppUser:
    """Établit le mot de passe d'un compte via son jeton de configuration (REQ-3.2).

    Schéma **selector + verifier** (ADR-2, observation #8) : le jeton présenté a la
    forme `"<selector>.<verifier>"`. Le selector NON secret, indexé, localise la ligne
    par un `SELECT` à coût constant ; le verifier est validé par **une seule**
    vérification Argon2id (lookup à coût constant quel que soit le nombre de comptes,
    P4/NFR-2). En cas de succès — jeton non expiré, compte actif, verifier valide — on
    fixe `password_hash` (Argon2id) et on **consomme** le jeton en effaçant
    `setup_token_selector`, `setup_token_hash` et `setup_token_expires_at` (usage
    unique). Ne valide PAS la transaction : le `commit` relève de l'appelant (routeur),
    qui journalise aussi l'événement.

    Reste **indistinct** en cas d'échec (non-fuite d'existence, P3) : format invalide,
    selector inconnu, jeton expiré, déjà consommé, verifier faux, ou compte inactif
    lèvent tous la même `SetupTokenError`. La couche HTTP la traduit en 400/410
    générique. **Anti-timing** : quand le format est invalide ou qu'aucune ligne ne
    correspond au selector, on exécute malgré tout une vérification Argon2id factice
    (comme `authenticate_user`) pour niveler le temps de réponse, de sorte qu'un
    selector inexistant ne soit pas distinguable d'un verifier faux par le temps.

    Lève :
        SetupTokenError: dans tous les cas d'échec, de façon indistincte.
    """
    parts = _split_setup_token(setup_token)
    if parts is None:
        # Format invalide (pas de `.`, selector ou verifier vide) : Argon2 factice
        # pour niveler le temps, puis échec générique (anti-timing, P3).
        verify_setup_token("", _dummy_setup_token_hash())
        raise SetupTokenError
    selector, verifier = parts

    # Lookup à coût constant par le selector indexé (index UNIQUE partiel, 0011) :
    # au plus une ligne. Ne considérer que les jetons encore présents (non consommés).
    result = await session.execute(
        select(AppUser).where(
            AppUser.setup_token_selector == selector,
            AppUser.setup_token_hash.is_not(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        # Selector inconnu (ou jeton déjà consommé) : Argon2 factice puis échec
        # indistinct (anti-timing, P3 — symétrique de `authenticate_user`).
        verify_setup_token(verifier, _dummy_setup_token_hash())
        raise SetupTokenError

    # UNE SEULE vérification Argon2id, sur la ligne trouvée par le selector.
    verifier_ok = verify_setup_token(verifier, user.setup_token_hash)

    # Expiration : un jeton sans échéance, ou dont l'échéance est passée, est rejeté.
    expires_at = user.setup_token_expires_at
    expired = expires_at is None or expires_at <= datetime.now(UTC)

    # Tout échec (verifier faux, expiré, compte inactif) → même erreur générique,
    # sans consommer le jeton ni révéler la cause (P3).
    if not verifier_ok or expired or not user.is_active:
        raise SetupTokenError

    # Succès : fixe l'empreinte du mot de passe et CONSOMME le jeton (usage unique).
    user.password_hash = hash_password(password)
    user.setup_token_selector = None
    user.setup_token_hash = None
    user.setup_token_expires_at = None
    return user
