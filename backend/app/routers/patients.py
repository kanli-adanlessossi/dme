"""Routeur du domaine patient — CRUD cadré au tenant, PHI auditées (tâche 9.2).

`GET/POST /api/patients` et `GET/PATCH/DELETE /api/patients/{id}` (exigence 5).
Implémente le contrat figé en tâche 3, dont les handlers étaient des stubs.

Garanties appliquées (design, « Components and Interfaces » / « Correctness
Properties ») :

- **Auth requise + cadrage par rôle.** Toute route exige un principal dont le
  rôle manipule des PHI (`tenant_admin`, `clinician`, `staff`) ; un `platform_admin`
  (transversal, tenant NUL) n'y a pas accès (403). Les PHI ne sont donc **jamais
  exposées publiquement** (P4, exigence 5.3) : aucune de ces routes n'est sur la
  surface publique sans auth.
- **Contexte RLS établi.** La session est fournie par `tenant_session` (tâche 4.3)
  qui pose `SET LOCAL app.tenant_id` depuis le JWT : la RLS filtre lectures et
  écritures au tenant courant (P1).
- **Tenancy autoritaire côté serveur (P2, exigence 5.2).** Le `tenant_id` écrit
  provient du **principal authentifié**, jamais du corps.
- **`id` client accepté (NFR-6).** La clé primaire UUIDv7 vient du client.
- **Suppression logique (P6, NFR-4).** DELETE pose `deleted_at` ; jamais physique.
- **PHI auditées (P5, NFR-3).** Création, consultation (liste + détail), mise à
  jour et suppression écrivent une ligne d'audit dans la **même transaction** que
  la mutation/lecture (fail-closed) ; `tenant_session` valide ensuite.
- **Non-fuite d'existence (P3).** Un patient inconnu ou hors tenant → 404
  identique ; un doublon de `mrn` → 409.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import build_event, record_audit
from app.core.deps import AuthPrincipal, require_roles, tenant_resource_not_found
from app.core.tenant_context import tenant_session
from app.models.patient import Patient
from app.schemas.auth import Role
from app.schemas.common import RESPONSES_AUTHED, ErrorResponse
from app.schemas.patients import (
    PatientCreateRequest,
    PatientResponse,
    PatientUpdateRequest,
    Sex,
)
from app.services.clinical import (
    PatientMrnConflictError,
    PatientNotFoundError,
    create_patient,
    get_patient,
    list_patients,
    soft_delete_patient,
    update_patient,
)

router = APIRouter(prefix="/api/patients", tags=["patients"])

# Rôles autorisés à manipuler les PHI patient. Le `platform_admin` (transversal,
# tenant NUL) en est volontairement exclu : il n'emprunte jamais le chemin cadré
# au tenant (il obtiendrait 403, puis 500 sur `tenant_session`). Garde construite
# une seule fois (singleton module), cohérent avec `admin`/`sync`.
_require_phi_role = require_roles(Role.tenant_admin, Role.clinician, Role.staff)

# 409 : doublon de `mrn` dans le tenant (viole `UNIQUE (tenant_id, mrn)`).
_RESPONSES_WITH_CONFLICT: dict[int | str, dict[str, object]] = {
    **RESPONSES_AUTHED,
    409: {
        "model": ErrorResponse,
        "description": "Un patient avec ce numéro de dossier (mrn) existe déjà "
        "dans le tenant (exigence 5.4).",
    },
}


def _to_response(patient: Patient) -> PatientResponse:
    """Projette un modèle `Patient` ORM vers le schéma de réponse (champs stockés)."""
    return PatientResponse(
        id=patient.id,
        tenant_id=patient.tenant_id,
        mrn=patient.mrn,
        given_name=patient.given_name,
        family_name=patient.family_name,
        dob=patient.dob,
        sex=Sex(patient.sex) if patient.sex is not None else Sex.unknown,
        phone=patient.phone,
        national_id=patient.national_id,
        consent_to_store=patient.consent_to_store,
        created_at=patient.created_at,
        updated_at=patient.updated_at,
        version=patient.version,
        deleted_at=patient.deleted_at,
    )


@router.get(
    "",
    response_model=list[PatientResponse],
    responses={**RESPONSES_AUTHED},
    summary="Liste les patients du tenant (exclut les supprimés logiquement)",
)
async def list_patients_endpoint(
    request: Request,
    principal: AuthPrincipal = Depends(_require_phi_role),
    session: AsyncSession = Depends(tenant_session),
) -> list[PatientResponse]:
    """Liste les patients actifs du tenant (soft-deleted exclus, P6).

    La consultation de PHI est auditée (P5, NFR-3) : une ligne `patient.list` est
    écrite dans la transaction de lecture avant le commit de `tenant_session`.
    """
    patients = await list_patients(session)
    await record_audit(
        session,
        build_event(
            request,
            action="patient.list",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="patient",
        ),
    )
    return [_to_response(p) for p in patients]


@router.post(
    "",
    response_model=PatientResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_RESPONSES_WITH_CONFLICT},
    summary="Crée un patient (id UUIDv7 client ; tenant_id apposé serveur)",
)
async def create_patient_endpoint(
    payload: PatientCreateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(_require_phi_role),
    session: AsyncSession = Depends(tenant_session),
) -> PatientResponse:
    """Crée un patient ; le serveur appose le `tenant_id` (P2, exigence 5.2).

    `consent_to_store` est capturé à la création (exigence 5.6). L'`id` client
    (UUIDv7) est écrit tel quel (NFR-6). Un doublon de `mrn` dans le tenant → 409
    (exigence 5.4). Création auditée (P5).
    """
    try:
        patient = await create_patient(
            session,
            patient_id=payload.id,
            tenant_id=principal.tenant_id,
            created_by=principal.sub,
            mrn=payload.mrn,
            given_name=payload.given_name,
            family_name=payload.family_name,
            dob=payload.dob,
            sex=payload.sex.value,
            phone=payload.phone,
            national_id=payload.national_id,
            consent_to_store=payload.consent_to_store,
        )
    except PatientMrnConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un patient avec ce numéro de dossier existe déjà.",
        ) from None

    await record_audit(
        session,
        build_event(
            request,
            action="patient.create",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="patient",
            entity_id=patient.id,
        ),
    )
    return _to_response(patient)


@router.get(
    "/{patient_id}",
    response_model=PatientResponse,
    responses={**RESPONSES_AUTHED},
    summary="Récupère un patient par id",
)
async def get_patient_endpoint(
    patient_id: UUID,
    request: Request,
    principal: AuthPrincipal = Depends(_require_phi_role),
    session: AsyncSession = Depends(tenant_session),
) -> PatientResponse:
    """Renvoie un patient du tenant ; inconnu ou hors tenant => 404 (P3).

    Consultation de PHI auditée (P5, NFR-3).
    """
    try:
        patient = await get_patient(session, patient_id)
    except PatientNotFoundError:
        raise tenant_resource_not_found("Patient introuvable.") from None

    await record_audit(
        session,
        build_event(
            request,
            action="patient.read",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="patient",
            entity_id=patient.id,
        ),
    )
    return _to_response(patient)


@router.patch(
    "/{patient_id}",
    response_model=PatientResponse,
    responses={**_RESPONSES_WITH_CONFLICT},
    summary="Met à jour un patient",
)
async def update_patient_endpoint(
    patient_id: UUID,
    payload: PatientUpdateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(_require_phi_role),
    session: AsyncSession = Depends(tenant_session),
) -> PatientResponse:
    """Met à jour un patient du tenant (PHI auditée, P5).

    Seuls les champs fournis sont modifiés ; `id`/`tenant_id` restent apposés par
    le serveur (P2). Inconnu/hors tenant => 404 (P3) ; collision `mrn` => 409.
    """
    fields = payload.model_dump(exclude_unset=True)
    # Normaliser l'enum `sex` en valeur str persistée (si fourni).
    if "sex" in fields and fields["sex"] is not None:
        fields["sex"] = (
            fields["sex"].value
            if isinstance(fields["sex"], Sex)
            else fields["sex"]
        )

    try:
        patient = await update_patient(session, patient_id=patient_id, fields=fields)
    except PatientNotFoundError:
        raise tenant_resource_not_found("Patient introuvable.") from None
    except PatientMrnConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un patient avec ce numéro de dossier existe déjà.",
        ) from None

    await record_audit(
        session,
        build_event(
            request,
            action="patient.update",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="patient",
            entity_id=patient.id,
        ),
    )
    return _to_response(patient)


@router.delete(
    "/{patient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**RESPONSES_AUTHED},
    summary="Suppression logique d'un patient (soft-delete)",
)
async def delete_patient_endpoint(
    patient_id: UUID,
    request: Request,
    principal: AuthPrincipal = Depends(_require_phi_role),
    session: AsyncSession = Depends(tenant_session),
) -> None:
    """Supprime logiquement un patient (pose `deleted_at`, P6) ; jamais physique.

    Inconnu/hors tenant/déjà supprimé => 404 (P3/P6). Suppression auditée (P5).
    """
    try:
        patient = await soft_delete_patient(session, patient_id)
    except PatientNotFoundError:
        raise tenant_resource_not_found("Patient introuvable.") from None

    await record_audit(
        session,
        build_event(
            request,
            action="patient.delete",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.sub,
            entity="patient",
            entity_id=patient.id,
        ),
    )
