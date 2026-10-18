"""Chargement de configuration via Pydantic Settings.

Lit les variables d'environnement injectées par `docker-compose.yml` / `.env`
(`APP_NAME`, `ENVIRONMENT`, `DEBUG`, `POSTGRES_*`) et construit la DSN
SQLAlchemy asynchrone (asyncpg). Les identifiants ne sont jamais codés en dur :
ils proviennent exclusivement de l'environnement.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Secret de signature JWT par défaut, destiné UNIQUEMENT au développement et aux
# tests locaux. Centralisé ici pour que le garde-fou de production
# (`Settings.validate_production_safety`) puisse détecter qu'un déploiement a
# oublié de le surcharger. Un seul point de vérité : le défaut du champ
# `jwt_secret` ci-dessous réutilise cette constante.
_DEV_JWT_SECRET = "dev-insecure-change-me-0000000000000000000000000000"


class UnsafeProductionConfigError(RuntimeError):
    """Configuration non sûre détectée en production (fail-closed, G-1).

    Levée au démarrage (`lifespan`) lorsque `environment == "production"` et
    qu'un réglage sensible conserve une valeur de développement (secret JWT par
    défaut, `debug` actif, rôle applicatif `APP_DB_*` absent). Le message nomme
    le réglage fautif. Hors production, aucune vérification n'est effectuée.
    """


class Settings(BaseSettings):
    """Configuration applicative dérivée de l'environnement.

    Les attributs `app_name` / `environment` sont utilisés par `main.py`. Les
    champs `postgres_*` servent à construire la DSN de connexion à la base.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---
    app_name: str = "DME - Centres médicaux Togo"
    environment: str = "development"
    debug: bool = True

    # --- CORS (frontend local-first) ---
    # Origines autorisées à appeler l'API depuis un navigateur. Le frontend Vite
    # tourne par défaut sur http://localhost:5173 ; sans ces en-têtes CORS, le
    # navigateur bloque les requêtes cross-origin (NFR-2). Jamais `*` : la liste
    # est explicite et surchargeable via `CORS_ALLOW_ORIGINS` (valeurs séparées
    # par des virgules) pour les autres environnements. En production, le
    # frontend est servi derrière le même reverse proxy (même origine) ou cette
    # liste énumère les origines web légitimes.
    cors_allow_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # Méthodes HTTP autorisées lors d'une requête cross-origin. Explicites (jamais
    # `*`) : le pré-vol CORS n'annonce que ce dont le frontend a besoin — GET/POST/
    # PATCH/DELETE pour les routes métier et OPTIONS pour le pré-vol lui-même
    # (NFR-2). Un joker est refusé par conception ; un `*` combiné à
    # `allow_credentials=True` est d'ailleurs invalide au sens de la spéc CORS.
    # Surchargeable via `CORS_ALLOW_METHODS` (valeurs séparées par des virgules).
    cors_allow_methods: list[str] = [
        "GET",
        "POST",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ]

    # En-têtes de requête autorisés en cross-origin. Explicites (jamais `*`) :
    # `Authorization` porte le jeton Bearer des routes protégées et `Content-Type`
    # les corps JSON ; ces deux en-têtes suffisent au frontend (NFR-2). Le joker
    # est refusé par conception. Surchargeable via `CORS_ALLOW_HEADERS` (valeurs
    # séparées par des virgules).
    cors_allow_headers: list[str] = [
        "Authorization",
        "Content-Type",
    ]

    @field_validator(
        "cors_allow_origins",
        "cors_allow_methods",
        "cors_allow_headers",
        mode="before",
    )
    @classmethod
    def _split_csv_list(cls, value: object) -> object:
        """Accepte une chaîne CSV (`a,b`) depuis l'environnement, ou une liste."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    # --- Limitation de débit des endpoints publics (tâche 4.1 / G-4 / NFR-2) ---
    # Seuil appliqué à `/api/public/*`. La conception place la limitation au reverse
    # proxy (Nginx `limit_req`, voir `deploy/reverse-proxy/`) ; ce réglage pilote le
    # REPLI applicatif (SlowAPI, `app.core.rate_limit`) qui rend le 429 reproductible
    # sans proxy en façade. Format « <nombre>/<fenêtre> » (syntaxe `limits`), par
    # défaut « 60/minute » (60 req/min/IP, exigence 4.1). Jamais codé en dur :
    # surchargeable via `PUBLIC_RATE_LIMIT`. Seules les routes publiques sont
    # limitées ; les routes authentifiées et `/health` ne le sont pas.
    public_rate_limit: str = "60/minute"

    # --- Base de données PostgreSQL + PostGIS ---
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "dme"
    postgres_user: str = "dme"
    postgres_password: str = "dme"
    # Délai (secondes) d'établissement d'une connexion asyncpg. Court par défaut :
    # une sonde de readiness (`/health`) ou une requête ne doit pas rester bloquée
    # plusieurs secondes quand la base est injoignable — elle échoue vite et
    # proprement. Configurable via `DB_CONNECT_TIMEOUT_SECONDS`.
    db_connect_timeout_seconds: float = 5.0

    # --- Hachage Argon2id (tâche 7.1 / NFR-2) ---
    # Paramètres de coût OWASP par défaut (production). Centralisés ici pour qu'un
    # durcissement — ou, en test, un allègement — reste un changement d'un seul
    # point, sans jamais affaiblir le défaut de production. Les tests surchargent
    # ces valeurs (conftest) pour accélérer la suite ; le défaut reste robuste.
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 64 * 1024  # 64 MiB
    argon2_parallelism: int = 4

    # --- Authentification JWT (tâche 7.2) ---
    # Secret de signature des JWT (HS256). JAMAIS codé en dur : fourni par
    # l'environnement (`JWT_SECRET`, voir `.env.example`). Le défaut n'est destiné
    # qu'au développement/aux tests locaux ; un déploiement DOIT le surcharger.
    jwt_secret: str = _DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    # Durée de vie de l'access token, raccourcie à 8 h (G-9). Huit heures couvrent
    # encore une journée de travail hors ligne typique (NFR-7) tout en réduisant
    # d'un tiers la fenêtre d'exposition : comme la révocation est stateless, celle
    # d'un acteur hors ligne reste bornée par ce TTL d'access. Configurable via
    # `JWT_ACCESS_TTL_SECONDS` pour l'ajuster à la réalité terrain d'un centre.
    jwt_access_ttl_seconds: int = 8 * 60 * 60  # 8 h (G-9 : access raccourci)
    # Durée de vie du refresh token (rotatif), ramenée à 14 j (G-9) : couvre une
    # longue période hors ligne sans réauthentification serveur (NFR-7), avec une
    # fenêtre plus courte qu'auparavant. Configurable via `JWT_REFRESH_TTL_SECONDS`.
    jwt_refresh_ttl_seconds: int = 14 * 24 * 60 * 60  # 14 j (G-9)

    # --- Intégration des tenants (tâche 8 / exigence 3.2) ---
    # Durée de vie du jeton de configuration à usage unique remis au tenant_admin
    # initial lors de l'intégration d'un tenant (`POST /api/admin/tenants`). Par
    # défaut 7 jours : laisse à l'administrateur de plateforme le temps de le
    # transmettre hors bande au centre, sans rester valable indéfiniment. Le jeton
    # n'est jamais stocké en clair (seule son empreinte Argon2id l'est) et est
    # consommé en usage unique via `POST /api/auth/set-credentials`. Configurable
    # via `SETUP_TOKEN_TTL_SECONDS`.
    setup_token_ttl_seconds: int = 7 * 24 * 60 * 60

    # --- Protection anti force brute à la connexion (tâche 7.3 / NFR-2) ---
    # Nombre d'échecs consécutifs toléré avant verrouillage temporaire du compte.
    # Au Nᵉ échec, `locked_until` est posé à `now + login_lockout_seconds` et les
    # tentatives suivantes (même avec le bon mot de passe) renvoient 429 jusqu'à
    # expiration du verrou. Configurable via `LOGIN_MAX_FAILED_ATTEMPTS`.
    login_max_failed_attempts: int = 5
    # Durée du verrou (en secondes) une fois le seuil atteint. Par défaut 15 min :
    # ralentit nettement une attaque par force brute sans bloquer durablement un
    # utilisateur légitime hors ligne. Configurable via `LOGIN_LOCKOUT_SECONDS`.
    login_lockout_seconds: int = 15 * 60

    # --- Rôle applicatif de base (tâche 4.1 / RLS 4.2) ---
    # Pour que la RLS soit TOUJOURS appliquée (tâche 4.2 / NFR-1), la connexion
    # runtime de l'API doit utiliser le rôle `dme_app` NON-superuser et
    # NOBYPASSRLS plutôt que le propriétaire du schéma (qui, bien que non
    # BYPASSRLS, détient les tables). Ces identifiants ne sont JAMAIS codés en
    # dur : ils proviennent de l'environnement (`APP_DB_USER` / `APP_DB_PASSWORD`,
    # voir `.env.example`). Lorsqu'ils sont absents (dev / tests sans rôle dédié),
    # on retombe sur `postgres_user` / `postgres_password` : la RLS reste
    # définie, mais seul le rôle non-privilégié en garantit l'application
    # systématique.
    app_db_user: str | None = None
    app_db_password: str | None = None

    # --- Amorçage des données de référence (tâche 6.2) ---
    # Chemin du GeoJSON des 5 régions du Togo chargé par `app.seed.regions`. Par
    # défaut, résolu relativement à la racine du dépôt (`<repo>/data/...`) pour le
    # développement/les tests hors conteneur. Dans l'image Docker, le fichier est
    # copié sous `/app/data/...` et ce chemin est surchargé via l'environnement
    # (`REGIONS_GEOJSON_PATH`), de sorte que la source d'amorçage ne soit jamais
    # codée en dur.
    regions_geojson_path: Path = (
        Path(__file__).resolve().parents[3] / "data" / "togo-regions.geojson"
    )

    # --- Amorçage des centres d'exemple pour le dev local (tâche 13) ---
    # Mot de passe de démonstration posé (haché Argon2id) sur les comptes amorcés
    # (`platform_admin` + un `tenant_admin` par région) afin qu'ils soient
    # immédiatement utilisables en développement local. UNIQUEMENT pour le dev :
    # connu et partagé, il n'est jamais destiné à la production (l'amorçage des
    # centres est un no-op quand `environment == 'production'`). Surchargeable via
    # `SEED_DEV_PASSWORD` ; jamais codé en dur ailleurs que ce défaut de dev.
    seed_dev_password: str = "dev-password-change-me"

    @property
    def _runtime_db_user(self) -> str:
        """Utilisateur de la connexion runtime (rôle applicatif si fourni)."""
        return self.app_db_user or self.postgres_user

    @property
    def _runtime_db_password(self) -> str:
        """Mot de passe de la connexion runtime (jamais codé en dur)."""
        if self.app_db_user:
            # Si un rôle applicatif est explicitement configuré, son mot de passe
            # doit l'être aussi (chaîne vide tolérée mais déconseillée).
            return self.app_db_password or ""
        return self.postgres_password

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """DSN SQLAlchemy asynchrone de la connexion runtime (pilote asyncpg).

        Forme : `postgresql+asyncpg://user:pass@host:port/db`.

        Utilise le rôle applicatif `dme_app` (`APP_DB_*`) s'il est configuré,
        sinon le compte principal (`POSTGRES_*`). Les migrations Alembic
        conservent leur propre DSN propriétaire (voir `alembic/env.py`).
        """
        return (
            f"postgresql+asyncpg://{self._runtime_db_user}:{self._runtime_db_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def owner_database_url(self) -> str:
        """DSN SQLAlchemy asynchrone du compte **propriétaire** du schéma.

        Toujours basée sur `POSTGRES_*`, indépendamment de `APP_DB_*`. Les
        migrations Alembic (création d'objets, RLS) doivent l'utiliser, car le
        rôle applicatif runtime (`dme_app`) est non-propriétaire et ne peut pas
        modifier le schéma.
        """
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def validate_production_safety(self) -> None:
        """Refuse un démarrage non sûr en production (fail-closed, G-1 / NFR-1, NFR-2).

        UNIQUEMENT lorsque `environment == "production"`, lève
        `UnsafeProductionConfigError` — en nommant le réglage fautif — si l'un
        des cas suivants est vrai :

        - le secret de signature JWT vaut encore le défaut de développement
          (`_DEV_JWT_SECRET`) : les jetons seraient forgeables ;
        - `debug` est actif : fuite d'informations sensibles ;
        - le rôle applicatif non-privilégié n'est pas configuré
          (`app_db_user` / `app_db_password` absents) : sans ce rôle `APP_DB_*`
          runtime (NOBYPASSRLS), l'application systématique de la RLS n'est pas
          garantie.

        Hors production (développement, tests), la méthode est un no-op : elle
        n'introduit aucune nouvelle erreur sur le chemin des requêtes et ne casse
        pas la collecte des tests (qui importe l'app avec des valeurs de dev).
        """
        if self.environment != "production":
            return

        if self.jwt_secret == _DEV_JWT_SECRET:
            raise UnsafeProductionConfigError(
                "Configuration de production non sûre : `jwt_secret` utilise "
                "encore le secret de développement par défaut. Surchargez "
                "`JWT_SECRET` par une valeur unique et aléatoire."
            )

        if self.debug:
            raise UnsafeProductionConfigError(
                "Configuration de production non sûre : `debug` est actif. "
                "Posez `DEBUG=false` en production."
            )

        if not self.app_db_user or not self.app_db_password:
            raise UnsafeProductionConfigError(
                "Configuration de production non sûre : le rôle applicatif "
                "non-privilégié n'est pas configuré (`app_db_user` / "
                "`app_db_password`). Fournissez `APP_DB_USER` et "
                "`APP_DB_PASSWORD` (rôle NOBYPASSRLS) pour garantir "
                "l'application systématique de la RLS."
            )


@lru_cache
def get_settings() -> Settings:
    """Renvoie une instance unique et mémoïsée de la configuration."""
    return Settings()
