# Design Document

_Spec : durcissement de la cryptographie locale (client) — `local-crypto-hardening`._

## Overview

Cette conception traite deux observations de la revue d'architecture, toutes deux
localisées côté navigateur (`frontend/src/lib/session.ts`, `frontend/src/lib/db.ts`,
`frontend/nginx.conf`) :

- **#10 — KDF de la clé de chiffrement locale.** Le KDF existe déjà et est conforme
  (PBKDF2-HMAC-SHA256, 210 000 itérations, sel aléatoire 16 octets par utilisateur,
  clé 32 octets). Le travail est donc une **confirmation documentée + un test de
  verrouillage anti-régression**, pas une correction. Exigence 1.
- **#9 — Stockage des jetons.** Les jetons (`access` + `refresh`) sont persistés en
  `localStorage` (clé `dme.session`). Compte tenu de la connectivité intermittente
  au Togo (NFR-7 prioritaire), l'orientation retenue est de **conserver
  `localStorage`** et de durcir la défense anti-XSS **à la source**, en livrant au
  moins une mitigation concrète et testée, plutôt que de déplacer le jeton (ce qui
  dégraderait NFR-7 sans fermer le XSS actif). Exigences 2 et 3.

Les deux changements sont **additifs et non régressifs** : aucun comportement de
session, de chiffrement ou de synchronisation existant n'est modifié. La conception
tient dans le frontend ; aucun changement serveur n'est requis (l'option cookie,
qui en aurait exigé, est rejetée par l'ADR ci-dessous).

### État vérifié du code (ancrages)

- `session.ts` : `PBKDF2_ITERATIONS = 210_000`, sel `new Uint8Array(16)` via
  `crypto.getRandomValues`, `DERIVED_KEY_BITS = 256`, dérivation
  `crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", ... })`. Clé et mot
  de passe jamais persistés ; sel persisté en clair sous `dme.kdf.salt.<email>`.
- `db.ts` : `initDb(encryptionKey)` passe la clé en `password` au stockage RxDB
  chiffré (`wrappedKeyEncryptionCryptoJsStorage`).
- `frontend/nginx.conf` : CSP applicative **sans joker** présente sur toutes les
  routes servies — `default-src 'self'; connect-src 'self' <api>; img-src 'self'
  data: blob:; style-src 'self' 'unsafe-inline'; font-src 'self'; worker-src 'self';
  frame-ancestors 'none'; object-src 'none'; base-uri 'self'; report-to csp-endpoint`.
- Frontend = **React** (`main.tsx`) : le rendu JSX échappe automatiquement les
  valeurs interpolées. **Aucun** sink dangereux (`innerHTML`, `outerHTML`,
  `dangerouslySetInnerHTML`, `eval(`, `insertAdjacentHTML`, `document.write`)
  n'existe aujourd'hui dans `frontend/src/**` (vérifié par recherche). C'est la
  raison structurelle pour laquelle l'injection de contenu n'a pas de point d'entrée
  JS trivial — propriété que cette spec va **verrouiller par un test**.

## Architecture

```
┌─────────────────────────────── Poste de travail (navigateur) ───────────────────────────────┐
│                                                                                               │
│  Saisie mot de passe ──► session.ts ──► deriveEncryptionKey(password, salt)                   │
│     (jamais persisté)        │              PBKDF2-SHA256 / 210k / 16o sel / 32o clé          │
│                              │                     │                                          │
│                              │                     ▼  clé hex (jamais persistée)              │
│                              │              db.ts : initDb(clé) ──► RxDB chiffré (PHI au repos)│
│                              │                                                                │
│                              ├──► localStorage["dme.session"] : access + refresh (persistés)  │
│                              │        │  menace couverte : perte/vol du poste                 │
│                              │        │  (jeton seul ≠ accès PHI : clé non persistée)         │
│                              │        │  menace résiduelle : XSS ───────────────┐             │
│                              └──► localStorage["dme.kdf.salt.<email>"] : sel     │             │
│                                       (public, non secret)                      │             │
│                                                                                 ▼             │
│   Défense XSS (à la source) : CSP sans joker + style-src durci + 0 sink JS + React auto-escape │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

Deux axes indépendants :

1. **Axe KDF (#10)** : purement défensif/documentaire. On fige les paramètres
   existants derrière des constantes déjà nommées et on ajoute un test qui échoue si
   un paramètre régresse. Aucun flux runtime ne change.
2. **Axe jetons (#9)** : on garde le stockage `localStorage` et on durcit la couche
   anti-XSS. Le seul changement de comportement runtime **possible** est le
   resserrement de `style-src` (retrait de `'unsafe-inline'`) ; il est traité avec
   précaution (voir Error Handling) car il peut casser des styles inline légitimes.

## Decision Record (ADR #9) — Stockage des jetons d'authentification

**Contexte.** Jetons `access` (TTL 8 h) + `refresh` (TTL 14 j) persistés en
`localStorage`. Menace principale assumée : perte/vol du poste (déjà couverte, le
jeton seul ne déverrouille pas les PHI). Menace résiduelle : vol de jeton par XSS.
Contrainte dominante : connectivité intermittente → NFR-7 (travail hors-ligne après
rechargement) est prioritaire.

**Options évaluées** (exigence 2.1 / 2.2) :

| Option | Résistance XSS (vol jeton au repos) | Impact NFR-7 | CSRF | Changement contrat `/api/auth/*` |
|--------|--------------------------------------|--------------|------|----------------------------------|
| **(a) `localStorage` + mitigations renforcées** | Faible au repos, mais défense portée à la **source** (CSP, style-src, 0 sink) | **Aucun** — session restaurée hors-ligne, inchangé | Aucune | Aucun |
| (b) Jetons en **mémoire seule** | Élevée (rien au repos) | **Cassé** — tout rechargement (veille mobile, éviction d'onglet) perd le jeton ; `login` exige le réseau → soignant bloqué hors-ligne | Aucune | Aucun |
| (c) Cookie `httpOnly` (refresh en cookie, access en mémoire) | Élevée (cookie invisible au JS) | **Dégradé** — le refresh passe par `/api/auth/refresh`, chemin **en ligne** ; le mode Bearer actuel (lu par le `TokenProvider` JS) devient inopérant hors-ligne | **Introduite** (cookie auto-envoyé) → à mitiger (SameSite, anti-CSRF) | **Important** — login/refresh posent/lisent un cookie ; revoir l'injection Bearer |

**Variante hybride considérée (et écartée).** Un schéma mixte — refresh en cookie
`httpOnly` utilisé **uniquement en ligne** (`/api/auth/refresh`), access conservé en
`localStorage`/mémoire pour l'usage hors-ligne — fermerait le vol XSS du **refresh**
(le jeton le plus durable, 14 j) tout en préservant NFR-7 pour l'access. Écartée
néanmoins pour la v1 : elle exige un changement de contrat serveur (poser/lire le
cookie, mitigation CSRF) et complexifie le cycle de vie de session pour un bénéfice
partiel (l'access, volé via XSS, reste exploitable 8 h). À réévaluer si le profil de
menace évolue ; notée ici pour que l'ADR soit complet.

**Décision : Option (a).** On conserve `localStorage` et on investit la défense
anti-XSS à la source. Justification (exigence 2.3) : (b) et (c) achètent une
résistance au vol de jeton **au repos** au prix d'une dégradation de NFR-7 —
inacceptable pour un centre à connectivité intermittente où le rechargement
hors-ligne est un cas nominal. De plus, **aucune** des deux ne ferme le **XSS
actif** (voir limite ci-dessous) : un script injecté pendant une session
déverrouillée lit la clé en mémoire et les PHI à l'écran, quel que soit le stockage
du jeton. Déplacer le jeton donnerait donc une fausse assurance pour un coût
terrain réel.

**Ce que (a) exige concrètement** (exigence 2.6) — la décision n'est PAS un statu
quo passif ; cette spec DOIT livrer au moins une mitigation vérifiable :

1. **Resserrement de `style-src`** : évaluer le retrait de `'unsafe-inline'`. Si
   faisable sans casser l'app (styles portés par Tailwind/classes, pas de
   `style="..."` dynamique injecté), passer à `style-src 'self'` (± nonce/hash).
   Sinon, documenter `'unsafe-inline'` comme résiduel assumé **avec sa raison**.
2. **Test anti-régression de sink JS** : un test qui scanne `frontend/src/**` et
   échoue si un sink dangereux (`innerHTML`, `dangerouslySetInnerHTML`, `eval(`,
   `insertAdjacentHTML`, `document.write`, `outerHTML`) réapparaît. Verrouille la
   propriété « 0 sink » constatée aujourd'hui.
3. **(Facultatif, borné)** Purge d'un jeton `localStorage` au-delà d'un seuil de
   péremption configurable, sans descendre sous une journée de travail hors-ligne
   (NFR-7). Non retenu en obligatoire : la fenêtre est déjà bornée par le TTL
   serveur (8 h access / 14 j refresh, G-9).

**Limite explicite (exigence 2.7).** L'option (a) — comme (b) et (c) — **ne protège
pas contre un XSS actif** exécuté pendant une session déverrouillée : lecture de la
clé de chiffrement en mémoire, appel direct au `TokenProvider`, ou exfiltration des
PHI déchiffrées affichées. La seule défense de fond est la **prévention de
l'injection** (CSP, `style-src` durci, 0 sink, auto-échappement React). Aucun
livrable ne présentera cette mesure comme « résolvant le XSS ».

## Components and Interfaces

### C1 — Constantes KDF (confirmation #10, aucune nouvelle API)

`session.ts` conserve ses constantes existantes, désormais traitées comme le
**point de vérité unique** référencé par le test de verrouillage :

- `PBKDF2_ITERATIONS = 210_000`
- longueur de sel = 16 octets (dans `getOrCreateSalt`, `new Uint8Array(16)`)
- `DERIVED_KEY_BITS = 256` (⇒ 32 octets)
- algorithme : `"PBKDF2"` / `hash: "SHA-256"`

Pour rendre ces valeurs **testables sans dupliquer de littéraux**, on les exporte
(export nommé depuis `session.ts`, ou un petit module `crypto-params.ts` réexporté)
afin que le test lise la constante réelle plutôt qu'un nombre recopié. Décision :
**exporter les constantes depuis `session.ts`** (surface minimale, pas de nouveau
module), avec un commentaire indiquant qu'elles sont sous contrat de test.

### C2 — CSP `frontend/nginx.conf` (mitigation #9)

Une seule directive change potentiellement : `style-src`. La CSP est répétée sur
4 blocs `location` (index, sw/manifest, assets, fallback) : toute modification DOIT
être appliquée **identiquement aux 4 occurrences** (invariant de cohérence — un
écart entre blocs est un bug de sécurité silencieux). Aucune autre directive n'est
touchée.

### C3 — Tests (livrables vérifiables)

- `crypto-params.test.ts` (nouveau) : verrouille les seuils KDF (exigence 1.3, 1.5,
  1.6).
- `no-dangerous-sinks.test.ts` (nouveau) : scanne les sources et verrouille « 0 sink »
  (exigence 2.6). **Limites explicites à documenter dans le test** : (a) il ne scanne
  que les motifs dans le **code réel**, en excluant commentaires et chaînes de
  caractères (sinon le commentaire « Styles inline » de `TogoMap.tsx` et les motifs
  du test lui-même produiraient de faux positifs) ; (b) il verrouille le code **du
  projet**, PAS les dépendances tierces — un sink introduit par une bibliothèque
  (p. ex. un composant faisant de l'`innerHTML`) n'est pas détecté. Le test réduit
  la surface, il ne la supprime pas ; cette limite est nommée pour ne pas donner de
  fausse assurance (cohérent avec la limite XSS actif, exigence 2.7).
- Non-régression : les tests de confidentialité locale existants restent verts
  (exigence 3.1).

## Data Models

Aucun modèle de données nouveau ou modifié. Rappel des clés `localStorage`
concernées (aucune ne change de forme) :

| Clé | Contenu | Secret ? | Persisté |
|-----|---------|----------|----------|
| `dme.session` | `{ access, refresh, expiresAt, user }` | Jetons = sensibles | Oui (décision ADR : conservé) |
| `dme.kdf.salt.<email>` | sel hex (16 o) | Non (sel public) | Oui |
| `dme.sync.cursor` | curseur opaque de sync | Non (pas de PHI) | Oui |
| clé de chiffrement dérivée | — | **Oui** | **Jamais** (mémoire seule) |
| mot de passe | — | **Oui** | **Jamais** |

## Correctness Properties

### Property 1: Dérivation de clé conforme (#10)

La clé locale est dérivée par PBKDF2-SHA256 avec itérations ≥ 210 000, sel ≥ 16
octets par utilisateur, et clé = 32 octets (256 bits). _Vérifié par
`crypto-params.test.ts`._

**Validates: Requirements 1.1, 1.2, 1.3**

### Property 2: Sel par utilisateur, non partagé (#10)

Deux utilisateurs distincts obtiennent des sels distincts (unicité par compte, pas
de réutilisation). _Vérifié par `crypto-params.test.ts`._

**Validates: Requirements 1.5**

### Property 3: Secrets jamais persistés (#10)

Ni la clé dérivée ni le mot de passe ne sont jamais écrits dans un stockage
persistant (`localStorage` ou autre). _Vérifié par test (inspection des clés
`localStorage` après un `login` simulé)._

**Validates: Requirements 1.6**

### Property 4: Aucun sink JS dangereux (#9)

Aucun sink dangereux (`innerHTML`, `dangerouslySetInnerHTML`, `eval(`,
`insertAdjacentHTML`, `document.write`, `outerHTML`) n'est présent dans le code du
projet sur un chemin atteignable. _Vérifié par `no-dangerous-sinks.test.ts` (limité
au code du projet, hors dépendances tierces)._

**Validates: Requirements 2.6**

### Property 5: Limite assumée — pas de protection contre le XSS actif (#9)

La protection du stockage des jetons NE couvre PAS un XSS actif exécuté pendant une
session déverrouillée (lecture de la clé en mémoire, appel direct au
`TokenProvider`, exfiltration des PHI affichées). _C'est une **limite documentée**,
pas une garantie vérifiable : elle est énoncée pour interdire toute sur-promesse
(exigence 2.7), la défense de fond restant la prévention de l'injection._

**Validates: Requirements 2.7**

### Property 6: Non-régression de la confidentialité locale (#9, #10)

Les garanties existantes restent vraies : PHI illisibles sans la clé, refus
d'ouverture avec une mauvaise clé, verrouillage de la clé à la fermeture. _Vérifié
par les tests de confidentialité locale existants._

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Error Handling

- **Resserrement `style-src` — ce qui est concerné et ce qui ne l'est pas.** Point
  de vigilance vérifié dans le code, car une confusion fréquente enverrait sur une
  fausse piste :
  - `style-src 'unsafe-inline'` ne régit QUE l'inline **CSS** du HTML : attributs
    `style="…"` et éléments `<style>`. Vérifié : le frontend ne contient **aucun**
    attribut `style={{…}}`/`style="…"` (React n'émet pas d'inline-style).
  - `TogoMap.tsx` pose des styles **par propriété DOM** (`el.style.pointerEvents`,
    `el.style.fontSize`, …). L'API `HTMLElement.style.*` **n'est PAS** soumise à
    `style-src` : ces lignes **ne cassent pas** avec `style-src 'self'`. Elles ne
    sont donc PAS une raison de conserver `'unsafe-inline'`.
  - Le **seul risque réel** est le CSS que MapLibre injecte **au runtime** pour ses
    contrôles (`NavigationControl`, marqueurs) : il peut créer des `<style>` ou
    poser des attributs `style` soumis à la CSP. Ce risque est **empirique** : il se
    teste, il ne se présume pas.
  - Stratégie **fail-safe** : émettre d'abord la CSP resserrée en
    `Content-Security-Policy-Report-Only` (le `report-to`/`Reporting-Endpoints`
    existe déjà), exercer les parcours **carte MapLibre** (le plus à risque) + pages
    tenant + login, et n'observer les violations que de `style-src`. Basculer en
    enforcing seulement si aucune violation légitime. Si MapLibre en génère une
    non contournable, **conserver `'unsafe-inline'`** et documenter le résidu avec sa
    cause précise (CSS runtime MapLibre, PAS `el.style.*`). Une CSP qui casse la
    carte est pire qu'un résidu documenté.
- **Cohérence des 4 blocs CSP.** Un changement appliqué à 3 blocs sur 4 crée une
  faille silencieuse. Mitigation : le test/vérification compare les 4 occurrences
  (ou un lint simple) pour garantir l'identité.
- **Sel manquant au démarrage** (déjà géré par `session.ts` : régénération). Non
  modifié ; rappelé pour mémoire.
- **Migration cryptographique (conséquence d'un futur durcissement).** Le test de
  verrouillage (tâche 2) autorise de **relever** les paramètres KDF (itérations, sel,
  longueur de clé). Or la clé est **dérivée** de ces paramètres : changer un
  paramètre produit une clé **différente**, qui ne déverrouille plus une base RxDB
  chiffrée avec l'ancienne clé → **PHI locales illisibles**. Pour un système santé,
  ce risque de perte silencieuse DOIT être nommé et une stratégie choisie au moment
  du durcissement, parmi : (a) **re-dérivation + re-chiffrement** au prochain login
  en ligne (l'utilisateur ressaisit son mot de passe, on re-dérive avec les nouveaux
  paramètres et on ré-écrit la base) ; (b) **versionnage des paramètres** stocké à
  côté du sel, pour dériver avec les paramètres d'origine tant que la base n'est pas
  migrée ; (c) acceptation explicite de la perte des données locales **non encore
  synchronisées** (les données synchronisées sont re-tirées du serveur). La v1 ne
  change AUCUN paramètre (donc aucune migration) ; cette note cadre tout durcissement
  **futur** pour qu'il ne provoque pas de perte de données par inadvertance.

## Testing Strategy

1. **Verrouillage KDF** (`crypto-params.test.ts`, Vitest) :
   - assertions sur les constantes exportées (itérations ≥ 210 000, sel ≥ 16,
     clé = 32, algo PBKDF2-SHA256) ;
   - deux dérivations avec des emails distincts ⇒ sels distincts ;
   - après un `login` simulé, aucune clé `localStorage` ne contient la clé dérivée
     ni le mot de passe.
2. **Verrouillage anti-sink** (`no-dangerous-sinks.test.ts`, Vitest) : lecture des
   fichiers sources `frontend/src/**`, échec si un motif de sink dangereux apparaît.
3. **CSP** : vérification que `style-src` est cohérent sur les 4 blocs et conforme à
   la décision (soit `'self'` durci, soit `'unsafe-inline'` documenté comme résidu).
4. **Non-régression** : `db.encryption-at-rest`, `db.wrong-key`, `db.lock` restent
   verts ; suite frontend complète verte (`npm run test -- --run`).
5. **Contrainte projet** : aucun avertissement de dépréciation ; les nouveaux tests
   respectent les conventions Vitest existantes.

## Hors périmètre (rappel)

- Passage du chiffrement au repos à Web Crypto (A-1) — non traité.
- Durcissement JWT serveur (denylist refresh) — déjà livré.
- Options (b) mémoire seule et (c) cookie `httpOnly` — évaluées puis rejetées par
  l'ADR ; non implémentées.
