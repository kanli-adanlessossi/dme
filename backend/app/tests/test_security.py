"""Tests unitaires du hachage Argon2id (tâche 7.1) — NFR-2, exigence 3.2.

Vérifient que le chemin de hachage/vérification respecte sa conception :
- l'empreinte produite est bien de type **Argon2id** et jamais le mot de passe en
  clair ;
- deux hachages du même mot de passe diffèrent (sel aléatoire) ;
- la vérification réussit pour le bon mot de passe et échoue pour un mauvais ;
- une empreinte absente/vide/corrompue échoue proprement (renvoie `False`) sans
  lever (cohérent avec la non-fuite d'existence, P3).
"""

from __future__ import annotations

from app.core.security import (
    generate_setup_token,
    hash_password,
    hash_setup_token,
    verify_password,
    verify_setup_token,
)


def test_hash_est_argon2id_et_pas_en_clair() -> None:
    """L'empreinte est au format PHC Argon2id et ne contient pas le clair."""
    password = "un-mot-de-passe-robuste-123"
    digest = hash_password(password)
    assert digest.startswith("$argon2id$")
    assert password not in digest


def test_hash_est_salé_valeurs_differentes() -> None:
    """Deux hachages du même mot de passe diffèrent (sel aléatoire intégré)."""
    password = "même-mot-de-passe"
    assert hash_password(password) != hash_password(password)


def test_verify_reussit_pour_le_bon_mot_de_passe() -> None:
    """La vérification réussit contre l'empreinte de son propre mot de passe."""
    password = "correct horse battery staple"
    assert verify_password(password, hash_password(password)) is True


def test_verify_echoue_pour_un_mauvais_mot_de_passe() -> None:
    """Un mot de passe erroné est rejeté."""
    digest = hash_password("le-vrai-mot-de-passe")
    assert verify_password("mauvais-mot-de-passe", digest) is False


def test_verify_echoue_si_empreinte_absente() -> None:
    """Une empreinte `None` (identifiants non configurés) échoue sans lever."""
    assert verify_password("peu importe", None) is False


def test_verify_echoue_si_empreinte_vide() -> None:
    """Une empreinte vide échoue proprement."""
    assert verify_password("peu importe", "") is False


def test_verify_echoue_si_empreinte_corrompue() -> None:
    """Une empreinte illisible / d'un autre type renvoie `False`, sans lever."""
    assert verify_password("peu importe", "pas-une-empreinte-argon2") is False


# --- Jeton de configuration à usage unique et expirant (tâche 7.4 / REQ-3.2) ----


def test_generate_setup_token_est_aleatoire_et_imprevisible() -> None:
    """Deux jetons générés diffèrent (entropie aléatoire) et sont non vides."""
    token_a = generate_setup_token()
    token_b = generate_setup_token()
    assert token_a and token_b
    assert token_a != token_b


def test_hash_setup_token_est_argon2id_et_pas_en_clair() -> None:
    """L'empreinte du jeton est au format Argon2id et ne contient pas le clair."""
    token = generate_setup_token()
    digest = hash_setup_token(token)
    assert digest.startswith("$argon2id$")
    assert token not in digest


def test_verify_setup_token_reussit_pour_le_bon_jeton() -> None:
    """Un jeton vérifie contre sa propre empreinte."""
    token = generate_setup_token()
    assert verify_setup_token(token, hash_setup_token(token)) is True


def test_verify_setup_token_echoue_pour_un_mauvais_jeton() -> None:
    """Un jeton erroné est rejeté."""
    digest = hash_setup_token(generate_setup_token())
    assert verify_setup_token("mauvais-jeton", digest) is False


def test_verify_setup_token_echoue_si_empreinte_absente() -> None:
    """Une empreinte `None` (jeton déjà consommé / jamais émis) échoue sans lever."""
    assert verify_setup_token("peu importe", None) is False
