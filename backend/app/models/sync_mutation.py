"""Modèle `sync_mutation` — registre d'idempotence du push (tâche 10.1).

NFR-6, vérifie P10 (synchronisation idempotente et sans perte). Reflète la table
créée par la migration `0007_sync_engine` (source de vérité pour la RLS forcée et
les privilèges).

Le protocole de sync figé (tâche 3) porte l'idempotence par `mutation_id` (UUID
de la mutation de l'outbox) : rejouer une mutation déjà appliquée — typiquement
après une coupure en cours de push — doit être **sans effet** (ni doublon ni
corruption). On matérialise cette garantie par une table dont la **clé primaire
est `mutation_id`** :

- la **première** application d'une mutation insère une ligne (dans la même
  transaction que la mutation métier, atomique) ;
- un **rejeu** retrouve cette ligne et renvoie l'issue mémorisée (`duplicate`)
  sans ré-appliquer la mutation (chaque mutation est appliquée **au plus une
  fois**, P10).

C'est une table **cadrée au tenant** (RLS activée + FORCÉE) : un `mutation_id`
n'est visible et réutilisable que dans le tenant de l'appelant, de sorte qu'un
client ne peut pas rejouer/injecter via un `mutation_id` d'un autre tenant (P11).

    sync_mutation  (registre d'idempotence ; cadré au tenant)
      mutation_id uuid pk          -- clé d'idempotence (NFR-6, P10)
      tenant_id uuid fk -> tenant
      entity text, entity_id uuid
      result_status text           -- applied | conflict (issue mémorisée)
      server_version int null      -- version serveur après application
      created_at timestamptz
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SyncMutation(Base):
    """Mutation de l'outbox déjà appliquée (clé d'idempotence `mutation_id`, P10)."""

    __tablename__ = "sync_mutation"

    # Clé d'idempotence : l'UUID de mutation fourni par le client (P10).
    mutation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
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
    # Issue mémorisée de la première application, rendue à chaque rejeu.
    result_status: Mapped[str] = mapped_column(Text, nullable=False)
    # Version serveur après application (si connue) — rendue au rejeu.
    server_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
