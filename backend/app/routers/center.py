"""Routeur de profil du centre médical (tâche 11) — exigence 6.

`GET/PATCH /api/center/profile`. Cadré au tenant (contexte RLS via
`tenant_session`, tâche 4.3). Implémente le contrat figé en tâche 3, dont les
handlers étaient des stubs.

Garanties appliquées (design, « Components and Interfaces » / « Validation de la
frontière du Togo » / « Correctness Properties ») :

- **Auth requise + cadrage par rôle.** La **lecture** du profil est ouverte aux
  rôles cadrés au tenant (`tenant_admin`, `clinician`, `staff`) ; la **mise à
  jour** est réservée au `tenant_admin` (exigence 6.1) — tout autre rôle (y
  compris `platform_admin`, transversal) → 403.
- **Contexte RLS établi (P1).** `tenant_session` pose `SET LOCAL app.tenant_id`
  depuis le JWT : lectures/écritures sont filtrées au tenant courant.
- **Tenancy autoritaire côté serveur (P2).** Le profil ciblé est celui du tenant
  authentifié ; le `tenant_id` n'est jamais accepté du corps (le schéma ne le
  porte pas, et le `WITH CHECK` RLS est le garde-fou ultime).
- **Validité géographique (P7, exigence 6.2).** Des coordonnées hors de la
  frontière du Togo → **422** avec un message clair.
- **Profil absent → 404.** Un tenant sans centre intégré n'expose rien (P3).

Les actions sont auditées (NFR-3), cohérent avec les routeurs des tâches 8/9.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import build_event, record_audit
from app.core.deps import AuthPrincipal, require_roles, tenant_resource_not_found
from app.core.tenant_context import tenant_session
from app.schemas.admin import GeoPoint, RegionCode
from app.schemas.auth import Role
from app.schemas.center import (
    CenterProfileResponse,
    CenterProfileUpdateRequest,
    OpeningHours,
)
from app.schemas.common import RESPONSES_AUTHED, ErrorResponse
from app.services.center import (
    CenterNotFoundError,
    CenterProfileData,
    CoordinatesOutsideTogoError,
    UnknownServiceTypeError,
    get_center_profile,
    update_center_profile,
)

router = APIRouter(prefix="/api/center", tags=["center"])

# Lecture du profil : rôles cadrés au tenant (le platform_admin transversal, au
# tenant NUL, n'emprunte pas le chemin RLS). Construit une seule fois (singleton).
_require_tenant_role = require_roles(
    Role.tenant_admin, Role.clinician, Role.staff
)
# Mise à jour du profil : réservée au tenant_admin (exigence 6.1).
_require_tenant_admin = require_roles(Role.tenant_admin)

# 422 ajouté explicitement au contrat (coordonnées hors Togo / service inconnu).
_RESPONSES_WITH_GEO: dict[int | str, dict[str, object]] = {
    **RESPONSES_AUTHED,
    422: {
        "model": ErrorResponse,
        "description": "Validation échouée : coordonnées hors de la frontière du "
        "Togo (P7) ou type de service inconnu.",
    },
}


def _to_response(data: CenterProfileData) -> CenterProfileResponse:
    """Projette la donnée de service vers le schéma de réponse HTTP."""
    opening_hours = [
        OpeningHours.model_validate(item) for item in (data.opening_hours or [])
    ]
    return CenterProfileResponse(
        id=data.id,
        tenant_id=data.tenant_id,
        name=data.name,
        region_code=RegionCode(data.region_code),
        address=data.address or "",
        location=GeoPoint(longitude=data.longitude or 0.0, latitude=data.latitude or 0.0),
        phone=data.phone or "",
        email=data.email or "",
        opening_hours=opening_hours,
        service_types=data.service_types,
        is_public=data.is_public,
    )


@router.get(
    "/profile",
    response_model=CenterProfileResponse,
    responses={**RESPONSES_AUTHED},
    summary="Récupère le profil du centre du tenant authentifié",
)
async def get_profile(
    request: Request,
    principal: AuthPrincipal = Depends(_require_tenant_role),
    session: AsyncSession = Depends(tenant_session),
) -> CenterProfileResponse:
    """Renvoie le profil du centre cadré au tenant (P2) ; absent => 404 (P3)."""
    try:
        data = await get_center_profile(session)
    except CenterNotFoundError:
        raise tenant_resource_not_found("Profil de centre introuvable.") from None

    await record_audit(
        session,
        build_event(
            request,
            action="center.read",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="medical_center",
            entity_id=data.id,
        ),
    )
    return _to_response(data)


@router.patch(
    "/profile",
    response_model=CenterProfileResponse,
    responses={**_RESPONSES_WITH_GEO},
    summary="Met à jour le profil du centre (coordonnées validées dans le Togo)",
)
async def update_profile(
    payload: CenterProfileUpdateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(_require_tenant_admin),
    session: AsyncSession = Depends(tenant_session),
) -> CenterProfileResponse:
    """Met à jour le profil (exigence 6.1) ; coordonnées hors Togo => 422 (P7).

    Réservé au `tenant_admin` (403 sinon). Seuls les champs fournis sont modifiés ;
    `id`/`tenant_id` restent apposés par le serveur (P2). Un tenant sans centre
    intégré → 404. Mise à jour auditée (NFR-3).
    """
    fields = payload.model_dump(exclude_unset=True)

    try:
        data = await update_center_profile(session, fields=fields)
    except CenterNotFoundError:
        raise tenant_resource_not_found("Profil de centre introuvable.") from None
    except CoordinatesOutsideTogoError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Les coordonnées doivent se situer à l'intérieur du Togo.",
        ) from None
    except UnknownServiceTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Type de service inconnu : {exc}.",
        ) from None

    await record_audit(
        session,
        build_event(
            request,
            action="center.update",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="medical_center",
            entity_id=data.id,
        ),
    )
    return _to_response(data)
