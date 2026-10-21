# Requirements Document

_Spec : clarifications de documentation et d'UX — `docs-and-ux-clarifications`._

## Introduction

Cette spec regroupe trois observations de la revue d'architecture qui partagent une
même nature : **rendre explicite une contrainte ou un comportement déjà vrai dans le
système**, pour qu'il ne soit ni une surprise pour l'exploitant, ni un angle mort
pour un futur mainteneur. Deux sont purement documentaires ; une est un petit
ajustement d'UX.

- **#6** — *Documenter la contrainte mono-instance d'écriture du curseur de sync.* Le
  curseur de pull (`"<updated_at>|<entity_id>"`) suppose une cohérence d'horloge qui
  tient sur un **nœud d'écriture unique** ; en multi-instance, un décalage d'horloge
  pourrait sauter un delta à la frontière de page. La contrainte existe **par
  construction** ; elle n'est documentée nulle part de visible.
- **#3** — *Rappeler à l'exploitant la fenêtre de révocation hors-ligne de 8 h.* Quand
  un compte/tenant est désactivé côté serveur, un poste déjà hors-ligne reste
  opérationnel jusqu'à la prochaine sync — au plus tard à l'expiration de l'access
  token (8 h). C'est un choix assumé (local-first) ; c'est une **propriété de
  sécurité** que l'exploitant doit connaître.
- **#4** — *Signaler côté UX que l'édition du profil de centre requiert le réseau.* Le
  profil de centre passe par un endpoint REST en ligne (pas par la sync local-first) ;
  son édition **ne fonctionne pas hors-ligne**, contrairement aux PHI. L'utilisateur
  doit en être informé **avant** d'éditer, pas seulement après un échec.

### État vérifié du code (base de cette spec)

1. **Curseur de sync (#6).** `backend/app/services/sync.py` encode un curseur opaque
   `"<updated_at ISO-8601>|<entity_id>"` et pagine par `(updated_at, id)`. La monotonie
   repose sur des horodatages `updated_at` cohérents. **Aucune** note de déploiement
   ne mentionne la contrainte « une seule instance d'écriture ». La limite SlowAPI
   multi-instance est déjà notée (`rate_limit.py`), ce qui confirme qu'une telle note
   a sa place dans la doc.
2. **Révocation 8 h (#3).** `core/deps.py` (`is_access_revoked`, `check_access_revoked`)
   et `routers/sync.py` (`revoked=true`) propagent la révocation à la prochaine sync ;
   la fenêtre est bornée par `jwt_access_ttl_seconds = 8h` (`config.py`, G-9). Documenté
   dans les docstrings du code, **pas** dans une note d'exploitation visible.
3. **UX profil (#4).** `frontend/src/pages/CenterAdmin.tsx` gère déjà l'échec réseau
   **a posteriori** : un `fetch` qui échoue mappe vers la clé i18n
   `pages.centerAdmin.errors.offline`, affichée APRÈS un clic « Enregistrer » raté.
   Ce qui **manque** est un signalement **a priori** : rien n'indique, avant l'édition,
   que cette page (contrairement aux PHI) requiert une connexion. Le bouton
   « Enregistrer » reste actif hors-ligne et laisse croire à une sauvegarde locale.

### Nature du travail

- #6 et #3 sont de la **documentation** (notes d'architecture/exploitation), sans
  changement de code applicatif.
- #4 est un **petit changement UX frontend** (affordance proactive), pas une refonte :
  la gestion d'erreur existante reste, on ajoute l'information en amont.

### Hors périmètre

- Rendre le curseur de sync réellement multi-instance (horloge logique, backend
  partagé) : #6 **documente** la contrainte, ne la lève pas.
- Raccourcir ou rendre configurable autrement la fenêtre de révocation : #3 la
  **documente**, ne la change pas (le TTL est déjà réglable via `JWT_ACCESS_TTL_SECONDS`).
- Rendre le profil de centre éditable hors-ligne (le passer dans la sync) : décision
  d'architecture déjà tranchée (ADR G-6, REST dédié) ; #4 **informe** l'utilisateur,
  ne change pas l'architecture.

## Glossary

- **Curseur de sync** : marqueur opaque `"<updated_at>|<entity_id>"` indiquant la
  position du dernier delta tiré, pour une pagination monotone.
- **Mono-instance d'écriture** : hypothèse qu'un seul processus serveur écrit les
  entités synchronisables, garantissant des horodatages `updated_at` cohérents.
- **Fenêtre de révocation** : délai maximal entre la désactivation serveur d'un
  compte/tenant et la perte d'accès effective d'un poste hors-ligne (bornée par le
  TTL de l'access token, 8 h).
- **A priori / a posteriori (UX)** : information donnée **avant** l'action (préventive)
  vs **après** un échec (réactive).

## Non-functional Requirements rattachées

- **NFR-6** — Synchronisation : pagination monotone du delta-sync (#6).
- **NFR-7** — Authentification tolérante à l'hors-ligne : révocation au plus tard à la
  prochaine sync (#3).
- **NFR-5** — Local-first : l'UI indique clairement l'état de connectivité ; le profil
  en ligne est une exception à signaler (#4).

## Requirements

### Requirement 1: Documenter la contrainte mono-instance du curseur de sync (#6)

**User Story:** En tant qu'exploitant ou futur mainteneur, je veux que la contrainte
« une seule instance d'écriture » du curseur de sync soit documentée de façon visible,
afin de ne pas déployer l'API en multi-instance d'écriture sans mesurer le risque de
sauter des deltas.

#### Acceptance Criteria

1. LA documentation d'architecture (p. ex. `docs/ARCHITECTURE.md` et/ou une note près
   du code du curseur) DOIT énoncer que la monotonie du curseur
   `"<updated_at>|<entity_id>"` suppose des horodatages `updated_at` cohérents, garantis
   par une **instance d'écriture unique**.
2. LA documentation DOIT décrire le **risque** en multi-instance (décalage d'horloge /
   deux écritures à la même résolution temporelle à la frontière de page → delta
   potentiellement non tiré) et la mitigation partielle existante (départage par `id`).
   LA note DOIT affirmer que le déploiement **actuel** (`docker-compose.yml`) est
   **mono-instance**, donc la contrainte est satisfaite aujourd'hui : c'est un
   garde-fou pour une mise à l'échelle future, pas un bug présent.
3. LA documentation DOIT indiquer la **condition de levée** (ce qu'exigerait un
   déploiement multi-instance : horloge logique / curseur basé séquence), sans
   l'implémenter (hors périmètre).
4. LA note DOIT être cohérente avec la limite multi-instance déjà documentée pour
   SlowAPI (`rate_limit.py`), pour un discours d'exploitation homogène.

_Requirements: observation #6 ; NFR-6_

### Requirement 2: Documenter la fenêtre de révocation hors-ligne de 8 h (#3)

**User Story:** En tant qu'exploitant, je veux connaître le délai maximal entre la
désactivation d'un compte/tenant et la perte d'accès effective d'un poste hors-ligne,
afin d'évaluer correctement le risque (p. ex. poste volé restant hors-ligne).

#### Acceptance Criteria

1. LA documentation de sécurité/exploitation DOIT énoncer que la révocation d'un
   compte/tenant est propagée **au plus tard à la prochaine synchronisation réussie**,
   et que pour un poste **hors-ligne** la fenêtre est bornée par le **TTL de l'access
   token (8 h par défaut)**.
2. LA documentation DOIT présenter ce délai comme un **compromis assumé** du modèle
   local-first (NFR-7), pas comme un défaut, et nommer le cas limite (poste perdu/volé
   restant hors-ligne reste exploitable jusqu'à l'expiration du jeton).
3. LA documentation DOIT indiquer le **levier de réglage** (`JWT_ACCESS_TTL_SECONDS`)
   et son arbitrage (plus court = fenêtre réduite mais ré-authentifications plus
   fréquentes, moins adapté à une journée de travail hors-ligne). LA valeur DOIT être
   formulée « **8 h par défaut, configurable** » (jamais « 8 h » figé), et la note
   DOIT préciser que le **refresh token (14 j) n'étend PAS** cette fenêtre pour un
   poste hors-ligne (le refresh exige le réseau).
4. CETTE note DOIT figurer dans un emplacement d'exploitation visible (p. ex.
   `docs/ARCHITECTURE.md` section sécurité et/ou `README` sécurité), pas uniquement dans
   une docstring de code.

_Requirements: observation #3 ; NFR-7_

### Requirement 3: Signaler côté UX que l'édition du profil requiert le réseau (#4)

**User Story:** En tant que tenant_admin travaillant dans un centre à connectivité
intermittente, je veux savoir **avant** d'éditer le profil du centre que cette page
requiert une connexion, afin de ne pas croire que mes modifications sont sauvegardées
localement comme les patients et visites.

#### Acceptance Criteria

1. LORSQUE la page d'édition du profil est affichée **hors-ligne** ALORS l'UI DOIT
   indiquer **a priori** (avant toute tentative d'enregistrement) que cette page
   requiert une connexion et que ses modifications ne sont pas sauvegardées localement.
   CECI couvre aussi le cas « **ouverte déjà hors-ligne** » : le chargement du profil
   échoue alors, et l'UI DOIT afficher le message hors-ligne plutôt qu'une « erreur de
   chargement » générique (distinguer absence de réseau d'une erreur serveur).
2. L'état « en ligne » DOIT être lu à la **source navigateur** (`navigator.onLine` +
   événements `online`/`offline`), PAS via le moteur de sync (dont l'état n'est frais
   que s'il a été démarré, et dont le profil ne dépend pas — ADR G-6).
3. LORSQUE le poste est hors-ligne ALORS l'action d'enregistrement DOIT être
   **empêchée ou clairement signalée comme indisponible** (p. ex. bouton désactivé avec
   explication), plutôt que de laisser l'utilisateur tenter puis échouer.
4. LA distinction avec les PHI DOIT être explicite : patients/visites fonctionnent
   hors-ligne (sync), le profil **non** (REST en ligne, décision ADR G-6).
5. LE message DOIT être en français (NFR-10), accessible (`role` approprié) et
   mobile-first (NFR-9), cohérent avec les messages existants de la page.
6. LA gestion d'erreur **a posteriori** existante (`errors.offline` après un échec de
   `fetch`) DOIT être conservée comme filet de sécurité (p. ex. si la connexion tombe
   pendant l'édition) ; #4 ajoute le signalement amont, ne le remplace pas.
7. LE comportement en ligne (chargement, édition, enregistrement) NE DOIT PAS être
   modifié ; seul le cas hors-ligne gagne une information proactive.

_Requirements: observation #4 ; NFR-5, NFR-9, NFR-10_

### Requirement 4: Non-régression (#4)

**User Story:** En tant que mainteneur, je veux que l'ajout d'UX du profil
n'introduise aucune régression, afin que l'édition en ligne et les tests existants
restent intacts.

#### Acceptance Criteria

1. APRÈS le changement, les tests existants de la page profil (`CenterAdmin.test.ts`)
   DOIVENT rester verts ; le nouveau comportement hors-ligne DOIT être couvert par un
   test (état hors-ligne simulé → information affichée / enregistrement empêché).
2. LA suite frontend complète (`npm run test -- --run`) DOIT rester verte.
3. LES changements #6 et #3 étant documentaires, ils N'introduisent aucun risque de
   régression de code ; leur seule exigence de qualité est l'exactitude et la
   visibilité.

_Requirements: observation #4 ; NFR-5_

## Assumptions

- #6 et #3 sont de la **documentation** : leur valeur est l'exactitude et la
  visibilité, pas un changement de comportement.
- #4 est un **petit ajout UX** s'appuyant sur l'état de connectivité déjà exposé (la
  session/sync connaissent `online`) ; aucune nouvelle source d'état n'est requise.
- La détection hors-ligne côté client (p. ex. `navigator.onLine` / l'état de sync
  déjà observable) est suffisamment fiable pour un signalement UX (ce n'est pas une
  garantie de sécurité, juste une aide).
- L'architecture reste inchangée : le profil demeure en REST en ligne (ADR G-6), le
  curseur reste mono-instance, le TTL reste à 8 h.
