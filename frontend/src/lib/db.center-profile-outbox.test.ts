// G-6 — Fin de la fuite d'outbox `center_profile` (ADR Option 2).
//
// DoD de la tâche 6.2 : sous éditions répétées du profil du centre, l'outbox
// ne croît pas (elle reste bornée), aucun contrat de sync conflictuel n'est
// introduit (l'outbox ne porte jamais que `patient`/`visit`), et le profil est
// tout de même persisté localement (LWW : version bornée et incrémentée).
//
// Le profil se synchronise par REST direct (`PATCH /api/center/profile`) et
// n'est donc **jamais** inscrit dans l'outbox (cf. `upsertCenterProfile` dans
// `db.ts`). Ce test exerce la VRAIE fonction contre la VRAIE base locale
// (fake-indexeddb), sans mock, pour prouver que la fuite est corrigée sans
// pour autant « réussir à tort » en n'enregistrant rien.
//
// Références : design.md « G-6 » (Option 2 — profil via REST direct, hors du
// contrat de sync figé patient/visit) ; Requirement 6 ; NFR-5, NFR-6.

import { afterEach, describe, expect, it } from "vitest";
import {
  createPatient,
  createVisit,
  destroyDb,
  getDb,
  initDb,
  isDbOpen,
  listOutbox,
  upsertCenterProfile,
  type CenterProfileDoc,
  type NewPatientInput,
  type NewVisitInput,
} from "./db";
import { GOOD_KEY } from "../test/phi-fixtures";

const TENANT = "11111111-1111-7111-8111-111111111111";
const PROFILE_ID = "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa";

/** Entrée minimale valide pour `upsertCenterProfile` (requis : id, tenant_id, name). */
type CenterProfileInput = Omit<
  CenterProfileDoc,
  "updated_at" | "version" | "deleted_at"
> & { version?: number };

function newCenterProfile(
  overrides: Partial<CenterProfileInput> = {},
): CenterProfileInput {
  return {
    id: PROFILE_ID,
    tenant_id: TENANT,
    name: "Centre Médical Lomé",
    region_code: "TG-M",
    address: "Boulevard du 13 Janvier",
    location: null,
    phone: "+22822000000",
    email: "contact@centre.tg",
    opening_hours: [],
    service_types: [],
    is_public: true,
    ...overrides,
  };
}

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

function newVisit(
  patientId: string,
  overrides: Partial<NewVisitInput> = {},
): NewVisitInput {
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

afterEach(async () => {
  // Purge complète entre tests pour isoler l'état (base + outbox).
  if (isDbOpen()) await destroyDb();
});

describe("G-6 — l'outbox reste bornée sous éditions répétées du profil", () => {
  it("n'inscrit AUCUNE entrée d'outbox après un insert + 15 mises à jour", async () => {
    await initDb(GOOD_KEY);

    const EDITS = 15;

    // Insert initial du profil.
    await upsertCenterProfile(newCenterProfile({ name: "Nom initial" }));

    // Puis N mises à jour du MÊME id, avec des champs changeants.
    for (let i = 1; i <= EDITS; i++) {
      await upsertCenterProfile(
        newCenterProfile({
          name: `Centre v${i}`,
          phone: `+2282200${String(1000 + i)}`,
        }),
      );
    }

    // DoD « outbox bornée / ne croît pas » : la forme la plus forte est
    // exactement 0 entrée, quel que soit le nombre d'éditions.
    const outbox = await listOutbox();
    expect(outbox).toHaveLength(0);
  });

  it("persiste réellement le profil en local (version bornée et incrémentée)", async () => {
    await initDb(GOOD_KEY);

    const EDITS = 12;

    await upsertCenterProfile(newCenterProfile({ name: "Nom initial" }));

    let lastName = "Nom initial";
    let lastPhone = "+22822000000";
    for (let i = 1; i <= EDITS; i++) {
      lastName = `Centre v${i}`;
      lastPhone = `+2282200${String(1000 + i)}`;
      await upsertCenterProfile(
        newCenterProfile({ name: lastName, phone: lastPhone }),
      );
    }

    // Le test ne « réussit » pas à tort en n'écrivant rien : le profil existe
    // bien et reflète la DERNIÈRE édition.
    const db = await getDb();
    const saved = await db.center_profile.findOne(PROFILE_ID).exec();
    expect(saved).not.toBeNull();
    expect(saved!.name).toBe(lastName);
    expect(saved!.phone).toBe(lastPhone);
    expect(saved!.deleted_at).toBeNull();

    // LWW local : la version est incrémentée à chaque upsert (1 insert + EDITS
    // updates ⇒ version = EDITS + 1). Elle reste un compteur borné, pas une
    // file qui enfle, et l'outbox n'en porte toujours aucune trace.
    expect(saved!.version).toBe(EDITS + 1);

    const outbox = await listOutbox();
    expect(outbox).toHaveLength(0);
  });
});

describe("G-6 — aucun contrat de sync conflictuel : l'outbox ne porte que patient/visit", () => {
  it("après patient + visite + éditions répétées du profil, l'outbox n'a que patient/visit", async () => {
    await initDb(GOOD_KEY);

    // Mutations synchronisables (contrat figé) entremêlées avec des éditions
    // de profil (hors contrat de sync).
    const patient = await createPatient(newPatient());
    await upsertCenterProfile(newCenterProfile({ name: "Avant visite" }));
    await createVisit(newVisit(patient.id));

    for (let i = 1; i <= 10; i++) {
      await upsertCenterProfile(newCenterProfile({ name: `Edition ${i}` }));
    }

    const outbox = await listOutbox();

    // Exactement les deux mutations attendues : insert patient + insert visite.
    expect(outbox).toHaveLength(2);

    // Verrou du contrat figé : aucune entrée `center_profile`, uniquement
    // `patient`/`visit`.
    const collections = outbox.map((e) => e.collection_name);
    expect(collections).toEqual(["patient", "visit"]);
    for (const entry of outbox) {
      expect(["patient", "visit"]).toContain(entry.collection_name);
      expect(entry.collection_name).not.toBe("center_profile");
    }
  });
});
