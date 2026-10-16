// P13 / NFR-8 — Confidentialité locale : une mauvaise clé n'ouvre pas le stockage.
//
// Écrit des PHI avec la bonne clé, ferme (données chiffrées au repos), puis tente
// de rouvrir la base avec une MAUVAISE clé. RxDB rejette l'ouverture d'un
// stockage chiffré avec un mot de passe différent : la mauvaise clé n'expose
// jamais les PHI en clair. On vérifie à la fois l'absence de fuite et le rejet.
//
// Isolation : on « ferme » (closeDb → destroy l'instance mais garde les données)
// puis on utilise `vi.resetModules()` pour obtenir un db.ts neuf réouvrant le
// MÊME stockage. Fichier séparé pour un registre RxDB vierge.

import { describe, expect, it, vi } from "vitest";
import {
  GOOD_KEY,
  phiPatientInput,
  SECRET_FAMILY,
  SECRET_GIVEN,
  SECRET_MRN,
  WRONG_KEY,
} from "../test/phi-fixtures";

describe("P13 — mauvaise clé", () => {
  it("refuse d'ouvrir le stockage chiffré avec une autre clé (pas de fuite)", async () => {
    // 1) Écriture avec la bonne clé, puis fermeture (verrouillage).
    const dbGood = await import("./db");
    await dbGood.initDb(GOOD_KEY);
    await dbGood.createPatient(phiPatientInput());
    await dbGood.closeDb();

    // 2) Module neuf (db.ts rechargé) ; mêmes données IndexedDB au repos.
    vi.resetModules();
    const dbWrong = await import("./db");

    let threw = false;
    let leakedInClear = false;
    try {
      await dbWrong.initDb(WRONG_KEY);
      const instance = await dbWrong.getDb();
      const docs = await instance.patient.find().exec();
      for (const d of docs) {
        const json = JSON.stringify(d.toJSON());
        if (
          json.includes(SECRET_MRN) ||
          json.includes(SECRET_GIVEN) ||
          json.includes(SECRET_FAMILY)
        ) {
          leakedInClear = true;
        }
      }
      await dbWrong.closeDb();
    } catch {
      // Mot de passe de chiffrement incompatible : ouverture refusée (P13).
      threw = true;
    }

    expect(leakedInClear).toBe(false);
    expect(threw).toBe(true);
  });
});
