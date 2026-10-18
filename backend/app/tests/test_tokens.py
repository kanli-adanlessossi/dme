"""Tests des helpers JWT d'authentification (tâche 7.2) — exigence 1.1, NFR-7.

Vérifient, sans toucher la base ni le réseau, que :
- un access et un refresh portent bien `sub`, `tenant_id` et `role` ;
- un `tenant_id=None` (platform_admin) est préservé en round-trip ;
- le décodage impose le **type** attendu (un refresh ne passe pas pour un access,
  et réciproquement) ;
- une signature invalide, un jeton mal formé ou expiré lèvent `TokenError` ;
- la durée de vie de l'access reflète la configuration (dimensionnée hors-ligne).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.tokens import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    TokenError,
    access_ttl_seconds,
    create_access_token,
    create_refresh_token,
    decode_token,
)


def test_access_token_porte_sub_tenant_role() -> None:
    """Un access vérifié restitue `sub`, `tenant_id` et `role` (exigence 1.1)."""
    sub = uuid.uuid4()
    tenant_id = uuid.uuid4()
    token = create_access_token(sub=sub, tenant_id=tenant_id, role="clinician")
    claims = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
    assert claims.sub == sub
    assert claims.tenant_id == tenant_id
    assert claims.role == "clinician"
    assert claims.token_type == TOKEN_TYPE_ACCESS


def test_refresh_token_porte_sub_tenant_role_jti_family() -> None:
    """Un refresh vérifié restitue `sub`, `tenant_id`, `role`, `jti` et `family_id`.

    Depuis la tâche 9.2 (G-9), un refresh porte toujours un `jti` (clé de denylist)
    et un `family_id` (famille de rotation), préservés en round-trip.
    """
    sub = uuid.uuid4()
    tenant_id = uuid.uuid4()
    jti = uuid.uuid4()
    family_id = uuid.uuid4()
    token = create_refresh_token(
        sub=sub,
        tenant_id=tenant_id,
        role="tenant_admin",
        jti=jti,
        family_id=family_id,
    )
    claims = decode_token(token, expected_type=TOKEN_TYPE_REFRESH)
    assert claims.sub == sub
    assert claims.tenant_id == tenant_id
    assert claims.role == "tenant_admin"
    assert claims.jti == jti
    assert claims.family_id == family_id


def test_access_token_sans_jti_ni_family() -> None:
    """Un access token ne porte ni `jti` ni `family_id` (→ None au décodage)."""
    token = create_access_token(
        sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="clinician"
    )
    claims = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
    assert claims.jti is None
    assert claims.family_id is None


def test_tenant_id_null_preserve_pour_platform_admin() -> None:
    """`tenant_id=None` (platform_admin) survit au round-trip encode/decode (P2)."""
    sub = uuid.uuid4()
    token = create_access_token(sub=sub, tenant_id=None, role="platform_admin")
    claims = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
    assert claims.tenant_id is None
    assert claims.role == "platform_admin"


def test_refresh_rejete_quand_type_access_attendu() -> None:
    """Un refresh présenté comme access est rejeté (séparation des types)."""
    token = create_refresh_token(
        sub=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role="staff",
        jti=uuid.uuid4(),
        family_id=uuid.uuid4(),
    )
    with pytest.raises(TokenError):
        decode_token(token, expected_type=TOKEN_TYPE_ACCESS)


def test_access_rejete_quand_type_refresh_attendu() -> None:
    """Un access présenté comme refresh est rejeté (séparation des types)."""
    token = create_access_token(sub=uuid.uuid4(), tenant_id=uuid.uuid4(), role="staff")
    with pytest.raises(TokenError):
        decode_token(token, expected_type=TOKEN_TYPE_REFRESH)


def test_signature_invalide_leve_token_error() -> None:
    """Un jeton signé avec un autre secret est rejeté (signature invalide)."""
    sub = uuid.uuid4()
    forged = jwt.encode(
        {
            "sub": str(sub),
            "tenant_id": None,
            "role": "clinician",
            "type": TOKEN_TYPE_ACCESS,
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        # Secret volontairement DIFFÉRENT de celui de l'application (la signature
        # doit donc être rejetée), mais long d'au moins 32 octets pour satisfaire
        # la longueur minimale HS256 recommandée (RFC 7518 §3.2) : un secret trop
        # court ne changerait pas le comportement testé mais émettrait un
        # avertissement de dépréciation parasite (NFR-13).
        "mauvais-secret-mais-suffisamment-long-0123456789",
        algorithm=get_settings().jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_token(forged, expected_type=TOKEN_TYPE_ACCESS)


def test_jeton_malforme_leve_token_error() -> None:
    """Une chaîne qui n'est pas un JWT est rejetée proprement."""
    with pytest.raises(TokenError):
        decode_token("pas-un-jwt", expected_type=TOKEN_TYPE_ACCESS)


def test_jeton_expire_leve_token_error() -> None:
    """Un jeton dont `exp` est passé est rejeté (expiration)."""
    settings = get_settings()
    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": None,
            "role": "clinician",
            "type": TOKEN_TYPE_ACCESS,
            "exp": int((datetime.now(UTC) - timedelta(seconds=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_token(expired, expected_type=TOKEN_TYPE_ACCESS)


def test_access_ttl_reflecte_la_configuration() -> None:
    """La TTL exposée au client correspond à la configuration (NFR-7)."""
    assert access_ttl_seconds() == get_settings().jwt_access_ttl_seconds
    # Défaut dimensionné pour une journée de travail hors ligne (≥ 8 h).
    assert access_ttl_seconds() >= 8 * 60 * 60
