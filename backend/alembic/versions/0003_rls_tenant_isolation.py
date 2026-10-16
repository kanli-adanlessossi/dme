"""RLS : active, FORCE la Row-Level Security et pose les politiques tenant.

Tâche 4.2 (NFR-1, 1.2, 1.3, 5.2). Cette migration verrouille l'isolation
multi-tenant au niveau du moteur, indépendamment du code applicatif.

Pour chaque table **cadrée au tenant** créée jusqu'ici — `medical_center` et
`app_user` — on applique :

1. ``ENABLE ROW LEVEL SECURITY`` : active la RLS sur la table.
2. ``FORCE ROW LEVEL SECURITY`` : rend la RLS applicable **même au propriétaire
   de la table** (design : « ALTER TABLE ... FORCE ROW LEVEL SECURITY »). Sans
   cela, le propriétaire du schéma (celui qui exécute les migrations) resterait
   exempté des politiques ; FORCE garantit que personne hormis un superuser /
   BYPASSRLS n'échappe à l'isolation. Le rôle applicatif `dme_app` (tâche 4.1)
   est précisément NON-superuser et NOBYPASSRLS.
3. Une politique ``tenant_isolation`` portant à la fois :
   - ``USING (tenant_id = current_setting('app.tenant_id', true)::uuid)``
     → filtre les lignes **lues** (SELECT/UPDATE/DELETE) au tenant courant ;
   - ``WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)``
     → rejette toute ligne **écrite** (INSERT/UPDATE) dont le `tenant_id` ne
       correspond pas au contexte. C'est la défense contre un `tenant_id` forgé
       dans un corps de requête (P2 — tenancy autoritaire côté serveur).

   Ensemble, USING + WITH CHECK réalisent P1 (isolation inter-tenant) et P2.

   Le second argument `true` de ``current_setting(..., true)`` rend l'appel
   tolérant à l'absence du paramètre : si `app.tenant_id` n'a jamais été posé,
   `current_setting` renvoie NULL au lieu de lever une erreur. La comparaison
   ``tenant_id = NULL`` vaut alors NULL (donc « faux » pour le filtre), si bien
   qu'une requête **sans contexte de tenant ne voit aucune ligne** — propriété
   recherchée (une connexion brute sans `SET LOCAL app.tenant_id` est inoffensive).

Tables **volontairement non protégées** par une politique par-tenant :
- ``region`` : table de **référence** (partagée, non cadrée au tenant) → pas de RLS.
- ``service_type`` : idem (référence) — n'existe pas encore (tâche 6).
- ``tenant`` : la table d'identité des tenants elle-même ne porte pas de colonne
  `tenant_id` cohérente avec le contexte ; une politique par-tenant y serait
  circulaire. Son accès est cadré par le rôle/route applicatif, pas par la RLS
  par-tenant.
- ``patient`` / ``visit`` : n'existent pas encore (tâche 9) — la migration qui
  les crée posera leur propre `tenant_isolation` sur le même modèle.
- ``audit_log`` : n'existe pas encore (tâche 5) — traité spécialement (append-only,
  écrivain non contraint par le contexte de tenant).

Remarque sur ``app_user`` (tenant_id NULLABLE) : les comptes `platform_admin`
ont `tenant_id` NUL. Avec la politique ci-dessus, ``NULL = <uuid>`` vaut NULL,
donc **les lignes à tenant NUL ne sont visibles d'aucun contexte tenant
applicatif** — comportement voulu par le design (« les lignes à tenant NULL ne
sont visibles d'aucun contexte tenant applicatif »). L'accès plateforme passe par
un chemin de code distinct (hors contexte tenant), non par cette politique.

Revision ID: 0003_rls_tenant_isolation
Revises: 0002_tenant_core
Create Date: 2024-01-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0003_rls_tenant_isolation"
down_revision: str | None = "0002_tenant_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables cadrées au tenant à protéger par RLS. `patient`/`visit` (tâche 9) et
# `audit_log` (tâche 5) n'existent pas encore et seront ajoutés par leurs propres
# migrations. `region`/`service_type` (référence) et `tenant` (identité) ne sont
# PAS cadrés au tenant et restent hors RLS par-tenant.
_TENANT_SCOPED_TABLES = ("medical_center", "app_user")

# Nom unique des politiques, aligné sur le design (CREATE POLICY tenant_isolation).
_POLICY_NAME = "tenant_isolation"

# Prédicat partagé par USING et WITH CHECK. `current_setting(..., true)` est
# tolérant à l'absence du paramètre (renvoie NULL → aucune ligne visible).
_TENANT_PREDICATE = "tenant_id = current_setting('app.tenant_id', true)::uuid"


def upgrade() -> None:
    """Active + FORCE la RLS et crée la politique `tenant_isolation` par table."""
    for table in _TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {_POLICY_NAME} ON {table}\n"
            f"  USING ({_TENANT_PREDICATE})\n"
            f"  WITH CHECK ({_TENANT_PREDICATE});"
        )


def downgrade() -> None:
    """Supprime la politique puis désactive la RLS, dans l'ordre inverse."""
    for table in reversed(_TENANT_SCOPED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
