// Tests CRUD hors ligne depuis la base locale (P9) + non-réenfilage à l'application
// de deltas serveur (P10, partie `applyRemoteChange`).
//
// P9 — Disponibilité hors ligne : après initDb, toutes les opérations cliniques
// courantes (create/read/update/soft-delete patient & visit) réussissent contre
// la base LOCALE, sans réseau. Aucune écriture n'est perdue : chaque mutation
// inscrit une entrée d'outbox ; la suppression est logique (deleted_at posé, ligne
// conservée).
//
// Hermétique : aucun appel réseau. Le setup global stub `fetch` pour échouer si
// un module tentait un appel ; ces tests ne touchent que db.ts (base locale).

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  applyRemoteChange,
  createPatient,
  createVisit,
  destroyDb,
  getDb,
  initDb,
  isDbOpen,
  listOutbox,
  softDeletePatient,
  softDeleteVisit,
  updatePatient,
  updateVisit,
  type NewPatientInput,
  type NewVisitInput,
} from "./db";

const TEST_KEY = "cle-de-test-aes-32chars-minimum!!";
const TENANT = "11111111-1111-7111-8111-111111111111";

function newPatient(overrides: Partial<NewPatientInput> = {}): NewPatientInput {
  return {
    tenant_id: TENANT,
    mrn: "MRN-001",
    given_name: "Koffi",
    family_name: "Mensah",
    dob: "1990-05-01",
    sex: "M",
    phone: "+22890000000",
    national_id: null,
    consent_to_store: true,
    created_by: null,
    ...overrides,
  };
}

function newVisit(patientId: string, overrides: Partial<NewVisitInput> = {}): NewVisitInput {
  return {
    tenant_id: TENANT,
    patient_id: patientId,
    visited_at: "2024-01-10T09:00:00.000Z",
    reason: "Consultation générale",
    notes: "Tension normale.",
    created_by: null,
    ...overrides,
  };
}

beforeEach(async () => {
  await initDb(TEST_KEY);
});

afterEach(async () => {
  // Purge complète entre tests pour isoler l'état (base + outbox).
  if (isDbOpen()) await destroyDb();
});

describe("P9 — CRUD patient hors ligne depuis la base locale", () => {
  it("crée un patient, le relit, et inscrit une entrée d'outbox", async () => {
    const created = await createPatient(newPatient());
    expect(created.id).toBeTruthy();

    const db = await getDb();
    const read = await db.patient.findOne(created.id).exec();
    expect(read).not.toBeNull();
    expect(read!.given_name).toBe("Koffi");
    expect(read!.family_name).toBe("Mensah");
    expect(read!.version).toBe(1);
    expect(read!.deleted_at).toBeNull();

    const outbox = await listOutbox();
    expect(outbox).toHaveLength(1);
    expect(outbox[0]).toMatchObject({
      operation: "insert",
      collection_name: "patient",
      entity_id: created.id,
    });
  });

  it("met à jour un patient (bump version/updated_at) et enfile la mutation", async () => {
    const created = await createPatient(newPatient());
    const before = created.updated_at;

    // Petit délai pour garantir un updated_at distinct.
    await new Promise((r) => setTimeout(r, 2));
    const updated = await updatePatient(created.id, { phone: "+22891111111" });

    expect(updated).not.toBeNull();
    expect(updated!.phone).toBe("+22891111111");
    expect(updated!.version).toBe(2);
    expect(updated!.updated_at >= before).toBe(true);

    const outbox = await listOutbox();
    // insert + update
    expect(outbox).toHaveLength(2);
    expect(outbox.map((e) => e.operation)).toEqual(["insert", "update"]);
  });

  it("supprime logiquement un patient : deleted_at posé, ligne conservée", async () => {
    const created = await createPatient(newPatient());
    const deleted = await softDeletePatient(created.id);

    expect(deleted).not.toBeNull();
    expect(deleted!.deleted_at).toBeTruthy();

    // La ligne EST toujours présente dans le stockage (suppression logique).
    const db = await getDb();
    const stillThere = await db.patient.findOne(created.id).exec();
    expect(stillThere).not.toBeNull();
    expect(stillThere!.deleted_at).toBeTruthy();

    // Mutation delete enfilée (aucune écriture perdue).
    const outbox = await listOutbox();
    expect(outbox.map((e) => e.operation)).toContain("delete");
    expect(outbox).toHaveLength(2); // insert + delete
  });

  it("ne modifie/supprime pas un patient déjà supprimé (idempotence locale)", async () => {
    const created = await createPatient(newPatient());
    await softDeletePatient(created.id);

    // Une seconde suppression ne doit rien faire (retourne null).
    const second = await softDeletePatient(created.id);
    expect(second).toBeNull();
    // Une mise à jour sur un supprimé ne doit rien faire non plus.
    const upd = await updatePatient(created.id, { phone: "x" });
    expect(upd).toBeNull();
  });
});

describe("P9 — CRUD visite hors ligne depuis la base locale", () => {
  it("crée/lit/met à jour/supprime logiquement une visite + outbox", async () => {
    const patient = await createPatient(newPatient());
    const visit = await createVisit(newVisit(patient.id));

    const db = await getDb();
    const readVisit = await db.visit.findOne(visit.id).exec();
    expect(readVisit).not.toBeNull();
    expect(readVisit!.reason).toBe("Consultation générale");

    const updated = await updateVisit(visit.id, { notes: "Révisé." });
    expect(updated!.notes).toBe("Révisé.");
    expect(updated!.version).toBe(2);

    const deleted = await softDeleteVisit(visit.id);
    expect(deleted!.deleted_at).toBeTruthy();
    const stillThere = await db.visit.findOne(visit.id).exec();
    expect(stillThere).not.toBeNull();

    // outbox : patient insert + visit insert + visit update + visit delete.
    const outbox = await listOutbox();
    const visitEntries = outbox.filter((e) => e.collection_name === "visit");
    expect(visitEntries.map((e) => e.operation)).toEqual([
      "insert",
      "update",
      "delete",
    ]);
  });
});

describe("P9 — aucune écriture perdue : chaque mutation enfile exactement une entrée", () => {
  it("accumule une entrée d'outbox par opération mutante", async () => {
    const p1 = await createPatient(newPatient({ mrn: "A" }));
    const p2 = await createPatient(newPatient({ mrn: "B" }));
    await updatePatient(p1.id, { phone: "1" });
    await createVisit(newVisit(p2.id));
    await softDeletePatient(p2.id);

    const outbox = await listOutbox();
    // 2 inserts patient + 1 update patient + 1 insert visit + 1 delete patient
    expect(outbox).toHaveLength(5);
    // Ordre de création préservé (listOutbox trie par created_at asc).
    const createdAts = outbox.map((e) => e.created_at);
    const sorted = [...createdAts].sort();
    expect(createdAts).toEqual(sorted);
  });
});

describe("P10 (partie locale) — applyRemoteChange ne réenfile pas dans l'outbox", () => {
  it("applique un delta serveur upsert SANS créer d'entrée d'outbox", async () => {
    const entityId = "22222222-2222-7222-8222-222222222222";
    await applyRemoteChange({
      entity: "patient",
      entity_id: entityId,
      op: "upsert",
      payload: {
        tenant_id: TENANT,
        mrn: "SRV-1",
        given_name: "Ama",
        family_name: "Dossou",
        consent_to_store: true,
        created_at: "2024-01-01T00:00:00.000Z",
      },
      version: 7,
      updated_at: "2024-02-01T00:00:00.000Z",
      deleted: false,
    });

    const db = await getDb();
    const read = await db.patient.findOne(entityId).exec();
    expect(read).not.toBeNull();
    expect(read!.version).toBe(7);
    expect(read!.given_name).toBe("Ama");

    // Point clé P10 : aucun réenfilage — sinon boucle de sync infinie.
    const outbox = await listOutbox();
    expect(outbox).toHaveLength(0);
  });

  it("applique un delta serveur delete (deleted_at posé) sans outbox", async () => {
    const entityId = "33333333-3333-7333-8333-333333333333";
    // D'abord présent via un delta upsert.
    await applyRemoteChange({
      entity: "patient",
      entity_id: entityId,
      op: "upsert",
      payload: {
        tenant_id: TENANT,
        mrn: "SRV-2",
        given_name: "Yao",
        family_name: "Agbeko",
        consent_to_store: true,
        created_at: "2024-01-01T00:00:00.000Z",
      },
      version: 1,
      updated_at: "2024-01-01T00:00:00.000Z",
      deleted: false,
    });
    // Puis supprimé côté serveur.
    await applyRemoteChange({
      entity: "patient",
      entity_id: entityId,
      op: "delete",
      payload: {},
      version: 2,
      updated_at: "2024-03-01T00:00:00.000Z",
      deleted: true,
    });

    const db = await getDb();
    const read = await db.patient.findOne(entityId).exec();
    expect(read).not.toBeNull();
    expect(read!.deleted_at).toBeTruthy();

    const outbox = await listOutbox();
    expect(outbox).toHaveLength(0);
  });
});
