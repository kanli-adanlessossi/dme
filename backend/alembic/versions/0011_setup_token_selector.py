"""Lookup setup-token en coût constant : colonne `app_user.setup_token_selector`.

Spec `tenant-data-rights`, observation #8 — ADR-2 (schéma « selector + verifier »).
Exigence 3 (critère 4) ; NFR-2.

Le lookup actuel du setup-token est en O(n) : `consume_setup_token` charge tous les
comptes candidats puis exécute un Argon2id par candidat. ADR-2 remplace ce schéma par
un couple **selector + verifier** :

- `selector` : identifiant **non secret**, aléatoire (`secrets.token_urlsafe(16)`),
  stocké **en clair et indexé** → `SELECT` en coût constant ;
- `verifier` : secret dont seule l'empreinte Argon2id reste stockée
  (`setup_token_hash`, inchangé) → **une seule** vérification Argon2 sur la ligne
  trouvée par le selector.

Cette migration est **additive et réversible** : elle ajoute la colonne non secrète
`setup_token_selector` (nullable) et un **index UNIQUE partiel**
(`WHERE setup_token_selector IS NOT NULL`). L'unicité partielle garantit qu'un selector
actif est unique sans contraindre les nombreuses lignes à selector NUL (comptes sans
setup-token en cours). Les setup-tokens déjà émis (sans selector) ne sont plus
consommables par le nouveau chemin : invalidation assumée et documentée (ADR-2), sans
risque — un setup-token est à usage unique et à durée de vie courte.

**Privilèges.** Aucun GRANT spécifique n'est requis : les privilèges s'appliquent au
**niveau table** et `app_user` a déjà reçu `SELECT, INSERT, UPDATE, DELETE` pour le
rôle applicatif `dme_app` en 0002. Une colonne ajoutée hérite de ces privilèges de
table (PostgreSQL n'exige un GRANT par-colonne que si l'on souhaite restreindre plus
finement, ce qui n'est pas le cas ici). Comportement cohérent avec les colonnes
existantes de `app_user`.

Revision ID: 0011_setup_token_selector
Revises: 0010_refresh_token
Create Date: 2024-01-11 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0011_setup_token_selector"
down_revision: str | None = "0010_refresh_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Nom de l'index UNIQUE partiel du selector (lookup O(1), unicité des selectors actifs).
_SELECTOR_INDEX = "ix_app_user_setup_token_selector"


def upgrade() -> None:
    """Ajoute `setup_token_selector` (nullable) + index UNIQUE partiel.

    Aucun GRANT : `app_user` porte déjà le CRUD de `dme_app` (0002) ; la nouvelle
    colonne hérite des privilèges de table.
    """
    op.add_column(
        "app_user",
        sa.Column("setup_token_selector", sa.Text(), nullable=True),
    )
    # Index UNIQUE partiel : n'indexe (et ne contraint l'unicité) que les lignes
    # dont le selector est présent. Les très nombreuses lignes à selector NUL
    # (comptes sans setup-token en cours) ne peuplent pas l'index et ne sont pas
    # soumises à l'unicité. Sert au `SELECT ... WHERE setup_token_selector = :sel`.
    op.create_index(
        _SELECTOR_INDEX,
        "app_user",
        ["setup_token_selector"],
        unique=True,
        postgresql_where=sa.text("setup_token_selector IS NOT NULL"),
    )


def downgrade() -> None:
    """Retire l'index UNIQUE partiel puis la colonne (ordre inverse)."""
    op.drop_index(_SELECTOR_INDEX, table_name="app_user")
    op.drop_column("app_user", "setup_token_selector")
