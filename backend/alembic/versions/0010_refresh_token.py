"""Rotation + denylist du refresh : table `refresh_token` (tâche 9.2, G-9).

Exigence 9 ; NFR-7, NFR-2. Décision ADR G-9 (Options 2 + 3) : raccourcir l'access
(fait en 9.1, simple configuration) ET ajouter une **famille `jti` + denylist** du
refresh, consultée **uniquement sur le chemin en ligne** (`/api/auth/refresh`).

Cette migration crée la **seule table du lot** introduisant un modèle de données.
Chaque refresh émis y est enregistré avec :

    refresh_token
      jti uuid pk                  -- identifiant unique du refresh émis (uuid4 app)
      family_id uuid not null      -- famille de rotation (un vol invalide la famille)
      user_id uuid fk -> app_user  -- propriétaire du refresh (ON DELETE CASCADE)
      consumed_at timestamptz null -- posé à l'échange (rotation) ; NUL = utilisable
      expires_at timestamptz       -- aligné sur le refresh TTL (14 j)
      created_at timestamptz       -- horodatage d'émission (défaut now())

Comportement (voir `app/services/refresh_tokens.py`) :

- **Émission.** `login` et `refresh` insèrent une ligne (`consumed_at` NUL).
- **Consommation (rotation).** `refresh` marque le `jti` présenté
  (`consumed_at = now()`) de façon **atomique et conditionnelle** (UPDATE … WHERE
  consumed_at IS NULL AND expires_at > now()). Zéro ligne touchée ⇒ refus
  (inconnu / déjà consommé / expiré / rejeu) → 401 générique.
- **Détection de rejeu / vol.** Si le `jti` présenté EXISTE mais était DÉJÀ
  consommé, toute la **famille** est invalidée (tous ses `jti` marqués consommés)
  et la demande est refusée (« un vol invalide la famille »).

**Pas de RLS par-tenant sur cette table — intentionnel.** Le chemin `/refresh` est
**pré-authentification** : l'access token n'est pas encore émis, donc aucun
contexte `app.tenant_id` n'est posé sur la transaction. Une politique RLS cadrée
au tenant (du modèle 0003/0006) rendrait alors AUCUNE ligne visible et casserait
la rotation. La portée est donc assurée applicativement par `user_id` / `jti` dans
les requêtes. La table ne contient **aucune PHI** : uniquement des identifiants
opaques (`jti`, `family_id`, `user_id`) et des horodatages — l'absence de RLS
n'expose donc aucune donnée sensible inter-tenant. La RLS reste **désactivée** sur
cette table (comportement par défaut d'une table nouvellement créée).

**Privilèges.** Les DEFAULT PRIVILEGES posés en 0002 accordent déjà le CRUD sur
les futures tables au rôle applicatif `dme_app`. On ré-accorde néanmoins le CRUD
de façon **explicite** (self-documenting, à l'image de 0004/0005/0006/0007), pour
que le privilège ne dépende pas uniquement d'un défaut hérité.

Revision ID: 0010_refresh_token
Revises: 0009_public_directory
Create Date: 2024-01-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0010_refresh_token"
down_revision: str | None = "0009_public_directory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rôle applicatif de connexion (créé en 0002), non-superuser / NOBYPASSRLS.
APP_ROLE = "dme_app"


def upgrade() -> None:
    """Crée `refresh_token` (sans RLS, pré-auth) + index + CRUD explicite."""
    op.create_table(
        "refresh_token",
        # jti fourni par l'application à l'émission (uuid4) : PAS de défaut serveur.
        sa.Column("jti", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("jti"),
    )
    # Index de filtrage : invalidation de famille (family_id) et purge par user.
    op.create_index("ix_refresh_token_user_id", "refresh_token", ["user_id"])
    op.create_index("ix_refresh_token_family_id", "refresh_token", ["family_id"])

    # CRUD explicite pour le rôle applicatif (hérité des DEFAULT PRIVILEGES de 0002,
    # ré-accordé ici de façon lisible). Pas de RLS : chemin pré-auth, aucune PHI.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE refresh_token TO {APP_ROLE};"
    )


def downgrade() -> None:
    """Supprime les privilèges, les index puis la table `refresh_token`."""
    op.execute(f"REVOKE ALL ON TABLE refresh_token FROM {APP_ROLE};")
    op.drop_index("ix_refresh_token_family_id", table_name="refresh_token")
    op.drop_index("ix_refresh_token_user_id", table_name="refresh_token")
    op.drop_table("refresh_token")
