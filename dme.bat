@echo off
setlocal enabledelayedexpansion

rem ============================================================================
rem  DME - Centres medicaux Togo : script de cycle de vie de l'application.
rem
rem  Enveloppe les commandes Docker Compose de la pile locale (db + api + web).
rem  Usage :
rem     dme start      Demarre la pile (build si necessaire) en arriere-plan
rem     dme stop       Arrete la pile (conserve les donnees)
rem     dme restart    Arrete puis redemarre la pile
rem     dme status     Affiche l'etat des services
rem     dme logs       Suit les journaux (Ctrl+C pour quitter)
rem     dme build      (Re)construit les images sans demarrer
rem     dme down       Arrete ET supprime les volumes (DONNEES PERDUES)
rem     dme test-db    Provisionne un PostGIS jetable isole (port 5433 par
rem                    defaut, surchargeable via DME_TEST_DB_PORT) et lance la
rem                    suite backend complete, puis supprime le conteneur.
rem  Sans argument, "start" est utilise.
rem ============================================================================

cd /d "%~dp0"

rem --- Choix du binaire compose : "docker compose" (v2) ou "docker-compose" ---
set "COMPOSE=docker compose"
docker compose version >nul 2>&1
if errorlevel 1 (
    docker-compose version >nul 2>&1
    if errorlevel 1 (
        echo [ERREUR] Docker Compose est introuvable. Installez Docker Desktop.
        exit /b 1
    )
    set "COMPOSE=docker-compose"
)

rem --- Verifie que le demon Docker repond ---
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Le demon Docker ne repond pas. Demarrez Docker Desktop.
    exit /b 1
)

rem --- Cree .env depuis .env.example a la premiere utilisation ---
if not exist ".env" (
    if exist ".env.example" (
        echo [INFO] .env absent : copie depuis .env.example.
        copy /y ".env.example" ".env" >nul
    )
)

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

if /i "%ACTION%"=="start"   goto :start
if /i "%ACTION%"=="up"      goto :start
if /i "%ACTION%"=="stop"    goto :stop
if /i "%ACTION%"=="restart" goto :restart
if /i "%ACTION%"=="status"  goto :status
if /i "%ACTION%"=="ps"      goto :status
if /i "%ACTION%"=="logs"    goto :logs
if /i "%ACTION%"=="build"   goto :build
if /i "%ACTION%"=="down"    goto :down
if /i "%ACTION%"=="test-db" goto :testdb

echo [ERREUR] Action inconnue : "%ACTION%".
echo Actions valides : start ^| stop ^| restart ^| status ^| logs ^| build ^| down ^| test-db
exit /b 2

:start
echo [DME] Demarrage de la pile (build si necessaire)...
%COMPOSE% up -d --build
if errorlevel 1 exit /b 1
echo.
echo [DME] Pile demarree.
echo    Frontend  : http://localhost:5173
echo    API       : http://localhost:8000
echo    API docs  : http://localhost:8000/docs
echo    Health    : http://localhost:8000/health
echo    Journaux  : dme logs    ^|    Arret : dme stop
goto :eof

:stop
echo [DME] Arret de la pile (les donnees sont conservees)...
%COMPOSE% stop
goto :eof

:restart
echo [DME] Redemarrage de la pile...
%COMPOSE% stop
%COMPOSE% up -d --build
if errorlevel 1 exit /b 1
echo [DME] Pile redemarree.
goto :eof

:status
%COMPOSE% ps
goto :eof

:logs
echo [DME] Suivi des journaux (Ctrl+C pour quitter)...
%COMPOSE% logs -f
goto :eof

:build
echo [DME] (Re)construction des images...
%COMPOSE% build
goto :eof

:down
echo [ATTENTION] "down" supprime les conteneurs ET les volumes : toutes les
echo             donnees de la base locale seront definitivement perdues.
set /p "CONFIRM=Taper OUI pour confirmer : "
if /i not "!CONFIRM!"=="OUI" (
    echo [DME] Annule.
    goto :eof
)
%COMPOSE% down -v
echo [DME] Pile et volumes supprimes.
goto :eof

:testdb
rem ---------------------------------------------------------------------------
rem  Suite d'integration backend contre un PostgreSQL+PostGIS jetable.
rem
rem  Provisionne un conteneur isole de la pile de dev (nom + port dedies),
rem  exporte TEST_DATABASE_URL (DSN proprietaire async : le compte est le
rem  superutilisateur du conteneur, donc habilite aux migrations, a CREATE ROLE
rem  dme_app et CREATE EXTENSION postgis), lance pytest, puis supprime le
rem  conteneur de maniere INCONDITIONNELLE (meme en cas d'echec) en propageant
rem  le code de sortie de pytest.
rem ---------------------------------------------------------------------------
set "TEST_CONTAINER=dme_test_pg"
if defined DME_TEST_DB_PORT (
    set "TEST_DB_PORT=%DME_TEST_DB_PORT%"
) else (
    set "TEST_DB_PORT=5433"
)
set "TEST_IMAGE=postgis/postgis:16-3.4"

rem Nettoyage prealable : supprime un conteneur homonyme laisse par un run
rem precedent interrompu (idempotence, silencieux).
docker rm -f "%TEST_CONTAINER%" >nul 2>&1

rem Detecte si le port est deja occupe par un AUTRE conteneur Docker : message
rem clair + code non nul plutot qu'une erreur Docker cryptique.
docker ps --format "{{.Ports}}" | findstr /r /c:":%TEST_DB_PORT%->" >nul 2>&1
if not errorlevel 1 (
    echo [ERREUR] Le port %TEST_DB_PORT% est deja utilise par un conteneur Docker.>&2
    echo          Liberez-le ou ajustez DME_TEST_DB_PORT ^(p. ex. set DME_TEST_DB_PORT=5434 ^&^& dme test-db^).>&2
    exit /b 1
)

echo [DME] Demarrage d'un PostGIS de test jetable ^(%TEST_IMAGE%^) sur le port %TEST_DB_PORT%...
docker run --rm -d --name "%TEST_CONTAINER%" -e POSTGRES_DB=dme -e POSTGRES_USER=dme -e POSTGRES_PASSWORD=dme -p "%TEST_DB_PORT%:5432" "%TEST_IMAGE%" >nul
if errorlevel 1 (
    echo [ERREUR] Impossible de demarrer le conteneur PostGIS de test.>&2
    docker rm -f "%TEST_CONTAINER%" >nul 2>&1
    exit /b 1
)

rem Attend que Postgres accepte les connexions (sonde dans le conteneur).
echo [DME] Attente de la disponibilite de la base...
set "READY=0"
for /l %%i in (1,1,30) do (
    if "!READY!"=="0" (
        docker exec "%TEST_CONTAINER%" pg_isready -U dme -d dme >nul 2>&1
        if not errorlevel 1 (
            set "READY=1"
        ) else (
            ping -n 2 127.0.0.1 >nul
        )
    )
)
if not "!READY!"=="1" (
    echo [ERREUR] La base de test n'est pas devenue disponible dans le delai imparti.>&2
    docker rm -f "%TEST_CONTAINER%" >nul 2>&1
    exit /b 1
)

set "TEST_DATABASE_URL=postgresql+asyncpg://dme:dme@localhost:%TEST_DB_PORT%/dme"
echo [DME] Base prete. Execution de la suite backend ^(pytest^)...
echo    TEST_DATABASE_URL=%TEST_DATABASE_URL%

rem Lance pytest depuis "backend" et CAPTURE son code de sortie AVANT le
rem nettoyage, pour le propager sans qu'un "docker rm" ulterieur ne le masque.
pushd backend
pytest -q
set "PYTEST_STATUS=!errorlevel!"
popd

rem Nettoyage INCONDITIONNEL du conteneur (succes OU echec).
docker rm -f "%TEST_CONTAINER%" >nul 2>&1

if "!PYTEST_STATUS!"=="0" (
    echo [DME] Suite d'integration : SUCCES.
) else (
    echo [DME] Suite d'integration : ECHEC ^(code !PYTEST_STATUS!^).>&2
)
exit /b !PYTEST_STATUS!
