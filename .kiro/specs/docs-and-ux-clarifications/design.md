# Design Document

_Spec : clarifications de documentation et d'UX — `docs-and-ux-clarifications`._

## Overview

Trois clarifications **à faible risque**, deux purement documentaires et une UX :

- **#6 (doc)** — note dans `docs/ARCHITECTURE.md` (section `## Synchronisation`) sur
  la contrainte mono-instance d'écriture du curseur de sync.
- **#3 (doc)** — enrichissement de `docs/ARCHITECTURE.md` (section `## Données de
  santé : les garde-fous`) sur la fenêtre de révocation hors-ligne de 8 h.
- **#4 (UX)** — signalement **a priori** dans `CenterAdmin.tsx` que l'édition du
  profil requiert le réseau, en réutilisant l'état `online` déjà observable.

Aucun changement d'architecture, de contrat d'API, ni de comportement en ligne.
#6/#3 ne touchent pas le code ; #4 est additif (le filet d'erreur a posteriori
existant est conservé).

### État vérifié du code (ancrages)

- `docs/ARCHITECTURE.md` possède déjà `## Synchronisation` (décrit curseur/push/pull)
  et `## Données de santé : les garde-fous` (contient « révocation appliquée à la
  synchronisation suivante »). Les deux notes s'y greffent naturellement.
- `backend/app/services/sync.py` : curseur `"<updated_at>|<entity_id>"`, tri
  `(updated_at, id)` ; `backend/app/core/rate_limit.py` documente déjà une limite
  « par instance » (précédent de ton pour une note multi-instance).
- `backend/app/core/config.py` : `jwt_access_ttl_seconds = 8h`, réglable via
  `JWT_ACCESS_TTL_SECONDS` ; révocation propagée par `core/deps.py` + `routers/sync.py`.
- `frontend/src/lib/sync.ts` : `SyncState.online` (booléen, maintenu par les
  événements `online`/`offline` + `navigator.onLine`), exposé via
  `getSyncEngine().state` (observable) et `.snapshot` (lecture synchrone).
- `frontend/src/pages/CenterAdmin.tsx` : s'abonne déjà à la **session** ; gère l'échec
  réseau **a posteriori** (`saveErrorKey` → `pages.centerAdmin.errors.offline`). Il n'y
  a PAS de signalement amont ni de désactivation du bouton hors-ligne.

## Architecture

```
#6  docs/ARCHITECTURE.md  §Synchronisation   → + encadré « contrainte mono-instance »
#3  docs/ARCHITECTURE.md  §Garde-fous santé   → + précision « fenêtre révocation 8 h »
#4  CenterAdmin.tsx  ─ s'abonne à SyncState.online (getSyncEngine().state)
        hors-ligne → bannière « page en ligne requise » + bouton Enregistrer désactivé
        en-ligne  → comportement inchangé
        (filet a posteriori errors.offline CONSERVÉ si la connexion tombe en cours)
```

## Decision Record (ADR-2) — #4 : source de l'état « en ligne »

**Contexte.** Le signalement #4 a besoin de savoir si le poste est en ligne. Deux
sources possibles : (a) `SyncState.online` du moteur de sync
(`getSyncEngine().state`), (b) la source **canonique du navigateur**
(`navigator.onLine` + événements `window` `online`/`offline`).

**Décision : (b) source navigateur directe, via un petit hook `useOnline()`.** Deux
raisons vérifiées dans le code :
1. **Fraîcheur.** `SyncState.online` n'est maintenu que si `SyncEngine.start()` a été
   appelé (c'est `start()` qui pose les écouteurs `online`/`offline`). Le moteur n'est
   démarré que dans une session déverrouillée (`wireSync()`). Lire
   `getSyncEngine().snapshot.online` hors de ce cas renvoie une **valeur initiale
   figée** → faux signal.
2. **Découplage.** Le profil de centre est précisément le sous-système qui **n'utilise
   PAS la sync** (ADR G-6, REST dédié). Faire dépendre son éditeur de `getSyncEngine()`
   relierait deux sous-systèmes délibérément séparés. `navigator.onLine` est toujours
   frais, sans dépendance, et trivial à piloter en test.

**Conséquence.** On ajoute un hook réutilisable `useOnline()` (lit `navigator.onLine`,
s'abonne aux événements `window` `online`/`offline`, se désabonne au démontage).
`CenterAdmin` l'utilise ; il ne touche jamais `getSyncEngine()`.

## Decision Record (ADR-1) — #4 : bouton désactivé vs actif hors-ligne

**Contexte.** Hors-ligne, le profil ne peut pas être enregistré (REST en ligne). Deux
options UX : (a) laisser le bouton actif + bannière d'avertissement, l'échec se
produisant au clic ; (b) **désactiver** le bouton hors-ligne + bannière explicative.

**Décision : (b) bouton désactivé + bannière.** Laisser un bouton actif qui échoue
systématiquement hors-ligne est un anti-pattern (l'utilisateur agit puis est puni).
Désactiver le bouton avec une explication claire (« connexion requise pour enregistrer
le profil ») est l'affordance honnête : l'état indisponible est visible **avant**
l'action. Le filet a posteriori (`errors.offline`) reste utile pour le cas où la
connexion tombe **pendant** l'édition (bouton encore actif à l'instant du clic, puis
`fetch` échoue) — on ne le retire pas.

**Cohérence local-first.** La bannière DOIT expliciter la distinction : patients et
visites fonctionnent hors-ligne (sync), le profil du centre non (endpoint en ligne).
Sans cette phrase, l'utilisateur pourrait croire à une incohérence de l'app.

## Components and Interfaces

### C1 — `docs/ARCHITECTURE.md` (#6, #3)

- **#6** : dans `## Synchronisation`, ajouter un paragraphe/encadré « Contrainte
  d'exploitation : instance d'écriture unique » — monotonie du curseur dépendant
  d'horodatages cohérents ; risque multi-instance (décalage d'horloge / même instant à
  la frontière de page → delta sauté) ; mitigation partielle (départage par `id`) ;
  condition de levée (horloge logique / curseur basé séquence, non implémenté).
  **État actuel (L2)** : affirmer explicitement que le `docker-compose.yml` déploie
  **une seule** instance d'API → la contrainte est **satisfaite aujourd'hui** ; la
  note est un garde-fou pour une mise à l'échelle **future**, pas un bug présent.
- **#3** : dans `## Données de santé : les garde-fous`, préciser la ligne révocation :
  délai borné par le TTL d'access (**8 h par défaut, configurable** — M2) pour un
  poste hors-ligne ; compromis assumé (NFR-7) ; cas limite (poste volé hors-ligne) ;
  levier `JWT_ACCESS_TTL_SECONDS` et son arbitrage. **Précision M2** : écrire
  systématiquement « 8 h par défaut, configurable » (ne pas figer « 8 h » comme une
  constante — une doc qui mentirait après un changement de TTL) ; et noter que le
  **refresh token (14 j) n'étend PAS** cette fenêtre pour un poste hors-ligne (le
  refresh exige le réseau), pour qu'un lecteur ne croie pas à une fenêtre de 14 j.

### C2 — `frontend/src/pages/CenterAdmin.tsx` (#4)

- Ajouter un hook `useOnline()` (nouveau, p. ex. `frontend/src/lib/useOnline.ts`) :
  lit `navigator.onLine`, s'abonne aux événements `window` `online`/`offline`, se
  désabonne au démontage (ADR-2). `CenterAdmin` l'utilise ; il NE touche PAS
  `getSyncEngine()`.
- Hors-ligne (`!online`) : afficher une **bannière** (`role="status"`, français,
  mobile-first) expliquant que l'édition du profil requiert une connexion et diffère
  des PHI local-first ; **désactiver** le bouton « Enregistrer » (ou son `fieldset`)
  avec un libellé/aide cohérent.
- **Cas « ouvert déjà hors-ligne » (L1)** : le `GET /api/center/profile` échoue alors
  et la page tombe dans l'état `error` (bouton Réessayer). Distinguer ce cas d'une
  vraie erreur serveur : lorsque `!online`, l'état d'erreur de chargement DOIT
  afficher le **message hors-ligne** (« connexion requise pour cette page »), pas le
  « erreur de chargement » générique. Ainsi la bannière #4 couvre tout le parcours
  (chargement ET édition), pas seulement l'édition.
- En ligne : aucun changement. Le mapping d'erreur `errors.offline` a posteriori est
  conservé.
- Ajouter les clés i18n nécessaires (p. ex. `pages.centerAdmin.offlineBanner`), en
  français, cohérentes avec les clés existantes.

## Data Models

Aucun modèle de données. Modifications : deux sections de `docs/ARCHITECTURE.md`, un
composant React existant, et des clés i18n.

## Correctness Properties

### Property 1: Contrainte mono-instance documentée (#6)

La documentation d'architecture énonce que la monotonie du curseur suppose une
instance d'écriture unique, décrit le risque multi-instance et la condition de levée.

**Validates: Requirements 1.1, 1.2, 1.3**

### Property 2: Fenêtre de révocation documentée (#3)

La documentation d'exploitation énonce que la révocation d'un poste hors-ligne est
bornée par le TTL d'access (8 h), comme compromis assumé, avec son levier de réglage.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

### Property 3: Signalement UX a priori hors-ligne (#4)

Hors-ligne, la page profil indique **avant** toute tentative que l'enregistrement
requiert une connexion et empêche/clarifie l'action, en distinguant le profil (en
ligne) des PHI (local-first) ; l'état en ligne est lu à la source navigateur.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

### Property 4: Non-régression du comportement en ligne (#4)

Le chargement, l'édition et l'enregistrement en ligne restent inchangés ; le filet
d'erreur a posteriori est conservé ; les tests existants restent verts.

**Validates: Requirements 3.6, 3.7, 4.1, 4.2**

## Error Handling

- **Détection hors-ligne imparfaite.** `navigator.onLine`/`SyncState.online` peut être
  « en ligne » alors que l'API est injoignable (réseau présent mais serveur down). Le
  signalement a priori est donc une **aide**, pas une garantie : le filet a posteriori
  (`errors.offline`) couvre ce cas (connexion apparente mais `fetch` échoue). Les deux
  couches sont complémentaires — c'est voulu.
- **Bascule online↔offline pendant l'édition.** La bannière et l'état du bouton
  réagissent à l'observable `online` ; si la connexion revient, le bouton se réactive
  sans rechargement. Si elle tombe juste avant un clic déjà parti, le filet a
  posteriori gère l'échec.
- **#6/#3 (doc)** : aucun chemin d'exécution, donc aucun cas d'erreur ; seule exigence
  de qualité = exactitude et cohérence avec le code référencé.

## Testing Strategy

1. **#4 (Vitest, M1)** : étendre `CenterAdmin.test.ts`. Technique de simulation
   hors-ligne (grâce à ADR-2, triviale et sans mock de sous-système) : piloter
   `navigator.onLine` (redéfinir la propriété via `Object.defineProperty`) puis
   `window.dispatchEvent(new Event('offline'))` / `'online'`. Le test existant mocke
   déjà `../lib/api` et `../lib/session` mais **pas** `../lib/sync` — avec `useOnline()`
   il n'y a **rien à mocker côté sync** (on ne touche pas le moteur). Vérifier :
   hors-ligne → bannière affichée + bouton « Enregistrer » désactivé ; retour en ligne
   → bouton réactivé, enregistrement inchangé ; chargement déjà hors-ligne → message
   hors-ligne (pas « erreur de chargement » générique, L1) ; le filet `errors.offline`
   reste couvert.
2. **Non-régression** : suite frontend complète verte (`npm run test -- --run`).
3. **#6/#3 (doc)** : relecture — exactitude factuelle (curseur, TTL 8 h, leviers) et
   cohérence avec le code (`sync.py`, `config.py`). Pas de test automatisé (doc).

## Hors périmètre (rappel)

- Rendre le curseur multi-instance (horloge logique) : #6 documente, n'implémente pas.
- Changer le TTL ou le mécanisme de révocation : #3 documente le comportement existant.
- Passer le profil dans la sync local-first : décision ADR G-6 inchangée ; #4 informe.
