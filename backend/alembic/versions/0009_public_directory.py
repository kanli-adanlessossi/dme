"""Chemin de lecture publique : politique RLS « projection sûre » (tâche 12).

Exigences 7.2, 7.3, 7.4, 8.1, 8.2, 8.3 — vérifie P4 (confidentialité des PHI) et
P8 (surface publique « actifs seulement »).

L'annuaire et la carte publics (routeur `public`) lisent `medical_center` SANS
authentification ni contexte de tenant. Or `medical_center` porte la RLS
**activée + FORCÉE** avec la seule politique `tenant_isolation` (migration 0003) :
une lecture sans `app.tenant_id` ne voit AUCUNE ligne. Il faut donc un **chemin de
lecture publique dédié**, conforme au design (« La lecture publique passe par un
chemin dédié en lecture seule exposant une projection sûre — pas de filtre RLS par
tenant, mais une liste blanche de colonnes/points d'accès — et excluant les
tenants inactifs »).

Choix : une **politique RLS PERMISSIVE de SELECT** `public_read`, activée
uniquement lorsque la requête pose explicitement le drapeau transaction-local
``app.public_read = 'on'`` (posé par la dépendance `public_session`, jamais par
une requête cadrée au tenant). Les politiques PERMISSIVE se cumulent par OU : une
ligne est donc visible soit par `tenant_isolation` (chemin authentifié cadré au
tenant), soit par `public_read` (chemin public), sans que l'un n'élargisse
l'autre. La politique `public_read` n'ouvre QUE le SELECT (lecture seule) et
restreint les lignes aux centres :

- appartenant à un tenant **actif** (`tenant.status = 'active'`) → exclut les
  tenants désactivés (P8, exigence 8.2) ;
- marqués publics (`is_public`).

Pourquoi une politique plutôt qu'une vue détenue par un superuser : l'application
tourne sous le rôle `dme_app` (NON-superuser, NOBYPASSRLS). Une vue « SECURITY
DEFINER » dépendrait du privilège BYPASSRLS de son propriétaire, non garanti en
production. Une politique PERMISSIVE conditionnée à un drapeau de session est
explicite, cadrée au moteur et indépendante du superuser.

La même ouverture est appliquée à la table de liaison `medical_center_service`
(services d'un centre, exigence 7.4) sur le même modèle : SELECT seulement, pour
les liens d'un centre dont le tenant est actif et le centre public.

`region` et `service_type` sont des tables de **référence** sans RLS : elles sont
déjà lisibles publiquement et n'ont besoin d'aucune politique.

Aucune ouverture publique sur `patient`/`visit`/`app_user` : les PHI restent
derrière la seule RLS `tenant_isolation` (P4). Le drapeau `app.public_read` n'a
aucun effet sur ces tables.

Revision ID: 0009_public_directory
Revises: 0008_center_profile_geo
Create Date: 2024-01-09 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Identifiants de révision, utilisés par Alembic.
revision: str = "0009_public_directory"
down_revision: str | None = "0008_center_profile_geo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Nom de la politique de lecture publique (distincte de `tenant_isolation`).
_PUBLIC_POLICY = "public_read"

# Drapeau transaction-local activant la lecture publique. Posé uniquement par la
# dépendance `public_session` (jamais par une requête cadrée au tenant). Tolérant
# à l'absence (`current_setting(..., true)` → NULL ≠ 'on' → aucune ligne ouverte).
_PUBLIC_FLAG = "current_setting('app.public_read', true) = 'on'"

# Prédicat « centre public d'un tenant actif » : exclut les tenants inactifs (P8)
# et les centres non publics. Référence `tenant` (sans RLS, lisible).
_ACTIVE_PUBLIC_CENTER = (
    "is_public AND EXISTS ("
    "  SELECT 1 FROM tenant t"
    "  WHERE t.id = medical_center.tenant_id AND t.status = 'active'"
    ")"
)

# Même prédicat pour la liaison services, qualifié via le centre lié.
_ACTIVE_PUBLIC_LINK = (
    "EXISTS ("
    "  SELECT 1 FROM medical_center mc"
    "  JOIN tenant t ON t.id = mc.tenant_id"
    "  WHERE mc.id = medical_center_service.medical_center_id"
    "    AND mc.is_public"
    "    AND t.status = 'active'"
    ")"
)


def upgrade() -> None:
    """Ajoute les politiques PERMISSIVE de SELECT public (centres + services)."""
    op.execute(
        f"CREATE POLICY {_PUBLIC_POLICY} ON medical_center\n"
        f"  FOR SELECT\n"
        f"  USING ({_PUBLIC_FLAG} AND ({_ACTIVE_PUBLIC_CENTER}));"
    )
    op.execute(
        f"CREATE POLICY {_PUBLIC_POLICY} ON medical_center_service\n"
        f"  FOR SELECT\n"
        f"  USING ({_PUBLIC_FLAG} AND ({_ACTIVE_PUBLIC_LINK}));"
    )


def downgrade() -> None:
    """Supprime les politiques de lecture publique (ordre inverse)."""
    op.execute(
        f"DROP POLICY IF EXISTS {_PUBLIC_POLICY} ON medical_center_service;"
    )
    op.execute(f"DROP POLICY IF EXISTS {_PUBLIC_POLICY} ON medical_center;")
