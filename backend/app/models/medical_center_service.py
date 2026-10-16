"""Table de liaison `medical_center_service` (centre ↔ service_type) — tâche 11.

Relation many-to-many entre un `medical_center` (profil public cadré au tenant)
et les `service_type` (référentiel partagé) qu'il propose. Comme son centre, la
liaison est **cadrée au tenant** : elle porte un `tenant_id` non nul apposé par le
serveur (jamais lu du corps, P2) et reçoit la RLS activée + FORCÉE avec la
politique `tenant_isolation` (migration 0008). Un tenant ne peut donc lier/délier
que les services de SON centre ; le `WITH CHECK` rejette toute ligne forgée pour
un autre tenant.

Design (section « Data Models ») :

    medical_center_service (liaison)
      medical_center_id uuid fk
      service_type_id uuid fk
      tenant_id uuid fk -> tenant   -- cadrage RLS (P2), apposé serveur
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class MedicalCenterService(Base):
    """Lien centre ↔ type de service (cadré au tenant, RLS forcée)."""

    __tablename__ = "medical_center_service"

    medical_center_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("medical_center.id", ondelete="CASCADE"),
        primary_key=True,
    )
    service_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("service_type.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    # Cadrage au tenant : `tenant_id` non nul, apposé par le serveur (P2).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=False,
    )
