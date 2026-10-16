"""Amorçage des 5 régions du Togo depuis le GeoJSON ouvert (tâche 6.2).

Exigences 7.6 et 9.2. Ce module charge `data/togo-regions.geojson` (jeu de
données ouvert et attribué — geoBoundaries / OpenStreetMap, ODbL 1.0 ; voir
`data/README.md`) et **upserte** les 5 régions administratives canoniques du
Togo (Savanes, Kara, Centrale, Plateaux, Maritime) dans la table de référence
`region`, avec leur géométrie `MultiPolygon` en SRID 4326.

Points de conception :

- **Table de référence, non cadrée au tenant.** `region` ne porte aucune
  politique RLS (migrations 0002/0003) : l'amorçage n'a donc pas besoin de poser
  `app.tenant_id`. Il s'exécute avec le compte **propriétaire** du schéma (même
  connexion que les migrations), car le rôle applicatif runtime `dme_app`
  convient aussi (il détient le CRUD sur `region`), mais l'amorçage est logiquement
  une opération d'administration du référentiel exécutée au démarrage, juste après
  `alembic upgrade head`.

- **Idempotence (ré-exécutable sans effet de bord).** L'upsert se fait **sur le
  `code`** (contrainte `UNIQUE(code)`), via `INSERT … ON CONFLICT (code) DO
  UPDATE`. Relancer l'amorçage ne crée pas de doublon et ne lève pas d'erreur :
  il réaligne simplement `name_fr` et la géométrie sur le GeoJSON courant. C'est
  exactement le comportement voulu pour un amorçage joué à chaque
  `docker compose up` (NFR-12).

- **Conversion Polygon → MultiPolygon en 4326.** La colonne `region.geometry` est
  `geometry(MultiPolygon, 4326)`. Le GeoJSON source contient des `Polygon`
  simples ; on normalise donc systématiquement via PostGIS :
  `ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326))`.
  `ST_GeomFromGeoJSON` lit la géométrie sans SRID explicite ; `ST_SetSRID(…,
  4326)` l'étiquette en WGS84 (le GeoJSON est par convention en CRS84/4326), et
  `ST_Multi` promeut un `Polygon` en `MultiPolygon` (un `MultiPolygon` déjà
  multiple est renvoyé inchangé). Le résultat satisfait toujours le type de la
  colonne et reste exploitable par `ST_Within` (validation géo, tâche 11).

- **Pas de code SQL injecté.** La chaîne GeoJSON de chaque géométrie, le `code` et
  le `name_fr` sont **liés comme paramètres** (aucune interpolation).

Le GeoJSON est validé avant écriture : exactement les 5 codes canoniques,
chacun présent une seule fois, chacun porteur d'une géométrie `Polygon`/
`MultiPolygon`. Toute dérive (code manquant/en trop, géométrie absente) lève une
erreur explicite plutôt que d'amorcer un référentiel incomplet.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import async_session_factory

logger = logging.getLogger(__name__)

# Les 5 régions administratives canoniques du Togo (du nord au sud). Le Grand
# Lomé N'EST PAS une 6ᵉ région : il est inclus dans Maritime. Cet ensemble est la
# vérité de référence contre laquelle le GeoJSON est validé.
CANONICAL_REGION_CODES: frozenset[str] = frozenset(
    {"SAVANES", "KARA", "CENTRALE", "PLATEAUX", "MARITIME"}
)

# Types de géométrie GeoJSON acceptés pour une région (promus en MultiPolygon).
_ACCEPTED_GEOMETRY_TYPES: frozenset[str] = frozenset({"Polygon", "MultiPolygon"})

# Upsert idempotent sur `code`. La géométrie est normalisée en MultiPolygon/4326
# côté PostGIS. Valeurs liées comme paramètres (anti-injection).
_UPSERT_REGION = text(
    """
    INSERT INTO region (code, name_fr, geometry)
    VALUES (
        :code,
        :name_fr,
        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326))
    )
    ON CONFLICT (code) DO UPDATE
        SET name_fr = EXCLUDED.name_fr,
            geometry = EXCLUDED.geometry
    """
)


@dataclass(frozen=True)
class RegionSeed:
    """Une région prête à amorcer : code canonique, nom FR, géométrie GeoJSON."""

    code: str
    name_fr: str
    # Fragment GeoJSON de la géométrie (objet `{"type": …, "coordinates": …}`),
    # sérialisé en chaîne pour `ST_GeomFromGeoJSON`.
    geometry_geojson: str


def load_region_seeds(path: Path | None = None) -> list[RegionSeed]:
    """Charge et valide les régions depuis le GeoJSON (`path` ou configuration).

    Lève `FileNotFoundError` si le fichier est absent, et `ValueError` si le
    contenu ne correspond pas exactement aux 5 régions canoniques avec géométrie.
    N'écrit rien en base — fonction pure, testable sans PostgreSQL.
    """
    geojson_path = path or get_settings().regions_geojson_path
    if not geojson_path.exists():
        raise FileNotFoundError(
            f"GeoJSON des régions introuvable : {geojson_path}. "
            "Voir data/README.md (tâche 6.2)."
        )

    raw = json.loads(geojson_path.read_text(encoding="utf-8"))
    if raw.get("type") != "FeatureCollection":
        raise ValueError(
            f"GeoJSON invalide : type attendu 'FeatureCollection', reçu "
            f"{raw.get('type')!r}."
        )

    features = raw.get("features") or []
    seeds: dict[str, RegionSeed] = {}
    for feature in features:
        props = feature.get("properties") or {}
        code = props.get("code")
        if code is None:
            raise ValueError(
                "Chaque feature doit porter une propriété 'code' (région "
                f"canonique). Feature sans code : {props!r}."
            )
        code = str(code).strip().upper()
        if code not in CANONICAL_REGION_CODES:
            raise ValueError(
                f"Code de région non canonique : {code!r}. Attendus : "
                f"{sorted(CANONICAL_REGION_CODES)}."
            )
        if code in seeds:
            raise ValueError(f"Code de région dupliqué dans le GeoJSON : {code!r}.")

        geometry = feature.get("geometry")
        if not geometry or geometry.get("type") not in _ACCEPTED_GEOMETRY_TYPES:
            geom_type = geometry.get("type") if geometry else None
            raise ValueError(
                f"La région {code!r} doit porter une géométrie Polygon/"
                f"MultiPolygon (reçu : {geom_type!r})."
            )
        if not geometry.get("coordinates"):
            raise ValueError(f"La région {code!r} a une géométrie sans coordonnées.")

        name_fr = props.get("name_fr") or code.capitalize()
        seeds[code] = RegionSeed(
            code=code,
            name_fr=str(name_fr),
            geometry_geojson=json.dumps(geometry, ensure_ascii=False),
        )

    missing = CANONICAL_REGION_CODES - seeds.keys()
    if missing:
        raise ValueError(
            f"GeoJSON incomplet : régions canoniques manquantes {sorted(missing)}."
        )

    # Ordre canonique nord→sud pour un amorçage déterministe/lisible.
    order = ["SAVANES", "KARA", "CENTRALE", "PLATEAUX", "MARITIME"]
    return [seeds[code] for code in order]


async def seed_regions(session: AsyncSession, path: Path | None = None) -> int:
    """Upserte les 5 régions (idempotent, sur `code`) dans la session fournie.

    N'ouvre ni ne valide de transaction : l'appelant gère le cadre transactionnel
    (voir `run_region_seed`). Renvoie le nombre de régions amorcées (5).
    """
    seeds = load_region_seeds(path)
    for seed in seeds:
        await session.execute(
            _UPSERT_REGION,
            {
                "code": seed.code,
                "name_fr": seed.name_fr,
                "geojson": seed.geometry_geojson,
            },
        )
    return len(seeds)


async def run_region_seed(path: Path | None = None) -> int:
    """Point d'entrée autonome : ouvre une session, upserte, valide la transaction.

    Appelé au démarrage (après `alembic upgrade head`, voir `entrypoint.sh`) pour
    que la table de référence `region` soit toujours peuplée au `docker compose
    up` (NFR-12). Idempotent : relancer ne duplique rien.
    """
    count = 0
    async with async_session_factory() as session, session.begin():
        count = await seed_regions(session, path)
    logger.info("Amorçage des régions : %d régions upsertées.", count)
    return count
