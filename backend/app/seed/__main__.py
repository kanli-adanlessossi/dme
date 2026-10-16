"""Exécution de l'amorçage en module : `python -m app.seed`.

Amorce, dans l'ordre de dépendance :

1. **les 5 régions du Togo** et leur géométrie (tâche 6.2) — table de référence
   `region`, toujours peuplée au `docker compose up` (NFR-12) ;
2. **le jeu de démonstration dev-local** (tâche 13) — un `platform_admin` et un
   centre d'exemple par région (tenant + centre + tenant_admin) avec coordonnées
   togolaises valides (exigences 9.1, 9.2). Dev-only : no-op en production.

Appelé par `entrypoint.sh` après `alembic upgrade head`. Les deux étapes sont
idempotentes : relancer l'amorçage ne crée aucun doublon.

Important — **une seule boucle asyncio**. Les deux amorçages partagent le moteur
SQLAlchemy global (`app.core.db.engine`) et son pool de connexions asyncpg, lié à
la boucle d'événements courante. Les enchaîner dans un unique `asyncio.run`
(plutôt qu'un `asyncio.run` par étape) évite qu'une connexion du pool reste
rattachée à une boucle déjà fermée (« Event loop is closed ») à la seconde étape.
"""

from __future__ import annotations

import asyncio
import logging

from app.seed.centers import run_center_seed
from app.seed.regions import run_region_seed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _seed_all() -> None:
    """Amorce régions puis centres d'exemple dans la MÊME boucle asyncio."""
    region_count = await run_region_seed()
    logger.info("Amorçage des régions terminé : %d régions.", region_count)

    counts = await run_center_seed()
    logger.info(
        "Amorçage dev terminé : %d platform_admin, %d centre(s) d'exemple.",
        counts["platform_admins"],
        counts["centers"],
    )


def main() -> None:
    """Lance l'amorçage complet (régions + centres) dans une seule boucle asyncio."""
    asyncio.run(_seed_all())


if __name__ == "__main__":
    main()
