"""Tests unitaires du contrat d'export par tenant (spec `tenant-data-rights`, #2).

Tâche 4.2 — schéma `TenantExport`. Isolent la **forme pure** de l'export, sans
base : l'export complet cadré au tenant sous RLS/PostGIS réel est couvert par les
tests d'intégration (tâche 8). On vérifie ici les invariants du contrat qui
garantissent la propriété P1 (« export sans secret ») au niveau du schéma :

- **Aucun secret par construction (exigence 1.2, NFR-2).** Les projections
  n'exposent AUCUN champ secret : ni `password_hash`, ni `setup_token_hash`, ni
  `setup_token_selector`, ni aucun jeton de refresh. Ces champs n'existent pas dans
  les modèles d'export — un secret ne peut donc pas fuiter même par erreur.
- **Forme stable et documentée (exigence 1.5).** `TenantExport` porte un
  `schema_version` et une structure par entité, sérialisable en JSON portable pour
  un import/restitution futur.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from app.schemas.tenant_data import (
    EXPORT_SCHEMA_VERSION,
    CenterExport,
    EraseReport,
    PatientExport,
    SyncExport,
    TenantExport,
    TenantProfileExport,
    UserExport,
    VisitExport,
)

# Champs secrets qui ne DOIVENT jamais apparaître dans un export (exigence 1.2).
_FORBIDDEN_SECRET_FIELDS = {
    "password_hash",
    "setup_token_hash",
    "setup_token_selector",
    "refresh_token",
    "token_hash",
    "jti",
}


def _minimal_export() -> TenantExport:
    """Construit un `TenantExport` minimal valide (un tenant, un centre, un compte)."""
    now = datetime.now(UTC)
    tid = uuid4()
    return TenantExport(
        tenant=TenantProfileExport(
            id=tid, name="Centre test", region_id=uuid4(), status="active", created_at=now
        ),
        center=CenterExport(
            id=uuid4(),
            tenant_id=tid,
            name="Centre test",
            region_id=uuid4(),
            longitude=1.2255,
            latitude=6.1319,
            is_public=True,
        ),
        users=[
            UserExport(
                id=uuid4(),
                tenant_id=tid,
                email="admin@centre.tg",
                role="tenant_admin",
                is_active=True,
                failed_login_count=0,
            )
        ],
    )


def _all_field_names() -> set[str]:
    """Agrège les noms de champs de toutes les projections d'export."""
    models = (
        TenantExport,
        TenantProfileExport,
        CenterExport,
        UserExport,
        PatientExport,
        VisitExport,
        SyncExport,
    )
    names: set[str] = set()
    for model in models:
        names.update(model.model_fields.keys())
    return names


# --- Aucun secret dans le contrat d'export (exigence 1.2, P1) ------------------


def test_aucun_champ_secret_dans_les_schemas_export() -> None:
    """Aucune projection d'export ne déclare de champ secret (exclusion structurelle)."""
    leaked = _all_field_names() & _FORBIDDEN_SECRET_FIELDS
    assert leaked == set(), f"Champs secrets exposés dans l'export : {sorted(leaked)}"


def test_user_export_ne_porte_aucun_secret() -> None:
    """Le compte exporté expose l'identité métier, jamais un secret."""
    fields = set(UserExport.model_fields.keys())
    assert fields & _FORBIDDEN_SECRET_FIELDS == set()
    # Champs métier attendus présents.
    assert {"email", "role", "is_active"} <= fields


def test_export_serialise_sans_secret_en_json() -> None:
    """La sérialisation JSON d'un export ne contient aucune clé secrète."""
    export = _minimal_export()
    payload = json.loads(export.model_dump_json())

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in _FORBIDDEN_SECRET_FIELDS, (
                    f"Clé secrète '{key}' présente dans l'export JSON"
                )
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)


# --- Forme stable et documentée (exigence 1.5) ---------------------------------


def test_schema_version_par_defaut() -> None:
    """`TenantExport` porte la version de format par défaut (import futur)."""
    export = _minimal_export()
    assert export.schema_version == EXPORT_SCHEMA_VERSION


def test_structure_par_entite_presente() -> None:
    """Le contrat expose bien une entrée par entité du tenant (forme documentée)."""
    fields = set(TenantExport.model_fields.keys())
    assert {
        "tenant",
        "center",
        "services",
        "users",
        "patients",
        "visits",
        "sync",
    } <= fields


def test_collections_vides_par_defaut() -> None:
    """Les collections d'entités sont des listes vides par défaut (sérialisation sûre)."""
    export = TenantExport(
        tenant=TenantProfileExport(
            id=uuid4(),
            name="C",
            region_id=uuid4(),
            status="inactive",
            created_at=datetime.now(UTC),
        )
    )
    assert export.center is None
    assert export.services == []
    assert export.users == []
    assert export.patients == []
    assert export.visits == []
    assert export.sync.conflicts == []
    assert export.sync.mutations == []


# --- Contrat du rapport d'effacement (exigence 2, tâche 6.2) -------------------


def test_erase_report_valeurs_par_defaut() -> None:
    """Un `EraseReport` minimal porte les invariants ADR-1 par défaut."""
    report = EraseReport(tenant_id=uuid4())
    assert report.erased_counts == {}
    # Invariant structurel ADR-1 : l'audit n'est jamais touché par l'effacement.
    assert report.audit_preserved is True
    # Décision tranchée : export préalable optionnel (information, pas condition).
    assert report.exported_first is False


def test_erase_report_compte_par_table() -> None:
    """Le rapport rend la portée exacte de l'effacement (lignes par table)."""
    tid = uuid4()
    counts = {
        "visit": 3,
        "patient": 2,
        "medical_center_service": 1,
        "sync_conflict": 0,
        "sync_mutation": 4,
        "refresh_token": 5,
        "app_user": 2,
        "medical_center": 1,
        "tenant": 1,
    }
    report = EraseReport(
        tenant_id=tid,
        erased_counts=counts,
        audit_preserved=True,
        exported_first=True,
    )
    assert report.tenant_id == tid
    assert report.erased_counts == counts
    assert report.exported_first is True


def test_erase_report_ne_porte_aucun_secret() -> None:
    """Le rapport d'effacement n'expose aucun champ secret (NFR-2)."""
    fields = set(EraseReport.model_fields.keys())
    assert fields & _FORBIDDEN_SECRET_FIELDS == set()
    assert {"tenant_id", "erased_counts", "audit_preserved", "exported_first"} <= fields
