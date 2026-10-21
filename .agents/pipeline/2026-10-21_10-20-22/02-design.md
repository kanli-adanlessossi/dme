# Design — Trois correctifs plateforme DME (rate-limit auth, docs E2E, spec denylist)

## Overview

Ce lot livre trois correctifs ciblés, additifs et non régressifs, dans le dépôt
`c:\PUBLIC\PROJECTS\MEDISYS\DME\projects\dme`, sans worktree.

1. **Correctif 1 — rate-limit par IP sur l'auth.** On réplique à l'identique le
   pattern SlowAPI déjà en place pour `/api/public/*` (décorateur
   `@limiter.limit(...)` + seuil configurable lu à chaque appel + handler 429 global
   déjà enregistré), en l'étendant aux trois endpoints d'auth, et on ajoute la règle
   miroir au reverse proxy Nginx. C'est une défense **par IP** complémentaire au
   verrouillage **par compte** déjà présent (`app/services/lockout.py`) : elle casse
   le balayage multi-comptes (un essai par compte, jamais de verrou par compte).

2. **Correctif 2 — régénération de `docs/E2E_VERIFICATION.md`.** Mise à jour
   purement documentaire des comptes de tests, **re-mesurés après** le Correctif 1
   (le nouveau fichier de test fait croître le total backend).

3. **Correctif 3 — correction de la spec `tasks.md`.** La tâche 9.2 (denylist `jti`
   + rotation de famille) est en réalité livrée et testée : on corrige le libellé
   « phase 2 » et la note de bas de fichier. Aucune modification de code.

La pile technologique est **verrouillée** et strictement celle déjà en place : Python
3.12, FastAPI ≥ 0.115, SlowAPI **0.1.10** (épinglé, repli de rate-limit), Pydantic
Settings, pytest + pytest-asyncio + `httpx2` pour le `TestClient`, ruff (line-length
100, target py312), Nginx pour le reverse proxy. **Aucune nouvelle dépendance** n'est
introduite (ni Redis, ni autre). Le repli SlowAPI reste en mémoire par instance,
conformément à la doctrine du projet (limite au proxy en prod, repli applicatif en
dev/tests/mono-instance).

Le fil conducteur de tout le Correctif 1 est : **ne rien inventer, tout calquer sur
`/api/public/*`.** Les quatre points d'ancrage vérifiés dans le dépôt rendent cela
mécanique :

- `app/core/rate_limit.py` expose `limiter = Limiter(key_func=get_remote_address)`
  (clé par IP cliente) et `public_rate_limit() -> str` qui lit `Settings` ;
- `app/core/config.py` porte `public_rate_limit: str = "60/minute"` avec un
  commentaire de doctrine ;
- `app/main.py` enregistre déjà `app.state.limiter = limiter` et
  `add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)` ;
- `app/routers/public.py` décore chaque route par `@limiter.limit(public_rate_limit())`
  avec `request: Request` en premier paramètre du handler.

## Correctif 1 — Rate-limit par IP sur l'auth

### Décision : calque exact du pattern public, pas de variante

Deux approches étaient envisageables pour factoriser le seuil. (a) Ajouter une
fonction dédiée `auth_rate_limit()` symétrique de `public_rate_limit()`. (b) Généraliser
en une fabrique paramétrée `rate_limit(attr_name)`. **On choisit (a).** Raison : les
requirements l'imposent explicitement (FR-1.2 : « construite exactement comme
`public_rate_limit()` »), c'est le pattern que le lecteur du code reconnaît déjà, et
une généralisation prématurée pour deux seuils seulement ajouterait de l'indirection
sans bénéfice. La cohérence de style prime sur la factorisation.

De même, on applique le même décorateur `@limiter.limit(auth_rate_limit())` aux trois
endpoints plutôt que d'introduire un `shared_limit` ou un regroupement : SlowAPI
compte par (clé IP, **fonction** décorée) — chaque endpoint a donc son propre compteur.
C'est le comportement observable du pattern public (chaque route publique a son
compteur) et il est **suffisant** pour l'objectif : casser un balayage de `login`.
Un compteur partagé entre les trois routes n'est pas requis par les requirements et
n'est pas retenu (il compliquerait le test et le raisonnement sans gain de sécurité
pour la menace visée).

### Fichier 1 — `backend/app/core/config.py`

Ajouter un champ à la classe `Settings`, immédiatement **après** le bloc
`public_rate_limit` (regroupement thématique « limitation de débit »), avec un
commentaire de doctrine calqué sur celui de `public_rate_limit` :

```python
    # --- Limitation de débit des routes d'authentification (par IP) -------------
    # Défense PAR IP sur `/api/auth/*`, COMPLÉMENTAIRE au verrouillage PAR COMPTE
    # (`app/services/lockout.py`, 5 échecs → verrou 15 min). Le lockout par compte
    # ne voit pas un balayage multi-comptes (un seul essai par compte, jamais de
    # seuil atteint sur un compte donné) ; cette limite par IP casse justement ce
    # balayage. Même doctrine que `public_rate_limit` : la limite vit au reverse
    # proxy en production (Nginx `limit_req`, zone `auth_zone`, voir
    # `deploy/reverse-proxy/`) ; ce réglage pilote le REPLI applicatif (SlowAPI)
    # qui rend le 429 reproductible sans proxy (dev, tests, mono-instance). Format
    # « <nombre>/<fenêtre> » (syntaxe `limits`). Jamais codé en dur :
    # surchargeable via `AUTH_RATE_LIMIT`. Défaut « 10/minute » : assez HAUT pour
    # ne pas gêner un utilisateur légitime qui se trompe de mot de passe quelques
    # fois, assez BAS pour casser un balayage multi-comptes depuis une même source.
    auth_rate_limit: str = "10/minute"
```

Le mécanisme de surcharge par environnement est déjà fourni par `BaseSettings` /
`SettingsConfigDict(case_sensitive=False)` : le champ `auth_rate_limit` est
automatiquement surchargeable par la variable `AUTH_RATE_LIMIT` (comme
`public_rate_limit` ↔ `PUBLIC_RATE_LIMIT`). Aucun validateur ni code supplémentaire
n'est nécessaire — le type est `str`, aucune coercition CSV ne s'applique.

### Fichier 2 — `backend/app/core/rate_limit.py`

Ajouter une fonction symétrique de `public_rate_limit()`, après elle :

```python
def auth_rate_limit() -> str:
    """Renvoie la chaîne de limite appliquée aux routes d'authentification.

    Lue depuis `Settings.auth_rate_limit` (défaut « 10/minute ») à chaque appel du
    décorateur, de sorte qu'un ajustement de configuration suffit à changer le
    seuil sans modifier le code des routes. Défense par IP complémentaire du
    verrouillage par compte (voir le commentaire de `Settings.auth_rate_limit`).
    """
    return _settings.auth_rate_limit
```

On réutilise l'instance mémoïsée `_settings = get_settings()` déjà présente en tête
de module (exactement comme `public_rate_limit()`). On **ne modifie ni la portée ni
la clé** du `limiter` existant : `Limiter(key_func=get_remote_address)` reste tel
quel, aucun `default_limits` n'est ajouté. La docstring de module décrit la portée
comme « seules les routes `/api/public/*` sont limitées » ; **mettre à jour cette
docstring** pour refléter que les routes d'auth sont désormais aussi limitées (voir
« Cohérence documentaire du module » ci-dessous), sans quoi le commentaire deviendrait
faux.

Note sur le moment de lecture du seuil : `_settings` est lue **à l'import** du module.
C'est le même comportement que `public_rate_limit()`. Les décorateurs `@limiter.limit(...)`
évaluent leur argument **au moment de la décoration** (à l'import du routeur), donc le
seuil est figé pour la durée du process. Ceci a une conséquence directe sur la
stratégie de test (voir « Testabilité ») : on ne peut pas simplement muter
`get_settings().auth_rate_limit` au runtime et espérer que le décorateur déjà posé en
tienne compte. La technique de référence (`test_public_rate_limit_cache.py`) contourne
cela en **lisant** le seuil configuré (`_limit_count()`) et en émettant juste ce
qu'il faut de requêtes pour le franchir — le test de rate-limit auth fera de même.

### Fichier 3 — `backend/app/routers/auth.py`

Trois modifications strictement additives.

**(a) Import.** Ajouter, dans le groupe d'imports `app.core.*` :

```python
from app.core.rate_limit import auth_rate_limit, limiter
```

**(b) Décoration des trois endpoints.** Poser `@limiter.limit(auth_rate_limit())`
comme **décorateur le plus proche de la fonction** (sous le `@router.post(...)`),
exactement comme dans `public.py` où `@limiter.limit(public_rate_limit())` suit
`@router.get(...)`. Ordre pour chaque endpoint :

```python
@router.post("/login", ...)
@limiter.limit(auth_rate_limit())
async def login(payload: LoginRequest, request: Request, ...):
    ...

@router.post("/refresh", ...)
@limiter.limit(auth_rate_limit())
async def refresh(payload: RefreshRequest, request: Request, ...):
    ...

@router.post("/set-credentials", ...)
@limiter.limit(auth_rate_limit())
async def set_credentials(payload: SetCredentialsRequest, request: Request, ...):
    ...
```

**(c) Signature de `refresh`.** `login` et `set_credentials` possèdent déjà
`request: Request` et ne changent pas. `refresh` ne l'a pas : ajouter le paramètre.
SlowAPI exige que le handler expose un paramètre nommé `request` de type `Request`
pour dériver la clé IP (il le recherche par introspection ; son absence lève une
erreur SlowAPI au runtime). Position : on insère `request: Request` **après** `payload`
et **avant** `session` pour refléter l'ordre déjà établi dans `login`/`set_credentials`
(payload métier, puis `request`, puis dépendances injectées) :

```python
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
```

`request` n'est **pas utilisé** dans le corps de `refresh` (contrairement à `login`
qui s'en sert pour l'audit) ; il n'est là que pour SlowAPI. Ce n'est pas un argument
inutilisé au sens de ruff (il est consommé par le décorateur via introspection), et
le même motif existe déjà dans `public.py` (`list_regions(request: Request, ...)` où
`request` sert uniquement au limiteur). Aucune règle ruff ne le signale (ARG n'est pas
activée ; la config ne liste que `flake8-bugbear`).

**Invariant préservé (NFR-1).** La logique interne des trois handlers (401 générique,
429 lockout + `Retry-After`, 410 setup-token, rotation refresh, audit) est **inchangée**.
Le décorateur n'intervient qu'**avant** l'entrée du handler : si le seuil IP est
dépassé, SlowAPI lève `RateLimitExceeded` et le handler n'est jamais exécuté. Les
chemins existants restent bit-à-bit identiques sous le seuil.

### Fichier 4 — `backend/app/main.py` : aucune modification

Le 429 est rendu par le handler global déjà enregistré
(`add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)` sur
`app.state.limiter = limiter`). Ce handler renvoie un **429 au corps minimal** sans
fuite d'information — exactement le corps que `test_public_rate_limit_cache.py` vérifie
déjà (clé `error`, aucun champ interne). On **ne touche pas** à `main.py` (FR-1.5).
Le commentaire de `main.py` décrit la portée du limiteur comme strictement limitée à
`/api/public/*` ; voir « Cohérence documentaire du module » pour l'ajustement de ce
commentaire (il deviendrait faux sinon), qui est le seul changement envisagé dans
`main.py` et reste purement textuel.

### Fichier 5 — `deploy/reverse-proxy/nginx.conf`

Trois ajouts calqués sur l'existant `public_zone`.

**(a) Zone**, dans le contexte `http`, juste après la ligne `public_zone` (avant
`limit_req_status 429;` qui reste partagé par les deux zones) :

```nginx
# Zone de limitation PAR IP des routes d'authentification (`/api/auth/*`). Même
# doctrine que `public_zone` : la limite vit au proxy en production ; le repli
# SlowAPI (`backend/app/core/rate_limit.py`, `auth_rate_limit()`) couvre le cas
# « aucun proxy » (dev/tests). Défense par IP complémentaire du verrouillage par
# compte applicatif : casse un balayage multi-comptes depuis une même source.
#   - `zone=auth_zone:10m` : ~160 000 IP suivies, large marge.
#   - `rate=10r/m` : 10 requêtes/min/IP, aligné sur `AUTH_RATE_LIMIT` (défaut
#     « 10/minute ») du repli applicatif.
limit_req_zone $binary_remote_addr zone=auth_zone:10m rate=10r/m;
```

**(b) Bloc `location /api/auth/`**, placé **AVANT** `location /api/` (Nginx retient
le préfixe le plus spécifique via `location` préfixe le plus long ; mais pour éviter
toute ambiguïté de lecture et refléter l'intention, on le place physiquement avant le
bloc générique), avec les **quatre** en-têtes proxy du bloc public répliqués :

```nginx
# --- Routes d'authentification : LIMITÉES PAR IP (complément du lockout) ----------
# `burst=5 nodelay` tolère une courte rafale (p.ex. un utilisateur qui corrige
# rapidement son mot de passe) sans relâcher le plafond moyen de 10 r/min/IP.
# Au-delà de la rafale → 429 (cf. `limit_req_status`). Ce bloc DOIT précéder le
# `location /api/` générique : le préfixe le plus spécifique l'emporte.
location /api/auth/ {
    limit_req zone=auth_zone burst=5 nodelay;

    proxy_pass http://dme_api;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

**(c) Commentaire du bloc `location /api/` existant.** Il affirme aujourd'hui que
`/api/auth` « ne passe PAS par `limit_req` ». Après ce correctif c'est faux : ajuster
ce commentaire pour dire que `/api/auth/` est désormais limité par `auth_zone` (bloc
dédié ci-dessus) et que les autres routes authentifiées restent non limitées par ces
zones. Changement purement textuel, aucun effet fonctionnel.

Ce fichier n'est pas exécuté par les tests (c'est un artefact de déploiement) ; sa
correction est vérifiée par relecture et par le fait que la grammaire reste celle,
déjà présente, du bloc `public_zone`.

### Fichier 6 (nouveau) — `backend/app/tests/test_auth_rate_limit.py`

Voir « Testabilité » pour la stratégie détaillée.

### Cohérence documentaire du module (obligatoire, sinon commentaires faux)

Trois commentaires/docstrings affirment aujourd'hui que **seules** les routes
publiques sont limitées. Les laisser tels quels rendrait la documentation mensongère
après le Correctif 1. On les ajuste (texte uniquement, zéro effet runtime) :

1. **`backend/app/core/rate_limit.py`** — docstring de module (« Portée volontairement
   étroite … Seules les routes `/api/public/*` sont limitées … les routes
   authentifiées (`auth`, …) ne sont jamais limitées ») : ajouter que `/api/auth/*`
   est désormais limité par IP (défense complémentaire du lockout par compte), toujours
   par décorateur explicite (jamais globalement), même clé IP, même handler 429.
2. **`backend/app/main.py`** — commentaire du bloc limiteur (« Portée STRICTEMENT
   limitée à `/api/public/*` … les routes authentifiées (`auth`, …) ne sont JAMAIS
   limitées ») : corriger pour inclure `/api/auth/*`, en précisant que c'est toujours
   par décorateur sur les seules routes concernées (pas de `default_limits`, pas de
   middleware global), et que `sync/center/patients/visits/admin` + `/health` restent
   non limités.
3. **`deploy/reverse-proxy/nginx.conf`** — commentaire du bloc `location /api/`
   (point (c) ci-dessus).

## Error handling (Correctif 1)

Le seul chemin d'erreur **nouveau** est le dépassement de seuil IP. Tous les autres
chemins d'erreur des trois endpoints sont inchangés.

| Opération | Condition d'échec | Récupérable ? | Reçu par l'appelant | Journalisation |
| --- | --- | --- | --- | --- |
| Décorateur `@limiter.limit` | Dépassement du seuil IP (> `auth_rate_limit` dans la fenêtre) | Oui (attendre la fenêtre) | **429**, corps minimal `{"error": …}` (handler SlowAPI global), aucun champ interne/sensible | Pas de journalisation applicative dédiée ; SlowAPI ne logge rien par défaut, et on n'ajoute rien. Côté proxy, Nginx logge selon sa config standard (`error_log`). |
| `login` sous le seuil | Identifiants invalides / tenant inactif | — | **401 générique** (inchangé) | `audit_log` `login.failure` (inchangé) |
| `login` sous le seuil | Compte verrouillé (lockout par compte) | Oui (après `Retry-After`) | **429** + `Retry-After` (inchangé) | `audit_log` `login.lockout` (inchangé) |
| `refresh` sous le seuil | Refresh invalide/expiré/rejeu/hérité sans `jti` | — | **401 générique** (inchangé) | aucune (inchangé) |
| `set_credentials` sous le seuil | Jeton invalide/expiré/consommé | — | **410 générique** (inchangé) | aucune (inchangé) |

Point d'attention : deux sources de **429** coexistent désormais sur `login` — le 429
de **lockout par compte** (avec `Retry-After`, levé *dans* le handler) et le 429 de
**rate-limit par IP** (sans `Retry-After` du handler, levé *avant* le handler par
SlowAPI). Ce sont deux défenses distinctes et complémentaires ; aucune ne masque
l'autre de façon problématique (si la limite IP est atteinte, le handler n'est pas
exécuté, donc pas de calcul de lockout — c'est acceptable et attendu). Les deux
renvoient 429, ce qui est sémantiquement correct (« trop de requêtes »). Aucune fuite
d'information : ni le corps du 429 SlowAPI (minimal) ni celui du 429 lockout
(message générique, sans révéler l'existence du compte) ne distinguent les cas d'une
façon exploitable.

## Validation des entrées (Correctif 1)

- **Seuil `auth_rate_limit` (env `AUTH_RATE_LIMIT`).** Type `str`, format
  `"<entier>/<fenêtre>"` (syntaxe de la lib `limits` sous-jacente à SlowAPI, p.ex.
  `"10/minute"`, `"100/hour"`). **Optionnel** (défaut `"10/minute"`). Pas de
  validateur applicatif ajouté : on calque `public_rate_limit` qui n'en a pas. En cas
  de valeur malformée, la lib `limits` lève à la décoration (import du routeur) — échec
  rapide et visible au démarrage/à la collecte des tests, cohérent avec `public_rate_limit`.
  C'est le comportement voulu : une configuration de seuil invalide doit casser tôt,
  pas silencieusement.
- **Clé de limitation.** `get_remote_address` (IP cliente) — inchangée, propriété du
  `limiter` partagé. Derrière le proxy, l'IP réelle est portée par `X-Forwarded-For` /
  `X-Real-IP` (répliqués dans le bloc Nginx) ; le repli SlowAPI local, lui, voit l'IP
  directe du `TestClient` (`testclient`) — ce qui suffit pour le test (une seule clé).

## Invariants et couche propriétaire (Correctif 1)

- **Invariant : « seules les routes explicitement décorées sont limitées ».** Propriété
  du **limiteur SlowAPI** (aucun `default_limits`, aucun middleware global) ; l'ajout
  du décorateur sur les trois routes d'auth est l'unique élargissement de portée. Les
  routes `sync/center/patients/visits/admin` et `/health` restent non décorées, donc
  non limitées — vérifiable par lecture (aucun décorateur) et non contredit par les
  suites existantes de ces routeurs.
- **Invariant : « défaut de seuil non régressif pour la suite ».** Propriété de la
  **couche test** (fixture d'isolation), pas du code applicatif. Voir « Testabilité ».
- **Invariant : « 429 sans fuite d'information ».** Propriété du **handler SlowAPI
  global** (inchangé) ; déjà couvert par `test_public_rate_limit_cache.py` et réaffirmé
  par le nouveau test auth.

## Testabilité (Correctif 1)

### Le risque central : l'état mémoire global du limiteur casse les suites d'auth existantes

`limiter` est **global et en mémoire** (`app.state.limiter`). Son compteur par
(IP, fonction) **persiste pour toute la durée du process pytest**. Or, sous le
`TestClient`, toutes les requêtes partent de la **même IP** (`testclient`). En
comptant les appels d'auth déjà émis par la suite :

- `test_auth_router.py` : ~9 `POST /api/auth/login` + 3 `POST /api/auth/refresh`
  + 4 `POST /api/auth/set-credentials` ≈ **16 requêtes d'auth** ;
- `test_auth_task7.py` : 2 `POST /api/auth/login` ;
- `test_refresh_denylist_db.py` : plusieurs `POST /api/auth/refresh` (mais **skippé**
  sans `TEST_DATABASE_URL` — non comptés en mode unitaire ; comptés en mode intégration).

Avec un défaut `auth_rate_limit = "10/minute"` et un compteur qui ne se réinitialise
jamais entre tests, le ~11ᵉ appel d'auth du process (p.ex. dans `test_auth_router.py`
en comptant login **et** refresh **et** set-credentials sur la fonction concernée —
en réalité le comptage est **par fonction décorée**, donc par endpoint) peut basculer
en 429 et **faire échouer des tests qui attendent 200/401/410**. Le comptage par
fonction atténue (login seul ≈ 9–11 appels — déjà à la limite), mais la marge est trop
faible pour être laissée au hasard, et le mode intégration ajoute encore des refresh.
**Conclusion : il faut réinitialiser l'état du limiteur entre les tests d'auth.**

### Décision : fixture `autouse` `limiter.reset()` au niveau conftest, scope fonction

Deux options. (a) Dupliquer la fixture `_reset_limiter` (déjà présente dans
`test_public_rate_limit_cache.py`) dans **chaque** fichier d'auth
(`test_auth_router.py`, `test_auth_task7.py`, le nouveau `test_auth_rate_limit.py`, et
en mode intégration `test_refresh_denylist_db.py`). (b) Promouvoir une fixture
`autouse` **unique** dans `backend/app/tests/conftest.py`, à scope fonction, qui
appelle `limiter.reset()` avant chaque test de toute la suite.

**On choisit (b).** Raisons : une seule source de vérité ; elle protège aussi tout
futur test qui toucherait une route limitée sans y penser ; elle est strictement
additive et sans effet fonctionnel (le reset d'un compteur mémoire entre tests ne
change aucun comportement de production). Le surcoût est négligeable (`reset()` vide un
dict en mémoire). Le `conftest.py` existant contient déjà exclusivement des fixtures
`autouse` d'infrastructure de test (Argon2 allégé, sonde `/health`), donc y ajouter une
fixture d'isolation du limiteur est cohérent avec son rôle.

Risque d'interaction à vérifier : `test_public_rate_limit_cache.py` possède déjà sa
propre fixture `autouse` `_reset_limiter` (scope fonction, reset avant **et** après).
Deux fixtures `autouse` qui resettent le même limiteur sont **idempotentes** et sans
conflit (reset puis reset = reset). On **conserve** la fixture locale de
`test_public_rate_limit_cache.py` telle quelle (elle reset aussi *après*, utile pour ne
pas laisser de compteur pour le test suivant) ; la fixture conftest reset *avant*
chaque test, ce qui est la garantie dont les suites d'auth ont besoin. Aucune des deux
n'est retirée.

Fixture à ajouter dans `conftest.py` :

```python
@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Isole l'état (mémoire) du limiteur SlowAPI avant chaque test.

    Le limiteur (`app/core/rate_limit.py`) est global et en mémoire : ses
    compteurs par (IP, endpoint) persisteraient entre tests sous le `TestClient`
    (même IP `testclient`). Sans ce reset, les suites d'auth qui émettent plusieurs
    requêtes `/api/auth/*` finiraient par déclencher le 429 par IP (défaut
    « 10/minute »), rendant les tests dépendants de l'ordre. Le reset est purement
    un isolant de test : il ne modifie aucun comportement de production.
    """
    from app.core.rate_limit import limiter

    limiter.reset()
```

### Nouveau test `backend/app/tests/test_auth_rate_limit.py`

Modèle calqué sur `test_public_rate_limit_cache.py`. Contraintes et choix :

- **Ne pas toucher la base.** `login` lit `find_user_by_email` et écrit la denylist.
  On surcharge `get_session` par une **fausse session** (même motif que
  `test_auth_router.py` : `_FakeSession` renvoyant `scalar_one_or_none() → None` pour un
  e-mail inconnu, et un résultat vide `first() → None` pour les `text()` de la denylist)
  et on neutralise l'écrivain d'audit (`monkeypatch` de
  `auth_router.record_audit_standalone`). Ainsi chaque `POST /api/auth/login` est bon
  marché et renvoie **401** sous le seuil — ce qui est parfait : on teste le **code de
  rate-limit**, pas le succès d'auth. Le passage de 401 (sous le seuil) à **429** (au-delà)
  est l'observable qui prouve la limite.
- **Lire le seuil configuré, ne pas le muter au runtime.** Comme le décorateur fige
  `auth_rate_limit()` à l'import du routeur, on ne peut pas abaisser le seuil après
  coup. On lit donc le seuil effectif (`int(get_settings().auth_rate_limit.split("/")[0])`,
  helper `_limit_count()` identique à celui du test public) et on émet exactement
  `limit` requêtes (toutes **401**, sous le seuil) puis une de plus (**429**), puis une
  autre (**429**, reproductible). Avec le défaut 10/minute, cela fait 12 requêtes dans
  un même test — trivial en mémoire.

  Alternative envisagée et **rejetée** : forcer un seuil très bas (p.ex. `2/minute`)
  via `AUTH_RATE_LIMIT`/monkeypatch de `Settings`. Elle n'aurait d'effet **que** si le
  décorateur relisait le seuil à chaque requête, ce qui n'est pas le cas (il le lit à
  la décoration, au premier import du routeur). Comme le routeur `auth` est importé par
  `app.main` bien avant la collecte du test, muter le setting après coup ne reconfigure
  pas le décorateur déjà posé. On s'aligne donc sur la technique **réellement** employée
  par `test_public_rate_limit_cache.py` : lire le seuil et émettre le compte voulu.
- **Isolation.** La fixture conftest `_reset_rate_limiter` reset avant le test ; on
  ajoute aussi un reset explicite en fin de test (ou on s'appuie sur le reset *avant*
  du test suivant) pour ne pas laisser 12 hits comptés. Par prudence et pour calquer le
  test public, on duplique localement une fixture `autouse` qui reset **avant et après**.

Cas de test (AC-7) :

1. `test_login_sous_le_seuil_ne_declenche_pas_429` : émettre `limit` requêtes
   `POST /api/auth/login` (e-mail inconnu) ; **toutes** répondent 401 (jamais 429).
   Borne positive : sous le seuil, la limite ne gêne pas.
2. `test_login_au_dela_du_seuil_renvoie_429_reproductible` : émettre `limit` requêtes
   (401), puis la suivante → **429**, puis encore une → **429** (reproductible). Vérifier
   le corps minimal (`error` présent ; aucun champ sensible — réutiliser le jeu
   `_FORBIDDEN_*` du test public ou un sous-ensemble).

On n'exerce pas `refresh`/`set-credentials` pour le seuil (login suffit à prouver le
mécanisme, et chaque endpoint a son propre compteur du même type) ; les requirements
ciblent `/api/auth/login` pour le test (AC-7). On peut optionnellement ajouter un cas
léger confirmant que `refresh` est aussi décoré (émettre `limit+1` refresh bidon et
voir le dernier en 429), mais ce n'est pas requis ; décision : **se limiter à `login`**
pour rester proportionné et éviter la redondance.

### Niveaux de test

- **Unitaire / composant (sans base) :** le nouveau `test_auth_rate_limit.py` et les
  suites d'auth existantes. C'est le niveau qui prouve le rate-limit (429
  reproductible) et la non-régression (401/410/200 sous le seuil).
- **Intégration (PostGIS réel, CI) :** `test_refresh_denylist_db.py` continue de
  prouver la denylist. La fixture conftest d'isolation du limiteur le protège aussi de
  tout 429 parasite sur ses multiples `POST /api/auth/refresh`.
- **Non testé automatiquement :** la config Nginx (`auth_zone`, `location /api/auth/`)
  — artefact de déploiement, vérifié par relecture et par symétrie avec `public_zone`
  déjà éprouvé.

## Correctif 2 — Régénération de `docs/E2E_VERIFICATION.md`

### Dépendance d'ordre (impératif)

Ce correctif **doit** être réalisé **après** le Correctif 1, car le nouveau fichier
`test_auth_rate_limit.py` augmente le total backend. Les chiffres écrits dans le
document sont ceux **re-mesurés** après implémentation, jamais ceux d'avant.

### Procédure de mesure

Depuis `backend/` : `.venv\Scripts\python.exe -m pytest -q` → relever le **nouveau**
total unitaire (strictement > 221, de l'ordre de 221 + 2 nouveaux cas = **223**, à
confirmer par la mesure réelle — ne pas coder le chiffre en dur dans ce design) et le
nombre de skips (inchangé : **56**, car les nouveaux cas ne sont pas gardés par
`TEST_DATABASE_URL`). Depuis `frontend/` : `npm run test -- --run` → confirmer **143**
(26 fichiers), inchangé par ce lot.

Le total du mode **intégration** est dérivé : `unitaires + skips exécutés` =
`(nouveau total unitaire) + 56`. Si PostGIS n'est pas exécutable localement, étiqueter
ce total comme **dérivé des deux modes et confirmé en CI** (A4), sans inventer de
chiffre non mesuré.

### Emplacements exacts à mettre à jour

Lecture faite du document, les occurrences des comptes périmés sont :

- **§2.1** tableau des deux modes backend : `202 passés, 54 ignorés` (unitaire) et
  `256 passés, 0 ignoré` (intégration) → remplacer par `<nouveau total unitaire>
  passés, 56 ignorés` et `<nouveau total unitaire + 56> passés, 0 ignoré`.
- **§2.1** phrase « Les 54 tests “ignorés” … » → `56`.
- **§2.2** frontend : `24 fichiers, 132 tests, tous passés` → `26 fichiers, 143 tests,
  tous passés`.
- **§8** tableau de synthèse : trois lignes `Backend unitaire (202)`,
  `Backend intégration PostGIS (256, 0 ignoré)`, `Frontend (132)` → mettre à jour les
  trois nombres de façon cohérente avec §2.1/§2.2.
- **§4** : relecture — ce paragraphe ne cite pas de compte agrégé (il nomme des tests
  individuels) ; **aucune** modification de chiffre n'y est attendue sauf si une mention
  numérique s'y trouve à la relecture. (FR-2.2 nomme §4 par prudence ; le design note
  qu'après lecture §4 ne contient pas de total à changer, mais le coder vérifie.)
- **Toute autre occurrence** de `202`, `54`, `256`, `132`, `24 fichiers` dans le
  fichier (recherche exhaustive) → mise en cohérence.

### Préservation (FR-2.3 / AC-12)

Ne **rien** modifier d'autre : traçabilité P1–P13 (§3), scénario terrain (§4 hors
chiffres), revue de sécurité (§5), vérifications appareil réel (§6), section
« reproduire » (§7). Seuls les nombres de tests et leurs phrases porteuses changent.
Le fichier reste du Markdown GFM (tableaux `|`), encodage UTF-8, inchangé par ailleurs.

### Testabilité (Correctif 2)

Non testé par une suite automatisée (document). La vérification est : (a) les commandes
de mesure ont été relancées et leurs sorties réelles reportées ; (b) une recherche
plein-texte des anciens nombres (`202`, `256`, `132`, `24 fichiers`, `54 ignoré`) ne
renvoie plus aucune occurrence porteuse de ces comptes ; (c) le reste du document est
inchangé (diff limité aux lignes de chiffres).

## Correctif 3 — Correction de la spec `tasks.md`

Modification **strictement documentaire** de
`.kiro/specs/production-readiness-gaps/tasks.md`. Aucun fichier `backend/` touché
(FR-3.3 / AC-15). Preuves vérifiées dans le dépôt que 9.2 est livrée : migration
`backend/alembic/versions/0010_refresh_token.py`, service
`backend/app/services/refresh_tokens.py` (`record_issued_refresh`, `consume_refresh`
avec UPDATE conditionnel atomique `… RETURNING` + invalidation de famille au rejeu),
câblage dans `backend/app/routers/auth.py` (login émet `jti`+`family_id` et appelle
`record_issued_refresh` ; refresh consomme via `consume_refresh`, refuse les refresh
hérités sans `jti`, rote avec nouveau `jti`/même `family_id`), et tests
`backend/app/tests/test_refresh_denylist_db.py`.

### Édition exacte

**(a) Bloc de la tâche 9.2** (actuellement) :

> - [x] 9.2 **(Hors lot courant — phase 2 ; introduit un modèle de données, donc non
>   additif.)** Implémenter la famille `jti` + denylist (voir Data Models) : … À
>   planifier comme lot distinct une fois 9.1 livré.

La case reste `[x]`. Remplacer le marqueur « (Hors lot courant — phase 2 …) » et la
phrase finale « À planifier comme lot distinct une fois 9.1 livré » par une note de
livraison, p.ex. :

> - [x] 9.2 **(Livrée.)** Famille `jti` + denylist de refresh (voir Data Models) :
>   migration de la table `refresh_token`, écriture/consommation du `jti` à l'émission,
>   et refus du rejeu d'un `jti` consommé sur `/api/auth/refresh` (chemin en ligne
>   uniquement). **Livrée et testée** : migration `0010_refresh_token.py`, service
>   `refresh_tokens.py` (`record_issued_refresh`, `consume_refresh` — UPDATE
>   conditionnel atomique + invalidation de famille au rejeu), câblage `auth.py`
>   (login émet `jti`+`family_id` ; refresh consomme, refuse les refresh hérités sans
>   `jti`, rote avec nouveau `jti`/même `family_id`), tests
>   `test_refresh_denylist_db.py`. DoD atteinte : rejeu d'un refresh consommé rejeté ;
>   travail hors ligne non affecté ; tests verts.

Conserver le reste de la DoD d'origine qui reste vrai. Le `_Requirements: 9 ; NFR-7,
NFR-2_` de la tâche 9 est inchangé.

**(b) Section « Notes », puce « Découpage de la tâche 9 »** (actuellement) :

> - Découpage de la tâche 9 : 9.1 (raccourcissement des TTL, simple configuration) est
>   dans ce lot ; 9.2 (table `refresh_token` + denylist) est un lot de phase 2 car elle
>   introduit un modèle de données (non additif).

Remplacer par une formulation reflétant que 9.2 est livrée, p.ex. :

> - Découpage de la tâche 9 : 9.1 (raccourcissement des TTL, configuration) et 9.2
>   (table `refresh_token` + denylist `jti` + rotation de famille) sont **toutes deux
>   livrées**. 9.2 a bien introduit un modèle de données (migration `0010`), implémenté
>   via le service `refresh_tokens.py`, câblé dans `auth.py` et couvert par
>   `test_refresh_denylist_db.py`.

**(c) Commentaire du graphe de dépendances** — la ligne ASCII
`9 Durcissement JWT (9.1 config, immédiat ; 9.2 denylist, phase 2)` mentionne encore
« phase 2 ». L'ajuster en `9.2 denylist, livrée` (texte du bloc de code ; purement
cosmétique mais nécessaire pour la cohérence demandée par FR-3.2/AC-14, qui vise « la
ligne de synthèse »).

Ne **pas** toucher au JSON de planification (`"tasks": [...]`) : il n'affirme rien sur
« phase 2 » (il porte `wave`/`severity`), et le modifier dépasserait le périmètre
documentaire demandé. Décision : le JSON reste inchangé.

### Testabilité (Correctif 3)

Non testé par une suite (document de spec). Vérification : recherche plein-texte de
« phase 2 » / « À planifier comme lot distinct » dans `tasks.md` → ne doit plus
qualifier 9.2 ; la case 9.2 reste `[x]` ; aucun fichier `backend/` n'apparaît au diff.

## Edge cases (transverses)

- **Double 429 sur `login`** (lockout par compte vs rate-limit par IP) : traité ci-dessus
  — coexistence voulue, aucune ne masque l'autre de façon exploitable.
- **`refresh` sans `request`** : SlowAPI lèverait au runtime s'il manque ; le paramètre
  est donc ajouté (FR-1.4). Un oubli serait attrapé par le premier test touchant
  `refresh` (y compris les tests existants de `test_auth_router.py`).
- **Seuil mal formé dans `AUTH_RATE_LIMIT`** : échec rapide à l'import du routeur (lib
  `limits`), cohérent avec `public_rate_limit` — voulu.
- **Ordre des `location` Nginx** : `/api/auth/` doit précéder `/api/` ; documenté et
  placé physiquement avant.
- **Mode intégration ajoutant des refresh** (`test_refresh_denylist_db.py`) : couvert
  par la fixture conftest d'isolation du limiteur (reset avant chaque test).
- **Commentaires devenus faux** (portée « publics uniquement ») : ajustés dans les
  trois emplacements listés, sinon la doc mentirait.
- **Comptes de tests** : ne jamais coder en dur un total non mesuré ; le total
  intégration est explicitement étiqueté « dérivé + CI » si PostGIS absent localement.

## Vérification finale (rappel, exécutée par l'implémenteur)

1. `backend/` → `.venv\Scripts\python.exe -m pytest -q` : **vert**, nouveaux tests de
   rate-limit auth inclus ; relever le total exact (pour le Correctif 2).
2. `backend/` → `.venv\Scripts\python.exe -m ruff check .` : aucune nouvelle violation
   (line-length 100, py312 ; attention à la longueur des commentaires ajoutés).
3. `frontend/` → `npm run test -- --run` : **vert** (143).
4. 429 d'auth reproductible et routes d'auth légitimes (sous le seuil) toujours
   fonctionnelles — prouvé par `test_auth_rate_limit.py` + non-régression des suites
   d'auth existantes.
5. `docs/E2E_VERIFICATION.md` reflète les comptes **post-changement** (re-mesurés), et
   `tasks.md` reflète 9.2 livrée.

## Récapitulatif des fichiers touchés

| Fichier | Nature | Correctif |
| --- | --- | --- |
| `backend/app/core/config.py` | Ajout champ `auth_rate_limit` + commentaire | 1 |
| `backend/app/core/rate_limit.py` | Ajout `auth_rate_limit()` + MAJ docstring de portée | 1 |
| `backend/app/routers/auth.py` | Import + 3 décorateurs + `request: Request` sur `refresh` | 1 |
| `backend/app/main.py` | MAJ commentaire de portée du limiteur (texte seul) | 1 |
| `deploy/reverse-proxy/nginx.conf` | Zone `auth_zone` + `location /api/auth/` + MAJ commentaire | 1 |
| `backend/app/tests/conftest.py` | Ajout fixture `autouse` `limiter.reset()` | 1 |
| `backend/app/tests/test_auth_rate_limit.py` | **Nouveau** fichier de test | 1 |
| `docs/E2E_VERIFICATION.md` | MAJ des comptes de tests (re-mesurés) | 2 |
| `.kiro/specs/production-readiness-gaps/tasks.md` | MAJ libellé 9.2 + note + graphe | 3 |
