"""Modèle `sync_conflict` — journal des conflits de synchronisation (tâche 10.3).

NFR-6, vérifie P12 (résolution de conflits non destructive). Reflète la table
créée par la migration `0007_sync_engine` (source de vérité pour la RLS forcée et
les privilèges).

Lorsqu'une mutation entre en conflit avec l'état serveur, la résolution
Last-Write-Wins (sur `updated_at`/`version`) désigne un gagnant ; le **perdant**
n'est jamais détruit silencieusement : il est consigné ici (`loser_payload`). Les
entités cliniquement **sensibles** (p. ex. les notes de visite, design question
ouverte #7 : « a minima les notes de visite ») sont marquées `sensitive = true`
afin d'être revues par un humain (ConflictBanner) plutôt qu'écrasées.

C'est une table **cadrée au tenant** (RLS activée + FORCÉE, politique
`tenant_isolation`) : un conflit n'est visible et consignable que dans le tenant
de l'appelant (P11).

Design (section « Data Models ») :

    sync_conflict  (journal des conflits de synchronisation ; NFR-6)
      id bigserial pk
      tenant_id uuid fk -> tenant
      entity text, entity_id uuid
      loser_payload jsonb                -- version perdante conservée (LWW)
      winner_version int
      sensitive boolean                  -- true => à revoir par un humain
      resolved_by uuid null fk -> app_user
      created_at timestamptz
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SyncConflict(Base):
    """Conflit de synchronisation consigné (perdant LWW conservé, P12)."""

    __tablename__ = "sync_conflict"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    # Table cadrée au tenant : `tenant_id` non nul (apposé par le serveur, P2).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=False,
    )
    # Version perdante conservée (LWW) : jamais détruite silencieusement (P12).
    loser_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    winner_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # true => entité cliniquement sensible, à revoir par un humain (NFR-6).
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Rempli lorsqu'un humain a tranché le conflit sensible (NUL tant que non).
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
