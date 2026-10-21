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
#     ./dme.sh test-db    Provisionne un PostGIS jetable isole (port 5433 par
#                         defaut, surchargeable via DME_TEST_DB_PORT) et lance la
#                         suite backend complete, puis supprime le conteneur.
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
    test-db)
        # -------------------------------------------------------------------
        #  Suite d'integration backend contre un PostgreSQL+PostGIS jetable.
        #
        #  Provisionne un conteneur isole de la pile de dev (nom + port dedies),
        #  exporte TEST_DATABASE_URL (DSN proprietaire async : le compte est le
        #  superutilisateur du conteneur, donc habilite aux migrations, a
        #  CREATE ROLE dme_app et CREATE EXTENSION postgis), lance pytest, puis
        #  supprime le conteneur de maniere INCONDITIONNELLE (meme en cas
        #  d'echec) en propageant le code de sortie de pytest.
        # -------------------------------------------------------------------
        TEST_CONTAINER="dme_test_pg"
        TEST_DB_PORT="${DME_TEST_DB_PORT:-5433}"
        TEST_IMAGE="postgis/postgis:16-3.4"

        # Nettoyage prealable : supprime un conteneur homonyme laisse par un run
        # precedent interrompu (idempotence, silencieux).
        docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true

        # Detecte si le port est deja occupe par un AUTRE conteneur/service :
        # message clair + code non nul plutot qu'une erreur Docker cryptique.
        if docker ps --format '{{.Ports}}' | grep -qE "(^|[^0-9])(0\.0\.0\.0|127\.0\.0\.1|\[::\]|::):${TEST_DB_PORT}->"; then
            echo "[ERREUR] Le port ${TEST_DB_PORT} est deja utilise par un conteneur Docker." >&2
            echo "         Liberez-le ou ajustez DME_TEST_DB_PORT (p. ex. DME_TEST_DB_PORT=5434 ./dme.sh test-db)." >&2
            exit 1
        fi

        # Nettoyage INCONDITIONNEL du conteneur a la sortie (succes OU echec),
        # sans masquer le code de sortie de pytest.
        cleanup_test_db() {
            docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
        }
        trap cleanup_test_db EXIT

        echo "[DME] Demarrage d'un PostGIS de test jetable (${TEST_IMAGE}) sur le port ${TEST_DB_PORT}..."
        if ! docker run --rm -d --name "$TEST_CONTAINER" \
            -e POSTGRES_DB=dme \
            -e POSTGRES_USER=dme \
            -e POSTGRES_PASSWORD=dme \
            -p "${TEST_DB_PORT}:5432" \
            "$TEST_IMAGE" >/dev/null; then
            echo "[ERREUR] Impossible de demarrer le conteneur PostGIS de test." >&2
            exit 1
        fi

        # Attend que Postgres accepte les connexions (sonde dans le conteneur).
        echo "[DME] Attente de la disponibilite de la base..."
        READY=0
        for _ in $(seq 1 30); do
            if docker exec "$TEST_CONTAINER" pg_isready -U dme -d dme >/dev/null 2>&1; then
                READY=1
                break
            fi
            sleep 1
        done
        if [[ "$READY" -ne 1 ]]; then
            echo "[ERREUR] La base de test n'est pas devenue disponible dans le delai imparti." >&2
            exit 1
        fi

        export TEST_DATABASE_URL="postgresql+asyncpg://dme:dme@localhost:${TEST_DB_PORT}/dme"
        echo "[DME] Base prete. Execution de la suite backend (pytest)..."
        echo "   TEST_DATABASE_URL=${TEST_DATABASE_URL}"

        # Lance pytest et PROPAGE son code de sortie (le trap EXIT nettoiera le
        # conteneur quel que soit le resultat).
        PYTEST_STATUS=0
        ( cd backend && pytest -q ) || PYTEST_STATUS=$?

        if [[ "$PYTEST_STATUS" -eq 0 ]]; then
            echo "[DME] Suite d'integration : SUCCES."
        else
            echo "[DME] Suite d'integration : ECHEC (code ${PYTEST_STATUS})." >&2
        fi
        exit "$PYTEST_STATUS"
        ;;
    *)
        echo "[ERREUR] Action inconnue : \"$ACTION\"." >&2
        echo "Actions valides : start | stop | restart | status | logs | build | down | test-db" >&2
        exit 2
        ;;
esac