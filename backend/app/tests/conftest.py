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


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Isole l'état (mémoire) du limiteur SlowAPI avant chaque test.

    Le limiteur (`app/core/rate_limit.py`) est global et en mémoire : ses compteurs
    par (IP, endpoint) persisteraient entre tests sous le `TestClient` (même IP
    `testclient`). Sans ce reset, les suites d'auth qui émettent plusieurs requêtes
    `/api/auth/*` (p. ex. `test_auth_router.py` : 9 login, 3 refresh, 4
    set-credentials ; `test_auth_task7.py` : 2 login) finiraient par déclencher le
    429 par IP (défaut « 10/minute »), rendant les tests dépendants de l'ordre : un
    `TestClient` neuf ne remet PAS à zéro ce compteur global. Le reset est purement
    un isolant de test ; il ne modifie aucun comportement de production.

    Cette fixture ne reset qu'au **setup** de chaque test (jamais pendant son corps) ;
    la fixture locale de `test_public_rate_limit_cache.py` reset au setup ET au
    teardown. L'accumulation intra-test dont dépend ce module (60+ requêtes dans la
    même fenêtre) est donc préservée, et l'ordre des deux `autouse` est indifférent
    (deux resets consécutifs au setup laissent le compteur à zéro).
    """
    from app.core.rate_limit import limiter

    limiter.reset()
