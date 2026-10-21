# Requirements — Trois correctifs plateforme DME (rate-limit auth, docs E2E, spec denylist)

## Summary

Ce lot regroupe trois correctifs ciblés et non régressifs sur le dépôt DME
(`backend` FastAPI + documentation + spec), à réaliser directement dans le dépôt
(pas de worktree) :

1. **Correctif 1 — Rate-limit par IP sur l'authentification.** Le verrouillage de
   compte (`app/services/lockout.py`) protège par compte (5 échecs → verrou 15 min),
   mais `POST /api/auth/login` n'a aucune limite par IP applicative. Un attaquant peut
   donc balayer de nombreux comptes (un essai chacun) sans jamais déclencher de verrou.
   On ajoute une défense par IP sur les routes d'auth, en répliquant exactement le
   pattern SlowAPI public existant (repli applicatif) + une règle au reverse proxy
   Nginx — conforme à la doctrine du projet (limite au proxy en prod, repli SlowAPI
   en dev/tests/mono-instance).

2. **Correctif 2 — Régénération de `docs/E2E_VERIFICATION.md`.** Le document cite des
   comptes de tests périmés. Il faut refléter les comptes réels mesurés, puis
   **re-mesurés** après le Correctif 1 (le nouveau fichier de test fait augmenter le
   total backend).

3. **Correctif 3 — Correction de la spec `tasks.md`.** La tâche 9.2 (denylist de
   refresh `jti` + rotation de famille) y est marquée « Hors lot courant — phase 2 »,
   alors que le code la livre et la teste entièrement. Mise à jour documentaire de la
   spec uniquement (aucune modification de code).

Le contexte de lancement établit, et ne doit pas être re-enquêté, que les suites
passent aujourd'hui (backend `221 passed, 56 skipped` en mode unitaire ; frontend
`143 passed`) et que la doctrine de rate-limit place la limite au reverse proxy avec
un repli applicatif SlowAPI (déjà présent pour `/api/public/*` uniquement).

**Faits vérifiés dans le dépôt (ancrage, non à redécouvrir) :**
- `backend/app/core/rate_limit.py` expose `limiter` (clé IP) et `public_rate_limit()`.
- `backend/app/core/config.py` : `Settings.public_rate_limit = "60/minute"`
  (surchargeable via `PUBLIC_RATE_LIMIT`), avec commentaire explicatif riche.
- `backend/app/routers/auth.py` : `login` et `set_credentials` reçoivent déjà
  `request: Request` ; **`refresh` ne reçoit PAS `request: Request`**.
- `backend/app/main.py` : handler global `RateLimitExceeded → 429` déjà enregistré
  (`app.state.limiter = limiter`, `add_exception_handler`).
- `backend/app/tests/test_public_rate_limit_cache.py` : technique de référence
  (surcharge de seuil, fixture `autouse` `limiter.reset()` avant/après chaque test,
  `TestClient`, parsing du seuil depuis la config).
- `deploy/reverse-proxy/nginx.conf` : `limit_req_zone … zone=public_zone:10m rate=60r/m`,
  `limit_req_status 429`, `location /api/public/`, `location /api/`, `location /health`.
- Denylist refresh effectivement livrée : migration `0010_refresh_token.py`, service
  `refresh_tokens.py` (`record_issued_refresh`, `consume_refresh`), câblage dans
  `auth.py`, tests `test_refresh_denylist_db.py`.
- Spec `.kiro/specs/production-readiness-gaps/tasks.md` : la tâche 9.2 est cochée
  `[x]` mais son libellé et la note de bas de fichier la disent « phase 2 ».

## Assumptions

- **A1.** Le seuil par défaut `auth_rate_limit = "10/minute"` est appliqué aux trois
  endpoints d'auth (`login`, `refresh`, `set-credentials`) via le même décorateur de
  limite, clé par IP cliente, comme pour le pattern public.
- **A2.** Le défaut de 10/min ne doit pas faire échouer les suites d'auth existantes
  (`test_auth_router.py`, `test_auth_task7.py`, `test_refresh_denylist_db.py`). S'il
  s'avère qu'un de ces fichiers émet plus de 10 requêtes d'auth dans la même fenêtre
  IP, la remédiation attendue est, par ordre de préférence : (a) reset du limiteur
  entre tests via fixture, ou (b) surcharge du seuil de test — sans jamais altérer le
  comportement fonctionnel de l'auth. Le choix précis relève du design.
- **A3.** Le 429 d'auth est rendu par le handler SlowAPI global déjà en place ; aucun
  ajout dans `main.py`. Corps minimal, aucune fuite d'information.
- **A4.** Pour le Correctif 2, le total du mode intégration (PostGIS réel) est dérivé
  comme `unitaires + skips exécutés`. Si le mode intégration n'est pas exécutable
  localement (pas de PostGIS), le document l'étiquette explicitement comme dérivé des
  deux modes et confirmé en CI, sans inventer de chiffre non mesuré.
- **A5.** Le nombre frontend (143, 26 fichiers) est inchangé par ce lot mais doit être
  re-confirmé par exécution.
- **A6.** Le Correctif 3 ne touche qu'à la documentation de spec (`tasks.md`). La case
  9.2 est déjà `[x]` ; le travail porte sur le **libellé** « phase 2 / à planifier » et
  la **note de bas de fichier**, à remplacer par l'état réel « livrée ».

## Functional Requirements

### Correctif 1 — Rate-limit par IP sur l'auth

- **FR-1.1 — Réglage de configuration.** Ajouter à `Settings`
  (`backend/app/core/config.py`) un champ `auth_rate_limit: str = "10/minute"`,
  surchargeable via la variable d'environnement `AUTH_RATE_LIMIT`, documenté dans le
  même style que `public_rate_limit` (défense par IP sur l'auth, complémentaire au
  lockout par compte ; jamais codé en dur ; assez haut pour ne pas gêner un
  utilisateur légitime qui se trompe de mot de passe, assez bas pour casser un
  balayage multi-comptes).

- **FR-1.2 — Fonction d'accès au seuil.** Ajouter dans
  `backend/app/core/rate_limit.py` une fonction `auth_rate_limit() -> str` qui lit
  `Settings.auth_rate_limit`, construite exactement comme `public_rate_limit()`. Ne
  pas modifier la portée ni la clé du limiteur existant.

- **FR-1.3 — Décoration des endpoints d'auth.** Décorer les trois endpoints de
  `backend/app/routers/auth.py` (`login`, `refresh`, `set_credentials`) avec
  `@limiter.limit(auth_rate_limit())`. Importer `limiter` et `auth_rate_limit` depuis
  `app.core.rate_limit`.

- **FR-1.4 — Signature de `refresh`.** Ajouter le paramètre `request: Request` à la
  signature de `refresh` (requis par SlowAPI pour dériver la clé IP). `login` et
  `set_credentials` le possèdent déjà et ne doivent pas changer de signature.

- **FR-1.5 — Pas de nouveau handler 429.** Ne rien ajouter dans `app/main.py` : le
  handler global `RateLimitExceeded → 429` (corps minimal) couvre déjà le cas.

- **FR-1.6 — Règle reverse proxy Nginx.** Dans `deploy/reverse-proxy/nginx.conf` :
  - ajouter, dans le contexte `http` à côté de `public_zone`, une zone
    `limit_req_zone $binary_remote_addr zone=auth_zone:10m rate=10r/m;` ;
  - ajouter un bloc `location /api/auth/ { limit_req zone=auth_zone burst=5 nodelay; proxy_pass … }`
    placé **avant** le `location /api/` générique (le plus spécifique d'abord) ;
  - répliquer les en-têtes proxy du bloc public (`Host`, `X-Real-IP`,
    `X-Forwarded-For`, `X-Forwarded-Proto`) ;
  - commenter dans le style du fichier (doctrine : limite au proxy en prod, repli
    SlowAPI en dev).

### Correctif 2 — Régénération de `docs/E2E_VERIFICATION.md`

- **FR-2.1 — Mesure post-Correctif 1.** Après implémentation du Correctif 1, relancer
  la suite backend complète (`.venv\Scripts\python.exe -m pytest -q` depuis `backend/`)
  et reporter le **nouveau** compte exact (supérieur à 221 du fait de
  `test_auth_rate_limit.py`). Relancer le frontend (`npm run test -- --run` depuis
  `frontend/`) et confirmer 143.

- **FR-2.2 — Mise à jour exhaustive des comptes.** Remplacer, partout dans le document
  (sections 2.1, 2.2, 4, 8 et toute autre mention), les comptes périmés par les
  comptes réels :
  - Backend mode unitaire (sans Postgres) : **nouveau total unitaire** (≥ 221) /
    skips correspondants.
  - Backend mode intégration (PostGIS réel) : total dérivé `unitaires + skips exécutés`
    (étiqueté comme dérivé des deux modes et confirmé en CI si le mode intégration
    n'est pas exécutable localement — voir A4).
  - Frontend : 26 fichiers, 143 tests, tous passés (re-confirmé).

- **FR-2.3 — Préservation du reste du document.** Ne pas altérer les autres contenus
  (traçabilité P1–P13, revue de sécurité, section « reproduire »). Seuls les nombres
  de tests et leurs phrases porteuses changent.

### Correctif 3 — Correction de la spec `tasks.md`

- **FR-3.1 — Libellé de la tâche 9.2.** Dans
  `.kiro/specs/production-readiness-gaps/tasks.md`, la tâche 9.2 reste cochée `[x]` et
  son texte « **(Hors lot courant — phase 2 ; introduit un modèle de données, donc non
  additif.)** … À planifier comme lot distinct » est remplacé par une note indiquant
  que la denylist `jti` + rotation de famille est **livrée**, avec les preuves :
  migration `0010_refresh_token.py`, service `refresh_tokens.py`
  (`record_issued_refresh`, `consume_refresh`), câblage `auth.py`, tests
  `test_refresh_denylist_db.py`.

- **FR-3.2 — Note de bas de fichier.** Ajuster la note « Découpage de la tâche 9 … 9.2
  est un lot de phase 2 car elle introduit un modèle de données (non additif) » (et la
  ligne de synthèse « 9.2 denylist, phase 2 ») pour refléter que 9.2 est désormais
  livrée.

- **FR-3.3 — Aucune modification de code.** Le Correctif 3 est strictement
  documentaire. Aucun fichier `backend/` n'est touché pour ce correctif.

## Non-Functional Requirements

- **NFR-1 — Changements additifs et non régressifs.** Aucun changement de comportement
  de l'auth existante hormis l'ajout du rate-limit. Les réponses existantes (401
  générique, 429 lockout, 410 setup-token, rotation refresh) restent inchangées.

- **NFR-2 — Conformité au style du projet.** Respecter ruff (line-length 100, target
  py312), docstrings en français, commentaires explicatifs riches comme dans le code
  existant. Répliquer fidèlement le pattern SlowAPI public plutôt qu'en inventer un.

- **NFR-3 — Isolation des tests.** Le limiteur SlowAPI est global et en mémoire ; les
  tests de rate-limit auth doivent réinitialiser son état (fixture `autouse`
  `limiter.reset()` avant/après chaque test) pour ne pas polluer les autres tests.

- **NFR-4 — Non-fuite d'information.** Le corps du 429 d'auth reste minimal et ne
  révèle aucune donnée interne/sensible (même exigence que le 429 public).

- **NFR-5 — Pas de modification de la config git.**

## Acceptance Criteria

1. `Settings` expose `auth_rate_limit` avec défaut `"10/minute"`, surchargeable via
   `AUTH_RATE_LIMIT`, documenté dans le style de `public_rate_limit`.
2. `app.core.rate_limit.auth_rate_limit()` existe, lit `Settings.auth_rate_limit`, et
   le limiteur existant garde sa portée et sa clé IP inchangées.
3. `login`, `refresh` et `set_credentials` sont décorés
   `@limiter.limit(auth_rate_limit())`, avec `limiter` et `auth_rate_limit` importés de
   `app.core.rate_limit`.
4. La signature de `refresh` inclut désormais `request: Request` ; les signatures de
   `login` et `set_credentials` sont inchangées.
5. Aucune modification dans `app/main.py` ; le 429 d'auth est servi par le handler
   SlowAPI global existant, avec corps minimal.
6. `nginx.conf` déclare `zone=auth_zone:10m rate=10r/m` dans `http`, et un
   `location /api/auth/ { limit_req zone=auth_zone burst=5 nodelay; … }` placé **avant**
   `location /api/`, avec les quatre en-têtes proxy répliqués et des commentaires de
   doctrine.
7. Un nouveau test `backend/app/tests/test_auth_rate_limit.py` vérifie, via
   `TestClient`, qu'au-delà du seuil `auth_rate_limit` configuré (surchargé à une
   petite valeur, p.ex. `2/minute`) `/api/auth/login` renvoie **429** de manière
   reproductible, et qu'un nombre de requêtes **sous** le seuil ne déclenche **pas** de
   429. Le test réinitialise l'état du limiteur avant/après chaque cas.
8. La suite backend complète (`.venv\Scripts\python.exe -m pytest -q` depuis
   `backend/`) est **verte** (0 échec), nouveaux tests de rate-limit auth inclus ; les
   suites d'auth existantes (`test_auth_router.py`, `test_auth_task7.py`,
   `test_refresh_denylist_db.py`) restent vertes.
9. `.venv\Scripts\python.exe -m ruff check .` depuis `backend/` ne signale aucune
   nouvelle violation.
10. La suite frontend (`npm run test -- --run` depuis `frontend/`) reste **verte**
    (143).
11. `docs/E2E_VERIFICATION.md` reflète, dans **toutes** les sections concernées (2.1,
    2.2, 4, 8 et autres mentions), les comptes **re-mesurés après le Correctif 1** :
    nouveau total backend unitaire (≥ 221) + skips, total intégration dérivé
    `unitaires + skips` (étiqueté/CI si non exécutable localement), frontend 26
    fichiers / 143 tests. Les anciens nombres (202/54, 256, 24 fichiers / 132 tests)
    n'apparaissent plus.
12. Le reste de `docs/E2E_VERIFICATION.md` (traçabilité P1–P13, revue de sécurité,
    section reproduire) est préservé.
13. Dans `tasks.md`, la tâche 9.2 est cochée `[x]` et son libellé ne mentionne plus
    « phase 2 / à planifier comme lot distinct » mais indique la livraison avec les
    preuves (migration 0010, `refresh_tokens.py`, `auth.py`, `test_refresh_denylist_db.py`).
14. La note de bas de fichier et la ligne de synthèse de `tasks.md` relatives au
    découpage de la tâche 9 reflètent que 9.2 est livrée (plus « phase 2 »).
15. Aucun fichier de code `backend/` n'est modifié au titre du Correctif 3 ; la config
    git n'est pas modifiée.

## Out of Scope

- Toute modification du comportement de l'auth autre que l'ajout du rate-limit
  (lockout par compte, 401/410 génériques, rotation refresh, audit) reste inchangée.
- Introduction d'un backend de compteur partagé (Redis) pour le rate-limit
  multi-instance : non visé ; le repli applicatif en mémoire suffit pour dev/tests/
  mono-instance, conformément à la doctrine existante.
- Modification de la portée ou de la clé du limiteur public existant, ou des routes
  `/api/public/*`.
- Réécriture du contenu de `docs/E2E_VERIFICATION.md` au-delà des comptes de tests.
- Toute évolution fonctionnelle de la denylist de refresh (Correctif 3 est purement
  documentaire).
- Configuration TLS/HSTS du reverse proxy (hors périmètre, déjà commentée dans
  `nginx.conf`).
