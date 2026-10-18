"""Émission et vérification des JWT d'authentification (tâche 7.2).

Ce module est le **chemin unique** d'encodage/décodage des jetons d'accès et de
rafraîchissement. Les couches HTTP (routeur auth) et les futures dépendances de
cadrage (tâche 7.5) l'utilisent sans manipuler directement la bibliothèque JWT.

Conception (design, section « Conception sécurité et audit ») :

- **Les jetons portent `sub`, `tenant_id`, `role`.** Le serveur redérive le
  contexte de tenant depuis le jeton vérifié, jamais depuis le corps de requête
  (P2, exigence 1.1). `tenant_id` est NUL pour un `platform_admin` (transversal).
- **Access de courte à moyenne durée, refresh rotatif.** Les deux portent un
  `type` (`access` / `refresh`) empêchant d'utiliser l'un à la place de l'autre
  (un refresh ne peut pas servir d'access, et inversement).
- **Durée de vie dimensionnée pour le hors-ligne (NFR-7).** Les TTL viennent de la
  configuration (`jwt_access_ttl_seconds` / `jwt_refresh_ttl_seconds`), avec un
  défaut access adapté à une journée de travail hors ligne. Le secret et
  l'algorithme sont également configurables, jamais codés en dur.

Les erreurs de vérification (signature invalide, expiration, mauvais type) sont
unifiées en une seule `TokenError` : la couche HTTP la traduit en 401 générique
sans révéler la cause précise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import get_settings

# Valeurs du claim `type` distinguant les deux usages de jeton. Un access ne peut
# pas être présenté là où un refresh est attendu (et réciproquement) : la
# vérification impose le type attendu.
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


class TokenError(Exception):
    """Jeton invalide, expiré, mal formé ou de type inattendu.

    Volontairement unique et sans cause distinctive : la couche HTTP la traduit en
    **401 générique**, cohérent avec la non-fuite d'information (P3).
    """


@dataclass(frozen=True)
class TokenClaims:
    """Revendications applicatives portées par un JWT vérifié.

    `tenant_id` est NUL pour un `platform_admin` (opérations transversales) ;
    toute couche cadrée au tenant doit traiter ce cas explicitement (tâche 7.5).

    `jti` / `family_id` ne sont portés que par les **refresh tokens** émis après
    la tâche 9.2 (rotation + denylist, G-9) ; ils valent `None` pour un access
    token et pour un refresh « hérité » émis avant cette tâche.
    """

    sub: uuid.UUID
    tenant_id: uuid.UUID | None
    role: str
    token_type: str
    jti: uuid.UUID | None = None
    family_id: uuid.UUID | None = None


def _encode(
    *,
    sub: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str,
    token_type: str,
    ttl_seconds: int,
    jti: uuid.UUID | None = None,
    family_id: uuid.UUID | None = None,
) -> str:
    """Encode un JWT signé portant `sub`, `tenant_id`, `role`, `type` et `exp`.

    `jti` / `family_id` ne sont ajoutés au payload que lorsqu'ils sont fournis
    (refresh tokens, tâche 9.2) ; un access token n'en porte pas.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(sub),
        # NUL pour platform_admin : sérialisé tel quel (null JSON).
        "tenant_id": str(tenant_id) if tenant_id is not None else None,
        "role": role,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    # Famille `jti` de rotation (refresh uniquement). Absents du payload d'un access.
    if jti is not None:
        payload["jti"] = str(jti)
    if family_id is not None:
        payload["family_id"] = str(family_id)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(
    *, sub: uuid.UUID, tenant_id: uuid.UUID | None, role: str
) -> str:
    """Émet un **access token** portant `tenant_id` et `role` (exigence 1.1).

    Sa durée de vie (`jwt_access_ttl_seconds`) est dimensionnée pour couvrir une
    journée de travail hors ligne (NFR-7).
    """
    return _encode(
        sub=sub,
        tenant_id=tenant_id,
        role=role,
        token_type=TOKEN_TYPE_ACCESS,
        ttl_seconds=get_settings().jwt_access_ttl_seconds,
    )


def create_refresh_token(
    *,
    sub: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str,
    jti: uuid.UUID,
    family_id: uuid.UUID,
) -> str:
    """Émet un **refresh token** (rotatif) portant `tenant_id`, `role`, `jti`, `family_id`.

    Un refresh valide permet d'émettre une nouvelle paire access + refresh
    (rotation) à la reconnexion (opportuniste, NFR-7). Depuis la tâche 9.2 (G-9),
    il porte toujours un `jti` (identifiant unique, clé de denylist) et un
    `family_id` (famille de rotation) : l'appelant les fournit pour contrôler la
    continuité de famille (un refresh rotatif conserve le `family_id` d'origine).
    """
    return _encode(
        sub=sub,
        tenant_id=tenant_id,
        role=role,
        token_type=TOKEN_TYPE_REFRESH,
        ttl_seconds=get_settings().jwt_refresh_ttl_seconds,
        jti=jti,
        family_id=family_id,
    )


def access_ttl_seconds() -> int:
    """Durée de vie courante de l'access token (exposée au client via `expires_in`)."""
    return get_settings().jwt_access_ttl_seconds


def decode_token(token: str, *, expected_type: str) -> TokenClaims:
    """Vérifie un JWT et renvoie ses revendications applicatives.

    Contrôle la signature, l'expiration (`exp`) et le **type** attendu
    (`access` / `refresh`). Toute anomalie (signature invalide, jeton expiré,
    revendications manquantes, type inattendu) lève `TokenError` — unifiée pour
    ne pas révéler la cause (401 générique côté HTTP, P3).
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        raise TokenError from exc

    if payload.get("type") != expected_type:
        raise TokenError

    try:
        sub = uuid.UUID(str(payload["sub"]))
        role = payload["role"]
        raw_tenant = payload.get("tenant_id")
    except (KeyError, ValueError) as exc:
        raise TokenError from exc

    if not isinstance(role, str):
        raise TokenError

    tenant_id: uuid.UUID | None
    if raw_tenant is None:
        tenant_id = None
    else:
        try:
            tenant_id = uuid.UUID(str(raw_tenant))
        except ValueError as exc:
            raise TokenError from exc

    # Famille `jti` de rotation (refresh émis depuis la tâche 9.2). Absents d'un
    # access et d'un refresh hérité (→ None) ; un uuid mal formé est une anomalie.
    jti = _optional_uuid(payload.get("jti"))
    family_id = _optional_uuid(payload.get("family_id"))

    return TokenClaims(
        sub=sub,
        tenant_id=tenant_id,
        role=role,
        token_type=expected_type,
        jti=jti,
        family_id=family_id,
    )


def _optional_uuid(raw: object) -> uuid.UUID | None:
    """Convertit un claim UUID optionnel : `None` si absent, `TokenError` si mal formé."""
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise TokenError from exc
