"""Service du domaine clinique (patient/visit) — CRUD cadré au tenant (tâche 9.2).

Logique **cœur** du CRUD PHI (exigence 5), appelée par les routeurs `patients` et
`visits`. Elle opère toujours sur une **session déjà cadrée au tenant**
(`tenant_session`, tâche 4.3) : le `SET LOCAL app.tenant_id` ayant été posé en
amont, la RLS forcée (migration 0006) filtre automatiquement toutes les lectures
et rejette toute écriture forgée pour un autre tenant (`WITH CHECK`, P1/P2).

Garde-fous appliqués ici (design, « Components and Interfaces » / « Data Models »
/ « Error Handling ») :

1. **Tenancy autoritaire côté serveur (P2, exigence 5.2).** Le `tenant_id` d'un
   enregistrement est **apposé par le serveur** depuis le contexte authentifié,
   jamais lu du corps de requête. Le `WITH CHECK` RLS sert de garde-fou ultime.

2. **`id` client accepté (NFR-6).** La clé primaire est un **UUIDv7 généré côté
   client** : on l'écrit telle quelle (aucune réécriture), condition du
   local-first (clé stable dès l'écriture hors ligne, pas de collision).

3. **Suppression logique uniquement (NFR-4, P6).** `delete_*` pose `deleted_at`
   et n'efface jamais physiquement. Les lectures normales excluent les lignes
   `deleted_at IS NOT NULL`.

4. **Unicité patient (exigence 5.4).** `UNIQUE (tenant_id, mrn)` est imposé en
   base ; on traduit la violation en `PatientMrnConflictError` (→ 409) plutôt
   qu'une `IntegrityError` opaque.

5. **Consentement (exigence 5.6).** `consent_to_store` est capturé à la création
   du patient (champ obligatoire du schéma).

6. **Versionnage LWW (NFR-6).** Chaque mutation incrémente `version` et
   rafraîchit `updated_at` : base de la résolution de conflits de la sync
   (tâche 10).

Le service ne valide PAS la transaction : le `commit` est assuré par la
dépendance `tenant_session` en sortie nominale du handler, de sorte que la
mutation ET son enregistrement d'audit (écrit par le routeur dans la même
transaction) soient atomiques (fail-closed, P5).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.patient import Patient
from app.models.visit import Visit


class PatientMrnConflictError(Exception):
    """Un patient avec ce `mrn` existe déjà dans le tenant (viole `UNIQUE`).

    `app_user`… non : ici c'est `UNIQUE (tenant_id, mrn)` (migration 0006). On
    détecte le doublon pour renvoyer une erreur claire (409) au lieu d'exposer une
    violation de contrainte brute.
    """


class PatientNotFoundError(Exception):
    """Le patient ciblé n'existe pas dans le tenant courant (ou est hors tenant).

    Indiscernable d'un patient inexistant par conception (P3) : le routeur traduit
    cette erreur en 404, qu'il s'agisse d'un id inconnu ou d'une ligne masquée par
    la RLS (autre tenant).
    """


class VisitNotFoundError(Exception):
    """La visite ciblée n'existe pas dans le tenant courant (ou est hors tenant)."""


class PatientForVisitNotFoundError(Exception):
    """Le `patient_id` d'une visite ne désigne aucun patient actif du tenant.

    La RLS garantit déjà l'isolation inter-tenant ; ce contrôle applicatif évite
    de créer une visite orpheline (patient inexistant ou supprimé logiquement) et
    renvoie un 404 cohérent.
    """


def _now() -> datetime.datetime:
    """Instant courant (UTC, timezone-aware) pour les horodatages de mutation."""
    return datetime.datetime.now(datetime.UTC)


# --------------------------------------------------------------------------- #
# Patient                                                                      #
# --------------------------------------------------------------------------- #


async def list_patients(session: AsyncSession) -> Sequence[Patient]:
    """Liste les patients **actifs** du tenant courant (soft-deleted exclus, P6).

    La RLS cadre déjà la lecture au tenant (contexte posé par `tenant_session`) ;
    on exclut en plus les lignes supprimées logiquement. Ordonné par
    `created_at` pour une sortie déterministe.
    """
    result = await session.execute(
        select(Patient)
        .where(Patient.deleted_at.is_(None))
        .order_by(Patient.created_at)
    )
    return result.scalars().all()


async def get_patient(
    session: AsyncSession, patient_id: uuid.UUID
) -> Patient:
    """Charge un patient **actif** du tenant par id, ou lève `PatientNotFoundError`.

    Un id inexistant ET un patient d'un autre tenant (invisible par la RLS)
    produisent la même erreur (P3). Un patient supprimé logiquement est traité
    comme introuvable (P6).
    """
    patient = await _get_active_patient(session, patient_id)
    if patient is None:
        raise PatientNotFoundError
    return patient


async def _get_active_patient(
    session: AsyncSession, patient_id: uuid.UUID
) -> Patient | None:
    """Récupère un patient actif (non supprimé) du tenant courant, ou `None`."""
    result = await session.execute(
        select(Patient).where(
            Patient.id == patient_id,
            Patient.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def create_patient(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    mrn: str,
    given_name: str,
    family_name: str,
    dob: datetime.date | None,
    sex: str,
    phone: str | None,
    national_id: str | None,
    consent_to_store: bool,
) -> Patient:
    """Crée un patient ; le serveur appose `tenant_id` (P2, exigence 5.2).

    - `patient_id` est l'**UUIDv7 fourni par le client** (NFR-6), écrit tel quel.
    - `tenant_id` provient du **contexte authentifié** (jamais du corps) ; le
      `WITH CHECK` RLS rejetterait toute valeur étrangère au contexte.
    - `consent_to_store` capture le consentement (exigence 5.6).

    Lève `PatientMrnConflictError` si `(tenant_id, mrn)` existe déjà (409).
    Ne valide pas la transaction (commit assuré par `tenant_session`).
    """
    patient = Patient(
        id=patient_id,
        tenant_id=tenant_id,
        mrn=mrn,
        given_name=given_name,
        family_name=family_name,
        dob=dob,
        sex=sex,
        phone=phone,
        national_id=national_id,
        consent_to_store=consent_to_store,
        created_by=created_by,
    )
    session.add(patient)
    try:
        # Flush pour matérialiser les défauts serveur (created_at/updated_at/
        # version) et déclencher la contrainte d'unicité AVANT le retour, afin de
        # traduire proprement un doublon de `mrn` en erreur métier.
        await session.flush()
    except IntegrityError as exc:
        raise PatientMrnConflictError from exc
    return patient


async def update_patient(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    fields: dict[str, object],
) -> Patient:
    """Met à jour partiellement un patient actif du tenant (exigence 5.1).

    `fields` ne contient que les champs effectivement fournis (exclude_unset).
    `id` et `tenant_id` ne sont jamais modifiables (apposés par le serveur, P2).
    Chaque mutation incrémente `version` et rafraîchit `updated_at` (base LWW,
    NFR-6).

    Lève `PatientNotFoundError` (id inconnu / hors tenant / supprimé, P3) ou
    `PatientMrnConflictError` si le nouveau `mrn` entre en collision (409).
    """
    patient = await _get_active_patient(session, patient_id)
    if patient is None:
        raise PatientNotFoundError

    for name, value in fields.items():
        setattr(patient, name, value)
    patient.version += 1
    patient.updated_at = _now()

    try:
        await session.flush()
    except IntegrityError as exc:
        raise PatientMrnConflictError from exc
    return patient


async def soft_delete_patient(
    session: AsyncSession, patient_id: uuid.UUID
) -> Patient:
    """Supprime **logiquement** un patient actif (pose `deleted_at`, P6/NFR-4).

    Jamais de suppression physique : la ligne est conservée en stockage et exclue
    des lectures normales. Incrémente `version` et rafraîchit `updated_at` pour
    que la suppression se propage par la sync (NFR-6). Lève `PatientNotFoundError`
    si le patient est inconnu, hors tenant, ou déjà supprimé (P3/P6).
    """
    patient = await _get_active_patient(session, patient_id)
    if patient is None:
        raise PatientNotFoundError

    patient.deleted_at = _now()
    patient.version += 1
    patient.updated_at = patient.deleted_at
    await session.flush()
    return patient


# --------------------------------------------------------------------------- #
# Visit                                                                        #
# --------------------------------------------------------------------------- #


async def list_visits(session: AsyncSession) -> Sequence[Visit]:
    """Liste les visites **actives** du tenant courant (soft-deleted exclues, P6)."""
    result = await session.execute(
        select(Visit)
        .where(Visit.deleted_at.is_(None))
        .order_by(Visit.updated_at)
    )
    return result.scalars().all()


async def get_visit(session: AsyncSession, visit_id: uuid.UUID) -> Visit:
    """Charge une visite active du tenant par id, ou lève `VisitNotFoundError` (P3)."""
    visit = await _get_active_visit(session, visit_id)
    if visit is None:
        raise VisitNotFoundError
    return visit


async def _get_active_visit(
    session: AsyncSession, visit_id: uuid.UUID
) -> Visit | None:
    """Récupère une visite active (non supprimée) du tenant courant, ou `None`."""
    result = await session.execute(
        select(Visit).where(
            Visit.id == visit_id,
            Visit.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def create_visit(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    patient_id: uuid.UUID,
    visited_at: datetime.datetime | None,
    reason: str | None,
    notes: str | None,
) -> Visit:
    """Crée une visite ; le serveur appose `tenant_id` (P2, exigence 5.2).

    `visit_id` est l'UUIDv7 client (NFR-6). Le `patient_id` DOIT désigner un
    patient actif du **même tenant** : on le vérifie (la RLS garantit déjà qu'un
    patient d'un autre tenant est invisible) pour éviter une visite orpheline et
    renvoyer un 404 clair (`PatientForVisitNotFoundError`). Ne valide pas la
    transaction (commit assuré par `tenant_session`).
    """
    patient = await _get_active_patient(session, patient_id)
    if patient is None:
        raise PatientForVisitNotFoundError

    visit = Visit(
        id=visit_id,
        tenant_id=tenant_id,
        patient_id=patient_id,
        visited_at=visited_at,
        reason=reason,
        notes=notes,
        created_by=created_by,
    )
    session.add(visit)
    await session.flush()
    return visit


async def soft_delete_visit(
    session: AsyncSession, visit_id: uuid.UUID
) -> Visit:
    """Supprime **logiquement** une visite active (pose `deleted_at`, P6/NFR-4).

    Jamais de suppression physique. Incrémente `version` et rafraîchit
    `updated_at` (propagation par la sync, NFR-6). Lève `VisitNotFoundError` si la
    visite est inconnue, hors tenant, ou déjà supprimée (P3/P6).
    """
    visit = await _get_active_visit(session, visit_id)
    if visit is None:
        raise VisitNotFoundError

    visit.deleted_at = _now()
    visit.version += 1
    visit.updated_at = visit.deleted_at
    await session.flush()
    return visit
