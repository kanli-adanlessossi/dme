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


# Nombre d'octets d'entropie du jeton de configuration à usage unique (REQ-3.2).
# 32 octets (256 bits) rendent le jeton impossible à deviner par force brute ;
# `token_urlsafe` le rend transportable tel quel dans une URL/un corps JSON.
_SETUP_TOKEN_BYTES = 32


def generate_setup_token() -> str:
    """Génère un jeton de configuration en clair, aléatoire et imprévisible.

    Secret à usage unique émis à la création d'un compte (tâche 8), remis UNE
    seule fois à l'administrateur pour qu'il définisse son mot de passe (exigence
    3.2) : il remplace tout envoi de mot de passe en clair. La valeur renvoyée est
    le secret EN CLAIR, destiné à être transmis à l'utilisateur ; seule son
    empreinte (`hash_setup_token`) est stockée en base — jamais le clair (NFR-2).
    """
    return secrets.token_urlsafe(_SETUP_TOKEN_BYTES)


def hash_setup_token(setup_token: str) -> str:
    """Hache un jeton de configuration en clair en une empreinte Argon2id.

    Même chemin de hachage que les mots de passe : le jeton est stocké HACHÉ dans
    `app_user.setup_token_hash`, jamais en clair (NFR-2, exigence 3.2). La valeur
    de retour est prête à être persistée au moment de l'émission du jeton.
    """
    return _get_hasher().hash(setup_token)


def verify_setup_token(setup_token: str, setup_token_hash: str | None) -> bool:
    """Vérifie un jeton de configuration en clair contre son empreinte stockée.

    Renvoie `True` si le jeton correspond, `False` sinon — y compris lorsque
    l'empreinte est absente (`None`, jeton déjà consommé ou jamais émis), vide, ou
    corrompue. Ne lève jamais : la couche HTTP décide seule du code d'erreur
    (400/410 générique, sans révéler l'état du compte — P3).
    """
    return verify_password(setup_token, setup_token_hash)
