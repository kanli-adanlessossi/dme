"""Tests du routeur d'administration de plateforme (tâche 8.3) — exigence 3.

Vérifient le comportement HTTP des endpoints de cycle de vie des tenants sans
toucher un vrai PostgreSQL : la session de base est remplacée par une fausse
session qui route les requêtes (lookup région/e-mail, insertions, mise à jour des
membres, insertion d'audit) et capture les objets ajoutés. Le cadrage par rôle
(`require_roles(platform_admin)`) est exercé sur le vrai chemin en injectant un
principal via `app.dependency_overrides`.

Couvre (vérifie P8 côté contrat + exigences 3.1–3.5, NFR-4) :
- un non-platform_admin ne peut créer (403) ni lister (403) des tenants (3.3) ;
- création valide → 201, renvoie le jeton de configuration (jamais de mot de passe
  en clair), crée tenant + medical_center + tenant_admin, audite `tenant.onboard`
  (3.1, 3.2, 3.5) ;
- coordonnées hors Togo → 422 (P7) ; e-mail admin déjà pris → 409 ;
- liste → renvoie les tenants projetés (3.3) ;
- désactivation → statut `inactive`, membres désactivés (refus de connexion),
  audit `tenant.deactivate` (3.4, 3.5, NFR-4) ; réactivation → `tenant.activate` ;
- tenant inconnu → 404.

L'isolation RLS réelle (écriture inter-tenant via `app.tenant_id`, masquage public
d'un tenant désactivé) est vérifiée par le test d'intégration
`test_admin_lifecycle_db.py` contre un vrai moteur PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Select, Update
from sqlalchemy.sql.elements import TextClause

from app.core.db import get_session
from app.core.deps import AuthPrincipal, get_current_principal
from app.core.security import verify_setup_token
from app.main import app
from app.models.app_user import AppUser
from app.models.medical_center import MedicalCenter
from app.models.tenant import Tenant
from app.routers import admin as admin_router
from app.schemas.audit import AuditEvent

# Coordonnées valides à Lomé (dans la boîte englobante du Togo).
_LOME_LON, _LOME_LAT = 1.2255, 6.1319

_REGION_ID = uuid.uuid4()
_REGION_CODE = "MARITIME"


class _ScalarResult:
    """Expose `scalar_one_or_none` / `scalars().all()` comme SQLAlchemy."""

    def __init__(self, value: object = None, rows: list | None = None) -> None:
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self) -> object:
        return self._value

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list:
        return self._rows


class _FakeAdminSession:
    """Fausse session routant les requêtes du service/routeur admin.

    Capture les objets ajoutés (`added`), l'état de `set_config` posé
    (`tenant_context`), les UPDATE de membres (`member_updates`) et compte les
    commits. Les lectures (région, e-mail, liste des tenants) renvoient des valeurs
    configurables.
    """

    def __init__(
        self,
        *,
        region_id: uuid.UUID | None = _REGION_ID,
        region_code: str = _REGION_CODE,
        email_taken: bool = False,
        tenants: list[Tenant] | None = None,
        get_tenant: Tenant | None = None,
    ) -> None:
        self._region_id = region_id
        self._region_code = region_code
        self._email_taken = email_taken
        self._tenants = tenants or []
        self._get_tenant = get_tenant
        self.added: list[object] = []
        self.commits = 0
        self.flushes = 0
        self.tenant_context: uuid.UUID | None = None
        self.member_updates: list[bool] = []
        self.audit_events: list[dict] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        # Simule le peuplement des valeurs serveur (server_default) à l'insertion :
        # `tenant.created_at` est posé par la base (now()) à la persistance.
        for obj in self.added:
            if isinstance(obj, Tenant) and obj.created_at is None:
                obj.created_at = datetime.now(UTC)
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def get(self, _model: object, pk: uuid.UUID) -> object | None:
        return self._get_tenant

    async def execute(self, statement: object, params: dict | None = None):
        # --- Instructions SQL textuelles : set_config (contexte tenant) / audit ---
        if isinstance(statement, TextClause):
            sql = str(statement)
            if "set_config" in sql and params is not None:
                self.tenant_context = uuid.UUID(params["tid"])
                return _ScalarResult()
            if "INSERT INTO audit_log" in sql and params is not None:
                self.audit_events.append(params)
                return _ScalarResult()
            return _ScalarResult()

        # --- UPDATE app_user (bascule is_active des membres) ---
        if isinstance(statement, Update):
            compiled = str(statement)
            if "app_user" in compiled:
                # La valeur is_active est portée par les paramètres liés du Update.
                values = getattr(statement, "_values", {}) or {}
                for col, val in values.items():
                    if getattr(col, "name", "") == "is_active":
                        self.member_updates.append(bool(val.value))
            return _ScalarResult()

        # --- SELECT : région (id/code), e-mail, liste des tenants ---
        if isinstance(statement, Select):
            # On route sur la (les) colonne(s) sélectionnée(s), pas sur une
            # sous-chaîne du SQL (le WHERE référence d'autres colonnes).
            selected = [getattr(c, "key", str(c)) for c in statement.selected_columns]
            label = ".".join(
                getattr(c, "table", None).name + "." + getattr(c, "key", "")  # type: ignore[union-attr]
                if getattr(c, "table", None) is not None
                else str(c)
                for c in statement.selected_columns
            )
            if "region.code" in label:
                return _ScalarResult(value=self._region_code)
            if "region.id" in label:
                return _ScalarResult(value=self._region_id)
            if "app_user.id" in label:
                sentinel = uuid.uuid4() if self._email_taken else None
                return _ScalarResult(value=sentinel)
            # Sinon : requête de liste des tenants (select(Tenant)).
            if "id" in selected or "tenant" in label.lower():
                return _ScalarResult(rows=self._tenants)
        return _ScalarResult()


# État partagé entre l'override de session et les tests.
_session: _FakeAdminSession | None = None
_principal: AuthPrincipal | None = None


async def _override_session():
    assert _session is not None
    yield _session


async def _override_principal() -> AuthPrincipal:
    assert _principal is not None
    return _principal


@pytest.fixture(autouse=True)
def _reset() -> None:
    global _session, _principal
    _session = None
    _principal = None


@pytest.fixture
def client() -> TestClient:
    """Client avec session factice et principal injecté (bypass du JWT)."""
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_principal] = _override_principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_principal, None)


@pytest.fixture
def captured_audit(monkeypatch: pytest.MonkeyPatch) -> list[AuditEvent]:
    """Capture les événements d'audit (record_audit) émis par le routeur admin."""
    events: list[AuditEvent] = []

    async def _fake_record(_session: object, event: AuditEvent) -> None:
        events.append(event)

    monkeypatch.setattr(admin_router, "record_audit", _fake_record)
    return events


def _set_principal(role: str) -> None:
    global _principal
    tenant_id = None if role == "platform_admin" else uuid.uuid4()
    _principal = AuthPrincipal(sub=uuid.uuid4(), tenant_id=tenant_id, role=role)


def _set_session(session: _FakeAdminSession) -> None:
    global _session
    _session = session


def _create_payload(**overrides: object) -> dict:
    payload = {
        "name": "Centre de santé de Lomé",
        "region_code": _REGION_CODE,
        "address": "12 Rue du Commerce, Lomé",
        "location": {"longitude": _LOME_LON, "latitude": _LOME_LAT},
        "phone": "+22890000000",
        "email": "contact@centre-lome.tg",
        "admin_email": "admin@centre-lome.tg",
    }
    payload.update(overrides)
    return payload


# --- Cadrage par rôle : non-platform_admin refusé (exigence 3.3) ----------------


def test_create_refuse_un_role_non_platform_admin_403(client: TestClient) -> None:
    """Un tenant_admin ne peut pas créer de tenant → 403 (exigence 3.3)."""
    _set_principal("tenant_admin")
    _set_session(_FakeAdminSession())

    resp = client.post("/api/admin/tenants", json=_create_payload())
    assert resp.status_code == 403


def test_list_refuse_un_role_non_platform_admin_403(client: TestClient) -> None:
    """Un clinician ne peut pas lister les tenants → 403 (exigence 3.3)."""
    _set_principal("clinician")
    _set_session(_FakeAdminSession())

    resp = client.get("/api/admin/tenants")
    assert resp.status_code == 403


def test_patch_refuse_un_role_non_platform_admin_403(client: TestClient) -> None:
    """Un staff ne peut pas changer le cycle de vie d'un tenant → 403."""
    _set_principal("staff")
    _set_session(_FakeAdminSession())

    resp = client.patch(
        f"/api/admin/tenants/{uuid.uuid4()}", json={"status": "inactive"}
    )
    assert resp.status_code == 403


# --- Création d'un tenant (exigences 3.1, 3.2, 3.5) -----------------------------


def test_create_tenant_201_renvoie_jeton_et_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Création valide → 201, jeton de configuration émis, onboarding audité."""
    _set_principal("platform_admin")
    session = _FakeAdminSession()
    _set_session(session)

    resp = client.post("/api/admin/tenants", json=_create_payload())
    assert resp.status_code == 201
    body = resp.json()

    # Le tenant est créé actif, avec sa région et son admin.
    assert body["status"] == "active"
    assert body["region_code"] == _REGION_CODE
    assert body["admin_email"] == "admin@centre-lome.tg"
    # Un jeton de configuration à usage unique est renvoyé (jamais un mot de passe).
    assert body["setup_token"]
    assert "password" not in body
    assert "setup_token_expires_at" in body

    # Tenant + medical_center + tenant_admin ont été ajoutés (une transaction).
    assert any(isinstance(o, Tenant) for o in session.added)
    center = next(o for o in session.added if isinstance(o, MedicalCenter))
    admin_user = next(o for o in session.added if isinstance(o, AppUser))
    assert admin_user.role == "tenant_admin"
    assert admin_user.password_hash is None  # jamais de mot de passe en clair
    assert admin_user.setup_token_hash is not None
    # Le jeton renvoyé correspond bien à l'empreinte stockée (usage unique).
    assert verify_setup_token(body["setup_token"], admin_user.setup_token_hash)
    # Le centre est cadré au tenant créé.
    assert center.tenant_id == admin_user.tenant_id

    # Le contexte RLS a été posé sur le tenant créé avant d'écrire les lignes
    # cadrées (medical_center / app_user).
    assert session.tenant_context == admin_user.tenant_id

    # L'onboarding est audité (exigence 3.5) et la transaction validée.
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "tenant.onboard"
    assert captured_audit[0].actor_user_id == _principal.sub  # type: ignore[union-attr]
    assert session.commits == 1


def test_create_tenant_coordonnees_hors_togo_422(client: TestClient) -> None:
    """Des coordonnées hors de la frontière du Togo → 422 (P7)."""
    _set_principal("platform_admin")
    _set_session(_FakeAdminSession())

    # Longitude/latitude hors boîte englobante du Togo (ex. Paris).
    resp = client.post(
        "/api/admin/tenants",
        json=_create_payload(location={"longitude": 2.35, "latitude": 48.85}),
    )
    assert resp.status_code == 422


def test_create_tenant_region_inconnue_422(client: TestClient) -> None:
    """Un code de région absent de la base → 422."""
    _set_principal("platform_admin")
    _set_session(_FakeAdminSession(region_id=None))

    resp = client.post("/api/admin/tenants", json=_create_payload())
    assert resp.status_code == 422


def test_create_tenant_email_admin_deja_pris_409(client: TestClient) -> None:
    """Un e-mail de tenant_admin déjà utilisé → 409."""
    _set_principal("platform_admin")
    _set_session(_FakeAdminSession(email_taken=True))

    resp = client.post("/api/admin/tenants", json=_create_payload())
    assert resp.status_code == 409


# --- Liste des tenants (exigence 3.3) -------------------------------------------


def test_list_tenants_renvoie_les_tenants(client: TestClient) -> None:
    """Un platform_admin liste tous les tenants (projection d'administration)."""
    _set_principal("platform_admin")
    tenants = [
        Tenant(
            id=uuid.uuid4(),
            name="Centre A",
            region_id=_REGION_ID,
            status="active",
            created_at=datetime.now(UTC),
        ),
        Tenant(
            id=uuid.uuid4(),
            name="Centre B",
            region_id=_REGION_ID,
            status="inactive",
            created_at=datetime.now(UTC),
        ),
    ]
    _set_session(_FakeAdminSession(tenants=tenants))

    resp = client.get("/api/admin/tenants")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    names = {t["name"] for t in body}
    assert names == {"Centre A", "Centre B"}
    assert {t["status"] for t in body} == {"active", "inactive"}


# --- Cycle de vie : activation / désactivation (exigences 3.4, 3.5, NFR-4) ------


def test_patch_desactive_tenant_et_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Désactivation → statut inactive, membres désactivés, audit tenant.deactivate."""
    _set_principal("platform_admin")
    tenant_id = uuid.uuid4()
    tenant = Tenant(
        id=tenant_id,
        name="Centre",
        region_id=_REGION_ID,
        status="active",
        created_at=datetime.now(UTC),
    )
    session = _FakeAdminSession(get_tenant=tenant)
    _set_session(session)

    resp = client.patch(
        f"/api/admin/tenants/{tenant_id}", json={"status": "inactive"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"

    # Les membres du tenant ont été désactivés (refus de connexion, exigence 3.4).
    assert session.member_updates == [False]
    # Le contexte RLS a été posé sur le tenant cible pour l'UPDATE app_user.
    assert session.tenant_context == tenant_id
    # Action auditée (exigence 3.5) et transaction validée.
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "tenant.deactivate"
    assert session.commits == 1


def test_patch_reactive_tenant_et_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
) -> None:
    """Réactivation → statut active, membres réactivés, audit tenant.activate."""
    _set_principal("platform_admin")
    tenant_id = uuid.uuid4()
    tenant = Tenant(
        id=tenant_id,
        name="Centre",
        region_id=_REGION_ID,
        status="inactive",
        created_at=datetime.now(UTC),
    )
    session = _FakeAdminSession(get_tenant=tenant)
    _set_session(session)

    resp = client.patch(
        f"/api/admin/tenants/{tenant_id}", json={"status": "active"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert session.member_updates == [True]
    assert captured_audit[0].action == "tenant.activate"


def test_patch_tenant_inconnu_404(client: TestClient) -> None:
    """Un tenant inexistant → 404."""
    _set_principal("platform_admin")
    _set_session(_FakeAdminSession(get_tenant=None))

    resp = client.patch(
        f"/api/admin/tenants/{uuid.uuid4()}", json={"status": "inactive"}
    )
    assert resp.status_code == 404
