"""Dépendances d'authentification et de cadrage par rôle (tâche 7.5).

Ce module est le **joint d'authentification** attendu par la fondation RLS
(tâche 4.3, `app.core.tenant_context`) : il dérive le principal authentifié
(`sub`, `tenant_id`, `role`) depuis le JWT d'accès **vérifié**, jamais depuis le
corps de requête (P2, exigence 1.1), puis l'expose aux couches HTTP sous trois
formes :

- `get_current_principal` — extrait et vérifie le Bearer access token
  (`app.core.tokens.decode_token`, `expected_type=access`) et renvoie
  l'`AuthPrincipal`. Un jeton absent, mal formé, expiré, de mauvais type ou de
  signature invalide donne **401** (générique, P3).
- `get_current_tenant_id` — dérive le `tenant_id` du principal, destiné à
  **remplacer/surcharger** le placeholder de `tenant_context` pour piloter le
  `SET LOCAL app.tenant_id` de la RLS (tâche 4.3). Le `platform_admin` a un
  `tenant_id` NUL : il n'emprunte PAS le chemin cadré au tenant. Appeler cette
  dépendance pour un `platform_admin` est un bug côté serveur (opération
  transversale routée à tort vers le chemin RLS) → **500** fail-closed, cohérent
  avec le contrat « contexte de tenant manquant = erreur interne » du design.
- `require_roles(*roles)` — fabrique de dépendance renvoyant **403** quand le rôle
  authentifié n'est pas autorisé (exigence 2.2). N'est PAS une erreur
  d'authentification (401) : le principal est valide, mais sa permission est
  insuffisante.

**Contrat de non-fuite d'existence (P3).** Un accès à la ressource d'un autre
tenant doit être **indiscernable** d'une ressource inexistante : les deux
renvoient **404** (jamais 403). La RLS (tâche 4) rend déjà les lignes d'un autre
tenant invisibles à une requête cadrée → une recherche par id ne ramène rien →
l'endpoint doit lever 404. Le helper `tenant_resource_not_found` encode ce choix
pour que les routeurs des tâches 9+ l'appliquent uniformément.

**Révocation tolérante à l'hors-ligne (NFR-7).** `is_access_revoked` détermine si
l'accès d'un principal est révoqué (compte `is_active=False` ou tenant au statut
`inactive`). `sync/pull` l'expose via `SyncPullResponse.revoked=true` afin que le
client purge sa session et ses PHI locales au plus tard à la prochaine
synchronisation réussie (design, « Authentification tolérante à l'hors-ligne »).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.tokens import TOKEN_TYPE_ACCESS, TokenError, decode_token
from app.models.app_user import AppUser
from app.models.tenant import Tenant
from app.schemas.auth import Role

# Rôle transversal dont le `tenant_id` est NUL : il n'emprunte pas le chemin RLS
# cadré au tenant (voir `get_current_tenant_id`).
PLATFORM_ADMIN = Role.platform_admin.value

# Extracteur du schéma `Authorization: Bearer <jwt>`. `auto_error=False` : on gère
# nous-mêmes l'absence d'en-tête pour renvoyer un 401 générique homogène (plutôt
# que le 403 par défaut de Starlette quand le header manque), cohérent avec le
# reste du contrat d'auth (P3).
_bearer_scheme = HTTPBearer(auto_error=False)

# 401 générique partagé : jamais de cause distinctive (jeton absent, mal formé,
# expiré, mauvais type, signature invalide → même réponse), cohérent avec P3 et le
# 401 générique du routeur d'auth.
_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Authentification requise ou jeton invalide.",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass(frozen=True)
class AuthPrincipal:
    """Principal authentifié, dérivé du JWT d'accès vérifié (jamais du corps, P2).

    `tenant_id` est NUL pour un `platform_admin` (opérations transversales) ;
    toute couche cadrée au tenant doit traiter ce cas explicitement.
    """

    sub: uuid.UUID
    tenant_id: uuid.UUID | None
    role: str

    @property
    def is_platform_admin(self) -> bool:
        """Vrai pour le rôle transversal `platform_admin` (tenant NUL)."""
        return self.role == PLATFORM_ADMIN


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthPrincipal:
    """Dépendance FastAPI : renvoie le principal du Bearer access token vérifié.

    Extrait l'en-tête `Authorization: Bearer <jwt>`, vérifie le jeton via
    `decode_token(..., expected_type=access)` (signature, expiration, type), et
    construit l'`AuthPrincipal` à partir des revendications. Toute anomalie —
    en-tête absent, schéma non-Bearer, jeton mal formé, expiré, de mauvais type ou
    de signature invalide — donne un **401 générique** sans révéler la cause (P3).
    """
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED

    try:
        claims = decode_token(credentials.credentials, expected_type=TOKEN_TYPE_ACCESS)
    except TokenError:
        raise _UNAUTHENTICATED from None

    return AuthPrincipal(
        sub=claims.sub,
        tenant_id=claims.tenant_id,
        role=claims.role,
    )


async def get_current_tenant_id(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> uuid.UUID:
    """Dérive le `tenant_id` du principal pour piloter le contexte RLS (tâche 4.3).

    Branche l'authentification sur le **joint** de `app.core.tenant_context` : la
    valeur renvoyée alimente `SET LOCAL app.tenant_id` au sein de la transaction,
    de sorte que les politiques RLS filtrent automatiquement toutes les lignes au
    tenant courant.

    Le `platform_admin` a un `tenant_id` NUL (transversal) et ne doit JAMAIS
    emprunter le chemin cadré au tenant : s'il atteint cette dépendance, c'est
    qu'une opération transversale a été routée par erreur vers le chemin RLS →
    **500** fail-closed, plutôt que d'exécuter une requête sans contexte (design,
    « Échecs de contexte de tenant »). Les routes transversales utilisent
    directement `get_current_principal` + `require_roles(platform_admin)`.
    """
    if principal.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Contexte de tenant indisponible pour un principal transversal "
                "(platform_admin) : opération non cadrée au tenant."
            ),
        )
    return principal.tenant_id


def require_roles(*roles: Role | str):
    """Fabrique de dépendance de cadrage par rôle : 403 si le rôle n'est pas permis.

    Renvoie une dépendance FastAPI qui laisse passer le principal lorsque son rôle
    figure dans `roles`, et lève **403** sinon (exigence 2.2). Ce n'est PAS une
    erreur d'authentification : le jeton est valide (401 déjà écarté par
    `get_current_principal`), mais la permission est insuffisante — à distinguer du
    404 inter-tenant (P3), qui concerne l'existence d'une ressource, pas la
    permission.

    Exemple d'usage dans un routeur ::

        @router.post(..., dependencies=[Depends(require_roles(Role.tenant_admin))])
        async def ... : ...

    ou pour récupérer le principal autorisé ::

        async def handler(principal: AuthPrincipal = Depends(require_roles("clinician", "staff"))): ...
    """
    allowed: frozenset[str] = frozenset(
        role.value if isinstance(role, Role) else role for role in roles
    )

    async def _dependency(
        principal: AuthPrincipal = Depends(get_current_principal),
    ) -> AuthPrincipal:
        if principal.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Rôle insuffisant pour cette action.",
            )
        return principal

    return _dependency


def tenant_resource_not_found(detail: str = "Ressource introuvable.") -> HTTPException:
    """Construit le 404 unique couvrant « inconnue » ET « hors tenant » (P3).

    Point central du contrat de non-fuite d'existence : une ressource inexistante
    et une ressource appartenant à un autre tenant doivent être **indiscernables**
    (toutes deux 404, jamais 403). La RLS (tâche 4) rend déjà les lignes d'un autre
    tenant invisibles à une lecture cadrée : une recherche par id ne ramène alors
    rien, exactement comme pour un id inexistant. Les routeurs des tâches 9+
    lèvent ce helper dans ce cas pour appliquer P3 uniformément. ::

        row = (await session.execute(select(Patient).where(Patient.id == pid))).scalar_one_or_none()
        if row is None:
            raise tenant_resource_not_found()
    """
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def is_access_revoked(user: AppUser | None, tenant: Tenant | None) -> bool:
    """Indique si l'accès d'un principal est révoqué (NFR-7).

    L'accès est révoqué dès que l'une des conditions suivantes est vraie :

    - le compte est introuvable (`user is None`) — supprimé/désactivé côté serveur ;
    - le compte est inactif (`user.is_active is False`) ;
    - le compte est cadré au tenant et le tenant est introuvable ou au statut
      `inactive` (désactivation du tenant, NFR-4).

    Un `platform_admin` (tenant NUL) n'est pas soumis à la révocation de tenant :
    seule son éventuelle désactivation de compte le révoque. La **fenêtre** de
    révocation est bornée par la durée de vie du jeton d'accès (hors-ligne) ; cet
    état est propagé au client via `sync/pull` (`revoked=true`) pour qu'il purge sa
    session et ses PHI locales au plus tard à la prochaine synchronisation réussie.
    """
    if user is None or not user.is_active:
        return True
    # Compte cadré au tenant : un tenant absent ou désactivé révoque l'accès.
    return user.tenant_id is not None and (tenant is None or tenant.status != "active")


async def check_access_revoked(
    principal: AuthPrincipal,
    session: AsyncSession,
) -> bool:
    """Charge le compte + le tenant du principal et évalue la révocation (NFR-7).

    Recharge l'état serveur courant (le jeton, émis plus tôt, peut être périmé par
    une désactivation ultérieure) : le compte (`app_user`) et, s'il est cadré au
    tenant, le `tenant`. Délègue la décision à `is_access_revoked`. Utilisé par la
    dépendance `sync_access_revoked` pour peupler `SyncPullResponse.revoked`.

    **Cadrage RLS (tâche 10).** `app_user` est une table cadrée au tenant (RLS
    forcée) : sous le rôle applicatif `dme_app` (NOBYPASSRLS), une lecture **sans**
    contexte ne renverrait aucune ligne, ce qui ferait croire à tort que le compte
    est révoqué. On pose donc `app.tenant_id` sur le tenant du principal (transaction
    -local) avant de lire son propre `app_user` : la RLS autorise alors cette
    lecture (le compte appartient à ce tenant). `tenant` n'est pas cadré RLS et se
    lit directement. Un `platform_admin` (tenant NUL) n'emprunte pas le chemin de
    sync : la pose du contexte est donc conditionnée à un `tenant_id` présent.
    """
    if principal.tenant_id is not None:
        # Contexte RLS transaction-local pour autoriser la lecture d'`app_user`.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(principal.tenant_id)},
        )

    user = await session.get(AppUser, principal.sub)

    tenant: Tenant | None = None
    tenant_id = user.tenant_id if user is not None else principal.tenant_id
    if tenant_id is not None:
        tenant = await session.get(Tenant, tenant_id)

    return is_access_revoked(user, tenant)


async def sync_access_revoked(
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> bool:
    """Dépendance FastAPI exposant l'état de révocation pour `sync/pull` (NFR-7).

    Renvoie `True` si l'accès du principal authentifié est révoqué (compte inactif
    ou tenant désactivé), à placer dans `SyncPullResponse.revoked`. Le moteur de
    sync complet (deltas + curseur) relève de la tâche 10 ; cette dépendance isole
    la seule exposition de l'état de révocation requise par la tâche 7.5.
    """
    return await check_access_revoked(principal, session)


def allowed_role_values(roles: Iterable[Role | str]) -> frozenset[str]:
    """Normalise un ensemble de rôles (enum ou str) en valeurs str (utilitaire)."""
    return frozenset(r.value if isinstance(r, Role) else r for r in roles)
