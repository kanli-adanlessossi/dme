// Verrouillage anti-régression du KDF de la clé de chiffrement locale (#10).
//
// Exigence 1 (critères 3, 5, 6) ; NFR-8, NFR-2, P13. Ce test fige les paramètres
// du KDF derrière les constantes exportées par `session.ts` (point de vérité
// unique, tâche 1) : il lit la constante réelle et NE recopie AUCUN littéral de
// seuil côté production. Seuls les seuils de sécurité (bornes basses OWASP) sont
// nommés ici, car ce sont EUX que le test verrouille.
//
// Il échoue si un paramètre régresse sous un seuil de sécurité :
//   - itérations PBKDF2  < 210 000
//   - longueur de sel    < 16 octets
//   - longueur de clé    < 32 octets (256 bits)
//   - algorithme         ≠ PBKDF2-SHA256
//
// Il vérifie aussi deux propriétés de confidentialité locale :
//   - P-KDF2 (exigence 1.5) : deux utilisateurs distincts ⇒ sels distincts ;
//   - P-KDF3 (exigence 1.6) : après un `login` simulé, aucune clé `localStorage`
//     ne contient la clé dérivée ni le mot de passe.
//
// Mocks : réseau stubé (`fetch`) + `fake-indexeddb` (setup global) ; `./sync` est
// mocké pour que le `login` n'ait pas à démarrer un vrai moteur de sync réseau
// (on s'inspire du mock de `./api` dans `sync.idempotent.test.ts` et de l'usage
// de `fake-indexeddb` dans les suites `db.*.test.ts`).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// --- Mock du moteur de sync -------------------------------------------------
// `login` appelle `wireSync()` qui fait `configureSync({ getToken }).start()`
// et s'abonne à `engine.state`. On remplace ce module par un faux moteur inerte
// afin que le `login` reste hermétique (aucun réseau de sync).
vi.mock("./sync", () => {
  const fakeEngine = {
    state: { subscribe: () => ({ unsubscribe: () => {} }) },
    start: () => {},
    stop: () => {},
  };
  return {
    configureSync: vi.fn(() => fakeEngine),
    getSyncEngine: vi.fn(() => fakeEngine),
  };
});

import {
  DERIVED_KEY_BITS,
  KDF_ALGORITHM,
  KDF_HASH,
  PBKDF2_ITERATIONS,
  SALT_LENGTH_BYTES,
  login,
} from "./session";
import { destroyDb, isDbOpen } from "./db";

// Seuils de sécurité (bornes basses OWASP Password Storage Cheat Sheet, rév.
// ≥ 2023). Ce sont les SEUILS que le test verrouille — pas une recopie des
// valeurs de production, qui sont lues via les constantes exportées ci-dessus.
const MIN_ITERATIONS = 210_000;
const MIN_SALT_BYTES = 16;
const MIN_KEY_BYTES = 32; // 256 bits.

describe("#10 — verrouillage des paramètres du KDF (constantes exportées)", () => {
  it("itérations PBKDF2 ≥ 210 000 (sinon régression)", () => {
    expect(PBKDF2_ITERATIONS).toBeGreaterThanOrEqual(MIN_ITERATIONS);
  });

  it("longueur de sel ≥ 16 octets (sinon régression)", () => {
    expect(SALT_LENGTH_BYTES).toBeGreaterThanOrEqual(MIN_SALT_BYTES);
  });

  it("longueur de clé dérivée ≥ 32 octets / 256 bits (sinon régression)", () => {
    // DERIVED_KEY_BITS est exprimé en bits ; le seuil est en octets (homogène
    // avec la spec : le code manipule des octets).
    expect(DERIVED_KEY_BITS / 8).toBeGreaterThanOrEqual(MIN_KEY_BYTES);
    // Garde-fou : une longueur de clé doit être un multiple entier d'octets.
    expect(DERIVED_KEY_BITS % 8).toBe(0);
  });

  it("algorithme = PBKDF2 avec hash SHA-256 (sinon régression)", () => {
    expect(KDF_ALGORITHM).toBe("PBKDF2");
    expect(KDF_HASH).toBe("SHA-256");
  });
});

// -------------------------------------------------------------------------
// Dérivation réelle via session.ts (login simulé) : sels par utilisateur +
// non-persistance des secrets.
// -------------------------------------------------------------------------

const SALT_PREFIX = "dme.kdf.salt.";
const SESSION_KEY = "dme.session";

/** Réponse minimale de `POST /api/auth/login` (miroir de TokenResponse). */
function tokenResponse(overrides: Record<string, unknown> = {}) {
  return {
    access: "access-jeton-test",
    refresh: "refresh-jeton-test",
    token_type: "bearer",
    expires_in: 3600,
    tenant_id: "11111111-1111-7111-8111-111111111111",
    role: "clinician",
    ...overrides,
  };
}

/** Stub `fetch` : répond 200 + JSON à `/api/auth/login`, échoue ailleurs. */
function stubLoginFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (String(url).endsWith("/api/auth/login")) {
        return {
          ok: true,
          status: 200,
          json: async () => tokenResponse(),
        } as Response;
      }
      throw new Error(`fetch inattendu dans ce test : ${String(url)}`);
    }),
  );
}

describe("#10 — sel par utilisateur et secrets jamais persistés (login simulé)", () => {
  beforeEach(() => {
    localStorage.clear();
    stubLoginFetch();
  });

  afterEach(async () => {
    // Purger la base chiffrée AVANT de couper la session : `destroyDb()` exige
    // une instance ouverte (sinon no-op), et `closeDb()` (via logout) ne purge
    // pas le stockage au repos. On évite ainsi un conflit de mot de passe RxDB
    // avec un éventuel login suivant sur le même nom de base (`dme_local`).
    if (isDbOpen()) await destroyDb();
    localStorage.clear();
  });

  it("deux emails distincts produisent des sels distincts (exigence 1.5)", async () => {
    const emailA = "alice@example.test";
    const emailB = "bob@example.test";

    // Un login dérive (et persiste) le sel de l'utilisateur sous dme.kdf.salt.*.
    await login(emailA, "mot-de-passe-A-123456");
    const saltA = localStorage.getItem(SALT_PREFIX + emailA.toLowerCase());

    // Purger la base chiffrée (store `dme_local`) AVANT le second login : sinon
    // la 2ᵉ ouverture avec une clé différente est rejetée par RxDB (DB1).
    // `destroyDb()` doit être appelé tant que l'instance est ouverte.
    if (isDbOpen()) await destroyDb();

    await login(emailB, "mot-de-passe-B-654321");
    const saltB = localStorage.getItem(SALT_PREFIX + emailB.toLowerCase());

    expect(saltA).toBeTruthy();
    expect(saltB).toBeTruthy();
    // Sel par utilisateur, non partagé : les deux valeurs diffèrent.
    expect(saltA).not.toBe(saltB);
  });

  it("après un login, aucune clé localStorage ne contient la clé dérivée ni le mot de passe (exigence 1.6, P-KDF3)", async () => {
    const email = "carol@example.test";
    const password = "MotDePasseUniqueXYZ-7Z9Q";

    await login(email, password);

    // Parcourir TOUT le contenu de localStorage (clés + valeurs).
    const entries: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i) as string;
      entries.push(key);
      entries.push(localStorage.getItem(key) ?? "");
    }
    const dump = entries.join("\u0000");

    // Le mot de passe en clair ne doit JAMAIS apparaître.
    expect(dump).not.toContain(password);

    // La clé dérivée ne doit pas non plus être persistée. On la re-dérive à la
    // volée (même sel que celui que le login vient de persister) et on vérifie
    // son absence des valeurs stockées.
    const saltHex = localStorage.getItem(SALT_PREFIX + email.toLowerCase());
    expect(saltHex).toBeTruthy();
    const salt = hexToBytes(saltHex as string);
    const derivedKeyHex = await deriveKeyHex(password, salt);
    expect(dump).not.toContain(derivedKeyHex);

    // Garde-fou : le stockage n'est pas vide (le sel et la session y sont),
    // l'absence des secrets n'est donc pas un faux négatif dû à un stockage vide.
    expect(localStorage.getItem(SESSION_KEY)).toBeTruthy();
    expect(saltHex).toBeTruthy();
  });
});

// --- Helpers locaux ---------------------------------------------------------
// Re-dérivation indépendante via Web Crypto, en lisant les constantes exportées
// (aucun littéral de paramètre dupliqué). Sert uniquement à prouver que la clé
// réellement dérivée n'est pas présente dans localStorage.

function hexToBytes(hex: string): Uint8Array {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

function bytesToHex(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

async function deriveKeyHex(password: string, salt: Uint8Array): Promise<string> {
  const baseKey = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    KDF_ALGORITHM,
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    {
      name: KDF_ALGORITHM,
      salt: salt as unknown as BufferSource,
      iterations: PBKDF2_ITERATIONS,
      hash: KDF_HASH,
    },
    baseKey,
    DERIVED_KEY_BITS,
  );
  return bytesToHex(new Uint8Array(bits));
}
