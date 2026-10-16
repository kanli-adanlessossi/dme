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

echo [ERREUR] Action inconnue : "%ACTION%".
echo Actions valides : start ^| stop ^| restart ^| status ^| logs ^| build ^| down
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
