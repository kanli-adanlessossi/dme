"""Service d'intégration et de cycle de vie des tenants (tâche 8).

Logique **cœur** des opérations transversales de l'administrateur de plateforme
(exigence 3) : intégrer un nouveau tenant (`create_tenant_onboarding`), lister les
tenants (`list_tenants`) et activer/désactiver un tenant (`set_tenant_status`).
Toutes ces opérations sont réservées au `platform_admin` ; le routeur (tâche 8)
les câble derrière `require_roles(platform_admin)` et les audite (NFR-3).

Chemin de code dédié et explicitement cadré (design, « Conception multi-tenant »,
« Les points d'accès transversaux de l'administrateur de plateforme utilisent un
chemin de code distinct et explicitement audité… ») :

- La table `tenant` n'est PAS cadrée par la RLS par-tenant (voir migration 0003 /
  design « Accès à la table tenant ») : sa lecture/écriture par le platform_admin
  ne dépend d'aucun `app.tenant_id`.
- En revanche, `medical_center` et `app_user` SONT sous RLS forcée (migration
  0003). Pour y écrire (création) ou y muter des lignes (désactivation des
  membres), le service pose explicitement `app.tenant_id` sur le **tenant cible**
  au sein de la transaction, via `set_tenant_context` — exactement comme le fait
  l'amorçage (`test_rls_isolation`). Le `WITH CHECK` de la politique garantit
  alors qu'aucune ligne forgée pour un autre tenant ne peut être écrite (P2).

Conservation des données (NFR-4, exigence 3.4) : la désactivation d'un tenant est
**logique** — `tenant.status = 'inactive'`. Aucune donnée n'est supprimée. Pour
que les membres du tenant se voient **refuser la connexion** (exigence 3.4), on
désactive aussi leurs comptes (`app_user.is_active = False`) ; la réactivation les
réactive. Les PHI (`patient`/`visit`, tâche 9) restent intactes : elles ne sont ni
touchées ni supprimées. La surface publique (tâche 12) filtre déjà sur les tenants
actifs (P8).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import generate_setup_token, hash_setup_token
from app.core.tenant_context import set_tenant_context
from app.models.app_user import AppUser
from app.models.medical_center import MedicalCenter
from app.models.region import Region
from app.models.tenant import Tenant
from app.schemas.admin import GeoPoint

# Rôle du compte administrateur de tenant initial créé lors de l'intégration.
_TENANT_ADMIN_ROLE = "tenant_admin"

# Boîte englobante du Togo (repli léger de la validation géographique, design
# « Validation de la frontière du Togo »). La validation fine `ST_Within` relève
# de la tâche 11 ; ici on rejette au moins les coordonnées manifestement hors
# pays à l'intégration (P7). Longitude ≈ [-0.15, 1.80], latitude ≈ [6.10, 11.14].
_TOGO_LON_MIN, _TOGO_LON_MAX = -0.15, 1.80
_TOGO_LAT_MIN, _TOGO_LAT_MAX = 6.10, 11.14


class RegionNotFoundError(Exception):
    """La région (code) fournie à l'intégration n'existe pas en base.

    Les 5 régions sont amorcées en tâche 6 ; une `region_code` valide au sens du
    schéma (enum) mais absente de la base (amorçage non exécuté) est une erreur de
    requête, traduite en 422 par le routeur.
    """


class CoordinatesOutsideTogoError(Exception):
    """Les coordonnées fournies ne se situent pas dans la frontière du Togo (P7)."""


class EmailAlreadyExistsError(Exception):
    """L'e-mail du tenant_admin initial est déjà utilisé par un compte existant.

    `app_user.email` est unique (migration 0002) : on détecte le doublon en amont
    pour renvoyer une erreur claire (409) plutôt qu'une violation de contrainte
    opaque.
    """


class TenantNotFoundError(Exception):
    """Le tenant ciblé par une opération de cycle de vie n'existe pas."""


@dataclass(frozen=True)
class TenantOnboardingResult:
    """Résultat d'une intégration : le tenant créé + le jeton de configuration.

    Le `setup_token` est le secret EN CLAIR, renvoyé **une seule fois** à
    l'administrateur de plateforme pour transmission hors bande au tenant_admin
    (exigence 3.2). Seule son empreinte est stockée en base.
    """

    tenant: Tenant
    center: MedicalCenter
    admin_user: AppUser
    setup_token: str
    setup_token_expires_at: datetime


def _validate_coordinates_in_togo(location: GeoPoint) -> None:
    """Rejette des coordonnées hors de la boîte englobante du Togo (P7, repli).

    Lève `CoordinatesOutsideTogoError` si le point est manifestement hors pays. La
    validation spatiale fine (`ST_Within` contre la géométrie réelle) est posée en
    tâche 11 ; ce repli garantit déjà qu'un centre intégré porte des coordonnées
    plausibles.
    """
    if not (_TOGO_LON_MIN <= location.longitude <= _TOGO_LON_MAX):
        raise CoordinatesOutsideTogoError
    if not (_TOGO_LAT_MIN <= location.latitude <= _TOGO_LAT_MAX):
        raise CoordinatesOutsideTogoError


async def _region_id_for_code(session: AsyncSession, code: str) -> uuid.UUID:
    """Résout l'`id` de la région depuis son code (ou lève `RegionNotFoundError`)."""
    result = await session.execute(select(Region.id).where(Region.code == code))
    region_id = result.scalar_one_or_none()
    if region_id is None:
        raise RegionNotFoundError
    return region_id


async def _email_exists(session: AsyncSession, email: str) -> bool:
    """Indique si un `app_user` porte déjà cet e-mail (unicité globale)."""
    result = await session.execute(select(AppUser.id).where(AppUser.email == email))
    return result.scalar_one_or_none() is not None


async def create_tenant_onboarding(
    session: AsyncSession,
    *,
    name: str,
    region_code: str,
    address: str,
    location: GeoPoint,
    phone: str,
    email: str,
    admin_email: str,
) -> TenantOnboardingResult:
    """Intègre un nouveau tenant en une seule transaction (exigences 3.1, 3.2).

    Crée, dans l'ordre et au sein de la transaction fournie par l'appelant :

    1. le `tenant` (nom, région, `status='active'`) — table non cadrée RLS ;
    2. le profil `medical_center` (nom, région, adresse, coordonnées validées dans
       le Togo, téléphone +228, e-mail) — table cadrée RLS ;
    3. le compte `tenant_admin` initial (`app_user`, rôle `tenant_admin`, cadré au
       nouveau tenant), sans mot de passe en clair : seul un **jeton de
       configuration à usage unique et expirant** (empreinte stockée) est émis
       (exigence 3.2).

    Pour écrire dans les tables cadrées RLS (`medical_center`, `app_user`), le
    service pose `app.tenant_id` sur le nouveau tenant via `set_tenant_context`
    (le `WITH CHECK` de la politique garantit l'absence d'injection inter-tenant,
    P2). Ne valide PAS la transaction : le `commit` (et l'audit) incombent au
    routeur, de sorte que l'intégration soit **atomique et auditée** (fail-closed,
    NFR-3).

    Renvoie un `TenantOnboardingResult` portant le jeton de configuration EN CLAIR
    (jamais reconsultable ensuite). Lève :
        RegionNotFoundError: code de région inconnu en base ;
        CoordinatesOutsideTogoError: coordonnées hors Togo (P7) ;
        EmailAlreadyExistsError: e-mail du tenant_admin déjà pris.
    """
    _validate_coordinates_in_togo(location)
    region_id = await _region_id_for_code(session, region_code)

    if await _email_exists(session, admin_email):
        raise EmailAlreadyExistsError

    # 1. Tenant (table non cadrée RLS : insertion directe, pas de contexte requis).
    tenant = Tenant(
        id=uuid.uuid4(),
        name=name,
        region_id=region_id,
        status="active",
    )
    session.add(tenant)
    # Flush pour matérialiser l'id/les valeurs serveur avant d'écrire les lignes
    # cadrées qui référencent `tenant.id`.
    await session.flush()

    # À partir d'ici, on écrit dans des tables sous RLS forcée : poser le contexte
    # du tenant cible pour satisfaire le WITH CHECK (chemin admin explicitement
    # cadré, design « Conception multi-tenant »).
    await set_tenant_context(session, tenant.id)

    # 2. Profil public du centre (coordonnées en EWKT, SRID 4326).
    center = MedicalCenter(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name=name,
        region_id=region_id,
        address=address,
        location=f"SRID=4326;POINT({location.longitude} {location.latitude})",
        phone=phone,
        email=email,
    )
    session.add(center)

    # 3. tenant_admin initial : jeton de configuration à usage unique (haché), pas
    #    de mot de passe en clair (exigence 3.2).
    setup_token = generate_setup_token()
    expires_at = datetime.now(UTC) + timedelta(
        seconds=get_settings().setup_token_ttl_seconds
    )
    admin_user = AppUser(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email=admin_email,
        password_hash=None,
        setup_token_hash=hash_setup_token(setup_token),
        setup_token_expires_at=expires_at,
        role=_TENANT_ADMIN_ROLE,
        is_active=True,
    )
    session.add(admin_user)
    await session.flush()

    return TenantOnboardingResult(
        tenant=tenant,
        center=center,
        admin_user=admin_user,
        setup_token=setup_token,
        setup_token_expires_at=expires_at,
    )


async def list_tenants(session: AsyncSession) -> list[Tenant]:
    """Renvoie tous les tenants (opération transversale platform_admin, exigence 3.3).

    La table `tenant` n'étant pas cadrée par la RLS par-tenant, la lecture
    transversale ne requiert aucun `app.tenant_id` : seul le cadrage par rôle
    (routeur) autorise cet accès. Ordonnée par date de création (croissante) pour
    une sortie déterministe.
    """
    result = await session.execute(select(Tenant).order_by(Tenant.created_at))
    return list(result.scalars().all())


async def get_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    """Charge un tenant par id (ou lève `TenantNotFoundError`)."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise TenantNotFoundError
    return tenant


async def set_tenant_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str,
) -> Tenant:
    """Active ou désactive un tenant (exigence 3.4) — suppression logique (NFR-4).

    Pose `tenant.status` à `active` ou `inactive`. **Aucune donnée n'est
    supprimée** : la désactivation est purement logique. Pour que les membres du
    tenant se voient refuser la connexion (exigence 3.4), on bascule aussi l'état
    actif de leurs comptes (`app_user.is_active`) de façon cohérente :

    - désactivation (`inactive`) → `is_active = False` sur tous les `app_user` du
      tenant → `verify_credentials` (tâche 7.1) refuse leur connexion (401) et
      `is_access_revoked` (tâche 7.5) propage la révocation à la sync (NFR-7) ;
    - réactivation (`active`) → `is_active = True`, les membres peuvent de nouveau
      se connecter.

    `app_user` étant sous RLS forcée, la mise à jour des comptes du tenant pose
    `app.tenant_id` sur le tenant cible (chemin admin cadré). La table `tenant`
    n'est pas cadrée RLS : son `UPDATE` ne dépend d'aucun contexte. Ne valide PAS
    la transaction (commit + audit au routeur).

    Lève `TenantNotFoundError` si le tenant n'existe pas.
    """
    tenant = await get_tenant(session, tenant_id)

    tenant.status = status

    # Refléter l'état sur les comptes membres (refus/rétablissement de connexion).
    # `app_user` est sous RLS forcée : poser le contexte du tenant cible pour que
    # l'UPDATE voie et modifie ses lignes (USING), sans fuite inter-tenant.
    await set_tenant_context(session, tenant.id)
    members_active = status == "active"
    await session.execute(
        update(AppUser)
        .where(AppUser.tenant_id == tenant.id)
        .values(is_active=members_active)
    )

    return tenant
