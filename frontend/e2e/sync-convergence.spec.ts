// Scénario E2E de convergence multi-poste (#12) — tâche 8.
//
// Ce fichier porte le **harnais réutilisable** (tâche 8.1) du scénario de
// convergence local-first : deux postes isolés, une API de sync simulée et
// déterministe, le pilotage de la connectivité, et un smoke test prouvant que
// les deux postes chargent l'app. Le scénario complet (travail hors ligne des
// deux côtés, reconnexion, convergence sans perte ni doublon, bannière de
// conflit de note de visite) est ajouté en tâche 8.2 par-dessus ce harnais.
//
// Décisions appliquées (ADR-1 + pièges H1/M1 de
// `.kiro/specs/dev-and-ci-tooling/design.md`) :
//
//  - **Deux `BrowserContext` isolés** (poste A, poste B), PAS deux `page` d'un
//    même contexte (M1). RxDB/IndexedDB est cloisonné par origine + contexte et
//    la base locale a un nom FIXE (`dme_local`, voir `src/lib/db.ts`). Deux
//    pages d'un même contexte partageraient donc la même base IndexedDB, ce qui
//    ruinerait la simulation « deux postes ». Seuls deux `BrowserContext`
//    distincts donnent deux bases locales isolées.
//
//  - **API interceptée sur l'ORIGINE ABSOLUE** (H1). `API_BASE_URL` par défaut =
//    `http://localhost:8000` (`src/lib/api.ts`), tandis que `vite preview` sert
//    sur `http://localhost:4173` : les appels de sync sont cross-origin. La
//    build E2E fige l'origine via `VITE_API_URL` (injecté par
//    `playwright.config.ts`). On intercepte donc `http://<origine>/api/**` avec
//    l'origine absolue, jamais un chemin relatif `/api/**` qui raterait les
//    requêtes.
//
//  - **Service worker bloqué** (`serviceWorkers: 'block'`) au niveau du contexte
//    pour que seuls le mock d'API et IndexedDB déterminent le résultat (le test
//    du vrai SW est séparé dans `offline-reload.spec.ts`, cf. H1).
//
//  - **Connectivité pilotée** via `context.setOffline(true|false)` — aucune
//    dépendance à un réseau réel ni à une temporisation arbitraire (exigence
//    2.4). Les attentes s'appuient sur des conditions observables.

import { test, expect, type BrowserContext, type Page, type Route } from "@playwright/test";

// -------------------------------------------------------------------------
// Origine absolue de l'API (H1)
// -------------------------------------------------------------------------
//
// DOIT coïncider avec `E2E_API_ORIGIN` de `playwright.config.ts` (la valeur
// injectée dans `VITE_API_URL` au build). Si l'environnement surcharge
// `VITE_API_URL`, on s'aligne sur la même valeur ; sinon on retombe sur le
// défaut de `src/lib/api.ts` (`http://localhost:8000`).
const API_ORIGIN = process.env.VITE_API_URL ?? "http://localhost:8000";

/** Motif de route ABSOLU ciblant tous les endpoints `/api/**` du mock (H1). */
const API_ROUTE_GLOB = `${API_ORIGIN}/api/**`;

// -------------------------------------------------------------------------
// Contrat de sync — miroir minimal de `src/lib/api.ts` / `app/schemas/sync.py`
// -------------------------------------------------------------------------
//
// On recopie ici (côté test) la forme du contrat figé consommé par `sync.ts`,
// pour que le mock soit fidèle sans importer le code applicatif.

type SyncEntity = "patient" | "visit";
type SyncOp = "upsert" | "delete";

interface Mutation {
  mutation_id: string;
  entity: SyncEntity;
  entity_id: string;
  op: SyncOp;
  payload: Record<string, unknown>;
  client_version: number;
  client_updated_at: string;
  event_at: string;
}

type MutationResultStatus = "applied" | "duplicate" | "conflict" | "rejected";

interface SyncConflict {
  entity: SyncEntity;
  entity_id: string;
  loser_payload: Record<string, unknown>;
  winner_version: number;
  sensitive: boolean;
}

interface MutationResult {
  mutation_id: string;
  entity: SyncEntity;
  entity_id: string;
  status: MutationResultStatus;
  server_version: number | null;
  conflict: SyncConflict | null;
  message: string | null;
}

interface SyncPushRequest {
  mutations: Mutation[];
}

interface SyncPushResponse {
  results: MutationResult[];
  conflicts: SyncConflict[];
}

interface SyncChange {
  entity: SyncEntity;
  entity_id: string;
  op: SyncOp;
  payload: Record<string, unknown>;
  version: number;
  updated_at: string;
  deleted: boolean;
}

interface SyncPullResponse {
  changes: SyncChange[];
  cursor: string;
  has_more: boolean;
  revoked: boolean;
}

// -------------------------------------------------------------------------
// Serveur de sync simulé, déterministe et scripté (ADR-1)
// -------------------------------------------------------------------------
//
// Le mock tient un état serveur **autoritaire** par entité (dernière version
// gagnante LWW) et une table d'idempotence par `mutation_id`. Il est partagé par
// les deux postes (comme un vrai serveur tenant) : c'est ce partage qui produit
// la convergence au pull. Le comportement est 100 % déterministe : aucune
// horloge réelle, aucun aléa, l'ordre des deltas est l'ordre d'application.
//
// Faits de contrat honorés (fidélité au backend) :
//   - **Idempotence par `mutation_id`** (P10) : une mutation déjà vue renvoie
//     `duplicate` et n'est PAS ré-appliquée (ni doublon, ni perte).
//   - **Résolution LWW** par (`client_updated_at`, puis `client_version`) : la
//     mutation la plus récente gagne ; la perdante est renvoyée en `conflict`
//     avec son `loser_payload`.
//   - **Conflit sensible** : un conflit sur une **note de visite** (`entity`
//     `visit` dont le `payload.notes` diffère) porte `sensitive: true` pour que
//     l'UID affiche la bannière de conflit (tâche 8.2).
//   - **Pull par curseur** : le curseur est un entier croissant (offset dans le
//     journal des changements) ; `since` filtre les changements postérieurs.

/** Entrée du journal serveur, support du pull delta. */
interface ChangeLogEntry extends SyncChange {
  /** Offset monotone (= base du curseur opaque). */
  seq: number;
}

/** État autoritaire d'une entité côté serveur (pour la résolution LWW). */
interface ServerEntityState {
  entity: SyncEntity;
  entity_id: string;
  payload: Record<string, unknown>;
  version: number;
  updated_at: string;
  deleted: boolean;
}

class MockSyncServer {
  /** État autoritaire par clé `${entity}:${entity_id}`. */
  private readonly entities = new Map<string, ServerEntityState>();
  /** Idempotence : `mutation_id` déjà appliqués → résultat mémorisé. */
  private readonly seenMutations = new Map<string, MutationResult>();
  /** Journal ordonné des changements (support du pull par curseur). */
  private readonly changeLog: ChangeLogEntry[] = [];
  /** Offset monotone du prochain changement journalisé. */
  private nextSeq = 1;

  /**
   * Si `true`, le prochain pull répond `revoked: true` (test de révocation,
   * NFR-7). Laissé à `false` par défaut.
   */
  revokeOnNextPull = false;

  private key(entity: SyncEntity, entityId: string): string {
    return `${entity}:${entityId}`;
  }

  /**
   * LWW : la mutation entrante gagne-t-elle sur l'état courant ? Compare
   * d'abord `updated_at` (ISO, comparable lexicographiquement), puis `version`
   * en départage. Un état absent est toujours battu (la mutation gagne).
   */
  private incomingWins(
    current: ServerEntityState | undefined,
    updatedAt: string,
    version: number,
  ): boolean {
    if (!current) return true;
    if (updatedAt > current.updated_at) return true;
    if (updatedAt < current.updated_at) return false;
    return version > current.version;
  }

  /** Les deux payloads de note de visite diffèrent-ils ? (conflit sensible). */
  private isSensitiveVisitNote(
    entity: SyncEntity,
    incoming: Record<string, unknown>,
    current: ServerEntityState | undefined,
  ): boolean {
    if (entity !== "visit" || !current) return false;
    return (incoming.notes ?? null) !== (current.payload.notes ?? null);
  }

  /** Applique une mutation (idempotente) et renvoie son résultat. */
  applyMutation(m: Mutation): MutationResult {
    // Idempotence (P10) : rejouer une mutation déjà vue → `duplicate`, sans
    // ré-écrire l'état (ni doublon, ni perte).
    const already = this.seenMutations.get(m.mutation_id);
    if (already) {
      return { ...already, status: "duplicate" };
    }

    const key = this.key(m.entity, m.entity_id);
    const current = this.entities.get(key);
    const isDelete = m.op === "delete";
    const wins = this.incomingWins(current, m.client_updated_at, m.client_version);

    let result: MutationResult;

    if (wins) {
      const nextVersion = current ? current.version + 1 : m.client_version;
      const nextState: ServerEntityState = {
        entity: m.entity,
        entity_id: m.entity_id,
        payload: isDelete ? (current?.payload ?? {}) : m.payload,
        version: nextVersion,
        updated_at: m.client_updated_at,
        deleted: isDelete,
      };
      this.entities.set(key, nextState);
      this.appendChange(nextState);
      result = {
        mutation_id: m.mutation_id,
        entity: m.entity,
        entity_id: m.entity_id,
        status: "applied",
        server_version: nextVersion,
        conflict: null,
        message: null,
      };
    } else {
      // La mutation entrante perd le LWW : conflit. L'état courant (gagnant)
      // reste ; on renvoie le perdant pour revue éventuelle.
      const sensitive = this.isSensitiveVisitNote(m.entity, m.payload, current);
      const conflict: SyncConflict = {
        entity: m.entity,
        entity_id: m.entity_id,
        loser_payload: m.payload,
        winner_version: current!.version,
        sensitive,
      };
      result = {
        mutation_id: m.mutation_id,
        entity: m.entity,
        entity_id: m.entity_id,
        status: "conflict",
        server_version: current!.version,
        conflict,
        message: null,
      };
    }

    this.seenMutations.set(m.mutation_id, result);
    return result;
  }

  /** Journalise un changement serveur pour qu'il soit tiré par les autres postes. */
  private appendChange(state: ServerEntityState): void {
    this.changeLog.push({
      seq: this.nextSeq++,
      entity: state.entity,
      entity_id: state.entity_id,
      op: state.deleted ? "delete" : "upsert",
      payload: state.payload,
      version: state.version,
      updated_at: state.updated_at,
      deleted: state.deleted,
    });
  }

  /** Traite un `POST /api/sync/push` complet (lot de mutations). */
  handlePush(body: SyncPushRequest): SyncPushResponse {
    const results: MutationResult[] = [];
    const conflicts: SyncConflict[] = [];
    for (const m of body.mutations ?? []) {
      const r = this.applyMutation(m);
      results.push(r);
      if (r.status === "conflict" && r.conflict) conflicts.push(r.conflict);
    }
    return { results, conflicts };
  }

  /**
   * Traite un `GET /api/sync/pull?since=<curseur>`. Le curseur est l'offset
   * (entier) du dernier changement déjà vu ; on renvoie les changements
   * postérieurs. Pagination : tout d'un coup ici (pas de `has_more`), le
   * harnais restant déterministe et borné.
   */
  handlePull(since: string | null): SyncPullResponse {
    if (this.revokeOnNextPull) {
      this.revokeOnNextPull = false;
      return { changes: [], cursor: since ?? "0", has_more: false, revoked: true };
    }
    const sinceSeq = since ? Number.parseInt(since, 10) || 0 : 0;
    const changes = this.changeLog.filter((c) => c.seq > sinceSeq);
    const cursor = changes.length > 0 ? String(changes[changes.length - 1].seq) : String(sinceSeq);
    return {
      changes: changes.map(({ seq: _seq, ...change }) => {
        void _seq;
        return change;
      }),
      cursor,
      has_more: false,
      revoked: false,
    };
  }

  // --- Introspection pour les assertions de 8.2 -------------------------

  /** Nombre de mutations distinctes appliquées (idempotence : rejeux exclus). */
  get appliedCount(): number {
    let n = 0;
    for (const r of this.seenMutations.values()) {
      if (r.status === "applied") n += 1;
    }
    return n;
  }

  /** État autoritaire courant d'une entité (ou `undefined`). */
  getEntity(entity: SyncEntity, entityId: string): ServerEntityState | undefined {
    return this.entities.get(this.key(entity, entityId));
  }

  /** Identifiants des entités d'un type donné (ordre d'insertion). */
  entityIds(entity: SyncEntity): string[] {
    const ids: string[] = [];
    for (const state of this.entities.values()) {
      if (state.entity === entity) ids.push(state.entity_id);
    }
    return ids;
  }

  /** Nombre de résultats `conflict` renvoyés (tous push confondus). */
  get conflictCount(): number {
    let n = 0;
    for (const r of this.seenMutations.values()) {
      if (r.status === "conflict") n += 1;
    }
    return n;
  }
}

// -------------------------------------------------------------------------
// Câblage de l'interception de route (H1 — origine absolue)
// -------------------------------------------------------------------------

/**
 * Branche un `MockSyncServer` sur une `page` donnée, en interceptant l'ORIGINE
 * ABSOLUE `${API_ORIGIN}/api/**` (jamais un chemin relatif). Les deux postes
 * partagent le même `server` (comme un vrai backend tenant), ce qui produit la
 * convergence au pull.
 *
 * Endpoints couverts :
 *   - `POST /api/auth/login`   → jeton tenant déterministe (déverrouille la
 *     session UI pour atteindre Patients/Visites ; voir `TENANT_ID`/`ROLE`).
 *   - `POST /api/auth/refresh` → même jeton (jamais atteint en pratique : le
 *     jeton est émis avec une longue durée de vie, mais on reste fidèle au
 *     contrat `session.ts`).
 *   - `POST /api/sync/push`    → `server.handlePush`
 *   - `GET  /api/sync/pull`    → `server.handlePull` (lit `?since=`)
 *   - tout autre `/api/**`     → 404 JSON. Les pages publiques (carte/annuaire)
 *     peuvent interroger `/api/public/**` au premier rendu ; le scénario n'ouvre
 *     pas ces onglets, et un 404 y est inoffensif (géré par les composants).
 */
async function routeApi(page: Page, server: MockSyncServer): Promise<void> {
  await page.route(API_ROUTE_GLOB, async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    const json = (status: number, payload: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(payload),
      });

    // --- Authentification (déverrouille l'aire tenant de l'UI) --------------
    //
    // `session.ts` dérive la clé de chiffrement RxDB du mot de passe (PBKDF2
    // réel, exécuté dans le vrai navigateur) et ouvre la base locale `dme_local`.
    // Le mock n'a qu'à renvoyer un `TokenResponse` fidèle : un `tenant_id` non
    // nul est REQUIS pour que Patients/Visites deviennent accessibles.
    if (path === "/api/auth/login" && method === "POST") {
      return json(200, buildTokenResponse());
    }
    if (path === "/api/auth/refresh" && method === "POST") {
      return json(200, buildTokenResponse());
    }

    if (path === "/api/sync/push" && method === "POST") {
      const body = request.postDataJSON() as SyncPushRequest;
      return json(200, server.handlePush(body));
    }

    if (path === "/api/sync/pull" && method === "GET") {
      const since = url.searchParams.get("since");
      return json(200, server.handlePull(since));
    }

    // Appel non prévu par le scénario de convergence (p. ex. `/api/public/**`
    // au premier rendu de la carte) : 404 inoffensif, les composants le gèrent.
    return json(404, { detail: `mock: route non gérée ${method} ${path}` });
  });
}

// -------------------------------------------------------------------------
// Contrat d'authentification — miroir minimal de `session.ts` / `auth.py`
// -------------------------------------------------------------------------
//
// Les deux postes partagent le MÊME tenant (comme deux postes d'un même
// centre) : c'est la condition de la convergence (le serveur de sync est cadré
// au tenant). Le jeton est déterministe et de longue durée (aucun
// rafraîchissement déclenché pendant le scénario).

/** Tenant commun aux deux postes (déterministe). */
const TENANT_ID = "7a3b1c2d-0000-4000-8000-000000000001";
/** Rôle cadré au tenant : donne accès à Patients/Visites (tenant_admin). */
const ROLE = "tenant_admin";
/** Identifiants saisis dans le formulaire de connexion (déterministes). */
const LOGIN_EMAIL = "poste@centre.tg";
const LOGIN_PASSWORD = "motdepasse-convergence-12";

/** Construit un `TokenResponse` fidèle au contrat (`session.ts`). */
function buildTokenResponse() {
  return {
    access: "e2e-access-token",
    refresh: "e2e-refresh-token",
    token_type: "bearer",
    // Durée de vie large (1 h) : aucun rafraîchissement opportuniste pendant le
    // scénario, donc aucun appel `/api/auth/refresh` à ordonnancer.
    expires_in: 3600,
    tenant_id: TENANT_ID,
    role: ROLE,
  };
}

// -------------------------------------------------------------------------
// Harnais de poste (deux `BrowserContext` isolés — M1)
// -------------------------------------------------------------------------

/** Un « poste de travail » : contexte isolé + sa page + helpers connectivité. */
interface Workstation {
  context: BrowserContext;
  page: Page;
  /** Passe le poste hors ligne (connectivité pilotée, pas de réseau réel). */
  goOffline: () => Promise<void>;
  /** Repasse le poste en ligne. */
  goOnline: () => Promise<void>;
  /** Ferme proprement le contexte isolé. */
  close: () => Promise<void>;
}

/**
 * Crée un poste : un `BrowserContext` DISTINCT (base IndexedDB `dme_local`
 * isolée, M1) avec le **service worker bloqué** (H1), l'API mock interceptée sur
 * l'origine absolue (H1), puis navigue sur l'app servie par `vite preview`.
 */
async function createWorkstation(
  browser: import("@playwright/test").Browser,
  server: MockSyncServer,
): Promise<Workstation> {
  // `serviceWorkers: 'block'` : seul le mock d'API + IndexedDB déterminent le
  // résultat (le vrai SW est testé séparément, cf. `offline-reload.spec.ts`).
  const context = await browser.newContext({ serviceWorkers: "block" });
  const page = await context.newPage();

  await routeApi(page, server);

  // L'app est servie par le `webServer` de `playwright.config.ts` (build de
  // prod sur 4173, `baseURL`). On attend le réseau au repos pour une base
  // observable stable avant toute assertion.
  await page.goto("/", { waitUntil: "networkidle" });

  return {
    context,
    page,
    goOffline: () => context.setOffline(true),
    goOnline: () => context.setOffline(false),
    close: () => context.close(),
  };
}

// -------------------------------------------------------------------------
// Pilotage de l'UI (tâche 8.2) — conditions observables, aucun `sleep`
// -------------------------------------------------------------------------
//
// Ces helpers conduisent l'app RÉELLE (formulaires de connexion, patients,
// visites) via des sélecteurs accessibles (libellés i18n français). Chaque
// attente s'appuie sur une condition observable (`expect` auto-retry sur le DOM,
// `waitForResponse` sur les endpoints mock), jamais sur une temporisation
// arbitraire (exigence 2.4).

/**
 * Connecte un poste : saisit e-mail/mot de passe, soumet, puis attend que l'aire
 * tenant soit déverrouillée (le bouton « Patients » apparaît). Le mock renvoie
 * un jeton avec `tenant_id`, et `session.ts` ouvre la base locale chiffrée.
 */
async function login(ws: Workstation): Promise<void> {
  const page = ws.page;

  // Onglet « Compte » (BottomNav) → formulaire de connexion.
  await page.getByRole("button", { name: "Compte" }).click();

  await page.getByLabel("Adresse e-mail").fill(LOGIN_EMAIL);
  await page.getByLabel("Mot de passe").fill(LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Se connecter" }).click();

  // Session déverrouillée : le panneau de session affiche l'accès Patients.
  await expect(page.getByRole("button", { name: "Patients" })).toBeVisible();
}

/** Ouvre la sous-vue Patients depuis le panneau de session tenant. */
async function openPatients(ws: Workstation): Promise<void> {
  await ws.page.getByRole("button", { name: "Patients" }).click();
  // Titre de la page Patients (h1) : condition observable d'arrivée.
  await expect(
    ws.page.getByRole("heading", { name: "Patients" }),
  ).toBeVisible();
}

/** Revient au panneau de session (depuis une sous-vue tenant). */
async function backToAccount(ws: Workstation): Promise<void> {
  const backLink = ws.page.getByRole("button", { name: "Retour au compte" });
  if (await backLink.isVisible().catch(() => false)) {
    await backLink.click();
    await expect(
      ws.page.getByRole("button", { name: "Visites" }),
    ).toBeVisible();
  }
}

/** Ouvre la sous-vue Visites depuis le panneau de session tenant. */
async function openVisits(ws: Workstation): Promise<void> {
  await backToAccount(ws);
  await ws.page.getByRole("button", { name: "Visites" }).click();
  await expect(ws.page.getByRole("heading", { name: "Visites" })).toBeVisible();
}

/**
 * Crée un patient via le formulaire réel (consentement obligatoire, REQ-5.6).
 * Attend que la nouvelle ligne apparaisse dans la liste locale (query réactive
 * RxDB) : condition observable d'écriture locale aboutie.
 */
async function createPatientUI(
  ws: Workstation,
  fields: { mrn: string; given: string; family: string },
): Promise<void> {
  const page = ws.page;
  await page.getByRole("button", { name: "Nouveau patient" }).click();

  await page.getByLabel("Identifiant (MRN)").fill(fields.mrn);
  await page.getByLabel("Prénom", { exact: true }).fill(fields.given);
  await page.getByLabel("Nom", { exact: true }).fill(fields.family);
  // Consentement requis pour activer « Enregistrer » (REQ-5.6).
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Enregistrer" }).click();

  // La liste locale (réactive) affiche le patient : écriture locale + outbox OK.
  await expect(
    page.getByText(`${fields.given} ${fields.family}`),
  ).toBeVisible();
}

/**
 * Crée une visite via le formulaire réel, rattachée au premier patient
 * disponible, avec une note initiale. Attend l'apparition du motif dans la
 * liste (condition observable).
 */
async function createVisitUI(
  ws: Workstation,
  fields: { reason: string; notes: string },
): Promise<void> {
  const page = ws.page;
  await page.getByRole("button", { name: "Nouvelle visite" }).click();
  await page.getByLabel("Motif").fill(fields.reason);
  await page.getByLabel("Notes cliniques").fill(fields.notes);
  await page.getByRole("button", { name: "Enregistrer" }).click();

  await expect(page.getByText(fields.reason)).toBeVisible();
}

/**
 * Édite la note d'une visite repérée par son motif actuel. Ouvre l'édition,
 * remplace la note, enregistre, et attend que la nouvelle note soit visible.
 */
async function editVisitNoteUI(
  ws: Workstation,
  reason: string,
  newNotes: string,
): Promise<void> {
  const page = ws.page;
  // Trouver la ligne contenant le motif, puis son bouton « Modifier ».
  const row = page.locator("li", { hasText: reason });
  await row.getByRole("button", { name: "Modifier" }).click();
  const notesField = page.getByLabel("Notes cliniques");
  await notesField.fill(newNotes);
  await page.getByRole("button", { name: "Enregistrer" }).click();
  await expect(page.getByText(newNotes)).toBeVisible();
}

/** Réponse `push` 200 observée sur le poste (mutations poussées). */
function pushResponse(ws: Workstation) {
  return ws.page.waitForResponse(
    (r) =>
      r.url().startsWith(`${API_ORIGIN}/api/sync/push`) && r.status() === 200,
  );
}

/** Réponse `pull` 200 observée sur le poste (deltas tirés). */
function pullResponse(ws: Workstation) {
  return ws.page.waitForResponse(
    (r) =>
      r.url().startsWith(`${API_ORIGIN}/api/sync/pull`) && r.status() === 200,
  );
}

/**
 * Reconnecte le poste et attend un aller-retour COMPLET : `push` (POST) ET
 * `pull` (GET) répondus. À utiliser quand le poste a des mutations locales en
 * attente (outbox non vide) : le cycle de `sync.ts` pousse puis tire.
 *
 * L'écoute est armée AVANT `goOnline()` (qui déclenche l'événement `online` et
 * donc `syncNow()`), pour ne pas rater la réponse. Aucune temporisation
 * arbitraire : on observe les réponses réelles du mock (exigence 2.4).
 */
async function reconnectPushAndPull(ws: Workstation): Promise<void> {
  const pushDone = pushResponse(ws);
  const pullDone = pullResponse(ws);
  await ws.goOnline();
  await Promise.all([pushDone, pullDone]);
}

/**
 * Reconnecte le poste et attend UNIQUEMENT un `pull` (GET) répondu. À utiliser
 * quand le poste n'a rien à pousser (outbox vide) : le cycle ne fait pas de
 * `push`, attendre un POST bloquerait. On tire seulement les deltas des autres
 * postes (convergence au pull).
 */
async function reconnectPullOnly(ws: Workstation): Promise<void> {
  const pullDone = pullResponse(ws);
  await ws.goOnline();
  await pullDone;
}

/**
 * Draine l'outbox du poste jusqu'à ce qu'une CONDITION observable (état du
 * serveur partagé) soit satisfaite, en relançant un cycle de sync à chaque
 * itération par une bascule offline→online (qui déclenche `syncNow`), chaque
 * cycle étant borné par l'attente d'un `pull` répondu.
 *
 * Pourquoi ce drainage itératif : l'écriture locale d'une mutation (RxDB + file
 * d'attente) peut devenir visible à l'UI juste AVANT que l'entrée d'outbox ne
 * soit committée, si bien qu'un unique push peut ne pas encore voir la dernière
 * mutation. Plutôt qu'une temporisation arbitraire (interdit, exigence 2.4), on
 * répète des cycles de sync BORNÉS et on s'arrête sur une condition OBSERVABLE
 * (la mutation est appliquée côté serveur). Déterministe et sans `sleep`.
 */
async function pushUntil(
  ws: Workstation,
  predicate: () => boolean,
  maxCycles = 10,
): Promise<void> {
  for (let i = 0; i < maxCycles && !predicate(); i++) {
    const pullDone = pullResponse(ws);
    await ws.goOffline();
    await ws.goOnline();
    await pullDone; // chaque cycle inclut au moins un pull (borne observable)
  }
}

// -------------------------------------------------------------------------
// Smoke test du harnais (tâche 8.1)
// -------------------------------------------------------------------------
//
// Vérifie uniquement que le harnais est fonctionnel : deux postes ISOLÉS
// chargent l'app, l'interception de route est en place, et les bascules
// online/offline s'appliquent sans erreur. Les assertions de convergence et de
// bannière de conflit sont la tâche 8.2 (qui réutilise ce harnais).

test.describe("Harnais de convergence multi-poste (#12, tâche 8.1)", () => {
  let server: MockSyncServer;
  let posteA: Workstation;
  let posteB: Workstation;

  test.beforeEach(async ({ browser }) => {
    server = new MockSyncServer();
    posteA = await createWorkstation(browser, server);
    posteB = await createWorkstation(browser, server);
  });

  test.afterEach(async () => {
    await posteA?.close();
    await posteB?.close();
  });

  test("les deux postes isolés chargent l'app", async () => {
    // En-tête de l'app (i18n `app.title`), rendu par `App.tsx` dans les deux
    // contextes : preuve que la build de prod est servie et que chaque poste
    // (contexte isolé) a bien amorcé l'application.
    const titre = "Centres médicaux du Togo";
    await expect(posteA.page.getByText(titre)).toBeVisible();
    await expect(posteB.page.getByText(titre)).toBeVisible();

    // La navigation inférieure (onglet Carte actif par défaut) est présente des
    // deux côtés : condition observable, aucune temporisation arbitraire.
    await expect(
      posteA.page.getByRole("button", { name: "Carte" }),
    ).toBeVisible();
    await expect(
      posteB.page.getByRole("button", { name: "Carte" }),
    ).toBeVisible();
  });

  test("les deux postes sont des contextes isolés distincts", () => {
    // Garde-fou M1 : deux `BrowserContext` distincts (donc deux bases locales
    // `dme_local` isolées), jamais deux `page` d'un même contexte.
    expect(posteA.context).not.toBe(posteB.context);
    expect(posteA.page).not.toBe(posteB.page);
  });

  test("la connectivité se pilote par contexte (online/offline)", async () => {
    // Bascules hors ligne/en ligne sans réseau réel : la connectivité est
    // pilotée au niveau du contexte (`setOffline`). On vérifie que l'app reste
    // chargée (condition observable) après les bascules.
    await posteA.goOffline();
    await posteB.goOffline();
    await expect(
      posteA.page.getByText("Centres médicaux du Togo"),
    ).toBeVisible();

    await posteA.goOnline();
    await posteB.goOnline();
    await expect(
      posteB.page.getByText("Centres médicaux du Togo"),
    ).toBeVisible();
  });

  test("le mock d'API répond de façon déterministe à push/pull (idempotence)", async () => {
    // Exerce directement le serveur simulé (unité du harnais) : l'idempotence
    // par `mutation_id` (P10) et la convergence au pull sont la base de 8.2.
    const now = "2026-01-01T10:00:00.000Z";
    const mutation: Mutation = {
      mutation_id: "11111111-1111-1111-1111-111111111111",
      entity: "patient",
      entity_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      op: "upsert",
      payload: { given_name: "Koffi", family_name: "Mensah" },
      client_version: 1,
      client_updated_at: now,
      event_at: now,
    };

    const first = server.handlePush({ mutations: [mutation] });
    expect(first.results[0].status).toBe("applied");

    // Rejeu de la MÊME mutation : `duplicate`, pas de doublon ni de perte.
    const replay = server.handlePush({ mutations: [mutation] });
    expect(replay.results[0].status).toBe("duplicate");
    expect(server.appliedCount).toBe(1);

    // Un pull depuis l'origine ramène le changement (convergence au pull).
    const pull = server.handlePull(null);
    expect(pull.changes).toHaveLength(1);
    expect(pull.changes[0].entity_id).toBe(mutation.entity_id);
    expect(pull.revoked).toBe(false);
  });
});

// -------------------------------------------------------------------------
// Scénario de convergence multi-poste (tâche 8.2) — exigences 2.2, 2.3, 2.4
// -------------------------------------------------------------------------
//
// Déroulé (deux postes du MÊME centre, base locale `dme_local` isolée par
// contexte — M1) :
//
//  Phase 0 (en ligne) — amorçage d'une entité PARTAGÉE.
//     Poste A crée un patient et une visite (avec note), les pousse ; poste B
//     tire ces deltas. Les deux postes détiennent alors la MÊME visite (même
//     `entity_id`), prérequis d'une édition concurrente.
//
//  Phase 1 (les deux hors ligne) — travail divergent.
//     - A crée son propre patient (P_A2) ; B crée le sien (P_B).
//     - B édite la note de la visite partagée (note B), PUIS A l'édite (note A).
//       B édite en premier ⇒ son `client_updated_at` est antérieur ⇒ A gagne le
//       LWW. (Déterminisme renforcé : A reconnecte et pousse en premier, donc la
//       mutation de B — plus ancienne ou à version égale — perd à coup sûr.)
//
//  Phase 2 (reconnexion ordonnée) — convergence + conflit.
//     - A reconnecte (push gagnant de la note + P_A2, puis pull).
//     - B reconnecte (push PERDANT de sa note ⇒ conflit sensible ; P_B poussé ;
//       pull des deltas de A ⇒ P_A2 arrive chez B).
//     - Puis A tire une dernière fois pour recevoir P_B (convergence complète).
//
//  Assertions :
//     - CONVERGENCE SANS PERTE NI DOUBLON (idempotence `mutation_id`, P10) :
//       patients de A et de B présents des DEUX côtés, exactement une fois ;
//       `appliedCount` du serveur = nombre de mutations distinctes (pas de
//       réapplication des rejeux).
//     - CONFLIT DE NOTE DE VISITE MARQUÉ POUR REVUE (exigence 2.3, P12) : la
//       bannière de conflit est visible côté B, en variante SENSIBLE (note de
//       visite), et non écrasée silencieusement.

test.describe("Convergence multi-poste de bout en bout (#12, tâche 8.2)", () => {
  let server: MockSyncServer;
  let posteA: Workstation;
  let posteB: Workstation;

  test.beforeEach(async ({ browser }) => {
    server = new MockSyncServer();
    posteA = await createWorkstation(browser, server);
    posteB = await createWorkstation(browser, server);
  });

  test.afterEach(async () => {
    await posteA?.close();
    await posteB?.close();
  });

  test("deux postes hors ligne convergent à la reconnexion ; le conflit de note de visite est marqué pour revue", async () => {
    // Libellés déterministes (évitent toute ambiguïté de sélecteur).
    const SHARED_REASON = "Consultation partagee";
    const NOTE_SEED = "Note initiale partagee";
    const NOTE_B = "Note redigee par le poste B";
    const NOTE_A = "Note redigee par le poste A";

    // === Phase 0 — amorçage d'une entité partagée (en ligne) ===============
    // Les deux postes se connectent EN LIGNE (la 1re connexion exige le réseau :
    // `session.ts` appelle `/api/auth/login`). Le mock renvoie un jeton tenant.
    await login(posteA);
    await login(posteB);

    // A crée un patient puis une visite partagée, et pousse le tout.
    await openPatients(posteA);
    await createPatientUI(posteA, {
      mrn: "MRN-A1",
      given: "Ama",
      family: "Koudjo",
    });
    await openVisits(posteA);
    await createVisitUI(posteA, { reason: SHARED_REASON, notes: NOTE_SEED });

    // Pousser l'amorçage de A vers le serveur (outbox non vide ⇒ push + pull).
    await posteA.goOffline();
    await reconnectPushAndPull(posteA);

    // B tire les deltas de A : il détient désormais la MÊME visite partagée.
    await posteB.goOffline();
    await reconnectPullOnly(posteB);

    // Preuve observable côté B : la visite partagée (son motif) est présente,
    // tirée depuis le serveur (convergence au pull).
    await openVisits(posteB);
    await expect(posteB.page.getByText(SHARED_REASON)).toBeVisible();

    // === Phase 1 — travail divergent des DEUX côtés, hors ligne ============
    await posteA.goOffline();
    await posteB.goOffline();

    // Chaque poste crée son propre patient (entités distinctes : convergence
    // sans conflit attendue).
    await backToAccount(posteB);
    await openPatients(posteB);
    await createPatientUI(posteB, {
      mrn: "MRN-B1",
      given: "Bea",
      family: "Mensah",
    });

    await backToAccount(posteA);
    await openPatients(posteA);
    await createPatientUI(posteA, {
      mrn: "MRN-A2",
      given: "Afi",
      family: "Dossou",
    });

    // Identifiant de la visite PARTAGÉE côté serveur (unique à ce stade) :
    // permet de constater de façon observable, sans `sleep`, QUAND l'édition
    // gagnante a atteint le serveur avant de reconnecter le poste perdant.
    const sharedVisitId = server.entityIds("visit")[0];

    // Édition CONCURRENTE de la MÊME note de visite. B édite EN PREMIER puis A :
    // l'ordre de reconnexion (A gagnant d'abord) rend le vainqueur déterministe.
    await openVisits(posteB);
    await editVisitNoteUI(posteB, SHARED_REASON, NOTE_B);

    await openVisits(posteA);
    await editVisitNoteUI(posteA, SHARED_REASON, NOTE_A);

    // === Phase 2 — reconnexion ORDONNÉE : A d'abord (gagnant), puis B ======
    //
    // Déterminisme (exigence 2.4) : A reconnecte et on ATTEND, par observation
    // de l'état serveur partagé, que l'édition gagnante de A y soit appliquée —
    // AVANT de reconnecter B. Le push de l'outbox de A peut prendre plusieurs
    // cycles (patient puis visite) ; on n'attend pas un push unique mais la
    // CONDITION observable « la note de A est sur le serveur ». B ne pousse
    // qu'ensuite ⇒ sa note (antérieure) perd le LWW ⇒ conflit.
    await pushUntil(
      posteA,
      () => server.getEntity("visit", sharedVisitId)?.payload.notes === NOTE_A,
    );
    // Condition observable confirmée : la note gagnante de A est sur le serveur.
    await expect
      .poll(() => server.getEntity("visit", sharedVisitId)?.payload.notes, {
        timeout: 10_000,
      })
      .toBe(NOTE_A);

    // B reconnecte : son édition de note PERD (note de A déjà gagnante sur le
    // serveur) ⇒ conflit sensible. Son patient P_B est poussé ; son pull tire
    // les deltas de A (dont P_A2 et la note gagnante de A). On draine jusqu'à ce
    // que le serveur enregistre le conflit (condition observable).
    await pushUntil(posteB, () => server.conflictCount >= 1);
    expect(server.conflictCount).toBe(1);

    // Convergence complète : le patient de B est déjà appliqué côté serveur
    // (condition observable) ; A tire une dernière fois pour le recevoir.
    expect(server.entityIds("patient").length).toBe(3);
    await posteA.goOffline();
    await reconnectPullOnly(posteA);

    // === Assertions : conflit de note de visite marqué pour revue (2.3) ====
    // Côté B, la bannière de conflit (role="alert") est visible et en variante
    // SENSIBLE (note de visite) : le perdant est SURFACÉ, non écrasé en silence.
    const bannerB = posteB.page.getByRole("alert");
    await expect(bannerB).toBeVisible();
    await expect(
      posteB.page.getByText("conflit sensible à revoir", { exact: false }),
    ).toBeVisible();
    // L'entité en conflit est bien une VISITE (étiquette de détail).
    await posteB.page
      .getByRole("button", { name: "Voir les détails" })
      .click();
    await expect(
      posteB.page.getByText("Sensible", { exact: true }),
    ).toBeVisible();

    // === Assertions : convergence sans perte ni doublon (2.2, P10) =========
    // Les patients des DEUX postes existent des DEUX côtés, exactement une fois.
    for (const ws of [posteA, posteB]) {
      await backToAccount(ws);
      await openPatients(ws);
      // Patient créé par A (amorçage), par A hors ligne, et par B hors ligne.
      await expect(ws.page.getByText("Ama Koudjo")).toHaveCount(1);
      await expect(ws.page.getByText("Afi Dossou")).toHaveCount(1);
      await expect(ws.page.getByText("Bea Mensah")).toHaveCount(1);
    }

    // La note GAGNANTE (A) a convergé des deux côtés sur la visite partagée.
    for (const ws of [posteA, posteB]) {
      await openVisits(ws);
      await expect(ws.page.getByText(NOTE_A)).toBeVisible();
    }

    // Idempotence serveur : le nombre de mutations DISTINCTES appliquées est fini
    // et ne compte pas les rejeux (`duplicate`). Mutations appliquées attendues :
    //   amorçage : patient A1 + visite (upsert initial)           → 2
    //   hors ligne : patient A2, patient B1                       → 2
    //   édition gagnante de la note (A)                           → 1
    //   (l'édition de B est un CONFLIT : non appliquée)
    // soit 5 applications distinctes ; aucune réapplication de rejeu.
    expect(server.appliedCount).toBe(5);
  });
});
