"""Hachage de mot de passe Argon2id (tâche 7.1).

Fournit le **chemin unique** de hachage/vérification des secrets applicatifs
(mots de passe `app_user`). La conception sécurité (design, section « Conception
sécurité et audit ») impose Argon2id ; ce module isole ce choix derrière deux
helpers afin que les couches ultérieures (auth tâche 7.1, flux de jeton de
configuration tâche 7.4) n'instancient jamais le hacheur elles-mêmes.

Garanties :

1. **Argon2id, jamais en clair.** `hash_password` produit toujours une empreinte
   encodée PHC (`$argon2id$...`) ; aucun mot de passe n'est stocké ni journalisé
   en clair (NFR-2, exigence 3.2).
2. **Vérification à temps constant, tolérante aux empreintes invalides.**
   `verify_password` renvoie un booléen et NE lève PAS sur une empreinte
   corrompue/d'un autre type : un `password_hash` absent ou illisible échoue
   proprement (renvoie `False`) plutôt que de propager une exception à la couche
   auth (évite d'exposer, par un code d'erreur distinct, qu'une empreinte était
   malformée — cohérent avec la non-fuite d'existence, P3).

Les paramètres de coût suivent les recommandations OWASP pour Argon2id ; ils sont
centralisés ici pour qu'un durcissement futur reste un changement d'un seul point.
Le réglage fin du coût ou l'exposition via la configuration ne relèvent pas de
cette tâche.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions
from argon2.low_level import Type

from app.core.config import get_settings

# Hacheur partagé, explicitement de type Argon2id (par défaut dans argon2-cffi,
# fixé ici pour ne pas dépendre du défaut de la lib). Les paramètres de coût
# viennent de la configuration (défauts alignés sur les recommandations OWASP) :
# un déploiement peut les durcir, et la suite de tests les allège (conftest) pour
# s'exécuter vite, sans jamais toucher au défaut de production. Instancié une fois
# au premier usage (lazy) pour que toute surcharge de configuration posée avant
# le premier hachage — typiquement en test — soit prise en compte.
_password_hasher: PasswordHasher | None = None


def _get_hasher() -> PasswordHasher:
    """Renvoie le hacheur Argon2id partagé, construit à la demande depuis la config."""
    global _password_hasher
    if _password_hasher is None:
        settings = get_settings()
        _password_hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
    return _password_hasher


def reset_password_hasher() -> None:
    """Réinitialise le hacheur mémoïsé (après une surcharge de config, en test)."""
    global _password_hasher
    _password_hasher = None


def hash_password(password: str) -> str:
    """Hache un mot de passe en clair en une empreinte Argon2id (format PHC).

    Chaque appel produit une empreinte différente (sel aléatoire intégré). La
    valeur de retour est prête à être stockée dans `app_user.password_hash`.
    """
    return _get_hasher().hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Vérifie un mot de passe en clair contre une empreinte stockée.

    Renvoie `True` si le mot de passe correspond, `False` sinon — y compris
    lorsque l'empreinte est absente (`None`), vide, ou n'est pas une empreinte
    Argon2 valide. Ne lève jamais : la couche auth décide seule du code d'erreur
    (générique, pour ne pas révéler l'état du compte, P3 / design « Échecs
    d'authentification »).
    """
    if not password_hash:
        # Compte sans mot de passe défini (identifiants non encore configurés) :
        # échec de vérification, sans divulguer la cause.
        return False
    try:
        return _get_hasher().verify(password_hash, password)
    except argon2_exceptions.VerifyMismatchError:
        return False
    except argon2_exceptions.InvalidHashError:
        # Empreinte corrompue / d'un autre algorithme : traité comme un échec de
        # vérification plutôt qu'une erreur propagée.
        return False


# Entropie du schéma « selector + verifier » du jeton de configuration (ADR-2,
# observation #8). Le jeton émis a la forme `"<selector>.<verifier>"` :
#
# - `selector` : identifiant NON secret, aléatoire d'au moins 16 octets
#   (`token_urlsafe(16)`), stocké EN CLAIR et indexé (`app_user.setup_token_selector`,
#   index UNIQUE partiel) → localise la ligne par un `SELECT` à coût constant. 16
#   octets d'entropie rendent une collision sur l'index UNIQUE négligeable.
# - `verifier` : secret aléatoire d'au moins 32 octets (`token_urlsafe(32)`), dont
#   seule l'empreinte Argon2id est conservée (`setup_token_hash`). Vérifié UNE seule
#   fois, sur la ligne trouvée par le selector (lookup à coût constant, P4/NFR-2).
#
# `token_urlsafe` produit l'alphabet base64url (`A–Z a–z 0–9 - _`), qui ne contient
# JAMAIS de `.` : le `.` est donc un séparateur non ambigu entre selector et verifier.
_SETUP_TOKEN_SELECTOR_BYTES = 16
_SETUP_TOKEN_VERIFIER_BYTES = 32

# Séparateur entre selector et verifier dans le jeton émis (jamais présent dans
# l'alphabet base64url de `token_urlsafe`, donc non ambigu — ADR-2).
SETUP_TOKEN_SEPARATOR = "."


@dataclass(frozen=True)
class GeneratedSetupToken:
    """Jeton de configuration fraîchement généré (schéma selector + verifier, ADR-2).

    Expose, en une seule émission :

    - `token` : le secret EN CLAIR `"<selector>.<verifier>"`, remis UNE seule fois à
      l'administrateur pour qu'il définisse son mot de passe (exigence 3.2) ; c'est la
      valeur du champ API opaque `setup_token: str` (contrat inchangé) ;
    - `selector` : l'identifiant NON secret, à persister EN CLAIR dans
      `app_user.setup_token_selector` (indexé) pour le lookup à coût constant ;
    - `verifier_hash` : l'empreinte Argon2id du verifier, à persister dans
      `app_user.setup_token_hash`. Le verifier en clair n'est JAMAIS stocké (NFR-2).
    """

    token: str
    selector: str
    verifier_hash: str


def generate_setup_token() -> GeneratedSetupToken:
    """Génère un jeton de configuration (schéma selector + verifier, ADR-2).

    Secret à usage unique émis à la création d'un compte (tâche 8), remis UNE seule
    fois à l'administrateur (exigence 3.2) : il remplace tout envoi de mot de passe en
    clair. Produit `"<selector>.<verifier>"` et n'en conserve, pour la base, que le
    selector EN CLAIR (indexé) et l'empreinte Argon2id du verifier — jamais le verifier
    en clair (NFR-2). Le découpage à la consommation se fait sur le PREMIER `.`.
    """
    selector = secrets.token_urlsafe(_SETUP_TOKEN_SELECTOR_BYTES)
    verifier = secrets.token_urlsafe(_SETUP_TOKEN_VERIFIER_BYTES)
    token = f"{selector}{SETUP_TOKEN_SEPARATOR}{verifier}"
    return GeneratedSetupToken(
        token=token,
        selector=selector,
        verifier_hash=hash_setup_token(verifier),
    )


def hash_setup_token(verifier: str) -> str:
    """Hache le **verifier** d'un jeton de configuration en une empreinte Argon2id.

    Depuis ADR-2 (schéma selector + verifier), seul le verifier — la part SECRÈTE du
    jeton — est haché : son empreinte est stockée dans `app_user.setup_token_hash`,
    jamais le verifier en clair (NFR-2, exigence 3.2). Le selector, non secret, est
    stocké en clair à part. Même chemin de hachage que les mots de passe. La valeur de
    retour est prête à être persistée au moment de l'émission du jeton.
    """
    return _get_hasher().hash(verifier)


def verify_setup_token(verifier: str, setup_token_hash: str | None) -> bool:
    """Vérifie le **verifier** d'un jeton de configuration contre son empreinte stockée.

    Renvoie `True` si le verifier correspond, `False` sinon — y compris lorsque
    l'empreinte est absente (`None`, jeton déjà consommé ou jamais émis), vide, ou
    corrompue. Ne lève jamais : la couche HTTP décide seule du code d'erreur (400/410
    générique, sans révéler l'état du compte — P3).
    """
    return verify_password(verifier, setup_token_hash)
