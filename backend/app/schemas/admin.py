"""Schémas d'administration de plateforme (tâche 3).

Couvre le cycle de vie des tenants (exigence 3) :
`POST /api/admin/tenants`, `GET /api/admin/tenants`,
`PATCH /api/admin/tenants/{id}`. Toutes ces opérations sont réservées au
platform_admin et auditées (NFR-3).

La création d'un tenant exige nom du centre, région (parmi les 5), adresse,
coordonnées géographiques et contact (exigence 3.1) ; elle crée l'utilisateur
tenant_admin initial et renvoie un jeton de configuration à usage unique
(exigence 3.2) — jamais un mot de passe en clair.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class RegionCode(str, Enum):
    """Les 5 régions administratives canoniques du Togo."""

    SAVANES = "SAVANES"
    KARA = "KARA"
    CENTRALE = "CENTRALE"
    PLATEAUX = "PLATEAUX"
    MARITIME = "MARITIME"


class TenantStatus(str, Enum):
    """Statut logique d'un tenant (cycle de vie, NFR-4)."""

    active = "active"
    inactive = "inactive"


class GeoPoint(BaseModel):
    """Coordonnées WGS84 ; validées dans la frontière du Togo au traitement (P7)."""

    longitude: float = Field(..., ge=-180, le=180, examples=[1.2255])
    latitude: float = Field(..., ge=-90, le=90, examples=[6.1319])


class TenantCreateRequest(BaseModel):
    """Données requises pour intégrer un nouveau tenant (exigence 3.1)."""

    name: str = Field(..., min_length=1, examples=["Centre de santé de Lomé"])
    region_code: RegionCode
    address: str = Field(..., min_length=1, examples=["12 Rue du Commerce, Lomé"])
    location: GeoPoint
    phone: str = Field(
        ...,
        description="Numéro de contact au format togolais (+228).",
        examples=["+22890000000"],
    )
    email: EmailStr = Field(..., examples=["contact@centre-lome.tg"])
    admin_email: EmailStr = Field(
        ...,
        description="E-mail du tenant_admin initial à créer.",
        examples=["admin@centre-lome.tg"],
    )


class TenantResponse(BaseModel):
    """Projection d'administration d'un tenant."""

    id: UUID
    name: str
    region_code: RegionCode
    status: TenantStatus
    created_at: datetime


class TenantCreatedResponse(TenantResponse):
    """Réponse de création : inclut le jeton de configuration à usage unique.

    Le `setup_token` est renvoyé une seule fois à l'administrateur de plateforme
    pour transmission hors bande au tenant_admin (exigence 3.2). Il n'est jamais
    reconsulté ni stocké en clair côté serveur.
    """

    admin_email: EmailStr
    setup_token: str = Field(
        ...,
        description="Jeton de configuration à usage unique et expirant pour le "
        "tenant_admin initial.",
    )
    setup_token_expires_at: datetime


class TenantUpdateRequest(BaseModel):
    """Active/désactive un tenant (exigence 3.4).

    La désactivation conserve les données, masque le tenant du public et bloque
    la connexion de ses membres.
    """

    status: TenantStatus
