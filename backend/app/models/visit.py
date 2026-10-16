"""Modèle `visit` — visite clinique cadrée au tenant, PHI, synchronisable (tâche 9.1).

Exigences 5.1, 5.2, 5.5, 5.8, NFR-1, NFR-3, NFR-4, NFR-6.

`visit` est une table **cadrée au tenant** porteuse de **PHI** : elle reçoit la
RLS activée + FORCÉE avec la politique `tenant_isolation` (USING + WITH CHECK sur
`app.tenant_id`), au même titre que `patient` (migration `0006_patient_visit`).

Comme `patient`, c'est une entité **synchronisable** (local-first) :

- `id` est un **UUIDv7 généré côté client** (NFR-6) — aucun défaut serveur, la
  clé est stable dès l'écriture locale hors ligne.
- `updated_at` + `version` fournissent la base de la résolution Last-Write-Wins
  (NFR-6). Les notes de visite sont pressenties « cliniquement sensibles » (conflit
  marqué pour revue plutôt qu'écrasé) ; cette logique relève de la tâche 10.
- `deleted_at` matérialise la **suppression logique** (NFR-4, P6).

`patient_id` référence le patient concerné (même tenant ; l'intégrité inter-tenant
est garantie par la RLS côté serveur et le cadrage applicatif, tâche 9.2).

Les endpoints CRUD (tâche 9.2) et les tests (tâche 9.3) ne sont PAS implémentés
ici : cette tâche ne crée que le schéma.

Design (section « Data Models ») :

    visit  (domaine d'amorce cadré au tenant, PHI ; synchronisable)
      id uuid pk                         -- UUIDv7 généré côté client (NFR-6)
      tenant_id uuid fk -> tenant
      patient_id uuid fk -> patient
      visited_at timestamptz
      reason text, notes text
      created_by uuid fk -> app_user
      updated_at timestamptz             -- base de résolution LWW (NFR-6)
      version int not null default 1
      deleted_at timestamptz null        -- suppression logique (NFR-4)
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Visit(Base):
    """Visite cadrée au tenant (PHI, synchronisable, suppression logique)."""

    __tablename__ = "visit"

    # UUIDv7 généré côté client (NFR-6) : AUCUN défaut serveur.
    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
    )
    # Table cadrée au tenant : `tenant_id` non nul (apposé par le serveur, REQ-5.2).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=False,
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("patient.id", ondelete="RESTRICT"),
        nullable=False,
    )
    visited_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Base de résolution LWW (NFR-6) ; mis à jour à chaque mutation.
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    # Compteur de version pour la détection de conflits (NFR-6).
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    # Suppression logique (NFR-4, P6) : NUL = actif ; horodaté = supprimé.
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
