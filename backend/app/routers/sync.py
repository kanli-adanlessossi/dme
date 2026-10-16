"""Routeur de synchronisation (tâche 10) — moteur push/pull cadré au tenant.

`POST /api/sync/push` (lot idempotent de mutations) et
`GET /api/sync/pull?since=<curseur>` (deltas + nouveau curseur). Cadré au tenant
(contexte RLS établi par `tenant_session`) ; audité (NFR-3).

Garanties (design, « Correctness Properties ») — déléguées au service
`app.services.sync` :

- **P10 — idempotence.** Le push rejoue sans effet toute mutation déjà appliquée
  (clé = `mutation_id`). Reprise après coupure sans doublon ni perte.
- **P11 — isolation à la sync.** Les deux routes passent par `tenant_session` :
  `SET LOCAL app.tenant_id` est posé depuis le JWT, la RLS forcée filtre les
  lectures (pull) et le `WITH CHECK` rejette toute injection inter-tenant (push).
  Le `tenant_id` écrit vient du **principal**, jamais du corps d'une mutation (P2).
- **P12 — conflits non destructifs.** Les conflits sont résolus en LWW et
  consignés dans `sync_conflict` ; les entités sensibles (notes de visite) sont
  marquées pour revue plutôt qu'écrasées.

Révocation (tâche 7.5, NFR-7) : `pull` renvoie `revoked=true` sans aucun delta si
l'accès du principal est révoqué (compte inactif ou tenant désactivé), afin que
le client purge sa session et ses PHI locales.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import build_event, record_audit
from app.core.deps import (
    AuthPrincipal,
    require_roles,
    sync_access_revoked,
)
from app.core.tenant_context import tenant_session
from app.schemas.auth import Role
from app.schemas.common import RESPONSES_AUTHED
from app.schemas.sync import SyncPullResponse, SyncPushRequest, SyncPushResponse
from app.services.sync import apply_push, pull_deltas

router = APIRouter(prefix="/api/sync", tags=["sync"])

# Rôles autorisés à synchroniser des PHI : personnel soignant/administratif du
# tenant. Le `platform_admin` (transversal, tenant NUL) n'emprunte pas le chemin
# de sync cadré au tenant. Cadrage appliqué via `require_roles` (403 si manquant).
_SYNC_ROLES = (Role.tenant_admin, Role.clinician, Role.staff)

# Garde de rôle construite une seule fois (singleton module) : évite d'appeler
# `require_roles(...)` dans une valeur par défaut d'argument (motif B008).
_require_sync_role = require_roles(*_SYNC_ROLES)


@router.post(
    "/push",
    response_model=SyncPushResponse,
    responses={**RESPONSES_AUTHED},
    summary="Applique un lot idempotent de mutations ; renvoie résultats + conflits",
)
async def push(
    payload: SyncPushRequest,
    request: Request,
    principal: AuthPrincipal = Depends(_require_sync_role),
    session: AsyncSession = Depends(tenant_session),
) -> SyncPushResponse:
    """Applique l'outbox de façon idempotente (clé = mutation_id, P10).

    Le serveur appose le `tenant_id` depuis le principal (P2) ; le `WITH CHECK`
    RLS empêche l'injection inter-tenant (P11). Les conflits sont résolus en LWW et
    journalisés ; les entités sensibles sont marquées pour revue (P12).

    L'événement de sync est audité dans la **même transaction** que l'application
    des mutations (fail-closed, NFR-3) ; `tenant_session` valide ensuite.
    """
    outcome = await apply_push(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.sub,
        mutations=payload.mutations,
    )

    await record_audit(
        session,
        build_event(
            request,
            action="sync.push",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="sync",
        ),
    )

    return SyncPushResponse(results=outcome.results, conflicts=outcome.conflicts)


@router.get(
    "/pull",
    response_model=SyncPullResponse,
    responses={**RESPONSES_AUTHED},
    summary="Renvoie les deltas du tenant depuis le curseur + un nouveau curseur",
)
async def pull(
    request: Request,
    since: str | None = Query(
        default=None,
        description="Curseur opaque du dernier pull. Absent = synchronisation "
        "initiale (depuis l'origine).",
        examples=["2025-01-15T10:30:00Z|018f8b2c-7c1a-7a2e-9f3b-2c1a7c1a7a2e"],
    ),
    principal: AuthPrincipal = Depends(_require_sync_role),
    revoked: bool = Depends(sync_access_revoked),
    session: AsyncSession = Depends(tenant_session),
) -> SyncPullResponse:
    """Renvoie les deltas cadrés au tenant depuis `since` (delta-sync, P11).

    Exposition de l'état de révocation (tâche 7.5, NFR-7) : si l'accès du principal
    est révoqué (compte inactif ou tenant désactivé), on renvoie immédiatement un
    `SyncPullResponse(revoked=true)` **sans aucun delta**, afin que le client purge
    sa session et ses PHI locales au plus tard à cette synchronisation (design,
    « Authentification tolérante à l'hors-ligne »).

    Sinon, on renvoie les deltas depuis le curseur (triés par `(updated_at, id)`
    pour une pagination monotone), plus un nouveau curseur opaque et `has_more`. La
    session cadrée au tenant (`tenant_session`) établit le contexte RLS : seules
    les lignes du tenant courant sont visibles (P11). Le pull est audité dans la
    transaction de lecture (NFR-3).
    """
    if revoked:
        # Curseur neutre : le client purge et n'a pas à conserver de position.
        return SyncPullResponse(changes=[], cursor=since or "", revoked=True)

    result = await pull_deltas(session, since=since)

    await record_audit(
        session,
        build_event(
            request,
            action="sync.pull",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="sync",
        ),
    )

    return SyncPullResponse(
        changes=result.changes,
        cursor=result.cursor,
        has_more=result.has_more,
        revoked=False,
    )
