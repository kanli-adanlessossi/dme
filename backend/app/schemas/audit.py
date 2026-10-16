"""Schémas de l'audit distribué (tâche 5.2) — NFR-3, vérifie P5.

Ce module gèle la forme d'un **événement d'audit** que les couches ultérieures
(auth, PHI, sync) produiront de manière cohérente, ainsi que la forme d'un
**événement hors ligne téléversé** à la synchronisation.

Points de conception (section « Journal d'audit (append-only, écriture
toujours) ») :

- Un événement d'audit capture : l'acteur (`actor_user_id`), le tenant
  (`tenant_id`, NUL pour une action de niveau plateforme), l'`action`, l'entité
  visée (`entity` / `entity_id`), l'horodatage de l'événement (`event_at`) et
  l'IP source (`ip`).
- `event_at` est l'horodatage **client** : pour un événement né côté serveur,
  l'appelant passe l'instant courant ; pour un événement hors ligne téléversé, le
  client transmet le moment réel où l'action a eu lieu. Le serveur n'écrase
  jamais `event_at` ; il appose `received_at` à l'insertion (NFR-3).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AuditEvent(BaseModel):
    """Événement d'audit à journaliser (une entrée `audit_log`).

    `tenant_id` est optionnel : NUL => action de niveau plateforme (jamais
    masquée par la RLS, l'écrivain n'étant pas contraint par `app.tenant_id`).
    `event_at` est optionnel côté API : s'il est absent (événement né côté
    serveur), le helper le fixe à l'instant courant ; s'il est fourni (événement
    hors ligne téléversé), il est **préservé tel quel**.
    """

    action: str = Field(
        ...,
        description="Action auditée (p. ex. 'login.success', 'patient.create').",
        examples=["patient.create"],
    )
    tenant_id: UUID | None = Field(
        default=None,
        description="Tenant concerné ; NUL pour une action de niveau plateforme.",
    )
    actor_user_id: UUID | None = Field(
        default=None,
        description="Utilisateur à l'origine de l'action, si connu.",
    )
    entity: str | None = Field(
        default=None,
        description="Type d'entité visée (p. ex. 'patient', 'tenant').",
    )
    entity_id: UUID | None = Field(
        default=None, description="Identifiant de l'entité visée, si applicable."
    )
    event_at: datetime | None = Field(
        default=None,
        description="Horodatage CLIENT de l'événement (hors ligne). Si absent, le "
        "serveur utilise l'instant courant ; s'il est fourni, il est préservé.",
    )
    ip: str | None = Field(
        default=None, description="Adresse IP source de la requête, si connue."
    )
