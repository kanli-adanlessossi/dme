# Implementation Plan

## Overview

Objet : durcissement de la cryptographie locale (client) — observations #9 et #10
de la revue d'architecture.

Plan conçu selon `design.md`, satisfaisant `requirements.md`. Deux axes
indépendants : **KDF (#10)** — confirmation + verrouillage anti-régression, sans
changement de comportement ; **jetons (#9)** — ADR tranché (conserver `localStorage`,
durcir l'anti-XSS à la source), avec au moins une mitigation concrète livrée et
testée.

Principes d'ordonnancement :
1. **Additif, non régressif d'abord.** Les axes KDF (tâches 1–2) et anti-sink
   (tâche 3) ne changent aucun comportement runtime : à faire en premier, risque
   nul. La suite frontend existante reste verte à chaque étape.
2. **Changement CSP en dernier et fail-safe.** Le resserrement de `style-src`
   (tâche 4) est le seul changement runtime potentiellement cassant : il passe par
   `Report-Only` avant enforcing, et se replie sur `'unsafe-inline'` documenté si un
   style légitime (MapLibre) est bloqué.
3. **Traçabilité directe** (observation ↔ exigence ↔ conception ↔ tâche ↔ test).

Chaque tâche indique sa Definition of Done (DoD) et référence les exigences
associées. Pile de test : Vitest (`npm run test -- --run`).

## Tasks

- [x] 1. [#10] Exposer les constantes du KDF comme point de vérité unique
  - [x] 1.1 Dans `frontend/src/lib/session.ts`, promouvoir en **exports nommés** les
    constantes du KDF (`PBKDF2_ITERATIONS`, longueur de sel, `DERIVED_KEY_BITS`,
    algorithme/hash), avec un commentaire indiquant qu'elles sont sous contrat de
    test (ne pas abaisser sans mettre à jour le test de verrouillage). Ne changer
    AUCUNE valeur : confirmation, pas correction (les valeurs actuelles — 210 000 /
    16 octets / 256 bits / PBKDF2-SHA256 — sont conformes). DoD : constantes
    importables depuis les tests ; comportement runtime inchangé ; suite frontend
    verte.
  - _Requirements: 1 (critères 1, 2) ; NFR-8, P13_

- [x] 2. [#10] Test de verrouillage anti-régression du KDF
  - [x] 2.1 Créer `frontend/src/lib/crypto-params.test.ts` (Vitest) qui échoue si un
    paramètre régresse sous les seuils : itérations < 210 000, sel < 16 octets,
    clé < 32 octets (256 bits), algorithme ≠ PBKDF2-SHA256. Le test lit les
    constantes exportées (tâche 1), sans recopier de littéral.
  - [x] 2.2 Ajouter au même fichier : (a) deux dérivations avec des emails distincts
    produisent des **sels distincts** (sel par utilisateur, exigence 1.5) ; (b) après
    un `login` simulé (mocks réseau + `fake-indexeddb`), **aucune** clé `localStorage`
    ne contient la clé dérivée ni le mot de passe (exigence 1.6, P-KDF3). S'inspirer
    des mocks de `sync.idempotent.test.ts` / `db.*.test.ts`.
  - DoD : `crypto-params.test.ts` vert ; un abaissement volontaire d'un paramètre le
    fait échouer (vérifié localement puis rétabli) ; aucune régression ailleurs.
  - _Requirements: 1 (critères 3, 5, 6) ; NFR-8, NFR-2, P13_

- [x] 3. [#9] Test de verrouillage « zéro sink JS dangereux »
  - [x] 3.1 Créer `frontend/src/lib/no-dangerous-sinks.test.ts` (Vitest) qui scanne
    les sources `frontend/src/**` et échoue si un sink dangereux réapparaît :
    `innerHTML`, `outerHTML`, `dangerouslySetInnerHTML`, `eval(`,
    `insertAdjacentHTML`, `document.write`. **Exclure les commentaires et les chaînes
    de caractères** (sinon le commentaire « Styles inline » de `TogoMap.tsx` et les
    motifs du test lui-même sont de faux positifs), pas seulement les fichiers de
    test. Verrouille la propriété « 0 sink » constatée aujourd'hui (défense de fond
    anti-XSS, le rendu React auto-échappant).
  - [x] 3.2 Documenter dans l'en-tête du test ses **limites** : il couvre le code du
    projet, PAS les dépendances tierces (un sink introduit par une bibliothèque n'est
    pas détecté) ; il réduit la surface sans la supprimer. Ne pas le présenter comme
    une garantie absolue (cohérent avec la limite XSS actif, exigence 2.7).
  - DoD : test vert sur l'état actuel (0 sink) ; l'ajout d'un sink le fait échouer
    (vérifié puis retiré) ; limites documentées. C'est la **mitigation concrète
    livrée et testée** exigée par le critère 2.6 — la spec ne se clôt pas sur le seul
    ADR.
  - _Requirements: 2 (critère 6) ; NFR-2, NFR-8_

- [x] 4. [#9] Durcissement de `style-src` dans la CSP (fail-safe, Report-Only d'abord)
  - [x] 4.1 Évaluer le retrait de `'unsafe-inline'` de `style-src` : inventorier les
    styles inline réellement émis (Tailwind via classes = sûr ; chercher les
    `style={{…}}` dynamiques et les styles injectés par MapLibre). Décider si
    `style-src 'self'` (± nonce/hash) est atteignable sans casser le rendu.
  - [x] 4.2 Étape fail-safe : d'abord émettre la CSP resserrée en
    `Content-Security-Policy-Report-Only` (le `report-to`/`Reporting-Endpoints`
    existe déjà dans `frontend/nginx.conf`), vérifier l'absence de violation sur les
    parcours clés (carte MapLibre, pages tenant, login). Ne basculer en enforcing
    (`Content-Security-Policy`) que si aucun style légitime n'est bloqué.
  - [x] 4.3 Appliquer le changement **identiquement aux 4 blocs `location`** de
    `frontend/nginx.conf` (index, sw/manifest, assets, fallback) — un écart entre
    blocs est une faille silencieuse. Ajouter/mettre à jour une vérification
    (test ou lint simple) que les 4 occurrences de la CSP sont identiques.
  - [x] 4.4 Si un style légitime (p. ex. MapLibre) est bloqué et non contournable
    proprement : **conserver `'unsafe-inline'`** et documenter le résidu assumé avec
    sa raison, plutôt que de casser le rendu. Une CSP qui casse l'app est pire qu'un
    résidu documenté.
  - DoD : soit `style-src` resserré et vérifié sans régression de rendu, soit
    `'unsafe-inline'` conservé avec justification écrite ; les 4 blocs CSP cohérents ;
    parcours carte + tenant fonctionnels.
  - _Requirements: 2 (critères 1, 4) ; NFR-2_

- [x] 5. [#9, #10] Documentation durable et non-régression finale
  - [x] 5.1 Consigner dans un emplacement durable (commentaire de `session.ts`
    faisant foi + cette spec) : (a) les paramètres KDF confirmés et leur contrat de
    test ; (b) l'ADR #9 (décision « conserver `localStorage` », options rejetées —
    dont la variante hybride cookie — et pourquoi) ; (c) la **limite explicite** — le
    durcissement du stockage des jetons ne protège PAS contre un XSS actif (clé en
    mémoire, appel direct au `TokenProvider`, PHI à l'écran). Aucun texte ne doit
    présenter la mesure comme « résolvant le XSS » (exigence 2.7).
  - [x] 5.2 Consigner la note **« Migration cryptographique »** (commentaire de
    `session.ts` près des constantes KDF) : tout futur relèvement d'un paramètre KDF
    change la clé dérivée et rend les PHI locales chiffrées avec l'ancienne clé
    illisibles ; nommer la stratégie à adopter alors (re-dérivation/re-chiffrement au
    login, versionnage des paramètres, ou perte acceptée des données non
    synchronisées). La v1 ne change aucun paramètre ; la note cadre le durcissement
    futur pour éviter une perte de données silencieuse (design, « Migration
    cryptographique »).
  - [x] 5.3 Non-régression : exécuter la suite frontend complète
    (`npm run test -- --run`) → verte, incluant les nouveaux tests. Confirmer que les
    comportements existants tiennent : base verrouillée après rechargement tant que
    le mot de passe n'est pas ressaisi ; révocation purge jetons + PHI ; verrouillage
    à la déconnexion/expiration (tests `db.encryption-at-rest`, `db.wrong-key`,
    `db.lock` verts).
  - DoD : documentation présente et conforme (dont la limite XSS actif) ; suite
    frontend complète verte ; aucun comportement existant régressé.
  - _Requirements: 2 (critère 7), 3 (tous critères) ; NFR-7, NFR-8, NFR-2, P13_

## Task Dependency Graph

```
1 (exposer constantes KDF) ──► 2 (test verrouillage KDF)
3 (test anti-sink)            indépendant
4 (durcir style-src CSP)      indépendant (changement runtime, fail-safe, en dernier)
2, 3, 4 ──► 5 (doc durable + non-régression finale)
```

Chemin critique : **1 → 2 → 5**. Les tâches 3 et 4 sont parallélisables ; la
tâche 4 est délibérément planifiée en dernier avant la clôture (tâche 5) car c'est
le seul changement potentiellement cassant.

```json
{
  "tasks": [
    { "id": "1", "obs": "#10",     "dependsOn": [],              "wave": 1, "severity": "low" },
    { "id": "3", "obs": "#9",      "dependsOn": [],              "wave": 1, "severity": "medium" },
    { "id": "4", "obs": "#9",      "dependsOn": [],              "wave": 1, "severity": "medium" },
    { "id": "2", "obs": "#10",     "dependsOn": ["1"],           "wave": 2, "severity": "medium" },
    { "id": "5", "obs": "#9, #10", "dependsOn": ["2", "3", "4"], "wave": 3, "severity": "low" }
  ],
  "waves": [
    { "wave": 1, "tasks": ["1", "3", "4"] },
    { "wave": 2, "tasks": ["2"] },
    { "wave": 3, "tasks": ["5"] }
  ],
  "criticalPath": ["1", "2", "5"]
}
```

## Notes

- Décision ADR #9 **tranchée** dans `design.md` (Option (a), conserver
  `localStorage`) : la tâche 4 applique le durcissement anti-XSS correspondant, la
  tâche 5 en consigne la justification et la limite.
- Résultat légitime de la tâche 4 : `'unsafe-inline'` conservé **si** son retrait
  casse un rendu légitime, à condition d'être documenté (critère 2.4). La mitigation
  obligatoire exigée par le critère 2.6 est alors portée par la tâche 3 (test
  anti-sink), déjà livrable indépendamment de la CSP.
- Aucun changement serveur : l'option cookie `httpOnly` (qui en aurait exigé) est
  rejetée par l'ADR.
