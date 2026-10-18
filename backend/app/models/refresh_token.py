"""Modèle `refresh_token` — rotation + denylist du refresh (tâche 9.2, G-9).

Table serveur de rotation des refresh tokens. Chaque refresh émis est enregistré
avec un `jti` unique et un `family_id` de rotation. À l'échange (rotation), le
`jti` consommé est marqué (`consumed_at`) ; un rejeu d'un `jti` déjà consommé est
refusé et invalide TOUTE la famille (détection de rejeu/vol). Voir ADR G-9 et la
section « Data Models » du design.

Chemin **en ligne uniquement** (`/api/auth/refresh`). Les postes hors ligne ne
touchent jamais cette table : ils continuent de travailler sur l'access valide
jusqu'à son expiration (NFR-7). Aucune PHI ici : uniquement des identifiants
opaques (`jti`, `family_id`, `user_id`) et des horodatages.

Pas de RLS par-tenant sur cette table : le chemin `/refresh` est **pré-auth**
(aucun `app.tenant_id` posé — l'access n'est pas encore émis), donc une politique
cadrée au tenant rendrait les lignes invisibles et casserait la fonctionnalité.
La correction de portée se fait par `user_id` / `jti` dans les requêtes. La
rationale complète figure dans la migration `0010_refresh_token`.

Design (section « Data Models ») :

    refresh_token  (rotation + denylist ; cadrée au tenant via user_id)
      jti uuid pk                  -- identifiant unique du refresh émis
      family_id uuid               -- famille de rotation (un vol invalide la famille)
      user_id uuid fk -> app_user
      consumed_at timestamptz null -- posé quand le jti est échangé (rotation)
      expires_at timestamptz       -- aligné sur le refresh TTL (14 j)
      created_at timestamptz
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RefreshToken(Base):
    """Refresh token émis (rotation + denylist) ; cadré à l'utilisateur via `user_id`."""

    __tablename__ = "refresh_token"

    # Identifiant unique du refresh émis. Fourni par l'application à l'émission
    # (uuid4 côté serveur) : pas de défaut serveur — il sert de clé de denylist.
    jti: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
    )
    # Famille de rotation : un refresh rotatif conserve le `family_id` d'origine.
    # Un `jti` consommé rejoué invalide toute la famille (détection de vol).
    family_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Posé lorsque le jti est échangé (rotation). NUL = refresh encore utilisable.
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Aligné sur le refresh TTL (14 j) : un refresh expiré est refusé même non consommé.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        # Invalidation de famille (marquage de tous les jti d'une famille) et
        # purge par utilisateur : index sur les deux colonnes de filtrage.
        Index("ix_refresh_token_user_id", "user_id"),
        Index("ix_refresh_token_family_id", "family_id"),
    )
