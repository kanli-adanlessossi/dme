# Implementation Plan

## Overview

Objet : droits et cycle de vie des données par tenant — observations #2
(export/effacement) et #8 (lookup setup-token) de la revue d'architecture.

Plan conçu selon `design.md`, satisfaisant `requirements.md`. Deux axes
indépendants côté serveur (`backend/`) :

- **#2** — nouvelles opérations transversales `platform_admin` : export (lecture
  cadrée, sans secret) et effacement (destructif, audité, append-only préservé).
  ADR-1 tranché : **conserver l'audit**, effacer tables PHI/tenant ; tenant à
  désactiver d'abord.
- **#8** — lookup setup-token en coût constant : schéma **selector + verifier**
  (ADR-2), additif et compatible contrat.

Principes d'ordonnancement :
1. **#8 en premier, car petit et à faible risque** (migration additive + réécriture
   locale d'une fonction + tests). Il valide la mécanique de migration de colonne
   avant le gros morceau #2.
2. **#2 : export avant effacement.** L'effacement réutilise l'export (sauvegarde
   avant suppression) et est l'unique opération destructive — livré en dernier, avec
   les garde-fous (désactivation préalable, audit avant acte, append-only préservé).
3. **Tests d'intégration PostGIS obligatoires** pour l'isolation RLS et l'audit (ces
   garanties ne sont pas vérifiables en mode unitaire).

Chaque tâche indique sa Definition of Done (DoD). Pile de test : pytest
(unitaire + intégration `TEST_DATABASE_URL`).

## Tasks

- [x] 1. [#8] Migration : colonne `setup_token_selector`
  - [x] 1.1 Créer une migration Alembic ajoutant `app_user.setup_token_selector text`
    nullable, avec un **index UNIQUE partiel** (`WHERE setup_token_selector IS NOT
    NULL`). Accorder les privilèges cohérents avec les autres colonnes (hérités du
    rôle `dme_app`). Suivre le style des migrations existantes (docstring, révision).
  - DoD : `alembic upgrade head` applique la colonne + l'index ; `downgrade` la
    retire ; migration testée en intégration.
  - _Requirements: 3 (critère 4) ; NFR-2_

- [x] 2. [#8] Lookup setup-token selector + verifier
  - [x] 2.1 Dans `backend/app/core/security.py` : helper générant un token
    `"<selector>.<verifier>"` (selector et verifier aléatoires, `token_urlsafe`) et
    exposant le selector en clair + l'empreinte Argon2id du verifier. Conserver
    `hash_setup_token`/`verify_setup_token` pour le verifier.
  - [x] 2.2 Dans `backend/app/services/auth.py` : réécrire `consume_setup_token` —
    découper `token` sur le premier `.` ; `SELECT ... WHERE setup_token_selector =
    :selector` (index) ; vérifier expiration + `is_active` + Argon2 du verifier (**une
    seule** vérification) ; succès → fixer `password_hash` et consommer (effacer
    selector + hash + expiration). Selector inconnu → **Argon2 factice** puis
    `SetupTokenError` générique (anti-timing, P3). Format invalide → même erreur.
  - [x] 2.3 Dans `backend/app/services/admin.py` : `create_tenant_onboarding` émet
    `"<selector>.<verifier>"` et stocke `setup_token_selector` + `setup_token_hash`.
  - DoD : la consommation effectue **au plus un** Argon2 (instrumenté/vérifié) ;
    usage unique, expiration, 410 générique et anti-timing inchangés ; contrat API
    (`setup_token: str`) inchangé.
  - _Requirements: 3 (critères 1, 2, 3, 5) ; NFR-2_

- [x] 3. [#8] Tests du nouveau lookup
  - [x] 3.1 Tests (intégration) : consommation réussie à coût constant ; usage unique
    (second appel rejeté) ; expiration ; selector inconnu → 410 générique ; token mal
    formé → 410 ; un token **hérité sans selector** est rejeté proprement. Prouver
    qu'une seule vérification Argon2 a lieu (compteur/mock).
  - DoD : tous verts ; non-fuite d'existence préservée.
  - _Requirements: 3 (critères 2, 5), 4 (critère 3) ; NFR-2_

- [x] 4. [#2] Service d'export de tenant
  - [x] 4.1 Créer `backend/app/services/tenant_data.py` avec `export_tenant(session,
    tenant_id) -> TenantExport`. Poser `set_tenant_context(session, tenant_id)` (chemin
    admin cadré) ; lire chaque entité cadrée (`medical_center`, services, `app_user`,
    `patient`, `visit`, `sync_conflict`, `sync_mutation`) + le `tenant` dans **une
    seule transaction en lecture cohérente** (snapshot : toutes les entités sur le même
    instantané, pas d'export partiel sous écriture concurrente) ; projeter en dict
    **sans aucun secret** (exclure `password_hash`, `setup_token_hash`,
    `setup_token_selector`, jetons refresh). Lève `TenantNotFoundError`. Limite de
    volumétrie (tout-en-mémoire) documentée ; streaming NDJSON noté comme évolution
    future, non implémenté ici.
  - [x] 4.2 Schéma Pydantic `TenantExport` documenté et stable (JSON structuré par
    entité), pour permettre un import/restitution futur.
  - DoD : export complet, cadré au tenant, sans secret ; forme documentée.
  - _Requirements: 1 (critères 1, 2, 3, 5) ; NFR-4, NFR-2_

- [x] 5. [#2] Endpoint d'export (admin, audité)
  - [x] 5.1 Dans `backend/app/routers/admin.py` : `POST /api/admin/tenants/{id}/export`,
    `require_roles(platform_admin)`, renvoie le `TenantExport`. Auditer dans la **même
    transaction** (`record_audit`, action `tenant.export`, acteur + tenant cible).
    Tenant inconnu → 404.
  - DoD : endpoint réservé platform_admin ; export audité ; 404 si inconnu.
  - _Requirements: 1 (critères 1, 4) ; NFR-3_

- [x] 6. [#2] Service d'effacement de tenant (ADR-1)
  - [x] 6.1 Dans `tenant_data.py` : `erase_tenant(session, tenant_id) -> EraseReport`.
    **Exiger `tenant.status == 'inactive'`** sinon `TenantStillActiveError` (→ 409).
    Mécanisme : **DELETE explicites cadrés** (ADR design, voie (ii)), PAS un cascade
    `DELETE FROM tenant` seul. Poser `set_tenant_context(session, tenant_id)` pour que
    la RLS `USING` borne chaque DELETE au tenant cible (un bug de ciblage ne touche
    aucun autre tenant, P1/P2). Supprimer dans l'**ordre imposé par les FK RESTRICT**
    `created_by`/`patient_id` : (1) `visit`, (2) `patient`, (3)
    `medical_center_service`, (4) `sync_conflict`, (5) `sync_mutation`, (6)
    `refresh_token` des comptes du tenant, (7) `app_user` du tenant, (8) `medical_center`,
    puis (9) `DELETE FROM tenant` (table non cadrée RLS). **Ne JAMAIS toucher
    `audit_log`** (il n'a pas de FK vers tenant → hors cascade par construction ;
    aucune action requise). Lève `TenantNotFoundError` (404).
  - [x] 6.2 Renvoyer un `EraseReport` (compte par table, `audit_preserved=true`,
    `exported_first`).
  - DoD : effacement cadré au tenant (aucune donnée d'un autre tenant touchée) ;
    audit intact ; 409 si tenant actif ; 404 si inconnu.
  - _Requirements: 2 (critères 1, 2, 3, 4, 7) ; NFR-4, NFR-1_

- [x] 7. [#2] Endpoint d'effacement (admin, audité AVANT l'acte)
  - [x] 7.1 Dans `routers/admin.py` : `POST /api/admin/tenants/{id}/erase`,
    `require_roles(platform_admin)`. Écrire l'audit d'intention d'effacement
    (`tenant.erase`) dans une transaction **séparée committée d'abord** (la trace
    survit même si l'effacement échoue), puis exécuter `erase_tenant` dans sa propre
    transaction. Mapper `TenantStillActiveError` → 409, `TenantNotFoundError` → 404.
  - [x] 7.2 Export préalable **optionnel** (décision tranchée) : l'endpoint
    d'effacement N'exige PAS d'export préalable et NE le génère pas. Garde-fou
    obligatoire en revanche : refuser (409) si `tenant.status != 'inactive'`. Le champ
    `exported_first` de l'`EraseReport` reflète simplement si un export a eu lieu
    avant (information, pas condition).
  - DoD : effacement réservé platform_admin ; audit d'intention écrit avant ;
    garde-fous 409/404 ; append-only jamais violé.
  - _Requirements: 2 (critères 1, 2, 5, 6, 7) ; NFR-3, NFR-4_

- [x] 8. [#2] Tests d'intégration export + effacement
  - [x] 8.1 Deux tenants peuplés (PostGIS réel) : l'export de l'un ne contient que ses
    données, aucun secret, audit écrit. Effacement d'un tenant **inactif** supprime ses
    PHI + identité, **laisse `audit_log` intact** (y compris la ligne d'effacement), ne
    touche pas l'autre tenant. Effacement d'un tenant **actif** → 409. Tenant inconnu →
    404. Vérifier qu'aucune ligne d'audit n'est supprimable par le rôle applicatif.
  - DoD : tous verts en mode intégration ; isolation et append-only prouvés.
  - _Requirements: 2 (tous), 4 (critères 1, 2, 4) ; NFR-1, NFR-3, NFR-4_

- [x] 9. [#2, #8] Non-régression finale
  - [x] 9.1 Suite backend complète verte (unitaire + intégration PostGIS), ruff
    propre, aucune dépréciation. Confirmer que l'onboarding, l'auth, l'isolation RLS et
    l'audit append-only existants restent intacts.
  - DoD : suite complète verte ; aucun comportement existant régressé.
  - _Requirements: 4 (tous) ; NFR-1, NFR-2, NFR-3, NFR-4_

## Task Dependency Graph

```
1 (migration selector) ──► 2 (lookup selector+verifier) ──► 3 (tests #8)
4 (service export) ──► 5 (endpoint export) ──► 6 (service effacement) ──► 7 (endpoint effacement) ──► 8 (tests #2)
3, 8 ──► 9 (non-régression finale)
```

#8 (tâches 1–3) et #2 (tâches 4–8) sont deux chaînes indépendantes, parallélisables.
L'effacement (6–7) suit l'export (4–5) car il le réutilise (sauvegarde avant
suppression). Chemin critique : **4 → 5 → 6 → 7 → 8 → 9**.

```json
{
  "tasks": [
    { "id": "1", "obs": "#8",      "dependsOn": [],              "wave": 1, "severity": "low" },
    { "id": "4", "obs": "#2",      "dependsOn": [],              "wave": 1, "severity": "medium" },
    { "id": "2", "obs": "#8",      "dependsOn": ["1"],           "wave": 2, "severity": "medium" },
    { "id": "5", "obs": "#2",      "dependsOn": ["4"],           "wave": 2, "severity": "medium" },
    { "id": "3", "obs": "#8",      "dependsOn": ["2"],           "wave": 3, "severity": "low" },
    { "id": "6", "obs": "#2",      "dependsOn": ["5"],           "wave": 3, "severity": "high" },
    { "id": "7", "obs": "#2",      "dependsOn": ["6"],           "wave": 4, "severity": "high" },
    { "id": "8", "obs": "#2",      "dependsOn": ["7"],           "wave": 5, "severity": "medium" },
    { "id": "9", "obs": "#2, #8",  "dependsOn": ["3", "8"],      "wave": 6, "severity": "medium" }
  ],
  "waves": [
    { "wave": 1, "tasks": ["1", "4"] },
    { "wave": 2, "tasks": ["2", "5"] },
    { "wave": 3, "tasks": ["3", "6"] },
    { "wave": 4, "tasks": ["7"] },
    { "wave": 5, "tasks": ["8"] },
    { "wave": 6, "tasks": ["9"] }
  ],
  "criticalPath": ["4", "5", "6", "7", "8", "9"]
}
```

## Notes

- ADR-1 (conserver l'audit) et ADR-2 (selector + verifier) sont **tranchés** dans
  `design.md` ; les tâches en appliquent le choix.
- Contrainte absolue : aucune tâche ne doit permettre la suppression/mutation de
  `audit_log` par le rôle applicatif (le trigger append-only lèverait de toute façon ;
  les tests le prouvent).
- L'effacement exige une désactivation préalable du tenant (409 sinon) : garde-fou
  anti-erreur, pas une contrainte fonctionnelle arbitraire.
- Décisions tranchées : **désactivation préalable obligatoire** (409 sinon) ; **export
  préalable optionnel** (l'effacement ne l'exige ni ne le génère). Parcours sûr
  recommandé à l'exploitant : désactiver → exporter si souhaité → effacer.
