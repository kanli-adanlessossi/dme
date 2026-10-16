"""Journal d'audit append-only, distribué-compatible (tâche 5.1).

NFR-3 — vérifie P5 (complétude de l'audit). Cette migration crée la table
`audit_log` conçue selon la section « Journal d'audit (append-only, écriture
toujours) » du design :

1. **Schéma** (section « Data Models ») :

       audit_log
         id bigserial pk
         tenant_id uuid null       -- NUL pour les actions de niveau plateforme
         actor_user_id uuid null
         action text, entity text, entity_id uuid null
         event_at timestamptz      -- horodatage CLIENT (événements hors ligne)
         received_at timestamptz   -- horodatage de réception SERVEUR (NFR-3)
         ip inet

2. **Append-only / immuable.** Le journal doit être inviolable :
   - le rôle applicatif `dme_app` reçoit **uniquement INSERT** (et SELECT, voir
     ci-dessous), jamais UPDATE/DELETE ;
   - en défense en profondeur, un **trigger** `audit_log_block_mutation` lève une
     exception sur tout UPDATE/DELETE, quel que soit le rôle (y compris le
     propriétaire du schéma qui, détenant la table, n'est pas bridé par les seuls
     GRANT). Ainsi l'immutabilité ne dépend pas que des privilèges.

3. **Lecture réservée au platform_admin.** Le design demande « INSERT et (pour
   l'administrateur de plateforme) SELECT ». Comme l'auth/RBAC (tâche 7) n'est pas
   encore câblée, on matérialise cette intention au niveau base par un rôle
   `dme_platform_admin` qui détient le SELECT sur `audit_log`, tandis que le rôle
   applicatif standard `dme_app` ne reçoit QUE INSERT (+ SELECT, voir note). Le
   chemin de lecture d'audit (console plateforme) utilisera ce rôle/chemin dédié.

   Note : `dme_app` reçoit aussi SELECT ici car le même rôle de connexion sert à
   la fois l'écriture d'audit et les lectures d'audit du platform_admin tant que
   la séparation de rôle fine n'est pas introduite (tâche 7/8). Ce qui est
   verrouillé de façon dure et testée, c'est l'**absence d'UPDATE/DELETE**.

4. **Écrivain NON contraint par le contexte de tenant.** Contrairement à
   `medical_center`/`app_user` (migration 0003), on **n'active PAS** de politique
   RLS par-tenant sur `audit_log`. L'audit doit capturer les actions de niveau
   plateforme (`tenant_id` NUL) : une politique ``tenant_id = app.tenant_id``
   masquerait précisément ces lignes (``NULL = <uuid>`` est faux). Le design :
   « il NE PEUT donc PAS se trouver derrière une politique de lecture RLS par
   tenant qui masquerait les lignes à tenant NULL ». L'insertion d'un événement
   plateforme réussit donc toujours, indépendamment de tout `SET LOCAL
   app.tenant_id` posé par la requête courante.

   Révoquer explicitement l'héritage des DEFAULT PRIVILEGES (migration 0002, qui
   accorde CRUD complet sur les futures tables) garantit que `dme_app` ne reçoit
   PAS UPDATE/DELETE sur `audit_log` : on révoque tout puis on ré-accorde le strict
   nécessaire.

Revision ID: 0004_audit_log
Revises: 0003_rls_tenant_isolation
Create Date: 2024-01-04 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0004_audit_log"
down_revision: str | None = "0003_rls_tenant_isolation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rôle applicatif de connexion (créé en 0002). Reçoit INSERT (+ SELECT), jamais
# UPDATE/DELETE sur `audit_log`.
APP_ROLE = "dme_app"

# Rôle de lecture d'audit (platform_admin). Matérialise au niveau base l'intention
# « SELECT réservé au platform_admin ». Le câblage RBAC applicatif relève des
# tâches 7/8 ; ici on prépare un rôle dédié détenant le SELECT.
PLATFORM_READER_ROLE = "dme_platform_admin"

# Nom du trigger/fonction bloquant les mutations (immutabilité append-only).
_BLOCK_FUNC = "audit_log_block_mutation"
_BLOCK_TRIGGER = "trg_audit_log_block_mutation"


def upgrade() -> None:
    """Crée `audit_log`, verrouille l'immutabilité et cadre les privilèges."""
    # --- 1. Table audit_log ---
    # `id` est un bigserial (clé technique monotone) ; `event_at` est fourni par
    # le client (peut être antérieur en cas d'événement hors ligne), `received_at`
    # est apposé par le serveur (défaut now()).
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # NUL pour les actions de niveau plateforme (jamais masqué par la RLS :
        # aucune politique par-tenant n'est posée sur cette table).
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Horodatage CLIENT de l'événement (hors ligne). Obligatoire : tout
        # événement audité porte le moment où il s'est produit côté client ; pour
        # un événement né côté serveur, l'appelant passe l'instant courant.
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        # Horodatage SERVEUR de réception (NFR-3). Défaut now() : apposé à l'insert.
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Index de lecture d'audit : par tenant puis par réception (ordre de parcours
    # naturel d'une console plateforme).
    op.create_index(
        "ix_audit_log_tenant_received",
        "audit_log",
        ["tenant_id", "received_at"],
    )

    # --- 2. Immutabilité : trigger bloquant UPDATE/DELETE (défense en profondeur) ---
    # Le trigger s'applique à TOUT rôle (y compris le propriétaire), là où les
    # seuls GRANT ne brident pas le propriétaire de la table. Toute tentative de
    # mutation lève une exception → la transaction échoue.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_BLOCK_FUNC}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log est append-only : % interdit', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_BLOCK_TRIGGER}
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION {_BLOCK_FUNC}();
        """
    )

    # --- 3. Rôle de lecture d'audit (platform_admin), idempotent ---
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT FROM pg_roles WHERE rolname = '{PLATFORM_READER_ROLE}'
            ) THEN
                CREATE ROLE {PLATFORM_READER_ROLE} NOLOGIN NOSUPERUSER NOINHERIT
                    NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
        END
        $$;
        """
    )

    # --- 4. Privilèges cadrés sur audit_log ---
    # D'abord TOUT révoquer (y compris les DEFAULT PRIVILEGES hérités de 0002 qui
    # accordent INSERT/UPDATE/DELETE/SELECT sur les nouvelles tables), puis
    # ré-accorder le strict nécessaire : INSERT (+ SELECT) à `dme_app`, SELECT au
    # lecteur plateforme. JAMAIS UPDATE/DELETE pour un rôle applicatif.
    op.execute(f"REVOKE ALL ON TABLE audit_log FROM {APP_ROLE};")
    op.execute(f"GRANT INSERT, SELECT ON TABLE audit_log TO {APP_ROLE};")
    # `id` est un bigserial → séquence associée ; INSERT doit pouvoir l'avancer.
    op.execute(
        f"GRANT USAGE, SELECT ON SEQUENCE audit_log_id_seq TO {APP_ROLE};"
    )
    op.execute(f"GRANT SELECT ON TABLE audit_log TO {PLATFORM_READER_ROLE};")


def downgrade() -> None:
    """Défait privilèges, trigger, fonction, index puis la table (ordre inverse)."""
    op.execute(f"REVOKE ALL ON TABLE audit_log FROM {PLATFORM_READER_ROLE};")
    op.execute(f"REVOKE ALL ON SEQUENCE audit_log_id_seq FROM {APP_ROLE};")
    op.execute(f"REVOKE ALL ON TABLE audit_log FROM {APP_ROLE};")
    # Supprimer le rôle lecteur s'il existe (il ne détient que ce SELECT ici).
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM pg_roles WHERE rolname = '{PLATFORM_READER_ROLE}'
            ) THEN
                DROP ROLE {PLATFORM_READER_ROLE};
            END IF;
        END
        $$;
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS {_BLOCK_TRIGGER} ON audit_log;")
    op.execute(f"DROP FUNCTION IF EXISTS {_BLOCK_FUNC}();")
    op.drop_index("ix_audit_log_tenant_received", table_name="audit_log")
    op.drop_table("audit_log")
