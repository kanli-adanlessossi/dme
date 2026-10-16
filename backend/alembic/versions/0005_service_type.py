"""Table de référence `service_type` (tâche 6.1).

Exigences 7.6, 9.2. Cette migration crée la table de **référence**
`service_type` (types de services proposés par un centre : p. ex. maternité,
pharmacie, laboratoire).

Points de conception (design, sections « Données de référence vs données de
tenant » et « Data Models ») :

- **Référence, non cadrée au tenant.** `service_type` est lisible par tous et ne
  porte **aucune politique RLS** par-tenant — exactement comme `region`. On ne
  pose donc ni ``ENABLE ROW LEVEL SECURITY`` ni politique ici (cohérent avec la
  migration 0003 qui ne cadre que `medical_center`/`app_user`).

- **Schéma minimal** (design, « Data Models ») :

      service_type (référence)      -- p. ex. maternité, pharmacie, laboratoire
        id uuid pk
        name_fr text

- **Privilèges.** La migration 0002 a posé des DEFAULT PRIVILEGES accordant
  SELECT/INSERT/UPDATE/DELETE sur les futures tables au rôle applicatif `dme_app`.
  Cette table en hérite donc ; on ré-accorde néanmoins le CRUD de façon
  **explicite** (self-documenting, à l'image de 0004) pour que le privilège ne
  dépende pas uniquement d'un défaut hérité d'une autre migration.

- La table de liaison `medical_center_service` N'EST PAS créée ici (tâche
  ultérieure).

Revision ID: 0005_service_type
Revises: 0004_audit_log
Create Date: 2024-01-05 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0005_service_type"
down_revision: str | None = "0004_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rôle applicatif de connexion (créé en 0002). Reçoit le CRUD sur cette table de
# référence en lecture/écriture (l'administration du référentiel se fera via le
# chemin applicatif ; la lecture publique est permissive).
APP_ROLE = "dme_app"


def upgrade() -> None:
    """Crée `service_type` (référence, sans RLS) et cadre les privilèges."""
    op.create_table(
        "service_type",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name_fr", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # CRUD explicite pour le rôle applicatif (hérité des DEFAULT PRIVILEGES de
    # 0002, ré-accordé ici de façon lisible). Pas de RLS : donnée de référence.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE service_type TO {APP_ROLE};"
    )


def downgrade() -> None:
    """Supprime les privilèges puis la table `service_type`."""
    op.execute(f"REVOKE ALL ON TABLE service_type FROM {APP_ROLE};")
    op.drop_table("service_type")
