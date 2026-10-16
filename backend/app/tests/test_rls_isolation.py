"""Tests d'isolation RLS (tâche 4.4) — NFR-1, 1.2, 1.3, 5.2 ; vérifie P1 et P2.

Contrairement aux smoke tests de la tâche 4.3 (`test_tenant_context.py`), ces
tests sont des **tests d'intégration** : la Row-Level Security est une
fonctionnalité du moteur PostgreSQL et ne peut être vérifiée qu'avec une **vraie
base** PostgreSQL/PostGIS. Ils exécutent les migrations réelles (dont la 0003 qui
pose les politiques `tenant_isolation`), afin que ce qui est testé soit
exactement ce qui est livré.

Propriétés vérifiées (design, « Correctness Properties ») :

- **P1 — Isolation inter-tenant.** Dans le contexte du tenant A, un `SELECT` brut
  sur `medical_center` — même SANS clause ``WHERE tenant_id = …`` — ne renvoie que
  les lignes de A, jamais celles de B ; et un `UPDATE`/`DELETE` ne peut toucher
  les lignes de B (0 ligne affectée). Appliqué par le moteur, pas par le code.
- **P2 — Tenancy autoritaire côté serveur.** Dans le contexte du tenant A, un
  `INSERT` portant `tenant_id = B` (un `tenant_id` « forgé » ne correspondant pas
  au contexte) est **rejeté** par le ``WITH CHECK`` de la politique.
- **« Pas de contexte ⇒ aucune ligne. »** Sur une transaction fraîche sans
  ``app.tenant_id`` posé, ``current_setting('app.tenant_id', true)`` vaut NULL,
  le prédicat est faux, et un `SELECT` sur la table RLS ne renvoie **aucune
  ligne** (une connexion brute sans contexte est inoffensive).

Dispositif de test (conforme au design) :

- Les requêtes d'assertion qui exercent la RLS se connectent avec le rôle
  **non-superuser `dme_app`** (NOBYPASSRLS). C'est la seule façon de prouver que
  l'application ne peut pas contourner la RLS : le propriétaire du schéma, bien
  que soumis au FORCE RLS, détient d'autres privilèges ; on exerce donc le vrai
  rôle runtime.

- La migration 0002 crée `dme_app` en NOLOGIN sans mot de passe (jamais
  d'identifiant committé). Le **fixture de test** lui octroie LOGIN + un mot de
  passe **éphémère** (uniquement en mémoire du test, via la connexion
  propriétaire). Aucune credential n'est écrite sur disque ni committée.

- Le seed (régions, tenants, centres des deux tenants) utilise la connexion
  **propriétaire**. Comme le FORCE RLS s'applique aussi au propriétaire, les
  lignes cadrées au tenant sont insérées en posant ``app.tenant_id`` au tenant
  cible pendant l'insertion. `region`/`tenant` ne sont pas cadrés RLS et sont
  insérés directement.

Garde CI : ces tests sont **désactivés** (``pytest.skip``) si aucune base de test
n'est fournie/joignable (variable d'environnement ``TEST_DATABASE_URL``), afin que
la suite unitaire tourne sans Postgres. Lors de la vérification de la tâche, ils
sont exécutés contre un conteneur Postgres/PostGIS isolé et DOIVENT passer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

# --- Garde CI : base de test requise -------------------------------------------
#
# `TEST_DATABASE_URL` doit être une DSN SQLAlchemy async **propriétaire** du schéma
# (compte autorisé à exécuter les migrations et à gérer le rôle `dme_app`), p. ex.
#   postgresql+asyncpg://dme_owner:owner_test_pw@localhost:5433/dme_test
# Si absente, on saute tout le module : la suite unitaire reste exécutable sans
# Postgres (les tests RLS exigent un vrai moteur).
OWNER_URL = os.environ.get("TEST_DATABASE_URL")

# Deux marqueurs au niveau module :
# - `skipif` : désactive tout le module si aucune base de test (garde CI).
# - `asyncio(loop_scope="module")` : TOUS les tests partagent la MÊME boucle
#   d'événements que le fixture `app_engine` (lui aussi en loop_scope="module").
#   Sans cet alignement, le moteur/pool créé par le fixture serait lié à la
#   boucle (function-scope par défaut) du premier test, fermée ensuite, d'où les
#   erreurs « another operation is in progress » / « Event loop is closed » sur
#   les tests suivants.
pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : tests d'intégration RLS désactivés "
        "(nécessitent un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

# Mot de passe **éphémère** octroyé au rôle `dme_app` pour la durée des tests.
# Il n'est jamais committé comme credential applicative : la migration 0002 crée
# `dme_app` NOLOGIN sans mot de passe ; ce fixture lui donne LOGIN + ce mot de
# passe uniquement pour permettre une connexion runtime non-superuser pendant le
# test, prouvant que la RLS ne peut être contournée.
_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "rls_test_" + uuid.uuid4().hex


def _app_role_url(owner_url: str, password: str) -> str:
    """Dérive la DSN du rôle `dme_app` depuis la DSN propriétaire.

    Remplace user:password par ``dme_app:<password>`` en conservant hôte, port et
    base. Évite d'avoir à configurer une seconde variable d'environnement.
    """
    parts = urlsplit(owner_url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{_APP_ROLE}:{password}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run_migrations(owner_url: str) -> None:
    """Exécute `alembic upgrade head` contre la base de test (connexion owner).

    Lancé en **sous-processus** et non via l'API de commande Alembic : `env.py`
    appelle ``asyncio.run()``, incompatible avec la boucle d'événements déjà
    active sous pytest-asyncio (``asyncio.run() cannot be called from a running
    event loop``). Le sous-processus a sa propre boucle, et c'est exactement le
    chemin de migration livré (l'entrypoint du conteneur lance `alembic upgrade
    head`).

    On pointe `env.py` (qui lit `get_settings().owner_database_url`) vers la base
    de test en surchargeant les variables `POSTGRES_*` dans l'environnement du
    sous-processus. Les migrations tournent en compte **propriétaire** (owner),
    jamais via le rôle applicatif `dme_app`.
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
            # Pas de rôle applicatif pour les migrations : elles tournent en owner.
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


# Identifiants fixes des deux tenants + leurs centres, pour des assertions nettes.
TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
CENTER_A = uuid.uuid4()
CENTER_B = uuid.uuid4()


async def _seed(owner_conn: AsyncConnection) -> None:
    """Amorce une région, deux tenants et un centre par tenant (connexion owner).

    `region`/`tenant` ne sont pas cadrés RLS → insertion directe. `medical_center`
    l'est (RLS forcée, y compris pour l'owner) → on pose `app.tenant_id` sur le
    tenant cible avant chaque insertion pour satisfaire le ``WITH CHECK``.
    """
    region_id = uuid.uuid4()
    await owner_conn.execute(
        text("INSERT INTO region (id, code, name_fr) VALUES (:id, :code, :name)"),
        {"id": region_id, "code": f"TEST-{uuid.uuid4().hex[:6]}", "name": "Région test"},
    )
    for tenant_id, name in ((TENANT_A, "Tenant A"), (TENANT_B, "Tenant B")):
        await owner_conn.execute(
            text(
                "INSERT INTO tenant (id, name, region_id, status) "
                "VALUES (:id, :name, :region_id, 'active')"
            ),
            {"id": tenant_id, "name": name, "region_id": region_id},
        )

    # medical_center est sous RLS forcée : poser le contexte par tenant à l'insert.
    for tenant_id, center_id, name in (
        (TENANT_A, CENTER_A, "Centre A"),
        (TENANT_B, CENTER_B, "Centre B"),
    ):
        await owner_conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, false)"),
            {"tid": str(tenant_id)},
        )
        await owner_conn.execute(
            text(
                "INSERT INTO medical_center (id, tenant_id, name, region_id) "
                "VALUES (:id, :tenant_id, :name, :region_id)"
            ),
            {
                "id": center_id,
                "tenant_id": tenant_id,
                "name": name,
                "region_id": region_id,
            },
        )
    # Pas besoin de réinitialiser `app.tenant_id` ici : le seed tourne sur le
    # moteur owner en NullPool (connexion jetée après usage) et les assertions
    # utilisent un moteur `dme_app` distinct, lui aussi en NullPool (connexions
    # neuves, aucun GUC hérité). NB : poser `''` ici serait même nuisible car
    # ``''::uuid`` lève une erreur — on laisse donc le GUC simplement non posé.


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def app_engine() -> AsyncIterator[object]:
    """Prépare la base de test et expose un moteur connecté en rôle `dme_app`.

    Étapes (toutes en connexion propriétaire sauf le moteur exposé) :
    1. migrations `alembic upgrade head` (crée les tables, le rôle `dme_app`
       NOLOGIN, et les politiques RLS de la 0003) ;
    2. octroi LOGIN + mot de passe éphémère au rôle `dme_app` ;
    3. seed d'une région, deux tenants et un centre par tenant ;
    4. yield d'un moteur async connecté en `dme_app` (NOBYPASSRLS) pour les
       assertions ;
    5. nettoyage : révocation du LOGIN, suppression des données de seed.
    """
    assert OWNER_URL is not None
    _run_migrations(OWNER_URL)

    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    # 2. Donner LOGIN + mot de passe éphémère à dme_app.
    #    `ALTER ROLE … PASSWORD` n'accepte PAS de paramètre lié (comme SET LOCAL) ;
    #    on injecte donc le mot de passe comme littéral SQL. Il est sûr : généré
    #    par le test (`rls_test_` + hex), aucun caractère à échapper, mais on
    #    double tout de même les apostrophes par précaution.
    pw_literal = "'" + _APP_TEST_PASSWORD.replace("'", "''") + "'"
    async with owner_engine.begin() as conn:
        await conn.execute(text(f"ALTER ROLE {_APP_ROLE} LOGIN PASSWORD {pw_literal}"))
    # 3. Seed.
    async with owner_engine.begin() as conn:
        await _seed(conn)

    app_url = _app_role_url(OWNER_URL, _APP_TEST_PASSWORD)
    # NullPool : chaque `connect()` ouvre une connexion physique neuve, sans état
    # de session hérité (aucun `app.tenant_id` résiduel d'un test précédent). Cela
    # rend le cas « sans contexte » déterministe (fresh ⇒ GUC jamais posé ⇒ NULL)
    # et évite toute fuite d'état entre tests via le pool.
    engine = create_async_engine(app_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()
        # 5. Nettoyage : retirer les données de seed et le LOGIN du rôle.
        async with owner_engine.begin() as conn:
            for tenant_id in (TENANT_A, TENANT_B):
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, false)"),
                    {"tid": str(tenant_id)},
                )
                await conn.execute(
                    text("DELETE FROM medical_center WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
            # `tenant` n'est pas cadré RLS : suppression directe (le GUC posé
            # ci-dessus pour les DELETE sur `medical_center` est sans effet ici).
            await conn.execute(
                text("DELETE FROM tenant WHERE id = ANY(:ids)"),
                {"ids": [TENANT_A, TENANT_B]},
            )
            await conn.execute(text(f"ALTER ROLE {_APP_ROLE} NOLOGIN PASSWORD NULL"))
        await owner_engine.dispose()


async def _set_context(conn: AsyncConnection, tenant_id: uuid.UUID) -> None:
    """Pose `app.tenant_id` (transaction-local) sur la connexion donnée."""
    await conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


@asynccontextmanager
async def _tenant_conn(
    engine: object, tenant_id: uuid.UUID | None
) -> AsyncIterator[AsyncConnection]:
    """Ouvre une connexion `dme_app` + une transaction, et pose le contexte tenant.

    Factorise le motif « connect() + begin() + set_config » répété par les tests
    (une seule entrée ``async with``, pas d'imbrication). Si ``tenant_id`` est
    ``None``, AUCUN contexte n'est posé — c'est le cas « sans contexte » servant à
    vérifier qu'une connexion brute ne voit/écrit aucune ligne.
    """
    async with engine.connect() as conn, conn.begin():  # type: ignore[attr-defined]
        if tenant_id is not None:
            await _set_context(conn, tenant_id)
        yield conn


# --- P1 : isolation en LECTURE -------------------------------------------------


async def test_p1_lecture_contexte_A_ne_voit_que_A(app_engine: object) -> None:
    """Dans le contexte A, un SELECT brut (sans WHERE) ne renvoie que les lignes A.

    Vérifie P1 (lecture) : l'absence volontaire de ``WHERE tenant_id = …`` prouve
    que c'est la RLS — et non le code — qui filtre.
    """
    async with _tenant_conn(app_engine, TENANT_A) as conn:
        rows = (await conn.execute(text("SELECT id, tenant_id FROM medical_center"))).all()
    returned_ids = {row[0] for row in rows}
    returned_tenants = {row[1] for row in rows}
    assert returned_ids == {CENTER_A}, "Le contexte A ne doit voir que le centre A"
    assert returned_tenants == {TENANT_A}
    assert CENTER_B not in returned_ids, "Le centre B ne doit jamais fuiter vers A"


async def test_p1_lecture_contexte_B_ne_voit_que_B(app_engine: object) -> None:
    """Symétrie : dans le contexte B, seul le centre B est visible (P1 lecture)."""
    async with _tenant_conn(app_engine, TENANT_B) as conn:
        rows = (await conn.execute(text("SELECT id FROM medical_center"))).all()
    returned_ids = {row[0] for row in rows}
    assert returned_ids == {CENTER_B}
    assert CENTER_A not in returned_ids


# --- P1 : isolation en ÉCRITURE (UPDATE / DELETE) ------------------------------


async def test_p1_update_ne_touche_pas_les_lignes_de_B(app_engine: object) -> None:
    """Dans le contexte A, un UPDATE ciblant le centre B n'affecte aucune ligne.

    Vérifie P1 (écriture) : la ligne de B est invisible (USING), donc l'UPDATE
    porte sur 0 ligne — A ne peut pas modifier les données de B.
    """
    async with _tenant_conn(app_engine, TENANT_A) as conn:
        result = await conn.execute(
            text("UPDATE medical_center SET name = 'piraté' WHERE id = :id"),
            {"id": CENTER_B},
        )
        assert result.rowcount == 0, "A ne doit pouvoir modifier aucune ligne de B"

    # Contre-vérification : le nom de B est intact (lu dans le contexte de B).
    async with _tenant_conn(app_engine, TENANT_B) as conn:
        name = (
            await conn.execute(
                text("SELECT name FROM medical_center WHERE id = :id"),
                {"id": CENTER_B},
            )
        ).scalar_one()
    assert name == "Centre B", "La ligne de B ne doit pas avoir été modifiée"


async def test_p1_delete_ne_touche_pas_les_lignes_de_B(app_engine: object) -> None:
    """Dans le contexte A, un DELETE ciblant le centre B n'affecte aucune ligne."""
    async with _tenant_conn(app_engine, TENANT_A) as conn:
        result = await conn.execute(
            text("DELETE FROM medical_center WHERE id = :id"),
            {"id": CENTER_B},
        )
        assert result.rowcount == 0, "A ne doit pouvoir supprimer aucune ligne de B"

    # La ligne de B existe toujours (vérifié dans le contexte de B).
    async with _tenant_conn(app_engine, TENANT_B) as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM medical_center WHERE id = :id"),
                {"id": CENTER_B},
            )
        ).scalar_one()
    assert count == 1, "La ligne de B doit toujours exister"


# --- P2 : tenancy autoritaire — INSERT d'un tenant_id forgé rejeté --------------


# Note : `medical_center` porte un ``UNIQUE(tenant_id)`` (1:1 avec le tenant en
# v1), on ne peut donc pas y insérer une 2ᵉ ligne pour le même tenant. Les tests
# d'INSERT (P2) utilisent `app_user`, également cadré RLS mais autorisant
# plusieurs lignes par tenant — le ``WITH CHECK`` y est identique.


async def test_p2_insert_tenant_id_forge_est_rejete(app_engine: object) -> None:
    """Dans le contexte A, insérer une ligne avec tenant_id = B est rejeté (P2).

    Le ``WITH CHECK`` de la politique exige ``tenant_id = app.tenant_id`` ; une
    valeur forgée (B ≠ A) viole la contrainte et PostgreSQL lève une erreur de
    politique RLS. On insère dans `app_user` (cadré RLS) un enregistrement dont
    le `tenant_id` est celui de B alors que le contexte est A : rejet attendu.
    """
    forged_id = uuid.uuid4()
    forged_email = f"forge-{uuid.uuid4().hex}@test.local"

    with pytest.raises((DBAPIError, ProgrammingError)) as exc_info:
        async with _tenant_conn(app_engine, TENANT_A) as conn:
            await conn.execute(
                text(
                    "INSERT INTO app_user (id, tenant_id, email, role) "
                    "VALUES (:id, :tenant_id, :email, 'staff')"
                ),
                {
                    "id": forged_id,
                    "tenant_id": TENANT_B,  # forgé : ne correspond pas au contexte A
                    "email": forged_email,
                },
            )
    # L'erreur PostgreSQL mentionne la politique RLS (row-level security).
    assert "row-level security" in str(exc_info.value).lower()

    # La ligne forgée n'existe nulle part (ni en A, ni en B).
    for tenant_id in (TENANT_A, TENANT_B):
        async with _tenant_conn(app_engine, tenant_id) as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM app_user WHERE id = :id"),
                    {"id": forged_id},
                )
            ).scalar_one()
        assert count == 0, "La ligne forgée ne doit jamais avoir été écrite"


async def test_p2_insert_tenant_id_correct_est_accepte(app_engine: object) -> None:
    """Contrôle positif : dans le contexte A, insérer avec tenant_id = A réussit.

    Garantit que le rejet du cas forgé vient bien du WITH CHECK (et non d'un
    manque de privilèges ou d'une autre contrainte) : la même insertion avec le
    bon tenant_id passe. On nettoie ensuite la ligne de contrôle.
    """
    good_id = uuid.uuid4()
    good_email = f"ok-{uuid.uuid4().hex}@test.local"
    async with _tenant_conn(app_engine, TENANT_A) as conn:
        await conn.execute(
            text(
                "INSERT INTO app_user (id, tenant_id, email, role) "
                "VALUES (:id, :tenant_id, :email, 'staff')"
            ),
            {"id": good_id, "tenant_id": TENANT_A, "email": good_email},
        )
        count = (
            await conn.execute(
                text("SELECT count(*) FROM app_user WHERE id = :id"),
                {"id": good_id},
            )
        ).scalar_one()
    assert count == 1, "Un INSERT avec le bon tenant_id doit réussir"

    # Nettoyage de la ligne de contrôle pour ne pas polluer les autres tests.
    async with _tenant_conn(app_engine, TENANT_A) as conn:
        await conn.execute(
            text("DELETE FROM app_user WHERE id = :id"),
            {"id": good_id},
        )


# --- « Pas de contexte ⇒ aucune ligne » ----------------------------------------


async def test_sans_contexte_aucune_ligne(app_engine: object) -> None:
    """Sans `app.tenant_id` posé, un SELECT sur une table RLS ne renvoie rien.

    ``current_setting('app.tenant_id', true)`` vaut NULL → le prédicat USING est
    faux pour toutes les lignes → zéro ligne. Une connexion brute sans contexte
    est donc inoffensive (ne divulgue aucune donnée).
    """
    # tenant_id=None ⇒ aucun set_config : contexte volontairement absent.
    async with _tenant_conn(app_engine, None) as conn:
        rows = (await conn.execute(text("SELECT id FROM medical_center"))).all()
    assert rows == [], "Sans contexte de tenant, aucune ligne ne doit être visible"


async def test_sans_contexte_insert_rejete(app_engine: object) -> None:
    """Sans contexte, un INSERT est aussi rejeté (WITH CHECK contre NULL).

    Renforce « pas de contexte ⇒ inoffensif » côté écriture : sans
    ``app.tenant_id`` posé, ``current_setting('app.tenant_id', true)`` vaut NULL,
    ``tenant_id = NULL`` est faux, le WITH CHECK échoue, l'INSERT est refusé —
    même avec un `tenant_id` par ailleurs valide.
    """
    orphan_id = uuid.uuid4()
    orphan_email = f"orphan-{uuid.uuid4().hex}@test.local"

    with pytest.raises((DBAPIError, ProgrammingError)) as exc_info:
        # tenant_id=None ⇒ aucun set_config : contexte volontairement absent.
        async with _tenant_conn(app_engine, None) as conn:
            await conn.execute(
                text(
                    "INSERT INTO app_user (id, tenant_id, email, role) "
                    "VALUES (:id, :tenant_id, :email, 'staff')"
                ),
                {"id": orphan_id, "tenant_id": TENANT_A, "email": orphan_email},
            )
    assert "row-level security" in str(exc_info.value).lower()
