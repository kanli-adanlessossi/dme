# Requirements Document

_Spec : droits et cycle de vie des données par tenant — `tenant-data-rights`._

## Introduction

Cette spec traite deux observations de la revue d'architecture, toutes deux liées
au **cycle de vie des données cadrées au tenant** et à la **scalabilité de
l'onboarding** côté serveur (`backend/`) :

- **#2** — *Planifier l'export et l'effacement des données par tenant.* NFR-4 exige
  que « l'architecture DOIT permettre l'export et l'effacement des données par
  tenant comme opération future (le schéma DOIT porter les champs nécessaires) ».
  Le schéma le permet déjà ; l'**exécution** n'existe pas. Pour un système de santé
  (PHI), le droit à l'export/effacement finit par devenir une exigence opérationnelle
  ou réglementaire : il faut le concevoir avant d'en avoir un besoin urgent.
- **#8** — *Repenser le lookup du jeton de configuration (setup-token) si l'onboarding
  monte en volume.* Le lookup actuel est en O(n) avec une vérification Argon2id par
  compte candidat — négligeable à petite échelle, coûteux (CPU) et surface DoS si de
  nombreux onboardings coexistent.

### État vérifié du code (base de cette spec)

1. **Schéma prêt pour #2, exécution absente.** Toutes les tables cadrées portent
   `tenant_id` (`medical_center`, `app_user`, `patient`, `visit`,
   `medical_center_service`, `sync_mutation`, `sync_conflict`, `refresh_token` via
   `user_id`). `tenant.status ∈ {active, inactive}` porte la désactivation logique
   (NFR-4) ; `app_user.tenant_id` a `ON DELETE CASCADE` vers `tenant`. La
   désactivation d'un tenant (`set_tenant_status`) est **logique** (aucune
   suppression). **Aucun** service ni endpoint d'export ou d'effacement n'existe
   aujourd'hui.
2. **Contrainte dure sur l'effacement : l'audit est append-only.** `audit_log` est
   protégé par un trigger `audit_log_block_mutation` qui lève sur tout UPDATE/DELETE
   (migration 0004), et le rôle applicatif n'a que INSERT/SELECT. Un effacement
   « physique total » d'un tenant **ne peut pas** supprimer ses lignes d'audit sans
   contourner cette garantie — ce qui est précisément interdit. L'effacement doit
   donc traiter l'audit à part (conservation, ou anonymisation encadrée), pas comme
   une table ordinaire.
3. **Lookup setup-token (#8) — O(n) Argon2.** `services/auth.consume_setup_token`
   charge tous les `app_user` dont `setup_token_hash` est non NUL, puis exécute
   `verify_setup_token` (Argon2id) contre chaque candidat jusqu'à correspondance. Le
   token en clair n'est pas indexable (empreinte Argon2id salée). `app_user.email`
   est unique et indexé, mais n'est pas présenté au `set-credentials`.

### Tension / contrainte centrale (à trancher dans le design)

L'**effacement des PHI** d'un tenant entre en tension avec deux garanties
existantes du système :

- **Audit append-only (NFR-3)** : les lignes d'audit ne peuvent être ni modifiées
  ni supprimées. Un effacement RGPD-like doit donc soit conserver l'audit
  (justifié : l'audit ne contient pas de PHI, seulement acteur/action/horodatage),
  soit anonymiser des champs ciblés par un chemin privilégié dédié — jamais via le
  rôle applicatif.
- **Suppression logique par défaut (NFR-4)** : le CRUD normal ne supprime jamais
  physiquement. L'effacement « dur » par tenant est une **opération d'exception,
  transversale, auditée et réservée au `platform_admin`**, distincte du soft-delete.

Cette spec cadre ces opérations ; le design DOIT produire un ADR tranchant la
politique d'effacement (conservation vs anonymisation de l'audit, portée exacte des
tables effacées, réversibilité).

### Hors périmètre

- L'automatisation complète d'un workflow RGPD (demandes des patients individuels,
  délais légaux, portail self-service) : cette spec vise l'**opération par tenant**
  au niveau plateforme, pas le droit individuel du patient.
- La refonte de l'authentification ou du flux d'onboarding au-delà du lookup
  setup-token (#8).
- Toute suppression affectant l'intégrité append-only de l'audit (interdite).

## Glossary

- **Export par tenant** : extraction de l'ensemble des données d'un tenant
  (profil, comptes, PHI) dans un format portable, pour archivage ou transfert.
- **Effacement par tenant** : suppression contrôlée des données d'un tenant, en
  respectant les contraintes d'audit (append-only) et de traçabilité.
- **Setup-token** : jeton de configuration à usage unique et expirant, remis au
  `tenant_admin` initial pour fixer son mot de passe (exigence 3.2). Stocké haché.
- **O(n)** : coût proportionnel au nombre de comptes candidats — ici, autant de
  vérifications Argon2id que de comptes avec un setup-token actif.

## Non-functional Requirements rattachées

- **NFR-3** — Auditabilité : piste append-only, jamais masquée ni mutée.
- **NFR-4** — Cycle de vie des données : soft-delete par défaut ; conservation à la
  désactivation ; **schéma permettant l'export/effacement par tenant**.
- **NFR-2** — Protection des données : pas de secret exposé ; opérations sensibles
  réservées et auditées.

## Requirements

### Requirement 1: Export des données d'un tenant (#2)

**User Story:** En tant qu'administrateur de plateforme, je veux exporter
l'intégralité des données d'un tenant dans un format portable, afin de permettre
l'archivage, le transfert ou la restitution avant un retrait de tenant.

#### Acceptance Criteria

1. LE système DOIT fournir une opération transversale, réservée au `platform_admin`
   et **auditée** (NFR-3), produisant un export complet des données d'un tenant :
   profil du centre, comptes `app_user` (sans secrets : jamais `password_hash` ni
   `setup_token_hash`), patients et visites (PHI), services, et conflits/mutations
   de sync pertinents.
2. L'export NE DOIT contenir AUCUN secret : empreintes de mots de passe, empreintes
   de setup-token et jetons de refresh DOIVENT être exclus.
3. L'export DOIT être cadré strictement au tenant demandé (aucune fuite
   inter-tenant) ; le design DOIT préciser comment le contexte RLS est posé pour une
   lecture transversale par le `platform_admin`.
4. LORSQU'un export est produit ALORS une ligne d'audit DOIT être écrite (acteur,
   tenant cible, horodatage).
5. LE format d'export DOIT être documenté et stable (p. ex. JSON structuré par
   entité), de sorte qu'un import/restitution futur soit possible.

_Requirements: observation #2 ; NFR-4, NFR-3, NFR-2_

### Requirement 2: Effacement des données d'un tenant (#2)

**User Story:** En tant qu'administrateur de plateforme, je veux effacer les données
d'un tenant de façon contrôlée et auditée, afin de répondre à un retrait définitif
tout en respectant les contraintes d'audit et de traçabilité.

#### Acceptance Criteria

1. LE design DOIT produire un **ADR** définissant la politique d'effacement : quelles
   tables sont effacées physiquement, lesquelles sont conservées, et le traitement de
   l'audit (append-only — voir critère 3).
2. L'effacement DOIT être une opération transversale réservée au `platform_admin`,
   **auditée**, et **distincte du soft-delete** du CRUD normal (NFR-4) : c'est une
   opération d'exception, pas un chemin ordinaire.
3. L'effacement NE DOIT PAS violer l'intégrité append-only de `audit_log` : les
   lignes d'audit NE DOIVENT être ni supprimées ni mutées par le rôle applicatif.
   L'ADR DOIT choisir entre (a) conserver l'audit tel quel (justifié : il ne contient
   pas de PHI) ou (b) une anonymisation ciblée par un chemin privilégié dédié — jamais
   via `dme_app`.
4. L'effacement DOIT être cadré strictement au tenant cible (aucune donnée d'un autre
   tenant touchée) ; le `WITH CHECK`/`USING` RLS et/ou le chemin admin explicite
   DOIVENT garantir l'isolation (P1/P2).
5. LORSQU'un effacement est exécuté ALORS une ligne d'audit DOIT être écrite AVANT
   l'effacement effectif (l'événement survit à l'opération), avec acteur, tenant
   cible, portée et horodatage.
6. L'effacement DOIT exiger que le tenant soit **préalablement désactivé**
   (`status='inactive'`) — sinon il est refusé (409). Cette double action délibérée
   (désactiver, puis effacer) est un garde-fou anti-accident et garantit qu'aucune
   écriture concurrente ne survient pendant l'opération. **Décision tranchée :** un
   **export préalable n'est PAS obligatoire** (optionnel, à la main de l'exploitant) ;
   l'effacement est **définitif** (distinct du soft-delete). L'ADR documente la
   portée exacte (tables effacées vs conservées).
7. LORSQU'un tenant n'existe pas ALORS l'opération DOIT échouer proprement (404) sans
   effet de bord.

_Requirements: observation #2 ; NFR-4, NFR-3, NFR-2_

### Requirement 3: Lookup efficace du jeton de configuration (#8)

**User Story:** En tant que responsable de la plateforme, je veux que la
consommation d'un setup-token reste performante quel que soit le nombre
d'onboardings en cours, afin d'éviter un coût CPU et une surface DoS proportionnels
au nombre de comptes.

#### Acceptance Criteria

1. LE design DOIT proposer un mécanisme de lookup du setup-token en coût **sensiblement
   constant** (indépendant du nombre de comptes candidats), SANS stocker le token en
   clair ni affaiblir la non-fuite d'existence (P3).
2. LE mécanisme retenu DOIT préserver l'usage unique, l'expiration, et la non-révélation
   de l'état du compte (même erreur générique 400/410, P3) — comportements actuels de
   `consume_setup_token`.
3. SI la solution introduit un **identifiant de lookup non secret** (p. ex. un
   préfixe/sélecteur transporté à côté du secret, modèle « selector + verifier »)
   ALORS ce sélecteur DOIT être indexé, et seul le **verifier** reste vérifié par
   Argon2id — une seule vérification Argon2, pas une par candidat.
4. LE changement DOIT être **additif et rétrocompatible** OU s'accompagner d'une
   migration documentée des setup-tokens en cours (les tokens déjà émis doivent
   rester consommables, ou leur invalidation doit être assumée et documentée).
5. LES comportements de sécurité existants (usage unique, expiration, 410 générique,
   mitigation du canal temporel) DOIVENT rester couverts par des tests après le
   changement.

_Requirements: observation #8 ; NFR-2_

### Requirement 4: Non-régression et sûreté des opérations (#2, #8)

**User Story:** En tant que mainteneur, je veux que ces opérations sensibles
n'affaiblissent aucune garantie existante, afin que l'isolation, l'audit et
l'onboarding restent corrects.

#### Acceptance Criteria

1. APRÈS les changements, la suite backend complète DOIT rester verte (mode unitaire
   et mode intégration PostGIS), y compris les tests d'isolation RLS, d'audit
   append-only et d'onboarding.
2. LES nouvelles opérations transversales (export, effacement) DOIVENT être couvertes
   par des tests d'intégration vérifiant le cadrage au tenant (aucune fuite
   inter-tenant) et l'écriture d'audit.
3. LE lookup setup-token modifié DOIT être couvert par des tests prouvant l'usage
   unique, l'expiration et le 410 générique inchangés.
4. AUCUNE opération NE DOIT permettre, même indirectement, la suppression ou la
   mutation de lignes d'`audit_log` par le rôle applicatif.

_Requirements: observations #2, #8 ; NFR-1, NFR-3, NFR-4, NFR-2_

## Assumptions

- #2 est un **lot de conception + implémentation serveur** (nouveaux endpoints admin
  transversaux), pas une simple documentation : le schéma est prêt, l'exécution ne
  l'est pas.
- La politique d'effacement de l'audit penche par défaut vers la **conservation**
  (l'audit ne contient pas de PHI, seulement acteur/action/entité/horodatage/IP),
  mais l'ADR tranche formellement.
- #8 est une optimisation dont le besoin dépend du volume ; la solution « selector +
  verifier » est l'option de référence, à confirmer dans le design. Un résultat
  acceptable est « conserver l'O(n) + documenter le seuil de volume à partir duquel
  optimiser », si le design démontre que le volume d'onboarding réel reste faible.
- Aucune des deux opérations ne doit toucher l'intégrité append-only de l'audit.
