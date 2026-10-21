"""Tests d'intégration du lookup setup-token « selector + verifier » (tâche 3.1).

Spec `tenant-data-rights`, observation #8 — ADR-2. Exigences 3 (critères 2, 5),
4 (critère 3) ; NFR-2.

Contrairement aux tests unitaires de `test_auth_service.py` (session factice), ce
module exerce le **vrai moteur PostgreSQL avec RLS forcée** via le rôle applicatif
non-superuser `dme_app`. C'est la seule façon de prouver que le nouveau lookup
`consume_setup_token` fonctionne réellement sur la colonne **indexée**
`app_user.setup_token_selector` (index UNIQUE partiel, migration 0011) et sous le
chemin cadré au tenant, tout en conservant les garanties de sécurité existantes.

Propriétés vérifiées (design, « Correctness Properties » P4/P5 ; exigences 3.2,
3.5, 4.3) :

- **Consommation réussie à coût constant.** Un jeton valide fixe `password_hash`
  (Argon2id) et consomme le jeton (selector + hash + échéance effacés). Avec
  plusieurs comptes porteurs d'un setup-token en base, **une seule** vérification
  Argon2id a lieu (lookup par selector indexé, pas O(n)).
- **Usage unique.** Un second appel avec le même jeton est rejeté (le selector a
  été effacé à la première consommation).
- **Expiration.** Un jeton dont l'échéance est passée est rejeté, sans être
  consommé.
- **Selector inconnu → même erreur générique (P3).** Un selector absent et un
  verifier faux lèvent la MÊME `SetupTokenError` (non-fuite d'existence).
- **Token mal formé → même erreur générique.** Un jeton sans `.`, à selector vide
  ou à verifier vide, est rejeté par la même erreur générique.
- **Token hérité sans selector.** Un compte dont `setup_token_selector IS NULL`
  (jeton émis avant la migration selector+verifier) n'est plus consommable : il
  est rejeté proprement (invalidation assumée, ADR-2).
- **Anti-timing (P3).** Pour un selector inexistant (et pour un format invalide),
  une vérification Argon2id **factice** est exécutée — niveler le temps de réponse
  afin qu'un selector inexistant ne soit pas distinguable par le temps.

Dispositif (identique à `test_rls_isolation.py` / `test_admin_lifecycle_db.py`) :
migrations réelles en sous-processus, octroi LOGIN éphémère à `dme_app`, moteur
`dme_app` injecté (NOBYPASSRLS), amorçage d'une région dédiée. Désactivé si
`TEST_DATABASE_URL` est absente.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import security
from app.core.security import generate_setup_token, verify_password
from app.core.tenant_context import set_tenant_context
from app.models.app_user import AppUser
from app.services.auth import SetupTokenError, consume_setup_token

OWNER_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not OWNER_URL,
        reason="TEST_DATABASE_URL non définie : tests d'intégration du lookup "
        "setup-token désactivés (nécessitent un vrai PostgreSQL/PostGIS).",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

_APP_ROLE = "dme_app"
_APP_TEST_PASSWORD = "setup_token_selector_test_" + uuid.uuid4().hex

# Région + tenant dédiés (région/tenant ne sont pas cadrés RLS).
_REGION_ID = uuid.uuid4()
_REGION_CODE = "SETUPTOK-" + uuid.uuid4().hex[:6]
_TENANT_ID = uuid.uuid4()

_NEW_PASSWORD = "un-nouveau-mot-de-passe-robuste-123"


def _app_role_url(owner_url: str, password: str) -> str:
    parts = urlsplit(owner_url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{_APP_ROLE}:{password}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run_migrations(owner_url: str) -> None:
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
async def app_engine() -> AsyncIterator[object]:
    """Prépare la base, octroie LOGIN à dme_app, amorce région + tenant, yield moteur."""
    assert OWNER_URL is not None
    _run_migrations(OWNER_URL)

    owner_engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    pw_literal = "'" + _APP_TEST_PASSWORD.replace("'", "''") + "'"
    async with owner_engine.begin() as conn:
        await conn.execute(text(f"ALTER ROLE {_APP_ROLE} LOGIN PASSWORD {pw_literal}"))
        await conn.execute(
            text("INSERT INTO region (id, code, name_fr) VALUES (:id, :code, :name)"),
            {"id": _REGION_ID, "code": _REGION_CODE, "name": "Région test setup-token"},
        )
        # `tenant` n'est pas cadré RLS : insertion directe.
        await conn.execute(
            text(
                "INSERT INTO tenant (id, name, region_id, status) "
                "VALUES (:id, :name, :rid, 'active')"
            ),
            {"id": _TENANT_ID, "name": "Tenant setup-token", "rid": _REGION_ID},
        )

    app_url = _app_role_url(OWNER_URL, _APP_TEST_PASSWORD)
    engine = create_async_engine(app_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()
        async with owner_engine.begin() as conn:
            # `app_user` est sous RLS forcée : poser le contexte du tenant pour purger.
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, false)"),
                {"tid": str(_TENANT_ID)},
            )
            await conn.execute(
                text("DELETE FROM app_user WHERE tenant_id = :tid"), {"tid": _TENANT_ID}
            )
            await conn.execute(
                text("DELETE FROM tenant WHERE id = :tid"), {"tid": _TENANT_ID}
            )
            await conn.execute(
                text("DELETE FROM region WHERE id = :rid"), {"rid": _REGION_ID}
            )
            await conn.execute(text(f"ALTER ROLE {_APP_ROLE} NOLOGIN PASSWORD NULL"))
        await owner_engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def session_factory(app_engine: object) -> async_sessionmaker:
    """Fabrique de sessions `dme_app` (RLS forcée) pour les tests."""
    return async_sessionmaker(
        bind=app_engine,  # type: ignore[arg-type]
        expire_on_commit=False,
        autoflush=False,
    )


async def _insert_setup_user(
    session_factory: async_sessionmaker,
    *,
    selector: str | None,
    verifier_hash: str | None,
    expires_at: datetime | None,
    is_active: bool = True,
) -> uuid.UUID:
    """Crée un `app_user` en attente de configuration (contexte tenant posé).

    Renvoie l'id du compte créé. `app_user` étant sous RLS forcée, on pose
    `app.tenant_id` sur le tenant cible avant l'INSERT (chemin admin cadré).
    """
    user_id = uuid.uuid4()
    email = f"setup-{uuid.uuid4().hex}@centre.tg"
    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        session.add(
            AppUser(
                id=user_id,
                tenant_id=_TENANT_ID,
                email=email,
                password_hash=None,
                setup_token_selector=selector,
                setup_token_hash=verifier_hash,
                setup_token_expires_at=expires_at,
                role="tenant_admin",
                is_active=is_active,
            )
        )
    return user_id


async def _load_user(
    session_factory: async_sessionmaker, user_id: uuid.UUID
) -> AppUser:
    """Recharge un `app_user` par id sous contexte tenant (RLS forcée)."""
    from sqlalchemy import select

    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        result = await session.execute(select(AppUser).where(AppUser.id == user_id))
        return result.scalars().one()


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


class _Argon2Counter:
    """Compteur d'appels à la vérification Argon2id (verifier du setup-token).

    Instrumente `app.services.auth.verify_setup_token` pour prouver qu'au plus UNE
    vérification Argon2 a lieu par consommation (P4/NFR-2), indépendamment du nombre
    de comptes candidats présents en base. Enveloppe l'implémentation réelle (pas un
    mock : la vraie vérification est bien exécutée).
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        self._real = security.verify_setup_token

        def _wrapped(verifier: str, setup_token_hash: str | None) -> bool:
            self.count += 1
            return self._real(verifier, setup_token_hash)

        # Patch au point d'usage : `auth.py` fait `from ...security import
        # verify_setup_token`, donc le nom est résolu dans le module `auth`.
        monkeypatch.setattr("app.services.auth.verify_setup_token", _wrapped)


async def test_consommation_reussie_a_cout_constant(
    session_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jeton valide → mot de passe fixé, jeton consommé, UNE SEULE vérif Argon2.

    Plusieurs comptes porteurs d'un setup-token coexistent en base : si le lookup
    était resté O(n), plusieurs vérifications Argon2 auraient lieu. Le selector
    indexé garantit au plus une vérification (P4/NFR-2).
    """
    # Comptes « bruit » : d'autres setup-tokens actifs, non ciblés par le jeton.
    for _ in range(3):
        other = generate_setup_token()
        await _insert_setup_user(
            session_factory,
            selector=other.selector,
            verifier_hash=other.verifier_hash,
            expires_at=_future(),
        )

    generated = generate_setup_token()
    user_id = await _insert_setup_user(
        session_factory,
        selector=generated.selector,
        verifier_hash=generated.verifier_hash,
        expires_at=_future(),
    )

    counter = _Argon2Counter(monkeypatch)
    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        user = await consume_setup_token(
            session,
            setup_token=generated.token,
            password=_NEW_PASSWORD,
        )
        assert user.id == user_id
        # Mot de passe désormais défini et vérifiable.
        assert user.password_hash is not None
        assert verify_password(_NEW_PASSWORD, user.password_hash) is True
        # Jeton consommé (usage unique) : selector, hash et échéance effacés.
        assert user.setup_token_selector is None
        assert user.setup_token_hash is None
        assert user.setup_token_expires_at is None

    # Coût constant : au plus UNE vérification Argon2, malgré 4 comptes porteurs.
    assert counter.count == 1, (
        "Le lookup par selector indexé doit effectuer au plus une vérification "
        f"Argon2id (observé : {counter.count})"
    )

    # Persistance : rechargé depuis la base, le jeton reste consommé.
    reloaded = await _load_user(session_factory, user_id)
    assert reloaded.setup_token_selector is None
    assert reloaded.setup_token_hash is None
    assert reloaded.password_hash is not None


async def test_usage_unique_second_appel_rejete(
    session_factory: async_sessionmaker,
) -> None:
    """Usage unique : un second appel avec le même jeton est rejeté."""
    generated = generate_setup_token()
    user_id = await _insert_setup_user(
        session_factory,
        selector=generated.selector,
        verifier_hash=generated.verifier_hash,
        expires_at=_future(),
    )

    # 1ʳᵉ consommation : succès, jeton consommé (commit).
    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        await consume_setup_token(
            session, setup_token=generated.token, password=_NEW_PASSWORD
        )

    # 2ᵉ consommation du MÊME jeton : le selector a été effacé → rejet générique.
    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        with pytest.raises(SetupTokenError):
            await consume_setup_token(
                session, setup_token=generated.token, password="autre-mot-de-passe"
            )

    # Le mot de passe de la 1ʳᵉ consommation est préservé (pas écrasé par le 2ᵉ).
    reloaded = await _load_user(session_factory, user_id)
    assert verify_password(_NEW_PASSWORD, reloaded.password_hash) is True


async def test_jeton_expire_rejete(session_factory: async_sessionmaker) -> None:
    """Expiration : un jeton dont l'échéance est passée est rejeté, non consommé."""
    generated = generate_setup_token()
    user_id = await _insert_setup_user(
        session_factory,
        selector=generated.selector,
        verifier_hash=generated.verifier_hash,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        with pytest.raises(SetupTokenError):
            await consume_setup_token(
                session, setup_token=generated.token, password=_NEW_PASSWORD
            )

    # Jeton NON consommé sur un échec d'expiration ; mot de passe toujours absent.
    reloaded = await _load_user(session_factory, user_id)
    assert reloaded.password_hash is None
    assert reloaded.setup_token_hash is not None
    assert reloaded.setup_token_selector == generated.selector


async def test_selector_inconnu_meme_erreur_generique(
    session_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selector inconnu → même `SetupTokenError` + vérif Argon2 factice (anti-timing).

    Un selector absent et un verifier faux (test suivant) donnent la MÊME erreur
    générique (non-fuite d'existence, P3). Ici le selector n'existe pas : une
    vérification Argon2id factice est néanmoins exécutée (nivellement du temps).
    """
    # Un compte réel existe, mais le jeton présenté porte un selector différent.
    real = generate_setup_token()
    await _insert_setup_user(
        session_factory,
        selector=real.selector,
        verifier_hash=real.verifier_hash,
        expires_at=_future(),
    )

    unknown = generate_setup_token()  # selector non présent en base
    counter = _Argon2Counter(monkeypatch)
    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        with pytest.raises(SetupTokenError):
            await consume_setup_token(
                session, setup_token=unknown.token, password=_NEW_PASSWORD
            )
    # Anti-timing : une vérification Argon2 factice a bien eu lieu pour un selector
    # inexistant (temps nivelé, P3).
    assert counter.count == 1, (
        "Un selector inconnu doit déclencher une vérification Argon2 factice "
        f"(anti-timing) (observé : {counter.count})"
    )


async def test_verifier_faux_meme_erreur_generique(
    session_factory: async_sessionmaker,
) -> None:
    """Verifier faux (selector valide) → même `SetupTokenError` qu'un selector inconnu.

    Non-fuite d'existence (P3) : un selector valide avec un mauvais verifier lève la
    même erreur générique qu'un selector inexistant. Le jeton n'est pas consommé.
    """
    generated = generate_setup_token()
    user_id = await _insert_setup_user(
        session_factory,
        selector=generated.selector,
        verifier_hash=generated.verifier_hash,
        expires_at=_future(),
    )

    # Même selector, verifier erroné.
    forged = f"{generated.selector}.{generate_setup_token().token.split('.', 1)[1]}"
    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        with pytest.raises(SetupTokenError):
            await consume_setup_token(
                session, setup_token=forged, password=_NEW_PASSWORD
            )

    # Jeton NON consommé sur verifier faux.
    reloaded = await _load_user(session_factory, user_id)
    assert reloaded.password_hash is None
    assert reloaded.setup_token_selector == generated.selector


@pytest.mark.parametrize(
    "malformed",
    [
        "jeton-sans-separateur",  # pas de `.`
        ".verifier-seul",  # selector vide
        "selector-seul.",  # verifier vide
        "",  # chaîne vide
    ],
)
async def test_token_mal_forme_rejete(
    session_factory: async_sessionmaker, malformed: str
) -> None:
    """Token mal formé (pas de `.`, selector ou verifier vide) → erreur générique."""
    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        with pytest.raises(SetupTokenError):
            await consume_setup_token(
                session, setup_token=malformed, password=_NEW_PASSWORD
            )


async def test_token_mal_forme_execute_argon2_factice(
    session_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Format invalide → vérification Argon2 factice (anti-timing, P3)."""
    counter = _Argon2Counter(monkeypatch)
    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        with pytest.raises(SetupTokenError):
            await consume_setup_token(
                session, setup_token="format-invalide", password=_NEW_PASSWORD
            )
    assert counter.count == 1, (
        "Un format invalide doit déclencher une vérification Argon2 factice "
        f"(anti-timing) (observé : {counter.count})"
    )


async def test_token_herite_sans_selector_rejete(
    session_factory: async_sessionmaker,
) -> None:
    """Jeton hérité sans selector (setup_token_selector IS NULL) → rejet propre.

    Un compte dont le hash est présent mais le selector NUL (jeton émis avant la
    migration selector+verifier) n'est plus consommable : le nouveau lookup filtre
    sur le selector. Quel que soit le jeton présenté, l'appel est rejeté par la
    même erreur générique — sans erreur base ni consommation (invalidation assumée,
    ADR-2).
    """
    # Compte « hérité » : hash présent, selector NUL.
    legacy = generate_setup_token()
    user_id = await _insert_setup_user(
        session_factory,
        selector=None,
        verifier_hash=legacy.verifier_hash,
        expires_at=_future(),
    )

    # Tenter avec le jeton d'origine (dont le selector n'est pas en base).
    async with session_factory() as session, session.begin():
        await set_tenant_context(session, _TENANT_ID)
        with pytest.raises(SetupTokenError):
            await consume_setup_token(
                session, setup_token=legacy.token, password=_NEW_PASSWORD
            )

    # Le compte hérité est intact (non consommé, toujours sans mot de passe).
    reloaded = await _load_user(session_factory, user_id)
    assert reloaded.password_hash is None
    assert reloaded.setup_token_selector is None
    assert reloaded.setup_token_hash is not None
