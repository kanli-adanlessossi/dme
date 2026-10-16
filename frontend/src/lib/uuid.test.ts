// Tests de la génération d'UUIDv7 côté client (NFR-6).
//
// L'UUIDv7 est la clé primaire stable des entités PHI et la clé d'idempotence
// des mutations d'outbox. Deux propriétés comptent pour le local-first :
//   - **Validité de format** (version 7 + variant RFC 4122) ;
//   - **Unicité** entre générations (pas de collision inter-poste / inter-appel) ;
//   - **Ordre temporel** : le préfixe horodaté rend les ids triables par création,
//     ce qui alimente l'ordre de l'outbox et les curseurs de sync.
//
// On combine des tests par l'exemple et des tests de propriété (fast-check).

import { describe, expect, it } from "vitest";
import fc from "fast-check";
import { isUuidv7, uuidv7, UUIDV7_REGEX } from "./uuid";

describe("uuidv7 — format", () => {
  it("génère un UUID canonique de version 7 et variant RFC 4122", () => {
    const id = uuidv7();
    expect(isUuidv7(id)).toBe(true);
    expect(id).toMatch(UUIDV7_REGEX);
    // 36 caractères : 32 hex + 4 tirets.
    expect(id).toHaveLength(36);
    // Le chiffre de version (début du 3e groupe) vaut 7.
    expect(id[14]).toBe("7");
    // Le variant (début du 4e groupe) est dans {8,9,a,b}.
    expect("89ab").toContain(id[19]);
  });

  it("rejette les chaînes non conformes", () => {
    expect(isUuidv7("")).toBe(false);
    expect(isUuidv7("not-a-uuid")).toBe(false);
    // UUIDv4 (version 4) ne doit pas passer le test v7.
    expect(isUuidv7("3f1b1c2e-0a1b-4c2d-8e3f-0a1b2c3d4e5f")).toBe(false);
  });
});

describe("uuidv7 — horodatage encodé", () => {
  it("encode le timestamp fourni dans les 48 bits de poids fort", () => {
    const ts = 0x0123456789ab; // valeur 48 bits connue.
    const id = uuidv7(ts);
    const hexPrefix = id.replace(/-/g, "").slice(0, 12);
    expect(hexPrefix).toBe("0123456789ab");
  });

  it("ordonne lexicographiquement les ids par timestamp croissant", () => {
    const earlier = uuidv7(1_700_000_000_000);
    const later = uuidv7(1_700_000_001_000);
    expect(earlier < later).toBe(true);
  });
});

describe("uuidv7 — propriétés (fast-check)", () => {
  // Propriété : tout id généré est un UUIDv7 valide, quel que soit le timestamp.
  // **Validates: Requirements NFR-6**
  it("produit toujours un UUIDv7 valide pour tout timestamp 48 bits", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 2 ** 48 - 1 }),
        (ts) => {
          expect(isUuidv7(uuidv7(ts))).toBe(true);
        },
      ),
      { numRuns: 500 },
    );
  });

  // Propriété : unicité — un lot d'ids générés au même instant ne collisionne pas
  // (les 74 bits aléatoires rendent une collision négligeable). Vérifie P10 côté
  // génération de clé (pas de doublon d'id d'entité/mutation).
  // **Validates: Requirements NFR-6**
  it("ne produit pas de collision sur un lot d'ids (même timestamp)", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 2 ** 48 - 1 }),
        fc.integer({ min: 2, max: 300 }),
        (ts, count) => {
          const ids = new Set<string>();
          for (let i = 0; i < count; i++) ids.add(uuidv7(ts));
          expect(ids.size).toBe(count);
        },
      ),
      { numRuns: 200 },
    );
  });

  // Propriété : monotonie temporelle — si ta < tb alors uuidv7(ta) < uuidv7(tb)
  // lexicographiquement (le préfixe horodaté domine l'ordre).
  // **Validates: Requirements NFR-6**
  it("respecte l'ordre temporel entre deux timestamps distincts", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 2 ** 48 - 2 }),
        fc.integer({ min: 1, max: 2 ** 40 }),
        (base, delta) => {
          const ta = base;
          const tb = Math.min(base + delta, 2 ** 48 - 1);
          fc.pre(tb > ta);
          expect(uuidv7(ta) < uuidv7(tb)).toBe(true);
        },
      ),
      { numRuns: 300 },
    );
  });
});
