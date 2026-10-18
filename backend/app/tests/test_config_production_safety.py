"""Tests du garde-fou de configuration de production (G-1 / NFR-1, NFR-2).

Vérifie `Settings.validate_production_safety()` : en production, un démarrage non
sûr échoue (fail-closed) avec un message nommant le réglage fautif ; hors
production, c'est un no-op. Couvre la propriété de correction 1 du design et les
critères d'acceptation 1.1 à 1.5 de l'exigence 1.
"""

from __future__ import annotations

import pytest

from app.core.config import (
    _DEV_JWT_SECRET,
    Settings,
    UnsafeProductionConfigError,
)

# Valeurs de production « saines » réutilisées par plusieurs cas : secret JWT
# surchargé, debug désactivé, rôle applicatif non-privilégié configuré.
_SAFE_PROD = {
    "environment": "production",
    "jwt_secret": "prod-secret-unique-et-aleatoire-abcdef0123456789",
    "debug": False,
    "app_db_user": "dme_app",
    "app_db_password": "app-secret",
}


def test_production_secret_par_defaut_leve() -> None:
    """(1) Production + secret JWT par défaut → lève en nommant `jwt_secret`."""
    settings = Settings(**{**_SAFE_PROD, "jwt_secret": _DEV_JWT_SECRET})
    with pytest.raises(UnsafeProductionConfigError, match="jwt_secret"):
        settings.validate_production_safety()


def test_production_debug_actif_leve() -> None:
    """(2) Production + `debug` actif → lève en nommant `debug`."""
    settings = Settings(**{**_SAFE_PROD, "debug": True})
    with pytest.raises(UnsafeProductionConfigError, match="debug"):
        settings.validate_production_safety()


def test_production_role_app_db_absent_leve() -> None:
    """(3) Production + rôle `APP_DB_*` absent → lève en nommant le rôle."""
    settings = Settings(
        **{**_SAFE_PROD, "app_db_user": None, "app_db_password": None}
    )
    with pytest.raises(UnsafeProductionConfigError, match="app_db_user"):
        settings.validate_production_safety()


def test_production_config_saine_passe() -> None:
    """(4) Production + configuration saine → aucun refus."""
    settings = Settings(**_SAFE_PROD)
    settings.validate_production_safety()  # ne doit pas lever


def test_developpement_est_no_op() -> None:
    """(5) Hors production → no-op, même avec des valeurs de dev non sûres."""
    settings = Settings(
        environment="development",
        jwt_secret=_DEV_JWT_SECRET,
        debug=True,
        app_db_user=None,
        app_db_password=None,
    )
    settings.validate_production_safety()  # ne doit pas lever
