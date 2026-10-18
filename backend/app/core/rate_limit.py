"""Limitation de débit applicative — repli sans reverse proxy (G-4, exigence 4.1).

La conception (design.md, « G-4 — Emplacement de la limitation de débit ») place la
limitation de débit **au reverse proxy** (Nginx `limit_req_zone`/`limit_req` sur
`/api/public/*`, 60 req/min/IP, 429). L'artefact de proxy prêt à déployer vit sous
`deploy/reverse-proxy/`. Ce module est le **repli documenté** : un middleware
applicatif (SlowAPI) qui rend le 429 reproductible et testable dans la pile locale
*sans* proxy en façade (NFR-2).

Portée volontairement étroite (design, Property 3 « surface publique bornée ») :

- Seules les routes `/api/public/*` sont limitées. Les routes authentifiées
  (`auth`, `sync`, `center`, `patients`, `visits`, `admin`) et `/health` ne sont
  **jamais** limitées par ce composant : le limiteur est posé par décorateur sur les
  seules routes publiques, pas globalement.
- Clé par **adresse IP du client** (`get_remote_address`) : un dépassement est borné
  à une source, conforme à l'exigence 4.1 (« une source dépassant le seuil »).
- Seuil **configurable** via `Settings.public_rate_limit` (défaut « 60/minute »), sans
  jamais coder le seuil en dur.
- Réponse **429** au dépassement, au corps minimal (handler SlowAPI par défaut) : pas
  de fuite d'information dans le corps (design, Error Handling G-4).

Limite assumée (design) : le stockage SlowAPI par défaut est **en mémoire** (par
instance). En multi-instance, un compteur global exigerait un backend partagé (Redis) ;
ce n'est à introduire que si nécessaire. Le repli applicatif seul suffit à une
instance unique et à la vérification (429 reproductible).
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings

_settings = get_settings()

# Limiteur partagé, sans limite par défaut : aucune route n'est limitée tant qu'elle
# n'est pas explicitement décorée par `public_rate_limit` (voir `app/routers/public.py`).
# La clé est l'adresse IP du client ; le seuil provient de la configuration.
limiter = Limiter(key_func=get_remote_address)


def public_rate_limit() -> str:
    """Renvoie la chaîne de limite appliquée aux routes publiques (configurable).

    Lue depuis `Settings.public_rate_limit` (défaut « 60/minute ») à chaque appel du
    décorateur, de sorte qu'un ajustement de configuration suffit à changer le seuil
    sans modifier le code des routes.
    """
    return _settings.public_rate_limit
