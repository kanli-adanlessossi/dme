"""Routeur d'administration de plateforme (tâche 8).

Cycle de vie des tenants (exigence 3), réservé au `platform_admin` et audité
(NFR-3). Trois opérations transversales :

- `POST /api/admin/tenants` — intègre un tenant : crée le tenant, son profil
  `medical_center`, le `tenant_admin` initial, et renvoie un **jeton de
  configuration à usage unique** (jamais un mot de passe en clair). (3.1, 3.2)
- `GET /api/admin/tenants` — liste tous les tenants. (3.3)
- `PATCH /api/admin/tenants/{id}` — active/désactive un tenant ; la désactivation
  conserve les données (NFR-4), masque le tenant du public (P8) et refuse la
  connexion de ses membres. (3.4)
- `POST /api/admin/tenants/{id}/export` — exporte l'intégralité des données d'un
  tenant (profil, comptes sans secret, PHI, services, sync) ; audité
  (`tenant.export`) dans la **même transaction** (fail-closed, NFR-3). (spec
  `tenant-data-rights`, exigence 1)
- `POST /api/admin/tenants/{id}/erase` — efface définitivement les données d'un
  tenant **désactivé** (opération destructive d'exception, ADR-1). L'audit
  d'intention (`tenant.erase`) est écrit **AVANT l'acte, dans une transaction
  séparée committée d'abord** (`record_audit_standalone`) : la trace de la
  tentative survit même si l'effacement échoue (design, « Error Handling »).
  L'effacement proprement dit s'exécute ensuite dans sa propre transaction.
  Garde-fous : 409 si le tenant est encore actif, 404 s'il est inconnu. (spec
  `tenant-data-rights`, exigence 2)

Chemin de code dédié et explicitement cadré (design, « Conception multi-tenant »)
plutôt que le contexte RLS ad hoc : ces endpoints n'empruntent PAS la dépendance
`tenant_session` (qui 500 pour un `platform_admin` au `tenant_id` NUL). Ils
utilisent une session simple (`get_session`), posent eux-mêmes `app.tenant_id` sur
le **tenant cible** quand ils touchent une table cadrée RLS (via le service), et
**valident la transaction** après avoir écrit l'audit — de sorte que chaque action
de cycle de vie soit atomique et journalisée (fail-closed, exigence 3.5).

Cadrage par rôle (tâche 7.5) : `require_roles(platform_admin)` renvoie 403 pour
tout autre rôle (exigence 3.3) avant toute logique.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import build_event, record_audit, record_audit_standalone
from app.core.db import get_session
from app.core.deps import AuthPrincipal, require_roles
from app.schemas.admin import (
    RegionCode,
    TenantCreatedResponse,
    TenantCreateRequest,
    TenantResponse,
    TenantStatus,
    TenantUpdateRequest,
)
from app.schemas.auth import Role
from app.schemas.common import (
    RESPONSES_403,
    RESPONSES_404,
    RESPONSES_AUTHED,
    ErrorResponse,
)
from app.schemas.tenant_data import EraseReport, TenantExport
from app.services.admin import (
    CoordinatesOutsideTogoError,
    EmailAlreadyExistsError,
    RegionNotFoundError,
    TenantNotFoundError,
    create_tenant_onboarding,
    list_tenants,
    set_tenant_status,
)
from app.services.tenant_data import (
    TenantNotFoundError as ExportTenantNotFoundError,
)
from app.services.tenant_data import (
    TenantStillActiveError,
    erase_tenant,
    export_tenant,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Opérations transversales : réservées au platform_admin. Garde construite une
# seule fois (singleton module) pour éviter `require_roles(...)` en valeur par
# défaut d'argument (motif B008), cohérent avec le routeur de sync.
_require_platform_admin = require_roles(Role.platform_admin)

# Mapping entre le code de région (enum du schéma) et sa valeur str persistée.
# L'enum `RegionCode` partage ses valeurs avec `region.code` (SAVANES, …).


def _region_code_str(code: RegionCode) -> str:
    """Valeur str du code de région (pour la résolution en base)."""
    return code.value


def _tenant_to_response(tenant: object, region_code: RegionCode) -> TenantResponse:
    """Projette un modèle `Tenant` vers le schéma d'administration."""
    return TenantResponse(
        id=tenant.id,  # type: ignore[attr-defined]
        name=tenant.name,  # type: ignore[attr-defined]
        region_code=region_code,
        status=TenantStatus(tenant.status),  # type: ignore[attr-defined]
        created_at=tenant.created_at,  # type: ignore[attr-defined]
    )


async def _region_code_for_tenant(
    session: AsyncSession, region_id: UUID
) -> RegionCode:
    """Résout le `RegionCode` d'un tenant depuis sa `region_id` (lecture référence)."""
    from sqlalchemy import select

    from app.models.region import Region

    result = await session.execute(select(Region.code).where(Region.id == region_id))
    code = result.scalar_one_or_none()
    # La région existe nécessairement (FK) ; par prudence, défaut improbable évité.
    return RegionCode(code)


@router.post(
    "/tenants",
    response_model=TenantCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        **RESPONSES_403,
        409: {
            "model": ErrorResponse,
            "description": "E-mail du tenant_admin déjà utilisé.",
        },
    },
    summary="Crée un tenant + tenant_admin initial + jeton de configuration",
)
async def create_tenant(
    payload: TenantCreateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantCreatedResponse:
    """Intègre un nouveau tenant (exigences 3.1, 3.2) ; audité (exigence 3.5).

    Crée en une transaction le tenant, son profil `medical_center` et le compte
    `tenant_admin` initial, puis renvoie un jeton de configuration à usage unique
    (jamais un mot de passe en clair). L'onboarding est journalisé dans
    l'`audit_log` **avant** le commit (fail-closed, NFR-3) ; un échec d'audit
    annule donc toute l'intégration.

    Codes d'erreur : 403 (rôle ≠ platform_admin), 409 (e-mail admin déjà pris),
    422 (coordonnées hors Togo ou code de région inconnu en base).
    """
    try:
        result = await create_tenant_onboarding(
            session,
            name=payload.name,
            region_code=_region_code_str(payload.region_code),
            address=payload.address,
            location=payload.location,
            phone=payload.phone,
            email=str(payload.email),
            admin_email=str(payload.admin_email),
        )
    except CoordinatesOutsideTogoError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Les coordonnées doivent se situer à l'intérieur du Togo.",
        ) from None
    except RegionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Code de région inconnu.",
        ) from None
    except EmailAlreadyExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un compte avec cet e-mail existe déjà.",
        ) from None

    # Audit de l'intégration (acteur = platform_admin ; tenant = nouveau tenant).
    # Écrit dans la MÊME transaction que l'onboarding (fail-closed, exigence 3.5).
    await record_audit(
        session,
        build_event(
            request,
            action="tenant.onboard",
            tenant_id=result.tenant.id,
            actor_user_id=principal.sub,
            entity="tenant",
            entity_id=result.tenant.id,
        ),
    )
    await session.commit()

    return TenantCreatedResponse(
        id=result.tenant.id,
        name=result.tenant.name,
        region_code=payload.region_code,
        status=TenantStatus(result.tenant.status),
        created_at=result.tenant.created_at,
        admin_email=payload.admin_email,
        setup_token=result.setup_token,
        setup_token_expires_at=result.setup_token_expires_at,
    )


@router.get(
    "/tenants",
    response_model=list[TenantResponse],
    responses={**RESPONSES_403},
    summary="Liste les tenants (platform_admin uniquement)",
)
async def list_all_tenants(
    _principal: AuthPrincipal = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> list[TenantResponse]:
    """Liste tous les tenants ; refusé (403) hors platform_admin (exigence 3.3)."""
    tenants = await list_tenants(session)
    responses: list[TenantResponse] = []
    for tenant in tenants:
        region_code = await _region_code_for_tenant(session, tenant.region_id)
        responses.append(_tenant_to_response(tenant, region_code))
    return responses


@router.patch(
    "/tenants/{tenant_id}",
    response_model=TenantResponse,
    responses={**RESPONSES_AUTHED},
    summary="Active ou désactive un tenant",
)
async def update_tenant(
    tenant_id: UUID,
    payload: TenantUpdateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantResponse:
    """Active/désactive un tenant (exigence 3.4) ; audité (exigence 3.5).

    La désactivation conserve toutes les données (NFR-4), masque le tenant de la
    surface publique (P8, appliqué en tâche 12) et refuse la connexion de ses
    membres (bascule `app_user.is_active`). L'action de cycle de vie est
    journalisée **avant** le commit (fail-closed). Un tenant inconnu → 404.
    """
    try:
        tenant = await set_tenant_status(
            session,
            tenant_id=tenant_id,
            status=payload.status.value,
        )
    except TenantNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant introuvable.",
        ) from None

    action = (
        "tenant.activate"
        if payload.status == TenantStatus.active
        else "tenant.deactivate"
    )
    await record_audit(
        session,
        build_event(
            request,
            action=action,
            tenant_id=tenant.id,
            actor_user_id=principal.sub,
            entity="tenant",
            entity_id=tenant.id,
        ),
    )
    await session.commit()

    region_code = await _region_code_for_tenant(session, tenant.region_id)
    return _tenant_to_response(tenant, region_code)


@router.post(
    "/tenants/{tenant_id}/export",
    response_model=TenantExport,
    responses={**RESPONSES_403, **RESPONSES_404},
    summary="Exporte l'intégralité des données d'un tenant (platform_admin)",
)
async def export_tenant_data(
    tenant_id: UUID,
    request: Request,
    principal: AuthPrincipal = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantExport:
    """Exporte toutes les données d'un tenant, sans secret (exigence 1) ; audité.

    Opération transversale réservée au `platform_admin` (403 sinon). Produit un
    `TenantExport` — JSON structuré par entité, cadré au tenant demandé et
    **sans aucun secret** (ni `password_hash`, ni `setup_token_hash`, ni jeton de
    refresh) — via `export_tenant`, qui pose le contexte RLS sur le tenant cible
    et lit sur un instantané cohérent.

    L'export est **audité dans la même transaction** (`tenant.export`, acteur =
    platform_admin, tenant cible) : l'écriture d'audit précède le `commit`, de
    sorte qu'un échec d'audit annule l'opération (fail-closed, NFR-3). La session
    n'a encore effectué aucune lecture, ce qui permet à `export_tenant` de poser
    l'isolation `REPEATABLE READ` en premier.

    Un tenant inconnu → 404, sans effet de bord.
    """
    try:
        export = await export_tenant(session, tenant_id)
    except ExportTenantNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant introuvable.",
        ) from None

    # Audit de l'export (acteur = platform_admin ; tenant = tenant cible), écrit
    # dans la MÊME transaction que la lecture d'export (fail-closed, NFR-3).
    await record_audit(
        session,
        build_event(
            request,
            action="tenant.export",
            tenant_id=tenant_id,
            actor_user_id=principal.sub,
            entity="tenant",
            entity_id=tenant_id,
        ),
    )
    await session.commit()

    return export


@router.post(
    "/tenants/{tenant_id}/erase",
    response_model=EraseReport,
    responses={
        **RESPONSES_403,
        **RESPONSES_404,
        409: {
            "model": ErrorResponse,
            "description": "Tenant encore actif : désactivation préalable requise.",
        },
    },
    summary="Efface définitivement les données d'un tenant désactivé (platform_admin)",
)
async def erase_tenant_data(
    tenant_id: UUID,
    request: Request,
    principal: AuthPrincipal = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> EraseReport:
    """Efface définitivement les données d'un tenant, audité AVANT l'acte (exigence 2).

    Opération **destructive d'exception** réservée au `platform_admin` (403 sinon),
    distincte du soft-delete du CRUD normal (ADR-1). Elle supprime physiquement les
    PHI et les données du tenant puis son identité, en préservant intégralement
    l'`audit_log` (append-only, NFR-3 — l'audit n'a aucune FK vers `tenant`).

    Ordre des opérations (décision « Error Handling » du design) :

    1. **Audit d'intention écrit AVANT l'acte, dans une transaction séparée
       committée d'abord** (`record_audit_standalone`, action `tenant.erase`,
       acteur = platform_admin, tenant cible). La trace « tentative d'effacement »
       survit donc même si l'effacement échoue ensuite — on ne peut pas effacer un
       tenant sans en laisser une trace indélébile.
    2. **Effacement dans sa propre transaction** (`erase_tenant`) : tous les DELETE
       cadrés RLS au tenant cible, dans l'ordre imposé par les FK RESTRICT, puis
       `commit`. Une erreur en cours annule l'ensemble des DELETE (atomicité), mais
       l'audit d'intention déjà committé subsiste.

    Garde-fous (décisions tranchées, ADR-1) :

    - **Tenant encore actif → 409** (`TenantStillActiveError`) : la désactivation
      préalable (`status='inactive'`) est obligatoire (exigence 2.6). Aucun export
      préalable n'est exigé ni généré par cet endpoint (exigence 2 ; l'export reste
      une opération découplée et optionnelle).
    - **Tenant inconnu → 404** (`TenantNotFoundError`), sans effet de bord sur les
      données (exigence 2.7). La trace d'intention reste néanmoins journalisée.
    """
    # (1) Audit d'intention AVANT l'acte, dans une transaction SÉPARÉE committée
    # d'abord : `record_audit_standalone` ouvre sa propre session/transaction,
    # insère la ligne `tenant.erase`, puis valide. La trace survit même si
    # l'effacement échoue (design, « Error Handling » ; NFR-3). On n'enveloppe
    # jamais cet appel dans un try/except qui masquerait l'échec (fail-closed).
    await record_audit_standalone(
        build_event(
            request,
            action="tenant.erase",
            tenant_id=tenant_id,
            actor_user_id=principal.sub,
            entity="tenant",
            entity_id=tenant_id,
        ),
    )

    # (2) Effacement dans sa PROPRE transaction (celle de la session de requête).
    # Les garde-fous sont portés par le service : 409 si le tenant est actif, 404
    # s'il est inconnu — levés AVANT toute suppression, donc sans effet de bord.
    try:
        report = await erase_tenant(session, tenant_id)
    except TenantStillActiveError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le tenant doit d'abord être désactivé avant d'être effacé.",
        ) from None
    except ExportTenantNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant introuvable.",
        ) from None

    await session.commit()

    return report
