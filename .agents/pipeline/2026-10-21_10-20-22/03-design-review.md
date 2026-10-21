# Design Review — Trois correctifs plateforme DME (rate-limit auth, docs E2E, spec denylist)

Revue du design `02-design.md` contre `01-requirements.md`, avec vérification des
affirmations sur le code existant directement dans le dépôt
(`c:\PUBLIC\PROJECTS\MEDISYS\DME\projects\dme`).

**Verdict : CHANGES_REQUESTED** (2 HIGH, 3 MEDIUM, 4 NIT).

---

## Findings

### 1. [MEDIUM] Base de comptes incohérente : le design mélange le baseline « 221/56 » du contexte et le baseline réel « 202/54 » du document

**Où :** Correctif 2, « Procédure de mesure » et « Emplacements exacts à mettre à jour ».

Le design est contradictoire sur les chiffres de départ. La « Procédure de mesure »
affirme :

> relever le nouveau total unitaire (strictement > 221, de l'ordre de 221 + 2
> nouveaux cas = 223 …) et le nombre de skips (inchangé : 56 …)

Or le document réel `docs/E2E_VERIFICATION.md` (vérifié) contient **202 passés, 54
ignorés** (unitaire) et **256 passés** (intégration), pas 221/56. La section
« Emplacements exacts » du design, elle, cite correctement **202 / 54 / 256**. Les
valeurs « 221 », « 223 » et « 56 » proviennent du contexte de lancement des
requirements (`221 passed, 56 skipped`), **pas** du fichier à corriger. Suivre « skips
inchangé : 56 » écrirait un nombre faux (le fichier dit 54), et « ≈ 223 » part d'un
mauvais baseline (202, pas 221).

Le design atténue en disant « ne pas coder le chiffre en dur … confirmer par la mesure
réelle », mais les deux valeurs numériques affirmées (56, 223) restent trompeuses pour
l'implémenteur.

**Fix concret :** Remplacer dans « Procédure de mesure » toute référence à « 221 »,
« 223 » et « skips inchangé : 56 » par : « Le baseline du **document** est 202 passés /
54 ignorés (unitaire) et 256 (intégration) — distinct du compte du contexte de
lancement. Après ajout de `test_auth_rate_limit.py`, le nouveau total unitaire = (total
mesuré) ; le nombre de skips reste **54** (les nouveaux cas ne sont pas gardés par
`TEST_DATABASE_URL`) ; ne reporter que des chiffres **mesurés**. » Expliciter que
l'écart 202↔221 relève d'un baseline différent et n'est pas à réconcilier ici.

---

### 2. [HIGH] Arithmétique du total d'intégration erronée (invariant 202 + 54 = 256 cassé par la formule proposée)

**Où :** Correctif 2, « Procédure de mesure » et « Emplacements exacts à mettre à jour »
(§2.1 intégration).

Dans le document réel, l'invariant est : **unitaires passés + skips exécutés =
intégration passés** → `202 + 54 = 256`. Le design propose « total intégration dérivé =
`unitaires + skips exécutés` = `(nouveau total unitaire) + 56` ». Avec le mauvais
`56` (cf. finding 1) et un nouveau total unitaire fondé sur 202, le calcul donne un
nombre incohérent avec le fichier. De plus, `test_auth_rate_limit.py` **n'est pas**
gardé par `TEST_DATABASE_URL` (le design l'affirme lui-même : « les nouveaux cas ne
sont pas gardés »), donc ses 2 cas comptent **dans les deux modes** ; ils s'ajoutent au
total intégration en tant que *passés*, pas en tant que *skips*.

Formule correcte avec les vrais nombres : si N₂ = nouveaux cas unitaires ajoutés (≈ 2),
alors unitaire passés = `202 + N₂`, skips = `54` (inchangé), intégration passés =
`(202 + N₂) + 54`. L'égalité `unit_passés + skips = intégration_passés` doit continuer
de tenir.

**Fix concret :** Remplacer la formule par : « Intégration passés = (nouveau total
unitaire passés) + 54 (skips), car les cas précédemment skippés s'exécutent et les 2
nouveaux cas de rate-limit passent dans les deux modes. Vérifier que l'identité
`unitaire_passés + 54 = intégration_passés` tient sur les nombres mesurés. » Corriger
« + 56 » en « + 54 ».

---

### 3. [HIGH] La fixture conftest `_reset_rate_limiter` (reset AVANT chaque test) peut casser `test_public_rate_limit_cache.py`

**Où :** Correctif 1, « Testabilité » → « Décision : fixture `autouse`
`limiter.reset()` au niveau conftest, scope fonction ».

Le design conclut que deux fixtures `autouse` qui resettent le même limiteur sont
« idempotentes et sans conflit (reset puis reset = reset) ». C'est vrai **à l'instant du
setup**, mais l'ordre d'exécution des fixtures `autouse` de même scope n'est pas
garanti d'être inoffensif ici : `test_public_rate_limit_cache.py` dépend d'un compteur
**qui démarre à zéro au début du test puis accumule 60+ requêtes sans reset
intermédiaire**. Les deux fixtures resettent uniquement au setup/teardown, pas pendant
le corps du test — donc l'accumulation intra-test est préservée. **Ce point précis est
sûr.**

Le risque réel est différent et non traité : la fixture conftest est `autouse` **sur
toute la suite**, y compris les tests qui **n'utilisent pas** `TestClient` mais
importent/instancient le limiteur indirectement, et surtout les tests où `limiter`
n'est pas encore importable au moment du reset si l'ordre de collecte diffère. L'import
`from app.core.rate_limit import limiter` dans la fixture est tardif (bon), mais
`limiter.reset()` suppose que le storage SlowAPI expose `reset()` — vérifié utilisé par
`test_public_rate_limit_cache.py`, donc l'API existe. **Ce point est également sûr.**

Ce qui reste non vérifié et doit l'être : le design n'établit pas que `limiter.reset()`
est **sans effet** sur un test qui a configuré des `dependency_overrides` mais pas
encore émis de requête — c'est le cas, `reset()` ne touche que le storage. 

**Conclusion de la vérification :** après analyse, l'interaction est sûre, MAIS le
design justifie sa sûreté par un argument incomplet (« reset puis reset = reset »), qui
ne couvre pas le vrai risque (accumulation intra-test). Le design doit **prouver**
explicitement que les deux fixtures ne resettent qu'aux bornes du test et jamais
pendant, sinon un lecteur pourrait croire la justification suffisante alors qu'elle ne
l'est pas.

**Fix concret :** Remplacer la justification par : « Les deux fixtures ne resettent
qu'au **setup** (et, pour la fixture locale, au **teardown**) ; aucune ne reset
*pendant* le corps d'un test. L'accumulation intra-test dont dépend
`test_public_rate_limit_cache.py` (60+ requêtes dans la même fenêtre) est donc
préservée. Ordre des deux `autouse` de scope fonction indifférent : deux resets
consécutifs au setup laissent le compteur à zéro avant la première requête du test. »
Ajouter une assertion de non-régression : le test public reste vert après l'ajout de la
fixture conftest (déjà dans la vérification finale, mais à lier explicitement à ce
risque).

---

### 4. [MEDIUM] `test_auth_router.py` / `test_auth_task7.py` n'ont PAS de reset local ; la non-régression repose entièrement sur la nouvelle fixture conftest — à confirmer pour `set-credentials`

**Où :** Correctif 1, « Testabilité » → comptage des requêtes d'auth.

Vérifié dans le code : `test_auth_router.py` émet **9** `POST /api/auth/login`, **3**
`POST /api/auth/refresh` et **4** `POST /api/auth/set-credentials` ; `test_auth_task7.py`
émet **2** login (via deux `TestClient` distincts dans un même test) + un `/needs-admin`
non concerné. Aucun de ces deux fichiers n'a de fixture de reset du limiteur
aujourd'hui. Le comptage SlowAPI est **par (IP, fonction décorée)** : sous `testclient`
(IP unique), `set_credentials` recevrait 4 appels, `refresh` 3, `login` 9 — tous sous
le défaut 10/min **par endpoint**, mais `login` à 9 est **à une unité** du seuil 10.
Sans la fixture conftest, l'ajout du décorateur ferait basculer tout 10ᵉ appel de login
du process en 429.

Le design choisit la bonne remédiation (fixture conftest reset-avant), ce qui ramène
chaque test à zéro et neutralise le risque. **Mais** il sous-estime en écrivant « login
seul ≈ 9–11 appels — déjà à la limite » : le vrai chiffre est exactement **9** sur
`test_auth_router.py` plus **2** sur `test_auth_task7.py`, et surtout ces deux fichiers
exécutent chaque test dans un `TestClient` **neuf** sans que cela remette le compteur
mémoire à zéro (le limiteur est global, pas lié au client). La fixture conftest est donc
**nécessaire**, pas seulement prudente — le design le dit, mais son comptage approximatif
(« ≈ 9–11 ») pourrait laisser penser qu'une marge existe.

**Fix concret :** Remplacer les comptages approximatifs par les comptages exacts
vérifiés (login : 9 dans `test_auth_router.py` + 2 dans `test_auth_task7.py` ; refresh :
3 ; set-credentials : 4) et affirmer sans ambiguïté : « La fixture conftest est
**obligatoire** (pas seulement prudente) ; sans elle, le 10ᵉ login du process bascule en
429 et casse `test_auth_router.py`. » Ajouter un critère de vérification explicite :
relancer `test_auth_router.py` et `test_auth_task7.py` **isolément ET dans la suite
complète** pour prouver l'indépendance à l'ordre.

---

### 5. [MEDIUM] Position de `request: Request` dans `refresh` : le design dit « après payload, avant session comme login/set_credentials » — à confirmer que SlowAPI accepte un paramètre non-premier

**Où :** Correctif 1, Fichier 3 (c) « Signature de `refresh` ».

Vérifié : dans `public.py`, `request: Request` est le **premier** paramètre du handler
(`async def list_regions(request: Request, response, session=...)`). Dans `auth.py`,
`login` et `set_credentials` ont `request` en **deuxième** position (après `payload`).
SlowAPI localise `request` par **introspection par nom/type**, pas par position — donc
la position importe peu fonctionnellement. Le design propose `payload, request, session`
pour `refresh`, cohérent avec `login`. C'est correct.

Le point non vérifié par le design : `login` et `set_credentials` **utilisent**
`request` dans leur corps (audit via `build_event(request, …)`), alors que `refresh`
**ne l'utilisera pas**. Le design l'affirme (« `request` n'est pas utilisé … il n'est là
que pour SlowAPI ») et conclut qu'aucune règle ruff ne le signale. Vérifié dans
`pyproject.toml` : `[tool.ruff.lint]` n'a **pas** de bloc `select`, donc seules les
règles par défaut (E, F) sont actives ; ARG (`flake8-unused-arguments`) n'est pas
activée, et F841 ne vise que les variables locales, pas les paramètres. **La claim est
exacte.**

**Fix concret :** Aucun changement de code requis, mais rendre la vérification
reproductible : ajouter à la vérification finale « `ruff check .` ne signale pas le
paramètre `request` inutilisé de `refresh` (règle ARG non activée, confirmé dans
`pyproject.toml` : aucun `select` personnalisé) ». Ceci verrouille l'hypothèse contre
une future activation d'ARG.

---

### 6. [NIT] Le design dit « placer `location /api/auth/` physiquement avant `/api/` » mais note aussi que Nginx choisit le préfixe le plus long — clarifier que l'ordre physique est cosmétique

**Où :** Correctif 1, Fichier 5 (b).

Le design écrit à la fois « placé AVANT `location /api/` » et « Nginx retient le préfixe
le plus spécifique via le préfixe le plus long ; mais pour éviter toute ambiguïté … on
le place physiquement avant ». Les deux phrases sont correctes mais redondantes et
peuvent laisser croire que l'ordre physique a un effet fonctionnel (il n'en a pas pour
les `location` préfixe). Le `nginx.conf` vérifié utilise bien des `location` préfixe
(pas regex), donc le plus long l'emporte quel que soit l'ordre.

**Fix concret :** Reformuler : « L'ordre physique est purement une aide à la lecture :
pour des `location` **préfixe** (non-regex), Nginx choisit toujours le préfixe le plus
long (`/api/auth/` > `/api/`) indépendamment de l'ordre. On le place néanmoins avant
`/api/` par lisibilité. »

---

### 7. [NIT] Incohérence de la fenêtre proxy vs repli : `rate=10r/m` (Nginx) ≈ « 10/minute » (SlowAPI) mais `burst=5` n'a pas d'équivalent applicatif

**Où :** Correctif 1, Fichier 5 (a)/(b).

Nginx `rate=10r/m` + `burst=5 nodelay` tolère une rafale de 5 au-delà du rythme lissé,
tandis que le repli SlowAPI « 10/minute » applique une fenêtre glissante/fixe sans
burst. Les deux défenses ne sont donc pas strictement équivalentes en comportement de
bord. Le design ne le mentionne pas. Ce n'est pas un bug (les deux satisfont « casser un
balayage »), mais un lecteur pourrait croire à une équivalence exacte.

**Fix concret :** Ajouter une phrase : « Le `burst=5` Nginx n'a pas d'équivalent dans le
repli SlowAPI (`limits` ne modélise pas de burst séparé) ; les deux couches ne sont pas
bit-à-bit identiques en tolérance de rafale, mais remplissent le même objectif. C'est
acceptable (la doctrine fait du proxy la couche de référence en prod). »

---

### 8. [NIT] Le design cite `httpx2` pour le `TestClient` — vérifié, mais la claim « pytest-asyncio + httpx2 » dans l'Overview est imprécise

**Où :** Overview, « pile technologique ».

Vérifié dans `pyproject.toml` : la dépendance de test est bien `httpx2>=2.13` (et non
`httpx`), adoptée en G-8. La claim est donc exacte. Imprécision mineure : le
`TestClient` provient de Starlette/FastAPI et s'appuie sur `httpx2` en interne ; le
design le présente comme si `httpx2` était directement l'outil de test. Sans impact.

**Fix concret :** Optionnel — préciser « `TestClient` (Starlette), adossé à `httpx2` ».

---

### 9. [NIT] `filterwarnings = error::DeprecationWarning` non mentionné comme risque pour le nouveau test / le décorateur SlowAPI

**Où :** Correctif 1, Testabilité ; Vérification finale.

Vérifié dans `pyproject.toml` : `filterwarnings` transforme tout
`DeprecationWarning`/`PendingDeprecationWarning`/`StarletteDeprecationWarning` en
**erreur**. SlowAPI 0.1.10 est déjà exercé par `test_public_rate_limit_cache.py` sans
déclencher d'erreur de dépréciation, donc étendre le décorateur à l'auth n'introduit pas
de risque nouveau. Le design ne le mentionne pas ; ce n'est pas bloquant, mais la
contrainte mérite d'être nommée pour que l'implémenteur ne soit pas surpris si un
warning apparaît.

**Fix concret :** Ajouter à la Vérification finale : « La suite tourne avec
`filterwarnings=error::DeprecationWarning` ; le nouveau test et les décorateurs
`@limiter.limit` ne doivent émettre aucune dépréciation (SlowAPI 0.1.10 déjà éprouvé sur
`/api/public/*`). »

---

## Verified Assumptions

Vérifiées directement dans le code du dépôt et **exactes** :

1. **`refresh` ne reçoit PAS `request: Request`.** Confirmé : `auth.py`,
   `async def refresh(payload, session=Depends(get_session))`. `login` et
   `set_credentials` ont bien `request: Request` en 2ᵉ position.
2. **`rate_limit.py` expose `limiter = Limiter(key_func=get_remote_address)` et
   `public_rate_limit()` lisant `_settings = get_settings()`.** Confirmé mot pour mot.
3. **`public.py` décore chaque route par `@limiter.limit(public_rate_limit())` sous
   `@router.get(...)`, avec `request: Request` en paramètre.** Confirmé.
4. **`main.py` enregistre `app.state.limiter = limiter` et
   `add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)`, sans
   `default_limits`.** Confirmé. Le commentaire de portée « STRICTEMENT limitée à
   `/api/public/*` » existe bien et deviendrait faux — l'ajustement prévu est justifié.
5. **`config.py` porte `public_rate_limit: str = "60/minute"` surchargeable via
   `PUBLIC_RATE_LIMIT` (`SettingsConfigDict(case_sensitive=False)`).** Confirmé ; le
   mécanisme de surcharge par env pour `auth_rate_limit`/`AUTH_RATE_LIMIT` est analogue
   et valide (type `str`, pas de validateur CSV).
6. **`test_public_rate_limit_cache.py` : fixture `autouse` `_reset_limiter` (reset avant
   ET après), helper `_limit_count()` parsant le seuil, `TestClient`, surcharge de
   `dependency_overrides` + monkeypatch du service.** Confirmé ; le modèle est
   transposable comme le décrit le design.
7. **`conftest.py` ne contient que des fixtures `autouse` d'infrastructure (`_fast_argon2`
   session-scope, `_fast_health_probe` function-scope).** Confirmé ; y ajouter une
   fixture d'isolation du limiteur est cohérent.
8. **Denylist refresh livrée :** migration `0010_refresh_token.py` (table sans RLS,
   intentionnel, CRUD explicite pour `dme_app`), service `refresh_tokens.py`
   (`record_issued_refresh`, `consume_refresh` avec `UPDATE … RETURNING` atomique +
   invalidation de famille au rejeu), câblage dans `auth.py` (login émet `jti`+`family_id`
   et appelle `record_issued_refresh` ; refresh consomme, refuse les hérités sans `jti`,
   rote avec nouveau `jti`/même `family_id`), tests `test_refresh_denylist_db.py` (gardés
   par `TEST_DATABASE_URL`). Toutes ces preuves existent — Correctif 3 est factuellement
   fondé.
9. **`tasks.md` : la tâche 9.2 est cochée `[x]` mais libellée « (Hors lot courant —
   phase 2 …) … À planifier comme lot distinct » ; la note « Découpage de la tâche 9 … 9.2
   … lot de phase 2 » et la ligne ASCII « 9.2 denylist, phase 2 » existent.** Confirmé.
   Le JSON de planification ne porte aucune mention « phase 2 » (il a `wave`/`severity`),
   donc la décision du design de ne pas le toucher est correcte.
10. **`nginx.conf` : `limit_req_zone … zone=public_zone:10m rate=60r/m`,
    `limit_req_status 429`, `location /api/public/` (`burst=30 nodelay`), `location /api/`
    (avec commentaire affirmant que `/api/auth` ne passe PAS par `limit_req`),
    `location /health`, 4 en-têtes proxy.** Confirmé ; le commentaire de `/api/` deviendra
    faux et son ajustement est justifié.
11. **ruff : `line-length = 100`, `target-version = "py312"`, seul `flake8-bugbear`
    configuré, aucun `select` personnalisé (donc ARG non activée).** Confirmé dans
    `pyproject.toml`. La claim « `request` inutilisé non signalé » est exacte.
12. **SlowAPI épinglé `0.1.10`.** Confirmé dans `pyproject.toml`.

## Unverified / Wrong Assumptions

1. **FAUX / INCOHÉRENT — Baseline des comptes E2E (findings 1 & 2).** Le design affirme
   « > 221 », « ≈ 223 », « skips inchangé : 56 » dans la « Procédure de mesure », alors
   que le fichier réel `docs/E2E_VERIFICATION.md` porte **202 passés / 54 ignorés** et
   **256** en intégration. Les valeurs 221/56/223 proviennent du contexte de lancement
   des requirements, pas du document. La formule « intégration = unitaire + 56 » casse
   l'invariant réel `202 + 54 = 256`. À corriger (voir findings 1 et 2).
2. **NON QUANTIFIÉ PRÉCISÉMENT — Comptage des requêtes d'auth (finding 4).** Le design
   écrit « login seul ≈ 9–11 appels — déjà à la limite ». Comptage exact vérifié : 9
   login dans `test_auth_router.py` + 2 dans `test_auth_task7.py`, 3 refresh, 4
   set-credentials. La conclusion (fixture conftest nécessaire) reste correcte, mais le
   chiffre approximatif doit être remplacé par le compte exact.
3. **JUSTIFICATION INCOMPLÈTE (vérifiée sûre au final) — Interaction des deux fixtures
   `autouse` (finding 3).** La claim « reset puis reset = reset » ne couvre pas le vrai
   risque (accumulation intra-test de `test_public_rate_limit_cache.py`). Après
   vérification, l'interaction est effectivement sûre (aucune des deux fixtures ne reset
   pendant le corps du test), mais la justification du design doit être corrigée pour le
   bon motif.
