"""Moteur de synchronisation : tables `sync_mutation` (idempotence) + `sync_conflict` (tâche 10).

Exigences 4.2, 4.3, 4.4, 4.6, NFR-3, NFR-6, NFR-13 — vérifie P10, P11, P12.

Cette migration crée les deux tables de support du moteur de sync côté serveur,
toutes deux **cadrées au tenant** avec la RLS activée + FORCÉE et la politique
`tenant_isolation` (USING + WITH CHECK), exactement sur le modèle des migrations
0003/0006. Elles portent une colonne `tenant_id` non nulle apposée par le serveur
depuis le jeton (P2) ; la RLS garantit l'isolation inter-tenant à la sync (P11).

1. **`sync_mutation` — registre d'idempotence (P10).** Le protocole figé (tâche 3)
   porte l'idempotence par `mutation_id` (UUID) : rejouer une mutation déjà
   appliquée doit être **sans effet** (NFR-6). On matérialise cette garantie par
   une table dont la **clé primaire est `mutation_id`** : la première application
   d'une mutation insère une ligne ; un rejeu retrouve cette ligne et renvoie le
   résultat mémorisé (`duplicate`) sans ré-appliquer. La clé primaire + le
   `WITH CHECK` RLS empêchent aussi qu'un `mutation_id` d'un autre tenant soit
   réutilisé pour fuiter/injecter (P11).

       sync_mutation  (registre d'idempotence ; cadré au tenant)
         mutation_id uuid pk          -- clé d'idempotence (NFR-6, P10)
         tenant_id uuid fk -> tenant
         entity text, entity_id uuid
         result_status text           -- applied | conflict (issue mémorisée)
         server_version int null      -- version serveur après application
         created_at timestamptz

2. **`sync_conflict` — journal des conflits (P12).** Reflète le modèle du design
   (« Data Models »). En cas de divergence résolue en Last-Write-Wins, le
   **perdant** est consigné ici (jamais détruit silencieusement) ; les entités
   cliniquement sensibles (`sensitive = true`, p. ex. notes de visite) sont
   marquées pour revue humaine.

       sync_conflict  (journal des conflits de synchronisation ; NFR-6)
         id bigserial pk
         tenant_id uuid fk -> tenant
         entity text, entity_id uuid
         loser_payload jsonb          -- version perdante conservée (LWW)
         winner_version int
         sensitive boolean            -- true => à revoir par un humain
         resolved_by uuid null fk -> app_user
         created_at timestamptz

Privilèges : le rôle applicatif `dme_app` reçoit le CRUD complet (ré-accordé
explicitement, à l'image de 0006), et l'USAGE/SELECT sur la séquence bigserial de
`sync_conflict` (nécessaire à l'INSERT).

Revision ID: 0007_sync_engine
Revises: 0006_patient_visit
Create Date: 2024-01-07 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0007_sync_engine"
down_revision: str | None = "0006_patient_visit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rôle applicatif de connexion (créé en 0002), non-superuser / NOBYPASSRLS.
APP_ROLE = "dme_app"

# Tables cadrées au tenant créées ici, à protéger par RLS forcée.
_TENANT_SCOPED_TABLES = ("sync_mutation", "sync_conflict")

# Nom de politique aligné sur 0003/0006.
_POLICY_NAME = "tenant_isolation"

# Prédicat partagé USING/WITH CHECK ; tolérant à l'absence du paramètre.
_TENANT_PREDICATE = "tenant_id = current_setting('app.tenant_id', true)::uuid"


def upgrade() -> None:
    """Crée `sync_mutation`/`sync_conflict`, cadre les privilèges, FORCE la RLS."""
    # --- 1. sync_mutation (registre d'idempotence, cadré au tenant) ---
    op.create_table(
        "sync_mutation",
        # Clé d'idempotence : l'UUID de mutation fourni par le client (P10).
        sa.Column("mutation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Issue mémorisée de la première application (rendue à chaque rejeu).
        sa.Column("result_status", sa.Text(), nullable=False),
        sa.Column("server_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("mutation_id"),
    )

    # --- 2. sync_conflict (journal des conflits, cadré au tenant) ---
    op.create_table(
        "sync_conflict",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Version perdante conservée (LWW) : jamais détruite silencieusement (P12).
        sa.Column(
            "loser_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("winner_version", sa.Integer(), nullable=False),
        # true => entité cliniquement sensible, à revoir par un humain (NFR-6).
        sa.Column("sensitive", sa.Boolean(), nullable=False),
        # Rempli lorsqu'un humain a tranché le conflit sensible (NUL tant que non).
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["resolved_by"], ["app_user.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Index de lecture des conflits d'un tenant (console de revue, ConflictBanner).
    op.create_index(
        "ix_sync_conflict_tenant_created",
        "sync_conflict",
        ["tenant_id", "created_at"],
    )

    # --- 3. Privilèges CRUD explicites pour le rôle applicatif ---
    for table in _TENANT_SCOPED_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {APP_ROLE};"
        )
    # `sync_conflict.id` est un bigserial → séquence associée ; INSERT l'avance.
    op.execute(
        f"GRANT USAGE, SELECT ON SEQUENCE sync_conflict_id_seq TO {APP_ROLE};"
    )

    # --- 4. RLS activée + FORCÉE + politique tenant_isolation (P11) ---
    for table in _TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {_POLICY_NAME} ON {table}\n"
            f"  USING ({_TENANT_PREDICATE})\n"
            f"  WITH CHECK ({_TENANT_PREDICATE});"
        )


def downgrade() -> None:
    """Défait RLS, privilèges, index puis tables (ordre inverse des dépendances)."""
    for table in reversed(_TENANT_SCOPED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        op.execute(f"REVOKE ALL ON TABLE {table} FROM {APP_ROLE};")

    op.execute(f"REVOKE ALL ON SEQUENCE sync_conflict_id_seq FROM {APP_ROLE};")
    op.drop_index("ix_sync_conflict_tenant_created", table_name="sync_conflict")
    op.drop_table("sync_conflict")
    op.drop_table("sync_mutation")
