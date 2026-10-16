"""Service de l'annuaire public + carte (tâche 12) — projection sûre, sans PHI.

Logique **cœur** des points d'accès publics (routeur `public`), exigences 7.2,
7.3, 7.4, 8.1, 8.2, 8.3 — vérifie P4 (confidentialité des PHI) et P8 (surface
publique « actifs seulement »).

Le service opère sur une **session de lecture publique** (`public_session`, tâche
4.3/12) : celle-ci pose ``app.public_read = 'on'`` SANS poser `app.tenant_id`, de
sorte que la politique RLS PERMISSIVE `public_read` (migration 0009) n'ouvre que
le **SELECT** des centres **publics** de tenants **actifs** — jamais les tenants
désactivés (P8), jamais de PHI (`patient`/`visit` restent derrière la seule RLS
`tenant_isolation`, P4).

Garanties appliquées ici (design, « Données de référence vs données de tenant » /
« Correctness Properties ») :

1. **Projection sûre, liste blanche de colonnes (P4, exigence 8.3).** On ne lit
   QUE des champs de profil non sensibles de `medical_center` (nom, adresse,
   contact, horaires, coordonnées) + le `region.code` et les `service_type.name_fr`
   liés. Aucune colonne interne (`tenant_id`, `is_public`, identité des
   utilisateurs) n'est renvoyée, et aucune table PHI n'est touchée.

2. **Actifs seulement (P8, exigences 8.2, 7.2).** La politique `public_read`
   exclut au moteur les centres de tenants inactifs. Un centre d'un tenant
   désactivé est donc **invisible** sur toute la surface publique : il n'apparaît
   ni dans les listes par région, ni par id (→ 404, `CenterNotFoundError`), et
   n'est pas compté dans l'annuaire des régions.

3. **Région vide → liste vide (exigences 7.3, 8.1).** Lister les centres d'une
   région sans centre public actif renvoie une **liste vide**, pas une erreur. Un
   code de région **inconnu** (hors des 5 canoniques) est en revanche traité par
   le routeur comme 404 (le type `RegionCode` borne l'entrée).

Le service ne valide pas la transaction (lecture seule) : `public_session` s'en
charge en sortie du handler.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.medical_center import MedicalCenter
from app.models.medical_center_service import MedicalCenterService
from app.models.region import Region
from app.models.service_type import ServiceType


class CenterNotFoundError(Exception):
    """Centre inconnu OU appartenant à un tenant inactif/non public (→ 404, P8)."""


class RegionsGeoJSONUnavailableError(Exception):
    """Le GeoJSON des limites de régions est introuvable sur le disque."""


@dataclass(frozen=True)
class PublicRegionData:
    """Région publique : code, nom FR, nombre de centres publics actifs."""

    code: str
    name_fr: str
    center_count: int


@dataclass(frozen=True)
class PublicCenterData:
    """Projection publique sûre d'un centre (champs non sensibles uniquement, P4)."""

    id: uuid.UUID
    name: str
    region_code: str
    address: str | None
    longitude: float | None
    latitude: float | None
    phone: str | None
    email: str | None
    opening_hours: object | None
    service_types: list[str] = field(default_factory=list)


async def _center_coords(
    session: AsyncSession, center_id: uuid.UUID
) -> tuple[float | None, float | None]:
    """Lit (longitude, latitude) du centre via PostGIS, ou (None, None)."""
    result = await session.execute(
        text(
            "SELECT ST_X(location::geometry) AS lon, "
            "ST_Y(location::geometry) AS lat "
            "FROM medical_center WHERE id = :id"
        ),
        {"id": center_id},
    )
    row = result.mappings().one_or_none()
    if row is None or row["lon"] is None:
        return (None, None)
    return (float(row["lon"]), float(row["lat"]))


async def _service_names(session: AsyncSession, center_id: uuid.UUID) -> list[str]:
    """Noms (name_fr) des services liés au centre, triés (projection publique)."""
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


async def _to_center_data(
    session: AsyncSession, center: MedicalCenter, region_code: str
) -> PublicCenterData:
    """Compose la projection publique sûre d'un centre (aucun champ interne)."""
    lon, lat = await _center_coords(session, center.id)
    services = await _service_names(session, center.id)
    return PublicCenterData(
        id=center.id,
        name=center.name,
        region_code=region_code,
        address=center.address,
        longitude=lon,
        latitude=lat,
        phone=center.phone,
        email=center.email,
        opening_hours=center.opening_hours,
        service_types=services,
    )


async def list_public_regions(session: AsyncSession) -> list[PublicRegionData]:
    """Liste les régions (référence) + le nombre de centres publics actifs.

    Les régions proviennent de la table de référence `region` (sans RLS). Le
    décompte des centres n'inclut que les centres **visibles** via la politique
    `public_read` (publics, tenants actifs) : un LEFT JOIN compte 0 pour une
    région sans centre public actif (exigence 7.3). Tri par `code` pour un ordre
    stable.
    """
    result = await session.execute(
        select(
            Region.code,
            Region.name_fr,
            func.count(MedicalCenter.id).label("center_count"),
        )
        .select_from(Region)
        .outerjoin(MedicalCenter, MedicalCenter.region_id == Region.id)
        .group_by(Region.code, Region.name_fr)
        .order_by(Region.code)
    )
    return [
        PublicRegionData(code=code, name_fr=name_fr, center_count=int(count))
        for code, name_fr, count in result.all()
    ]


async def list_region_centers(
    session: AsyncSession, region_code: str
) -> list[PublicCenterData]:
    """Centres publics actifs d'une région (projection sûre) ; vide si aucun.

    La politique `public_read` filtre déjà aux centres publics de tenants actifs
    (P8). On restreint en plus à la région demandée (jointure sur `region.code`).
    Une région sans centre public actif renvoie une **liste vide** (exigences 7.3,
    8.1/8.2), jamais une erreur. Tri par nom pour un affichage stable.
    """
    result = await session.execute(
        select(MedicalCenter)
        .join(Region, Region.id == MedicalCenter.region_id)
        .where(Region.code == region_code)
        .order_by(MedicalCenter.name)
    )
    centers = list(result.scalars().all())
    return [
        await _to_center_data(session, center, region_code) for center in centers
    ]


async def get_public_center(
    session: AsyncSession, center_id: uuid.UUID
) -> PublicCenterData:
    """Profil public d'un centre (projection sûre) ; sinon `CenterNotFoundError`.

    La recherche par id passe par la politique `public_read` : un centre
    inexistant, non public, ou d'un tenant **inactif** est invisible — la requête
    ne ramène rien et le service lève `CenterNotFoundError` (→ 404, P8). Les deux
    cas (inconnu / inactif) sont ainsi **indiscernables** côté public.
    """
    result = await session.execute(
        select(MedicalCenter, Region.code)
        .join(Region, Region.id == MedicalCenter.region_id)
        .where(MedicalCenter.id == center_id)
    )
    row = result.one_or_none()
    if row is None:
        raise CenterNotFoundError
    center, region_code = row
    return await _to_center_data(session, center, region_code)


@lru_cache(maxsize=4)
def _read_geojson(path: Path) -> dict[str, Any]:
    """Lit et met en cache le GeoJSON des régions (fichier statique, immuable).

    Mémoïsé par chemin : le fichier `data/togo-regions.geojson` est servi tel quel
    à de nombreuses requêtes (carte publique) ; on évite une relecture disque à
    chaque appel. Lève `RegionsGeoJSONUnavailableError` si le fichier est absent.
    """
    if not path.exists():
        raise RegionsGeoJSONUnavailableError(str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def load_regions_geojson(path: Path | None = None) -> dict[str, Any]:
    """Renvoie la `FeatureCollection` des limites de régions pour la carte.

    Sert le jeu de données ouvert et attribué `data/togo-regions.geojson` (chemin
    issu de la configuration, surchargeable). Le résultat est une
    `FeatureCollection` GeoJSON standard, servie telle quelle à MapLibre et mise
    en cache par le service worker (exigence 7.6, NFR-11). Donnée publique de
    référence : aucune PHI, aucun accès base. Lève
    `RegionsGeoJSONUnavailableError` si le fichier est introuvable.
    """
    geojson_path = path or get_settings().regions_geojson_path
    return _read_geojson(geojson_path)
