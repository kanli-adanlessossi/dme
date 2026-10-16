"""Moteur de synchronisation côté serveur (tâche 10) — push/pull, LWW, conflits.

Exigences 4.2, 4.3, 4.4, 4.6, NFR-3, NFR-6, NFR-13 — vérifie P10, P11, P12.

Ce service porte la **logique métier** de la synchronisation local-first (design,
section « Synchronisation »), appelée par le routeur `sync`. Il opère toujours sur
une **session déjà cadrée au tenant** (`tenant_session`, tâche 4.3) : le
`SET LOCAL app.tenant_id` étant posé en amont, la RLS forcée (migrations
0006/0007) filtre automatiquement toutes les lectures et rejette toute écriture
forgée pour un autre tenant (`WITH CHECK`), ce qui réalise l'isolation à la sync
(P11). Le `tenant_id` écrit provient du **principal authentifié**, jamais du corps
d'une mutation (P2).

Garanties (design, « Correctness Properties ») :

- **P10 — idempotence et absence de perte.** Chaque mutation porte un
  `mutation_id` (UUID). Avant d'appliquer, on consulte le registre
  `sync_mutation` : si le `mutation_id` y figure déjà, la mutation a déjà été
  appliquée (rejeu après coupure) → on renvoie l'issue mémorisée (`duplicate`)
  **sans ré-appliquer**. L'insertion du registre partage la transaction de la
  mutation métier : les deux réussissent ou échouent ensemble (au plus une
  application).

- **P11 — isolation maintenue à la sync.** `pull` ne renvoie que les deltas du
  tenant courant (RLS) ; `push` écrit sous `WITH CHECK` RLS. Un `mutation_id` est
  cadré au tenant (clé primaire + RLS) : impossible de rejouer/injecter via le
  `mutation_id` d'un autre tenant.

- **P12 — résolution de conflits non destructive.** En cas de divergence, la règle
  par défaut est Last-Write-Wins sur `updated_at` (départage par `version`). Le
  **perdant** est consigné dans `sync_conflict` (jamais détruit silencieusement).
  Les entités **sensibles** (notes de visite, design question ouverte #7 : « a
  minima les notes de visite ») ne sont **jamais écrasées** en cas de conflit :
  le conflit est marqué `sensitive = true` pour revue humaine, l'état serveur est
  conservé et la version cliente entrante est consignée comme perdante.

Modèle de conflit (granularité enregistrement, cf. design) : un conflit est
détecté quand le client a fondé son édition sur une version **antérieure ou
égale** à celle déjà connue du serveur (`client_version <= server_version`) — deux
éditions concurrentes de la même ligne. Un `client_version > server_version` est
un *fast-forward* propre (le client avait l'état serveur courant) → appliqué sans
conflit.

Format de curseur (figé en tâche 3, opaque pour le client) :
``"<updated_at ISO-8601>|<entity_id>"`` — horodatage de dernière mutation serveur
+ départage par id, pour une pagination monotone et stable des deltas.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.patient import Patient
from app.models.sync_conflict import SyncConflict
from app.models.sync_mutation import SyncMutation
from app.models.visit import Visit
from app.schemas.sync import (
    Mutation,
    MutationResult,
    MutationResultStatus,
    SyncChange,
    SyncEntity,
    SyncOp,
)
from app.schemas.sync import SyncConflict as SyncConflictSchema

# Entités cliniquement **sensibles** (conflit marqué pour revue plutôt
# qu'écrasé, P12 / NFR-6). Décision de conception (design, question ouverte #7 :
# « a minima les notes de visite ») : la visite porte les notes cliniques, elle
# est donc sensible. Le patient (données d'état civil) reste en LWW simple.
_SENSITIVE_ENTITIES: frozenset[SyncEntity] = frozenset({SyncEntity.visit})

# Taille maximale d'une page de deltas renvoyée par un pull (pagination).
_PULL_PAGE_SIZE = 500

# Modèles ORM par entité synchronisable.
_ENTITY_MODELS: dict[SyncEntity, type[Patient | Visit]] = {
    SyncEntity.patient: Patient,
    SyncEntity.visit: Visit,
}


# --------------------------------------------------------------------------- #
# Sérialisation des payloads (entité ORM <-> dict JSON-compatible)             #
# --------------------------------------------------------------------------- #

# Champs exposés par entité dans les payloads de sync (deltas + perdants). On
# n'expose jamais de champ hors de ce périmètre ; `tenant_id` n'y figure pas (il
# est apposé par le serveur et jamais répliqué vers un autre tenant, P2/P11).
_PATIENT_FIELDS = (
    "mrn",
    "given_name",
    "family_name",
    "dob",
    "sex",
    "phone",
    "national_id",
    "consent_to_store",
)
_VISIT_FIELDS = ("patient_id", "visited_at", "reason", "notes")


def _entity_fields(entity: SyncEntity) -> tuple[str, ...]:
    """Champs métier publics d'une entité synchronisable."""
    return _PATIENT_FIELDS if entity is SyncEntity.patient else _VISIT_FIELDS


def _jsonable(value: Any) -> Any:
    """Rend une valeur sérialisable en JSON (dates/UUID -> str)."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _entity_payload(entity: SyncEntity, row: Patient | Visit) -> dict[str, Any]:
    """Projette une ligne ORM vers un payload de sync (champs publics de l'entité)."""
    return {name: _jsonable(getattr(row, name)) for name in _entity_fields(entity)}


# Types de colonnes des champs typés par entité : le payload de mutation arrive en
# JSON (chaînes ISO pour dates/UUID), mais les colonnes ORM/PostgreSQL attendent
# des objets Python (`date`/`datetime`/`UUID`). On coerce donc à l'écriture, là où
# le CRUD HTTP s'appuyait sur Pydantic — ici le payload est un dict brut.
_PATIENT_FIELD_TYPES: dict[str, str] = {"dob": "date"}
_VISIT_FIELD_TYPES: dict[str, str] = {
    "patient_id": "uuid",
    "visited_at": "datetime",
}


def _coerce_field(entity: SyncEntity, name: str, value: Any) -> Any:
    """Coerce une valeur de payload vers le type Python attendu par la colonne.

    `None` et les valeurs déjà typées passent tels quels. Les chaînes ISO des
    champs date/datetime/UUID sont converties ; un format invalide lève (la
    transaction échoue proprement plutôt que d'écrire une donnée corrompue).
    """
    if value is None:
        return None
    field_types = (
        _PATIENT_FIELD_TYPES if entity is SyncEntity.patient else _VISIT_FIELD_TYPES
    )
    kind = field_types.get(name)
    if kind is None or not isinstance(value, str):
        return value
    if kind == "date":
        return datetime.date.fromisoformat(value)
    if kind == "datetime":
        return datetime.datetime.fromisoformat(value)
    if kind == "uuid":
        return uuid.UUID(value)
    return value


# --------------------------------------------------------------------------- #
# PUSH : application idempotente d'un lot de mutations                         #
# --------------------------------------------------------------------------- #


@dataclass
class _PushContext:
    """Contexte d'un push : tenant/acteur du principal + collecte des résultats."""

    session: AsyncSession
    tenant_id: uuid.UUID
    actor_user_id: uuid.UUID
    results: list[MutationResult] = field(default_factory=list)
    conflicts: list[SyncConflictSchema] = field(default_factory=list)
    # Nombre de mutations réellement appliquées (hors rejeux) — pour l'audit.
    applied_count: int = 0


@dataclass(frozen=True)
class PushOutcome:
    """Résultat agrégé d'un push (résultats par mutation + conflits + compteur)."""

    results: list[MutationResult]
    conflicts: list[SyncConflictSchema]
    applied_count: int


async def apply_push(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    mutations: list[Mutation],
) -> PushOutcome:
    """Applique un lot de mutations de façon idempotente (P10) sous contexte RLS.

    Traite les mutations **dans l'ordre fourni**. Pour chacune :
    1. si son `mutation_id` figure déjà dans `sync_mutation` → `duplicate` (rejeu
       sans effet, P10) ;
    2. sinon on résout l'opération (upsert/delete) avec arbitrage LWW et détection
       de conflit, on consigne un éventuel perdant dans `sync_conflict` (P12), et
       on enregistre le `mutation_id` dans le registre d'idempotence.

    Ne valide PAS la transaction : le `commit` est assuré par la dépendance
    `tenant_session` en sortie nominale du handler, de sorte que mutations, entrée
    du registre d'idempotence, journal de conflits ET audit soient atomiques
    (fail-closed).
    """
    ctx = _PushContext(
        session=session, tenant_id=tenant_id, actor_user_id=actor_user_id
    )
    for mutation in mutations:
        await _apply_one(ctx, mutation)
    return PushOutcome(
        results=ctx.results,
        conflicts=ctx.conflicts,
        applied_count=ctx.applied_count,
    )


async def _apply_one(ctx: _PushContext, mutation: Mutation) -> None:
    """Applique une seule mutation (idempotence + LWW + conflit)."""
    # 1. Idempotence (P10) : un mutation_id déjà vu => rejeu sans effet.
    existing = await ctx.session.get(SyncMutation, mutation.mutation_id)
    if existing is not None:
        ctx.results.append(
            MutationResult(
                mutation_id=mutation.mutation_id,
                entity=mutation.entity,
                entity_id=mutation.entity_id,
                status=MutationResultStatus.duplicate,
                server_version=existing.server_version,
            )
        )
        return

    model = _ENTITY_MODELS[mutation.entity]
    # Charge l'état serveur courant (y compris supprimé logiquement : on a besoin
    # de sa version/updated_at pour l'arbitrage LWW). La RLS cadre déjà au tenant.
    server_row = await ctx.session.get(model, mutation.entity_id)

    if server_row is None:
        await _apply_create(ctx, mutation)
    else:
        await _apply_with_lww(ctx, mutation, server_row)


def _record_mutation(
    ctx: _PushContext,
    mutation: Mutation,
    *,
    status: MutationResultStatus,
    server_version: int | None,
) -> None:
    """Inscrit le `mutation_id` au registre d'idempotence (dans la transaction)."""
    ctx.session.add(
        SyncMutation(
            mutation_id=mutation.mutation_id,
            tenant_id=ctx.tenant_id,
            entity=mutation.entity.value,
            entity_id=mutation.entity_id,
            result_status=status.value,
            server_version=server_version,
        )
    )


def _finish(
    ctx: _PushContext,
    mutation: Mutation,
    status: MutationResultStatus,
    server_version: int | None,
    *,
    conflict: SyncConflictSchema | None = None,
) -> None:
    """Mémorise la mutation (idempotence) et agrège son résultat + conflit.

    Point de sortie unique de l'application d'une mutation : inscrit le
    `mutation_id` au registre (P10), ajoute le `MutationResult`, et agrège le
    conflit éventuel dans la liste de haut niveau (pour le ConflictBanner).
    """
    _record_mutation(ctx, mutation, status=status, server_version=server_version)
    ctx.results.append(
        MutationResult(
            mutation_id=mutation.mutation_id,
            entity=mutation.entity,
            entity_id=mutation.entity_id,
            status=status,
            server_version=server_version,
            conflict=conflict,
        )
    )
    if conflict is not None:
        ctx.conflicts.append(conflict)
    if status in (MutationResultStatus.applied, MutationResultStatus.conflict):
        ctx.applied_count += 1


async def _apply_create(ctx: _PushContext, mutation: Mutation) -> None:
    """Crée une entité absente côté serveur (upsert sans ligne préexistante).

    Un `delete` d'une entité inexistante est un no-op idempotent (consigné
    `applied`). Le serveur appose le `tenant_id` depuis le principal (P2) ; le
    `WITH CHECK` RLS est le garde-fou ultime.
    """
    if mutation.op is SyncOp.delete:
        # Supprimer une entité inexistante : rien à faire, mais on mémorise la
        # mutation pour l'idempotence (le rejeu renverra `duplicate`).
        _finish(ctx, mutation, MutationResultStatus.applied, None)
        return

    row = _build_entity(ctx, mutation)
    ctx.session.add(row)
    await ctx.session.flush()  # matérialise les défauts serveur si besoin
    _finish(ctx, mutation, MutationResultStatus.applied, row.version)


def _build_entity(ctx: _PushContext, mutation: Mutation) -> Patient | Visit:
    """Construit une entité ORM depuis le payload d'un upsert (création).

    Le serveur appose `tenant_id` (du principal) et `created_by` (acteur) ;
    `updated_at`/`version` prennent les horodatages/compteur de la mutation
    cliente (base LWW). La clé primaire est l'`entity_id` UUIDv7 du client (NFR-6).
    """
    payload = mutation.payload
    entity = mutation.entity

    def _field(name: str, default: Any = None) -> Any:
        return _coerce_field(entity, name, payload.get(name, default))

    common: dict[str, Any] = {
        "id": mutation.entity_id,
        "tenant_id": ctx.tenant_id,
        "created_by": ctx.actor_user_id,
        "updated_at": mutation.client_updated_at,
        "version": mutation.client_version,
    }
    if entity is SyncEntity.patient:
        return Patient(
            **common,
            mrn=_field("mrn"),
            given_name=_field("given_name"),
            family_name=_field("family_name"),
            dob=_field("dob"),
            sex=_field("sex"),
            phone=_field("phone"),
            national_id=_field("national_id"),
            consent_to_store=_field("consent_to_store", False),
        )
    return Visit(
        **common,
        patient_id=_field("patient_id"),
        visited_at=_field("visited_at"),
        reason=_field("reason"),
        notes=_field("notes"),
    )


async def _apply_with_lww(
    ctx: _PushContext, mutation: Mutation, server_row: Patient | Visit
) -> None:
    """Applique une mutation sur une entité existante avec arbitrage LWW (P12).

    Détection de conflit (granularité enregistrement) : il y a conflit quand le
    client a édité à partir d'une version antérieure ou égale à celle du serveur
    (`client_version <= server_version`) — éditions concurrentes. Sinon
    (`client_version > server_version`) c'est un fast-forward propre, appliqué sans
    conflit.

    En cas de conflit :
    - **entité sensible** (visite) → jamais d'écrasement : l'état serveur est
      conservé, la version cliente entrante est consignée comme perdante et
      marquée `sensitive=true` pour revue humaine ;
    - **entité non sensible** → Last-Write-Wins sur `updated_at` : le perdant
      (serveur ou client) est consigné, le gagnant est retenu.
    """
    is_conflict = mutation.client_version <= server_row.version

    if not is_conflict:
        # Fast-forward propre : le client partait de l'état serveur courant.
        _mutate_server_row(mutation, server_row)
        await ctx.session.flush()
        _finish(ctx, mutation, MutationResultStatus.applied, server_row.version)
        return

    sensitive = mutation.entity in _SENSITIVE_ENTITIES
    client_wins = _client_wins_lww(mutation, server_row)

    if sensitive or not client_wins:
        # On conserve l'état serveur ; le payload client entrant est le perdant.
        loser_payload = _incoming_payload(mutation)
        winner_version = server_row.version
        await _log_conflict(
            ctx,
            mutation,
            loser_payload=loser_payload,
            winner_version=winner_version,
            sensitive=sensitive,
        )
        _finish(
            ctx,
            mutation,
            MutationResultStatus.conflict,
            winner_version,
            conflict=_conflict_schema(
                mutation, loser_payload, winner_version, sensitive
            ),
        )
        return

    # Entité non sensible, le client gagne le LWW : l'état serveur devient le
    # perdant (conservé), on applique la mutation cliente.
    loser_payload = _entity_payload(mutation.entity, server_row)
    loser_payload["version"] = server_row.version
    loser_payload["updated_at"] = _jsonable(server_row.updated_at)
    _mutate_server_row(mutation, server_row)
    await ctx.session.flush()
    winner_version = server_row.version
    await _log_conflict(
        ctx,
        mutation,
        loser_payload=loser_payload,
        winner_version=winner_version,
        sensitive=False,
    )
    _finish(
        ctx,
        mutation,
        MutationResultStatus.conflict,
        winner_version,
        conflict=_conflict_schema(mutation, loser_payload, winner_version, False),
    )


def _client_wins_lww(mutation: Mutation, server_row: Patient | Visit) -> bool:
    """Vrai si la mutation cliente gagne le Last-Write-Wins sur `updated_at`.

    Comparaison sur `updated_at` ; en cas d'égalité stricte des horodatages, on
    départage par la version (version la plus élevée gagne) pour un résultat
    déterministe.
    """
    client_ts = mutation.client_updated_at
    server_ts = server_row.updated_at
    if client_ts != server_ts:
        return client_ts > server_ts
    return mutation.client_version > server_row.version


def _mutate_server_row(mutation: Mutation, server_row: Patient | Visit) -> None:
    """Applique l'upsert/delete du client sur la ligne serveur (gagnant retenu).

    Pose `updated_at`/`version` depuis la mutation cliente (base LWW) ; un `delete`
    pose `deleted_at`. `tenant_id`/`id` ne sont jamais modifiés (P2).
    """
    if mutation.op is SyncOp.delete:
        server_row.deleted_at = mutation.client_updated_at
    else:
        _assign_payload(mutation, server_row)
        server_row.deleted_at = None
    server_row.updated_at = mutation.client_updated_at
    server_row.version = mutation.client_version


def _assign_payload(mutation: Mutation, row: Patient | Visit) -> None:
    """Recopie les champs métier du payload d'upsert sur la ligne ORM (coercés)."""
    payload = mutation.payload
    for name in _entity_fields(mutation.entity):
        if name in payload:
            setattr(row, name, _coerce_field(mutation.entity, name, payload[name]))


def _incoming_payload(mutation: Mutation) -> dict[str, Any]:
    """Payload entrant du client (perdant conservé en cas de conflit sensible)."""
    data = dict(mutation.payload)
    data["version"] = mutation.client_version
    data["updated_at"] = _jsonable(mutation.client_updated_at)
    if mutation.op is SyncOp.delete:
        data["op"] = SyncOp.delete.value
    return data


async def _log_conflict(
    ctx: _PushContext,
    mutation: Mutation,
    *,
    loser_payload: dict[str, Any],
    winner_version: int,
    sensitive: bool,
) -> None:
    """Consigne le perdant du conflit dans `sync_conflict` (non destructif, P12)."""
    ctx.session.add(
        SyncConflict(
            tenant_id=ctx.tenant_id,
            entity=mutation.entity.value,
            entity_id=mutation.entity_id,
            loser_payload=loser_payload,
            winner_version=winner_version,
            sensitive=sensitive,
        )
    )
    await ctx.session.flush()


def _conflict_schema(
    mutation: Mutation,
    loser_payload: dict[str, Any],
    winner_version: int,
    sensitive: bool,
) -> SyncConflictSchema:
    """Construit la représentation API d'un conflit (pour le ConflictBanner)."""
    return SyncConflictSchema(
        entity=mutation.entity,
        entity_id=mutation.entity_id,
        loser_payload=loser_payload,
        winner_version=winner_version,
        sensitive=sensitive,
    )


# --------------------------------------------------------------------------- #
# PULL : deltas du tenant depuis le curseur + nouveau curseur                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Cursor:
    """Position de pull décodée : (updated_at, entity_id) du dernier delta rendu."""

    updated_at: datetime.datetime
    entity_id: uuid.UUID


@dataclass(frozen=True)
class PullResult:
    """Résultat d'un pull : deltas + nouveau curseur opaque + indicateur de suite."""

    changes: list[SyncChange]
    cursor: str
    has_more: bool


def _parse_cursor(raw: str | None) -> _Cursor | None:
    """Décode un curseur ``"<updated_at ISO-8601>|<entity_id>"`` (ou None).

    Opaque pour le client : un curseur absent/illisible est traité comme une
    synchronisation initiale (depuis l'origine). On ne lève jamais sur un curseur
    mal formé — on repart simplement du début, ce qui reste correct (le client
    recevra tous les deltas et dédoublonnera par `entity_id`).
    """
    if not raw:
        return None
    try:
        ts_part, id_part = raw.split("|", 1)
        return _Cursor(
            updated_at=datetime.datetime.fromisoformat(ts_part),
            entity_id=uuid.UUID(id_part),
        )
    except (ValueError, TypeError):
        return None


def _encode_cursor(updated_at: datetime.datetime, entity_id: uuid.UUID) -> str:
    """Encode un curseur opaque ``"<updated_at ISO-8601>|<entity_id>"``."""
    return f"{updated_at.isoformat()}|{entity_id}"


def _change_from_row(entity: SyncEntity, row: Patient | Visit) -> SyncChange:
    """Projette une ligne ORM vers un delta de pull (upsert ou suppression)."""
    deleted = row.deleted_at is not None
    return SyncChange(
        entity=entity,
        entity_id=row.id,
        op=SyncOp.delete if deleted else SyncOp.upsert,
        payload={} if deleted else _entity_payload(entity, row),
        version=row.version,
        updated_at=row.updated_at,
        deleted=deleted,
    )


async def _rows_since(
    session: AsyncSession,
    entity: SyncEntity,
    cursor: _Cursor | None,
) -> list[Patient | Visit]:
    """Charge les lignes du tenant modifiées depuis le curseur (RLS cadre au tenant).

    Les lignes supprimées logiquement sont **incluses** (le delta porte alors
    `deleted=true`) afin que la suppression se propage au client (NFR-6, P6). Tri
    monotone par `(updated_at, id)` pour une pagination stable ; au-delà de la
    page, `has_more` est positionné par l'appelant.
    """
    model = _ENTITY_MODELS[entity]
    stmt = select(model)
    if cursor is not None:
        # Deltas strictement après la position du curseur : updated_at postérieur,
        # ou même updated_at mais id strictement supérieur (départage stable).
        stmt = stmt.where(
            or_(
                model.updated_at > cursor.updated_at,
                (model.updated_at == cursor.updated_at)
                & (model.id > cursor.entity_id),
            )
        )
    stmt = stmt.order_by(model.updated_at, model.id).limit(_PULL_PAGE_SIZE + 1)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def pull_deltas(
    session: AsyncSession,
    *,
    since: str | None,
) -> PullResult:
    """Renvoie les deltas du tenant depuis `since` + un nouveau curseur (P11).

    Fusionne les deltas des entités synchronisables (`patient`, `visit`) survenus
    après le curseur, triés par `(updated_at, id)` pour une pagination monotone.
    La RLS garantit que seules les lignes du tenant courant sont visibles. Le
    nouveau curseur pointe le dernier delta rendu ; `has_more` indique qu'un pull
    immédiat ramènera d'autres deltas.

    En l'absence de delta, on renvoie le curseur fourni tel quel (position
    inchangée), de sorte qu'un pull à vide soit idempotent côté position.
    """
    cursor = _parse_cursor(since)

    # Collecte des lignes candidates de chaque entité (chacune bornée à PAGE+1).
    candidates: list[tuple[datetime.datetime, uuid.UUID, SyncEntity, Patient | Visit]] = []
    for entity in (SyncEntity.patient, SyncEntity.visit):
        for row in await _rows_since(session, entity, cursor):
            candidates.append((row.updated_at, row.id, entity, row))

    # Tri global monotone par (updated_at, id) : ordre déterministe du flux.
    candidates.sort(key=lambda c: (c[0], c[1]))

    has_more = len(candidates) > _PULL_PAGE_SIZE
    page = candidates[:_PULL_PAGE_SIZE]

    changes = [_change_from_row(entity, row) for (_ts, _id, entity, row) in page]

    if page:
        last_ts, last_id, _entity, _row = page[-1]
        new_cursor = _encode_cursor(last_ts, last_id)
    else:
        # Aucun delta : conserver la position fournie (ou curseur vide initial).
        new_cursor = since or ""

    return PullResult(changes=changes, cursor=new_cursor, has_more=has_more)
