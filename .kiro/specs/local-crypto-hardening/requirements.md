# Requirements Document

_Spec : durcissement de la cryptographie locale (client) — `local-crypto-hardening`._

## Introduction

Cette spec traite le **durcissement de la protection des secrets côté navigateur**
sur le poste de travail tenant : (1) la **clé de chiffrement de la base locale**
(dérivée du mot de passe, protège les PHI au repos, NFR-8/P13) et (2) les **jetons
d'authentification** (access + refresh, NFR-7). Elle regroupe deux observations de
la revue d'architecture :

- **#10** — *Confirmer le KDF de la clé de chiffrement locale.*
- **#9** — *Migrer le JWT hors de `localStorage` (exposition XSS).*

Les deux portent sur `frontend/src/lib/session.ts` (propriétaire du cycle de vie
de session, tâche 14.5) et son interaction avec `frontend/src/lib/db.ts` (base
RxDB chiffrée).

### État vérifié du code (base de cette spec)

Avant de poser les exigences, l'état réel du dépôt a été vérifié :

1. **KDF (#10) — déjà implémenté.** `session.ts` dérive la clé via **Web Crypto
   `PBKDF2-SHA256`**, **210 000 itérations** (recommandation OWASP 2023+), **sel
   aléatoire de 16 octets par utilisateur** (persisté en clair — un sel n'est pas
   un secret), sortie **256 bits** encodée hex passée à `initDb()`. Ni la clé ni le
   mot de passe ne sont persistés. **Le KDF est donc conforme aux bonnes
   pratiques.** Le travail de #10 n'est PAS une correction mais une **confirmation
   formelle + un verrouillage anti-régression** des paramètres.

2. **Jetons (#9) — en `localStorage`, par conception documentée.** Les jetons
   `access` + `refresh` sont persistés sous la clé `dme.session` de `localStorage`.
   Le fichier documente explicitement le **modèle de menace** : la menace couverte
   est la **perte/vol du poste** ; sur un poste volé, les jetons seuls ne donnent
   PAS accès aux PHI (la clé de chiffrement n'est jamais persistée). La persistance
   des jetons sert la **survie au rechargement pour l'usage hors-ligne** (NFR-7).
   Le risque restant est le **vol de jeton par XSS** (un script injecté lit
   `localStorage`). Migrer hors de `localStorage` est donc un **durcissement**, pas
   une correction de bug, et il entre en **tension directe avec NFR-7**.

### Tension de conception centrale (à trancher dans le design)

`localStorage` survit au rechargement de l'onglet → permet de restaurer la session
hors-ligne (NFR-7). Les alternatives résistantes au XSS ont des coûts :

- **Mémoire seule (pas de persistance)** : immunise contre le vol XSS passif de
  jeton au repos, mais **casse** la restauration de session après un rechargement
  hors-ligne (NFR-7) — l'utilisateur devrait se reconnecter, impossible sans
  réseau pour le `login`.
- **Cookie `httpOnly`** : invisible au JS (anti-XSS), mais exige que le serveur
  pose/lise le cookie (changement de contrat `/api/auth/*`), introduit une surface
  **CSRF** à mitiger, et un cookie `httpOnly` **n'est pas lisible** par le
  `TokenProvider` JS qui injecte aujourd'hui le `Authorization: Bearer` — ce qui
  change le mode d'authentification des appels `fetch`.

Aucune option n'est « gratuite ». Cette spec **n'impose pas** de solution : elle
exige qu'une décision d'architecture (ADR) soit prise et justifiée au regard de la
tension NFR-7 ↔ NFR-8/sécurité XSS, puis implémentée et testée. Un résultat
légitime de l'investigation #9 est « conserver `localStorage` avec des mitigations
renforcées, ADR documenté » si le design démontre que les alternatives dégradent
NFR-7 de façon inacceptable pour le contexte terrain (connectivité intermittente).

### Hors périmètre

- Le chiffrement au repos lui-même (plugin RxDB `encryption-crypto-js`) et son
  éventuel passage à Web Crypto (amélioration A-1) — non traité ici.
- Toute modification du protocole de synchronisation ou du durcissement JWT côté
  serveur (denylist de refresh, déjà livrée).
- La sécurité des autres entrées de `localStorage` non sensibles (curseur de sync,
  sel KDF) : le sel n'est pas un secret ; le curseur ne contient pas de PHI.
- **La rotation de clé et le changement de mot de passe** (et leur effet sur les PHI
  chiffrées avec l'ancienne clé) : volontairement HORS PÉRIMÈTRE de cette spec, qui
  se limite à confirmer/verrouiller le KDF existant et à arbitrer le stockage des
  jetons. En système de santé, le changement de mot de passe invalide la clé dérivée
  et donc l'accès aux PHI locales chiffrées avec la précédente — un sujet réel qui
  relève d'une **spec future dédiée** (re-dérivation/re-chiffrement, politique de
  rotation). Il est nommé ici pour qu'un lecteur ne le suppose PAS couvert. La
  conséquence technique d'un futur durcissement des paramètres KDF est traitée dans
  `design.md` (« Migration cryptographique »).
- **La ré-authentification périodique forcée** (expiration de session côté politique
  plutôt que côté TTL de jeton) : hors périmètre, à traiter avec la rotation
  ci-dessus si le besoin est confirmé.

## Glossary

- **KDF** (Key Derivation Function) : fonction dérivant une clé cryptographique à
  partir d'un secret à faible entropie (mot de passe) + un sel.
- **PBKDF2** : KDF normalisée (PBKDF2-HMAC-SHA256 ici), à coût itératif
  configurable.
- **Clé de chiffrement locale** : clé (hex, 256 bits) passée à `initDb()`,
  déverrouillant la base RxDB chiffrée. Jamais persistée.
- **Sel** : valeur aléatoire par utilisateur, publique (non secrète), rendant la
  dérivation unique par compte. Persisté en clair.
- **XSS** (Cross-Site Scripting) : injection de script dans la page, capable de
  lire tout ce qui est accessible au JS (dont `localStorage`).
- **TokenProvider** : fonction JS fournissant l'access token courant au moteur de
  sync et aux appels `fetch` (en-tête `Authorization: Bearer`).

## Non-functional Requirements rattachées

- **NFR-2** — Protection des données : hachage moderne, pas de secret exposé.
- **NFR-7** — Authentification tolérante à l'hors-ligne : session restaurable sans
  réseau tant que le jeton est valide.
- **NFR-8** — Sécurité des données locales : PHI chiffrées au repos ; clé
  verrouillée à la déconnexion ; perte/vol du poste n'expose pas les PHI en clair.
- **P13** — Confidentialité locale (propriété de correction du design).

## Requirements

### Requirement 1: Confirmation et verrouillage du KDF de la clé locale (#10)

**User Story:** En tant que responsable sécurité, je veux une preuve explicite et
non régressable que la clé de chiffrement locale est dérivée selon les bonnes
pratiques, afin que la protection des PHI au repos ne puisse pas être affaiblie par
inadvertance lors d'une évolution future.

#### Acceptance Criteria

1. LE système DOIT documenter, dans un emplacement durable (design + commentaire de
   code faisant foi), que la clé locale est dérivée via PBKDF2-HMAC-SHA256 avec un
   nombre d'itérations conforme à une référence **datée** (OWASP Password Storage
   Cheat Sheet, révision ≥ 2023 : PBKDF2-SHA256 ≥ 210 000 itérations) — et non à un
   « en vigueur » flou qui dériverait avec le temps —, un sel
   aléatoire d'au moins **16 octets** par utilisateur, et une longueur de clé de
   **32 octets (256 bits)**. Les seuils sont exprimés en **octets** de façon
   homogène dans toute cette spec (le code manipule des `Uint8Array`), pour que le
   seuil du test de verrouillage (critère 3) soit sans ambiguïté.
2. LES paramètres du KDF (algorithme, itérations, longueur de sel, longueur de clé)
   DOIVENT être exprimés comme des **constantes nommées** uniques (déjà le cas), de
   sorte qu'un durcissement reste un changement d'un seul point.
3. LE système DOIT comporter un **test automatisé** qui échoue si l'un des
   paramètres du KDF descend sous un seuil de sécurité défini : itérations
   < 210 000, sel < 16 octets, clé < 32 octets (256 bits), ou algorithme
   ≠ PBKDF2-SHA256. Le test verrouille la conformité contre une régression.
4. SI l'investigation révèle un écart par rapport aux bonnes pratiques (itérations
   insuffisantes, sel trop court, réutilisation de sel entre utilisateurs) ALORS le
   système DOIT corriger le paramètre fautif ; SINON la confirmation documentée +
   le test de verrouillage suffisent (aucune modification de comportement requise).
5. LE sel DOIT rester **par utilisateur** et NE DOIT PAS être partagé entre
   comptes ; le test DOIT vérifier que deux comptes distincts obtiennent des sels
   distincts.
6. LA clé dérivée et le mot de passe NE DOIVENT jamais être persistés (ni en
   `localStorage`, ni ailleurs sur le poste) ; cette propriété DOIT être couverte
   par un test.

- _Requirements: observation #10 ; NFR-8, NFR-2, P13_

### Requirement 2: Décision d'architecture sur le stockage des jetons (#9)

**User Story:** En tant qu'architecte, je veux une décision tranchée et justifiée
sur le stockage des jetons d'authentification côté client, afin d'arbitrer
explicitement entre la résistance au XSS et la tolérance à l'hors-ligne (NFR-7),
plutôt que de laisser le choix actuel implicite.

> **Orientation (décision produit, contexte Togo).** La connectivité intermittente
> rend NFR-7 prioritaire : un jeton non persistant (mémoire seule) ou un refresh
> dépendant du réseau (cookie `httpOnly` + `/api/auth/refresh` en ligne)
> laisserait un soignant hors de son application au premier rechargement sans
> réseau — inacceptable en clinique rurale. Le défaut attendu est donc de
> **conserver `localStorage`** (dont le modèle de menace perte/vol est déjà couvert :
> le jeton seul ne déverrouille pas les PHI) et d'investir la défense **à la
> source du XSS**, PLUTÔT que de déplacer le jeton. Le déplacement reste à
> comparer dans l'ADR, mais n'est retenu que s'il préserve NFR-7.

#### Acceptance Criteria

1. LE design DOIT produire un **ADR** comparant au minimum trois options :
   (a) conserver `localStorage` avec mitigations renforcées, (b) jetons en mémoire
   seule, (c) cookie `httpOnly` (± refresh en cookie, access en mémoire).
2. L'ADR DOIT évaluer chaque option contre : résistance au vol de jeton par XSS,
   impact sur NFR-7 (restauration de session hors-ligne après rechargement),
   surface CSRF introduite, et ampleur du changement de contrat `/api/auth/*`.
3. L'ADR DOIT retenir une option et **justifier** le rejet des autres au regard du
   contexte terrain (connectivité intermittente, menace principale = perte/vol du
   poste, déjà documentée dans `session.ts`). Toute option retenue DOIT **préserver
   NFR-7** (travail hors-ligne après rechargement tant que le jeton est valide) ;
   une option qui dégrade NFR-7 NE PEUT être retenue que si l'ADR démontre que le
   bénéfice sécurité l'emporte ET documente le compromis terrain accepté.
4. SI l'option retenue conserve `localStorage` ALORS elle DOIT s'accompagner de
   mitigations XSS renforcées **nommées et vérifiables**, parmi :
   - la CSP applicative sans joker, **confirmée présente** dans `frontend/nginx.conf`
     (`default-src 'self'`, `object-src 'none'`, `base-uri 'self'`,
     `frame-ancestors 'none'`) — première ligne de défense ;
   - le **durcissement de la directive `style-src`**, qui contient aujourd'hui
     `'unsafe-inline'` : ce n'est pas un joker, mais c'est une brèche XSS connue
     (injection de style) que le design DOIT évaluer et, si possible, resserrer
     (nonce/hash) ou documenter comme résiduelle ;
   - l'absence de sink JS dangereux (`innerHTML`/`eval`/`dangerouslySetInnerHTML`)
     sur un chemin atteignable par une entrée non fiable, à vérifier.
   La durée de vie de l'access token n'entre PAS dans ce critère : elle est fixée
   côté serveur (8 h, spec `production-readiness-gaps` G-9) et le client ne fait que
   stocker l'`expiresAt` reçu — il ne « choisit » aucune durée de persistance.
5. SI l'option retenue déplace les jetons hors de `localStorage` ALORS la
   restauration de session hors-ligne (NFR-7) NE DOIT PAS être cassée : le
   comportement « travailler après rechargement tant que le jeton est valide »
   DOIT être préservé OU l'ADR DOIT documenter explicitement le compromis accepté
   et son effet terrain.
6. SI l'option retenue conserve `localStorage` ALORS cette spec NE DOIT PAS se
   clore sur le seul ADR : au moins **une mitigation XSS concrète DOIT être livrée
   et couverte par un test ou une vérification automatisée** (p. ex. resserrement
   effectif de `style-src`, ou un test qui échoue si un sink JS dangereux
   réapparaît sur un chemin atteignable). Un document seul, sans amélioration
   vérifiable, NE satisfait PAS l'exigence. En complément facultatif, la **fenêtre
   d'exposition d'un jeton volé** PEUT être bornée en réduisant la durée de validité
   effective du jeton **persisté côté client** (ignorer/purger un jeton local
   au-delà d'un seuil configurable), SANS descendre sous ce qui couvre une journée
   de travail hors-ligne typique (NFR-7).
7. LE design ET l'ADR DOIVENT nommer explicitement la **limite** de cette mesure :
   déplacer ou durcir le stockage des jetons ne protège PAS contre un **XSS actif**
   exécuté pendant une session déverrouillée (lecture de la clé de chiffrement en
   mémoire, appel direct au `TokenProvider`, exfiltration de PHI déchiffrées à
   l'écran). Aucun livrable NE DOIT présenter la mesure comme « résolvant le XSS » :
   la défense de fond reste la prévention de l'injection (CSP, `style-src` durci,
   absence de sink). Cette limite DOIT figurer dans la documentation durable.

- _Requirements: observation #9 ; NFR-7, NFR-8, NFR-2_

### Requirement 3: Non-régression de la sécurité locale existante (#9, #10)

**User Story:** En tant que mainteneur, je veux que ce durcissement n'affaiblisse
aucune garantie déjà en place, afin que les propriétés P13 et NFR-7 restent
vérifiées.

#### Acceptance Criteria

1. APRÈS les changements, les tests existants de confidentialité locale DOIVENT
   rester verts — identifiés par leur **comportement** (noms de fichiers à ce jour,
   susceptibles d'évoluer) : PHI illisibles sans la clé
   (`db.encryption-at-rest.test.ts`), refus d'ouverture avec une mauvaise clé
   (`db.wrong-key.test.ts`), verrouillage de la clé à la fermeture
   (`db.lock.test.ts`). Le critère porte sur la préservation de ces comportements,
   pas sur l'existence de fichiers aux noms exacts.
2. APRÈS les changements, le comportement de session documenté DOIT rester vrai :
   la base reste **verrouillée** après rechargement tant que le mot de passe n'est
   pas ressaisi (clé non persistée), et une **révocation** purge jetons + PHI.
3. LE verrouillage à la déconnexion/expiration (fermeture de la base, effacement
   des jetons en mémoire et persistés) DOIT continuer de fonctionner, quel que soit
   le mécanisme de stockage retenu pour les jetons.
4. LA suite frontend complète (`npm run test -- --run`) DOIT rester verte, et tout
   nouveau test introduit DOIT passer.

- _Requirements: observations #9, #10 ; NFR-7, NFR-8, P13_

## Assumptions

- Le KDF actuel (PBKDF2-SHA256, 210 000 itérations, sel 16 octets, 256 bits) est
  réputé conforme ; l'investigation #10 est attendue comme une **confirmation**,
  sauf découverte contraire.
- Du fait de la connectivité intermittente au Togo (NFR-7 prioritaire),
  l'orientation par défaut de #9 est de **conserver `localStorage`** et de durcir la
  défense anti-XSS à la source, les alternatives (mémoire seule, cookie `httpOnly`)
  dégradant la restauration de session hors-ligne. Mais « conserver `localStorage` »
  n'est PAS un statu quo passif : la spec exige au moins une **mitigation concrète
  livrée et testée** (critère 2.6), pas seulement un ADR.
- Déplacer les jetons hors de `localStorage` ne ferme pas le XSS actif (clé en
  mémoire, appel direct au `TokenProvider`) : la mesure est présentée avec sa limite
  (critère 2.7), jamais comme une résolution du XSS.
- La CSP applicative sans joker (durcissement de production, spec
  `production-readiness-gaps`, G-3) est la première ligne de défense contre le XSS
  et reste en vigueur indépendamment de la décision sur les jetons.
- Aucun changement côté serveur n'est présumé nécessaire pour #10 ; #9 peut en
  exiger (option cookie) — à trancher dans l'ADR.
