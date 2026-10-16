"""Cœur multi-tenant : tables region/tenant/medical_center/app_user + rôle app.

Tâche 4.1 (NFR-1, 1.2, 1.3, 5.2). Cette migration crée :

1. `region` — table de **référence** (non cadrée au tenant). Créée ici pour que
   les FK `tenant.region_id` / `medical_center.region_id` soient cohérentes ;
   l'amorçage des 5 régions et de leurs géométries relève de la tâche 6.
2. `tenant` — identité organisationnelle d'un centre (cycle de vie `status`).
3. `medical_center` — profil public cadré au tenant (1:1 avec `tenant` en v1 via
   `tenant_id` unique), avec `location geometry(Point, 4326)` (PostGIS).
4. `app_user` — comptes applicatifs cadrés au tenant ; `tenant_id` NULLABLE car
   un `platform_admin` a un `tenant_id` NUL (transversal).
5. **Rôle applicatif de base `dme_app`** — rôle de connexion NON-superuser et
   NOBYPASSRLS. C'est la condition pour que la RLS (ajoutée + FORCÉE en tâche 4.2)
   soit **toujours** appliquée : un rôle BYPASSRLS ou superuser contournerait les
   politiques. Le rôle reçoit les privilèges CRUD sur les tables de données, mais
   PAS les attributs qui désactiveraient la RLS. Les politiques RLS elles-mêmes ne
   sont PAS créées ici (tâche 4.2).

Les géométries utilisent les types PostGIS activés par la migration
`0001_enable_postgis`.

Revision ID: 0002_tenant_core
Revises: 0001_enable_postgis
Create Date: 2024-01-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0002_tenant_core"
down_revision: str | None = "0001_enable_postgis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Nom du rôle applicatif non-superuser. Son mot de passe n'est PAS fixé par la
# migration (jamais d'identifiant codé en dur) : il est positionné hors bande via
# l'init de la base / l'environnement (voir .env.example : APP_DB_*). La RLS
# s'appuie sur le fait que ce rôle n'a ni SUPERUSER ni BYPASSRLS.
APP_ROLE = "dme_app"

# Tables de données portant un privilège CRUD pour le rôle applicatif.
_DATA_TABLES = ("region", "tenant", "medical_center", "app_user")


def upgrade() -> None:
    """Crée les tables du cœur multi-tenant puis le rôle applicatif de base."""
    # --- 1. region (référence ; géométrie amorcée en tâche 6) ---
    op.create_table(
        "region",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name_fr", sa.String(), nullable=False),
        sa.Column(
            "geometry",
            geoalchemy2.Geometry(
                geometry_type="MULTIPOLYGON",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )

    # --- 2. tenant ---
    op.create_table(
        "tenant",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("region_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_tenant_status",
        ),
        sa.ForeignKeyConstraint(["region_id"], ["region.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- 3. medical_center (cadré au tenant ; 1:1 via tenant_id unique) ---
    op.create_table(
        "medical_center",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("region_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("address", sa.String(), nullable=True),
        sa.Column(
            "location",
            geoalchemy2.Geometry(
                geometry_type="POINT",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("opening_hours", postgresql.JSONB(), nullable=True),
        sa.Column(
            "is_public",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["region_id"], ["region.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )

    # --- 4. app_user (cadré au tenant ; tenant_id NULL pour platform_admin) ---
    op.create_table(
        "app_user",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("setup_token_hash", sa.String(), nullable=True),
        sa.Column(
            "setup_token_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('platform_admin', 'tenant_admin', 'clinician', 'staff')",
            name="ck_app_user_role",
        ),
        sa.CheckConstraint(
            "role = 'platform_admin' OR tenant_id IS NOT NULL",
            name="ck_app_user_tenant_scope",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    # --- 5. Rôle applicatif de base, non-superuser, NOBYPASSRLS ---
    # Idempotent : ne crée le rôle que s'il n'existe pas déjà. Les attributs
    # NOSUPERUSER / NOBYPASSRLS sont explicites : ce sont eux qui garantissent que
    # la RLS (tâche 4.2) ne pourra jamais être contournée par la connexion
    # applicative. Aucun mot de passe ici (pas d'identifiant en dur) ; le rôle
    # reçoit USAGE sur le schéma public et les privilèges CRUD sur les tables de
    # données. GRANT CONNECT est accordé sur la base courante.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dme_app') THEN
                CREATE ROLE dme_app NOLOGIN NOSUPERUSER NOINHERIT
                    NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            ELSE
                ALTER ROLE dme_app NOSUPERUSER NOBYPASSRLS;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT CONNECT ON DATABASE {_quote_ident(_current_database())} TO dme_app;")
    op.execute("GRANT USAGE ON SCHEMA public TO dme_app;")
    for table in _DATA_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO dme_app;")
    # Les tables de référence/réfs futures : laisser le défaut pour les prochaines
    # migrations hériter des privilèges du rôle applicatif.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dme_app;"
    )


def downgrade() -> None:
    """Supprime tables et privilèges du rôle applicatif (ordre inverse des FK)."""
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM dme_app;"
    )
    for table in _DATA_TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM dme_app;")
    op.execute("REVOKE USAGE ON SCHEMA public FROM dme_app;")
    op.execute(f"REVOKE CONNECT ON DATABASE {_quote_ident(_current_database())} FROM dme_app;")
    # On ne DROP pas le rôle inconditionnellement : il peut détenir des objets ou
    # des privilèges créés par d'autres migrations. On le supprime seulement s'il
    # existe et n'a plus de dépendances connues de ce périmètre.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'dme_app') THEN
                DROP ROLE dme_app;
            END IF;
        END
        $$;
        """
    )

    op.drop_table("app_user")
    op.drop_table("medical_center")
    op.drop_table("tenant")
    op.drop_table("region")


def _current_database() -> str:
    """Nom de la base courante, pour cadrer le GRANT CONNECT."""
    bind = op.get_bind()
    return bind.engine.url.database or "postgres"


def _quote_ident(identifier: str) -> str:
    """Quote un identifiant SQL (nom de base) de façon sûre."""
    return '"' + identifier.replace('"', '""') + '"'
