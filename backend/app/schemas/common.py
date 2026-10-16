"""Schémas transversaux : contrat d'erreur et types partagés (tâche 3).

Le contrat d'erreur est figé ici afin que `/openapi.json` documente de façon
stable les réponses d'erreur attendues par le frontend (NFR-13) :

- **404** — ressource inconnue OU hors du tenant de l'appelant (pas de fuite
  d'existence, propriété P3).
- **403** — échec d'autorisation (rôle manquant).
- **422** — échec de validation (détail d'erreur Pydantic, forme FastAPI native).
- **429** — trop de tentatives (verrouillage / limitation de débit, NFR-2).
- **400 / 410** — jeton de configuration expiré ou déjà utilisé.

Les corps 404/403/400/410/429 partagent la forme `{"detail": "..."}` de FastAPI.
`ErrorResponse` fournit un modèle réutilisable pour les déclarations `responses=`
des routeurs. Le 422 conserve la forme native `HTTPValidationError` de FastAPI
(FastAPI l'ajoute automatiquement dès qu'une route a un corps/paramètre validé).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Corps d'erreur standard FastAPI (`{"detail": ...}`).

    Utilisé pour documenter 400, 403, 404, 410 et 429 dans les routeurs.
    """

    detail: str = Field(
        ...,
        description="Message d'erreur lisible. N'expose jamais l'existence d'une "
        "ressource hors tenant (contrat P3).",
        examples=["Ressource introuvable"],
    )


# Déclarations `responses=` réutilisables pour factoriser le contrat d'erreur.
# FastAPI fusionne ces entrées avec le 200/201 déduit du `response_model`.

RESPONSES_403: dict[int | str, dict[str, object]] = {
    403: {
        "model": ErrorResponse,
        "description": "Rôle insuffisant pour cette action (autorisation refusée).",
    }
}

RESPONSES_404: dict[int | str, dict[str, object]] = {
    404: {
        "model": ErrorResponse,
        "description": "Ressource inconnue ou hors du tenant de l'appelant "
        "(indiscernables : pas de fuite d'existence, P3).",
    }
}

RESPONSES_AUTHED: dict[int | str, dict[str, object]] = {
    **RESPONSES_403,
    **RESPONSES_404,
}
