# Design Document

_Spec : droits et cycle de vie des données par tenant — `tenant-data-rights`._

## Overview

Trois livrables serveur (`backend/`), deux axes :

- **#2 — Export + effacement par tenant** (exigences 1, 2). Deux opérations
  transversales, réservées au `platform_admin`, auditées, ajoutées au routeur
  `admin` (qui porte déjà l'onboarding et le cycle de vie). Elles réutilisent le
  **chemin admin explicitement cadré** déjà employé par `services/admin.py`
  (`set_tenant_context` sur le tenant cible pour satisfaire la RLS forcée).
- **#8 — Lookup setup-token en coût constant** (exigence 3). Passage du schéma
  « O(n) × Argon2 » à un schéma **selector + verifier** : un sélecteur non secret
  indexé localise la ligne, une seule vérification Argon2id valide le verifier.

Les trois sont **additifs** : aucune route existante ni comportement d'auth/isolation
n'est modifié. L'effacement est la seule opération destructive — traitée avec un ADR
et des garde-fous stricts (audit préservé, écrit avant l'acte, cadrage tenant).

### État vérifié du code (ancrages)

- **Tables cadrées au tenant** (portent `tenant_id`, sous RLS forcée sauf mention) :
  `medical_center`, `medical_center_service`, `app_user` (tenant_id nullable —
  platform_admin NUL), `patient`, `visit`, `sync_mutation`, `sync_conflict`.
  `refresh_token` est lié au tenant indirectement via `user_id → app_user`.
  `tenant` est l'identité (non cadrée RLS). `region`/`service_type` sont des
  **références partagées** (hors périmètre export/effacement). `audit_log` est
  **append-only** (trigger `audit_log_block_mutation`, migration 0004).
- **Chemin admin cadré** : `services/admin.py` pose déjà `set_tenant_context(session,
  tenant.id)` avant d'écrire dans `medical_center`/`app_user` ; même technique
  réutilisée ici pour lire/effacer sous RLS.
- **Setup-token** : `app_user.setup_token_hash` + `setup_token_expires_at` ;
  `consume_setup_token` charge tous les candidats non consommés et fait Argon2 par
  candidat. Le contrat API `SetCredentialsRequest.setup_token: str` est une **chaîne
  opaque** — un format `selector.verifier` y tient sans changer le type du champ.

## Architecture

```
POST /api/admin/tenants/{id}/export   ─┐   platform_admin only (require_roles)
POST /api/admin/tenants/{id}/erase    ─┤   + audit (record_audit, même transaction)
                                       │
                 services/tenant_data.py (nouveau)
                   ├─ export_tenant(session, tenant_id) ──► lecture cadrée (set_tenant_context)
                   │     → projection sûre par entité (jamais de secret)
                   └─ erase_tenant(session, tenant_id, policy) ──► audit AVANT, puis
                         suppression cadrée des tables PHI/tenant ; audit_log CONSERVÉ

POST /api/auth/set-credentials  ──► consume_setup_token(selector.verifier)
                 services/auth.py (modifié)
                   └─ split "selector.verifier" → SELECT par selector indexé
                       → 1 seule vérification Argon2 du verifier
```

## Decision Record (ADR-1) — Politique d'effacement et traitement de l'audit

**Contexte.** L'effacement par tenant doit supprimer les PHI et les données du
tenant, mais `audit_log` est append-only (NFR-3) et ne contient **pas** de PHI
(acteur, action, entité, entity_id, horodatages, IP).

**Fait structurel vérifié (décisif).** `audit_log.tenant_id` est une colonne
**nullable SANS clé étrangère** vers `tenant` (vérifié dans `models/audit_log.py` :
aucun `ForeignKey`). Conséquence directe : supprimer un `tenant` ne déclenche
**aucun** cascade vers `audit_log` — l'audit est **hors du graphe de cascade par
construction**. Conserver l'audit ne demande donc **aucune action** ; le trigger
append-only n'est même jamais sollicité par l'effacement. À l'inverse, toucher
l'audit exigerait un effort **délibéré** (requête ciblée via un rôle privilégié).

**Options.**
- (a) **Conserver l'audit** (comportement par défaut du schéma, voir ci-dessus).
- (b) **Anonymiser des champs d'audit** (p. ex. `ip`) par un chemin privilégié
  (superuser/propriétaire, hors `dme_app`), contournant délibérément l'append-only.

**Décision : (a) conserver l'audit.** Ce n'est pas seulement le bon choix, c'est la
**trajectoire naturelle du schéma** : l'audit n'ayant pas de FK vers `tenant`, il
survit sans intervention. Justification de fond : l'audit ne contient pas de PHI,
NFR-3 **exige** sa pérennité, et la trace « tenant X effacé par Y le Z » est
précisément ce qu'un audit doit garder. (b) introduirait un chemin à privilèges
élevés capable de muter l'append-only — risque disproportionné pour retirer une IP
non-PHI. Une anonymisation réglementaire future fera l'objet d'une spec dédiée avec
son propre chemin contrôlé.

**Portée de l'effacement (tables) — fondée sur les FK réelles.** Vérifié dans les
modèles : toutes les tables cadrées portent `tenant_id → tenant ON DELETE CASCADE`
(`medical_center`, `patient`, `visit`, `medical_center_service`, `sync_mutation`,
`sync_conflict`, `app_user`), et `refresh_token → app_user ON DELETE CASCADE`. **Mais**
plusieurs FK `ON DELETE RESTRICT` **bloqueraient** un cascade naïf :
- `patient.created_by → app_user` **RESTRICT**
- `visit.created_by → app_user` **RESTRICT**
- `visit.patient_id → patient` **RESTRICT**
- `medical_center_service.service_type_id → service_type` **RESTRICT** (référence
  partagée, jamais effacée — non bloquant tant qu'on supprime la ligne de liaison,
  pas le `service_type`).

Le RESTRICT **déterminant** est `created_by` : supprimer un `app_user` échoue tant
qu'un `patient` ou une `visit` le référence. **L'ordre d'effacement n'est donc pas
cosmétique, il est imposé par ces RESTRICT.** Ordre correct (enfants portant un
`created_by`/`patient_id` d'abord) :

1. `visit` (référence `patient` et `app_user` en RESTRICT)
2. `patient` (référence `app_user` en RESTRICT)
3. `medical_center_service` (liaison ; CASCADE depuis `medical_center`, mais on la
   retire explicitement pour rester déterministe)
4. `sync_conflict`, `sync_mutation`
5. `refresh_token` des comptes du tenant (CASCADE depuis `app_user`, retiré
   explicitement)
6. `app_user` du tenant (plus aucun RESTRICT `created_by` ne pointe vers lui)
7. `tenant`

- **Conservées** : `audit_log` (sans FK vers tenant, hors cascade), `region` /
  `service_type` (références partagées).
- **Pré-requis** : l'effacement DOIT écrire la ligne d'audit **avant** la suppression
  (l'événement survit) et, par défaut, être **précédé d'un export** (sauvegarde
  avant suppression, décision ci-dessous).

**Mécanisme d'effacement vis-à-vis de la RLS (M2 — tranché).** Deux voies
techniques existent, aux propriétés de sécurité différentes :
- **(i) `DELETE FROM tenant` + cascade** : en PostgreSQL, les actions référentielles
  de FK (CASCADE) s'exécutent avec les droits du **propriétaire de la table**, donc
  **en contournant la RLS**. Simple, mais l'effacement ne passe alors PAS par le
  chemin cadré : une erreur de ciblage ne serait pas rattrapée par la RLS, et le
  cascade buterait de toute façon sur les RESTRICT `created_by` ci-dessus.
- **(ii) DELETE explicites cadrés** (RETENU) : poser `set_tenant_context(session,
  tenant_id)` puis exécuter les `DELETE` des tables enfants **dans l'ordre imposé par
  les RESTRICT** (liste ci-dessus) sous le rôle applicatif `dme_app` (NOBYPASSRLS).
  La politique RLS `USING (tenant_id = app.tenant_id)` garantit alors qu'on ne peut
  supprimer QUE les lignes du tenant cadré — un bug de ciblage ne touche aucun autre
  tenant (P1/P2). Le `DELETE FROM tenant` final porte sur une table non cadrée RLS
  (OK sous `dme_app` qui a le GRANT DELETE, vérifié migration 0002).

**Décision : voie (ii), DELETE explicites cadrés.** Pour un système à RLS forcée,
s'appuyer sur un cascade exécuté « en tant que propriétaire » (hors RLS) retirerait
précisément le garde-fou qui empêche un effacement de déborder sur un autre tenant.
La voie (ii) rend l'effacement **testable sous RLS** (un test prouve qu'aucune ligne
d'un autre tenant n'est touchée) et déterministe (ordre explicite). Le coût (quelques
DELETE au lieu d'un seul) est négligeable.

**Réversibilité et garde-fous (décisions tranchées).** L'effacement « dur » est
**définitif** (distinct du soft-delete). Garde-fou **obligatoire** : `erase_tenant`
exige que le tenant soit d'abord **désactivé** (`status='inactive'`, via
`set_tenant_status`) — un effacement ne peut pas frapper un tenant actif par mégarde,
et comme la désactivation coupe déjà tout accès/écriture (membres désactivés), aucune
écriture concurrente ne survient pendant l'opération. L'**export préalable n'est PAS
obligatoire** (décision : optionnel, laissé à la main de l'exploitant) : l'export et
l'effacement restent deux opérations découplées ; l'exploitant exporte avant s'il le
souhaite, mais le serveur ne l'impose pas.

**Rétention médicale (contrainte à nommer, L2).** « Effacement définitif des PHI »
peut entrer en conflit avec des obligations de **rétention** du dossier médical
(conservation X années selon la juridiction). Cette spec ne tranche pas une durée
légale (hors de son périmètre et dépendant du pays) : elle **expose** la tension et
laisse l'exploitant décider quand un effacement est licite. L'effacement est un
**outil** (retrait de tenant, demande explicite), pas une purge automatique ; sa
gouvernance (quand l'exécuter) relève de la politique de l'exploitant.

## Decision Record (ADR-2) — Lookup setup-token : selector + verifier

**Contexte.** `consume_setup_token` fait Argon2 × N candidats. Le token en clair
n'est pas indexable (empreinte salée). On veut un lookup à coût constant sans
stocker le secret en clair ni affaiblir P3.

**Décision : schéma « selector + verifier ».** Le token émis devient
`"<selector>.<verifier>"` où :
- `selector` : identifiant **non secret**, aléatoire d'**au moins 16 octets**
  (`secrets.token_urlsafe(16)`), stocké **en clair et indexé**
  (`app_user.setup_token_selector`, UNIQUE). 16 octets d'entropie rendent une
  collision sur l'index UNIQUE négligeable. Sert au `SELECT` O(1).
- `verifier` : secret aléatoire d'au moins 32 octets (`token_urlsafe(32)`), dont
  seule l'empreinte **Argon2id** est stockée (`setup_token_hash`, inchangé
  sémantiquement). Vérifié **une seule fois**, sur la ligne trouvée par le selector.
- **Format / séparateur** : `token_urlsafe` produit l'alphabet base64url
  (`A–Z a–z 0–9 - _`), qui **ne contient jamais de `.`** ; le `.` est donc un
  séparateur non ambigu. Découpage sur le **premier** `.` : `selector = avant`,
  `verifier = tout le reste` (robuste même si un `.` apparaissait côté verifier).
  Un token sans `.`, ou avec selector/verifier vide, est un format invalide → échec
  générique 410 (P3).

Flux `consume_setup_token(token)` :
1. découper `token` sur le premier `.` → `(selector, verifier)` ; format invalide →
   échec générique 410 (P3) ;
2. `SELECT ... WHERE setup_token_selector = :selector` (index) → au plus une ligne ;
3. vérifier expiration + `is_active` + `verify_setup_token(verifier, hash)` (1 Argon2) ;
4. tout échec → **même** `SetupTokenError` générique (P3) ; succès → fixer
   `password_hash`, consommer (effacer `selector`/`hash`/`expires_at`).

**Non-fuite d'existence (P3) préservée.** Un selector inconnu et un verifier faux
donnent la **même** erreur. La mitigation du canal temporel est conservée : si aucune
ligne n'est trouvée, exécuter une vérification Argon2 **factice** (comme
`authenticate_user`) pour niveler le temps de réponse, afin qu'un selector inexistant
ne soit pas distinguable par le temps.

**Compatibilité.** Le champ API reste `setup_token: str` (chaîne opaque) : pas de
changement de contrat. Migration : nouvelle colonne `setup_token_selector` (nullable,
UNIQUE partiel). Les setup-tokens **déjà émis** (sans selector) ne sont plus
consommables par le nouveau chemin → l'onboarding les ré-émet (acceptable : un
setup-token est à usage unique et à durée de vie courte ; invalidation documentée).

## Components and Interfaces

### C1 — `services/tenant_data.py` (nouveau)

- `export_tenant(session, tenant_id) -> TenantExport` : pose `set_tenant_context`,
  lit chaque entité cadrée, projette en dict **sans secrets** (exclut
  `password_hash`, `setup_token_hash`, `setup_token_selector`, jetons refresh).
  Lève `TenantNotFoundError` (→ 404).
  - **Cohérence transactionnelle (M3).** L'export s'exécute dans **une seule
    transaction en lecture** (snapshot cohérent PostgreSQL `REPEATABLE READ` ou la
    transaction unique de `tenant_session`/chemin admin) : toutes les entités sont
    lues sur le même instantané, de sorte qu'une écriture concurrente ne produise pas
    un export partiellement incohérent (p. ex. une visite référençant un patient
    absent de l'export). Pour un système santé, un export qui rate des lignes sous
    charge serait un risque de conformité.
  - **Volumétrie (M3).** La v1 charge l'export en mémoire (simple, suffisant pour la
    taille d'un tenant réaliste). Limite **nommée** : pour un très gros tenant, cette
    approche peut être lourde ; l'évolution vers un export **en flux** (streaming
    NDJSON par entité) est notée comme amélioration future, sans l'implémenter ici.
    Recommandation d'exiger la **désactivation préalable** du tenant pour l'export
    précédant un effacement (pas d'écriture concurrente), tout en autorisant l'export
    d'un tenant actif pour l'archivage courant.
- `erase_tenant(session, tenant_id) -> EraseReport` : exige `status='inactive'` ;
  supprime les tables dans l'ordre FK sous contexte cadré ; ne touche jamais
  `audit_log`. Lève `TenantNotFoundError` (404) et `TenantStillActiveError` (409).

### C2 — Routeur `admin` (étendu)

- `POST /api/admin/tenants/{id}/export` et `.../erase`, tous deux
  `require_roles(platform_admin)`, audités dans la **même transaction** (fail-closed,
  NFR-3) via `record_audit` — l'audit d'effacement écrit **avant** la suppression.

### C3 — `services/auth.py` (modifié) + migration

- `consume_setup_token` réécrit en selector+verifier (voir ADR-2).
- `services/admin.create_tenant_onboarding` émet désormais `"<selector>.<verifier>"`
  et stocke `setup_token_selector` + `setup_token_hash`.
- Migration : `ALTER TABLE app_user ADD COLUMN setup_token_selector text` + index
  UNIQUE partiel (`WHERE setup_token_selector IS NOT NULL`).

## Data Models

```
app_user (ajout)
  setup_token_selector text null   -- non secret, indexé UNIQUE (partiel) ; #8

TenantExport (projection, aucun secret)
  tenant: {...}  center: {...}  users: [{email, role, is_active}]  (jamais de hash)
  patients: [...]  visits: [...]  services: [...]
  sync: { conflicts: [...], mutations: [...] }

EraseReport
  tenant_id, erased_counts: {table: n}, audit_preserved: true, exported_first: bool
```

## Correctness Properties

### Property 1: Export sans secret et cadré tenant (#2)

L'export d'un tenant ne contient aucun secret (`password_hash`, `setup_token_hash`,
`setup_token_selector`, refresh) et ne porte que les données de ce tenant.

**Validates: Requirements 1.1, 1.2, 1.3**

### Property 2: Effacement auditée et préservant l'audit (#2)

Un effacement écrit sa ligne d'audit **avant** la suppression, ne supprime ni ne mute
aucune ligne d'`audit_log`, et est réservé au `platform_admin`.

**Validates: Requirements 2.2, 2.3, 2.5**

### Property 3: Effacement cadré au tenant (#2)

Un effacement ne touche aucune donnée d'un autre tenant ; l'isolation est garantie
par le contexte RLS cadré et/ou le chemin admin explicite.

**Validates: Requirements 2.4**

### Property 4: Lookup setup-token à coût constant (#8)

La consommation d'un setup-token effectue au plus **une** vérification Argon2id,
indépendamment du nombre de comptes candidats (selector indexé).

**Validates: Requirements 3.1, 3.3**

### Property 5: Sécurité du setup-token préservée (#8)

Usage unique, expiration et non-fuite d'existence (410 générique, mitigation du canal
temporel) restent vrais après le passage au selector+verifier.

**Validates: Requirements 3.2, 3.5**

### Property 6: Intégrité append-only de l'audit (#2, #8)

Aucune opération (export, effacement, onboarding) ne permet au rôle applicatif de
supprimer ou muter une ligne d'`audit_log`.

**Validates: Requirements 2.1, 4.4**

## Error Handling

- **Effacement d'un tenant actif** → `TenantStillActiveError` → **409** : on exige une
  désactivation préalable (garde-fou anti-erreur). Pas d'effacement d'un tenant vivant.
- **Tenant inexistant** (export ou effacement) → `TenantNotFoundError` → **404**, sans
  effet de bord.
- **Atomicité de l'effacement** : toute la suppression + l'audit dans **une seule
  transaction** ; une erreur en cours annule tout (sauf l'audit déjà écrit — qui, par
  conception, doit rester même en cas d'échec partiel : voir note ci-dessous).
  Décision : écrire l'audit d'intention d'effacement dans une transaction **séparée et
  committée d'abord**, puis l'effacement dans sa propre transaction — ainsi la trace
  « tentative d'effacement » survit même si l'effacement échoue.
- **Setup-token mal formé** (pas de `.`, selector/verifier vides) → `SetupTokenError`
  générique → 410 (P3), jamais de cause distinctive.
- **Selector inconnu** → vérification Argon2 factice puis 410 générique (anti-timing).

## Testing Strategy

1. **Export** (intégration PostGIS) : deux tenants peuplés ; l'export de l'un ne
   contient que ses données, aucun secret ; audit écrit.
2. **Effacement** (intégration) : effacement d'un tenant inactif supprime ses PHI et
   son identité, **laisse l'audit intact** (y compris la ligne d'effacement), ne
   touche pas l'autre tenant ; tentative sur un tenant actif → 409 ; tenant inconnu →
   404.
3. **Append-only** : vérifier qu'aucune des opérations ne supprime de ligne d'audit
   (le trigger lèverait de toute façon ; test le prouve).
4. **Setup-token selector+verifier** : un seul Argon2 par consommation (instrumenté) ;
   usage unique, expiration, 410 générique, anti-timing, tous couverts ; un token
   hérité sans selector est rejeté proprement.
5. **Non-régression** : suite backend complète verte (unitaire + intégration), ruff
   propre, aucune dépréciation.

## Hors périmètre (rappel)

- Workflow RGPD patient individuel (self-service, délais légaux) — non traité.
- Anonymisation de champs d'audit (ADR-1 rejette ; spec future si exigé).
- Import/restitution d'un export (le format est stable et documenté pour le rendre
  possible, mais l'import n'est pas implémenté ici).
