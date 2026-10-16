#!/usr/bin/env bash
# =============================================================================
#  DME - Centres medicaux Togo : script de cycle de vie de l'application.
#
#  Enveloppe les commandes Docker Compose de la pile locale (db + api + web).
#  Usage :
#     ./dme.sh start      Demarre la pile (build si necessaire) en arriere-plan
#     ./dme.sh stop       Arrete la pile (conserve les donnees)
#     ./dme.sh restart    Arrete puis redemarre la pile
#     ./dme.sh status     Affiche l'etat des services
#     ./dme.sh logs       Suit les journaux (Ctrl+C pour quitter)
#     ./dme.sh build      (Re)construit les images sans demarrer
#     ./dme.sh down       Arrete ET supprime les volumes (DONNEES PERDUES)
#  Sans argument, "start" est utilise.
# =============================================================================
set -euo pipefail

# Se place a la racine du depot (dossier du script), quel que soit l'appelant.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Choix du binaire compose : "docker compose" (v2) ou "docker-compose" ---
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif docker-compose version >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "[ERREUR] Docker Compose est introuvable. Installez Docker." >&2
    exit 1
fi

# --- Verifie que le demon Docker repond ---
if ! docker info >/dev/null 2>&1; then
    echo "[ERREUR] Le demon Docker ne repond pas. Demarrez Docker." >&2
    exit 1
fi

# --- Cree .env depuis .env.example a la premiere utilisation ---
if [[ ! -f .env && -f .env.example ]]; then
    echo "[INFO] .env absent : copie depuis .env.example."
    cp .env.example .env
fi

ACTION="${1:-start}"

case "$ACTION" in
    start|up)
        echo "[DME] Demarrage de la pile (build si necessaire)..."
        "${COMPOSE[@]}" up -d --build
        echo
        echo "[DME] Pile demarree."
        echo "   Frontend  : http://localhost:5173"
        echo "   API       : http://localhost:8000"
        echo "   API docs  : http://localhost:8000/docs"
        echo "   Health    : http://localhost:8000/health"
        echo "   Journaux  : ./dme.sh logs    |    Arret : ./dme.sh stop"
        ;;
    stop)
        echo "[DME] Arret de la pile (les donnees sont conservees)..."
        "${COMPOSE[@]}" stop
        ;;
    restart)
        echo "[DME] Redemarrage de la pile..."
        "${COMPOSE[@]}" stop
        "${COMPOSE[@]}" up -d --build
        echo "[DME] Pile redemarree."
        ;;
    status|ps)
        "${COMPOSE[@]}" ps
        ;;
    logs)
        echo "[DME] Suivi des journaux (Ctrl+C pour quitter)..."
        "${COMPOSE[@]}" logs -f
        ;;
    build)
        echo "[DME] (Re)construction des images..."
        "${COMPOSE[@]}" build
        ;;
    down)
        echo "[ATTENTION] \"down\" supprime les conteneurs ET les volumes : toutes"
        echo "            les donnees de la base locale seront perdues."
        read -r -p "Taper OUI pour confirmer : " CONFIRM
        if [[ "$CONFIRM" != "OUI" ]]; then
            echo "[DME] Annule."
            exit 0
        fi
        "${COMPOSE[@]}" down -v
        echo "[DME] Pile et volumes supprimes."
        ;;
    *)
        echo "[ERREUR] Action inconnue : \"$ACTION\"." >&2
        echo "Actions valides : start | stop | restart | status | logs | build | down" >&2
        exit 2
        ;;
esac