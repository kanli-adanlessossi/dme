#!/usr/bin/env sh
# Point d'entrée du conteneur api.
#
# Applique les migrations Alembic (dont l'activation de PostGIS) AVANT de lancer
# le serveur ASGI, conformément au design (« l'api exécute les migrations
# Alembic … au démarrage »). Puis amorce les 5 régions du Togo et leur géométrie
# (tâche 6.2) suivies du jeu de démonstration dev-local — platform_admin + un
# centre d'exemple par région (tâche 13). Les deux amorçages sont idempotents ;
# l'amorçage des centres est un no-op en production (données de démonstration).
set -e

echo "[entrypoint] Application des migrations Alembic (alembic upgrade head)…"
alembic upgrade head

echo "[entrypoint] Amorçage des régions + centres d'exemple (tâches 6.2 & 13, idempotent)…"
python -m app.seed

echo "[entrypoint] Démarrage du serveur Uvicorn…"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
