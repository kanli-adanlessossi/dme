# Architecture — synthèse

Cette note résume l'architecture pour un nouvel arrivant. La conception complète,
faisant autorité, est dans
[`.kiro/specs/medical-centers-togo/design.md`](../.kiro/specs/medical-centers-togo/design.md).

## Principe directeur : local-first

La connectivité étant intermittente hors de Lomé, l'application de travail du
personnel ne dépend pas du réseau sur le chemin critique. Elle lit et écrit une
base locale (sur le poste), puis se synchronise avec le serveur en arrière-plan.

```
   POSTE (centre médical) — fonctionne hors ligne
   ┌───────────────────────────────────────────────┐
   │  PWA React/TS + service worker (app en cache)  │
   │  UI ──lit/écrit──> IndexedDB chiffré (RxDB)     │
   │                      + OUTBOX (mutations)       │
   └───────────────┬─────────────────────────────────┘
                   │ sync bidirectionnelle (si réseau)
                   │ push outbox + pull deltas
   ┌───────────────▼─────────────┐
   │  FastAPI (async)             │
   │  auth · contexte tenant ·    │
   │  audit · endpoints de sync   │
   └───────────────┬─────────────┘
                   │ SET LOCAL app.tenant_id (par requête)
   ┌───────────────▼─────────────┐
   │  PostgreSQL 16 + PostGIS     │  source de vérité consolidée
   │  Row-Level Security (RLS)    │
   └──────────────────────────────┘
```

## Les trois surfaces

1. **Carte publique (en ligne, cache)** — visiteurs grand public : carte MapLibre
   des 5 régions + annuaire des centres. Lecture seule, projection sûre (jamais de
   données patient). Ressources mises en cache par le service worker.
2. **Application de travail (local-first)** — personnel authentifié d'un tenant :
   patients, visites, profil du centre. Fonctionne hors ligne.
3. **Administration de plateforme** — opérateur transversal : intégration et
   cycle de vie des tenants, audité.

## Multi-tenant : isolation par RLS

- Modèle retenu : **schéma partagé + colonne `tenant_id` + Row-Level Security**.
- Chaque requête authentifiée définit `SET LOCAL app.tenant_id` dans sa
  transaction ; les politiques RLS restreignent les lignes à ce tenant.
- Le rôle applicatif est **non-superuser** avec `FORCE ROW LEVEL SECURITY` : même
  un oubli de filtre `WHERE tenant_id = …` ne peut pas franchir les tenants.
- Le `tenant_id` est toujours apposé par le serveur (jamais depuis le corps de la
  requête).

> Alternatives écartées pour la v1 : schéma-par-tenant et base-par-tenant
> (engorgement du catalogue, dérive des migrations). À réévaluer si un tenant
> exige une résidence physique des données.

## Synchronisation

- **Écriture locale** → IndexedDB + outbox, id = **UUIDv7 côté client** (pas de
  collision entre postes hors ligne).
- **Push** vers `POST /api/sync/push` : lot **idempotent** (clé = UUID de
  mutation), rejouable sans doublon après coupure.
- **Pull** depuis `GET /api/sync/pull?since=<curseur>` : deltas du tenant,
  cadrés RLS.
- **Conflits** : Last-Write-Wins sur `updated_at`/`version` avec journal
  (`sync_conflict`) ; les entités cliniquement sensibles sont **marquées pour
  revue humaine**, jamais écrasées silencieusement.
- Une bibliothèque éprouvée (**RxDB**) porte la réplication/reprise/curseurs ; la
  logique métier (RLS, audit, conflits) reste côté serveur.

## Données de santé : les garde-fous

- **TLS** partout ; mots de passe **Argon2id** ; **limitation de débit** à la
  connexion.
- **Audit append-only** distribué-compatible : les événements PHI hors ligne sont
  journalisés localement puis téléversés (horodatage client + réception serveur).
- **Suppression logique** des PHI (`deleted_at`) — jamais de suppression physique
  en usage normal.
- **Consentement** capturé par dossier patient.
- **Chiffrement local** des PHI au repos ; clé verrouillée à la déconnexion
  (menace : perte/vol du poste).
- **Auth tolérante à l'hors-ligne** : session locale, jeton à durée de vie
  adaptée, révocation appliquée à la synchronisation suivante.

## Propriétés de correction (invariants)

La conception définit des invariants vérifiables (P1–P13), p. ex. : isolation
inter-tenant (P1), tenancy autoritaire côté serveur (P2), pas de fuite d'existence
(P3), confidentialité des PHI (P4), complétude de l'audit (P5), disponibilité hors
ligne (P9), synchronisation idempotente (P10), isolation maintenue à la sync (P11),
résolution de conflits non destructive (P12), confidentialité locale (P13). La
stratégie de test existe pour les prouver.

## Pour aller plus loin

- Exigences : [`requirements.md`](../.kiro/specs/medical-centers-togo/requirements.md)
- Conception complète : [`design.md`](../.kiro/specs/medical-centers-togo/design.md)
- Plan d'implémentation : [`tasks.md`](../.kiro/specs/medical-centers-togo/tasks.md)
