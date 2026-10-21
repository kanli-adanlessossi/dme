"""Schémas d'export/effacement par tenant (spec `tenant-data-rights`, #2).

Couvre l'opération transversale `platform_admin`
`POST /api/admin/tenants/{id}/export` (exigence 1) — l'endpoint lui-même relève de
la tâche 5 ; ce module ne fige que la **forme de l'export** produite par le service
`services/tenant_data.export_tenant` (tâche 4).

## Contrat d'export (stable et documenté — exigence 1.5)

`TenantExport` est un **JSON structuré par entité** destiné à l'archivage, au
transfert ou à une **restitution/import futur** (l'import n'est PAS implémenté ici,
voir design « Hors périmètre » ; seule la stabilité du format est garantie). Chaque
entité cadrée au tenant y figure sous une projection dédiée. Invariants du contrat :

- **Aucun secret (exigence 1.2, NFR-2).** Les projections excluent *par
  construction* toute empreinte de mot de passe (`password_hash`), toute empreinte
  de setup-token (`setup_token_hash`), le sélecteur de setup-token
  (`setup_token_selector`) et tout jeton de refresh (`refresh_token`). Ces champs
  n'existent tout simplement pas dans les modèles ci-dessous : un secret ne peut
  donc pas fuiter même par erreur de peuplement.
- **Cadré au tenant (exigence 1.3).** L'export ne porte que les données du tenant
  demandé ; le cadrage est garanti côté service par le contexte RLS posé sur le
  tenant cible (voir `services/tenant_data`).
- **`schema_version`.** Un entier de version du format, pour qu'un futur import
  sache interpréter la structure. Incrémenté sur tout changement incompatible.

## Volumétrie (note, NFR-4)

L'export v1 est **tout-en-mémoire** (ces modèles portent des listes Python
complètes). C'est suffisant pour la taille d'un tenant réaliste. Pour un très gros
tenant, une évolution vers un export **en flux** (NDJSON par entité) est notée comme
amélioration future (design, C1) — non implémentée ici, et sans impact sur la forme
documentée par entité.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# Version du format d'export. À incrémenter sur tout changement **incompatible** de
# la structure (ajout de champ optionnel = compatible, pas d'incrément requis).
EXPORT_SCHEMA_VERSION = 1


class TenantProfileExport(BaseModel):
    """Identité du tenant (table `tenant`, non cadrée RLS)."""

    id: UUID
    name: str
    region_id: UUID
    status: str
    created_at: datetime


class CenterExport(BaseModel):
    """Profil du centre (`medical_center`).

    Les coordonnées géographiques sont projetées en longitude/latitude WGS84
    (SRID 4326) plutôt qu'en géométrie binaire opaque, pour un export portable.
    """

    id: UUID
    tenant_id: UUID
    name: str
    region_id: UUID
    address: str | None = None
    longitude: float | None = None
    latitude: float | None = None
    phone: str | None = None
    email: str | None = None
    opening_hours: Any | None = None
    is_public: bool


class ServiceLinkExport(BaseModel):
    """Lien centre ↔ type de service (`medical_center_service`)."""

    medical_center_id: UUID
    service_type_id: UUID
    tenant_id: UUID


class UserExport(BaseModel):
    """Compte applicatif (`app_user`) — SANS AUCUN secret (exigence 1.2).

    N'expose jamais `password_hash`, `setup_token_hash` ni `setup_token_selector` :
    ces champs sont volontairement absents du modèle.
    """

    id: UUID
    tenant_id: UUID | None = None
    email: str
    role: str
    is_active: bool
    failed_login_count: int
    locked_until: datetime | None = None
    setup_token_expires_at: datetime | None = None


class PatientExport(BaseModel):
    """Patient (`patient`, PHI cadré au tenant)."""

    id: UUID
    tenant_id: UUID
    mrn: str
    given_name: str
    family_name: str
    dob: date | None = None
    sex: str | None = None
    phone: str | None = None
    national_id: str | None = None
    consent_to_store: bool
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: datetime | None = None


class VisitExport(BaseModel):
    """Visite clinique (`visit`, PHI cadré au tenant)."""

    id: UUID
    tenant_id: UUID
    patient_id: UUID
    visited_at: datetime | None = None
    reason: str | None = None
    notes: str | None = None
    created_by: UUID
    updated_at: datetime
    version: int
    deleted_at: datetime | None = None


class SyncMutationExport(BaseModel):
    """Entrée du registre d'idempotence de sync (`sync_mutation`)."""

    mutation_id: UUID
    tenant_id: UUID
    entity: str
    entity_id: UUID
    result_status: str
    server_version: int | None = None
    created_at: datetime


class SyncConflictExport(BaseModel):
    """Conflit de synchronisation consigné (`sync_conflict`)."""

    id: int
    tenant_id: UUID
    entity: str
    entity_id: UUID
    loser_payload: dict[str, Any]
    winner_version: int
    sensitive: bool
    resolved_by: UUID | None = None
    created_at: datetime


class SyncExport(BaseModel):
    """Agrégat des données de synchronisation (conflits + mutations)."""

    conflicts: list[SyncConflictExport] = Field(default_factory=list)
    mutations: list[SyncMutationExport] = Field(default_factory=list)


class TenantExport(BaseModel):
    """Export complet et portable des données d'un tenant (exigence 1).

    JSON structuré par entité, **sans aucun secret**, cadré au tenant demandé. La
    forme est **stable et documentée** pour rendre un import/restitution futur
    possible (exigence 1.5). `schema_version` identifie la version du format.
    """

    schema_version: int = Field(
        default=EXPORT_SCHEMA_VERSION,
        description="Version du format d'export (pour un import futur).",
    )
    tenant: TenantProfileExport
    center: CenterExport | None = Field(
        default=None,
        description="Profil du centre (1:1 avec le tenant en v1 ; None si absent).",
    )
    services: list[ServiceLinkExport] = Field(default_factory=list)
    users: list[UserExport] = Field(default_factory=list)
    patients: list[PatientExport] = Field(default_factory=list)
    visits: list[VisitExport] = Field(default_factory=list)
    sync: SyncExport = Field(default_factory=SyncExport)


class EraseReport(BaseModel):
    """Compte-rendu d'un effacement de tenant (exigence 2, ADR-1).

    Retourné par `services/tenant_data.erase_tenant` après un effacement réussi.
    Il rend le résultat **vérifiable et auditable** :

    - `erased_counts` : nombre de lignes supprimées **par table**, dans l'ordre FK
      imposé (`visit`, `patient`, `medical_center_service`, `sync_conflict`,
      `sync_mutation`, `refresh_token`, `app_user`, `medical_center`, `tenant`).
      Permet de prouver la portée exacte de l'effacement.
    - `audit_preserved` : **toujours `True`** (invariant structurel, ADR-1).
      `audit_log` n'a aucune FK vers `tenant` : il est hors du graphe de cascade et
      n'est jamais touché par l'effacement. Ce champ matérialise cette garantie
      dans le rapport (exigence 2.3, NFR-3).
    - `exported_first` : **information**, non condition. Indique si un export a été
      réalisé avant l'effacement (décision tranchée : l'export préalable est
      optionnel ; l'endpoint, tâche 7, renseignera ce drapeau).
    """

    tenant_id: UUID
    erased_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Lignes supprimées par table (ordre FK).",
    )
    audit_preserved: bool = Field(
        default=True,
        description="Invariant ADR-1 : l'audit n'est jamais touché par l'effacement.",
    )
    exported_first: bool = Field(
        default=False,
        description="Un export a-t-il précédé l'effacement (information, pas condition).",
    )
