"""Migration initiale : active l'extension PostGIS (NFR-12).

Cette migration établit la ligne de base du schéma versionné. Elle n'introduit
AUCUNE table de domaine — l'isolation multi-tenant (tâche 4), l'audit (tâche 5)
et les entités cliniques (tâches 6/9) apportent leurs propres migrations. Son
unique rôle est d'activer PostGIS, requis par les requêtes et validations
spatiales (frontières du Togo, carte des régions).

Revision ID: 0001_enable_postgis
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0001_enable_postgis"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Active l'extension PostGIS si elle n'est pas déjà présente."""
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")


def downgrade() -> None:
    """Retire l'extension PostGIS.

    `CASCADE` est nécessaire car l'image `postgis/postgis` installe aussi les
    extensions dépendantes `postgis_topology` et `postgis_tiger_geocoder` ;
    `IF EXISTS` garde l'opération idempotente. En pratique, on ne redescend pas
    sous la ligne de base en environnement normal.
    """
    op.execute("DROP EXTENSION IF EXISTS postgis CASCADE;")
