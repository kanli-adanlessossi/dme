"""Environnement d'exécution Alembic (SQLAlchemy asynchrone / asyncpg).

La DSN de connexion est lue depuis la configuration applicative
(`app.core.config.get_settings`), elle-même alimentée par les variables
d'environnement. Aucun identifiant n'est codé en dur.

`target_metadata` pointe vers `app.core.db.Base.metadata` afin que
l'autogénération des futures migrations détecte les modèles des tâches
suivantes.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Rend le paquet `app` importable quelle que soit la façon dont Alembic est
# lancé. Le script console `alembic` n'ajoute pas le répertoire courant à
# `sys.path` (contrairement à `python -m`/`python fichier.py`) : sans ce garde,
# `from app.core.config import …` échoue avec `ModuleNotFoundError: No module
# named 'app'` au démarrage du conteneur (entrypoint `alembic upgrade head`),
# ce qui casserait le lancement en une commande (NFR-12). La racine projet est
# le dossier parent de `alembic/` (où réside `app/`).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Imports applicatifs après l'ajustement de sys.path ci-dessus (volontaire).
# L'import de `app.models` est volontaire bien qu'il ne soit pas référencé
# directement : il peuple `Base.metadata` (enregistrement des tables) pour
# l'autogénération et la cible de migration.
import app.models  # noqa: F401
from app.core.config import get_settings
from app.core.db import Base

# Objet de configuration Alembic (lit alembic.ini).
config = context.config

# Injecte la DSN async « propriétaire » depuis la configuration applicative.
# Les migrations doivent s'exécuter avec le compte propriétaire du schéma
# (`POSTGRES_*`), PAS avec le rôle applicatif runtime `dme_app` : ce dernier est
# NOLOGIN / non-propriétaire / NOBYPASSRLS et ne peut pas créer d'objets. La
# connexion runtime (API) utilise `database_url` (qui peut pointer sur `dme_app`
# via `APP_DB_*`), mais Alembic reste sur la DSN propriétaire.
config.set_main_option("sqlalchemy.url", get_settings().owner_database_url)

# Journalisation.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Métadonnées cibles pour l'autogénération (modèles des tâches ultérieures).
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Exécute les migrations en mode « offline » (génération de SQL).

    Configure le contexte avec seulement une URL, sans Engine. Utile pour
    produire un script SQL (`alembic upgrade head --sql`) sans base joignable.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure le contexte sur une connexion et exécute les migrations."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Crée un moteur asynchrone et exécute les migrations sur une connexion."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Point d'entrée « online » : exécute les migrations async."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
