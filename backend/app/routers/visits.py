"""Routeur du domaine visite — CRUD cadré au tenant, PHI auditées (tâche 9.2).

`GET/POST /api/visits` et `DELETE /api/visits/{id}` (exigence 5), conformément au
contrat figé en tâche 3 et au modèle du design (section « Components and
Interfaces » : la visite expose liste/création/suppression logique, pas de
GET/PATCH par id — la lecture d'une visite isolée n'est pas au contrat v1).

Garanties appliquées (identiques au routeur `patients`, voir sa docstring) :

- **Auth requise + cadrage par rôle** (`tenant_admin`, `clinician`, `staff`) :
  les PHI visite ne sont **jamais** exposées publiquement (P4, exigence 5.3).
- **Contexte RLS établi** via `tenant_session` (P1).
- **Tenancy autoritaire côté serveur** : `tenant_id` apposé depuis le principal
  (P2, exigence 5.2).
- **`id` client UUIDv7 accepté** (NFR-6).
- **Suppression logique** : DELETE pose `deleted_at`, jamais physique (P6, NFR-4).
- **PHI auditées** : liste, création et suppression journalisées dans la
  transaction (fail-closed, P5, NFR-3).

Les notes de visite sont une entité cliniquement sensible : la résolution de
conflit (tâche 10) les marquera pour revue plutôt que de les écraser (P12). Ce
routeur ne traite que le CRUD ; la sync relève de la tâche 10.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import build_event, record_audit
from app.core.deps import AuthPrincipal, require_roles, tenant_resource_not_found
from app.core.tenant_context import tenant_session
from app.models.visit import Visit
from app.schemas.auth import Role
from app.schemas.common import RESPONSES_AUTHED
from app.schemas.visits import VisitCreateRequest, VisitResponse
from app.services.clinical import (
    PatientForVisitNotFoundError,
    VisitNotFoundError,
    create_visit,
    list_visits,
    soft_delete_visit,
)

router = APIRouter(prefix="/api/visits", tags=["visits"])

# Rôles autorisés à manipuler les PHI visite (voir `patients`). Le platform_admin
# en est exclu (403 avant tout accès cadré au tenant).
_require_phi_role = require_roles(Role.tenant_admin, Role.clinician, Role.staff)


def _to_response(visit: Visit) -> VisitResponse:
    """Projette un modèle `Visit` ORM vers le schéma de réponse (champs stockés)."""
    return VisitResponse(
        id=visit.id,
        tenant_id=visit.tenant_id,
        patient_id=visit.patient_id,
        visited_at=visit.visited_at,
        reason=visit.reason,
        notes=visit.notes,
        updated_at=visit.updated_at,
        version=visit.version,
        deleted_at=visit.deleted_at,
    )


@router.get(
    "",
    response_model=list[VisitResponse],
    responses={**RESPONSES_AUTHED},
    summary="Liste les visites du tenant (exclut les supprimées logiquement)",
)
async def list_visits_endpoint(
    request: Request,
    principal: AuthPrincipal = Depends(_require_phi_role),
    session: AsyncSession = Depends(tenant_session),
) -> list[VisitResponse]:
    """Liste les visites actives du tenant (soft-deleted exclues, P6).

    Consultation de PHI auditée (P5, NFR-3).
    """
    visits = await list_visits(session)
    await record_audit(
        session,
        build_event(
            request,
            action="visit.list",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="visit",
        ),
    )
    return [_to_response(v) for v in visits]


@router.post(
    "",
    response_model=VisitResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**RESPONSES_AUTHED},
    summary="Crée une visite (id UUIDv7 client ; tenant_id apposé serveur)",
)
async def create_visit_endpoint(
    payload: VisitCreateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(_require_phi_role),
    session: AsyncSession = Depends(tenant_session),
) -> VisitResponse:
    """Crée une visite ; le serveur appose le `tenant_id` (P2, exigence 5.2).

    L'`id` client (UUIDv7) est écrit tel quel (NFR-6). Le `patient_id` doit
    désigner un patient actif du même tenant (sinon 404). Création auditée (P5).
    """
    try:
        visit = await create_visit(
            session,
            visit_id=payload.id,
            tenant_id=principal.tenant_id,
            created_by=principal.sub,
            patient_id=payload.patient_id,
            visited_at=payload.visited_at,
            reason=payload.reason,
            notes=payload.notes,
        )
    except PatientForVisitNotFoundError:
        raise tenant_resource_not_found(
            "Patient introuvable pour cette visite."
        ) from None

    await record_audit(
        session,
        build_event(
            request,
            action="visit.create",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="visit",
            entity_id=visit.id,
        ),
    )
    return _to_response(visit)


@router.delete(
    "/{visit_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**RESPONSES_AUTHED},
    summary="Suppression logique d'une visite (soft-delete)",
)
async def delete_visit_endpoint(
    visit_id: UUID,
    request: Request,
    principal: AuthPrincipal = Depends(_require_phi_role),
    session: AsyncSession = Depends(tenant_session),
) -> None:
    """Supprime logiquement une visite (pose `deleted_at`, P6) ; jamais physique.

    Inconnue/hors tenant/déjà supprimée => 404 (P3/P6). Suppression auditée (P5).
    """
    try:
        visit = await soft_delete_visit(session, visit_id)
    except VisitNotFoundError:
        raise tenant_resource_not_found("Visite introuvable.") from None

    await record_audit(
        session,
        build_event(
            request,
            action="visit.delete",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="visit",
            entity_id=visit.id,
        ),
    )
