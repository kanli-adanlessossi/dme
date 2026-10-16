"""Domaine clinique synchronisable : tables `patient`/`visit` + RLS forcée (tâche 9.1).

Exigences 5.1, 5.2, 5.4, 5.5, 5.6, 5.8, NFR-1, NFR-3, NFR-4, NFR-6.

Cette migration crée les deux tables PHI du domaine d'amorce clinique, puis leur
applique la Row-Level Security **activée + FORCÉE** avec la politique
`tenant_isolation`, exactement sur le modèle de la migration 0003
(`medical_center`/`app_user`). Ce sont des tables **cadrées au tenant** et
**jamais exposées publiquement**.

Points de conception (design, sections « Data Models », « Synchronisation »,
« Conception multi-tenant ») :

1. **Clés primaires UUIDv7 côté client (NFR-6).** `patient.id` et `visit.id`
   n'ont **aucun défaut serveur** (contrairement aux autres tables en
   `gen_random_uuid()`) : l'identifiant est généré par le poste à l'écriture
   locale hors ligne et reste stable à la synchronisation (pas de réécriture de
   clé, pas de collision entre postes).

2. **Champs de synchronisation.** `updated_at` + `version` (défaut 1) fournissent
   la base de la résolution Last-Write-Wins ; `created_at` horodate la création.

3. **Suppression logique (NFR-4, P6).** `deleted_at` nullable : NUL = actif ;
   horodaté = supprimé (exclu des lectures, conservé en stockage). Aucun DELETE
   physique sur le chemin CRUD normal (tâche 9.2).

4. **Consentement (NFR-4, REQ-5.6).** `patient.consent_to_store` non nul capture
   le consentement du patient.

5. **Unicité patient (REQ-5.4).** `UNIQUE (tenant_id, mrn)` garantit l'unicité du
   dossier dans le tenant. `national_id` est optionnel (nullable).

6. **RLS forcée (P1, P2).** Pour `patient` et `visit` :
   - ``ENABLE`` puis ``FORCE ROW LEVEL SECURITY`` (le rôle applicatif `dme_app`
     est NON-superuser et NOBYPASSRLS, migration 0002) ;
   - politique ``tenant_isolation`` avec ``USING`` (filtre les lignes lues au
     tenant courant) **et** ``WITH CHECK`` (rejette tout `tenant_id` forgé à
     l'écriture) sur ``current_setting('app.tenant_id', true)::uuid``. Le second
     argument ``true`` rend l'appel tolérant à l'absence du paramètre : sans
     contexte, aucune ligne n'est visible.

7. **Privilèges.** `dme_app` reçoit le CRUD complet (ré-accordé explicitement, à
   l'image de 0004/0005, même si les DEFAULT PRIVILEGES de 0002 le couvriraient).

Revision ID: 0006_patient_visit
Revises: 0005_service_type
Create Date: 2024-01-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0006_patient_visit"
down_revision: str | None = "0005_service_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rôle applicatif de connexion (créé en 0002), non-superuser / NOBYPASSRLS.
APP_ROLE = "dme_app"

# Tables PHI cadrées au tenant créées ici, à protéger par RLS forcée.
_TENANT_SCOPED_TABLES = ("patient", "visit")

# Nom de politique aligné sur 0003 (CREATE POLICY tenant_isolation).
_POLICY_NAME = "tenant_isolation"

# Prédicat partagé USING/WITH CHECK ; tolérant à l'absence du paramètre.
_TENANT_PREDICATE = "tenant_id = current_setting('app.tenant_id', true)::uuid"


def upgrade() -> None:
    """Crée `patient`/`visit`, cadre les privilèges, active + FORCE la RLS."""
    # --- 1. patient (PHI, cadré au tenant, synchronisable) ---
    op.create_table(
        "patient",
        # UUIDv7 côté client : PAS de server_default (NFR-6).
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mrn", sa.String(), nullable=False),
        sa.Column("given_name", sa.String(), nullable=False),
        sa.Column("family_name", sa.String(), nullable=False),
        sa.Column("dob", sa.Date(), nullable=True),
        sa.Column("sex", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        # Optionnel : beaucoup de patients n'ont pas d'identifiant national.
        sa.Column("national_id", sa.String(), nullable=True),
        # Capture du consentement (NFR-4, REQ-5.6) — non nul, aucun défaut.
        sa.Column("consent_to_store", sa.Boolean(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Base de résolution LWW (NFR-6).
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Compteur de version pour conflits (NFR-6).
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        # Suppression logique (NFR-4, P6).
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["app_user.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Unicité du dossier patient dans le tenant (REQ-5.4).
        sa.UniqueConstraint("tenant_id", "mrn", name="uq_patient_tenant_mrn"),
    )

    # --- 2. visit (PHI, cadré au tenant, synchronisable) ---
    op.create_table(
        "visit",
        # UUIDv7 côté client : PAS de server_default (NFR-6).
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("visited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        # Base de résolution LWW (NFR-6).
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        # Suppression logique (NFR-4, P6).
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patient.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["app_user.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Index de lecture des visites d'un patient (parcours naturel).
    op.create_index(
        "ix_visit_tenant_patient",
        "visit",
        ["tenant_id", "patient_id"],
    )

    # --- 3. Privilèges CRUD explicites pour le rôle applicatif ---
    for table in _TENANT_SCOPED_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {APP_ROLE};"
        )

    # --- 4. RLS activée + FORCÉE + politique tenant_isolation (P1, P2) ---
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

    op.drop_index("ix_visit_tenant_patient", table_name="visit")
    op.drop_table("visit")
    op.drop_table("patient")
