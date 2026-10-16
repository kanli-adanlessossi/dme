// P13 / NFR-8 — Confidentialité locale : verrouillage effectif à la fermeture.
//
// Après `closeDb()` (déclenché par `logout`/expiration côté session.ts), la clé
// de chiffrement en mémoire est libérée : `isDbOpen()` repasse à false et toute
// opération nécessitant la base échoue tant que la clé n'a pas été re-fournie
// (re-authentification). Les PHI au repos restent chiffrées et inaccessibles.
//
// Fichier isolé pour un registre RxDB vierge.

import { describe, expect, it } from "vitest";
import { closeDb, createPatient, getDb, initDb, isDbOpen } from "./db";
import { GOOD_KEY, phiPatientInput } from "../test/phi-fixtures";

describe("P13 — verrouillage à la fermeture", () => {
  it("closeDb() verrouille : isDbOpen() false et getDb() échoue", async () => {
    await initDb(GOOD_KEY);
    expect(isDbOpen()).toBe(true);
    await createPatient(phiPatientInput());

    await closeDb();
    // Base fermée : clé en mémoire libérée, PHI inaccessibles sans ré-init.
    expect(isDbOpen()).toBe(false);
    // getDb lève tant que la base n'a pas été ré-initialisée avec la clé.
    await expect(getDb()).rejects.toThrow();
  });
});
