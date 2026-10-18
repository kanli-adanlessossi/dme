// Moteur de synchronisation : outbox + delta-sync (push/pull) — NFR-5/6/7/13.
//
// Rôle (tâche 14.4)
// -----------------
// Seul composant qui parle à l'API tenant. Les pages écrivent uniquement dans la
// base locale (`db.ts`), qui met en file les mutations dans l'outbox. Ce module :
//
//  1. **Push** l'outbox vers `POST /api/sync/push` (lot idempotent, ordre de
//     création). Clé d'idempotence = `id` de l'entrée d'outbox (= `mutation_id`).
//     Les entrées confirmées `applied` ou `duplicate` sont retirées de l'outbox ;
//     tout le reste y demeure et sera rejoué (reprise après coupure sans doublon :
//     c'est le serveur qui garantit l'idempotence via `mutation_id`, P10).
//  2. **Pull** les deltas depuis `GET /api/sync/pull?since=<curseur>` et les
//     applique dans la base locale **sans** les ré-enfiler (écriture directe via
//     `applyRemoteChange`). Le curseur opaque est persisté (localStorage) pour
//     reprendre le delta-sync après rechargement. Pagination via `has_more`.
//  3. **Révocation** (NFR-7) : si le pull renvoie `revoked=true` (ou un 401/403),
//     la session locale et les PHI sont purgées via `destroyDb()` ; le curseur
//     est effacé. 14.5 (session) s'appuiera sur cet état.
//  4. **Déclenchement** : automatique au retour de connectivité (`online`), à
//     intervalle régulier, et manuellement via `syncNow()`.
//  5. **État UI** (NFR-5) : connectivité (en ligne/hors ligne) + nombre
//     d'éléments en attente (taille de l'outbox) + conflits à revoir, exposés via
//     des observables RxJS pour `SyncStatusBar` / `ConflictBanner`.
//
// Mapping outbox → contrat figé
// ------------------------------
// Le contrat (`backend/app/schemas/sync.py`) ne connaît que les entités
// `patient`/`visit` et les opérations `upsert`/`delete`. L'outbox local porte
// `operation` ∈ insert|update|delete et une collection `center_profile`.
//  - operation `insert`/`update` → op `upsert` ; `delete` → op `delete`.
//  - `center_profile` N'EST PAS dans le contrat de sync : il possède son propre
//    endpoint REST dédié `GET/PATCH /api/center/profile` (cf. `routers/center.py`
//    côté serveur, `api.ts` → `fetchCenterProfile`/`updateCenterProfile` côté
//    client, appelés par `CenterAdmin.tsx`). Le profil n'est donc **jamais**
//    inscrit dans l'outbox (`upsertCenterProfile` écrit en local sans l'enfiler),
//    de sorte que l'outbox reste borné malgré des éditions répétées du profil.

import { BehaviorSubject, type Observable } from "rxjs";

import {
  isAuthError,
  syncPull,
  syncPush,
  type Mutation,
  type SyncConflict,
  type SyncEntity,
  type TokenProvider,
} from "./api";

// Ré-export pour les consommateurs UI (SyncStatusBar / ConflictBanner) : ils
// n'ont pas à connaître `api.ts` directement.
export type { SyncConflict } from "./api";
import {
  applyRemoteChange,
  destroyDb,
  isDbOpen,
  listOutbox,
  removeOutboxEntry,
  type OutboxDoc,
} from "./db";

// -------------------------------------------------------------------------
// État observable pour l'UI (SyncStatusBar / ConflictBanner)
// -------------------------------------------------------------------------

/** Phase courante du moteur de sync. */
export type SyncPhase = "idle" | "syncing" | "error" | "revoked";

/** Instantané d'état exposé à l'UI (NFR-5). */
export interface SyncState {
  /** Connectivité réseau (navigator.onLine + événements online/offline). */
  online: boolean;
  /** Nombre de mutations en attente dans l'outbox. */
  pending: number;
  phase: SyncPhase;
  /** Horodatage ISO de la dernière synchro réussie (ou `null`). */
  lastSyncedAt: string | null;
  /** Dernier message d'erreur (ou `null`). */
  lastError: string | null;
}

const CURSOR_KEY = "dme.sync.cursor";

function readCursor(): string | null {
  try {
    return localStorage.getItem(CURSOR_KEY);
  } catch {
    return null;
  }
}

function writeCursor(cursor: string): void {
  try {
    localStorage.setItem(CURSOR_KEY, cursor);
  } catch {
    /* localStorage indisponible (mode privé strict) : sync non-persistée. */
  }
}

function clearCursor(): void {
  try {
    localStorage.removeItem(CURSOR_KEY);
  } catch {
    /* ignore */
  }
}

function isOnline(): boolean {
  return typeof navigator !== "undefined" ? navigator.onLine : true;
}

// -------------------------------------------------------------------------
// Mapping outbox → mutation du contrat
// -------------------------------------------------------------------------

/** Collections synchronisables par le protocole figé. */
const SYNC_ENTITIES: ReadonlySet<string> = new Set<SyncEntity>([
  "patient",
  "visit",
]);

/**
 * Convertit une entrée d'outbox en `Mutation` du contrat figé, ou `null` si la
 * collection n'est pas synchronisable par ce protocole. `center_profile` n'est
 * jamais enfilé dans l'outbox (il est synchronisé via son propre endpoint REST,
 * voir l'en-tête de fichier) ; ce garde `null` reste néanmoins défensif.
 */
function toMutation(entry: OutboxDoc): Mutation | null {
  if (!SYNC_ENTITIES.has(entry.collection_name)) return null;

  const op = entry.operation === "delete" ? "delete" : "upsert";
  const snapshot = (entry.snapshot ?? {}) as Record<string, unknown>;

  // `client_version` / `client_updated_at` proviennent du snapshot de l'entité
  // (posés par les helpers CRUD de db.ts). Valeurs de repli défensives.
  const clientVersion =
    typeof snapshot.version === "number" && snapshot.version >= 1
      ? snapshot.version
      : 1;
  const clientUpdatedAt =
    typeof snapshot.updated_at === "string"
      ? snapshot.updated_at
      : entry.created_at;

  return {
    mutation_id: entry.id,
    entity: entry.collection_name as SyncEntity,
    entity_id: entry.entity_id,
    op,
    // Le serveur appose le tenant_id depuis le jeton : on ne l'envoie jamais.
    payload: op === "delete" ? {} : stripTenant(snapshot),
    client_version: clientVersion,
    client_updated_at: clientUpdatedAt,
    event_at: entry.created_at,
  };
}

/** Retire `tenant_id` du payload (apposé côté serveur depuis le jeton, P2). */
function stripTenant(snapshot: Record<string, unknown>): Record<string, unknown> {
  const { tenant_id: _ignored, ...rest } = snapshot;
  void _ignored;
  return rest;
}

// -------------------------------------------------------------------------
// Moteur de synchronisation
// -------------------------------------------------------------------------

export interface SyncEngineOptions {
  /** Fournisseur de jeton Bearer (branché par la session, 14.5). */
  getToken: TokenProvider;
  /** Intervalle d'auto-sync en ms (défaut 30 s). `0` désactive l'intervalle. */
  intervalMs?: number;
}

const DEFAULT_INTERVAL_MS = 30_000;

export class SyncEngine {
  private readonly getToken: TokenProvider;
  private readonly intervalMs: number;

  private readonly state$: BehaviorSubject<SyncState>;
  private readonly conflicts$: BehaviorSubject<SyncConflict[]>;

  private timer: ReturnType<typeof setInterval> | null = null;
  private running = false;
  /** Sync en cours : évite les exécutions concurrentes (coalescence). */
  private inFlight: Promise<void> | null = null;
  /** Une sync a été demandée pendant qu'une autre tournait → relancer. */
  private rerun = false;

  private readonly onOnline = () => {
    this.patch({ online: true });
    void this.syncNow();
  };
  private readonly onOffline = () => this.patch({ online: false });

  constructor(options: SyncEngineOptions) {
    this.getToken = options.getToken;
    this.intervalMs = options.intervalMs ?? DEFAULT_INTERVAL_MS;
    this.state$ = new BehaviorSubject<SyncState>({
      online: isOnline(),
      pending: 0,
      phase: "idle",
      lastSyncedAt: null,
      lastError: null,
    });
    this.conflicts$ = new BehaviorSubject<SyncConflict[]>([]);
  }

  /** État de sync observable (connectivité + file d'attente + phase). */
  get state(): Observable<SyncState> {
    return this.state$.asObservable();
  }

  /** Lecture synchrone de l'état courant. */
  get snapshot(): SyncState {
    return this.state$.getValue();
  }

  /** Conflits à revoir observables (alimente ConflictBanner, tâche 17). */
  get conflicts(): Observable<SyncConflict[]> {
    return this.conflicts$.asObservable();
  }

  /** Acquitte (vide) la liste des conflits une fois revus par l'utilisateur. */
  clearConflicts(): void {
    this.conflicts$.next([]);
  }

  /**
   * Démarre le moteur : écoute online/offline, lance l'intervalle d'auto-sync et
   * tente une première synchro. Idempotent.
   */
  start(): void {
    if (this.running) return;
    this.running = true;

    if (typeof window !== "undefined") {
      window.addEventListener("online", this.onOnline);
      window.addEventListener("offline", this.onOffline);
    }
    if (this.intervalMs > 0) {
      this.timer = setInterval(() => {
        void this.syncNow();
      }, this.intervalMs);
    }

    void this.refreshPending();
    void this.syncNow();
  }

  /** Arrête le moteur (retrait des écouteurs + intervalle). Idempotent. */
  stop(): void {
    if (!this.running) return;
    this.running = false;
    if (typeof window !== "undefined") {
      window.removeEventListener("online", this.onOnline);
      window.removeEventListener("offline", this.onOffline);
    }
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  /**
   * Déclenche une synchro (push puis pull). Les appels concurrents sont
   * coalescés : si une synchro tourne déjà, une seule relance est programmée.
   */
  syncNow(): Promise<void> {
    if (this.inFlight) {
      this.rerun = true;
      return this.inFlight;
    }
    this.inFlight = this.runCycle().finally(() => {
      this.inFlight = null;
      if (this.rerun) {
        this.rerun = false;
        void this.syncNow();
      }
    });
    return this.inFlight;
  }

  private async runCycle(): Promise<void> {
    // Rien à faire si la base n'est pas ouverte (hors session) ou si hors ligne.
    if (!isDbOpen()) {
      await this.refreshPending();
      return;
    }
    if (!isOnline()) {
      this.patch({ online: false });
      await this.refreshPending();
      return;
    }

    this.patch({ phase: "syncing", lastError: null });
    try {
      await this.pushOutbox();
      await this.pullDeltas();
      // Si un `pull` a renvoyé `revoked=true`, `handleRevocation()` a déjà posé
      // la phase `revoked` (et purgé la base) : ne pas l'écraser par `idle`.
      if (this.state$.getValue().phase !== "revoked") {
        this.patch({
          phase: "idle",
          lastSyncedAt: new Date().toISOString(),
          lastError: null,
        });
      }
    } catch (err) {
      if (isAuthError(err)) {
        // 401/403 => accès révoqué : purge locale (NFR-7).
        await this.handleRevocation();
        return;
      }
      this.patch({
        phase: "error",
        lastError: err instanceof Error ? err.message : String(err),
      });
    } finally {
      await this.refreshPending();
    }
  }

  /**
   * Pousse l'outbox par lot (ordre de création). Seules les mutations confirmées
   * `applied`/`duplicate` sont retirées : une coupure en vol laisse le reste en
   * file, rejoué sans doublon grâce à l'idempotence serveur (P10).
   */
  private async pushOutbox(): Promise<void> {
    const entries = await listOutbox();
    if (entries.length === 0) return;

    // Mapping + mise de côté des entrées non synchronisables par ce protocole.
    const mutations: Mutation[] = [];
    const byMutationId = new Map<string, OutboxDoc>();
    for (const entry of entries) {
      const mutation = toMutation(entry);
      if (!mutation) continue; // non synchronisable (garde défensif) : on ignore.
      mutations.push(mutation);
      byMutationId.set(mutation.mutation_id, entry);
    }
    if (mutations.length === 0) return;

    const res = await syncPush({ mutations }, this.getToken);

    // Retrait des mutations confirmées appliquées ou déjà appliquées (rejeu).
    for (const result of res.results) {
      if (result.status === "applied" || result.status === "duplicate") {
        await removeOutboxEntry(result.mutation_id);
      }
      // `conflict` : le serveur a tranché (LWW) et journalisé le perdant ; la
      // mutation locale est obsolète, on la retire pour ne pas la rejouer en
      // boucle (le pull ramènera l'état gagnant). `rejected` : laissé en file.
      if (result.status === "conflict") {
        await removeOutboxEntry(result.mutation_id);
      }
    }

    // Surface des conflits sensibles à revoir (ConflictBanner, tâche 17).
    if (res.conflicts.length > 0) {
      const merged = [...this.conflicts$.getValue(), ...res.conflicts];
      this.conflicts$.next(merged);
    }
  }

  /**
   * Récupère et applique les deltas depuis le curseur persisté, en paginant tant
   * que `has_more`. Applique chaque changement sans ré-enfiler (origine serveur).
   */
  private async pullDeltas(): Promise<void> {
    let cursor = readCursor();
    // Garde-fou contre une pagination non bornée (serveur mal configuré).
    for (let page = 0; page < 1000; page++) {
      const res = await syncPull(cursor, this.getToken);

      if (res.revoked) {
        await this.handleRevocation();
        return;
      }

      for (const change of res.changes) {
        await applyRemoteChange({
          entity: change.entity,
          entity_id: change.entity_id,
          op: change.op,
          payload: change.payload ?? {},
          version: change.version,
          updated_at: change.updated_at,
          deleted: change.deleted,
        });
      }

      // Le curseur est opaque : on le persiste tel quel après avoir appliqué la
      // page, pour reprendre exactement ici après rechargement/coupure.
      cursor = res.cursor;
      writeCursor(cursor);

      if (!res.has_more) break;
    }
  }

  /** Purge locale sur révocation (NFR-7) : efface PHI + curseur, phase revoked. */
  private async handleRevocation(): Promise<void> {
    try {
      await destroyDb();
    } finally {
      clearCursor();
      this.conflicts$.next([]);
      this.patch({ phase: "revoked", pending: 0, lastError: null });
    }
  }

  /** Rafraîchit le compteur d'éléments en attente (taille de l'outbox). */
  private async refreshPending(): Promise<void> {
    if (!isDbOpen()) {
      this.patch({ pending: 0, online: isOnline() });
      return;
    }
    try {
      const entries = await listOutbox();
      this.patch({ pending: entries.length, online: isOnline() });
    } catch {
      this.patch({ online: isOnline() });
    }
  }

  private patch(partial: Partial<SyncState>): void {
    this.state$.next({ ...this.state$.getValue(), ...partial });
  }
}

// -------------------------------------------------------------------------
// Singleton de commodité
// -------------------------------------------------------------------------
//
// La plupart de l'app n'a besoin que d'un moteur. On expose un singleton
// paresseux configurable ; 14.5 appellera `configureSync({ getToken })` avec le
// fournisseur de jeton de la session, puis `getSyncEngine().start()`.

let singleton: SyncEngine | null = null;
let pendingToken: TokenProvider = () => null;

/**
 * Configure (ou reconfigure) le moteur singleton avec un fournisseur de jeton.
 * À appeler avant `getSyncEngine()` (typiquement par la session, 14.5).
 */
export function configureSync(options: SyncEngineOptions): SyncEngine {
  pendingToken = options.getToken;
  if (singleton) singleton.stop();
  singleton = new SyncEngine(options);
  return singleton;
}

/** Renvoie le moteur singleton (le crée avec le token courant si absent). */
export function getSyncEngine(): SyncEngine {
  if (!singleton) {
    singleton = new SyncEngine({ getToken: pendingToken });
  }
  return singleton;
}

/** Raccourci : déclenche une synchro immédiate sur le moteur singleton. */
export function syncNow(): Promise<void> {
  return getSyncEngine().syncNow();
}
