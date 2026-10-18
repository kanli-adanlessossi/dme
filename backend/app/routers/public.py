"""Routeur de l'annuaire public et de la carte (tâche 12) — exigences 7, 8.

Sans auth, lecture seule, projection sûre, tenants actifs seulement (P4, P8).
Implémente le contrat figé en tâche 3 (handlers alors stubs).

Chemin de lecture dédié (design, « Données de référence vs données de tenant ») :
ces endpoints n'empruntent PAS `tenant_session` (pas de contexte de tenant) mais
`public_session`, qui active la politique RLS PERMISSIVE `public_read` (migration
0009). Celle-ci n'ouvre que le **SELECT** des centres **publics** de tenants
**actifs** — jamais de PHI (`patient`/`visit` restent derrière la seule RLS
`tenant_isolation`, P4), jamais un tenant désactivé (P8).

Garanties observables ici :

- **Projection sûre (P4, exigence 8.3).** Les schémas `PublicRegion`,
  `PublicCenterSummary`, `PublicCenterDetail` n'exposent que des champs non
  sensibles ; aucun `tenant_id`, statut, ni donnée PHI.
- **Actifs seulement (P8, exigences 7.2, 8.2).** Un centre d'un tenant désactivé
  est invisible : absent des listes, et un accès par id renvoie 404.
- **Région vide → liste vide (exigences 7.3, 8.1).** Une région sans centre
  public actif renvoie `[]`, pas une erreur. Un code de région hors des 5
  canoniques est rejeté par le type `RegionCode` (422) au niveau du chemin.
- **Centre inconnu/inactif → 404.** Les deux cas sont indiscernables côté public.

Limitation de débit (G-4, exigence 4.1). Chaque route publique est décorée par
`@limiter.limit(public_rate_limit())` (SlowAPI) : au-delà du seuil configuré
(`PUBLIC_RATE_LIMIT`, défaut 60 req/min/IP), la réponse est **429**, au corps minimal
(aucune fuite d'information). C'est le REPLI applicatif documenté ; en production, la
limitation vit de préférence au reverse proxy (voir `deploy/reverse-proxy/`). Le
paramètre `request: Request` est requis par SlowAPI pour accéder à l'IP cliente ; il
ne change ni la projection sûre (P4) ni la règle « actifs seulement » (P8).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import limiter, public_rate_limit
from app.core.tenant_context import public_session
from app.schemas.admin import GeoPoint, RegionCode
from app.schemas.center import OpeningHours
from app.schemas.common import ErrorResponse
from app.schemas.public import (
    GeoJSONFeatureCollection,
    PublicCenterDetail,
    PublicCenterSummary,
    PublicRegion,
)
from app.services.public import (
    CenterNotFoundError,
    PublicCenterData,
    RegionsGeoJSONUnavailableError,
    get_public_center,
    list_public_regions,
    list_region_centers,
    load_regions_geojson,
)

router = APIRouter(prefix="/api/public", tags=["public"])


def _geo_point(center: PublicCenterData) -> GeoPoint | None:
    """Compose un `GeoPoint` à partir des coordonnées, ou None si absentes."""
    if center.longitude is None or center.latitude is None:
        return None
    return GeoPoint(longitude=center.longitude, latitude=center.latitude)


def _opening_hours(center: PublicCenterData) -> list[OpeningHours]:
    """Projette les horaires JSONB vers le schéma public (liste, défaut vide)."""
    return [OpeningHours.model_validate(item) for item in (center.opening_hours or [])]


def _to_summary(center: PublicCenterData) -> PublicCenterSummary:
    """Projette un centre vers le résumé public (champs non sensibles)."""
    return PublicCenterSummary(
        id=center.id,
        name=center.name,
        region_code=RegionCode(center.region_code),
        address=center.address,
        location=_geo_point(center),
    )


def _to_detail(center: PublicCenterData) -> PublicCenterDetail:
    """Projette un centre vers le profil public complet (contact + services)."""
    return PublicCenterDetail(
        id=center.id,
        name=center.name,
        region_code=RegionCode(center.region_code),
        address=center.address,
        location=_geo_point(center),
        phone=center.phone,
        email=center.email,
        opening_hours=_opening_hours(center),
        service_types=center.service_types,
    )


@router.get(
    "/regions",
    response_model=list[PublicRegion],
    summary="Liste les 5 régions du Togo (public)",
)
@limiter.limit(public_rate_limit())
async def list_regions(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(public_session),
) -> list[PublicRegion]:
    """Renvoie les 5 régions + le nombre de centres publics actifs (exigence 7).

    Donnée de référence quasi statique : porte `Cache-Control: public, max-age=300`
    (G-4, exigence 4 AC2, NFR-2). L'API connaît la nature des données ; le proxy
    peut ajouter un cache de bord.
    """
    response.headers["Cache-Control"] = "public, max-age=300"
    regions = await list_public_regions(session)
    return [
        PublicRegion(
            code=RegionCode(region.code),
            name_fr=region.name_fr,
            center_count=region.center_count,
        )
        for region in regions
    ]


@router.get(
    "/regions/{code}/centers",
    response_model=list[PublicCenterSummary],
    responses={
        404: {"model": ErrorResponse, "description": "Code de région inconnu."}
    },
    summary="Liste les centres publics actifs d'une région",
)
@limiter.limit(public_rate_limit())
async def list_region_centers_endpoint(
    request: Request,
    code: RegionCode,
    session: AsyncSession = Depends(public_session),
) -> list[PublicCenterSummary]:
    """Centres actifs de la région ; région vide => liste vide (exigences 7.3, 8.2)."""
    centers = await list_region_centers(session, code.value)
    return [_to_summary(center) for center in centers]


@router.get(
    "/centers/{center_id}",
    response_model=PublicCenterDetail,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Centre inconnu ou appartenant à un tenant inactif (P8).",
        }
    },
    summary="Profil public d'un centre (champs non sensibles, aucune PHI)",
)
@limiter.limit(public_rate_limit())
async def get_center(
    request: Request,
    center_id: UUID,
    session: AsyncSession = Depends(public_session),
) -> PublicCenterDetail:
    """Profil public d'un centre (projection sûre, P4) ; inactif => 404 (P8)."""
    try:
        center = await get_public_center(session, center_id)
    except CenterNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Centre introuvable.",
        ) from None
    return _to_detail(center)


@router.get(
    "/geo/togo-regions.geojson",
    response_model=GeoJSONFeatureCollection,
    summary="Limites GeoJSON des régions du Togo pour la carte",
)
@limiter.limit(public_rate_limit())
async def togo_regions_geojson(
    request: Request,
    response: Response,
) -> GeoJSONFeatureCollection:
    """Renvoie les limites des régions (FeatureCollection) pour MapLibre (exigence 7.6).

    Donnée publique de référence servie depuis `data/togo-regions.geojson` (jeu
    ouvert et attribué), mise en cache par le service worker (NFR-11). Aucun accès
    base, aucune PHI. Porte `Cache-Control: public, max-age=300` (G-4, exigence 4
    AC2, NFR-2) : donnée quasi statique, le proxy peut ajouter un cache de bord.
    """
    response.headers["Cache-Control"] = "public, max-age=300"
    try:
        raw = load_regions_geojson()
    except RegionsGeoJSONUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Limites des régions indisponibles.",
        ) from exc
    return GeoJSONFeatureCollection(
        type=raw.get("type", "FeatureCollection"),
        features=raw.get("features", []),
    )
