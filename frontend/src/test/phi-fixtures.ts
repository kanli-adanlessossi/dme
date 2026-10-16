// Fixtures et utilitaires partagés par les tests de confidentialité locale (P13).
//
// Volontairement dans un module non-test : les suites P13 sont réparties en
// plusieurs fichiers pour profiter de l'isolation par fichier de Vitest (chaque
// fichier obtient un registre RxDB neuf, ce qui évite les collisions de nom de
// base « dme_local » entre scénarios).

export const GOOD_KEY = "bonne-cle-aes-de-test-32chars!!!!";
export const WRONG_KEY = "mauvaise-cle-totalement-differente";
const TENANT = "11111111-1111-7111-8111-111111111111";

// PHI « marqueurs » faciles à repérer en clair dans le stockage brut.
export const SECRET_MRN = "MRN-SECRET-7Z9";
export const SECRET_GIVEN = "PrenomSecretXy";
export const SECRET_FAMILY = "NomSecretQw";
export const SECRET_PHONE = "+22899887766";
export const SECRET_NID = "NID-123";

export function phiPatientInput() {
  return {
    tenant_id: TENANT,
    mrn: SECRET_MRN,
    given_name: SECRET_GIVEN,
    family_name: SECRET_FAMILY,
    dob: "1980-01-01",
    sex: "F",
    phone: SECRET_PHONE,
    national_id: SECRET_NID,
    consent_to_store: true,
    created_by: null,
  };
}

/**
 * Vide intégralement le contenu de toutes les bases IndexedDB en une chaîne, en
 * contournant RxDB (lecture brute du stockage sous-jacent). Permet de chercher
 * d'éventuelles PHI en clair au repos.
 */
export async function dumpAllIndexedDb(): Promise<string> {
  const dbs = (await indexedDB.databases?.()) ?? [];
  const chunks: string[] = [];
  for (const info of dbs) {
    if (!info.name) continue;
    const raw = await new Promise<string>((resolve) => {
      const open = indexedDB.open(info.name as string);
      open.onsuccess = () => {
        const idb = open.result;
        const storeNames = Array.from(idb.objectStoreNames);
        if (storeNames.length === 0) {
          idb.close();
          return resolve("");
        }
        const tx = idb.transaction(storeNames, "readonly");
        const parts: string[] = [];
        let pending = storeNames.length;
        for (const name of storeNames) {
          const req = tx.objectStore(name).getAll();
          const done = () => {
            if (--pending === 0) {
              idb.close();
              resolve(parts.join("|"));
            }
          };
          req.onsuccess = () => {
            parts.push(JSON.stringify(req.result));
            done();
          };
          req.onerror = done;
        }
      };
      open.onerror = () => resolve("");
    });
    chunks.push(raw);
  }
  return chunks.join("||");
}
