"""Configuration de session des tests (accélération de la suite).

Deux réglages, actifs UNIQUEMENT pendant les tests (aucun impact sur le défaut de
production) :

1. **Argon2id allégé.** Les paramètres de coût OWASP de production (64 MiB, etc.)
   sont volontairement lourds ; multipliés par les nombreux hachages de la suite,
   ils la ralentissent. On surcharge la configuration par des paramètres minimaux
   et on réinitialise le hacheur mémoïsé, de sorte que `hash_password` /
   `verify_password` restent fonctionnellement identiques (même algorithme, même
   format `$argon2id$`, même salage) tout en s'exécutant vite. Le défaut de
   production, lui, n'est jamais modifié.

2. **Sonde de readiness instantanée.** `/health` tente sinon une vraie connexion
   asyncpg vers une base absente en test, qui bloque jusqu'au délai de connexion.
   On surcharge `check_database_ready` pour renvoyer immédiatement sans socket.
   Un test conserve l'assertion du contrat réel (`database` ∈ {ok, unavailable}).
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings


@pytest.fixture(scope="session", autouse=True)
def _fast_argon2() -> None:
    """Allège Argon2id pour toute la session de test (sécurité fonctionnelle intacte)."""
    from app.core import security

    settings = get_settings()
    # Paramètres minimaux : suffisants pour vérifier le comportement (format PHC,
    # salage, succès/échec) sans le coût mémoire/CPU des réglages de production.
    settings.argon2_time_cost = 1
    settings.argon2_memory_cost_kib = 8  # 8 KiB
    settings.argon2_parallelism = 1
    # Repartir d'un hacheur neuf pour que les nouveaux paramètres s'appliquent.
    security.reset_password_hasher()


@pytest.fixture(autouse=True)
def _fast_health_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rend la sonde de readiness instantanée (pas de vraie connexion réseau).

    En test, aucune base n'écoute : sans cette surcharge, chaque appel à `/health`
    attend le délai de connexion asyncpg. On force la sonde à répondre
    « indisponible » tout de suite, ce qui préserve le contrat (`database` reste
    une valeur valide) sans bloquer.
    """

    async def _instant_probe() -> bool:
        return False

    # Surcharge aux deux points de référence : la source (db) et l'alias importé
    # dans main (résolu au moment de l'appel de l'endpoint).
    monkeypatch.setattr("app.core.db.check_database_ready", _instant_probe)
    monkeypatch.setattr("app.main.check_database_ready", _instant_probe)
