"""Schémas du domaine visite (tâche 3), cadrés au tenant et synchronisables.

Couvre `GET/POST /api/visits` et `DELETE /api/visits/{id}` (exigence 5).
DELETE est logique (soft-delete, P6). Les PHI visite ne sont jamais exposées
publiquement (P4).

Comme pour le patient, l'entité porte les champs de synchronisation (`id` UUIDv7
client, `version`, `updated_at`, `deleted_at`) et le `tenant_id` n'est jamais
accepté dans le corps (P2). Les notes de visite sont une entité cliniquement
sensible : un conflit de sync les marque pour revue humaine plutôt que de les
écraser (NFR-6, P12).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class VisitBase(BaseModel):
    """Champs métier communs aux lectures et écritures visite."""

    patient_id: UUID = Field(..., description="Patient concerné (même tenant).")
    visited_at: datetime = Field(..., description="Date/heure de la visite.")
    reason: str | None = Field(None, examples=["Consultation générale"])
    notes: str | None = Field(
        None,
        description="Notes cliniques — entité sensible (conflit marqué pour revue, P12).",
    )


class VisitCreateRequest(VisitBase):
    """Création d'une visite.

    `id` est fourni par le client (UUIDv7). `tenant_id` est **absent** par
    conception (P2) ; le serveur l'appose depuis le contexte authentifié.
    """

    id: UUID = Field(
        ...,
        description="UUIDv7 généré côté client (clé primaire stable hors ligne).",
    )


class VisitResponse(VisitBase):
    """Projection d'une visite avec ses champs de synchronisation.

    Aligné sur le modèle de données du design (section « Data Models ») et la
    table `visit` (migration 0006) : la visite ne porte **pas** de `created_at`
    propre (contrairement au patient) — seul `updated_at` (base LWW) horodate la
    dernière mutation. On n'expose donc que les champs effectivement stockés.
    """

    id: UUID
    tenant_id: UUID = Field(..., description="Apposé par le serveur ; lecture seule (P2).")
    updated_at: datetime
    version: int = Field(..., ge=1)
    deleted_at: datetime | None = Field(
        None, description="Horodatage de suppression logique (P6) ; nul si actif."
    )
