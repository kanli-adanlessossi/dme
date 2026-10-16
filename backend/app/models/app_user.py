"""Modèle `app_user` — comptes applicatifs cadrés au tenant (tâche 4.1).

Cadré au tenant, SAUF le `platform_admin` dont le `tenant_id` est NUL (opérations
transversales). C'est pour cette raison que `tenant_id` est **nullable** ici,
contrairement aux autres tables cadrées au tenant où il est non nul. La RLS forcée
(tâche 4.2) devra tenir compte de ce cas (les lignes à tenant NULL ne sont
visibles d'aucun contexte tenant applicatif).

Les identifiants ne sont jamais livrés en clair : `password_hash` (Argon2id) reste
NUL tant qu'il n'est pas défini, et un jeton de configuration à usage unique et
expirant (`setup_token_hash` / `setup_token_expires_at`) sert à l'initialisation
(exigence 3.2). Le hachage effectif et le flux d'auth relèvent de la tâche 7 ;
ici on ne crée que le schéma.

Design (section « Data Models ») :

    app_user
      id uuid pk
      tenant_id uuid null fk -> tenant   -- nul seulement pour platform_admin
      email text unique
      password_hash text null            -- Argon2id ; nul tant que non défini
      setup_token_hash text null
      setup_token_expires_at timestamptz null
      failed_login_count int default 0
      locked_until timestamptz null
      role text  -- platform_admin | tenant_admin | clinician | staff
      is_active boolean
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# Rôles applicatifs autorisés (reflète schemas.auth.Role).
APP_USER_ROLE_VALUES = ("platform_admin", "tenant_admin", "clinician", "staff")


class AppUser(Base):
    """Compte utilisateur ; `tenant_id` NUL uniquement pour un platform_admin."""

    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # Nullable : NUL seulement pour platform_admin (transversal, voir design).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=True,
    )
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    setup_token_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    setup_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    failed_login_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
