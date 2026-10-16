"""Smoke tests du helper d'audit (tâche 5.2) — NFR-3, vers P5.

Ces tests ne touchent PAS la base : la persistance réelle, l'immutabilité et la
non-perte des événements hors ligne sont vérifiées par les tests d'intégration
(`test_audit_log.py`) contre un vrai PostgreSQL. On vérifie ici, via une fausse
session capturant l'INSERT, que le helper :

- émet un INSERT **paramétré** (valeurs liées, pas d'interpolation) sans jamais
  fixer `received_at` (laissé au défaut SQL serveur, NFR-3) ;
- **préserve** `event_at` quand il est fourni (événement hors ligne téléversé) ;
- **appose l'instant courant** comme `event_at` quand il est absent (événement né
  côté serveur) ;
- accepte un `tenant_id=None` (événement de niveau plateforme) tel quel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import Request

from app.core import audit
from app.schemas.audit import AuditEvent


class _FakeSession:
    """Session factice : capture le dernier `execute(statement, params)`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, statement: object, params: dict[str, object]) -> None:
        self.calls.append((str(statement), params))


async def test_record_audit_insere_sans_fixer_received_at() -> None:
    """L'INSERT ne fixe jamais `received_at` (défaut SQL serveur, NFR-3)."""
    session = _FakeSession()
    await audit.record_audit(
        session,  # type: ignore[arg-type]
        AuditEvent(action="login.success", tenant_id=uuid.uuid4()),
    )
    sql, params = session.calls[0]
    assert "INSERT INTO audit_log" in sql
    # Le serveur appose received_at : il n'apparaît ni dans les colonnes ni dans
    # les paramètres liés.
    assert "received_at" not in sql
    assert "received_at" not in params


async def test_record_audit_preserve_event_at_fourni() -> None:
    """Un `event_at` fourni (événement hors ligne) est préservé tel quel."""
    session = _FakeSession()
    offline_moment = datetime(2024, 1, 2, 8, 30, tzinfo=UTC)
    await audit.record_audit(
        session,  # type: ignore[arg-type]
        AuditEvent(
            action="patient.create",
            tenant_id=uuid.uuid4(),
            entity="patient",
            entity_id=uuid.uuid4(),
            event_at=offline_moment,
        ),
    )
    _, params = session.calls[0]
    assert params["event_at"] == offline_moment


async def test_record_audit_stampe_event_at_si_absent() -> None:
    """Sans `event_at` (événement serveur), le helper pose l'instant courant."""
    session = _FakeSession()
    before = datetime.now(UTC)
    await audit.record_audit(
        session,  # type: ignore[arg-type]
        AuditEvent(action="tenant.create"),
    )
    after = datetime.now(UTC)
    _, params = session.calls[0]
    stamped = params["event_at"]
    assert isinstance(stamped, datetime)
    assert before <= stamped <= after


async def test_record_audit_evenement_plateforme_tenant_null() -> None:
    """Un événement de niveau plateforme porte `tenant_id=None` tel quel."""
    session = _FakeSession()
    await audit.record_audit(
        session,  # type: ignore[arg-type]
        AuditEvent(action="admin.tenant.create", entity="tenant"),
    )
    _, params = session.calls[0]
    assert params["tenant_id"] is None


async def test_record_offline_events_journalise_chaque_evenement() -> None:
    """Chaque événement hors ligne téléversé produit un INSERT (aucune perte)."""
    session = _FakeSession()
    events = [
        AuditEvent(
            action="patient.create",
            tenant_id=uuid.uuid4(),
            event_at=datetime(2024, 1, 1, 9, 0, tzinfo=UTC),
        ),
        AuditEvent(
            action="visit.create",
            tenant_id=uuid.uuid4(),
            event_at=datetime(2024, 1, 1, 9, 5, tzinfo=UTC),
        ),
    ]
    count = await audit.record_offline_events(session, events)  # type: ignore[arg-type]
    assert count == 2
    assert len(session.calls) == 2


# --- Câblage transversal : capture de l'IP source et build_event --------------


def _fake_request(
    *, headers: dict[str, str] | None = None, client_host: str | None = "203.0.113.7"
) -> object:
    """Construit une fausse requête FastAPI minimale (headers + pair réseau).

    `Request` lit `scope["headers"]` (liste de tuples d'octets, en minuscules)
    et `scope["client"]` (tuple `(host, port)` ou None). On n'instancie donc pas
    un serveur ; on fournit juste le scope ASGI dont `client_ip` a besoin.
    """
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": raw_headers,
        "client": (client_host, 12345) if client_host is not None else None,
    }
    return Request(scope)


def test_client_ip_utilise_le_pair_direct_sans_proxy() -> None:
    """Sans `X-Forwarded-For`, l'IP est celle du pair direct (dev local)."""
    request = _fake_request(client_host="198.51.100.42")
    assert audit.client_ip(request) == "198.51.100.42"


def test_client_ip_prefere_la_premiere_adresse_de_x_forwarded_for() -> None:
    """Derrière le proxy TLS, l'IP d'origine est la première de l'en-tête."""
    request = _fake_request(
        headers={"X-Forwarded-For": "203.0.113.9, 70.41.3.18, 150.172.238.178"},
        client_host="10.0.0.1",  # IP du proxy — ne doit PAS être retenue.
    )
    assert audit.client_ip(request) == "203.0.113.9"


def test_client_ip_none_si_aucun_pair_ni_entete() -> None:
    """Sans pair ni en-tête (certains clients de test), l'IP est None."""
    request = _fake_request(client_host=None)
    assert audit.client_ip(request) is None


def test_build_event_capture_ip_et_champs() -> None:
    """`build_event` capture l'IP source et porte les champs fournis."""
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    request = _fake_request(client_host="192.0.2.55")

    event = audit.build_event(
        request,
        action="patient.view",
        tenant_id=tenant_id,
        actor_user_id=actor_id,
        entity="patient",
        entity_id=entity_id,
    )

    assert event.action == "patient.view"
    assert event.tenant_id == tenant_id
    assert event.actor_user_id == actor_id
    assert event.entity == "patient"
    assert event.entity_id == entity_id
    assert event.ip == "192.0.2.55"
    # Événement né côté serveur : event_at reste None (le helper d'écriture le
    # posera à l'instant courant).
    assert event.event_at is None


def test_build_event_preserve_event_at_hors_ligne() -> None:
    """Un `event_at` hors ligne fourni à `build_event` est préservé."""
    offline_moment = datetime(2024, 3, 4, 7, 15, tzinfo=UTC)
    request = _fake_request()
    event = audit.build_event(
        request,
        action="patient.create",
        tenant_id=uuid.uuid4(),
        event_at=offline_moment,
    )
    assert event.event_at == offline_moment
