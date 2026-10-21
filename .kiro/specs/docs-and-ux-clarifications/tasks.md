# Implementation Plan

## Overview

Objet : clarifications de documentation et d'UX — observations #6 (contrainte
mono-instance du curseur), #3 (fenêtre de révocation 8 h) et #4 (UX profil requiert
réseau) de la revue d'architecture.

Plan conçu selon `design.md`, satisfaisant `requirements.md`. Trois axes
**indépendants** et à faible risque :

- **#6, #3** — deux notes dans `docs/ARCHITECTURE.md` (sections existantes), sans
  changement de code.
- **#4** — petit ajout UX dans `CenterAdmin.tsx` (signalement a priori hors-ligne +
  bouton Enregistrer désactivé, ADR-1), en réutilisant `SyncState.online` déjà
  observable. Le filet d'erreur a posteriori existant est conservé.

Principes d'ordonnancement :
1. **Doc d'abord (#6, #3)** : risque nul, aucune dépendance ; peut être livré
   immédiatement et en parallèle.
2. **#4 ensuite** : seul axe avec du code + un test ; non régressif (comportement en
   ligne inchangé).

Chaque tâche indique sa Definition of Done (DoD). Pile de test : Vitest pour #4 ;
relecture pour la doc.

## Tasks

- [x] 1. [#6] Documenter la contrainte mono-instance du curseur de sync
  - [x] 1.1 Dans `docs/ARCHITECTURE.md`, section `## Synchronisation`, ajouter un
    encadré « Contrainte d'exploitation : instance d'écriture unique » : la monotonie
    du curseur `"<updated_at>|<entity_id>"` suppose des horodatages `updated_at`
    cohérents (garantis par un seul nœud d'écriture) ; décrire le risque multi-instance
    (décalage d'horloge / deux écritures au même instant à la frontière de page → delta
    potentiellement non tiré) et la mitigation partielle (départage par `id`) ;
    indiquer la condition de levée (horloge logique / curseur basé séquence, non
    implémenté). Cohérent avec la limite « par instance » déjà documentée pour SlowAPI.
  - DoD : note présente, factuellement exacte (alignée sur `services/sync.py`), dans
    la section sync ; discours d'exploitation homogène avec la note SlowAPI.
  - _Requirements: 1 (critères 1, 2, 3, 4) ; NFR-6_

- [x] 2. [#3] Documenter la fenêtre de révocation hors-ligne de 8 h
  - [x] 2.1 Dans `docs/ARCHITECTURE.md`, section `## Données de santé : les
    garde-fous`, enrichir la ligne révocation : préciser que la révocation d'un
    compte/tenant est propagée **au plus tard à la prochaine synchronisation réussie**,
    et que pour un poste **hors-ligne** la fenêtre est bornée par le **TTL de l'access
    token (8 h par défaut)** ; présenter ce délai comme un **compromis assumé** du
    local-first (NFR-7), nommer le cas limite (poste perdu/volé restant hors-ligne) ;
    indiquer le levier `JWT_ACCESS_TTL_SECONDS` et son arbitrage.
  - DoD : note présente et exacte (alignée sur `config.py` / `core/deps.py` /
    `routers/sync.py`), dans un emplacement d'exploitation visible.
  - _Requirements: 2 (critères 1, 2, 3, 4) ; NFR-7_

- [x] 3. [#4] Signalement UX a priori hors-ligne dans l'éditeur de profil
  - [x] 3.1 Créer un hook `useOnline()` (`frontend/src/lib/useOnline.ts`) : lit
    `navigator.onLine`, s'abonne aux événements `window` `online`/`offline`, se
    désabonne au démontage (ADR-2). `CenterAdmin` l'utilise pour `online` ; il NE touche
    PAS `getSyncEngine()` (découplage + fraîcheur — H1 de la revue).
  - [x] 3.2 Hors-ligne (`!online`) : afficher une **bannière** (`role="status"`,
    français, mobile-first ≥44px) indiquant que l'édition du profil requiert une
    connexion et **diffère des PHI** (patients/visites fonctionnent hors-ligne, pas le
    profil — ADR G-6) ; **désactiver** le bouton « Enregistrer » (ou le `fieldset`)
    avec un libellé/aide cohérent (ADR-1 : bouton désactivé, pas actif-qui-échoue).
  - [x] 3.3 **Cas « ouvert déjà hors-ligne » (L1)** : quand `!online` et que le
    `GET /api/center/profile` a échoué (état `error`), afficher le **message
    hors-ligne** (« connexion requise pour cette page ») plutôt que l'« erreur de
    chargement » générique — distinguer absence de réseau d'une erreur serveur.
  - [x] 3.4 En ligne : comportement **inchangé** (chargement, édition, enregistrement).
    **Conserver** le mapping d'erreur a posteriori `pages.centerAdmin.errors.offline`
    (filet si la connexion tombe pendant l'édition).
  - [x] 3.5 Ajouter les clés i18n nécessaires (p. ex. `pages.centerAdmin.offlineBanner`)
    en français, cohérentes avec les clés existantes de la page.
  - DoD : hors-ligne, la bannière s'affiche et l'enregistrement est empêché avant toute
    tentative ; en ligne, aucun changement ; distinction profil/PHI explicite.
  - _Requirements: 3 (critères 1, 2, 3, 4, 5, 6) ; NFR-5, NFR-9, NFR-10_

- [x] 4. [#4] Tests et non-régression
  - [x] 4.1 Étendre `frontend/src/pages/CenterAdmin.test.ts`. Technique (M1, grâce à
    `useOnline()` — rien à mocker côté sync) : piloter `navigator.onLine`
    (`Object.defineProperty`) + `window.dispatchEvent(new Event('offline'|'online'))`.
    Vérifier : hors-ligne → bannière présente ET bouton « Enregistrer » désactivé ;
    chargement déjà hors-ligne → message hors-ligne (pas « erreur de chargement »
    générique, L1) ; retour en ligne → bouton réactivé et enregistrement fonctionnel ;
    filet a posteriori (`errors.offline`) toujours couvert. Le test existant mocke déjà
    `../lib/api` et `../lib/session` ; `../lib/sync` n'a PAS besoin d'être mocké.
  - [x] 4.2 Exécuter la suite frontend complète (`npm run test -- --run`) → verte, y
    compris les tests existants de la page profil.
  - DoD : nouveau cas hors-ligne couvert ; suite frontend verte ; aucun comportement
    en ligne régressé.
  - _Requirements: 4 (critères 1, 2) ; NFR-5_

## Task Dependency Graph

```
1 (doc #6)  indépendant
2 (doc #3)  indépendant
3 (UX #4)  ──► 4 (tests #4)
```

Les notes de doc (1, 2) sont totalement indépendantes et parallélisables ; l'axe UX
(3 → 4) est une courte chaîne. Aucun chemin critique long : la plus grande tâche est
3 (UX). Chemin critique : **3 → 4**.

```json
{
  "tasks": [
    { "id": "1", "obs": "#6", "dependsOn": [],    "wave": 1, "severity": "low" },
    { "id": "2", "obs": "#3", "dependsOn": [],    "wave": 1, "severity": "low" },
    { "id": "3", "obs": "#4", "dependsOn": [],    "wave": 1, "severity": "medium" },
    { "id": "4", "obs": "#4", "dependsOn": ["3"], "wave": 2, "severity": "low" }
  ],
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "3"] },
    { "wave": 2, "tasks": ["4"] }
  ],
  "criticalPath": ["3", "4"]
}
```

## Notes

- ADR-1 (#4 : bouton désactivé hors-ligne, pas actif-qui-échoue) est **tranché** dans
  `design.md` ; la tâche 3 l'applique.
- #6 et #3 sont documentaires : leur seule exigence de qualité est l'exactitude
  factuelle et la visibilité (pas de test automatisé).
- Aucun changement d'architecture : le curseur reste mono-instance, le TTL reste à
  8 h, le profil reste en REST en ligne (ADR G-6). Ces trois points sont **documentés
  ou signalés**, pas modifiés.
- Le signalement hors-ligne est une **aide UX**, pas une garantie : le filet d'erreur
  a posteriori (`errors.offline`) reste la couche de sécurité si l'API est injoignable
  malgré un `navigator.onLine` vrai.
