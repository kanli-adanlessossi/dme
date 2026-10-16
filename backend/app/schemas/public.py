"""Schémas de l'annuaire public (tâche 3) — projection sûre, sans PHI (P4).

Couvre les points d'accès publics (sans auth, lecture seule, tenants actifs
seulement, P8) :
- `GET /api/public/regions`
- `GET /api/public/regions/{code}/centers`
- `GET /api/public/centers/{id}`
- `GET /api/public/geo/togo-regions.geojson`

Ces schémas n'exposent QUE des champs de profil non sensibles du centre médical
(exigence 8.3). Aucune donnée `patient`/`visit` ni champ interne n'y figure (P4).
Le GeoJSON est renvoyé tel quel (FeatureCollection) pour la carte MapLibre.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.schemas.admin import GeoPoint, RegionCode
from app.schemas.center import OpeningHours


class PublicRegion(BaseModel):
    """Région administrative exposée publiquement (donnée de référence)."""

    code: RegionCode
    name_fr: str = Field(..., examples=["Maritime"])
    center_count: int = Field(
        default=0, ge=0, description="Nombre de centres publics actifs dans la région."
    )


class PublicCenterSummary(BaseModel):
    """Résumé public d'un centre pour les listes par région (projection sûre).

    `address`/`location` sont optionnels : un centre peut être intégré sans
    adresse ni coordonnées renseignées (le profil est complété ultérieurement par
    le tenant_admin, tâche 11). Ne sont exposés que des champs non sensibles (P4).
    """

    id: UUID
    name: str
    region_code: RegionCode
    address: str | None = None
    location: GeoPoint | None = None


class PublicCenterDetail(PublicCenterSummary):
    """Profil public complet d'un centre (champs non sensibles uniquement, P4).

    Contact et services sont inclus car publics (exigence 7.4). Aucune PHI, aucun
    champ interne (identité du tenant, statut, utilisateurs) n'est exposé. Les
    champs de contact sont optionnels (un centre peut ne pas les avoir renseignés).
    """

    phone: str | None = Field(default=None, examples=["+22890000000"])
    email: EmailStr | None = None
    opening_hours: list[OpeningHours] = Field(default_factory=list)
    service_types: list[str] = Field(default_factory=list)


class GeoJSONFeatureCollection(BaseModel):
    """Enveloppe GeoJSON des limites de régions pour la carte.

    Forme standard `FeatureCollection` ; servie telle quelle à MapLibre et mise
    en cache par le service worker (NFR-11).
    """

    type: str = Field(default="FeatureCollection", examples=["FeatureCollection"])
    features: list[dict[str, Any]] = Field(default_factory=list)
