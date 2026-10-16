"""Schémas d'authentification (tâche 3).

Couvre `POST /api/auth/login`, `POST /api/auth/refresh` et
`POST /api/auth/set-credentials`. Les jetons portent `tenant_id` et `role`
(dérivés côté serveur, jamais fournis par le client — P2). La durée de vie de
l'access token est dimensionnée pour une journée de travail hors ligne (NFR-7) ;
`expires_in` l'expose au client pour sa gestion de session locale.
"""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class Role(str, Enum):
    """Rôles applicatifs (exigence 2.1)."""

    platform_admin = "platform_admin"
    tenant_admin = "tenant_admin"
    clinician = "clinician"
    staff = "staff"


class LoginRequest(BaseModel):
    """Identifiants de connexion.

    Aucun `tenant_id` n'est accepté ici : le serveur le dérive du compte
    authentifié puis l'inscrit dans le jeton (P2, exigence 1.1).
    """

    email: EmailStr = Field(..., examples=["admin@centre-lome.tg"])
    password: str = Field(..., min_length=1, examples=["motdepasse-exemple"])


class TokenResponse(BaseModel):
    """Paire de jetons émise à la connexion / au rafraîchissement.

    Les jetons encodent `sub`, `tenant_id` et `role` ; ces champs sont répétés en
    clair ici pour le confort du client (session locale hors-ligne, NFR-7).
    """

    access: str = Field(..., description="JWT d'accès (courte à moyenne durée).")
    refresh: str = Field(..., description="JWT de rafraîchissement (rotatif).")
    token_type: str = Field(default="bearer", examples=["bearer"])
    expires_in: int = Field(
        ...,
        description="Durée de vie de l'access token en secondes "
        "(dimensionnée pour une journée hors ligne, NFR-7).",
        examples=[43200],
    )
    tenant_id: UUID | None = Field(
        None,
        description="Tenant porté par le jeton. NUL pour un platform_admin "
        "(opérations transversales).",
    )
    role: Role = Field(..., description="Rôle porté par le jeton.")


class RefreshRequest(BaseModel):
    """Demande de rafraîchissement à partir d'un refresh token valide."""

    refresh: str = Field(..., description="JWT de rafraîchissement précédemment émis.")


class SetCredentialsRequest(BaseModel):
    """Consomme un jeton de configuration à usage unique et fixe le mot de passe.

    Le jeton de configuration remplace tout envoi de mot de passe en clair
    (exigence 3.2). Un jeton expiré ou déjà consommé est rejeté en 400/410 sans
    révéler l'état du compte.
    """

    setup_token: str = Field(
        ...,
        description="Jeton de configuration à usage unique et expirant.",
        examples=["st_8a1f...e4"],
    )
    password: str = Field(
        ...,
        min_length=12,
        description="Nouveau mot de passe (haché en Argon2id côté serveur).",
        examples=["un-mot-de-passe-robuste"],
    )


class SetCredentialsResponse(BaseModel):
    """Confirmation que les identifiants ont été établis."""

    email: EmailStr
    status: str = Field(default="credentials_set", examples=["credentials_set"])
