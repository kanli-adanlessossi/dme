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

> **Contrainte d'exploitation : instance d'écriture unique.**
> Le curseur de pull est opaque, de la forme `"<updated_at>|<entity_id>"`, et la
> pagination des deltas trie par `(updated_at, id)`. Sa **monotonie** suppose des
> horodatages `updated_at` **cohérents** — garantis tant qu'un **seul nœud
> d'écriture** sérialise les mutations.
>
> - **Risque en multi-instance.** Si plusieurs processus écrivaient en parallèle,
>   un décalage d'horloge entre nœuds (ou deux écritures au même instant juste à la
>   frontière d'une page de deltas) pourrait placer une ligne « avant » le curseur
>   déjà rendu : le delta correspondant serait **potentiellement non tiré** au pull
>   suivant. Le départage par `id` (second critère de tri) **atténue partiellement**
>   le cas d'égalité stricte d'horodatage au sein d'une même instance, mais ne
>   corrige **pas** un décalage d'horloge entre instances.
> - **État actuel : contrainte satisfaite.** Le déploiement de référence
>   (`docker-compose.yml`) ne lance qu'**une seule** instance d'API (service `api`).
>   La contrainte est donc **respectée aujourd'hui** : cette note est un **garde-fou
>   pour une mise à l'échelle future**, pas un bug présent.
> - **Condition de levée.** Passer l'API en multi-instance d'écriture exigerait un
>   curseur indépendant de l'horloge murale — horloge logique (p. ex. Lamport) ou
>   curseur basé sur une **séquence** monotone partagée. **Non implémenté, hors
>   périmètre** actuel.
>
> Même logique d'exploitation que la limitation de débit applicative (SlowAPI, repli
> `backend/app/core/rate_limit.py`), dont le compteur est **par instance** : les deux
> composants supposent une instance unique et exigeraient un état partagé pour passer
> à l'échelle horizontalement.

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

> **Fenêtre de révocation hors-ligne (exploitation, NFR-7).** Quand un compte ou un
> tenant est désactivé côté serveur (`is_access_revoked` / `check_access_revoked`,
> `backend/app/core/deps.py`), la révocation est propagée au client **au plus tard à
> la prochaine synchronisation réussie** : `GET /api/sync/pull` renvoie alors
> `revoked=true` sans aucun delta (`backend/app/routers/sync.py`), ce qui déclenche la
> purge de la session et des PHI locales. Pour un poste **hors-ligne**, aucune sync ne
> peut avoir lieu : la fenêtre d'accès résiduel est donc bornée par la durée de vie de
> l'**access token** (`jwt_access_ttl_seconds`, **8 h par défaut, configurable** —
> `backend/app/core/config.py`). C'est un **compromis assumé** du modèle local-first
> (NFR-7), pas un défaut : un poste doit pouvoir travailler une journée entière sans
> réseau. Cas limite à connaître : un poste **perdu ou volé** qui reste hors-ligne
> demeure exploitable jusqu'à l'expiration de son access token (au plus 8 h par
> défaut). Le **chiffrement local** des PHI, dont la clé est verrouillée à la
> déconnexion, reste la protection de fond contre ce scénario.
>
> Levier de réglage : `JWT_ACCESS_TTL_SECONDS` (secondes). Le **raccourcir** réduit
> d'autant la fenêtre d'exposition hors-ligne, au prix de **ré-authentifications plus
> fréquentes** — moins adapté à une journée de travail hors-ligne ; l'**allonger**
> améliore l'autonomie mais élargit la fenêtre. Le **refresh token (14 j par défaut,
> `JWT_REFRESH_TTL_SECONDS`) n'étend PAS** cette fenêtre pour un poste hors-ligne : le
> renouvellement d'un access token via le refresh **exige le réseau** ; sans réseau,
> seul le TTL de l'access token courant s'applique.

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
