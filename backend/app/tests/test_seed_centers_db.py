"""Test d'intégration de l'amorçage dev-local (tâche 13) — REQ-9.1, 9.2.

L'écriture des centres d'exemple traverse la **RLS forcée** (`medical_center`,
`app_user`) et pose directement un `password_hash` : des comportements du **moteur
PostgreSQL** qui ne peuvent être vérifiés qu'avec une **vraie base**. Comme
`test_seed_regions_db.py` / `test_admin_lifecycle_db.py`, ce module exécute les
migrations réelles (en sous-processus), amorce les régions puis joue le chemin de
code livré (`seed_centers`) via le compte propriétaire (non-BYPASSRLS : la RLS
s'applique, le contexte de tenant posé par l'amorçage est donc indispensable).

Vérifications :

- après amorçage : **un `platform_admin`** (tenant NUL) et **un centre d'exemple
  par région** (5 tenants actifs + 5 `medical_center` + 5 `tenant_admin`) ;
- chaque centre porte des **coordonnées dans la frontière du Togo** (`ST_Within`
  contre l'union des régions amorcées, exigence 9.2) ;
- **idempotence** : relancer l'amorçage ne crée aucun doublon (toujours 1
  platform_admin, 5 centres) et ne lève pas.

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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.seed.centers import (
    _PLATFORM_ADMIN_EMAIL,
    SAMPLE_CENTERS,
    seed_centers,
)
from app.seed.regions import CANONICAL_REGION_CODES, seed_regions

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : test d'intégration de l'amorçage "
        "dev-local désactivé (nécessite un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

_ADMIN_EMAILS = [c.admin_email for c in SAMPLE_CENTERS]
_ALL_SEED_EMAILS = [_PLATFORM_ADMIN_EMAIL, *_ADMIN_EMAILS]


def _run_migrations(owner_url: str) -> None:
    """Exécute `alembic upgrade head` (sous-processus, connexion owner)."""
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
    """Migre la base, amorce les régions (prérequis), expose le moteur propriétaire.

    Nettoie en fin de module les lignes amorcées (comptes, centres, tenants, puis
    régions) afin de laisser la base propre. Les tables cadrées RLS sont purgées en
    posant `app.tenant_id` sur chaque tenant amorcé.
    """
    assert OWNER_URL is not None
    _run_migrations(OWNER_URL)

    engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    # Prérequis : régions amorcées (FK tenant/center + validation géo ST_Within).
    async with engine.connect() as conn, conn.begin():
        await seed_regions(conn)
    try:
        yield engine
    finally:
        async with engine.connect() as conn, conn.begin():
            tenant_rows = (
                await conn.execute(
                    text("SELECT id FROM tenant WHERE name = ANY(:names)"),
                    {"names": [c.center_name for c in SAMPLE_CENTERS]},
                )
            ).all()
            for (tid,) in tenant_rows:
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(tid)},
                )
                await conn.execute(
                    text("DELETE FROM app_user WHERE tenant_id = :tid"), {"tid": tid}
                )
                await conn.execute(
                    text("DELETE FROM medical_center WHERE tenant_id = :tid"),
                    {"tid": tid},
                )
            # platform_admin (tenant NUL) : supprimé par e-mail (hors contexte).
            await conn.execute(
                text("DELETE FROM app_user WHERE email = ANY(:emails)"),
                {"emails": _ALL_SEED_EMAILS},
            )
            await conn.execute(
                text("DELETE FROM tenant WHERE name = ANY(:names)"),
                {"names": [c.center_name for c in SAMPLE_CENTERS]},
            )
            await conn.execute(
                text("DELETE FROM region WHERE code = ANY(:codes)"),
                {"codes": sorted(CANONICAL_REGION_CODES)},
            )
        await engine.dispose()


async def _seed_once(engine: object) -> dict[str, int]:
    """Joue l'amorçage dev une fois dans sa propre transaction (chemin livré).

    `seed_centers` écrit via l'ORM (`session.add`) ; on l'exerce donc avec une
    vraie `AsyncSession` bornée au moteur propriétaire — exactement le type de
    session que `run_center_seed` ouvre au démarrage.
    """
    session_factory = async_sessionmaker(
        bind=engine,  # type: ignore[arg-type]
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session, session.begin():
        return await seed_centers(session)


async def test_amorcage_cree_platform_admin_et_un_centre_par_region(
    owner_engine: object,
) -> None:
    """Après amorçage : 1 platform_admin + 1 centre d'exemple par région (5)."""
    counts = await _seed_once(owner_engine)
    assert counts["platform_admins"] == 1
    assert counts["centers"] == 5

    async with owner_engine.connect() as conn:  # type: ignore[attr-defined]
        # platform_admin : exactement un, tenant NUL.
        admin_rows = (
            await conn.execute(
                text(
                    "SELECT tenant_id, role, is_active FROM app_user "
                    "WHERE email = :email"
                ),
                {"email": _PLATFORM_ADMIN_EMAIL},
            )
        ).all()
        assert len(admin_rows) == 1
        tenant_id, role, is_active = admin_rows[0]
        assert tenant_id is None, "Le platform_admin a un tenant_id NUL"
        assert role == "platform_admin"
        assert is_active is True

        # Un tenant actif par centre d'exemple.
        tenant_count = (
            await conn.execute(
                text("SELECT count(*) FROM tenant WHERE name = ANY(:names)"),
                {"names": [c.center_name for c in SAMPLE_CENTERS]},
            )
        ).scalar_one()
        assert tenant_count == 5

        # Cinq tenant_admin amorcés.
        admin_user_count = (
            await conn.execute(
                text("SELECT count(*) FROM app_user WHERE email = ANY(:emails)"),
                {"emails": _ADMIN_EMAILS},
            )
        ).scalar_one()
        assert admin_user_count == 5


async def test_centres_ont_des_coordonnees_dans_le_togo(owner_engine: object) -> None:
    """Chaque centre d'exemple tombe dans la frontière du Togo (ST_Within, 9.2)."""
    await _seed_once(owner_engine)

    async with owner_engine.connect() as conn:  # type: ignore[attr-defined]
        # Union des régions amorcées = frontière du pays ; tout centre doit y être.
        outside = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM medical_center mc "
                    "WHERE mc.name = ANY(:names) AND NOT ST_Within("
                    "  mc.location::geometry, "
                    "  (SELECT ST_Union(geometry) FROM region WHERE geometry IS NOT NULL)"
                    ")"
                ),
                {"names": [c.center_name for c in SAMPLE_CENTERS]},
            )
        ).scalar_one()
    assert outside == 0, "Tous les centres d'exemple doivent être dans le Togo"


async def test_amorcage_est_idempotent(owner_engine: object) -> None:
    """Relancer l'amorçage ne crée aucun doublon et renvoie des décomptes nuls."""
    # Premier passage garanti par les tests précédents ; on rejoue deux fois.
    second = await _seed_once(owner_engine)
    third = await _seed_once(owner_engine)
    # Rien de nouveau créé (déjà présents) : décomptes à zéro.
    assert second == {"platform_admins": 0, "centers": 0}
    assert third == {"platform_admins": 0, "centers": 0}

    async with owner_engine.connect() as conn:  # type: ignore[attr-defined]
        admin_count = (
            await conn.execute(
                text("SELECT count(*) FROM app_user WHERE email = :email"),
                {"email": _PLATFORM_ADMIN_EMAIL},
            )
        ).scalar_one()
        tenant_count = (
            await conn.execute(
                text("SELECT count(*) FROM tenant WHERE name = ANY(:names)"),
                {"names": [c.center_name for c in SAMPLE_CENTERS]},
            )
        ).scalar_one()
    assert admin_count == 1, "Un seul platform_admin après ré-amorçage (idempotent)"
    assert tenant_count == 5, "Toujours 5 centres après ré-amorçage (idempotent)"
