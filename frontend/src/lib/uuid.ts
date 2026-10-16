// Génération d'UUIDv7 côté client (NFR-6).
//
// Les PHI (patient/visit) et les mutations de l'outbox utilisent des UUIDv7
// générés **sur le poste** comme clé primaire / clé d'idempotence. Deux postes
// travaillant hors ligne ne produisent donc pas de collision, et l'id est stable
// dès l'écriture locale (aucune réécriture de clé à la synchronisation).
//
// UUIDv7 (RFC 9562) = 48 bits d'horodatage Unix en millisecondes (big-endian)
// suivis de 74 bits aléatoires, avec les champs version (0b0111) et variant
// (0b10) positionnés. Le préfixe temporel rend les identifiants triables par
// ordre de création, ce qui est utile pour les index et les curseurs de sync.

/**
 * Octets aléatoires cryptographiquement sûrs.
 * Utilise `crypto.getRandomValues` (navigateur / Web Worker / Node moderne).
 */
function randomBytes(length: number): Uint8Array {
  const out = new Uint8Array(length);
  crypto.getRandomValues(out);
  return out;
}

/**
 * Génère un UUIDv7 sous forme canonique
 * (`xxxxxxxx-xxxx-7xxx-yxxx-xxxxxxxxxxxx`).
 *
 * @param timestampMs horodatage Unix en millisecondes (par défaut `Date.now()`).
 *                    Paramétrable pour les tests déterministes.
 */
export function uuidv7(timestampMs: number = Date.now()): string {
  const bytes = randomBytes(16);

  // 48 bits d'horodatage (ms) dans les 6 premiers octets, big-endian.
  // `Number` tient 48 bits sans perte de précision (< 2^53).
  const ts = Math.floor(timestampMs);
  bytes[0] = (ts / 2 ** 40) & 0xff;
  bytes[1] = (ts / 2 ** 32) & 0xff;
  bytes[2] = (ts / 2 ** 24) & 0xff;
  bytes[3] = (ts / 2 ** 16) & 0xff;
  bytes[4] = (ts / 2 ** 8) & 0xff;
  bytes[5] = ts & 0xff;

  // Version 7 : les 4 bits de poids fort de l'octet 6 valent 0b0111.
  bytes[6] = (bytes[6] & 0x0f) | 0x70;
  // Variant RFC 4122 : les 2 bits de poids fort de l'octet 8 valent 0b10.
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex: string[] = [];
  for (let i = 0; i < 16; i++) {
    hex.push(bytes[i].toString(16).padStart(2, "0"));
  }

  return (
    hex.slice(0, 4).join("") +
    "-" +
    hex.slice(4, 6).join("") +
    "-" +
    hex.slice(6, 8).join("") +
    "-" +
    hex.slice(8, 10).join("") +
    "-" +
    hex.slice(10, 16).join("")
  );
}

/** Expression régulière de validation d'un UUIDv7 canonique (version + variant). */
export const UUIDV7_REGEX =
  /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Indique si `value` est un UUIDv7 valide. */
export function isUuidv7(value: string): boolean {
  return UUIDV7_REGEX.test(value);
}
