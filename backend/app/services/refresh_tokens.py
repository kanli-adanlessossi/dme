"""Service de rotation + denylist du refresh (tâche 9.2, G-9).

Logique serveur de la famille `jti` : enregistrement d'un refresh émis, et
consommation **atomique et résistante au rejeu** d'un refresh à l'échange. Utilisé
sur le **seul chemin en ligne** (`/api/auth/refresh` et émission initiale au
`login`) ; les postes hors ligne n'y accèdent jamais.

Conception (ADR G-9, design « Data Models ») :

- **Émission.** `record_issued_refresh` insère une ligne (`consumed_at` NUL) pour
  chaque refresh émis (initial au login, ou rotatif au refresh).
- **Consommation atomique.** `consume_refresh` tente un UPDATE conditionnel
  (`WHERE jti = … AND consumed_at IS NULL AND expires_at > now()`). Comme le filtre
  et la mutation sont dans le même énoncé, deux requêtes concurrentes présentant le
  même `jti` ne peuvent pas réussir toutes les deux : une seule touche la ligne, le
  rejeu voit zéro ligne affectée (pas de fenêtre de course).
- **Détection de rejeu / vol → invalidation de famille.** Si l'UPDATE conditionnel
  ne touche aucune ligne mais que le `jti` **existe** et était **déjà consommé**,
  c'est un rejeu : on invalide TOUTE la famille (`consumed_at = now()` sur chaque
  `jti` encore vivant de ce `family_id`) avant de refuser. Ainsi un attaquant qui
  rejoue un ancien refresh fait aussi tomber le refresh légitime qui l'a remplacé
  (« un vol invalide la famille »).

La couche HTTP (routeur auth) traduit tout refus en **401 générique** (P3) : le
service signale simplement l'échec via `RefreshReplayError` / renvoi vide.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class RefreshDenied(Exception):
    """Un refresh présenté est refusé (inconnu, expiré, déjà consommé, rejeu).

    Volontairement unique et sans cause distinctive : la couche HTTP la traduit en
    **401 générique** sans révéler pourquoi le refresh a été refusé (P3).
    """


@dataclass(frozen=True)
class ConsumedRefresh:
    """Résultat d'une consommation réussie : le `jti` consommé et sa famille."""

    jti: uuid.UUID
    family_id: uuid.UUID


async def record_issued_refresh(
    session: AsyncSession,
    *,
    jti: uuid.UUID,
    family_id: uuid.UUID,
    user_id: uuid.UUID,
    expires_at: datetime,
) -> None:
    """Enregistre un refresh émis (consumed_at NUL) pour la denylist/rotation.

    Appelé à l'émission initiale (login) comme à chaque rotation (refresh). Ne
    valide PAS la transaction : le `commit` relève de l'appelant (routeur).
    """
    await session.execute(
        text(
            "INSERT INTO refresh_token (jti, family_id, user_id, expires_at) "
            "VALUES (:jti, :family_id, :user_id, :expires_at)"
        ),
        {
            "jti": jti,
            "family_id": family_id,
            "user_id": user_id,
            "expires_at": expires_at,
        },
    )


async def consume_refresh(
    session: AsyncSession,
    *,
    jti: uuid.UUID,
) -> ConsumedRefresh:
    """Consomme un refresh de façon atomique et résistante au rejeu (rotation).

    Marque le `jti` présenté comme consommé si — et seulement si — il est encore
    vivant (non consommé et non expiré), le tout dans un **unique énoncé** UPDATE …
    RETURNING : aucune fenêtre de course entre la vérification et la mutation.

    - Une ligne touchée → succès : renvoie le `jti` et son `family_id`.
    - Zéro ligne touchée → refus (`RefreshDenied`). Si le `jti` existe mais était
      **déjà consommé**, c'est un **rejeu** : on invalide d'abord TOUTE la famille
      (tous les `jti` vivants de ce `family_id` marqués consommés) pour faire
      tomber le refresh légitime qui aurait pu lui succéder (défense anti-vol).

    Ne valide PAS la transaction : le `commit` relève de l'appelant.

    Lève :
        RefreshDenied: refresh inconnu, expiré, déjà consommé ou rejoué.
    """
    result = await session.execute(
        text(
            "UPDATE refresh_token SET consumed_at = now() "
            "WHERE jti = :jti AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING jti, family_id"
        ),
        {"jti": jti},
    )
    row = result.first()
    if row is not None:
        return ConsumedRefresh(jti=row.jti, family_id=row.family_id)

    # Échec de la consommation : distinguer un rejeu (jti déjà consommé) d'un jti
    # inconnu/expiré. Un rejeu invalide toute la famille avant de refuser.
    family_row = (
        await session.execute(
            text(
                "SELECT family_id FROM refresh_token "
                "WHERE jti = :jti AND consumed_at IS NOT NULL"
            ),
            {"jti": jti},
        )
    ).first()
    if family_row is not None:
        # Rejeu détecté : invalider TOUTE la famille (« un vol invalide la famille »).
        await session.execute(
            text(
                "UPDATE refresh_token SET consumed_at = now() "
                "WHERE family_id = :family_id AND consumed_at IS NULL"
            ),
            {"family_id": family_row.family_id},
        )

    raise RefreshDenied
