// Setup global des tests (tâche 14.6).
//
// Objectifs :
//  1. Fournir IndexedDB dans l'environnement de test (RxDB/Dexie en a besoin).
//     `fake-indexeddb/auto` installe une implémentation en mémoire sur les
//     globals (`indexedDB`, `IDBKeyRange`, ...).
//  2. Garantir un `crypto` complet avec `crypto.subtle` (PBKDF2 pour la
//     dérivation de clé dans session.ts) et `crypto.getRandomValues` (uuid.ts).
//     jsdom n'expose pas toujours `subtle` ; on complète avec le webcrypto natif
//     de Node.
//  3. Rendre les tests hermétiques : aucun test ne doit toucher le réseau. On
//     installe un `fetch` qui échoue bruyamment par défaut ; chaque test qui a
//     besoin du réseau le remplace explicitement (vi.stubGlobal / mock).

import "fake-indexeddb/auto";
import { webcrypto } from "node:crypto";
import { afterEach, beforeEach, vi } from "vitest";

// --- crypto : s'assurer que subtle + getRandomValues existent --------------
// jsdom fournit getRandomValues mais pas toujours subtle. On préfère le
// webcrypto natif de Node, complet et conforme.
if (
  typeof globalThis.crypto === "undefined" ||
  typeof globalThis.crypto.subtle === "undefined"
) {
  Object.defineProperty(globalThis, "crypto", {
    value: webcrypto,
    configurable: true,
    writable: true,
  });
}

// `structuredClone` est requis par fake-indexeddb pour cloner les valeurs.
// Node 17+ le fournit nativement ; garde-fou défensif.
if (typeof globalThis.structuredClone === "undefined") {
  globalThis.structuredClone = (val: unknown) =>
    JSON.parse(JSON.stringify(val));
}

// --- Réseau : interdit par défaut (tests hermétiques) ----------------------
// Un test qui n'a pas explicitement mocké le réseau et appelle fetch doit
// échouer de façon visible plutôt que tenter un appel réel.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => {
      throw new Error(
        "fetch non mocké : ce test ne doit pas atteindre le réseau. " +
          "Mockez le module api.ts ou vi.stubGlobal('fetch', ...).",
      );
    }),
  );
  // Connectivité en ligne par défaut (surchargée par test si besoin).
  Object.defineProperty(window.navigator, "onLine", {
    value: true,
    configurable: true,
    writable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
