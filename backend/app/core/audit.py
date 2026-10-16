"""Écrivain d'audit transversal, distribué-compatible (tâche 5.2).

NFR-3 — vérifie P5 (complétude de l'audit). Ce module expose le **chemin
d'écriture** unique que les couches ultérieures (auth tâche 7, PHI tâche 9, sync
tâche 10) utiliseront pour journaliser de façon cohérente, ainsi que l'ingestion
des **événements hors ligne téléversés** à la synchronisation.

Garanties de conception (section « Journal d'audit (append-only, écriture
toujours) » et « Error handling ») :

1. **Écrivain NON contraint par le contexte de tenant.** `audit_log` ne porte
   aucune politique RLS par-tenant (migration 0004) : une insertion réussit donc
   quelle que soit la valeur de `app.tenant_id` posée pour la requête courante.
   En particulier, un événement de niveau plateforme (`tenant_id = NULL`)
   **persiste toujours**, même si un contexte tenant masquerait par ailleurs ces
   lignes. On n'a donc pas besoin de dé-poser le GUC : l'absence de politique
   suffit, et c'est ce que vérifient les tests (tâche 5.3).

2. **Audit distribué.** `event_at` porté par l'événement (horodatage CLIENT) est
   **préservé tel quel** — essentiel pour les événements PHI hors ligne téléversés
   à la sync, qui se sont produits avant la reconnexion. Le serveur appose
   `received_at` à la réception (défaut SQL `now()` de la table). Aucun événement
   hors ligne n'est perdu : chaque entrée téléversée produit une ligne.

3. **Fail-closed pour les mutations auditées.** `record_audit` écrit dans la
   **même session/transaction** que la mutation appelante : si l'INSERT d'audit
   échoue, l'exception remonte et fait échouer toute la transaction (la mutation
   est annulée). On ne « ravale » jamais une erreur d'écriture d'audit — c'est la
   condition de P5. Les appelants NE DOIVENT donc PAS encapsuler `record_audit`
   dans un `try/except` qui masquerait l'échec.

Ce module n'implémente pas l'auth (tâche 7) : l'`actor_user_id` et le `tenant_id`
sont fournis par l'appelant (dérivés plus tard du jeton vérifié). Pour le reste
du **câblage transversal**, il fournit deux joints stables :

- `client_ip(request)` dérive l'IP source de la requête (gère `X-Forwarded-For`
  derrière le proxy TLS de production) ;
- `build_event(request, …)` construit un `AuditEvent` cohérent capturant cette IP,
  afin que les couches ultérieures (auth, PHI, sync) journalisent de façon
  uniforme sans ré-extraire l'IP ni reconstruire l'enveloppe à la main.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.schemas.audit import AuditEvent

# INSERT paramétré partagé par les deux chemins (session fournie / autonome).
# `received_at` n'est pas listé : il prend le défaut SQL `now()` de la table
# (horodatage SERVEUR de réception, NFR-3). Les valeurs sont **liées** comme
# paramètres (aucune interpolation : pas d'injection).
_INSERT_AUDIT = text(
    """
    INSERT INTO audit_log
        (tenant_id, actor_user_id, action, entity, entity_id, event_at, ip)
    VALUES
        (:tenant_id, :actor_user_id, :action, :entity, :entity_id, :event_at, :ip)
    """
)


def _event_params(event: AuditEvent) -> dict[str, object | None]:
    """Transforme un `AuditEvent` en paramètres liés pour l'INSERT.

    `event_at` est préservé s'il est fourni (événement hors ligne téléversé) ;
    sinon on pose l'instant courant (événement né côté serveur). On ne touche
    jamais à `received_at` : la base l'appose (NFR-3).
    """
    event_at = event.event_at or datetime.now(UTC)
    return {
        "tenant_id": event.tenant_id,
        "actor_user_id": event.actor_user_id,
        "action": event.action,
        "entity": event.entity,
        "entity_id": event.entity_id,
        "event_at": event_at,
        "ip": str(event.ip) if event.ip is not None else None,
    }


async def record_audit(session: AsyncSession, event: AuditEvent) -> None:
    """Journalise un événement dans la transaction **courante** (fail-closed).

    À utiliser pour toute **mutation auditée** : l'écriture partage la session et
    la transaction de la mutation, de sorte qu'un échec d'audit annule la
    mutation (P5). Ne JAMAIS envelopper cet appel dans un `try/except` qui
    masquerait l'échec — laisser l'exception remonter.

    L'insertion n'est pas bridée par `app.tenant_id` (aucune RLS par-tenant sur
    `audit_log`) : un événement plateforme (`tenant_id=None`) persiste même sous
    un contexte tenant.
    """
    await session.execute(_INSERT_AUDIT, _event_params(event))


async def record_audit_standalone(event: AuditEvent) -> None:
    """Journalise un événement dans sa **propre** transaction autonome.

    À utiliser pour les événements qui n'appartiennent pas à une mutation
    englobante — typiquement les événements d'authentification (succès/échec de
    connexion), où il faut une ligne d'audit même quand aucune donnée métier
    n'est modifiée. Ouvre une session dédiée, insère, puis valide.

    Reste distribué-compatible (préserve `event_at`) et non contraint par un
    contexte de tenant (aucun `SET LOCAL app.tenant_id` posé ici).
    """
    async with async_session_factory() as session, session.begin():
        await session.execute(_INSERT_AUDIT, _event_params(event))


async def record_offline_events(
    session: AsyncSession, events: Iterable[AuditEvent]
) -> int:
    """Ingère des événements d'audit **hors ligne téléversés** (audit distribué).

    Chaque événement conserve son `event_at` client ; le serveur appose
    `received_at` à l'insertion. Écrit dans la transaction fournie (même
    sémantique fail-closed que `record_audit` : aucun événement hors ligne n'est
    silencieusement perdu — un échec annule le lot). Renvoie le nombre
    d'événements journalisés.
    """
    count = 0
    for event in events:
        await session.execute(_INSERT_AUDIT, _event_params(event))
        count += 1
    return count


# --- Câblage transversal : capture de l'IP source et construction cohérente ----


def client_ip(request: Request) -> str | None:
    """Dérive l'IP source de la requête pour l'audit (`AuditEvent.ip`).

    En production, TLS est terminé au niveau d'un proxy (voir design, « Conception
    sécurité et audit ») : l'adresse de pair vue par l'app est alors celle du
    proxy, et l'IP réelle du client figure dans l'en-tête `X-Forwarded-For`. On
    privilégie donc la **première** adresse de `X-Forwarded-For` (le client
    d'origine ; les suivants sont les proxys traversés), puis on retombe sur
    l'adresse de pair directe (`request.client.host`) en l'absence d'en-tête (dev
    local sans proxy).

    Renvoie `None` si aucune IP n'est déterminable (p. ex. transport sans pair,
    comme certains clients de test). `AuditEvent.ip` étant optionnel, une IP
    absente ne doit jamais faire échouer la journalisation — seul un échec
    d'écriture le fait (fail-closed, P5).
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # `X-Forwarded-For: client, proxy1, proxy2` — l'origine est en tête.
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return _valid_ip_or_none(first)
    client = request.client
    return _valid_ip_or_none(client.host) if client is not None else None


def _valid_ip_or_none(candidate: str | None) -> str | None:
    """Renvoie `candidate` si c'est une IP valide, sinon `None`.

    La colonne `audit_log.ip` est de type `inet` : une valeur non-IP (hôte
    symbolique d'un client de test, socket de domaine Unix, en-tête proxy mal
    formé) ferait échouer l'INSERT — et, l'audit étant fail-closed (P5), toute la
    mutation auditée avec elle. Comme `ip` est **optionnel** (NFR-3 n'exige pas une
    IP toujours résoluble), on préfère journaliser l'événement avec `ip = NULL`
    plutôt que de perdre l'événement. On ne laisse jamais une IP non résoluble
    compromettre la complétude de l'audit.
    """
    if not candidate:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def build_event(
    request: Request,
    *,
    action: str,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    entity: str | None = None,
    entity_id: uuid.UUID | None = None,
    event_at: datetime | None = None,
) -> AuditEvent:
    """Construit un `AuditEvent` cohérent à partir du contexte de requête.

    Joint transversal destiné aux couches ultérieures (auth tâche 7, PHI tâche 9,
    sync tâche 10) : il garantit que **tout** événement né d'une requête HTTP
    capture l'IP source de façon uniforme (via `client_ip`) et porte les mêmes
    champs d'acteur/tenant/entité. L'appelant fournit l'`actor_user_id` et le
    `tenant_id` dérivés du jeton vérifié ; il n'a pas à ré-extraire l'IP ni à
    reconstruire l'enveloppe à la main.

    `event_at` reste optionnel : absent pour un événement né côté serveur (le
    helper d'écriture posera l'instant courant) ; fourni pour un événement hors
    ligne téléversé (il sera préservé tel quel, NFR-3).
    """
    return AuditEvent(
        action=action,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        entity=entity,
        entity_id=entity_id,
        event_at=event_at,
        ip=client_ip(request),
    )
