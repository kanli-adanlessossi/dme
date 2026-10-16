"""Protocole de synchronisation local-first (tâche 3) — contrat figé (NFR-13).

Ce module gèle le protocole de sync afin que le frontend puisse construire son
moteur outbox + delta-sync contre une interface stable, en parallèle du backend.

Deux points d'accès (auth requise ; contexte RLS ; audité) :

- `POST /api/sync/push` — applique un **lot idempotent** de mutations issues de
  l'outbox du client. L'idempotence est portée par le `mutation_id` (UUID) :
  rejouer une mutation déjà appliquée est sans effet (NFR-6, P10). La RLS
  (`WITH CHECK`) empêche l'injection inter-tenant (P11).
- `GET /api/sync/pull?since=<curseur>` — renvoie les **deltas** du tenant depuis
  le curseur fourni, plus un **nouveau curseur** à présenter au prochain appel
  (delta-sync, cadré RLS, P11).

Formats figés :

- **Enveloppe de mutation** : `mutation_id` (UUID d'idempotence), `entity`
  (type d'entité), `entity_id` (UUIDv7 de l'entité), `op` (upsert/delete),
  `payload`, `client_version`, `client_updated_at` et `event_at`.
- **Curseur** : chaîne opaque pour le client. Encodage de référence =
  `"<received_at ISO-8601>|<entity_id>"` (horodatage de réception serveur +
  départage par id), permettant une pagination monotone des deltas. Le client ne
  DOIT PAS l'interpréter ; il le renvoie tel quel.
- **Conflit** : `entity`, `entity_id`, `loser_payload`, `winner_version` et
  `sensitive` (true => revue humaine requise pour une entité clinique sensible,
  NFR-6 / P12).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class SyncEntity(str, Enum):
    """Types d'entités synchronisables (granularité enregistrement)."""

    patient = "patient"
    visit = "visit"


class SyncOp(str, Enum):
    """Opération portée par une mutation.

    `upsert` crée ou met à jour (clé = `entity_id`) ; `delete` est une suppression
    logique (pose `deleted_at`), jamais physique (P6).
    """

    upsert = "upsert"
    delete = "delete"


class Mutation(BaseModel):
    """Enveloppe d'une mutation de l'outbox (unité d'idempotence).

    `mutation_id` est la clé d'idempotence : deux envois portant le même
    `mutation_id` produisent au plus une application (P10). `payload` contient
    l'état de l'entité pour un `upsert` ; il peut être vide pour un `delete`.
    `tenant_id` n'y figure jamais : le serveur l'appose depuis le jeton (P2).
    """

    mutation_id: UUID = Field(
        ...,
        description="UUID de la mutation — clé d'idempotence (NFR-6, P10).",
    )
    entity: SyncEntity
    entity_id: UUID = Field(
        ..., description="UUIDv7 de l'entité ciblée (généré côté client)."
    )
    op: SyncOp
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="État de l'entité pour un upsert ; vide pour un delete.",
    )
    client_version: int = Field(
        ..., ge=1, description="Version de l'entité connue du client (conflits)."
    )
    client_updated_at: datetime = Field(
        ..., description="Horodatage client de la mutation (base LWW)."
    )
    event_at: datetime = Field(
        ...,
        description="Horodatage de l'événement côté client, pour l'audit distribué "
        "(le serveur ajoute received_at à la réception, NFR-3).",
    )


class MutationResultStatus(str, Enum):
    """Issue de l'application d'une mutation."""

    applied = "applied"
    duplicate = "duplicate"  # déjà appliquée (rejeu idempotent) — sans effet
    conflict = "conflict"  # résolue en LWW ; perdant journalisé
    rejected = "rejected"  # refusée (p. ex. WITH CHECK RLS, validation)


class SyncConflict(BaseModel):
    """Conflit détecté lors de l'application d'une mutation (NFR-6, P12).

    Le perdant LWW est conservé (`loser_payload`) et consigné ; il n'est jamais
    silencieusement détruit. `sensitive=true` signale une entité cliniquement
    sensible (p. ex. notes de visite) à faire revoir par un humain plutôt que
    d'écraser.
    """

    entity: SyncEntity
    entity_id: UUID
    loser_payload: dict[str, Any] = Field(
        ..., description="Version perdante conservée pour revue/traçabilité."
    )
    winner_version: int = Field(
        ..., ge=1, description="Version gagnante retenue après résolution LWW."
    )
    sensitive: bool = Field(
        ...,
        description="true => conflit à revoir par un humain (entité sensible).",
    )


class MutationResult(BaseModel):
    """Résultat d'application d'une mutation, corrélé par `mutation_id`."""

    mutation_id: UUID
    entity: SyncEntity
    entity_id: UUID
    status: MutationResultStatus
    server_version: int | None = Field(
        None, ge=1, description="Version serveur après application (si applicable)."
    )
    conflict: SyncConflict | None = Field(
        None, description="Détail du conflit si `status == conflict`."
    )
    message: str | None = Field(
        None, description="Explication en cas de `rejected`."
    )


class SyncPushRequest(BaseModel):
    """Lot de mutations poussées depuis l'outbox (idempotent par mutation)."""

    mutations: list[Mutation] = Field(
        default_factory=list,
        description="Mutations de l'outbox à appliquer dans l'ordre fourni.",
    )


class SyncPushResponse(BaseModel):
    """Résultats d'un push : un résultat par mutation + conflits détectés.

    `conflicts` agrège les conflits présents dans `results` pour que le client
    puisse les surfacer directement (ConflictBanner) sans réparcourir la liste.
    """

    results: list[MutationResult] = Field(default_factory=list)
    conflicts: list[SyncConflict] = Field(default_factory=list)


class SyncChange(BaseModel):
    """Un enregistrement de delta renvoyé par le pull.

    `deleted=true` reflète une suppression logique (le client applique le
    `deleted_at`). `payload` porte l'état courant de l'entité pour un upsert.
    """

    entity: SyncEntity
    entity_id: UUID
    op: SyncOp
    payload: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(..., ge=1)
    updated_at: datetime
    deleted: bool = Field(default=False)


class SyncPullResponse(BaseModel):
    """Deltas du tenant depuis le curseur fourni + nouveau curseur.

    `cursor` est opaque pour le client : il le conserve et le renvoie tel quel au
    prochain pull. `has_more` indique qu'un pull immédiat ramènera d'autres
    deltas (pagination). `revoked` propage une révocation d'accès (tenant/
    utilisateur désactivé) à appliquer côté client en purgeant la session locale
    (NFR-7).
    """

    changes: list[SyncChange] = Field(default_factory=list)
    cursor: str = Field(
        ...,
        description="Nouveau curseur opaque à renvoyer au prochain pull.",
        examples=["2025-01-15T10:30:00Z|018f8b2c-7c1a-7a2e-9f3b-2c1a7c1a7a2e"],
    )
    has_more: bool = Field(
        default=False, description="true s'il reste des deltas à récupérer."
    )
    revoked: bool = Field(
        default=False,
        description="true si l'accès a été révoqué : le client purge sa session "
        "et ses PHI locales (NFR-7).",
    )
