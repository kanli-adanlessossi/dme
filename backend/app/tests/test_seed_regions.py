"""Tests unitaires du chargeur de régions (tâche 6.2) — REQ-7.6, 9.2.

Ces tests ne touchent PAS la base : ils exercent la **fonction pure**
`load_region_seeds`, qui lit et valide le GeoJSON avant tout accès PostgreSQL.
L'upsert réel (conversion MultiPolygon/4326, idempotence) est vérifié par le
test d'intégration `test_seed_regions_db.py` contre un vrai PostGIS.

On vérifie ici que :

- le **vrai** fichier livré (`data/togo-regions.geojson`) contient exactement les
  5 régions canoniques du Togo, chacune avec une géométrie Polygon/MultiPolygon
  non vide et un `code`/`name_fr` exploitables (garantit qu'on amorce un
  référentiel complet et correctement attribué) ;
- le chargeur **rejette** les écarts : code non canonique, région manquante,
  doublon de code, feature sans géométrie — plutôt que d'amorcer un référentiel
  incohérent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.seed.regions import CANONICAL_REGION_CODES, load_region_seeds

# Chemin du GeoJSON réellement livré (défaut de la configuration = <repo>/data/...).
_SHIPPED_GEOJSON = get_settings().regions_geojson_path


def _write_geojson(tmp_path: Path, features: list[dict]) -> Path:
    """Écrit une FeatureCollection minimale et renvoie son chemin."""
    path = tmp_path / "regions.geojson"
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )
    return path


def _polygon() -> dict:
    """Un petit Polygon valide (carré), suffisant pour la validation de forme."""
    return {
        "type": "Polygon",
        "coordinates": [[[1.0, 6.5], [1.1, 6.5], [1.1, 6.6], [1.0, 6.6], [1.0, 6.5]]],
    }


def _feature(code: str, name_fr: str | None = None, geometry: dict | None = None) -> dict:
    props: dict[str, object] = {"code": code}
    if name_fr is not None:
        props["name_fr"] = name_fr
    return {
        "type": "Feature",
        "properties": props,
        "geometry": geometry if geometry is not None else _polygon(),
    }


# --- Le fichier réellement livré ------------------------------------------------


def test_fichier_livre_contient_les_5_regions_canoniques() -> None:
    """`data/togo-regions.geojson` porte exactement les 5 régions canoniques."""
    seeds = load_region_seeds(_SHIPPED_GEOJSON)
    assert {s.code for s in seeds} == set(CANONICAL_REGION_CODES)
    assert len(seeds) == 5


def test_fichier_livre_ordre_canonique_nord_sud() -> None:
    """Les régions sont renvoyées dans l'ordre canonique nord→sud."""
    seeds = load_region_seeds(_SHIPPED_GEOJSON)
    assert [s.code for s in seeds] == [
        "SAVANES",
        "KARA",
        "CENTRALE",
        "PLATEAUX",
        "MARITIME",
    ]


def test_fichier_livre_geometries_non_vides() -> None:
    """Chaque région porte une géométrie Polygon/MultiPolygon avec des coordonnées."""
    seeds = load_region_seeds(_SHIPPED_GEOJSON)
    for seed in seeds:
        geometry = json.loads(seed.geometry_geojson)
        assert geometry["type"] in {"Polygon", "MultiPolygon"}
        assert geometry["coordinates"], f"{seed.code} doit avoir des coordonnées"
        assert seed.name_fr


def test_fichier_livre_attribution_presente() -> None:
    """Le GeoJSON livré porte un bloc d'attribution (jeu ouvert, REQ-7.6)."""
    raw = json.loads(_SHIPPED_GEOJSON.read_text(encoding="utf-8"))
    attribution = raw.get("attribution")
    assert attribution, "Le GeoJSON doit porter une attribution de haut niveau"
    assert attribution.get("license"), "La licence doit être renseignée"
    assert attribution.get("source"), "La source doit être renseignée"


# --- Validation : rejets attendus ----------------------------------------------


def test_rejette_code_non_canonique(tmp_path: Path) -> None:
    """Un code hors des 5 régions canoniques est rejeté."""
    features = [_feature(c) for c in CANONICAL_REGION_CODES]
    features.append(_feature("GRAND_LOME"))  # 6ᵉ unité non canonique
    path = _write_geojson(tmp_path, features)
    with pytest.raises(ValueError, match="non canonique"):
        load_region_seeds(path)


# Ordre canonique explicite (CANONICAL_REGION_CODES est un frozenset non ordonné :
# ne pas s'appuyer sur son ordre d'itération pour construire des cas de test).
_CANON_ORDER = ["SAVANES", "KARA", "CENTRALE", "PLATEAUX", "MARITIME"]


def test_rejette_region_manquante(tmp_path: Path) -> None:
    """Un GeoJSON auquel il manque une région canonique est rejeté."""
    codes = _CANON_ORDER[:-1]  # 4 sur 5 : il manque MARITIME
    path = _write_geojson(tmp_path, [_feature(c) for c in codes])
    with pytest.raises(ValueError, match="manquantes"):
        load_region_seeds(path)


def test_rejette_code_duplique(tmp_path: Path) -> None:
    """Un code de région apparaissant deux fois est rejeté."""
    features = [_feature(c) for c in _CANON_ORDER]
    features.append(_feature("MARITIME"))  # doublon
    path = _write_geojson(tmp_path, features)
    with pytest.raises(ValueError, match="dupliqué"):
        load_region_seeds(path)


def test_rejette_feature_sans_geometrie(tmp_path: Path) -> None:
    """Une région sans géométrie Polygon/MultiPolygon est rejetée."""
    # 4 régions valides + MARITIME sans géométrie (aucun doublon).
    features = [_feature(c) for c in _CANON_ORDER[:-1]]
    features.append(
        {"type": "Feature", "properties": {"code": "MARITIME"}, "geometry": None}
    )
    path = _write_geojson(tmp_path, features)
    with pytest.raises(ValueError, match="géométrie"):
        load_region_seeds(path)


def test_rejette_fichier_absent(tmp_path: Path) -> None:
    """Un chemin inexistant lève FileNotFoundError explicite."""
    with pytest.raises(FileNotFoundError):
        load_region_seeds(tmp_path / "absent.geojson")
