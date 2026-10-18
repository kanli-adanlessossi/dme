# Design Document

## Overview

Objet : mise en production — correction des écarts recensés dans `requirements.md`.
Ce document propose une conception (le *comment*) pour chaque écart énoncé dans
`requirements.md` (le *quoi*/*pourquoi*). Le principe directeur est **le moindre
changement sûr** : les corrections sont, autant que possible, **additives** et
**fail-closed**, sans toucher au cœur fonctionnel déjà vérifié (RLS, audit, sync).
Aucun changement ne doit régresser les 256 tests existants ni la pile locale.

Chaque écart garde son identifiant `G-n` (aligné sur la numérotation des exigences :
G-1 ↔ Requirement 1, etc.). Les correctifs se répartissent en trois familles :

1. **Code applicatif backend** : garde-fou de démarrage (G-1), en-têtes API (G-2),
   restriction CORS (G-5), finition `center_profile` (G-6).
2. **Frontière de déploiement** (Docker / reverse proxy / serveur statique / CI) :
   image frontend de production (G-3), limitation de débit (G-4), CI avec Postgres
   (G-7).
3. **Qualité / décision** : résorption des dépréciations (G-8) ; les décisions G-6 et
   G-9 (désormais **tranchées**) sont développées en ADR dans la section
   « Architecture » (sous-section « Décisions d'architecture (ADR) » : Contexte /
   Options / Décision).

Les sections ci-dessous suivent le gabarit de conception : Architecture (où se
branche chaque correctif dans la pile), Components and Interfaces (les composants
touchés, par écart), Data Models (le peu d'état introduit), Correctness Properties
(effet sur P1–P13 et décisions ADR), Error Handling, Testing Strategy.

## Architecture

Les correctifs se branchent à des points précis de la pile existante, sans
réorganiser l'architecture local-first/multi-tenant d'origine.

```
   Navigateur (PWA)
      │
      ▼
   [Reverse proxy TLS]  ← G-4 limitation de débit /api/public/* ; HSTS (G-2)
      │
      ├──────────────► [Serveur statique Nginx]  ← G-3 image de prod + CSP applicative
      │                 (sert dist/ ; en-têtes de sécurité G-2)
      │
      ▼
   [API FastAPI]        ← G-1 garde-fou démarrage (lifespan) ; G-2 middleware
      │                   en-têtes API ; G-5 CORS restreint ; G-8 dépréciations
      ▼
   [PostgreSQL + PostGIS]  ← G-7 vérifié réellement en CI (RLS/sync)

   Frontend local (RxDB outbox)  ← G-6 : fin de la fuite d'outbox center_profile
```

### Frontière de responsabilité des en-têtes (tranchée)

Deux composants peuvent poser des en-têtes ; pour éviter les doublons ou les trous,
la responsabilité est **répartie explicitement** :

| En-tête | Posé par | Justification |
|---------|----------|---------------|
| `Content-Security-Policy` (applicative, complète) | **Serveur statique frontend** (G-3) | Lui seul connaît les origines réelles des tuiles/styles/glyphs MapLibre et sert le HTML/JS. |
| `Content-Security-Policy` (API, verrouillée) | **API** (G-2) | Réponses API = données, jamais de HTML exécutable → CSP minimale `default-src 'none'`. |
| `X-Content-Type-Options`, `X-Frame-Options` | **API** + **serveur statique** | Peu coûteux, utile des deux côtés ; valeurs identiques, pas de conflit. |
| `Strict-Transport-Security` (HSTS) | **Point de terminaison TLS uniquement** (reverse proxy en prod) | HSTS n'a de sens qu'émis par le composant qui termine TLS ; l'émettre depuis l'API (derrière le proxy) serait inopérant ou trompeur. L'API ne pose donc PAS HSTS. |

Cette répartition satisfait G-2 sans double émission : l'API verrouille ses propres
réponses ; le serveur statique porte la CSP applicative riche ; le proxy porte HSTS.

### G-1 — Point de branchement du garde-fou de démarrage
Validation au démarrage de l'app via un gestionnaire `lifespan` FastAPI, de sorte
qu'un démarrage non sûr échoue **avant** d'accepter la moindre requête. Le `lifespan`
est préféré à un contrôle à l'import pour ne pas casser la collecte des tests (qui
importent `app.main`).

Alternatives écartées :
- **Validateur Pydantic au chargement de `Settings`** : se déclencherait à l'import,
  pendant la collecte des tests (valeurs de dev) → fragiliserait la suite.
- **Vérification dans `entrypoint.sh`** comme contrôle principal : dépend du
  conteneur, contournable hors Docker ; acceptable seulement en garde-fou secondaire.
- **Avertissement non bloquant (log)** : contraire au fail-closed.

### G-3 — Image de déploiement du frontend
`frontend/Dockerfile` en build **multi-stage** : *stage build* (`node:20-slim`,
`npm ci`, `npm run build` → `dist/`) puis *stage run* (`nginx:alpine` servant
`dist/`). Nginx (a) pose les en-têtes de sécurité et la CSP applicative ; (b) applique
le fallback SPA (`try_files ... /index.html`) ; (c) sert `sw`/manifeste avec un cache
court et les assets horodatés avec un cache long immuable. Le service `web` du compose
local continue d'utiliser `npm run dev` (NFR-12 préservé).

Alternatives écartées : **Caddy** (viable ; Nginx retenu pour l'alignement avec le
reverse proxy TLS déjà évoqué) ; **serveur statique applicatif** (`vite preview`,
`serve` — non durci) ; **servir le frontend depuis FastAPI** (couple les cycles de
vie, complique CSP/cache).

### G-4 — Emplacement de la limitation de débit
Limitation au **reverse proxy** (Nginx `limit_req_zone` / `limit_req`) sur
`/api/public/*`, seuil 60 req/min/IP (burst configurable), réponse 429. Repli
documenté : middleware applicatif SlowAPI si aucun proxy (charge l'API ; en
multi-instance, exige Redis pour un compteur global). **Rate-limit distribué (Redis)**
seulement si nécessaire. Option « s'appuyer sur le seul service worker » écartée (ne
protège pas d'un client sans SW).

### Décisions d'architecture (ADR)

Les deux décisions ci-dessous sont **tranchées** (justification : bonnes pratiques +
contexte togolais).

#### ADR G-6 — Traitement de `center_profile` dans l'outbox
**Statut : DÉCIDÉ (Option 2).**

**Options.** (1) Brancher le profil sur `PATCH /api/center/profile` depuis `sync.ts`,
puis retirer l'entrée de l'outbox sur succès (permet l'édition hors ligne du profil
avec propagation différée). (2) Ne plus enfiler `center_profile` dans l'outbox
(`upsertCenterProfile` en REST direct, en ligne ; plus simple, supprime la croissance
de l'outbox).

**Recommandation.** Option 2 si le profil est toujours édité en ligne (cas courant :
l'admin tenant a du réseau pour gérer la vitrine publique) ; option 1 si l'édition
hors ligne du profil doit survivre à une coupure. Dans les deux cas, ne pas créer de
contrat de sync conflictuel avec le protocole figé.

**Décision (tranchée) : Option 2 — ne plus enfiler `center_profile` dans l'outbox.**
Le profil du centre est la **vitrine publique** (annuaire + carte), édité par le
`tenant_admin`. Ce rôle administre la présence publique du centre et dispose
typiquement de connectivité au moment de cette tâche, contrairement au personnel
clinique opérant sur le terrain. L'édition **hors ligne** du profil n'est donc pas un
besoin terrain togolais réel : la rareté du réseau concerne le travail clinique
(patients/visites, déjà couverts par l'outbox), pas la gestion de la vitrine. On
retient donc la solution la plus simple et sans dette : `upsertCenterProfile`
(`frontend/src/lib/db.ts`) **n'inscrit plus** d'entrée d'outbox ; l'enregistrement du
profil passe en REST direct via `PATCH /api/center/profile` (en ligne). Conséquence :
l'outbox cesse de croître (Property 5) et aucun contrat de sync conflictuel n'est
introduit. Si un besoin d'édition hors ligne du profil émergeait, on rebasculerait
vers l'option 1 sans remettre en cause le protocole figé.

#### ADR G-9 — Fenêtre de révocation JWT et rotation du refresh
**Statut : DÉCIDÉ (Options 2 + 3).**

**Contexte.** Un access token reste valide jusqu'à son expiration (défaut 12 h) après
désactivation de compte/tenant ; la purge côté client ne survient qu'au prochain
`sync/pull` réussi. Le refresh reste valide jusqu'à son expiration (défaut 30 j) sans
invalidation serveur. Cible : poste de santé **partagé** à connectivité intermittente
(NFR-7) ; le vol d'un jeton y est un risque PHI concret.

**Options.**
1. **Statu quo** (stateless, TTL 12 h / 30 j) : simple, pleinement compatible
   hors-ligne ; fenêtre de révocation = TTL.
2. **Raccourcir `jwt_access_ttl_seconds`** : réduit la fenêtre au prix de refresh
   plus fréquents (impose davantage de connectivité).
3. **Denylist / famille `jti` côté serveur** : invalide un refresh avant expiration
   et détecte le rejeu (un `jti` consommé invalide la famille) ; consultée **en
   ligne** seulement — les acteurs hors ligne restent bornés par le TTL. Ajoute un
   état serveur (table + lecture au refresh), seul modèle de données éventuel de ce
   lot.
4. **Jetons hors `localStorage`** (cookie `HttpOnly`/`Secure`) : mitige le XSS à la
   source mais révise le modèle local-first et la révocation hors ligne.

**Cadre de décision attendu.** Comparer explicitement la fenêtre de révocation vs la
tolérance au hors-ligne (NFR-7) et l'impact d'un vol de jeton sur poste partagé.
Documenter la limite assumée : la révocation d'un acteur hors ligne reste, par nature,
bornée par le TTL.

**Décision (tranchée) : combinaison Options 2 + 3 — raccourcir l'access ET denylist
`jti` du refresh, consultée en ligne uniquement.**

Justification (contexte togolais : connectivité intermittente, postes partagés, PHI).
Les options 4 (cookie `HttpOnly`) et une révocation en ligne systématique sont
écartées : elles supposent de la connectivité à chaque vérification et casseraient le
travail hors ligne (NFR-7), déterminant hors de Lomé. Le statu quo (option 1) laisse à
l'inverse une fenêtre trop large sur un poste partagé manipulant des PHI. On retient
donc un compromis defense-in-depth qui **ne sacrifie pas la disponibilité hors
ligne** :

- **Access token : TTL ramené de 12 h à 8 h** (`jwt_access_ttl_seconds = 28800`). Huit
  heures couvrent encore une journée de travail hors ligne typique (NFR-7) tout en
  réduisant d'un tiers la fenêtre de révocation par rapport à 12 h. Le refresh
  opportuniste à la reconnexion reste inchangé.
- **Refresh : famille `jti` + denylist côté serveur**, consultée **uniquement sur le
  chemin en ligne** (endpoint `/api/auth/refresh`). Un `jti` déjà consommé invalide
  toute la famille (détection de rejeu/vol). Les postes **hors ligne** ne consultent
  jamais la denylist : ils continuent de travailler sur l'access valide jusqu'à son
  TTL — la révocation d'un acteur hors ligne reste donc bornée par le TTL (8 h), limite
  assumée et documentée.
- **Refresh TTL : ramené de 30 j à 14 j** pour borner la durée d'exploitabilité d'un
  refresh volé qui ne se reconnecterait jamais (donc jamais confronté à la denylist),
  tout en couvrant une absence prolongée raisonnable.

Conséquences et portée. Cette décision ajoute le **seul modèle de données** du lot :
une table serveur de refresh (`jti`, `family_id`, `consumed_at`, `expires_at`,
`user_id`) et une lecture au `refresh`. Son implémentation **dépasse le périmètre du
lot courant** (qui ne contient que des correctifs additifs) : elle est donc sortie en
tâches dédiées à créer (cf. tâche 9.2), tandis que le raccourcissement des TTL (simple
changement de configuration) peut être appliqué immédiatement. Le compromis
« révocation hors ligne bornée par le TTL » est la limite explicitement assumée
(NFR-7).

## Components and Interfaces

Composants touchés par chaque écart, avec le contrat d'interface concerné.

### G-1 — `app/core/config.py` + `app/main.py`
Ajouter une méthode `Settings.validate_production_safety()` qui, **uniquement**
lorsque `environment == "production"`, lève `UnsafeProductionConfigError` si : le
secret JWT vaut le défaut de développement (constante `_DEV_JWT_SECRET` centralisée),
`debug` est vrai, ou `app_db_user`/`app_db_password` est absent. Invocation depuis le
`lifespan` de `app/main.py`.

> État du code (vérifié) : `config.py` ne contient actuellement **aucune** de ces
> définitions (ébauche antérieure entièrement revertie). Implémentation à partir de
> zéro, sans réconciliation.

### G-2 — Middleware d'en-têtes API (`app/main.py`)
Middleware Starlette léger ajoutant à chaque réponse : `X-Content-Type-Options:
nosniff`, `X-Frame-Options: DENY`, et une CSP verrouillée adaptée à une API JSON
(`default-src 'none'; frame-ancestors 'none'`). L'API ne pose **pas** HSTS (cf.
Architecture). Alternative `secweb`/`starlette-secureheaders` écartée (dépendance pour
quelques en-têtes statiques ; un middleware maison ~15 lignes suffit et reste
auditable). Poser les en-têtes au seul proxy est écarté : l'API doit rester sûre même
exposée directement (dev, tests, déploiement sans proxy).

### G-3 — CSP applicative servie par Nginx
Directives concrètes à dériver des origines réellement utilisées (MapLibre + API +
service worker). Base proposée, à ajuster selon le fournisseur de tuiles :

```
default-src 'self';
connect-src 'self' <ORIGINE_API>;            # appels fetch vers l'API + /api/public
img-src 'self' data: blob:;                   # tuiles raster/sprites encodées + GeoJSON rendu
style-src 'self' 'unsafe-inline';             # MapLibre injecte des styles inline
font-src 'self';                              # glyphs servis localement (pas de CDN)
worker-src 'self';                            # service worker + web workers MapLibre
frame-ancestors 'none';
object-src 'none';
base-uri 'self';
report-to csp-endpoint;                       # observabilité des violations (G-2)
```

> Note : le projet sert ses propres tuiles/GeoJSON (MapLibre « sans clé d'API ») ;
> aucune origine tierce n'est nécessaire a priori. Si un fournisseur externe est
> introduit, son origine DOIT être ajoutée à `connect-src`/`img-src` et documentée.
> `'unsafe-inline'` sur `style-src` est le compromis connu de MapLibre ; à remplacer
> par des nonces si l'effort est consenti.

**Collecte et surveillance des rapports CSP (bonne pratique PHI).** Une CSP dont les
violations ne sont ni collectées ni surveillées est aveugle : les tentatives d'XSS
passeraient inaperçues. Le `report-to csp-endpoint` ci-dessus DOIT donc pointer vers
un **endpoint de collecte réel** (groupe `Report-To`/`Reporting-Endpoints` nommé
`csp-endpoint`), dédié à la réception des rapports de violation. Cet endpoint peut
être interne (une route `POST /api/csp-report` journalisant dans l'`audit_log` sans
PHI — action transversale, cohérent avec NFR-3) ou un collecteur externe maîtrisé.
Les rapports DOIVENT être **surveillés** : un pic de violations est un signal
d'attaque ou de régression de la CSP. Déploiement progressif recommandé : commencer en
`Content-Security-Policy-Report-Only` pour calibrer les directives sans casser
l'application, puis basculer en CSP applicative dès que le flux de rapports est
propre.

### G-2/G-3 — Endpoint de collecte des rapports CSP
Pour que la directive `report-to` ne soit pas aveugle (bonne pratique PHI), prévoir un
collecteur de rapports de violation CSP. Option interne recommandée : une route
`POST /api/csp-report` qui valide sommairement le corps (type `application/reports+json`
ou `application/csp-report`), **ne journalise aucune PHI**, et inscrit un événement
transversal dans l'`audit_log` (action `csp.violation`, `tenant_id` NUL — cohérent
avec l'écrivain d'audit non contraint par le tenant, NFR-3). Alternative : collecteur
externe maîtrisé. Les rapports sont destinés à la **surveillance** (un pic signale une
attaque ou une régression de CSP).

### G-4 — Cache des endpoints publics (`app/routers/public.py`)
Poser `Cache-Control: public, max-age=300` sur `/api/public/regions` et
`/api/public/geo/togo-regions.geojson` (l'API connaît la nature des données ; le proxy
peut ajouter un cache de bord). Aucune modification des schémas publics ni de
`public_session` / `public_read`.

### G-5 — Configuration CORS (`app/main.py`)
Remplacer `allow_methods=["*"]` / `allow_headers=["*"]` par des listes explicites
pilotées par la configuration : méthodes `GET, POST, PATCH, DELETE, OPTIONS` ;
en-têtes `Authorization, Content-Type`. Les origines restent explicites (déjà le cas).
Déléguer CORS au proxy est écarté (le middleware FastAPI reste nécessaire hors proxy).

### G-6 — Moteur de sync frontend (`frontend/src/lib/sync.ts`, `db.ts`)
Deux options d'interface (voir Correctness Properties pour l'ADR) : (1) pousser les
entrées d'outbox `center_profile` via `PATCH /api/center/profile` puis les retirer sur
succès ; (2) cesser de les inscrire dans l'outbox (`upsertCenterProfile` en REST
direct). Dans les deux cas, ne pas créer de contrat de sync conflictuel avec le
protocole figé (`patient`/`visit`).

### G-7 — Pipeline CI
Job CI avec service `postgis/postgis:16-3.4`, `alembic upgrade head`, puis `pytest`
complet (les 54 tests DB ne sont plus ignorés). Alternative testcontainers
équivalente ; le service CI natif suffit.

### G-8 — Dépendances
Montée de version FastAPI/Starlette/httpx ; remplacer `HTTP_422_UNPROCESSABLE_ENTITY`
par son nouveau nom ; migrer l'usage `httpx`/`testclient` déprécié. Mécanique, sans
impact fonctionnel.

## Data Models

Les correctifs sont presque entièrement **sans état** : ils ne modifient aucun schéma
de base de données ni aucun modèle de domaine existant.

- **G-1** : ajoute une constante (`_DEV_JWT_SECRET`) et une exception
  (`UnsafeProductionConfigError`) en code ; aucun champ persistant.
- **G-2 / G-5** : configuration et en-têtes uniquement ; aucun modèle. Le collecteur
  de rapports CSP (`POST /api/csp-report`) n'introduit **aucune** nouvelle table : il
  réutilise l'`audit_log` existant (événement transversal `csp.violation`, sans PHI).
- **G-3 / G-4 / G-7 / G-8** : infrastructure et dépendances ; aucun modèle.
- **G-6** : aucun nouveau modèle ; agit sur la table locale `outbox` existante (RxDB)
  en bornant/supprimant les entrées `center_profile`.
- **G-9 (décidé : Options 2 + 3)** : **seul** correctif introduisant un modèle. La
  denylist de refresh retenue ajoute une table serveur de rotation des refresh :

  ```
  refresh_token  (rotation + denylist ; cadrée au tenant via user_id)
    jti uuid pk                 -- identifiant unique du refresh émis
    family_id uuid              -- famille de rotation (un vol invalide la famille)
    user_id uuid fk -> app_user
    consumed_at timestamptz null -- posé quand le jti est échangé (rotation)
    expires_at timestamptz       -- aligné sur le refresh TTL (14 j)
    created_at timestamptz
  ```

  La lecture/écriture n'intervient que sur le chemin **en ligne** (`/api/auth/refresh`)
  ; les postes hors ligne n'y touchent jamais. Le raccourcissement des TTL (access 8 h,
  refresh 14 j) est, lui, une simple modification de configuration sans modèle. Cette
  table est hors du périmètre additif du lot courant (voir tâche 9).

## Correctness Properties

Invariants que les correctifs de ce lot doivent maintenir, en complément des
propriétés P1–P13 du design d'origine (dont ils ne modifient aucun chemin de données).
La stratégie de test existe pour les vérifier.

### Property 1: Démarrage sûr en production (G-1)

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

LORSQUE l'environnement est « production », l'application ne démarre JAMAIS avec un
secret JWT par défaut, `debug` actif, ou sans le rôle applicatif NOBYPASSRLS. Le
garde-fou n'**implémente pas** P1/P2 : il **empêche une configuration qui les
invaliderait** (un runtime sous le propriétaire du schéma contournerait la RLS
forcée). Hors production, le contrôle est neutre.

### Property 2: En-têtes de sécurité toujours présents (G-2, G-3)

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 3.2**

Toute réponse de production porte les en-têtes de sécurité attendus (CSP, `nosniff`,
anti-cadrage ; HSTS au point de terminaison TLS), et les violations CSP sont
effectivement **collectées et surveillables** (endpoint de rapport réel, non aveugle).
Renforce la défense en profondeur autour de **P4** (aucune PHI publique) en réduisant
l'impact d'un XSS et en rendant visibles les tentatives, sans modifier les chemins de
données.

### Property 3: Surface publique bornée et préservée (G-4)

**Validates: Requirements 4.1, 4.2, 4.3**

LA limitation de débit et le cache des endpoints publics NE modifient JAMAIS la
projection sûre (**P4**) ni la règle « actifs seulement » (**P8**) : aucun champ privé
ni PHI exposé, tenants inactifs toujours exclus. Un dépassement de seuil renvoie 429.

### Property 4: Garanties d'isolation vérifiées, non théoriques (G-7)

**Validates: Requirements 7.1, 7.2**

LES propriétés **P1**, **P2**, **P11** sont vérifiées à chaque intégration (les 54
tests RLS/sync s'exécutent réellement en CI), et non seulement supposées.

### Property 5: Outbox bornée (G-6)

**Validates: Requirements 6.1, 6.2**

LE nombre d'entrées d'outbox non synchronisables (p. ex. `center_profile`) NE croît
JAMAIS sans borne : ces mutations sont soit propagées puis retirées, soit jamais
inscrites. Aucun contrat de sync conflictuel avec le protocole figé (`patient`/`visit`)
n'est introduit.

### Property 6: Neutralité des correctifs restants (G-5, G-8)

**Validates: Requirements 5.1, 5.2, 5.3, 8.1**

LES correctifs CORS (G-5) et dépréciations (G-8) sont neutres vis-à-vis de P1–P13 :
ils ne modifient ni les chemins de données, ni les garanties d'isolation, ni la
surface publique.

## Error Handling

- **G-1** : en production avec configuration non sûre, lever
  `UnsafeProductionConfigError` au démarrage (fail-fast) avec un message nommant le
  réglage fautif ; l'app n'accepte aucune requête. Hors production, no-op. Aucune
  nouvelle erreur runtime n'est introduite sur le chemin des requêtes.
- **G-2** : le middleware n'altère que les en-têtes, jamais les corps ni les statuts ;
  il ne peut donc pas transformer une réponse valide en erreur. Le collecteur
  `POST /api/csp-report` répond toujours 204 (même sur corps mal formé : on ignore
  silencieusement plutôt que de renvoyer une erreur exploitable) et ne bloque jamais
  le rendu client ; un échec d'écriture d'audit du rapport est journalisé sans faire
  échouer la requête de rapport (le rapport CSP n'est pas une mutation PHI).
- **G-4** : un dépassement de seuil renvoie **429** (réponse normalisée du proxy) ;
  aucune fuite d'information dans le corps.
- **G-5** : un pré-vol CORS non autorisé échoue côté navigateur comme aujourd'hui ;
  la restriction n'ajoute pas de code d'erreur serveur.
- **G-6** : si l'option REST échoue (option 1), l'entrée reste dans l'outbox et sera
  réessayée — jamais perdue silencieusement (cohérent avec le modèle outbox existant).
- Les autres correctifs (G-3, G-7, G-8) n'introduisent pas de nouveau chemin d'erreur
  runtime.

## Testing Strategy

- **Unitaire backend** : G-1 (5 cas de validation : prod+secret défaut, prod+debug,
  prod+APP_DB absent, prod+config saine, dev), G-2 (présence des en-têtes API sur
  `/health`), G-5 (config CORS restreinte sans joker).
- **Intégration (CI avec Postgres, G-7)** : réactiver les 54 tests ignorés
  (vérification réelle de P1/P2/P11).
- **Manuel / e2e** : G-3 (rechargement hors ligne sur le build Nginx + présence CSP
  applicative sans joker), G-4 (429 au-delà du seuil ; `max-age` présent), G-6
  (l'outbox ne croît plus sous éditions répétées de profil).
- **Non-régression** : les 256 tests existants restent verts après G-1/G-2/G-5/G-6.

### Definition of Done transverse
Un correctif est « terminé » quand : (1) son comportement observable satisfait les
critères d'acceptation de l'exigence ; (2) un test automatisé le couvre (ou, pour les
éléments d'infrastructure, une procédure de vérification documentée) ; (3) aucune
régression des 256 tests ; (4) la décision associée (le cas échéant) est complétée
dans sa sous-section ADR de « Architecture » (G-6, G-9).
