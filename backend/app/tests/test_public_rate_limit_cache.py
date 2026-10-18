"""DoD de la tâche 4.3 (G-4) — limitation de débit + cache, sans régression (P4/P8).

Verrouille par des tests automatisés le fait que les tâches 4.1 (limitation de
débit) et 4.2 (`Cache-Control`) **n'ont pas altéré** la projection sûre (P4) ni la
règle « actifs seulement » (P8) des endpoints publics, ET que la Definition of Done
de la tâche 4.3 tient :

- **AC1 — 429 au-delà du seuil, reproductible.** Une même source (IP cliente) qui
  dépasse le seuil configuré (`Settings.public_rate_limit`, défaut « 60/minute »)
  reçoit un **429** ; une fois la fenêtre dépassée, les appels suivants restent 429
  (reproductible), et le corps est minimal (aucune fuite d'information : ni PHI, ni
  champ interne).
- **AC2 — `max-age` présent.** `GET /api/public/regions` et
  `GET /api/public/geo/togo-regions.geojson` portent
  `Cache-Control: public, max-age=300`. Les deux autres routes publiques ne portent
  PAS de `max-age` (la tâche 4.2 a bien cadré l'en-tête aux deux endpoints de
  référence).
- **Non-régression P4.** Une réponse publique continue de n'exposer aucun champ
  interne/sensible **tout en** portant le nouvel en-tête `Cache-Control` : prouve
  que l'ajout de l'en-tête n'a pas changé la projection.

Même motif que `test_public_router.py` : aucun vrai PostgreSQL. La session de lecture
publique (`public_session`) est remplacée par une session factice via
`app.dependency_overrides`, et la couche service est stubbée par monkeypatch, de sorte
que chaque appel est bon marché (aucune base) — ce qui rend acceptable d'émettre
60+ requêtes en mémoire pour franchir le seuil.

Isolation du limiteur (IMPORTANT). Le limiteur SlowAPI est **global et en mémoire**
(`app.state.limiter`, cf. `app/core/rate_limit.py`). Sans précaution, les compteurs
s'accumuleraient entre tests et pourraient faire basculer en 429 d'autres tests qui
touchent les routes publiques (`test_public_router.py`), ou rendre ce module
dépendant de l'ordre d'exécution. Une fixture `autouse` appelle donc
`limiter.reset()` (qui vide le stockage mémoire via `_storage.reset()`) **avant et
après chaque test** de ce module, garantissant des tests isolés et indépendants de
l'ordre.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.tenant_context import public_session
from app.main import app
from app.routers import public as public_router
from app.services.public import PublicCenterData, PublicRegionData

_CENTER_ID = uuid.uuid4()
_LOME_LON, _LOME_LAT = 1.2255, 6.1319

# Champs internes/sensibles qui ne doivent JAMAIS fuiter dans une réponse publique
# (P4) ni dans le corps d'un 429 (design, Error Handling G-4).
_FORBIDDEN_PUBLIC_FIELDS = frozenset(
    {
        "tenant_id",
        "status",
        "is_public",
        "created_by",
        "created_at",
        "updated_at",
        "deleted_at",
        "mrn",
        "national_id",
        "given_name",
        "family_name",
        "dob",
        "sex",
        "consent_to_store",
        "patient_id",
        "notes",
        "password_hash",
        "setup_token_hash",
    }
)


class _FakePublicSession:
    """Session factice : absorbe toute exécution, ne touche pas la base."""

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


async def _override_public_session() -> AsyncIterator[_FakePublicSession]:
    yield _FakePublicSession()


def _center(**overrides: object) -> PublicCenterData:
    """Construit une projection publique de centre (valeurs cohérentes)."""
    base: dict = {
        "id": _CENTER_ID,
        "name": "Centre de santé de Lomé",
        "region_code": "MARITIME",
        "address": "12 Rue du Commerce, Lomé",
        "longitude": _LOME_LON,
        "latitude": _LOME_LAT,
        "phone": "+22890000000",
        "email": "contact@centre-lome.tg",
        "opening_hours": [{"day": "lundi", "open": "08:00", "close": "17:00"}],
        "service_types": ["Maternité", "Pharmacie"],
    }
    base.update(overrides)
    return PublicCenterData(**base)


@pytest.fixture(autouse=True)
def _reset_limiter() -> Iterator[None]:
    """Isole l'état du limiteur mémoire avant ET après chaque test (voir docstring).

    Empêche toute contamination croisée : les compteurs du limiteur global ne
    s'accumulent pas entre tests, ce qui garantit l'indépendance à l'ordre et évite
    de faire basculer d'autres tests des routes publiques en 429.
    """
    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Client FastAPI : session de lecture publique factice (aucune base)."""
    app.dependency_overrides[public_session] = _override_public_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(public_session, None)


def _limit_count() -> int:
    """Nombre de requêtes autorisées par fenêtre, lu depuis la configuration.

    Parse le nombre en tête de `Settings.public_rate_limit` (ex. « 60/minute » → 60),
    sans coder le seuil en dur dans le test.
    """
    raw = get_settings().public_rate_limit
    return int(raw.split("/", 1)[0].strip())


# --------------------------------------------------------------------------- #
# AC1 — 429 au-delà du seuil, reproductible, corps minimal                     #
# --------------------------------------------------------------------------- #


def test_depassement_seuil_renvoie_429_reproductible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 : au-delà du seuil → 429 reproductible, sans fuite d'information.

    Pilote `GET /api/public/regions` depuis une même source (TestClient = hôte
    client constant, donc une seule clé d'IP) jusqu'à franchir le seuil configuré.
    On vérifie que :
      - les premiers appels (≤ seuil) répondent 200 ;
      - une fois le seuil dépassé, la réponse est 429 ;
      - un appel supplémentaire dans la même fenêtre reste 429 (reproductible) ;
      - le corps du 429 est minimal (aucun champ interne/sensible).
    """

    async def _fake(_session: object) -> list[PublicRegionData]:
        return [PublicRegionData(code="MARITIME", name_fr="Maritime", center_count=0)]

    monkeypatch.setattr(public_router, "list_public_regions", _fake)

    limit = _limit_count()

    # Les `limit` premiers appels doivent passer (200) : on reste sous le seuil.
    for i in range(limit):
        resp = client.get("/api/public/regions")
        assert resp.status_code == 200, f"appel {i + 1}/{limit} inattendu: {resp.text}"

    # L'appel suivant franchit le seuil → 429.
    over = client.get("/api/public/regions")
    assert over.status_code == 429, over.text

    # Reproductible : un autre appel dans la même fenêtre reste 429.
    again = client.get("/api/public/regions")
    assert again.status_code == 429, again.text

    # Corps minimal, aucune fuite d'information (design, Error Handling G-4).
    body = over.json()
    assert "error" in body
    assert not (_FORBIDDEN_PUBLIC_FIELDS & set(body.keys()))
    # La valeur du message ne contient pas non plus de champ sensible connu.
    serialized = over.text.lower()
    for forbidden in ("tenant_id", "password_hash", "national_id", "mrn"):
        assert forbidden not in serialized


# --------------------------------------------------------------------------- #
# AC2 — `Cache-Control: public, max-age=300` sur les deux endpoints de référence #
# --------------------------------------------------------------------------- #


def test_regions_porte_cache_control_max_age(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 : `GET /regions` porte `Cache-Control: public, max-age=300`."""

    async def _fake(_session: object) -> list[PublicRegionData]:
        return [PublicRegionData(code="MARITIME", name_fr="Maritime", center_count=0)]

    monkeypatch.setattr(public_router, "list_public_regions", _fake)

    resp = client.get("/api/public/regions")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_geojson_porte_cache_control_max_age(client: TestClient) -> None:
    """AC2 : `GET /geo/togo-regions.geojson` porte `Cache-Control: public, max-age=300`.

    Sert le vrai fichier de référence livré (aucune base), comme dans
    `test_public_router.py`.
    """
    resp = client.get("/api/public/geo/togo-regions.geojson")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_centres_region_sans_cache_control(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 (cadrage) : `GET /regions/{code}/centers` NE porte PAS de `max-age`.

    Verrouille que la tâche 4.2 a limité l'en-tête aux deux seuls endpoints de
    référence (régions + GeoJSON), pas à l'ensemble des routes publiques.
    """

    async def _fake(_session: object, _code: str) -> list[PublicCenterData]:
        return [_center()]

    monkeypatch.setattr(public_router, "list_region_centers", _fake)

    resp = client.get("/api/public/regions/MARITIME/centers")
    assert resp.status_code == 200, resp.text
    assert "max-age" not in resp.headers.get("cache-control", "")


def test_center_detail_sans_cache_control(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 (cadrage) : `GET /centers/{id}` NE porte PAS de `max-age`."""

    async def _fake(_session: object, _cid: uuid.UUID) -> PublicCenterData:
        return _center()

    monkeypatch.setattr(public_router, "get_public_center", _fake)

    resp = client.get(f"/api/public/centers/{_CENTER_ID}")
    assert resp.status_code == 200, resp.text
    assert "max-age" not in resp.headers.get("cache-control", "")


# --------------------------------------------------------------------------- #
# Non-régression — projection sûre (P4) inchangée malgré le `Cache-Control`     #
# --------------------------------------------------------------------------- #


def test_projection_sure_inchangee_avec_cache_control(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-régression P4 : la réponse n'expose aucun champ sensible ET porte le cache.

    Prouve simultanément que l'ajout de `Cache-Control` (tâche 4.2) n'a pas altéré la
    projection sûre : la réponse `GET /regions` porte bien l'en-tête de cache tout en
    ne contenant que des champs publics (aucun champ interne/sensible, P4).
    """

    async def _fake(_session: object) -> list[PublicRegionData]:
        return [PublicRegionData(code="MARITIME", name_fr="Maritime", center_count=3)]

    monkeypatch.setattr(public_router, "list_public_regions", _fake)

    resp = client.get("/api/public/regions")
    assert resp.status_code == 200, resp.text
    # L'en-tête de cache est présent...
    assert resp.headers["cache-control"] == "public, max-age=300"
    # ...et la projection reste sûre : seulement des champs publics.
    region = resp.json()[0]
    assert region == {"code": "MARITIME", "name_fr": "Maritime", "center_count": 3}
    assert not (_FORBIDDEN_PUBLIC_FIELDS & region.keys())
