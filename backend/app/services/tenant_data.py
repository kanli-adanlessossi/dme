"""Service d'export (et, ultérieurement, d'effacement) par tenant (spec
`tenant-data-rights`, observation #2).

Logique **cœur** des opérations transversales de l'administrateur de plateforme sur
le cycle de vie des données d'un tenant (exigences 1 et 2). Ce module porte en
tâche 4 l'**export** (`export_tenant`) et en tâche 6 l'**effacement**
(`erase_tenant`). Les endpoints admin (réservés `platform_admin`, audités) relèvent
des tâches 5 et 7.

## Effacement : DELETE explicites cadrés (ADR-1, voie (ii))

`erase_tenant` n'effectue PAS un `DELETE FROM tenant` + cascade (voie (i), qui
s'exécuterait « en tant que propriétaire », hors RLS, et buterait de toute façon sur
les FK `RESTRICT` `created_by`/`patient_id`). Il applique la **voie (ii)** retenue
par l'ADR : poser `set_tenant_context(session, tenant_id)` puis exécuter des `DELETE`
explicites **dans l'ordre imposé par les FK RESTRICT**, sous le rôle applicatif
`dme_app` (NOBYPASSRLS). La politique RLS `USING (tenant_id = app.tenant_id)` borne
alors chaque DELETE au tenant cible : un bug de ciblage ne peut toucher aucun autre
tenant (P1/P2, exigence 2.4). Le `DELETE FROM tenant` final porte sur une table non
cadrée RLS (le rôle a le GRANT DELETE, migration 0002).

L'audit (`audit_log`) n'est **JAMAIS** touché : il n'a aucune FK vers `tenant`, il est
donc hors du graphe de cascade par construction (ADR-1). Aucune action n'est requise
pour le préserver, et le trigger append-only (migration 0004) n'est jamais sollicité
par l'effacement.

## Chemin admin explicitement cadré (RLS)

Comme `services/admin.py`, ce service emprunte le **chemin admin explicitement
cadré** : la plupart des tables lues sont sous RLS **forcée** (`medical_center`,
`medical_center_service`, `app_user`, `patient`, `visit`, `sync_mutation`,
`sync_conflict`). Pour qu'une lecture transversale du `platform_admin` voie les
lignes du tenant **cible**, on pose `app.tenant_id` sur ce tenant via
`set_tenant_context` au sein de la transaction. La politique RLS `USING (tenant_id =
app.tenant_id)` cadre alors toutes les lectures au tenant demandé — aucune fuite
inter-tenant possible (exigence 1.3). La table `tenant` n'est PAS cadrée RLS : sa
lecture ne dépend d'aucun contexte.

## Cohérence transactionnelle (snapshot — design C1)

`export_tenant` lit **toutes** les entités dans **une seule transaction en lecture
cohérente** en isolation `REPEATABLE READ` : toutes les lectures observent le **même
instantané** de la base. Sous écriture concurrente, l'export ne peut donc pas être
partiellement incohérent (p. ex. une `visit` référençant un `patient` absent de
l'export). Pour un système de santé, un export qui raterait des lignes sous charge
serait un risque de conformité. L'isolation est posée en tout **début** de
transaction, avant la première lecture.

## Absence de secret (exigence 1.2, NFR-2)

La projection exclut **par construction** tout secret : `password_hash`,
`setup_token_hash`, `setup_token_selector` et les jetons de refresh
(`refresh_token`) ne figurent dans aucun schéma d'export (voir `schemas.tenant_data`)
et ne sont jamais lus ici. Un secret ne peut donc pas fuiter, même par erreur.

## Volumétrie (note — design C1, NFR-4)

L'export v1 est **tout-en-mémoire** : toutes les lignes du tenant sont chargées puis
sérialisées d'un bloc. C'est simple et suffisant pour la taille d'un tenant réaliste.
Limite nommée : pour un très gros tenant, cette approche peut être lourde (empreinte
mémoire proportionnelle au volume). L'évolution vers un export **en flux** (NDJSON
par entité, lecture par curseur) est notée comme amélioration future — **non
implémentée ici**. Recommandation d'exploitation : désactiver le tenant avant un
export précédant un effacement (aucune écriture concurrente), tout en autorisant
l'export d'un tenant actif pour l'archivage courant.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_context import set_tenant_context
from app.models.app_user import AppUser
from app.models.medical_center import MedicalCenter
from app.models.medical_center_service import MedicalCenterService
from app.models.patient import Patient
from app.models.sync_conflict import SyncConflict
from app.models.sync_mutation import SyncMutation
from app.models.tenant import Tenant
from app.models.visit import Visit
from app.schemas.tenant_data import (
    CenterExport,
    EraseReport,
    PatientExport,
    ServiceLinkExport,
    SyncConflictExport,
    SyncExport,
    SyncMutationExport,
    TenantExport,
    TenantProfileExport,
    UserExport,
    VisitExport,
)


class TenantNotFoundError(Exception):
    """Le tenant ciblé par une opération d'export/effacement n'existe pas.

    Suit le même motif que `services.admin.TenantNotFoundError` ; le routeur le
    traduit en 404 (exigence 1, cadrage d'erreur du design). Défini localement pour
    garder le service d'export autonome.
    """


class TenantStillActiveError(Exception):
    """Le tenant ciblé par un effacement n'est pas désactivé (`status != 'inactive'`).

    Garde-fou obligatoire de l'effacement (exigence 2.6, ADR-1) : on refuse d'effacer
    un tenant encore actif. La désactivation préalable (`set_tenant_status` →
    `inactive`) est une **double action délibérée** anti-accident, qui coupe aussi
    tout accès/écriture concurrente pendant l'opération. Le routeur (tâche 7) le
    traduit en **409**.
    """


async def _coords(
    session: AsyncSession, center_id: uuid.UUID
) -> tuple[float | None, float | None]:
    """Renvoie (longitude, latitude) WGS84 du centre, ou (None, None) sans point.

    La colonne `medical_center.location` est une géométrie PostGIS ; on la projette
    en lon/lat via `ST_X`/`ST_Y` (comme `services.center`) pour un export portable
    plutôt qu'une représentation binaire opaque.
    """
    result = await session.execute(
        text(
            "SELECT ST_X(location::geometry) AS lon, ST_Y(location::geometry) AS lat "
            "FROM medical_center WHERE id = :id"
        ),
        {"id": center_id},
    )
    row = result.mappings().one_or_none()
    if row is None or row["lon"] is None:
        return (None, None)
    return (float(row["lon"]), float(row["lat"]))


async def export_tenant(
    session: AsyncSession, tenant_id: uuid.UUID
) -> TenantExport:
    """Exporte l'intégralité des données d'un tenant, sans secret (exigence 1).

    Produit un `TenantExport` (JSON structuré par entité, forme stable et
    documentée) contenant : l'identité du `tenant`, le profil `medical_center`, les
    liens de services, les comptes `app_user` (sans secrets), les `patient` et
    `visit` (PHI), ainsi que les `sync_conflict`/`sync_mutation` du tenant.

    Garanties :

    - **Cadrage au tenant (exigence 1.3).** Pose `app.tenant_id` sur le tenant cible
      (`set_tenant_context`) : la RLS forcée cadre toutes les lectures des tables
      cadrées à ce tenant — aucune fuite inter-tenant.
    - **Cohérence (design C1).** Toutes les lectures s'exécutent dans la transaction
      fournie, portée en isolation `REPEATABLE READ` dès son ouverture : un seul
      instantané, pas d'export partiel sous écriture concurrente.
    - **Sans secret (exigence 1.2).** Aucun `password_hash`, `setup_token_hash`,
      `setup_token_selector` ni jeton de refresh — exclus par construction des
      schémas de projection.

    Prérequis (appelant) : une transaction est ouverte sur `session` et **aucune
    lecture** n'y a encore eu lieu (l'isolation `REPEATABLE READ` doit être posée en
    premier). La dépendance admin de la tâche 5 fournit cette transaction ; le
    `commit` (et l'audit) incombent au routeur.

    Lève `TenantNotFoundError` (→ 404) si le tenant n'existe pas, sans effet de bord.
    """
    # Snapshot cohérent : fixer l'isolation AVANT toute lecture (design C1). En
    # REPEATABLE READ, la première requête fige l'instantané pour toute la
    # transaction — toutes les entités sont donc lues de façon cohérente.
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

    # Identité du tenant (table NON cadrée RLS : lecture directe). Un tenant absent
    # est une erreur d'entrée, sans effet de bord.
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise TenantNotFoundError

    # À partir d'ici, lectures des tables cadrées RLS : poser le contexte du tenant
    # cible pour que la politique `USING` n'expose QUE ses lignes (exigence 1.3).
    await set_tenant_context(session, tenant_id)

    tenant_export = TenantProfileExport(
        id=tenant.id,
        name=tenant.name,
        region_id=tenant.region_id,
        status=tenant.status,
        created_at=tenant.created_at,
    )

    # Centre (1:1 avec le tenant en v1 ; la RLS cadre déjà au tenant courant).
    center_row = (
        await session.execute(select(MedicalCenter))
    ).scalars().first()
    center_export: CenterExport | None = None
    if center_row is not None:
        lon, lat = await _coords(session, center_row.id)
        center_export = CenterExport(
            id=center_row.id,
            tenant_id=center_row.tenant_id,
            name=center_row.name,
            region_id=center_row.region_id,
            address=center_row.address,
            longitude=lon,
            latitude=lat,
            phone=center_row.phone,
            email=center_row.email,
            opening_hours=center_row.opening_hours,
            is_public=center_row.is_public,
        )

    # Liens de services (liaison cadrée au tenant).
    service_rows = (
        await session.execute(
            select(MedicalCenterService).order_by(
                MedicalCenterService.medical_center_id,
                MedicalCenterService.service_type_id,
            )
        )
    ).scalars().all()
    services_export = [
        ServiceLinkExport(
            medical_center_id=row.medical_center_id,
            service_type_id=row.service_type_id,
            tenant_id=row.tenant_id,
        )
        for row in service_rows
    ]

    # Comptes applicatifs — SANS secret (exigence 1.2) : on ne projette jamais
    # `password_hash`, `setup_token_hash` ni `setup_token_selector`.
    user_rows = (
        await session.execute(select(AppUser).order_by(AppUser.email))
    ).scalars().all()
    users_export = [
        UserExport(
            id=row.id,
            tenant_id=row.tenant_id,
            email=row.email,
            role=row.role,
            is_active=row.is_active,
            failed_login_count=row.failed_login_count,
            locked_until=row.locked_until,
            setup_token_expires_at=row.setup_token_expires_at,
        )
        for row in user_rows
    ]

    # Patients (PHI cadré au tenant).
    patient_rows = (
        await session.execute(select(Patient).order_by(Patient.created_at, Patient.id))
    ).scalars().all()
    patients_export = [
        PatientExport(
            id=row.id,
            tenant_id=row.tenant_id,
            mrn=row.mrn,
            given_name=row.given_name,
            family_name=row.family_name,
            dob=row.dob,
            sex=row.sex,
            phone=row.phone,
            national_id=row.national_id,
            consent_to_store=row.consent_to_store,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            version=row.version,
            deleted_at=row.deleted_at,
        )
        for row in patient_rows
    ]

    # Visites (PHI cadré au tenant).
    visit_rows = (
        await session.execute(select(Visit).order_by(Visit.updated_at, Visit.id))
    ).scalars().all()
    visits_export = [
        VisitExport(
            id=row.id,
            tenant_id=row.tenant_id,
            patient_id=row.patient_id,
            visited_at=row.visited_at,
            reason=row.reason,
            notes=row.notes,
            created_by=row.created_by,
            updated_at=row.updated_at,
            version=row.version,
            deleted_at=row.deleted_at,
        )
        for row in visit_rows
    ]

    # Données de synchronisation (conflits + registre d'idempotence), cadrées tenant.
    conflict_rows = (
        await session.execute(select(SyncConflict).order_by(SyncConflict.id))
    ).scalars().all()
    conflicts_export = [
        SyncConflictExport(
            id=row.id,
            tenant_id=row.tenant_id,
            entity=row.entity,
            entity_id=row.entity_id,
            loser_payload=row.loser_payload,
            winner_version=row.winner_version,
            sensitive=row.sensitive,
            resolved_by=row.resolved_by,
            created_at=row.created_at,
        )
        for row in conflict_rows
    ]
    mutation_rows = (
        await session.execute(
            select(SyncMutation).order_by(SyncMutation.created_at, SyncMutation.mutation_id)
        )
    ).scalars().all()
    mutations_export = [
        SyncMutationExport(
            mutation_id=row.mutation_id,
            tenant_id=row.tenant_id,
            entity=row.entity,
            entity_id=row.entity_id,
            result_status=row.result_status,
            server_version=row.server_version,
            created_at=row.created_at,
        )
        for row in mutation_rows
    ]

    return TenantExport(
        tenant=tenant_export,
        center=center_export,
        services=services_export,
        users=users_export,
        patients=patients_export,
        visits=visits_export,
        sync=SyncExport(conflicts=conflicts_export, mutations=mutations_export),
    )


async def erase_tenant(
    session: AsyncSession, tenant_id: uuid.UUID
) -> EraseReport:
    """Efface définitivement les données d'un tenant, cadré et audité (exigence 2).

    Opération **destructive d'exception** (distincte du soft-delete du CRUD normal) :
    supprime physiquement les PHI et les données du tenant, puis son identité. Suit
    strictement l'ADR-1, **voie (ii) — DELETE explicites cadrés** (jamais un cascade
    `DELETE FROM tenant` seul).

    Garde-fous et garanties :

    - **Désactivation préalable obligatoire (exigence 2.6).** Exige
      `tenant.status == 'inactive'` ; sinon lève `TenantStillActiveError` (→ 409).
      Comme la désactivation coupe déjà tout accès/écriture des membres, aucune
      écriture concurrente ne survient pendant l'opération.
    - **Cadrage au tenant (exigence 2.4, P1/P2).** Pose `app.tenant_id` sur le tenant
      cible (`set_tenant_context`) : la RLS `USING (tenant_id = app.tenant_id)` borne
      chaque DELETE des tables cadrées au seul tenant visé — un bug de ciblage ne
      peut toucher aucun autre tenant.
    - **Ordre imposé par les FK RESTRICT.** `patient.created_by`, `visit.created_by`
      et `visit.patient_id` sont `ON DELETE RESTRICT` : on supprime donc les enfants
      avant les parents, dans l'ordre (1) `visit`, (2) `patient`,
      (3) `medical_center_service`, (4) `sync_conflict`, (5) `sync_mutation`,
      (6) `refresh_token` des comptes du tenant, (7) `app_user` du tenant,
      (8) `medical_center`, puis (9) `DELETE FROM tenant` (table non cadrée RLS).
    - **Audit intact (exigence 2.3, ADR-1, NFR-3).** `audit_log` n'a aucune FK vers
      `tenant` : il est hors du graphe de cascade et n'est **jamais** touché ici
      (`audit_preserved=True` dans le rapport). L'audit d'intention d'effacement est
      écrit par le routeur (tâche 7), **avant** l'appel, dans sa propre transaction.

    Ne valide PAS la transaction : le `commit` incombe au routeur, de sorte que tout
    l'effacement soit atomique (une erreur annule l'ensemble des DELETE).

    Renvoie un `EraseReport` (compte de lignes supprimées par table, `audit_preserved`,
    `exported_first`). Lève `TenantNotFoundError` (→ 404) si le tenant n'existe pas, et
    `TenantStillActiveError` (→ 409) si le tenant n'est pas désactivé — sans effet de
    bord dans les deux cas.
    """
    # Identité du tenant (table NON cadrée RLS : lecture directe). Un tenant absent
    # est une erreur d'entrée, sans effet de bord (exigence 2.7).
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise TenantNotFoundError

    # Garde-fou obligatoire : refuser l'effacement d'un tenant encore actif
    # (exigence 2.6). Levé AVANT toute suppression → aucun effet de bord (→ 409).
    if tenant.status != "inactive":
        raise TenantStillActiveError

    # Poser le contexte du tenant cible : la RLS `USING` borne chaque DELETE suivant
    # (tables cadrées) au seul tenant visé — aucune donnée d'un autre tenant touchée
    # (exigence 2.4, P1/P2).
    await set_tenant_context(session, tenant_id)

    erased_counts: dict[str, int] = {}

    # (1–5) Tables cadrées RLS, dans l'ordre imposé par les FK RESTRICT
    # (`created_by`/`patient_id`) : enfants d'abord. Chaque DELETE est cadré par la
    # RLS au tenant courant (pas de clause `tenant_id` nécessaire, mais la RLS la
    # garantit de toute façon).
    for table in ("visit", "patient", "medical_center_service", "sync_conflict", "sync_mutation"):
        result = await session.execute(text(f"DELETE FROM {table}"))
        erased_counts[table] = result.rowcount or 0

    # (6) Jetons de refresh des comptes du tenant. `refresh_token` n'est PAS cadrée
    # RLS (chemin pré-auth) : elle est liée au tenant indirectement via
    # `user_id → app_user`. On la borne donc explicitement aux comptes du tenant
    # cible (sous-requête sur `app_user`, lui-même cadré RLS au tenant courant).
    result = await session.execute(
        text(
            "DELETE FROM refresh_token WHERE user_id IN "
            "(SELECT id FROM app_user WHERE tenant_id = :tid)"
        ),
        {"tid": str(tenant_id)},
    )
    erased_counts["refresh_token"] = result.rowcount or 0

    # (7) Comptes du tenant : plus aucun RESTRICT `created_by` ne pointe vers eux
    # une fois `patient`/`visit` supprimés. Cadré RLS au tenant courant.
    result = await session.execute(text("DELETE FROM app_user"))
    erased_counts["app_user"] = result.rowcount or 0

    # (8) Profil du centre. Cadré RLS au tenant courant.
    result = await session.execute(text("DELETE FROM medical_center"))
    erased_counts["medical_center"] = result.rowcount or 0

    # (9) Identité du tenant (table NON cadrée RLS) : DELETE ciblé explicitement.
    # Le rôle applicatif a le GRANT DELETE sur `tenant` (migration 0002).
    result = await session.execute(
        text("DELETE FROM tenant WHERE id = :tid"),
        {"tid": str(tenant_id)},
    )
    erased_counts["tenant"] = result.rowcount or 0

    # `audit_log` n'est JAMAIS supprimé (ADR-1) : hors du graphe de cascade (aucune FK
    # vers tenant), jamais ciblé ci-dessus. `audit_preserved=True` l'atteste.
    return EraseReport(
        tenant_id=tenant_id,
        erased_counts=erased_counts,
        audit_preserved=True,
        exported_first=False,
    )
