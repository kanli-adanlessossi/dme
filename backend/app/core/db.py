"""Moteur et session SQLAlchemy asynchrones.

Fournit :
- `Base` : classe déclarative dont hériteront les modèles des tâches suivantes ;
- `engine` : moteur asynchrone (asyncpg) construit depuis la configuration ;
- `async_session_factory` : fabrique de sessions `AsyncSession` ;
- `get_session` : dépendance FastAPI ouvrant une session par requête.

Note sur le multi-tenant (tâche 4) : la session est fournie par requête dans une
portée transactionnelle propre, de sorte que la future dépendance de contexte de
tenant pourra exécuter `SET LOCAL app.tenant_id = …` au sein de la transaction
sans fuite entre requêtes mutualisées. Aucune logique RLS n'est implémentée ici.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """Base déclarative commune à tous les modèles ORM."""


# Moteur asynchrone partagé. `pool_pre_ping` évite les connexions mortes après
# une coupure réseau (pertinent pour une pile tolérante à l'intermittence).
# `connect_args={"timeout": …}` borne l'établissement d'une connexion asyncpg :
# si la base est injoignable, l'appel échoue vite au lieu de bloquer plusieurs
# secondes (lecture readiness `/health`, requêtes). Le délai vient de la config.
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    future=True,
    connect_args={"timeout": settings.db_connect_timeout_seconds},
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def check_database_ready() -> bool:
    """Sonde de readiness : `True` si la base répond à un `SELECT 1`, sinon `False`.

    Isolée ici (plutôt qu'inline dans `/health`) pour être surchargeable en test
    sans tenter de vraie connexion réseau, et pour centraliser la sémantique de
    readiness. Ne lève jamais : toute erreur (base injoignable, délai dépassé) est
    traduite en `False` afin que la liveness ne dépende pas de la readiness.
    """
    from sqlalchemy import text

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - readiness ne doit pas faire échouer la liveness
        return False
    return True


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dépendance FastAPI : fournit une `AsyncSession` par requête.

    La session est systématiquement fermée en fin de requête. Les tâches
    ultérieures (contexte de tenant, audit) encapsuleront l'ouverture de
    transaction et le `SET LOCAL app.tenant_id`.
    """
    async with async_session_factory() as session:
        yield session
