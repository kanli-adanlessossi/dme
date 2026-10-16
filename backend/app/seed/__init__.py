"""Données d'amorçage pour le développement local.

- Tâche 6.2 : `regions` amorce les 5 régions canoniques du Togo et leur
  géométrie dans la table de référence `region` (idempotent, exécuté au
  démarrage après les migrations). Voir `entrypoint.sh` et `app/seed/__main__.py`.
- Tâche 13 : `centers` amorce, par-dessus les régions, un `platform_admin` et un
  centre médical d'exemple par région (tenant + centre + tenant_admin) avec des
  coordonnées togolaises valides (idempotent, dev-only). Voir `app/seed/centers.py`.
"""

from __future__ import annotations

from app.seed.centers import (
    run_center_seed,
    seed_centers,
    seed_platform_admin,
    seed_sample_center,
)
from app.seed.regions import load_region_seeds, run_region_seed, seed_regions

__all__ = [
    "load_region_seeds",
    "run_center_seed",
    "run_region_seed",
    "seed_centers",
    "seed_platform_admin",
    "seed_regions",
    "seed_sample_center",
]
