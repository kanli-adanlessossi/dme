"""Modèle `tenant` — identité organisationnelle d'un centre médical (tâche 4.1).

Chaque centre médical est un tenant (voir design, « Conception multi-tenant »).
Le `tenant` porte le cycle de vie logique (`status` active | inactive, NFR-4) :
la désactivation conserve les données, masque du public et bloque la connexion
(géré par les tâches ultérieures).

Note RLS : `tenant` lui-même n'est PAS protégé par une politique RLS par tenant
(ce serait circulaire) ; l'accès est cadré dans la couche applicative/admin
(voir design, « Accès à la table tenant »). La RLS forcée (tâche 4.2) s'applique
aux tables *cadrées* au tenant (`medical_center`, `app_user`, puis `patient`,
`visit`).

Design (section « Data Models ») :

    tenant
      id uuid pk
      name text
      region_id uuid fk -> region
      status text  -- active | inactive
      created_at timestamptz
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# Valeurs autorisées du statut de tenant (reflète schemas.admin.TenantStatus).
TENANT_STATUS_VALUES = ("active", "inactive")


class Tenant(Base):
    """Tenant : un centre médical en tant qu'organisation isolée."""

    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    region_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("region.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
