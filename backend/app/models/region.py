"""Modèle de la table de référence `region` (tâche 4.1 — FK avant-compatible).

`region` est une table de **référence** (non cadrée au tenant) : les 5 régions
administratives du Togo. Son amorçage (géométries + lignes) relève de la tâche 6
et N'EST PAS réalisé ici. La table est néanmoins créée dès maintenant afin que
les clés étrangères `tenant.region_id` et `medical_center.region_id` soient
cohérentes et que les migrations restent linéaires (pas de FK « orpheline »
ajoutée après coup).

Le design (section « Data Models ») décrit `region` ainsi :

    region (référence)
      id uuid pk
      code text unique            -- SAVANES, KARA, CENTRALE, PLATEAUX, MARITIME
      name_fr text
      geometry geometry(MultiPolygon, 4326)

La colonne `geometry` est définie ici pour fixer le schéma, mais la tâche 6
chargera les géométries réelles depuis le GeoJSON ouvert.
"""

from __future__ import annotations

import uuid

from geoalchemy2 import Geometry
from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Region(Base):
    """Région administrative du Togo (donnée de référence, lecture permissive)."""

    __tablename__ = "region"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    code: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name_fr: Mapped[str] = mapped_column(String, nullable=False)
    # Géométrie des limites régionales (amorcée en tâche 6). Nullable pour que la
    # création du schéma n'exige pas encore de données spatiales.
    geometry: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
