"""Service du profil de centre médical + validation géo (tâche 11).

Logique **cœur** de `GET/PATCH /api/center/profile` (exigence 6), appelée par le
routeur `center`. Elle opère toujours sur une **session déjà cadrée au tenant**
(`tenant_session`, tâche 4.3) : le `SET LOCAL app.tenant_id` ayant été posé en
amont, la RLS forcée (migration 0003 pour `medical_center`, 0008 pour la liaison
`medical_center_service`) filtre toutes les lectures au tenant courant et rejette
toute écriture forgée pour un autre tenant (`WITH CHECK`, P1/P2).

Garanties appliquées ici (design, « Components and Interfaces » / « Validation de
la frontière du Togo » / « Correctness Properties ») :

1. **Tenancy autoritaire côté serveur (P2).** Le profil ciblé est TOUJOURS celui
   du tenant du contexte (il y a une relation 1:1 tenant ↔ centre en v1) : le
   `tenant_id` n'est jamais lu du corps. La liaison services est écrite avec le
   `tenant_id` du contexte ; le `WITH CHECK` RLS est le garde-fou ultime.

2. **Validité géographique (P7, exigence 6.2).** À chaque écriture de
   coordonnées, le point est validé comme se situant **dans la frontière du
   Togo**. La validation **préférée** utilise `ST_Within` contre la géométrie du
   pays, dérivée de l'union (`ST_Union`) des géométries de région amorcées en
   tâche 6. Si cette géométrie n'est pas disponible (régions non amorcées), on
   retombe sur un **repli boîte englobante** (longitude ∈ [-0.15, 1.80], latitude
   ∈ [6.10, 11.14]). Un point hors limites lève `CoordinatesOutsideTogoError`,
   traduit en 422 par le routeur (message clair). Une contrainte `CHECK` en base
   (migration 0008) sert de dernière ligne de défense.

3. **Services (exigence 6.1).** Les services proposés sont gérés via la table de
   liaison `medical_center_service` (cadrée au tenant). La mise à jour remplace
   l'ensemble des liens par ceux correspondant aux `service_types` fournis
   (résolus par `service_type.name_fr`). Un nom de service inconnu du référentiel
   lève `UnknownServiceTypeError` (422).

Le service ne valide PAS la transaction : le `commit` est assuré par la
dépendance `tenant_session` en sortie nominale du handler, de sorte que la
mutation et son éventuel enregistrement d'audit (écrit par le routeur dans la
même transaction) soient atomiques (fail-closed).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.medical_center import MedicalCenter
from app.models.medical_center_service import MedicalCenterService
from app.models.region import Region
from app.models.service_type import ServiceType
from app.schemas.admin import GeoPoint

# Boîte englobante du Togo (repli léger si la géométrie des régions est absente).
# Alignée sur le design et sur la validation d'intégration (`services.admin`).
# Longitude ≈ [-0.15, 1.80], latitude ≈ [6.10, 11.14].
_TOGO_LON_MIN, _TOGO_LON_MAX = -0.15, 1.80
_TOGO_LAT_MIN, _TOGO_LAT_MAX = 6.10, 11.14


class CenterNotFoundError(Exception):
    """Le centre du tenant courant est introuvable (profil non intégré)."""


class CoordinatesOutsideTogoError(Exception):
    """Les coordonnées fournies ne se situent pas dans la frontière du Togo (P7)."""


class UnknownServiceTypeError(Exception):
    """Un `service_type` demandé (par son nom) n'existe pas au référentiel."""


@dataclass(frozen=True)
class CenterProfileData:
    """Projection applicative du profil de centre (indépendante de l'ORM/HTTP)."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    region_code: str
    address: str | None
    longitude: float | None
    latitude: float | None
    phone: str | None
    email: str | None
    opening_hours: object | None
    is_public: bool
    service_types: list[str] = field(default_factory=list)


async def _in_togo_fallback(location: GeoPoint) -> bool:
    """Repli boîte englobante : vrai si le point est dans la boîte du Togo (P7)."""
    return (
        _TOGO_LON_MIN <= location.longitude <= _TOGO_LON_MAX
        and _TOGO_LAT_MIN <= location.latitude <= _TOGO_LAT_MAX
    )


async def validate_coordinates_in_togo(
    session: AsyncSession, location: GeoPoint
) -> None:
    """Valide que `location` tombe dans la frontière du Togo (P7, exigence 6.2).

    Stratégie préférée : `ST_Within(point, ST_Union(region.geometry))`, en
    dérivant la frontière du pays de l'union des géométries de région amorcées en
    tâche 6. Si aucune géométrie n'est disponible (régions non amorcées, p. ex.
    base de test minimale), on retombe sur le **repli boîte englobante**.

    Le point est construit côté PostGIS en SRID 4326 à partir de la lon/lat
    **liées comme paramètres** (aucune interpolation, anti-injection). Lève
    `CoordinatesOutsideTogoError` si le point est hors limites.
    """
    # Y a-t-il au moins une géométrie de région amorcée ? Si oui, on utilise la
    # validation spatiale fine ; sinon, repli boîte englobante.
    has_geometry = (
        await session.execute(
            select(Region.id).where(Region.geometry.isnot(None)).limit(1)
        )
    ).scalar_one_or_none() is not None

    if not has_geometry:
        if not await _in_togo_fallback(location):
            raise CoordinatesOutsideTogoError
        return

    # ST_Within contre l'union des régions (frontière du pays). `ST_Union` agrège
    # toutes les géométries régionales ; `ST_Within` teste l'appartenance stricte.
    result = await session.execute(
        text(
            "SELECT ST_Within("
            "  ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),"
            "  (SELECT ST_Union(geometry) FROM region WHERE geometry IS NOT NULL)"
            ")"
        ),
        {"lon": location.longitude, "lat": location.latitude},
    )
    inside = bool(result.scalar_one())
    if not inside:
        raise CoordinatesOutsideTogoError


async def _get_center_for_tenant(session: AsyncSession) -> MedicalCenter | None:
    """Charge le centre du tenant courant (relation 1:1 ; RLS filtre au tenant)."""
    result = await session.execute(select(MedicalCenter))
    return result.scalars().first()


async def _region_code(session: AsyncSession, region_id: uuid.UUID) -> str:
    """Résout le code de la région d'un centre (lecture référence)."""
    result = await session.execute(
        select(Region.code).where(Region.id == region_id)
    )
    return result.scalar_one()


async def _service_names(
    session: AsyncSession, center_id: uuid.UUID
) -> list[str]:
    """Liste les noms (name_fr) des services liés au centre, triés."""
    result = await session.execute(
        select(ServiceType.name_fr)
        .join(
            MedicalCenterService,
            MedicalCenterService.service_type_id == ServiceType.id,
        )
        .where(MedicalCenterService.medical_center_id == center_id)
        .order_by(ServiceType.name_fr)
    )
    return list(result.scalars().all())


async def _coords(session: AsyncSession, center_id: uuid.UUID) -> tuple[float | None, float | None]:
    """Renvoie (longitude, latitude) du centre, ou (None, None) si pas de point."""
    result = await session.execute(
        text(
            "SELECT ST_X(location::geometry) AS lon, ST_Y(location::geometry) AS lat "
            "FROM medical_center WHERE id = :id"
        ),
        {"id": center_id},
    )
    row = result.mappings().one_or_none()
    if row is None or row["lon"] is None:
        return (None, None)
    return (float(row["lon"]), float(row["lat"]))


async def _to_profile_data(
    session: AsyncSession, center: MedicalCenter
) -> CenterProfileData:
    """Compose la projection applicative complète du profil d'un centre."""
    region_code = await _region_code(session, center.region_id)
    lon, lat = await _coords(session, center.id)
    services = await _service_names(session, center.id)
    return CenterProfileData(
        id=center.id,
        tenant_id=center.tenant_id,
        name=center.name,
        region_code=region_code,
        address=center.address,
        longitude=lon,
        latitude=lat,
        phone=center.phone,
        email=center.email,
        opening_hours=center.opening_hours,
        is_public=center.is_public,
        service_types=services,
    )


async def get_center_profile(session: AsyncSession) -> CenterProfileData:
    """Renvoie le profil du centre du tenant courant (P2) ou lève `CenterNotFoundError`.

    La RLS cadre déjà la lecture au tenant : il existe au plus un centre visible
    (relation 1:1 en v1). Un tenant sans centre intégré → `CenterNotFoundError`.
    """
    center = await _get_center_for_tenant(session)
    if center is None:
        raise CenterNotFoundError
    return await _to_profile_data(session, center)


async def _resolve_service_type_ids(
    session: AsyncSession, names: list[str]
) -> list[uuid.UUID]:
    """Résout des noms de services (name_fr) en ids ; nom inconnu → erreur (422)."""
    ids: list[uuid.UUID] = []
    for name in names:
        result = await session.execute(
            select(ServiceType.id).where(ServiceType.name_fr == name)
        )
        service_id = result.scalar_one_or_none()
        if service_id is None:
            raise UnknownServiceTypeError(name)
        ids.append(service_id)
    return ids


async def _replace_services(
    session: AsyncSession,
    *,
    center: MedicalCenter,
    names: list[str],
) -> None:
    """Remplace l'ensemble des liens services du centre par ceux fournis.

    Les ids sont résolus depuis les noms (référentiel partagé) ; les doublons sont
    dédupliqués. On supprime d'abord tous les liens existants du centre puis on
    insère les nouveaux, chacun portant le `tenant_id` du contexte (P2). La RLS
    `WITH CHECK` rejette toute injection inter-tenant.
    """
    service_ids = await _resolve_service_type_ids(session, names)

    await session.execute(
        delete(MedicalCenterService).where(
            MedicalCenterService.medical_center_id == center.id
        )
    )
    seen: set[uuid.UUID] = set()
    for service_id in service_ids:
        if service_id in seen:
            continue
        seen.add(service_id)
        session.add(
            MedicalCenterService(
                medical_center_id=center.id,
                service_type_id=service_id,
                tenant_id=center.tenant_id,
            )
        )
    await session.flush()


async def update_center_profile(
    session: AsyncSession,
    *,
    fields: dict[str, object],
) -> CenterProfileData:
    """Met à jour partiellement le profil du centre du tenant (exigence 6.1).

    `fields` ne contient que les champs effectivement fournis (exclude_unset) parmi
    `name`, `address`, `location`, `phone`, `email`, `opening_hours`,
    `service_types`. `id`/`tenant_id` ne sont jamais modifiables (apposés par le
    serveur, P2).

    - Les coordonnées (`location`) sont validées dans la frontière du Togo avant
      écriture (P7) ; hors limites → `CoordinatesOutsideTogoError` (422).
    - Les services sont remplacés par l'ensemble fourni (noms résolus au
      référentiel) ; un nom inconnu → `UnknownServiceTypeError` (422).

    Lève `CenterNotFoundError` si le tenant n'a pas de centre intégré.
    """
    center = await _get_center_for_tenant(session)
    if center is None:
        raise CenterNotFoundError

    # Coordonnées : valider AVANT toute écriture (P7).
    location = fields.get("location")
    if "location" in fields and location is not None:
        if not isinstance(location, GeoPoint):
            location = GeoPoint.model_validate(location)
        await validate_coordinates_in_togo(session, location)
        center.location = (
            f"SRID=4326;POINT({location.longitude} {location.latitude})"
        )

    # Champs scalaires directement portés par le modèle.
    if "name" in fields and fields["name"] is not None:
        center.name = fields["name"]  # type: ignore[assignment]
    if "address" in fields:
        center.address = fields["address"]  # type: ignore[assignment]
    if "phone" in fields:
        center.phone = fields["phone"]  # type: ignore[assignment]
    if "email" in fields:
        center.email = fields["email"]  # type: ignore[assignment]
    if "opening_hours" in fields:
        center.opening_hours = fields["opening_hours"]

    await session.flush()

    # Services (liaison cadrée au tenant) : remplacement d'ensemble si fourni.
    if "service_types" in fields and fields["service_types"] is not None:
        await _replace_services(
            session,
            center=center,
            names=list(fields["service_types"]),  # type: ignore[arg-type]
        )

    return await _to_profile_data(session, center)
