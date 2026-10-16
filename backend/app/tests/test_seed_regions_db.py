"""Test d'intégration de l'amorçage des régions (tâche 6.2) — REQ-7.6, 9.2.

La conversion `Polygon → MultiPolygon` en SRID 4326 et l'idempotence de l'upsert
sont des comportements du **moteur PostGIS** : ils ne peuvent être vérifiés
qu'avec une **vraie base**. Comme `test_rls_isolation.py` / `test_audit_log.py`,
ce module exécute les migrations réelles (en sous-processus) puis amorce via le
chemin de code livré (`seed_regions`).

Vérifications :

- après amorçage, il existe **exactement 5 régions** avec les codes canoniques
  attendus et des `name_fr` non nuls ;
- chaque géométrie est une **MultiPolygon valide en SRID 4326**, non nulle
  (`ST_SRID = 4326`, `GeometryType = 'MULTIPOLYGON'`, `ST_IsValid`), condition
  pour que `ST_Within` (validation géo, tâche 11) fonctionne ensuite ;
- **idempotence** : relancer l'amorçage ne crée pas de 6ᵉ ligne et ne lève pas
  d'erreur (toujours 5 régions, mêmes codes).

Garde CI : désactivé si `TEST_DATABASE_URL` est absente (la suite unitaire tourne
sans Postgres). Exécuté contre un conteneur PostGIS lors de la vérification.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from app.seed.regions import CANONICAL_REGION_CODES, seed_regions

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : test d'intégration d'amorçage des "
        "régions désactivé (nécessite un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


def _run_migrations(owner_url: str) -> None:
    """Exécute `alembic upgrade head` (sous-processus, connexion owner).

    Même approche que les autres tests d'intégration : `env.py` appelle
    `asyncio.run()`, incompatible avec la boucle pytest-asyncio ; le sous-processus
    a sa propre boucle et c'est le chemin de migration réellement livré.
    """
    parts = urlsplit(owner_url)
    env = dict(os.environ)
    env.update(
        {
            "POSTGRES_HOST": parts.hostname or "localhost",
            "POSTGRES_PORT": str(parts.port or 5432),
            "POSTGRES_DB": (parts.path or "/").lstrip("/"),
            "POSTGRES_USER": parts.username or "",
            "POSTGRES_PASSWORD": parts.password or "",
            "APP_DB_USER": "",
            "APP_DB_PASSWORD": "",
        }
    )
    backend_dir = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Échec de `alembic upgrade head` sur la base de test :\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def owner_engine() -> AsyncIterator[object]:
    """Migre la base de test et expose un moteur connecté en compte propriétaire.

    `region` étant une table de référence SANS RLS, l'amorçage se fait avec le
    compte propriétaire (celui qui exécute aussi les migrations). On nettoie les
    lignes de régions insérées en fin de module.
    """
    assert OWNER_URL is not None
    _run_migrations(OWNER_URL)

    engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    try:
        yield engine
    finally:
        # Nettoyage : retirer les régions amorcées pour laisser la base propre.
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM region WHERE code = ANY(:codes)"),
                {"codes": sorted(CANONICAL_REGION_CODES)},
            )
        await engine.dispose()


async def _seed_once(engine: object) -> int:
    """Joue l'amorçage une fois dans sa propre transaction (chemin livré)."""
    async with engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        return await seed_regions(conn)


async def _region_rows(conn: AsyncConnection) -> list[tuple]:
    """Renvoie (code, name_fr, srid, geomtype, valid) pour les régions amorcées."""
    result = await conn.execute(
        text(
            "SELECT code, name_fr, ST_SRID(geometry) AS srid, "
            "GeometryType(geometry) AS geomtype, ST_IsValid(geometry) AS valid "
            "FROM region WHERE code = ANY(:codes) ORDER BY code"
        ),
        {"codes": sorted(CANONICAL_REGION_CODES)},
    )
    return list(result.all())


async def test_amorcage_insere_5_regions_multipolygon_4326(owner_engine: object) -> None:
    """Après amorçage : 5 régions, géométrie MultiPolygon valide en SRID 4326."""
    inserted = await _seed_once(owner_engine)
    assert inserted == 5

    async with owner_engine.connect() as conn:  # type: ignore[attr-defined]
        rows = await _region_rows(conn)

    assert {row[0] for row in rows} == set(CANONICAL_REGION_CODES)
    for code, name_fr, srid, geomtype, valid in rows:
        assert name_fr, f"{code} doit avoir un name_fr non nul"
        assert srid == 4326, f"{code} doit être en SRID 4326 (reçu {srid})"
        assert geomtype == "MULTIPOLYGON", (
            f"{code} doit être une MULTIPOLYGON (reçu {geomtype})"
        )
        assert valid, f"la géométrie de {code} doit être valide (ST_IsValid)"


async def test_amorcage_est_idempotent(owner_engine: object) -> None:
    """Relancer l'amorçage ne crée pas de doublon et ne lève pas d'erreur."""
    # Premier passage (peut déjà avoir eu lieu dans l'autre test selon l'ordre) :
    # on rejoue deux fois de plus et on vérifie l'invariant « exactement 5 ».
    await _seed_once(owner_engine)
    await _seed_once(owner_engine)

    async with owner_engine.connect() as conn:  # type: ignore[attr-defined]
        count = (
            await conn.execute(
                text("SELECT count(*) FROM region WHERE code = ANY(:codes)"),
                {"codes": sorted(CANONICAL_REGION_CODES)},
            )
        ).scalar_one()
    assert count == 5, "L'amorçage doit rester idempotent (exactement 5 régions)"
