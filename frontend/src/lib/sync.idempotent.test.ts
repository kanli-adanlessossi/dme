// Rejeu idempotent et sans perte de l'outbox (P10) + application des deltas
// serveur sans réenfilage (P10) + révocation purge la session (P13 / NFR-7).
//
// Le moteur de sync (`sync.ts`) est le SEUL composant qui parle au réseau. On
// mocke donc `./api` (syncPush/syncPull) pour être hermétique : aucun backend
// requis. La base locale (`db.ts`) est réelle (fake-indexeddb) afin de vérifier
// les effets concrets sur l'outbox et les documents.
//
// Scénarios P10 vérifiés :
//   - un push `applied` retire l'entrée d'outbox ;
//   - **reprise après coupure** : une 1ʳᵉ tentative de push échoue (réseau),
//     l'entrée reste en file ; au 2ᵉ cycle le serveur renvoie `duplicate` pour
//     la même `mutation_id` (il l'avait déjà appliquée) → l'entrée est retirée
//     comme un `applied`, SANS double effet local ;
//   - l'id de mutation (UUIDv7) est bien la clé d'idempotence transmise ;
//   - les deltas tirés (`pull`) sont appliqués sans créer d'entrée d'outbox.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock du module réseau. Les implémentations sont injectées par test.
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual, // conserve ApiError, isAuthError, types…
    syncPush: vi.fn(),
    syncPull: vi.fn(),
  };
});

import { ApiError, type SyncPullResponse, type SyncPushResponse } from "./api";
import * as api from "./api";
import {
  createPatient,
  destroyDb,
  initDb,
  isDbOpen,
  listOutbox,
  type NewPatientInput,
} from "./db";
import { SyncEngine } from "./sync";

const TEST_KEY = "cle-de-test-aes-32chars-minimum!!";
const TENANT = "11111111-1111-7111-8111-111111111111";

const syncPushMock = vi.mocked(api.syncPush);
const syncPullMock = vi.mocked(api.syncPull);

function newPatient(overrides: Partial<NewPatientInput> = {}): NewPatientInput {
  return {
    tenant_id: TENANT,
    mrn: "MRN-001",
    given_name: "Koffi",
    family_name: "Mensah",
    dob: null,
    sex: null,
    phone: null,
    national_id: null,
    consent_to_store: true,
    created_by: null,
    ...overrides,
  };
}

/** Pull vide par défaut (pas de delta, pas de révocation). */
function emptyPull(): SyncPullResponse {
  return { changes: [], cursor: "c0", has_more: false, revoked: false };
}

function makeEngine(): SyncEngine {
  // intervalMs=0 : pas de timer. On pilote la sync manuellement via syncNow().
  return new SyncEngine({ getToken: () => "jeton-test", intervalMs: 0 });
}

beforeEach(async () => {
  localStorage.clear();
  await initDb(TEST_KEY);
  syncPushMock.mockReset();
  syncPullMock.mockReset();
  syncPullMock.mockResolvedValue(emptyPull());
});

afterEach(async () => {
  if (isDbOpen()) await destroyDb();
  localStorage.clear();
});

describe("P10 — push idempotent, sans perte", () => {
  it("retire l'entrée d'outbox quand le serveur répond `applied`", async () => {
    const patient = await createPatient(newPatient());
    const before = await listOutbox();
    expect(before).toHaveLength(1);
    const mutationId = before[0].id;

    syncPushMock.mockResolvedValueOnce({
      results: [
        {
          mutation_id: mutationId,
          entity: "patient",
          entity_id: patient.id,
          status: "applied",
          server_version: 1,
          conflict: null,
          message: null,
        },
      ],
      conflicts: [],
    } satisfies SyncPushResponse);

    await makeEngine().syncNow();

    // La mutation confirmée est retirée : plus rien à rejouer.
    expect(await listOutbox()).toHaveLength(0);
    // L'id de mutation (= clé d'idempotence) a bien été transmis.
    const pushedBody = syncPushMock.mock.calls[0][0];
    expect(pushedBody.mutations).toHaveLength(1);
    expect(pushedBody.mutations[0].mutation_id).toBe(mutationId);
    expect(pushedBody.mutations[0].entity).toBe("patient");
  });

  it("reprise après coupure : rejeu → `duplicate` retire l'entrée sans double effet", async () => {
    const patient = await createPatient(newPatient());
    const before = await listOutbox();
    const mutationId = before[0].id;

    // 1er cycle : coupure réseau en plein push → l'entrée DOIT rester en file.
    syncPushMock.mockRejectedValueOnce(new Error("network down"));
    const engine = makeEngine();
    await engine.syncNow();
    expect(await listOutbox()).toHaveLength(1); // rien perdu, à rejouer.

    // 2e cycle (reprise) : le serveur avait déjà appliqué la mutation lors de la
    // tentative coupée ; il répond donc `duplicate` pour la même mutation_id.
    syncPushMock.mockResolvedValueOnce({
      results: [
        {
          mutation_id: mutationId,
          entity: "patient",
          entity_id: patient.id,
          status: "duplicate",
          server_version: 1,
          conflict: null,
          message: "déjà appliqué",
        },
      ],
      conflicts: [],
    } satisfies SyncPushResponse);
    await engine.syncNow();

    // `duplicate` est traité comme `applied` : entrée retirée, aucun doublon.
    expect(await listOutbox()).toHaveLength(0);

    // Les DEUX tentatives ont transmis la MÊME mutation_id (idempotence).
    const firstTry = syncPushMock.mock.calls[0][0].mutations[0].mutation_id;
    const secondTry = syncPushMock.mock.calls[1][0].mutations[0].mutation_id;
    expect(firstTry).toBe(mutationId);
    expect(secondTry).toBe(mutationId);
  });

  it("laisse en file une mutation `rejected` (pas de perte, pas de retrait)", async () => {
    const patient = await createPatient(newPatient());
    const mutationId = (await listOutbox())[0].id;

    syncPushMock.mockResolvedValueOnce({
      results: [
        {
          mutation_id: mutationId,
          entity: "patient",
          entity_id: patient.id,
          status: "rejected",
          server_version: null,
          conflict: null,
          message: "validation",
        },
      ],
      conflicts: [],
    } satisfies SyncPushResponse);

    await makeEngine().syncNow();
    // `rejected` : l'entrée reste (ni appliquée ni dupliquée).
    expect(await listOutbox()).toHaveLength(1);
  });

  it("rejoue le lot entier à l'identique si tout le push échoue (sans perte)", async () => {
    await createPatient(newPatient({ mrn: "A" }));
    await createPatient(newPatient({ mrn: "B" }));
    const idsBefore = (await listOutbox()).map((e) => e.id).sort();
    expect(idsBefore).toHaveLength(2);

    syncPushMock.mockRejectedValue(new Error("still offline"));
    const engine = makeEngine();
    await engine.syncNow();
    await engine.syncNow();

    const idsAfter = (await listOutbox()).map((e) => e.id).sort();
    // Mêmes entrées, mêmes ids : rien n'a été perdu ni dupliqué.
    expect(idsAfter).toEqual(idsBefore);
  });
});

describe("P10 — pull applique les deltas sans réenfiler", () => {
  it("un delta tiré n'ajoute AUCUNE entrée d'outbox", async () => {
    // Pas de mutation locale : l'outbox part vide.
    expect(await listOutbox()).toHaveLength(0);

    const remoteId = "44444444-4444-7444-8444-444444444444";
    syncPushMock.mockResolvedValue({ results: [], conflicts: [] });
    syncPullMock.mockResolvedValueOnce({
      changes: [
        {
          entity: "patient",
          entity_id: remoteId,
          op: "upsert",
          payload: {
            mrn: "SRV-9",
            given_name: "Afi",
            family_name: "Lawson",
            consent_to_store: true,
            created_at: "2024-01-01T00:00:00.000Z",
          },
          version: 3,
          updated_at: "2024-02-01T00:00:00.000Z",
          deleted: false,
        },
      ],
      cursor: "c1",
      has_more: false,
      revoked: false,
    } satisfies SyncPullResponse);

    await makeEngine().syncNow();

    // Le delta a été appliqué localement mais SANS réenfilage (sinon boucle).
    expect(await listOutbox()).toHaveLength(0);
    // Le curseur opaque est persisté pour reprendre après rechargement.
    expect(localStorage.getItem("dme.sync.cursor")).toBe("c1");
  });
});

describe("P13 / NFR-7 — révocation détectée à la sync purge PHI et session", () => {
  it("pull `revoked=true` → destroyDb (base purgée) + phase revoked", async () => {
    // Donnée PHI locale présente avant la révocation.
    await createPatient(newPatient());
    expect(isDbOpen()).toBe(true);

    syncPushMock.mockResolvedValue({ results: [], conflicts: [] });
    syncPullMock.mockResolvedValueOnce({
      changes: [],
      cursor: "c-rev",
      has_more: false,
      revoked: true,
    } satisfies SyncPullResponse);

    const engine = makeEngine();
    await engine.syncNow();

    // La base a été détruite (PHI purgées) : isDbOpen false.
    expect(isDbOpen()).toBe(false);
    // Phase révoquée exposée à l'UI/session (session.ts purge ses jetons dessus).
    expect(engine.snapshot.phase).toBe("revoked");
    // Curseur effacé.
    expect(localStorage.getItem("dme.sync.cursor")).toBeNull();
  });

  it("un 401 au push → révocation (purge locale + phase revoked)", async () => {
    await createPatient(newPatient());
    expect(isDbOpen()).toBe(true);

    syncPushMock.mockRejectedValueOnce(new ApiError(401, "unauthorized"));

    const engine = makeEngine();
    await engine.syncNow();

    expect(isDbOpen()).toBe(false);
    expect(engine.snapshot.phase).toBe("revoked");
  });
});
