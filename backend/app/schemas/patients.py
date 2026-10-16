"""Schémas du domaine patient (tâche 3), cadrés au tenant et synchronisables.

Couvre `GET/POST /api/patients` et `GET/PATCH/DELETE /api/patients/{id}`
(exigence 5). DELETE est logique (soft-delete, P6). Les PHI patient ne sont
jamais exposées publiquement (P4).

Champs de synchronisation portés par l'entité (exigence 5.8 / NFR-6) :
- `id` : UUIDv7 **généré côté client** (évite les collisions hors ligne) ;
- `version` : compteur de version pour la détection de conflits ;
- `updated_at` : base de résolution Last-Write-Wins ;
- `deleted_at` : suppression logique.

Le `tenant_id` n'est jamais accepté dans le corps : le serveur l'appose depuis le
contexte authentifié (P2, exigence 5.2). `consent_to_store` est capturé à la
création (exigence 5.6). L'unicité patient est imposée par `UNIQUE(tenant_id, mrn)`
(exigence 5.4) ; `national_id` est optionnel (beaucoup de patients n'en ont pas).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class Sex(str, Enum):
    """Sexe administratif du dossier patient."""

    M = "M"
    F = "F"
    other = "other"
    unknown = "unknown"


class PatientBase(BaseModel):
    """Champs métier communs aux lectures et écritures patient."""

    mrn: str = Field(
        ...,
        min_length=1,
        description="Numéro de dossier local au tenant ; unique dans le tenant "
        "(exigence 5.4).",
        examples=["MRN-000123"],
    )
    given_name: str = Field(..., min_length=1, examples=["Koffi"])
    family_name: str = Field(..., min_length=1, examples=["Mensah"])
    dob: date | None = Field(None, examples=["1990-05-14"])
    sex: Sex = Field(default=Sex.unknown)
    phone: str | None = Field(None, examples=["+22890000000"])
    national_id: str | None = Field(
        None,
        description="Identifiant national optionnel (absent pour beaucoup de patients).",
    )


class PatientCreateRequest(PatientBase):
    """Création d'un patient.

    `id` est fourni par le client (UUIDv7) afin que l'enregistrement soit stable
    dès l'écriture locale hors ligne (NFR-6). `consent_to_store` est obligatoire
    (exigence 5.6). `tenant_id` est **absent** par conception (P2).
    """

    id: UUID = Field(
        ...,
        description="UUIDv7 généré côté client (clé primaire stable hors ligne).",
    )
    consent_to_store: bool = Field(
        ...,
        description="Indique si le patient a consenti au stockage de ses données "
        "(exigence 5.6).",
    )


class PatientUpdateRequest(BaseModel):
    """Mise à jour partielle d'un patient (aucun `id`/`tenant_id` modifiable)."""

    mrn: str | None = Field(None, min_length=1)
    given_name: str | None = Field(None, min_length=1)
    family_name: str | None = Field(None, min_length=1)
    dob: date | None = None
    sex: Sex | None = None
    phone: str | None = None
    national_id: str | None = None
    consent_to_store: bool | None = None


class PatientResponse(PatientBase):
    """Projection d'un patient avec ses champs de synchronisation."""

    id: UUID
    tenant_id: UUID = Field(..., description="Apposé par le serveur ; lecture seule (P2).")
    consent_to_store: bool
    created_at: datetime
    updated_at: datetime
    version: int = Field(..., ge=1)
    deleted_at: datetime | None = Field(
        None, description="Horodatage de suppression logique (P6) ; nul si actif."
    )
