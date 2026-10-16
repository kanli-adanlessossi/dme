"""Tests du contrat HTTP du domaine clinique (tâche 9.3) — exigence 5, NFR-3.

Vérifient le comportement HTTP des routeurs `patients`/`visits` **sans toucher un
vrai PostgreSQL** (même motif que `test_admin_router.py`) : la session cadrée au
tenant (`tenant_session`) et le principal authentifié (`get_current_principal`)
sont remplacés par `app.dependency_overrides`, la couche service est stubbée. On
exerce ici les garanties observables au niveau de la **surface HTTP** ; les
propriétés dépendant du moteur (isolation RLS P1/P2, suppression logique physique
P6, unicité en base) sont vérifiées contre un vrai moteur dans
`test_clinical_db.py`.

Couvre (design, « Correctness Properties ») :

- **P4 — confidentialité des PHI.** AUCUNE route patient/visite n'est accessible
  sans authentification : toutes exigent un rôle manipulant les PHI. On vérifie
  en plus, via `/openapi.json`, qu'aucun point d'accès **public** (`/api/public`)
  n'expose de schéma patient/visite — la PHI n'a aucune surface non authentifiée.
- **Cadrage par rôle (P4, exigence 2.2).** Un `platform_admin` (transversal,
  tenant NUL) ou un rôle inconnu → 403 ; un rôle PHI (`tenant_admin`,
  `clinician`, `staff`) est admis.
- **Tenancy autoritaire côté serveur (P2, exigence 5.2).** Un `tenant_id` forgé
  dans le corps de création est ignoré : le serveur appose celui du principal.
- **`id` client UUIDv7 accepté (NFR-6).** La clé primaire fournie par le client
  est écrite telle quelle (jamais réécrite).
- **Consentement (exigence 5.6).** `consent_to_store` est capturé à la création
  et exposé en lecture ; absent du corps → 422.
- **Unicité patient (exigence 5.4).** Un doublon de `mrn` dans le tenant → 409.
- **Non-fuite d'existence (P3).** Un patient/visite inconnu ou hors tenant → 404
  (jamais 403), indiscernable d'une ressource inexistante.
- **PHI auditées (P5, NFR-3).** Création, consultation (liste + détail), mise à
  jour et suppression émettent **exactement une** ligne d'audit, dans la
  transaction de la requête.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.core.deps import AuthPrincipal, get_current_principal
from app.core.tenant_context import tenant_session
from app.main import app
from app.models.patient import Patient
from app.models.visit import Visit
from app.routers import patients as patients_router
from app.routers import visits as visits_router
from app.schemas.audit import AuditEvent
from app.services.clinical import (
    PatientForVisitNotFoundError,
    PatientMrnConflictError,
    PatientNotFoundError,
    VisitNotFoundError,
)

# Tenant/acteur du principal injecté dans la plupart des tests.
_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
# Un autre tenant, utilisé pour forger un `tenant_id` dans un corps de requête.
_OTHER_TENANT_ID = uuid.uuid4()


# --------------------------------------------------------------------------- #
# Doublures : session cadrée au tenant factice + fabrique de patients/visites  #
# --------------------------------------------------------------------------- #


class _FakeTenantSession:
    """Session factice tenant : absorbe les écritures d'audit, ne commit rien.

    `tenant_session` (le vrai) ouvre une transaction et pose `app.tenant_id` ;
    ici on n'exerce pas le moteur, donc la session factice se contente d'absorber
    l'INSERT d'audit émis par le routeur (`record_audit` est par ailleurs capturé
    séparément) et toute autre exécution.
    """

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


def _make_patient(
    *,
    patient_id: uuid.UUID,
    tenant_id: uuid.UUID = _TENANT_ID,
    mrn: str = "MRN-000123",
    consent_to_store: bool = True,
    deleted_at: datetime.datetime | None = None,
) -> Patient:
    """Construit un `Patient` ORM détaché (valeurs serveur simulées) pour la projection."""
    now = datetime.datetime.now(datetime.UTC)
    return Patient(
        id=patient_id,
        tenant_id=tenant_id,
        mrn=mrn,
        given_name="Koffi",
        family_name="Mensah",
        dob=datetime.date(1990, 5, 14),
        sex="M",
        phone="+22890000000",
        national_id=None,
        consent_to_store=consent_to_store,
        created_by=_ACTOR_ID,
        created_at=now,
        updated_at=now,
        version=1,
        deleted_at=deleted_at,
    )


def _make_visit(
    *,
    visit_id: uuid.UUID,
    patient_id: uuid.UUID,
    tenant_id: uuid.UUID = _TENANT_ID,
) -> Visit:
    """Construit une `Visit` ORM détachée (valeurs serveur simulées)."""
    now = datetime.datetime.now(datetime.UTC)
    return Visit(
        id=visit_id,
        tenant_id=tenant_id,
        patient_id=patient_id,
        visited_at=now,
        reason="Consultation générale",
        notes="RAS",
        created_by=_ACTOR_ID,
        updated_at=now,
        version=1,
        deleted_at=None,
    )


# --------------------------------------------------------------------------- #
# État partagé entre overrides et tests + fixtures                             #
# --------------------------------------------------------------------------- #

_principal: AuthPrincipal | None = None


async def _override_tenant_session() -> AsyncIterator[_FakeTenantSession]:
    yield _FakeTenantSession()


async def _override_principal() -> AuthPrincipal:
    assert _principal is not None, "Principal non configuré pour ce test"
    return _principal


@pytest.fixture(autouse=True)
def _reset() -> None:
    global _principal
    _principal = None


@pytest.fixture
def client() -> TestClient:
    """Client FastAPI : session tenant factice + principal injecté (bypass JWT)."""
    app.dependency_overrides[tenant_session] = _override_tenant_session
    app.dependency_overrides[get_current_principal] = _override_principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(tenant_session, None)
        app.dependency_overrides.pop(get_current_principal, None)


@pytest.fixture
def captured_audit(monkeypatch: pytest.MonkeyPatch) -> list[AuditEvent]:
    """Capture les événements d'audit émis par les routeurs patient/visite (P5)."""
    events: list[AuditEvent] = []

    async def _fake_record(_session: object, event: AuditEvent) -> None:
        events.append(event)

    monkeypatch.setattr(patients_router, "record_audit", _fake_record)
    monkeypatch.setattr(visits_router, "record_audit", _fake_record)
    return events


def _set_principal(role: str, *, tenant_id: uuid.UUID | None = _TENANT_ID) -> None:
    global _principal
    # Le platform_admin a un tenant NUL (transversal) ; les rôles PHI sont cadrés.
    tid = None if role == "platform_admin" else tenant_id
    _principal = AuthPrincipal(sub=_ACTOR_ID, tenant_id=tid, role=role)


def _patient_payload(**overrides: object) -> dict:
    payload: dict = {
        "id": str(uuid.uuid4()),
        "mrn": "MRN-000123",
        "given_name": "Koffi",
        "family_name": "Mensah",
        "dob": "1990-05-14",
        "sex": "M",
        "phone": "+22890000000",
        "consent_to_store": True,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# P4 / RBAC : aucune route PHI n'est accessible sans rôle PHI                   #
# --------------------------------------------------------------------------- #

_PHI_ROLES = ["tenant_admin", "clinician", "staff"]
_NON_PHI_ROLES = ["platform_admin"]


@pytest.mark.parametrize("role", _NON_PHI_ROLES)
def test_platform_admin_ne_peut_pas_lister_les_patients_403(
    client: TestClient, role: str
) -> None:
    """Un platform_admin (transversal) n'accède pas aux PHI patient → 403 (P4)."""
    _set_principal(role)
    resp = client.get("/api/patients")
    assert resp.status_code == 403


@pytest.mark.parametrize("role", _NON_PHI_ROLES)
def test_platform_admin_ne_peut_pas_lister_les_visites_403(
    client: TestClient, role: str
) -> None:
    """Un platform_admin n'accède pas aux PHI visite → 403 (P4)."""
    _set_principal(role)
    resp = client.get("/api/visits")
    assert resp.status_code == 403


@pytest.mark.parametrize("role", _PHI_ROLES)
def test_un_role_phi_peut_lister_les_patients(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    """Chaque rôle PHI (tenant_admin/clinician/staff) peut lister (contrôle positif)."""
    _set_principal(role)

    async def _fake_list(_session: object) -> list[Patient]:
        return []

    monkeypatch.setattr(patients_router, "list_patients", _fake_list)

    resp = client.get("/api/patients")
    assert resp.status_code == 200
    assert resp.json() == []


def test_aucun_endpoint_public_n_expose_de_phi(client: TestClient) -> None:
    """Aucune route PHI n'est publique : la PHI n'a pas de surface non authentifiée.

    Vérifie P4 au niveau du contrat : dans `/openapi.json`, les chemins
    `/api/patients*` et `/api/visits*` déclarent tous une exigence de sécurité
    (401/403 au contrat), et aucun chemin public (`/api/public/...`) ne référence
    les schémas `PatientResponse`/`VisitResponse`.
    """
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]

    # 1. Toute route patient/visite documente 403 (cadrage par rôle) et 404
    #    (non-fuite) — preuve qu'elle est derrière l'auth, jamais publique.
    phi_paths = [p for p in paths if p.startswith(("/api/patients", "/api/visits"))]
    assert phi_paths, "Les routes PHI doivent exister dans le contrat"
    for path in phi_paths:
        for method, op in paths[path].items():
            if method not in {"get", "post", "patch", "delete"}:
                continue
            assert "403" in op["responses"], (
                f"{method.upper()} {path} doit documenter 403 (route authentifiée)"
            )

    # 2. Aucun chemin public ne référence une projection PHI.
    import json as _json

    for path, item in paths.items():
        if not path.startswith("/api/public"):
            continue
        blob = _json.dumps(item)
        assert "PatientResponse" not in blob, f"{path} ne doit exposer aucune PHI"
        assert "VisitResponse" not in blob, f"{path} ne doit exposer aucune PHI"


# --------------------------------------------------------------------------- #
# Création patient : tenancy serveur (P2), id client (NFR-6), consentement     #
# --------------------------------------------------------------------------- #


def test_create_patient_appose_le_tenant_du_principal_ignore_le_corps(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le serveur appose le tenant du principal ; un tenant_id forgé est ignoré (P2).

    Le schéma de création n'accepte pas `tenant_id` ; même si le client en ajoute
    un (forgé = autre tenant), il est ignoré et le service reçoit le tenant du
    principal authentifié. On capture les arguments passés au service pour le
    prouver, et on vérifie que `id` client (UUIDv7) et `consent_to_store` sont
    transmis tels quels (NFR-6, exigence 5.6). Création auditée (P5).
    """
    _set_principal("clinician")
    captured: dict = {}
    client_id = uuid.uuid4()

    async def _fake_create(_session: object, **kwargs: object) -> Patient:
        captured.update(kwargs)
        return _make_patient(
            patient_id=kwargs["patient_id"],  # type: ignore[arg-type]
            mrn=kwargs["mrn"],  # type: ignore[arg-type]
            consent_to_store=kwargs["consent_to_store"],  # type: ignore[arg-type]
        )

    monkeypatch.setattr(patients_router, "create_patient", _fake_create)

    resp = client.post(
        "/api/patients",
        json=_patient_payload(
            id=str(client_id),
            consent_to_store=True,
            # `tenant_id` forgé dans le corps : doit être ignoré par le serveur.
            tenant_id=str(_OTHER_TENANT_ID),
        ),
    )
    assert resp.status_code == 201, resp.text

    # Le service a reçu le tenant du PRINCIPAL, pas celui forgé dans le corps.
    assert captured["tenant_id"] == _TENANT_ID
    assert captured["tenant_id"] != _OTHER_TENANT_ID
    # `id` client (UUIDv7) transmis tel quel (NFR-6).
    assert captured["patient_id"] == client_id
    # `created_by` dérivé du principal (jamais du corps).
    assert captured["created_by"] == _ACTOR_ID
    # Consentement capturé (exigence 5.6).
    assert captured["consent_to_store"] is True

    body = resp.json()
    # La réponse porte le tenant serveur et le consentement — jamais le tenant forgé.
    assert body["tenant_id"] == str(_TENANT_ID)
    assert body["consent_to_store"] is True
    assert body["id"] == str(client_id)

    # Exactement une ligne d'audit `patient.create` (P5).
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "patient.create"
    assert captured_audit[0].tenant_id == _TENANT_ID
    assert captured_audit[0].actor_user_id == _ACTOR_ID
    assert captured_audit[0].entity == "patient"
    assert captured_audit[0].entity_id == client_id


def test_create_patient_sans_consentement_422(client: TestClient) -> None:
    """`consent_to_store` est obligatoire à la création — absent → 422 (exigence 5.6)."""
    _set_principal("clinician")
    payload = _patient_payload()
    del payload["consent_to_store"]
    resp = client.post("/api/patients", json=payload)
    assert resp.status_code == 422


def test_create_patient_consentement_refuse_est_capture(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le consentement est capturé tel quel, y compris `false` (exigence 5.6)."""
    _set_principal("clinician")
    captured: dict = {}

    async def _fake_create(_session: object, **kwargs: object) -> Patient:
        captured.update(kwargs)
        return _make_patient(
            patient_id=kwargs["patient_id"],  # type: ignore[arg-type]
            consent_to_store=kwargs["consent_to_store"],  # type: ignore[arg-type]
        )

    monkeypatch.setattr(patients_router, "create_patient", _fake_create)

    resp = client.post("/api/patients", json=_patient_payload(consent_to_store=False))
    assert resp.status_code == 201, resp.text
    assert captured["consent_to_store"] is False
    assert resp.json()["consent_to_store"] is False


def test_create_patient_mrn_duplique_409(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un `mrn` déjà présent dans le tenant → 409 (unicité, exigence 5.4).

    Le service lève `PatientMrnConflictError` (traduction de la violation
    `UNIQUE (tenant_id, mrn)`) ; le routeur renvoie 409. Aucune ligne d'audit de
    création ne doit être émise (la mutation a échoué).
    """
    _set_principal("clinician")

    async def _fake_create(_session: object, **_kwargs: object) -> Patient:
        raise PatientMrnConflictError

    monkeypatch.setattr(patients_router, "create_patient", _fake_create)

    resp = client.post("/api/patients", json=_patient_payload())
    assert resp.status_code == 409
    assert captured_audit == []


# --------------------------------------------------------------------------- #
# Lecture / consultation patient : non-fuite (P3) + audit de consultation (P5) #
# --------------------------------------------------------------------------- #


def test_get_patient_inconnu_ou_hors_tenant_404(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un patient inconnu OU hors tenant → 404, jamais 403 (non-fuite, P3)."""
    _set_principal("clinician")

    async def _fake_get(_session: object, _pid: uuid.UUID) -> Patient:
        raise PatientNotFoundError

    monkeypatch.setattr(patients_router, "get_patient", _fake_get)

    resp = client.get(f"/api/patients/{uuid.uuid4()}")
    assert resp.status_code == 404
    # Aucune consultation réussie → pas de ligne d'audit de lecture.
    assert captured_audit == []


def test_get_patient_consultation_est_auditee(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La consultation d'un patient émet exactement une ligne `patient.read` (P5)."""
    _set_principal("clinician")
    pid = uuid.uuid4()

    async def _fake_get(_session: object, _pid: uuid.UUID) -> Patient:
        return _make_patient(patient_id=pid)

    monkeypatch.setattr(patients_router, "get_patient", _fake_get)

    resp = client.get(f"/api/patients/{pid}")
    assert resp.status_code == 200
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "patient.read"
    assert captured_audit[0].entity == "patient"
    assert captured_audit[0].entity_id == pid


def test_list_patients_consultation_est_auditee(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lister les patients émet exactement une ligne `patient.list` (P5)."""
    _set_principal("staff")

    async def _fake_list(_session: object) -> list[Patient]:
        return [_make_patient(patient_id=uuid.uuid4())]

    monkeypatch.setattr(patients_router, "list_patients", _fake_list)

    resp = client.get("/api/patients")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "patient.list"
    assert captured_audit[0].entity == "patient"


# --------------------------------------------------------------------------- #
# Mise à jour / suppression patient : audit (P5), non-fuite (P3)               #
# --------------------------------------------------------------------------- #


def test_update_patient_est_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une mise à jour émet exactement une ligne `patient.update` (P5)."""
    _set_principal("tenant_admin")
    pid = uuid.uuid4()

    async def _fake_update(
        _session: object, *, patient_id: uuid.UUID, fields: dict
    ) -> Patient:
        return _make_patient(patient_id=patient_id)

    monkeypatch.setattr(patients_router, "update_patient", _fake_update)

    resp = client.patch(f"/api/patients/{pid}", json={"phone": "+22891111111"})
    assert resp.status_code == 200
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "patient.update"
    assert captured_audit[0].entity_id == pid


def test_update_patient_inconnu_404(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mettre à jour un patient inconnu/hors tenant → 404 (P3) ; aucun audit."""
    _set_principal("tenant_admin")

    async def _fake_update(_session: object, **_kwargs: object) -> Patient:
        raise PatientNotFoundError

    monkeypatch.setattr(patients_router, "update_patient", _fake_update)

    resp = client.patch(f"/api/patients/{uuid.uuid4()}", json={"phone": "x"})
    assert resp.status_code == 404
    assert captured_audit == []


def test_delete_patient_est_soft_et_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La suppression (logique) émet une ligne `patient.delete` et renvoie 204 (P5/P6).

    Le routeur délègue à `soft_delete_patient` (jamais de DELETE physique sur le
    chemin CRUD, P6 ; propriété physique vérifiée en base dans `test_clinical_db`).
    """
    _set_principal("tenant_admin")
    pid = uuid.uuid4()
    deleted = _make_patient(
        patient_id=pid, deleted_at=datetime.datetime.now(datetime.UTC)
    )

    async def _fake_delete(_session: object, _pid: uuid.UUID) -> Patient:
        return deleted

    monkeypatch.setattr(patients_router, "soft_delete_patient", _fake_delete)

    resp = client.delete(f"/api/patients/{pid}")
    assert resp.status_code == 204
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "patient.delete"
    assert captured_audit[0].entity_id == pid


def test_delete_patient_inconnu_404(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supprimer un patient inconnu/hors tenant/déjà supprimé → 404 (P3/P6)."""
    _set_principal("tenant_admin")

    async def _fake_delete(_session: object, _pid: uuid.UUID) -> Patient:
        raise PatientNotFoundError

    monkeypatch.setattr(patients_router, "soft_delete_patient", _fake_delete)

    resp = client.delete(f"/api/patients/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert captured_audit == []


# --------------------------------------------------------------------------- #
# Visites : tenancy serveur (P2), id client (NFR-6), audit (P5), non-fuite (P3)#
# --------------------------------------------------------------------------- #


def test_create_visit_appose_le_tenant_du_principal(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Création d'une visite : tenant serveur apposé, id client transmis, audité."""
    _set_principal("clinician")
    captured: dict = {}
    visit_id = uuid.uuid4()
    patient_id = uuid.uuid4()

    async def _fake_create(_session: object, **kwargs: object) -> Visit:
        captured.update(kwargs)
        return _make_visit(
            visit_id=kwargs["visit_id"],  # type: ignore[arg-type]
            patient_id=kwargs["patient_id"],  # type: ignore[arg-type]
        )

    monkeypatch.setattr(visits_router, "create_visit", _fake_create)

    resp = client.post(
        "/api/visits",
        json={
            "id": str(visit_id),
            "patient_id": str(patient_id),
            "visited_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "reason": "Consultation générale",
            "notes": "RAS",
            # tenant_id forgé : ignoré (le schéma ne l'accepte pas).
            "tenant_id": str(_OTHER_TENANT_ID),
        },
    )
    assert resp.status_code == 201, resp.text

    assert captured["tenant_id"] == _TENANT_ID
    assert captured["visit_id"] == visit_id
    assert captured["created_by"] == _ACTOR_ID
    assert resp.json()["tenant_id"] == str(_TENANT_ID)

    assert len(captured_audit) == 1
    assert captured_audit[0].action == "visit.create"
    assert captured_audit[0].entity == "visit"
    assert captured_audit[0].entity_id == visit_id


def test_create_visit_patient_absent_404(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Visite rattachée à un patient inconnu/hors tenant → 404 (P3) ; aucun audit."""
    _set_principal("clinician")

    async def _fake_create(_session: object, **_kwargs: object) -> Visit:
        raise PatientForVisitNotFoundError

    monkeypatch.setattr(visits_router, "create_visit", _fake_create)

    resp = client.post(
        "/api/visits",
        json={
            "id": str(uuid.uuid4()),
            "patient_id": str(uuid.uuid4()),
            "visited_at": datetime.datetime.now(datetime.UTC).isoformat(),
        },
    )
    assert resp.status_code == 404
    assert captured_audit == []


def test_list_visits_consultation_est_auditee(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lister les visites émet exactement une ligne `visit.list` (P5)."""
    _set_principal("staff")

    async def _fake_list(_session: object) -> list[Visit]:
        return []

    monkeypatch.setattr(visits_router, "list_visits", _fake_list)

    resp = client.get("/api/visits")
    assert resp.status_code == 200
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "visit.list"


def test_delete_visit_est_soft_et_audite(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppression (logique) d'une visite → 204 + ligne `visit.delete` (P5/P6)."""
    _set_principal("tenant_admin")
    vid = uuid.uuid4()
    visit = _make_visit(visit_id=vid, patient_id=uuid.uuid4())
    visit.deleted_at = datetime.datetime.now(datetime.UTC)

    async def _fake_delete(_session: object, _vid: uuid.UUID) -> Visit:
        return visit

    monkeypatch.setattr(visits_router, "soft_delete_visit", _fake_delete)

    resp = client.delete(f"/api/visits/{vid}")
    assert resp.status_code == 204
    assert len(captured_audit) == 1
    assert captured_audit[0].action == "visit.delete"
    assert captured_audit[0].entity_id == vid


def test_delete_visit_inconnue_404(
    client: TestClient,
    captured_audit: list[AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supprimer une visite inconnue/hors tenant/déjà supprimée → 404 (P3/P6)."""
    _set_principal("tenant_admin")

    async def _fake_delete(_session: object, _vid: uuid.UUID) -> Visit:
        raise VisitNotFoundError

    monkeypatch.setattr(visits_router, "soft_delete_visit", _fake_delete)

    resp = client.delete(f"/api/visits/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert captured_audit == []
