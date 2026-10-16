"""Amorçage des centres d'exemple pour le développement local (tâche 13).

Exigences 9.1 et 9.2. Ce module amorce, **par-dessus les 5 régions déjà
amorcées** (`app.seed.regions`, tâche 6.2), un jeu de données de démonstration
minimal mais complet pour faire tourner la pile en local :

- **un `platform_admin`** (compte transversal, `tenant_id` NUL) ;
- **un centre médical d'exemple par région** (5 au total), chacun matérialisé
  par un `tenant` (actif) + son profil `medical_center` + un `tenant_admin`
  dédié, avec des **coordonnées togolaises valides** (dans la boîte englobante du
  Togo, et — en pratique — à l'intérieur de la région correspondante).

Points de conception
--------------------

- **Dev-only, jamais en production.** Ce jeu de données porte des identifiants
  connus (mot de passe de démonstration partagé, `SEED_DEV_PASSWORD`) afin que les
  comptes soient immédiatement utilisables en local. Contrairement à l'intégration
  réelle d'un tenant (`services.admin.create_tenant_onboarding`, qui n'émet qu'un
  jeton de configuration à usage unique, exigence 3.2), l'amorçage pose
  directement un `password_hash` Argon2id pour la commodité du développement. Il
  ne s'exécute donc **que** lorsque `ENVIRONMENT != production` ; en production,
  `run_center_seed` est un no-op explicite.

- **Idempotence (ré-exécutable sans effet de bord).** L'amorçage est joué à chaque
  `docker compose up`. L'ancrage d'idempotence est l'**e-mail** des comptes
  (`app_user.email`, contrainte `UNIQUE` globale, migration 0002) : si le compte
  `platform_admin` ou le `tenant_admin` d'une région existe déjà, l'entité
  correspondante est laissée en l'état et n'est pas re-créée. Relancer l'amorçage
  ne crée donc ni doublon de tenant, ni doublon de centre, ni doublon de compte.

- **RLS respectée (comme le chemin admin, P2).** `tenant` n'est pas cadrée par la
  RLS par-tenant : son insertion est directe. En revanche `medical_center` et
  `app_user` sont sous **RLS forcée** (migration 0003) ; avant d'y écrire, on pose
  `app.tenant_id` sur le tenant cible via `set_tenant_context` (exactement comme
  `services.admin.create_tenant_onboarding`). Le `WITH CHECK` de la politique
  garantit qu'aucune ligne forgée pour un autre tenant ne peut être écrite.

- **Coordonnées togolaises valides (exigence 9.2, P7).** Chaque centre d'exemple
  porte des coordonnées dans la boîte englobante du Togo (longitude ∈ [-0.15,
  1.80], latitude ∈ [6.10, 11.14]) — ce sont des villes réelles de chaque région
  (Dapaong, Kara, Sokodé, Atakpamé, Lomé). Elles satisfont la validation géo de la
  tâche 11 (`ST_Within` contre l'union des régions amorcées).

- **Pas de SQL injecté.** Les valeurs scalaires sont liées via l'ORM / des
  paramètres ; les coordonnées sont posées en EWKT littéral construit à partir de
  flottants validés (même forme que `services.admin`).

Ordonnancement : exécuté par `entrypoint.sh` **après** l'amorçage des régions
(les tenants/centres référencent `region.id`). Hors conteneur, lançable via
`python -m app.seed` (voir `app/seed/__main__.py`).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.core.tenant_context import set_tenant_context
from app.models.app_user import AppUser
from app.models.medical_center import MedicalCenter
from app.models.region import Region
from app.models.tenant import Tenant

logger = logging.getLogger(__name__)

# Rôles des comptes amorcés.
_PLATFORM_ADMIN_ROLE = "platform_admin"
_TENANT_ADMIN_ROLE = "tenant_admin"

# Mot de passe de démonstration par défaut, posé en clair -> haché Argon2id pour
# tous les comptes amorcés. UNIQUEMENT pour le développement local : connu et
# partagé, il rend les comptes immédiatement utilisables sans flux de jeton de
# configuration. Surchargeable via `SEED_DEV_PASSWORD` (voir `app.core.config`).
_DEFAULT_DEV_PASSWORD = "dev-password-change-me"

# E-mail (stable) du platform_admin amorcé — ancre d'idempotence.
_PLATFORM_ADMIN_EMAIL = "platform-admin@dme.local"


@dataclass(frozen=True)
class CenterSeed:
    """Un centre d'exemple à amorcer pour une région donnée.

    `region_code` relie le centre à l'une des 5 régions canoniques amorcées
    (tâche 6.2). `longitude`/`latitude` sont des coordonnées togolaises valides
    (ville réelle de la région). Les e-mails sont les ancres d'idempotence.
    """

    region_code: str
    center_name: str
    address: str
    longitude: float
    latitude: float
    phone: str
    center_email: str
    admin_email: str


# Un centre d'exemple par région, du nord au sud (ordre canonique). Coordonnées =
# chefs-lieux/villes réelles de chaque région, toutes dans la frontière du Togo.
SAMPLE_CENTERS: tuple[CenterSeed, ...] = (
    CenterSeed(
        region_code="SAVANES",
        center_name="Centre de santé de Dapaong",
        address="Avenue de l'Hôpital, Dapaong",
        longitude=0.2082,
        latitude=10.8625,
        phone="+22827700001",
        center_email="contact@centre-dapaong.tg",
        admin_email="admin.savanes@dme.local",
    ),
    CenterSeed(
        region_code="KARA",
        center_name="Centre de santé de Kara",
        address="Boulevard de la Kara, Kara",
        longitude=1.1861,
        latitude=9.5511,
        phone="+22826600002",
        center_email="contact@centre-kara.tg",
        admin_email="admin.kara@dme.local",
    ),
    CenterSeed(
        region_code="CENTRALE",
        center_name="Centre de santé de Sokodé",
        address="Rue du Marché, Sokodé",
        longitude=1.1333,
        latitude=8.9833,
        phone="+22825500003",
        center_email="contact@centre-sokode.tg",
        admin_email="admin.centrale@dme.local",
    ),
    CenterSeed(
        region_code="PLATEAUX",
        center_name="Centre de santé d'Atakpamé",
        address="Avenue de la Gare, Atakpamé",
        longitude=1.1264,
        latitude=7.5264,
        phone="+22824400004",
        center_email="contact@centre-atakpame.tg",
        admin_email="admin.plateaux@dme.local",
    ),
    CenterSeed(
        region_code="MARITIME",
        center_name="Centre de santé de Lomé",
        address="12 Rue du Commerce, Lomé",
        longitude=1.2255,
        latitude=6.1319,
        phone="+22822200005",
        center_email="contact@centre-lome.tg",
        admin_email="admin.maritime@dme.local",
    ),
)


def _dev_password() -> str:
    """Mot de passe de démonstration des comptes amorcés (configurable)."""
    return getattr(get_settings(), "seed_dev_password", None) or _DEFAULT_DEV_PASSWORD


async def _user_exists(session: AsyncSession, email: str) -> bool:
    """Indique si un `app_user` porte déjà cet e-mail (ancre d'idempotence).

    `app_user` est sous RLS forcée. Cette requête est appelée dans deux contextes :
    - pour le `platform_admin`, aucun `app.tenant_id` n'est posé : la ligne à
      tenant NUL n'est de toute façon visible d'aucun contexte tenant, et le
      compte propriétaire (non-BYPASSRLS) exécutant l'amorçage voit la ligne qu'il
      vient d'insérer dans la même transaction ;
    - pour un `tenant_admin`, le contexte du tenant correspondant est déjà posé.

    Comme l'e-mail est globalement UNIQUE, l'absence de ligne visible suffit à
    décider de créer le compte sans risque de doublon.
    """
    result = await session.execute(select(AppUser.id).where(AppUser.email == email))
    return result.scalar_one_or_none() is not None


async def _region_id_for_code(session: AsyncSession, code: str) -> uuid.UUID | None:
    """Résout l'`id` d'une région depuis son code, ou None si non amorcée."""
    result = await session.execute(select(Region.id).where(Region.code == code))
    return result.scalar_one_or_none()


async def seed_platform_admin(session: AsyncSession) -> bool:
    """Amorce le `platform_admin` de démonstration (idempotent sur l'e-mail).

    Crée un compte transversal (`tenant_id` NUL, rôle `platform_admin`) avec un
    `password_hash` Argon2id dérivé du mot de passe de démonstration. Renvoie
    `True` si le compte a été créé, `False` s'il existait déjà. N'ouvre ni ne
    valide de transaction (l'appelant gère le cadre transactionnel).
    """
    if await _user_exists(session, _PLATFORM_ADMIN_EMAIL):
        logger.info("platform_admin déjà présent (%s) : ignoré.", _PLATFORM_ADMIN_EMAIL)
        return False

    session.add(
        AppUser(
            id=uuid.uuid4(),
            tenant_id=None,
            email=_PLATFORM_ADMIN_EMAIL,
            password_hash=hash_password(_dev_password()),
            role=_PLATFORM_ADMIN_ROLE,
            is_active=True,
        )
    )
    await session.flush()
    logger.info("platform_admin amorcé (%s).", _PLATFORM_ADMIN_EMAIL)
    return True


async def seed_sample_center(session: AsyncSession, spec: CenterSeed) -> bool:
    """Amorce un centre d'exemple (tenant + centre + tenant_admin) pour une région.

    Idempotent sur l'e-mail du `tenant_admin` : si le compte existe déjà, la région
    est considérée comme déjà amorcée et rien n'est créé (`False`). Sinon, crée en
    une passe, dans la transaction fournie :

    1. le `tenant` (actif, lié à la région) — table non cadrée RLS ;
    2. le profil `medical_center` (coordonnées togolaises valides) — cadrée RLS ;
    3. le `tenant_admin` (mot de passe de démonstration haché) — cadrée RLS.

    Avant d'écrire les tables cadrées RLS, pose `app.tenant_id` sur le nouveau
    tenant (le `WITH CHECK` de la politique garantit l'absence d'injection
    inter-tenant, P2). Renvoie `True` si le centre a été créé, `False` s'il
    existait déjà. Les régions non amorcées sont ignorées (log d'avertissement).
    """
    region_id = await _region_id_for_code(session, spec.region_code)
    if region_id is None:
        logger.warning(
            "Région %s absente : centre d'exemple ignoré (amorcer les régions "
            "d'abord).",
            spec.region_code,
        )
        return False

    if await _user_exists(session, spec.admin_email):
        logger.info(
            "Centre d'exemple de %s déjà présent (%s) : ignoré.",
            spec.region_code,
            spec.admin_email,
        )
        return False

    # 1. Tenant (actif) — table non cadrée RLS, insertion directe.
    tenant = Tenant(
        id=uuid.uuid4(),
        name=spec.center_name,
        region_id=region_id,
        status="active",
    )
    session.add(tenant)
    await session.flush()

    # À partir d'ici, écriture dans des tables sous RLS forcée : poser le contexte
    # du tenant cible pour satisfaire le WITH CHECK (chemin cadré, P2).
    await set_tenant_context(session, tenant.id)

    # 2. Profil public du centre (coordonnées EWKT, SRID 4326).
    session.add(
        MedicalCenter(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=spec.center_name,
            region_id=region_id,
            address=spec.address,
            location=f"SRID=4326;POINT({spec.longitude} {spec.latitude})",
            phone=spec.phone,
            email=spec.center_email,
        )
    )

    # 3. tenant_admin : mot de passe de démonstration haché (dev-only).
    session.add(
        AppUser(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email=spec.admin_email,
            password_hash=hash_password(_dev_password()),
            role=_TENANT_ADMIN_ROLE,
            is_active=True,
        )
    )
    await session.flush()

    logger.info(
        "Centre d'exemple amorcé pour %s : %s (admin %s).",
        spec.region_code,
        spec.center_name,
        spec.admin_email,
    )
    return True


async def seed_centers(session: AsyncSession) -> dict[str, int]:
    """Amorce le platform_admin et les centres d'exemple dans la session fournie.

    N'ouvre ni ne valide de transaction : l'appelant gère le cadre transactionnel
    (voir `run_center_seed`). Renvoie un décompte des entités effectivement créées
    `{"platform_admins": int, "centers": int}` (0 pour celles déjà présentes —
    idempotence).
    """
    created_admin = await seed_platform_admin(session)

    created_centers = 0
    for spec in SAMPLE_CENTERS:
        # Chaque centre pose son propre contexte de tenant ; le platform_admin et
        # la détection d'existence du tenant_admin ne dépendent pas d'un contexte
        # résiduel. On ne réinitialise pas explicitement `app.tenant_id` entre
        # itérations : chaque `seed_sample_center` le repose sur son tenant, et
        # `_user_exists`/`_region_id_for_code` interrogent des données (app_user par
        # e-mail globalement unique, region non cadrée) non ambiguës sous ce
        # contexte.
        if await seed_sample_center(session, spec):
            created_centers += 1

    return {
        "platform_admins": int(created_admin),
        "centers": created_centers,
    }


async def run_center_seed() -> dict[str, int]:
    """Point d'entrée autonome : ouvre une session, amorce, valide la transaction.

    Appelé au démarrage (après l'amorçage des régions, voir `entrypoint.sh` et
    `app/seed/__main__.py`) pour que la pile locale dispose d'un jeu de données de
    démonstration au `docker compose up` (exigences 9.1, 9.2). Idempotent.

    **No-op en production.** Les comptes amorcés portent un mot de passe de
    démonstration connu : l'amorçage ne s'exécute donc que hors production
    (`ENVIRONMENT != 'production'`). En production, renvoie un décompte nul sans
    rien écrire.
    """
    from app.core.db import async_session_factory

    settings = get_settings()
    if settings.environment.strip().lower() == "production":
        logger.info(
            "ENVIRONMENT=production : amorçage des centres d'exemple ignoré "
            "(données de démonstration réservées au développement local)."
        )
        return {"platform_admins": 0, "centers": 0}

    async with async_session_factory() as session, session.begin():
        counts = await seed_centers(session)
    logger.info(
        "Amorçage dev : %d platform_admin, %d centre(s) d'exemple créé(s).",
        counts["platform_admins"],
        counts["centers"],
    )
    return counts
