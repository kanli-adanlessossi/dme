"""Utilitaire de stub pour les routeurs contract-first (tâche 3).

La tâche 3 fige le **contrat** (schémas, chemins, codes, erreurs) pour que
`/openapi.json` soit stable avant le travail frontend. Les handlers n'ont pas
encore de logique : ils lèvent 501 Not Implemented de façon cohérente. Les tâches
ultérieures (4 à 12) remplaceront ces corps par l'implémentation réelle sans
changer les signatures ni les schémas, préservant ainsi le contrat.
"""

from __future__ import annotations

from fastapi import HTTPException, status


def not_implemented(feature: str) -> None:
    """Lève 501 pour un endpoint dont seul le contrat est figé (tâche 3)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Contrat figé (tâche 3) ; implémentation à venir : {feature}.",
    )
