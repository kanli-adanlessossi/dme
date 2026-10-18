# Requirements Document

## Introduction

Ce document regroupe l'ensemble des **écarts (« gaps ») identifiés lors de l'analyse du code
v1** de la plateforme DME (centres médicaux du Togo). L'analyse a lu les couches à
plus fort risque (sécurité backend, isolation RLS, moteur de synchronisation, cœur
local-first du frontend, migrations, Docker) et exécuté les suites de tests
(frontend 132/132 ; backend 202 réussis, 54 ignorés faute de Postgres actif).

Le cœur fonctionnel et de sécurité (RLS forcée, audit append-only, chiffrement des
PHI au repos côté client, synchronisation idempotente) est de bonne qualité et
vérifié. Les écarts restants se situent très majoritairement à la **frontière de
déploiement** : configuration de production, en-têtes de sécurité, façon de servir le
frontend, et quelques finitions. Aucun écart n'invalide l'architecture ; ils doivent
néanmoins être traités **avant tout déploiement hors poste local**.

Chaque exigence décrit un comportement observable (le *quoi*/*pourquoi*), jamais une
solution technique (le *comment*, qui relève de `design.md`). Les critères
d'acceptation suivent le style EARS.

Chaque exigence porte un identifiant d'écart stable (`G-1` … `G-9`) rappelé dans son
titre, qui sert de clé de traçabilité entre les trois documents (la tâche qui corrige
`G-n` porte le même identifiant dans `tasks.md`). La numérotation `Requirement N` est
séquentielle et distincte de cet identifiant. La sévérité est un attribut
(Élevée / Moyenne / Faible) indiqué sur chaque exigence.

Les exigences 1 à 6 et 9 sont des exigences produit ; 7 et 8 sont des exigences de
processus/CI. L'exigence 9 découle d'une décision d'architecture **tranchée** (ADR
G-9, développé dans `design.md`) et porte désormais des critères d'acceptation
concrets et testables. Deux améliorations facultatives non exigées (A-1, A-2) sont
décrites dans la section Glossary pour mémoire.

Hors portée : nouvelles fonctionnalités métier, DME clinique complet, et tout élément
déjà marqué hors périmètre v1 dans `medical-centers-togo`.

## Glossary

- **CSP** (Content-Security-Policy) : en-tête HTTP restreignant les sources de
  contenu exécutable, principale défense contre le XSS.
- **HSTS** (HTTP Strict Transport Security) : en-tête forçant l'usage de TLS.
- **Fail-fast au démarrage** : refus explicite de booter lorsqu'une configuration est
  non sûre, plutôt qu'un démarrage silencieux dégradé.
- **Point de terminaison TLS** : composant (reverse proxy en production) qui termine
  la connexion TLS ; c'est lui qui émet HSTS.
- **Amélioration A-1 (facultative, non exigée)** : bascule du chiffrement local du
  plugin RxDB gratuit (crypto-js) vers le plugin Web Crypto natif (API wrapper
  identique), pour la performance, sans changer l'architecture (NFR-8, P13).
- **Amélioration A-2 (facultative, non exigée)** : déplacement des jetons de session
  hors `localStorage` vers un cookie `HttpOnly`/`Secure` pour réduire l'exposition au
  XSS. Examinée puis **écartée** par l'ADR G-9 (option 4) : elle réviserait le modèle
  local-first et la révocation hors ligne, incompatibles avec la connectivité
  intermittente togolaise. Conservée ici comme piste de refonte éventuelle hors v1.

## Requirements

### Requirement 1: [G-1] Refus de démarrage en production avec une configuration non sûre

**Sévérité:** Élevée.

**Constat:** la configuration fournit un secret JWT par défaut non sûr et le mode
`debug` actif par défaut. Rien n'empêche un déploiement en production de démarrer
avec ces valeurs, ni sans le rôle applicatif NOBYPASSRLS — auquel cas la RLS n'est
plus garantie d'être systématiquement appliquée.

**User Story:** En tant qu'exploitant de la plateforme, je veux que l'application
refuse de démarrer en production avec une configuration non sûre, afin de ne jamais
exposer de PHI avec des réglages faibles.

#### Acceptance Criteria

1. LORSQUE l'environnement est « production » ET que le secret de signature des
   jetons vaut encore sa valeur par défaut de développement ALORS le système DOIT
   refuser de démarrer et émettre un message d'erreur explicite nommant le réglage
   fautif.
2. LORSQUE l'environnement est « production » ET que le mode `debug` est actif ALORS
   le système DOIT refuser de démarrer.
3. LORSQUE l'environnement est « production » ET que le rôle applicatif
   non-privilégié (NOBYPASSRLS) n'est pas configuré ALORS le système DOIT refuser de
   démarrer, car la connexion runtime retomberait sur le propriétaire du schéma et
   l'application systématique de la RLS ne serait plus garantie.
4. LORSQUE le refus survient ALORS il DOIT se produire avant que l'application
   n'accepte la moindre requête.
5. TANT QUE l'environnement n'est pas « production » (développement, tests) LE
   contrôle NE DOIT PAS s'appliquer : aucune régression de la pile locale ni des 256
   tests existants.

- _Requirements: NFR-1, NFR-2 ; P1, P2_

### Requirement 2: [G-2] En-têtes de sécurité HTTP sur les réponses

**Sévérité:** Élevée.

**Constat:** recherche sur tout le dépôt → aucun `Content-Security-Policy`,
`Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`.
`index.html` ne porte aucune balise `<meta http-equiv>`. Or le frontend conserve des
jetons côté navigateur et manipule des PHI : l'absence de CSP élargit fortement
l'impact d'un éventuel XSS.

**User Story:** En tant que responsable sécurité, je veux que les réponses servies
portent des en-têtes de sécurité stricts, afin de réduire la surface d'attaque (XSS,
clickjacking, downgrade TLS, MIME-sniffing).

#### Acceptance Criteria

1. LORSQUE le frontend applicatif est servi ALORS la réponse DOIT porter une
   `Content-Security-Policy` dont, au minimum : `default-src 'self'`,
   `frame-ancestors 'none'`, `object-src 'none'`, et des directives
   `connect-src`/`img-src`/`style-src`/`font-src`/`worker-src` énumérant
   explicitement les origines réellement utilisées (API, tuiles/styles/glyphs
   MapLibre, service worker), sans joker (`*`) ni origine tierce non maîtrisée.
2. LORSQUE la CSP détecte une violation ALORS la politique DOIT émettre un rapport
   vers un endpoint de collecte configuré (`report-to`/`report-uri`), ET cet endpoint
   DOIT recevoir et conserver les rapports de façon à pouvoir être surveillé (un
   rapport émis mais non collecté ne satisfait pas ce critère).
3. LORSQU'une réponse est servie ALORS elle DOIT porter `X-Content-Type-Options:
   nosniff` et interdire l'inclusion en cadre (`X-Frame-Options: DENY` ou
   `frame-ancestors 'none'`).
4. LORSQUE le point de terminaison TLS répond en production ALORS il DOIT émettre
   `Strict-Transport-Security` (HSTS) avec `max-age` ≥ 15552000 (180 jours).
5. LES en-têtes DOIVENT être posés sur le chemin de production ; une CSP de base
   DEVRAIT être présente dès le développement.

- _Requirements: NFR-2 ; P4_

### Requirement 3: [G-3] Image de déploiement du frontend en artefacts de production

**Sévérité:** Élevée.

**Constat:** l'image Docker du frontend lance le serveur de développement Vite (HMR,
sources non minifiées, aucun durcissement) au lieu d'un build servi par un serveur
statique. Combinée à l'exigence 2, l'image n'est pas déployable au-delà du local.

**User Story:** En tant qu'exploitant, je veux une image frontend de production
(artefacts compilés, servis par un serveur statique durci), afin de déployer en toute
sécurité et performance.

#### Acceptance Criteria

1. LORSQUE l'image de production est construite ALORS elle DOIT contenir les
   artefacts compilés (build de production) et NON le serveur de développement.
2. LORSQUE l'application est servie en production ALORS elle DOIT l'être par un
   serveur statique durci qui porte les en-têtes de sécurité de l'exigence 2.
3. LORSQUE l'application de production est chargée puis rouverte sans réseau ALORS
   elle DOIT se recharger et fonctionner hors ligne (service worker actif).
4. TANT QUE l'on développe en local LA pile `docker compose up` PEUT continuer
   d'utiliser le serveur de développement.

- _Requirements: NFR-5, NFR-11, NFR-12_

### Requirement 4: [G-4] Limitation de débit et cache des endpoints publics

**Sévérité:** Moyenne.

**Constat:** `/api/public/*` est non authentifié et touche la base. Rien ne borne le
débit → surface de déni de service et de scraping de l'annuaire.

**User Story:** En tant qu'exploitant, je veux limiter le débit des endpoints publics
et mettre en cache les données de référence, afin de résister au DoS et au scraping
sans dégrader l'expérience légitime.

#### Acceptance Criteria

1. LORSQU'une même source dépasse le seuil de débit configuré sur `/api/public/*`
   (défaut proposé : 60 requêtes par minute et par IP) ALORS le système DOIT répondre
   429 jusqu'à retour sous le seuil.
2. LORSQUE les données de référence quasi statiques (`/api/public/regions`,
   `/api/public/geo/togo-regions.geojson`) sont servies ALORS la réponse DOIT porter
   `Cache-Control: public, max-age=300` au minimum.
3. LA limitation et le cache NE DOIVENT PAS modifier la projection sûre ni la règle
   « actifs seulement » : aucun champ privé ni PHI exposé, tenants inactifs toujours
   exclus.

- _Requirements: NFR-2 ; P4, P8_

### Requirement 5: [G-5] Restriction CORS aux méthodes et en-têtes nécessaires

**Sévérité:** Faible.

**Constat:** les origines CORS sont correctement explicites (jamais `*`), mais les
méthodes et en-têtes autorisés sont des jokers (`*`).

**User Story:** En tant que responsable sécurité, je veux restreindre les méthodes et
en-têtes CORS au strict nécessaire, afin de réduire la surface.

#### Acceptance Criteria

1. LORSQUE la réponse de pré-vol CORS est émise en production ALORS elle NE DOIT
   autoriser que les méthodes réellement utilisées par le frontend et que les
   en-têtes nécessaires (au minimum `Authorization` et `Content-Type`), sans joker.
2. LES origines autorisées DOIVENT rester explicites (jamais `*`).
3. LORSQUE la restriction est appliquée ALORS le frontend (authentification Bearer +
   requêtes JSON) DOIT continuer de fonctionner.

- _Requirements: NFR-2_

### Requirement 6: [G-6] Fin de la fuite d'outbox pour `center_profile`

**Sévérité:** Moyenne.

**Constat:** les mutations de `center_profile` sont inscrites dans l'outbox mais
jamais poussées (le contrat de sync ne couvre que `patient`/`visit` ; le profil a son
endpoint REST dédié). L'outbox peut donc accumuler indéfiniment des entrées jamais
purgées.

**User Story:** En tant que mainteneur, je veux que l'outbox ne croisse pas sans fin
à cause des mutations de profil, afin de préserver les performances et la clarté de
l'état local.

#### Acceptance Criteria

1. LORSQU'un profil de centre est modifié de façon répétée ALORS le nombre d'entrées
   d'outbox non synchronisables NE DOIT PAS croître sans borne : soit ces mutations
   sont effectivement propagées et retirées de l'outbox, soit elles n'y sont plus
   inscrites.
2. LE comportement retenu NE DOIT PAS créer de contrat de synchronisation
   conflictuel avec le protocole figé (`patient`/`visit`).

- _Requirements: NFR-5, NFR-6_

### Requirement 7: [G-7] Couverture RLS réelle vérifiée en intégration continue

**Sévérité:** Moyenne (processus).

**Constat:** 54 tests d'intégration base de données (RLS, sync bout-en-bout) sont
ignorés hors conteneur. C'est là que vivent l'essentiel des garanties d'isolation.

**User Story:** En tant que mainteneur, je veux que la couverture RLS soit vérifiée à
chaque intégration, afin que les garanties d'isolation ne soient pas seulement
théoriques.

#### Acceptance Criteria

1. LE pipeline CI DOIT provisionner Postgres/PostGIS, appliquer les migrations, puis
   exécuter la suite de tests complète.
2. LORSQUE la CI s'exécute ALORS les 54 tests d'intégration base de données NE
   DOIVENT PAS être ignorés et DOIVENT réussir.

- _Requirements: NFR-1 ; P1, P2, P11_

### Requirement 8: [G-8] Résorption des avertissements de dépréciation

**Sévérité:** Faible (processus).

**Constat:** `starlette.testclient` + `httpx` déprécié, `HTTP_422_UNPROCESSABLE_ENTITY`
renommé. Sans impact fonctionnel aujourd'hui.

**User Story:** En tant que mainteneur, je veux une sortie de test sans avertissement
de dépréciation, afin de repérer facilement les régressions réelles.

#### Acceptance Criteria

1. LORS d'une montée de version FastAPI/Starlette/httpx LE projet DEVRAIT résorber
   ces avertissements pour conserver une sortie de test propre.

- _Requirements: NFR-13_

### Requirement 9: [G-9] Fenêtre de révocation JWT et rotation du refresh

**Sévérité:** Moyenne (décision ; ADR développé dans `design.md`).

**Constat:** un access token reste valide jusqu'à son expiration (défaut 12 h) après
désactivation de compte/tenant ; la purge client dépend du prochain pull réussi. Le
refresh reste valide jusqu'à son expiration (défaut 30 j) sans invalidation serveur —
« rotation » signifie ici « émission d'une nouvelle paire », pas « invalidation de
l'ancienne ».

**User Story:** En tant que responsable conformité, je veux que la fenêtre
d'exploitabilité d'un accès révoqué soit un choix explicite, afin d'aligner le risque
sur nos exigences PHI.

#### Acceptance Criteria

1. LE TTL de l'access token DOIT être ramené à 8 heures et celui du refresh à 14
   jours (durées couvrant une journée de travail hors ligne tout en réduisant la
   fenêtre d'exploitabilité d'un jeton volé).
2. LORSQU'un refresh déjà consommé est rejoué sur le chemin en ligne
   (`/api/auth/refresh`) ALORS le système DOIT le refuser et invalider sa famille
   `jti` (détection de rejeu/vol).
3. TANT QU'un poste travaille hors ligne IL NE DOIT PAS dépendre de la denylist de
   refresh : la révocation d'un acteur hors ligne reste bornée par le TTL d'access,
   contrainte explicitement assumée.

- _Requirements: NFR-7, NFR-2_
