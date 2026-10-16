"""Profil du centre + validation géo : liaison services + CHECK boîte englobante (tâche 11).

Exigences 6.1, 6.2, 6.3 — vérifie P7 (validité géographique).

Cette migration complète le modèle du profil de centre médical pour la tâche 11 :

1. **Table de liaison `medical_center_service` (centre ↔ service_type).** Le
   design (« Data Models ») décrit une table de liaison many-to-many entre un
   centre et les types de services qu'il propose. Comme `medical_center`, c'est
   une table **cadrée au tenant** : elle porte un `tenant_id` non nul (apposé par
   le serveur depuis le jeton, P2) et reçoit la même RLS **activée + FORCÉE** avec
   la politique `tenant_isolation` (USING + WITH CHECK), sur le modèle exact des
   migrations 0003/0006. Ainsi, un tenant ne peut lier/délier que les services de
   SON centre, et aucune ligne forgée pour un autre tenant ne peut être écrite
   (P1/P2). Le `service_type` référencé reste une donnée de **référence**
   (non cadrée) partagée par tous.

       medical_center_service (liaison ; cadré au tenant)
         medical_center_id uuid fk -> medical_center
         service_type_id   uuid fk -> service_type
         tenant_id         uuid fk -> tenant   (cadrage RLS + WITH CHECK, P2)
         PK (medical_center_id, service_type_id)

2. **Repli léger de la validation géographique : CHECK de boîte englobante (P7).**
   La validation **préférée** s'appuie sur `ST_Within` contre la géométrie réelle
   du Togo (union des régions amorcées en tâche 6), appliquée côté service à
   l'écriture (voir `app.services.center`). On ajoute ici un **repli au niveau du
   moteur** : une contrainte `CHECK` imposant que, lorsque `location` est non nul,
   son point tombe dans la boîte englobante du Togo
   (longitude ∈ [-0.15, 1.80], latitude ∈ [6.10, 11.14], design « Validation de
   la frontière du Togo »). Ce garde-fou en base garantit qu'aucune coordonnée
   manifestement hors pays ne peut être persistée, même si un chemin d'écriture
   oubliait la validation applicative (défense en profondeur, P7).

Revision ID: 0008_center_profile_geo
Revises: 0007_sync_engine
Create Date: 2024-01-08 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0008_center_profile_geo"
down_revision: str | None = "0007_sync_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rôle applicatif de connexion (créé en 0002), non-superuser / NOBYPASSRLS.
APP_ROLE = "dme_app"

# Table de liaison cadrée au tenant à protéger par RLS forcée.
_LINK_TABLE = "medical_center_service"

# Nom de politique aligné sur 0003 (CREATE POLICY tenant_isolation).
_POLICY_NAME = "tenant_isolation"

# Prédicat partagé USING/WITH CHECK ; tolérant à l'absence du paramètre.
_TENANT_PREDICATE = "tenant_id = current_setting('app.tenant_id', true)::uuid"

# Boîte englobante du Togo (design « Validation de la frontière du Togo »).
# Longitude ≈ [-0.15, 1.80], latitude ≈ [6.10, 11.14].
_TOGO_LON_MIN, _TOGO_LON_MAX = -0.15, 1.80
_TOGO_LAT_MIN, _TOGO_LAT_MAX = 6.10, 11.14

# Nom de la contrainte CHECK de repli (boîte englobante) sur medical_center.
_BBOX_CHECK = "ck_medical_center_location_in_togo_bbox"


def upgrade() -> None:
    """Crée la liaison services (RLS forcée) + le CHECK boîte englobante (P7)."""
    # --- 1. Table de liaison centre ↔ service_type (cadrée au tenant) ---
    op.create_table(
        _LINK_TABLE,
        sa.Column(
            "medical_center_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "service_type_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        # Cadrage RLS + WITH CHECK (P2) : tenant_id apposé par le serveur.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["medical_center_id"], ["medical_center.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["service_type_id"], ["service_type.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("medical_center_id", "service_type_id"),
    )

    # Privilèges CRUD explicites pour le rôle applicatif (cf. 0004/0005/0006).
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {_LINK_TABLE} TO {APP_ROLE};"
    )

    # RLS activée + FORCÉE + politique tenant_isolation (P1, P2).
    op.execute(f"ALTER TABLE {_LINK_TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_LINK_TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {_POLICY_NAME} ON {_LINK_TABLE}\n"
        f"  USING ({_TENANT_PREDICATE})\n"
        f"  WITH CHECK ({_TENANT_PREDICATE});"
    )

    # --- 2. Repli : CHECK boîte englobante du Togo sur medical_center (P7) ---
    # Lorsque `location` est non nul, son point doit tomber dans la boîte
    # englobante du Togo. Défense en profondeur du moteur (la validation fine
    # ST_Within est appliquée côté service). `ST_X`/`ST_Y` lisent la lon/lat du
    # point ; `location IS NULL OR …` tolère l'absence de coordonnées.
    op.execute(
        f"ALTER TABLE medical_center ADD CONSTRAINT {_BBOX_CHECK} CHECK (\n"
        f"  location IS NULL OR (\n"
        f"    ST_X(location::geometry) BETWEEN {_TOGO_LON_MIN} AND {_TOGO_LON_MAX}\n"
        f"    AND ST_Y(location::geometry) BETWEEN {_TOGO_LAT_MIN} AND {_TOGO_LAT_MAX}\n"
        f"  )\n"
        f");"
    )


def downgrade() -> None:
    """Défait le CHECK boîte englobante puis la table de liaison (ordre inverse)."""
    op.execute(
        f"ALTER TABLE medical_center DROP CONSTRAINT IF EXISTS {_BBOX_CHECK};"
    )
    op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {_LINK_TABLE};")
    op.execute(f"ALTER TABLE {_LINK_TABLE} NO FORCE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_LINK_TABLE} DISABLE ROW LEVEL SECURITY;")
    op.execute(f"REVOKE ALL ON TABLE {_LINK_TABLE} FROM {APP_ROLE};")
    op.drop_table(_LINK_TABLE)
