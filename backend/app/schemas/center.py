"""Schémas de profil du centre médical (tâche 3).

Couvre `GET /api/center/profile` et `PATCH /api/center/profile` (exigence 6).
Le tenant_admin gère nom, adresse, coordonnées, contact, horaires et services.
Le `tenant_id` n'est jamais un champ modifiable : il est dérivé du jeton (P2).
Les coordonnées sont validées dans la frontière du Togo au traitement (P7).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.schemas.admin import GeoPoint, RegionCode


class OpeningHours(BaseModel):
    """Horaires d'ouverture par jour (champ libre structuré, JSONB côté base)."""

    day: str = Field(..., examples=["lundi"])
    open: str = Field(..., examples=["08:00"])
    close: str = Field(..., examples=["17:00"])


class CenterProfileResponse(BaseModel):
    """Profil complet du centre, visible par son tenant_admin."""

    id: UUID
    tenant_id: UUID = Field(..., description="Dérivé du jeton ; en lecture seule (P2).")
    name: str
    region_code: RegionCode
    address: str
    location: GeoPoint
    phone: str = Field(..., examples=["+22890000000"])
    email: EmailStr
    opening_hours: list[OpeningHours] = Field(default_factory=list)
    service_types: list[str] = Field(
        default_factory=list,
        description="Services proposés (p. ex. maternité, pharmacie, laboratoire).",
    )
    is_public: bool = Field(default=True)


class CenterProfileUpdateRequest(BaseModel):
    """Mise à jour partielle du profil (exigence 6.1).

    Aucun `tenant_id` ni `id` : le serveur cible le centre du tenant authentifié
    (P2). Les coordonnées hors du Togo sont rejetées en 422 (P7).
    """

    name: str | None = Field(None, min_length=1)
    address: str | None = Field(None, min_length=1)
    location: GeoPoint | None = None
    phone: str | None = Field(None, examples=["+22890000000"])
    email: EmailStr | None = None
    opening_hours: list[OpeningHours] | None = None
    service_types: list[str] | None = None
