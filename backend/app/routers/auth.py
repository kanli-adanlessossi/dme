"""Routeur d'authentification (tâche 3 ; login/refresh implémentés en tâche 7.2).

Endpoints : login, refresh, set-credentials. `login` et `refresh` émettent des
JWT portant `tenant_id` et `role` (dérivés côté serveur, jamais du corps — P2) ;
`set-credentials` (tâche 7.4) consomme un jeton de configuration à usage unique et
expirant pour fixer le mot de passe (exigence 3.2), sans révéler l'état du compte.

Conception (design, « Conception sécurité et audit » / « Échecs
d'authentification ») :

- Le `login` valide les identifiants via `authenticate_user` (tâche 7.1), émet une
  paire access + refresh, et journalise l'événement d'auth (succès/échec) dans
  l'`audit_log` (NFR-3, vers P5). Un échec renvoie un **401 générique** qui ne
  révèle jamais si l'e-mail existe (P3). La limitation de débit / le verrouillage
  relèvent de la tâche 7.3 : on NE mute PAS `failed_login_count` ici.
- Le `refresh` vérifie un refresh token valide et émet une nouvelle paire
  (rotation du refresh, design « refresh rotatif »).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import build_event, record_audit_standalone
from app.core.config import get_settings
from app.core.db import get_session
from app.core.tokens import (
    TOKEN_TYPE_REFRESH,
    TokenError,
    access_ttl_seconds,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    Role,
    SetCredentialsRequest,
    SetCredentialsResponse,
    TokenResponse,
)
from app.schemas.common import ErrorResponse
from app.services.auth import (
    AuthenticationError,
    SetupTokenError,
    consume_setup_token,
    find_user_by_email,
    is_tenant_active,
    verify_credentials,
)
from app.services.lockout import (
    AccountLockedError,
    ensure_not_locked,
    register_failure,
    register_success,
)
from app.services.refresh_tokens import (
    RefreshDenied,
    consume_refresh,
    record_issued_refresh,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

def _refresh_expires_at() -> datetime:
    """Échéance d'un refresh émis maintenant, alignée sur le TTL configuré (14 j)."""
    return datetime.now(UTC) + timedelta(
        seconds=get_settings().jwt_refresh_ttl_seconds
    )


# 401 générique partagé par login et refresh : jamais de cause distinctive (P3).
_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Identifiants invalides.",
)
_INVALID_REFRESH = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Jeton de rafraîchissement invalide ou expiré.",
)
# 410 générique pour un jeton de configuration invalide/expiré/déjà consommé :
# jamais de cause distinctive ne révélant si le compte/jeton existe (P3).
_INVALID_SETUP_TOKEN = HTTPException(
    status_code=status.HTTP_410_GONE,
    detail="Jeton de configuration invalide, expiré ou déjà utilisé.",
)


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Identifiants invalides (générique)."},
        429: {
            "model": ErrorResponse,
            "description": "Trop de tentatives : compte verrouillé temporairement (NFR-2).",
        },
    },
    summary="Connexion : émet access + refresh portant tenant_id et rôle",
)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Authentifie l'utilisateur et émet une paire de jetons (exigence 1.1).

    Journalise l'événement d'auth (succès/échec/verrouillage) dans l'`audit_log`
    (NFR-3). En cas d'échec, renvoie un 401 générique sans révéler si l'e-mail
    existe (P3). Protection anti force brute (NFR-2, tâche 7.3) : les échecs
    répétés sur un compte existant incrémentent `failed_login_count` et, au seuil
    atteint, posent `locked_until` ; tant que le verrou court, toute tentative —
    même avec le bon mot de passe — renvoie **429** jusqu'à son expiration ; un
    succès réinitialise le compteur et lève le verrou.
    """
    # Chargement du compte (pur) : nécessaire pour appliquer le verrouillage sur la
    # ligne existante. Un e-mail inconnu => `user is None` ; aucun état n'est alors
    # muté et seule la mitigation temporelle de `verify_credentials` défend P3.
    user = await find_user_by_email(session, email=payload.email)

    # Verrou actif ? Refus en 429 AVANT toute vérification du mot de passe : un
    # compte verrouillé est rejeté même avec des identifiants corrects (NFR-2).
    if user is not None:
        try:
            ensure_not_locked(user)
        except AccountLockedError as locked:
            await record_audit_standalone(
                build_event(
                    request,
                    action="login.lockout",
                    tenant_id=user.tenant_id,
                    actor_user_id=user.id,
                )
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Trop de tentatives : compte temporairement verrouillé.",
                headers={"Retry-After": str(locked.retry_after_seconds)},
            ) from None

    try:
        user = verify_credentials(user, payload.password)
    except AuthenticationError:
        if user is not None:
            # Échec sur un compte existant : on incrémente le compteur (et on pose
            # le verrou au seuil), puis on valide la mutation en base.
            newly_locked = register_failure(user)
            await session.commit()
            if newly_locked:
                # Le seuil vient d'être atteint : on journalise le verrouillage.
                await record_audit_standalone(
                    build_event(
                        request,
                        action="login.lockout",
                        tenant_id=user.tenant_id,
                        actor_user_id=user.id,
                    )
                )
        # Échec audité (acteur/tenant inconnus pour un e-mail inexistant).
        await record_audit_standalone(
            build_event(
                request,
                action="login.failure",
                tenant_id=user.tenant_id if user is not None else None,
                actor_user_id=user.id if user is not None else None,
            )
        )
        raise _INVALID_CREDENTIALS from None

    # Tenant désactivé ? Un membre d'un tenant au statut `inactive` se voit refuser
    # la connexion (exigence 3.4) — même avec des identifiants valides. Réponse
    # 401 générique, sans révéler l'état du tenant (P3). Défense en profondeur : la
    # désactivation bascule déjà `is_active` (refusé plus haut), ce contrôle
    # garantit le refus au niveau du tenant lui-même.
    if not await is_tenant_active(session, user):
        await record_audit_standalone(
            build_event(
                request,
                action="login.failure",
                tenant_id=user.tenant_id,
                actor_user_id=user.id,
            )
        )
        raise _INVALID_CREDENTIALS from None

    # Succès : on réinitialise l'état de verrouillage.
    register_success(user)

    # Succès : jetons dérivés du compte authentifié (jamais du corps — P2). On
    # ouvre une nouvelle famille de rotation (`family_id`) et un `jti` unique, puis
    # on enregistre le refresh émis dans la denylist (chemin en ligne, G-9). La
    # réinitialisation du verrou ET l'insertion du refresh sont validées ensemble.
    access = create_access_token(
        sub=user.id, tenant_id=user.tenant_id, role=user.role
    )
    family_id = uuid.uuid4()
    jti = uuid.uuid4()
    refresh_token = create_refresh_token(
        sub=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        jti=jti,
        family_id=family_id,
    )
    await record_issued_refresh(
        session,
        jti=jti,
        family_id=family_id,
        user_id=user.id,
        expires_at=_refresh_expires_at(),
    )
    await session.commit()

    await record_audit_standalone(
        build_event(
            request,
            action="login.success",
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
        )
    )

    return TokenResponse(
        access=access,
        refresh=refresh_token,
        expires_in=access_ttl_seconds(),
        tenant_id=user.tenant_id,
        role=Role(user.role),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Refresh token invalide ou expiré."}
    },
    summary="Rafraîchit la paire de jetons depuis un refresh token valide",
)
async def refresh(
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Émet une nouvelle paire de jetons à partir d'un refresh valide (rotation).

    Vérifie le refresh token (signature, expiration, type), **consomme** son `jti`
    dans la denylist serveur (chemin EN LIGNE, G-9), puis redérive `tenant_id` et
    `role` depuis le jeton (jamais du corps — P2) et émet une paire access +
    refresh fraîche. Le nouveau refresh conserve le `family_id` d'origine (rotation
    continue) avec un `jti` neuf.

    Refus en 401 générique (P3) : refresh invalide/expiré/mauvais type, refresh
    **hérité sans `jti`** (émis avant la tâche 9.2 — invalidé volontairement : la
    fenêtre est bornée par le TTL de 14 j), `jti` déjà consommé (rejeu) ou inconnu.
    Un rejeu invalide toute la famille (détection de vol, voir service).
    """
    try:
        claims = decode_token(payload.refresh, expected_type=TOKEN_TYPE_REFRESH)
    except TokenError:
        raise _INVALID_REFRESH from None

    # Refresh hérité (pré-9.2) sans `jti`/`family_id` : aucune entrée de denylist
    # ne peut le couvrir, on le refuse (durcissement assumé, borné par le TTL).
    if claims.jti is None or claims.family_id is None:
        raise _INVALID_REFRESH from None

    # Consommation atomique + anti-rejeu (invalide la famille en cas de rejeu).
    try:
        await consume_refresh(session, jti=claims.jti)
    except RefreshDenied:
        # On valide l'éventuelle invalidation de famille posée par le service.
        await session.commit()
        raise _INVALID_REFRESH from None

    # Rotation : nouveau `jti`, MÊME `family_id` (continuité de la famille).
    new_jti = uuid.uuid4()
    access = create_access_token(
        sub=claims.sub, tenant_id=claims.tenant_id, role=claims.role
    )
    refresh_token = create_refresh_token(
        sub=claims.sub,
        tenant_id=claims.tenant_id,
        role=claims.role,
        jti=new_jti,
        family_id=claims.family_id,
    )
    await record_issued_refresh(
        session,
        jti=new_jti,
        family_id=claims.family_id,
        user_id=claims.sub,
        expires_at=_refresh_expires_at(),
    )
    await session.commit()

    return TokenResponse(
        access=access,
        refresh=refresh_token,
        expires_in=access_ttl_seconds(),
        tenant_id=claims.tenant_id,
        role=Role(claims.role),
    )


@router.post(
    "/set-credentials",
    response_model=SetCredentialsResponse,
    status_code=status.HTTP_200_OK,
    responses={
        400: {
            "model": ErrorResponse,
            "description": "Jeton de configuration invalide (sans révéler le compte).",
        },
        410: {
            "model": ErrorResponse,
            "description": "Jeton de configuration expiré ou déjà consommé.",
        },
    },
    summary="Consomme un jeton de configuration à usage unique et fixe le mot de passe",
)
async def set_credentials(
    payload: SetCredentialsRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SetCredentialsResponse:
    """Établit les identifiants via le jeton de configuration (exigence 3.2).

    Consomme un jeton de configuration à usage unique et expirant : vérifie qu'il
    correspond à un compte, qu'il n'est ni expiré ni déjà consommé, puis fixe le
    mot de passe (Argon2id) et invalide le jeton (usage unique). Un jeton invalide,
    expiré ou déjà consommé est rejeté en **410 générique** sans révéler l'état du
    compte (P3, design « Configuration des identifiants »). L'événement est
    journalisé dans l'`audit_log` (NFR-3).
    """
    try:
        user = await consume_setup_token(
            session, setup_token=payload.setup_token, password=payload.password
        )
    except SetupTokenError:
        # Jeton invalide / expiré / déjà consommé : 410 générique, sans révéler si
        # un compte/jeton existe (non-fuite d'existence, P3).
        raise _INVALID_SETUP_TOKEN from None

    # Mutation (empreinte du mot de passe posée + jeton consommé) validée en base.
    await session.commit()

    await record_audit_standalone(
        build_event(
            request,
            action="credentials.set",
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
        )
    )

    return SetCredentialsResponse(email=user.email)
