"""Limitation de débit PAR IP des routes d'authentification (Correctif 1, G-9/NFR-2).

Verrouille, sans vraie base, le repli applicatif SlowAPI posé sur `/api/auth/*`
(décorateur `@limiter.limit(auth_rate_limit())`, cf. `app/routers/auth.py`). C'est
une défense PAR IP, complémentaire du verrouillage PAR COMPTE
(`app/services/lockout.py`) : elle casse un balayage multi-comptes (un essai par
compte, jamais de seuil de lockout atteint) depuis une même source.

- **Sous le seuil → pas de 429.** Émettre exactement `limit` requêtes
  `POST /api/auth/login` (e-mail inconnu) : toutes répondent **401** (générique,
  P3), jamais 429. Borne positive : la limite ne gêne pas un usage légitime.
- **Au-delà du seuil → 429 reproductible.** La requête suivante franchit le seuil →
  **429** ; une requête de plus dans la même fenêtre reste **429** (reproductible).
  Corps minimal (aucune fuite d'information : ni PHI, ni champ interne).

Même motif que `test_public_rate_limit_cache.py` et `test_auth_router.py` : la
session de lookup e-mail est remplacée par une fausse session (aucun PostgreSQL),
l'écrivain d'audit est neutralisé, de sorte que chaque `POST /login` est bon marché
et renvoie **401** sous le seuil — on teste le code de rate-limit, pas le succès
d'auth. Le passage de 401 (sous le seuil) à **429** (au-delà) est l'observable.

Le seuil n'est PAS muté au runtime : le décorateur fige `auth_rate_limit()` à
l'import du routeur (bien avant la collecte des tests), donc muter la configuration
après coup ne reconfigure pas le décorateur déjà posé. On LIT le seuil configuré
(`_limit_count()`) et on émet juste ce qu'il faut de requêtes pour le franchir —
exactement la technique de `test_public_rate_limit_cache.py`.

Isolation du limiteur : une fixture `autouse` locale reset le limiteur mémoire avant
ET après chaque test (en plus de la fixture conftest qui reset avant chaque test),
pour ne pas laisser de compteur accumulé au test suivant.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.main import app
from app.routers import auth as auth_router

# Champs internes/sensibles qui ne doivent JAMAIS fuiter dans le corps d'un 429
# (design, Error Handling G-4).
_FORBIDDEN_FIELDS = frozenset(
    {
        "tenant_id",
        "password_hash",
        "setup_token_hash",
        "national_id",
        "mrn",
        "email",
    }
)


class _EmptyExecuteResult:
    """Résultat factice d'un INSERT/UPDATE de la denylist (`first()` → None)."""

    def first(self) -> None:
        return None


class _UnknownUserResult:
    """Résultat factice du lookup e-mail : aucun compte (`scalar_one_or_none` → None)."""

    def scalar_one_or_none(self) -> None:
        return None


class _FakeSession:
    """Session factice : e-mail toujours inconnu, absorbe commit (aucune base).

    Reconnaît le lookup e-mail (seul `select()` ORM, porteur de `whereclause`) des
    énoncés SQL bruts (`text()`) de la denylist de refresh. Pour un e-mail inconnu,
    `login` n'émet de toute façon pas d'écriture de denylist ; on renvoie malgré
    tout un résultat vide par robustesse.
    """

    async def execute(self, statement: object, _params: object = None) -> object:
        if not hasattr(statement, "whereclause"):
            return _EmptyExecuteResult()
        return _UnknownUserResult()

    async def commit(self) -> None:  # pragma: no cover - mutation absorbée
        return None


async def _override_get_session() -> object:
    yield _FakeSession()


@pytest.fixture(autouse=True)
def _reset_limiter() -> Iterator[None]:
    """Isole l'état du limiteur mémoire avant ET après chaque test de ce module."""
    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Client FastAPI : lookup e-mail factice (inconnu) et audit neutralisé."""

    async def _noop(_event: object) -> None:
        return None

    monkeypatch.setattr(auth_router, "record_audit_standalone", _noop)
    app.dependency_overrides[get_session] = _override_get_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _limit_count() -> int:
    """Nombre de requêtes autorisées par fenêtre, lu depuis la configuration.

    Parse le nombre en tête de `Settings.auth_rate_limit` (ex. « 10/minute » → 10),
    sans coder le seuil en dur dans le test.
    """
    raw = get_settings().auth_rate_limit
    return int(raw.split("/", 1)[0].strip())


def _login(client: TestClient) -> object:
    """Émet un `POST /api/auth/login` avec un e-mail inconnu (réponse 401 attendue)."""
    return client.post(
        "/api/auth/login",
        json={"email": "inconnu@centre-lome.tg", "password": "peu-importe"},
    )


def test_login_sous_le_seuil_ne_declenche_pas_429(client: TestClient) -> None:
    """Sous le seuil configuré → toutes les tentatives répondent 401, jamais 429.

    Borne positive : un utilisateur légitime qui se trompe de mot de passe quelques
    fois (sous le seuil) n'est jamais limité par IP.
    """
    limit = _limit_count()
    for i in range(limit):
        resp = _login(client)
        assert resp.status_code == 401, f"appel {i + 1}/{limit} inattendu: {resp.text}"


def test_login_au_dela_du_seuil_renvoie_429_reproductible(client: TestClient) -> None:
    """Au-delà du seuil → 429 reproductible, corps minimal sans fuite d'information."""
    limit = _limit_count()

    # Les `limit` premiers appels restent sous le seuil → 401.
    for i in range(limit):
        resp = _login(client)
        assert resp.status_code == 401, f"appel {i + 1}/{limit} inattendu: {resp.text}"

    # L'appel suivant franchit le seuil → 429.
    over = _login(client)
    assert over.status_code == 429, over.text

    # Reproductible : un autre appel dans la même fenêtre reste 429.
    again = _login(client)
    assert again.status_code == 429, again.text

    # Corps minimal, aucune fuite d'information (design, Error Handling G-4).
    body = over.json()
    assert "error" in body
    assert not (_FORBIDDEN_FIELDS & set(body.keys()))
    serialized = over.text.lower()
    for forbidden in ("tenant_id", "password_hash", "national_id", "mrn"):
        assert forbidden not in serialized
