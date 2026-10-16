"""Modèle `audit_log` — journal d'audit append-only (tâche 5.1).

NFR-3, vérifie P5. Reflète la table créée par la migration `0004_audit_log` (qui
reste la source de vérité pour l'immutabilité et les privilèges : trigger
bloquant UPDATE/DELETE, GRANT INSERT seul au rôle applicatif). Le modèle sert au
**chemin d'écriture** (le helper d'audit de la tâche 5.2) et aux lectures
plateforme.

Points de conception (section « Journal d'audit (append-only, écriture
toujours) ») :

- `tenant_id` est **nullable** : NUL pour les actions de niveau plateforme. La
  table ne porte volontairement PAS de politique RLS par-tenant, sans quoi ces
  lignes à tenant NUL seraient masquées (``NULL = app.tenant_id`` est faux).
- `event_at` est l'horodatage **client** de l'événement (préservé tel quel pour
  les événements hors ligne téléversés à la sync) ; `received_at` est apposé par
  le **serveur** à la réception (défaut SQL `now()`), matérialisant l'audit
  distribué (NFR-3).

Design (section « Data Models ») :

    audit_log  (append-only)
      id bigserial pk
      tenant_id uuid null
      actor_user_id uuid null
      action text, entity text, entity_id uuid null
      event_at timestamptz    -- horodatage côté client (hors ligne)
      received_at timestamptz -- horodatage de réception serveur (NFR-3)
      ip inet
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text, text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AuditLog(Base):
    """Enregistrement d'audit immuable (insertion seule au niveau base)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    # NUL pour les actions de niveau plateforme (jamais masqué : pas de RLS
    # par-tenant sur cette table).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=True,
    )
    # Horodatage CLIENT de l'événement (préservé pour les événements hors ligne).
    event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # Horodatage SERVEUR de réception (NFR-3) ; apposé à l'insert par défaut SQL.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
