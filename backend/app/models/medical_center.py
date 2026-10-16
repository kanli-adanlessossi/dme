"""Modèle `medical_center` — profil public du centre (tâche 4.1).

Relation 1:1 avec `tenant` en v1 (`tenant_id` unique). C'est la projection dont
les champs sont lisibles publiquement (carte/annuaire) mais modifiables seulement
par le tenant propriétaire. C'est une table **cadrée au tenant** : elle porte un
`tenant_id` non nul et recevra la RLS forcée en tâche 4.2 (le chemin de lecture
publique utilise une projection sûre dédiée, voir design).

Design (section « Data Models ») :

    medical_center
      id uuid pk
      tenant_id uuid fk -> tenant unique
      name text
      region_id uuid fk -> region
      address text
      location geometry(Point, 4326)
      phone text   -- +228 ...
      email text
      opening_hours jsonb
      is_public boolean default true
"""

from __future__ import annotations

import uuid

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class MedicalCenter(Base):
    """Profil public d'un centre médical (1:1 avec son tenant en v1)."""

    __tablename__ = "medical_center"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # Table cadrée au tenant : `tenant_id` non nul (unique car 1:1 en v1).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    region_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("region.id", ondelete="RESTRICT"),
        nullable=False,
    )
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    # Coordonnées WGS84 ; la validation « dans la frontière du Togo » (P7) est
    # ajoutée par la tâche 11, pas ici.
    location: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    opening_hours: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
