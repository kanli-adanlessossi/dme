"""Tests unitaires des spécifications de centres d'exemple (tâche 13) — REQ-9.2.

Ces tests ne touchent PAS la base : ils vérifient les **données statiques**
`SAMPLE_CENTERS` et quelques invariants du module d'amorçage dev-local. L'écriture
réelle (RLS, idempotence sur l'e-mail, no-op en production) est couverte par le
test d'intégration `test_seed_centers_db.py` contre un vrai PostGIS.

Invariants vérifiés :

- il existe **exactement un centre d'exemple par région canonique** (5 au total),
  sans doublon de région ;
- chaque centre porte des **coordonnées togolaises valides** (dans la boîte
  englobante du Togo, exigence 9.2) ;
- les e-mails (ancres d'idempotence) sont **tous distincts** entre eux et du
  platform_admin ;
- les coordonnées plausibles tombent dans la bonne enveloppe nord→sud (le centre
  des SAVANES est au nord de celui de MARITIME), garde-fou anti-interversion.
"""

from __future__ import annotations

from app.seed.centers import _PLATFORM_ADMIN_EMAIL, SAMPLE_CENTERS
from app.seed.regions import CANONICAL_REGION_CODES

# Boîte englobante du Togo (design « Validation de la frontière du Togo »,
# exigence 9.2). Longitude ∈ [-0.15, 1.80], latitude ∈ [6.10, 11.14].
_TOGO_LON_MIN, _TOGO_LON_MAX = -0.15, 1.80
_TOGO_LAT_MIN, _TOGO_LAT_MAX = 6.10, 11.14


def test_un_centre_par_region_canonique() -> None:
    """Exactement un centre d'exemple par région canonique (5 régions)."""
    region_codes = [c.region_code for c in SAMPLE_CENTERS]
    assert len(region_codes) == 5
    assert set(region_codes) == set(CANONICAL_REGION_CODES)
    # Aucune région dupliquée.
    assert len(region_codes) == len(set(region_codes))


def test_coordonnees_dans_la_boite_englobante_du_togo() -> None:
    """Chaque centre d'exemple a des coordonnées dans la frontière du Togo (9.2)."""
    for spec in SAMPLE_CENTERS:
        assert _TOGO_LON_MIN <= spec.longitude <= _TOGO_LON_MAX, (
            f"{spec.region_code}: longitude {spec.longitude} hors Togo"
        )
        assert _TOGO_LAT_MIN <= spec.latitude <= _TOGO_LAT_MAX, (
            f"{spec.region_code}: latitude {spec.latitude} hors Togo"
        )


def test_emails_tous_distincts() -> None:
    """Les e-mails (ancres d'idempotence) sont uniques entre eux et vs admin."""
    admin_emails = [c.admin_email for c in SAMPLE_CENTERS]
    center_emails = [c.center_email for c in SAMPLE_CENTERS]
    all_emails = [*admin_emails, *center_emails, _PLATFORM_ADMIN_EMAIL]
    assert len(all_emails) == len(set(all_emails)), "Les e-mails doivent être uniques"


def test_enveloppe_nord_sud_coherente() -> None:
    """Garde-fou anti-interversion : SAVANES (nord) plus au nord que MARITIME (sud)."""
    by_code = {c.region_code: c for c in SAMPLE_CENTERS}
    assert by_code["SAVANES"].latitude > by_code["MARITIME"].latitude
    # Ordre latitudinal décroissant nord→sud sur les chefs-lieux retenus.
    ordered = ["SAVANES", "KARA", "CENTRALE", "PLATEAUX", "MARITIME"]
    lats = [by_code[code].latitude for code in ordered]
    assert lats == sorted(lats, reverse=True), (
        "Les latitudes doivent décroître du nord (SAVANES) au sud (MARITIME)"
    )


def test_telephones_au_format_togolais() -> None:
    """Les numéros de contact portent l'indicatif togolais +228."""
    for spec in SAMPLE_CENTERS:
        assert spec.phone.startswith("+228"), (
            f"{spec.region_code}: téléphone {spec.phone!r} doit être au format +228"
        )
