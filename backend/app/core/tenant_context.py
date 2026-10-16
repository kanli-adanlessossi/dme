"""Contexte de tenant par requête (tâche 4.3) — fondation de la RLS.

Cette dépendance FastAPI établit, pour chaque requête cadrée au tenant, le
paramètre de session PostgreSQL `app.tenant_id` **au sein d'une transaction**,
afin que les politiques RLS `tenant_isolation` (migration 0003, tâche 4.2)
filtrent automatiquement toutes les lignes — même si le code applicatif oublie
une clause ``WHERE tenant_id = …`` (propriété P1 du design).

Points de conception (section « Définition du contexte de tenant ») :

- **Transaction + `SET LOCAL`** : le contexte est posé via la fonction
  transaction-locale ``set_config('app.tenant_id', :tid, true)`` (équivalent de
  ``SET LOCAL app.tenant_id = …``). Le troisième argument ``true`` = *is_local*
  limite le réglage à la transaction en cours. Les connexions mutualisées du
  pool ne divulguent donc JAMAIS de contexte d'une requête à l'autre : à la fin
  de la transaction, le paramètre disparaît.

- **Pourquoi `set_config(..., true)` plutôt que `SET LOCAL …`** : ``SET LOCAL``
  n'accepte pas de paramètre lié dans sa syntaxe (le nom/valeur doit être
  littéral), ce qui obligerait à interpoler le UUID dans la chaîne SQL — risque
  d'injection. ``set_config`` est une **fonction** : sa valeur se lie proprement
  comme un paramètre (``:tid``). On neutralise ainsi tout risque d'injection
  tout en obtenant exactement la sémantique transaction-locale voulue.

- **Rôle non-superuser** : la connexion runtime utilise le rôle applicatif
  `dme_app` (NON-superuser, NOBYPASSRLS) lorsqu'il est configuré via `APP_DB_*`
  (voir `app.core.config`). C'est la condition pour que la RLS forcée soit
  TOUJOURS appliquée.

- **Fail-closed** : si le `tenant_id` ne peut être dérivé du contexte
  authentifié pour une opération cadrée au tenant, on lève une erreur interne
  (500) plutôt que d'exécuter la requête sans contexte. Sans contexte, la RLS
  renverrait zéro ligne (non une fuite), mais exécuter « hors contexte » est un
  bug côté serveur : on le traite comme tel (design : « la dépendance traite un
  contexte manquant comme une erreur interne »).

Note sur l'authentification : le flux JWT complet relève de la **tâche 7**. Ce
module n'implémente donc pas l'auth ; il expose un **joint** propre,
`get_current_tenant_id`, destiné à être remplacé (ou surchargé via
`app.dependency_overrides`) par la dépendance d'auth de la tâche 7, qui dérivera
le `tenant_id` depuis la revendication du jeton (NFR-1, REQ-1.2/1.3). Tant que
cette dépendance n'est pas branchée, elle échoue en fail-closed (501/500) : rien
ne s'exécute « hors tenant » par accident.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory

# Nom du paramètre de session PostgreSQL lu par les politiques RLS
# (`current_setting('app.tenant_id', true)`). Centralisé ici pour rester aligné
# avec la migration 0003.
TENANT_SETTING = "app.tenant_id"

# Nom du drapeau transaction-local activant la politique de **lecture publique**
# `public_read` (migration 0009). Posé uniquement par `public_session` ci-dessous
# — jamais par une requête cadrée au tenant. Aligné avec la migration 0009.
PUBLIC_READ_SETTING = "app.public_read"


async def get_current_tenant_id() -> uuid.UUID:
    """Joint d'authentification : renvoie le `tenant_id` du contexte courant.

    **Placeholder volontaire (tâche 4.3).** L'authentification JWT (tâche 7)
    remplacera cette dépendance — ou la surchargera via
    `app.dependency_overrides[get_current_tenant_id]` — pour extraire le
    `tenant_id` de la revendication du jeton vérifié (le jeton porte `tenant_id`
    et `role`, cf. design). La signature (coroutine renvoyant un `uuid.UUID`)
    est choisie pour être compatible en l'état avec ce futur branchement.

    Tant que l'auth n'est pas câblée, on échoue en **fail-closed** : lever 501
    plutôt que d'inventer un tenant garantit qu'aucune requête ne s'exécute sans
    contexte authentifié dérivé côté serveur (P2).
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Contexte de tenant indisponible : l'authentification (tâche 7) "
            "n'est pas encore câblée. Surcharger `get_current_tenant_id`."
        ),
    )


async def set_tenant_context(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Pose `app.tenant_id` pour la transaction courante de `session`.

    Exécute ``select set_config('app.tenant_id', :tid, true)`` : la valeur du
    UUID est **liée comme paramètre** (aucune interpolation de chaîne, donc
    aucune injection possible) et le réglage est **transaction-local** (3ᵉ
    argument ``true``). `set_config` attend une valeur texte ; on convertit donc
    le `UUID` en `str`.

    Prérequis : une transaction doit être ouverte sur la session (sans quoi le
    réglage « local » n'aurait aucune portée utile). `tenant_session` ci-dessous
    garantit cette condition.
    """
    await session.execute(
        text(f"SELECT set_config('{TENANT_SETTING}', :tid, true)"),
        {"tid": str(tenant_id)},
    )


async def tenant_session(
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
) -> AsyncIterator[AsyncSession]:
    """Dépendance FastAPI : session cadrée au tenant, contexte RLS établi.

    Pour chaque requête :
    1. ouvre une `AsyncSession` et une **transaction** (``session.begin()``) ;
    2. pose `app.tenant_id` via `set_tenant_context` (transaction-local) ;
    3. cède la session aux handlers/services ; toutes leurs requêtes sont donc
       automatiquement filtrées par la RLS au tenant courant ;
    4. valide la transaction en sortie nominale, ou l'annule sur exception.

    Fail-closed : si `get_current_tenant_id` ne peut dériver de tenant, la
    dépendance échoue avant toute requête — rien ne s'exécute hors contexte.
    """
    async with async_session_factory() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        yield session


async def public_session() -> AsyncIterator[AsyncSession]:
    """Dépendance FastAPI : session **de lecture publique** (projection sûre).

    Chemin dédié de l'annuaire/carte publics (routeur `public`, tâche 12) : SANS
    authentification ni contexte de tenant. Il n'emprunte donc PAS
    `tenant_session` (qui exige un `tenant_id` dérivé du jeton). À la place, il
    pose le drapeau transaction-local ``app.public_read = 'on'`` qui active la
    politique RLS PERMISSIVE `public_read` (migration 0009) : celle-ci n'ouvre que
    le **SELECT** des centres **publics** appartenant à des tenants **actifs**
    (exclut les tenants désactivés, P8), sans jamais exposer de PHI (P4 : aucune
    ouverture sur `patient`/`visit`).

    Déroulé par requête :
    1. ouvre une `AsyncSession` et une **transaction** (``session.begin()``) ;
    2. pose ``app.public_read = 'on'`` (transaction-local, lié comme paramètre) ;
    3. **ne pose PAS** `app.tenant_id` : sans contexte de tenant, la politique
       `tenant_isolation` ne voit aucune ligne — seule `public_read` ouvre la
       projection sûre, et uniquement en lecture ;
    4. cède la session en lecture seule aux handlers.

    La transaction est validée en sortie nominale (aucune écriture n'est faite sur
    ce chemin ; le commit libère simplement proprement le drapeau transaction-local).
    """
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(f"SELECT set_config('{PUBLIC_READ_SETTING}', 'on', true)")
        )
        yield session
