"""Modèle de la table de référence `service_type` (tâche 6.1).

`service_type` est une table de **référence** (non cadrée au tenant), au même
titre que `region` : elle énumère les types de services qu'un centre médical peut
proposer (p. ex. maternité, pharmacie, laboratoire). Comme toute donnée de
référence (design, section « Données de référence vs données de tenant »), elle
est **lisible par tous** et ne porte **aucune politique RLS** par-tenant.

La table de liaison `medical_center_service` (centre ↔ service) N'EST PAS créée
ici : elle relève d'une tâche ultérieure (voir design, « Data Models »). Seul le
référentiel `service_type` est introduit par cette tâche.

Design (section « Data Models ») :

    service_type (référence)      -- p. ex. maternité, pharmacie, laboratoire
      id uuid pk
      name_fr text
"""

from __future__ import annotations

import uuid

from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ServiceType(Base):
    """Type de service proposé par un centre (donnée de référence, lecture permissive)."""

    __tablename__ = "service_type"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name_fr: Mapped[str] = mapped_column(String, nullable=False)
