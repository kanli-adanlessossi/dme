"""Modèles SQLAlchemy.

Importer les modèles ici garantit que leurs tables sont enregistrées sur
`Base.metadata` dès que le paquet `app.models` est importé (nécessaire pour
l'autogénération Alembic et pour l'accès ORM applicatif).

Tâche 4.1 — cœur multi-tenant :
- `Region` : table de référence (FK avant-compatible ; amorçage en tâche 6) ;
- `Tenant` : identité organisationnelle du centre ;
- `MedicalCenter` : profil public cadré au tenant (1:1 avec tenant en v1) ;
- `AppUser` : comptes applicatifs cadrés au tenant (tenant_id NUL = platform_admin).

Tâche 5.1 — audit transversal :
- `AuditLog` : journal append-only, distribué-compatible (tenant_id nullable).

Tâche 6.1 — données de référence :
- `ServiceType` : référentiel des services proposés (non cadré au tenant).

Tâche 11 — profil du centre + validation géo (cadré au tenant) :
- `MedicalCenterService` : liaison centre ↔ service_type (RLS forcée, P2).

Tâche 9.1 — domaine clinique synchronisable (PHI, cadré au tenant) :
- `Patient` : dossier patient (consentement, soft-delete, champs de sync) ;
- `Visit` : visite clinique (soft-delete, champs de sync).

Tâche 10 — moteur de synchronisation côté serveur (cadré au tenant) :
- `SyncMutation` : registre d'idempotence du push (clé = `mutation_id`, P10) ;
- `SyncConflict` : journal des conflits résolus en LWW (perdant conservé, P12).

Tâche 9.2 (G-9) — durcissement JWT : rotation + denylist du refresh :
- `RefreshToken` : refresh émis (jti/family/consumed_at), consulté sur le seul
  chemin en ligne `/api/auth/refresh` (pas de RLS par-tenant, chemin pré-auth).
"""

from __future__ import annotations

from app.models.app_user import AppUser
from app.models.audit_log import AuditLog
from app.models.medical_center import MedicalCenter
from app.models.medical_center_service import MedicalCenterService
from app.models.patient import Patient
from app.models.refresh_token import RefreshToken
from app.models.region import Region
from app.models.service_type import ServiceType
from app.models.sync_conflict import SyncConflict
from app.models.sync_mutation import SyncMutation
from app.models.tenant import Tenant
from app.models.visit import Visit

__all__ = [
    "AppUser",
    "AuditLog",
    "MedicalCenter",
    "MedicalCenterService",
    "Patient",
    "RefreshToken",
    "Region",
    "ServiceType",
    "SyncConflict",
    "SyncMutation",
    "Tenant",
    "Visit",
]
