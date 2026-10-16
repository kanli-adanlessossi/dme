"""Modèle `patient` — domaine clinique cadré au tenant, PHI, synchronisable (tâche 9.1).

Exigences 5.1, 5.2, 5.4, 5.5, 5.6, 5.8, NFR-1, NFR-3, NFR-4, NFR-6.

`patient` est une table **cadrée au tenant** porteuse de **PHI** : elle reçoit la
RLS activée + FORCÉE avec la politique `tenant_isolation` (USING + WITH CHECK sur
`app.tenant_id`), exactement comme `medical_center`/`app_user` (migration 0003).
La RLS proprement dite est posée par la migration qui crée la table (voir
`0006_patient_visit`) ; ce modèle en est le reflet ORM.

Particularités **local-first / synchronisation** (design, « Synchronisation » et
« Data Models ») :

- `id` est un **UUIDv7 généré côté client** (NFR-6). Contrairement aux autres
  tables (qui ont `server_default gen_random_uuid()`), on ne pose **aucun défaut
  serveur** : la clé est stable dès l'écriture locale hors ligne et n'est jamais
  réécrite à la synchronisation (évite les collisions entre postes).
- `updated_at` + `version` fournissent la base de la résolution de conflits
  Last-Write-Wins (NFR-6).
- `deleted_at` matérialise la **suppression logique** (NFR-4, P6) : une PHI
  « supprimée » est exclue des lectures mais conservée en stockage.

Autres garde-fous :

- `consent_to_store` capture le consentement du patient (NFR-4, REQ-5.6).
- `UNIQUE (tenant_id, mrn)` garantit l'unicité du dossier dans le tenant
  (REQ-5.4). `mrn` est le numéro de dossier **local au tenant**.
- `national_id` est **optionnel** (beaucoup de patients n'en ont pas).

Les endpoints CRUD (tâche 9.2) et les tests (tâche 9.3) ne sont PAS implémentés
ici : cette tâche ne crée que le schéma.

Design (section « Data Models ») :

    patient  (domaine d'amorce cadré au tenant, PHI ; synchronisable)
      id uuid pk                         -- UUIDv7 généré côté client (NFR-6)
      tenant_id uuid fk -> tenant
      mrn text                           -- numéro de dossier local au tenant
      given_name text, family_name text
      dob date, sex text, phone text
      national_id text null              -- optionnel
      consent_to_store boolean not null  -- capture du consentement (NFR-4, REQ-5.6)
      created_by uuid fk -> app_user
      created_at timestamptz
      updated_at timestamptz             -- base de résolution LWW (NFR-6)
      version int not null default 1     -- compteur de version pour conflits
      deleted_at timestamptz null        -- suppression logique (NFR-4)
      UNIQUE (tenant_id, mrn)            -- unicité patient dans le tenant (REQ-5.4)
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Patient(Base):
    """Patient cadré au tenant (PHI, synchronisable, suppression logique)."""

    __tablename__ = "patient"

    # UUIDv7 généré côté client (NFR-6) : AUCUN défaut serveur, la clé provient de
    # l'écriture locale et reste stable à la synchronisation.
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
    # Numéro de dossier local au tenant ; unique dans le tenant (REQ-5.4).
    mrn: Mapped[str] = mapped_column(String, nullable=False)
    given_name: Mapped[str] = mapped_column(String, nullable=False)
    family_name: Mapped[str] = mapped_column(String, nullable=False)
    dob: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    sex: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    # Optionnel : beaucoup de patients n'ont pas d'identifiant national.
    national_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Capture du consentement au stockage (NFR-4, REQ-5.6).
    consent_to_store: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
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

    __table_args__ = (
        # Unicité du dossier patient dans le tenant (REQ-5.4).
        UniqueConstraint("tenant_id", "mrn", name="uq_patient_tenant_mrn"),
    )
