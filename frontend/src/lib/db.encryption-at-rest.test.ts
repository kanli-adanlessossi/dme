// P13 / NFR-8 — Confidentialité locale : PHI chiffrées au repos.
//
// Vérifie qu'après écriture de PHI puis fermeture de la base, les octets stockés
// dans IndexedDB ne contiennent AUCUNE PHI en clair (mrn/given_name/family_name/
// phone/national_id). On inspecte le stockage sous-jacent directement, en
// contournant RxDB (lecture brute). C'est l'observable qui compte pour P13 : un
// attaquant lisant le disque sans la clé ne retrouve pas les PHI.
//
// Fichier isolé (voir src/test/phi-fixtures.ts) : Vitest donne un registre RxDB
// neuf par fichier, ce qui évite les collisions de nom de base entre scénarios.

import { describe, expect, it } from "vitest";
import {
  closeDb,
  createPatient,
  destroyDb,
  getDb,
  initDb,
  isDbOpen,
} from "./db";
import {
  dumpAllIndexedDb,
  GOOD_KEY,
  phiPatientInput,
  SECRET_FAMILY,
  SECRET_GIVEN,
  SECRET_MRN,
  SECRET_NID,
  SECRET_PHONE,
} from "../test/phi-fixtures";

describe("P13 — PHI chiffrées au repos", () => {
  it("ne stocke pas les PHI en clair dans IndexedDB", async () => {
    await initDb(GOOD_KEY);
    await createPatient(phiPatientInput());

    // Avec la bonne clé, les PHI sont lisibles EN MÉMOIRE (déchiffrement).
    const instance = await getDb();
    const docs = await instance.patient.find().exec();
    expect(docs).toHaveLength(1);
    expect(docs[0].mrn).toBe(SECRET_MRN);
    expect(docs[0].given_name).toBe(SECRET_GIVEN);

    // Fermer (verrouille la clé) puis inspecter le stockage brut.
    await closeDb();
    const dump = await dumpAllIndexedDb();

    expect(dump).not.toContain(SECRET_MRN);
    expect(dump).not.toContain(SECRET_GIVEN);
    expect(dump).not.toContain(SECRET_FAMILY);
    expect(dump).not.toContain(SECRET_PHONE);
    expect(dump).not.toContain(SECRET_NID);

    // Garde-fou : le stockage n'est pas vide — l'absence de PHI vient du
    // chiffrement, pas d'un stockage vide.
    expect(dump.length).toBeGreaterThan(0);

    if (isDbOpen()) await destroyDb();
  });
});
