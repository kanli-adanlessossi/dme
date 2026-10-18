// Base locale RxDB (IndexedDB chiffré) — NFR-5, NFR-8, NFR-6.
//
// Rôle (tâche 14.3) : fournir le stockage local-first du poste de travail. Les
// pages tenant lisent/écrivent **uniquement** cette base ; `sync.ts` (14.4) est
// le seul composant qui parle à l'API tenant. Les PHI sont chiffrées au repos.
//
// Choix d'implémentation
// ----------------------
// - Stockage : RxDB sur IndexedDB via le plugin **gratuit** `storage-dexie`
//   (Dexie.js). Le stockage IndexedDB « premium » n'est pas requis pour la v1.
// - Chiffrement (NFR-8) : plugin **gratuit** `encryption-crypto-js`
//   (`wrappedKeyEncryptionCryptoJsStorage`, AES via crypto-js). C'est un wrapper
//   autour du RxStorage : les champs listés dans `encrypted` de chaque schéma
//   sont chiffrés avant écriture sur disque et déchiffrés en mémoire à la
//   lecture. Sans le mot de passe fourni à `createRxDatabase`, les PHI stockées
//   dans IndexedDB sont illisibles (P13).
//   > Le plugin premium `encryption-web-crypto` (Web Crypto natif) est plus
//   > rapide ; on pourra y basculer sans changer ce module (même API wrapper).
// - Clé de chiffrement : fournie à `initDb(encryptionKey)` (dérivée des
//   identifiants de session, voir 14.5). Elle n'est jamais persistée en clair ;
//   `closeDb()` la verrouille en fermant la base (14.5 câble le cycle de vie).
//
// Les champs de sync (NFR-6) — `id` UUIDv7, `version`, `updated_at`,
// `deleted_at` (suppression logique) — sont portés par chaque entité
// synchronisable, et chaque écriture inscrit une entrée dans l'outbox.

import {
  createRxDatabase,
  addRxPlugin,
  type RxDatabase,
  type RxCollection,
  type RxJsonSchema,
  type RxDocument,
} from "rxdb/plugins/core";
import { getRxStorageDexie } from "rxdb/plugins/storage-dexie";
import { wrappedKeyEncryptionCryptoJsStorage } from "rxdb/plugins/encryption-crypto-js";
import { uuidv7 } from "./uuid";

// -------------------------------------------------------------------------
// Types de documents (alignés sur le design « Data Models »)
// -------------------------------------------------------------------------

/** Base commune des entités synchronisables (NFR-6). */
export interface SyncFields {
  /** UUIDv7 généré côté client (clé primaire stable). */
  id: string;
  tenant_id: string;
  /** Base de résolution LWW (ISO 8601). */
  updated_at: string;
  /** Compteur de version pour la détection de conflits. */
  version: number;
  /** Suppression logique (NFR-4) ; `null` tant que vivant. */
  deleted_at: string | null;
}

/** Patient — PHI, cadré au tenant, synchronisable. */
export interface PatientDoc extends SyncFields {
  mrn: string;
  given_name: string;
  family_name: string;
  dob: string | null;
  sex: string | null;
  phone: string | null;
  national_id: string | null;
  /** Capture du consentement au stockage (NFR-4, REQ-5.6) — requis. */
  consent_to_store: boolean;
  created_by: string | null;
  created_at: string;
}

/** Visite — PHI, cadrée au tenant, synchronisable. */
export interface VisitDoc extends SyncFields {
  patient_id: string;
  visited_at: string | null;
  reason: string | null;
  notes: string | null;
  created_by: string | null;
  created_at: string;
}

/** Profil du centre médical (projection publique éditable par le tenant). */
export interface CenterProfileDoc extends SyncFields {
  name: string;
  region_code: string | null;
  address: string | null;
  /** Coordonnées WGS84 ; `null` tant que non positionnées. */
  location: { lat: number; lng: number } | null;
  phone: string | null;
  email: string | null;
  opening_hours: { day: string; open: string; close: string }[];
  service_types: string[];
  is_public: boolean;
}

/** Type d'opération portée par une mutation de l'outbox. */
export type OutboxOperation = "insert" | "update" | "delete";

/**
 * Mutation en attente de synchronisation (NFR-6).
 *
 * `id` est un **UUIDv7 de mutation** servant de clé d'idempotence au push
 * (14.4 / `POST /api/sync/push`). `snapshot` est l'état du document après
 * l'opération, suffisant pour rejouer la mutation sans relire la base.
 */
export interface OutboxDoc {
  /** UUIDv7 de la mutation = clé d'idempotence du push. */
  id: string;
  operation: OutboxOperation;
  /**
   * Collection cible (`"patient"`, `"visit"`, `"center_profile"`).
   *
   * NB : le champ est nommé `collection_name` et non `collection` car
   * `collection` est une propriété réservée de `RxDocument` (accesseur interne) ;
   * RxDB interdit un champ de schéma de ce nom (dev-mode SC17), et s'y fier
   * masquerait l'accesseur en production.
   */
  collection_name: string;
  /** Id de l'entité visée (UUIDv7 de l'entité). */
  entity_id: string;
  /** Snapshot du document après l'opération (rejouable). */
  snapshot: unknown;
  /** Horodatage de création de la mutation (ISO 8601). */
  created_at: string;
}

// -------------------------------------------------------------------------
// Schémas RxDB
// -------------------------------------------------------------------------
//
// Les champs sensibles (PHI) sont déclarés dans `encrypted`. RxDB ne permet pas
// d'indexer ni de requêter un champ chiffré ; on garde donc `id`, `tenant_id`,
// `updated_at`, `deleted_at` en clair (nécessaires aux index et à la sync) et on
// chiffre les attributs identifiants/cliniques.

const patientSchema: RxJsonSchema<PatientDoc> = {
  title: "patient",
  version: 0,
  primaryKey: "id",
  type: "object",
  properties: {
    id: { type: "string", maxLength: 36 },
    tenant_id: { type: "string", maxLength: 36 },
    updated_at: { type: "string", maxLength: 30 },
    version: { type: "number", minimum: 1, multipleOf: 1, maximum: 1e12 },
    deleted_at: { type: ["string", "null"] },
    mrn: { type: "string" },
    given_name: { type: "string" },
    family_name: { type: "string" },
    dob: { type: ["string", "null"] },
    sex: { type: ["string", "null"] },
    phone: { type: ["string", "null"] },
    national_id: { type: ["string", "null"] },
    consent_to_store: { type: "boolean" },
    created_by: { type: ["string", "null"] },
    created_at: { type: "string", maxLength: 30 },
  },
  required: [
    "id",
    "tenant_id",
    "updated_at",
    "version",
    "mrn",
    "given_name",
    "family_name",
    "consent_to_store",
    "created_at",
  ],
  indexes: ["tenant_id", "updated_at"],
  encrypted: [
    "mrn",
    "given_name",
    "family_name",
    "dob",
    "sex",
    "phone",
    "national_id",
  ],
};

const visitSchema: RxJsonSchema<VisitDoc> = {
  title: "visit",
  version: 0,
  primaryKey: "id",
  type: "object",
  properties: {
    id: { type: "string", maxLength: 36 },
    tenant_id: { type: "string", maxLength: 36 },
    updated_at: { type: "string", maxLength: 30 },
    version: { type: "number", minimum: 1, multipleOf: 1, maximum: 1e12 },
    deleted_at: { type: ["string", "null"] },
    patient_id: { type: "string", maxLength: 36 },
    visited_at: { type: ["string", "null"] },
    reason: { type: ["string", "null"] },
    notes: { type: ["string", "null"] },
    created_by: { type: ["string", "null"] },
    created_at: { type: "string", maxLength: 30 },
  },
  required: [
    "id",
    "tenant_id",
    "updated_at",
    "version",
    "patient_id",
    "created_at",
  ],
  indexes: ["tenant_id", "patient_id", "updated_at"],
  // `reason` et `notes` sont cliniques (notes = entité sensible, cf. design).
  encrypted: ["reason", "notes"],
};

const centerProfileSchema: RxJsonSchema<CenterProfileDoc> = {
  title: "center_profile",
  version: 0,
  primaryKey: "id",
  type: "object",
  properties: {
    id: { type: "string", maxLength: 36 },
    tenant_id: { type: "string", maxLength: 36 },
    updated_at: { type: "string", maxLength: 30 },
    version: { type: "number", minimum: 1, multipleOf: 1, maximum: 1e12 },
    deleted_at: { type: ["string", "null"] },
    name: { type: "string" },
    region_code: { type: ["string", "null"] },
    address: { type: ["string", "null"] },
    location: {
      type: ["object", "null"],
      properties: {
        lat: { type: "number" },
        lng: { type: "number" },
      },
    },
    phone: { type: ["string", "null"] },
    email: { type: ["string", "null"] },
    opening_hours: { type: "array", items: { type: "object" } },
    service_types: { type: "array", items: { type: "string" } },
    is_public: { type: "boolean" },
  },
  required: ["id", "tenant_id", "updated_at", "version", "name"],
  indexes: ["tenant_id", "updated_at"],
  // Le profil du centre est une projection publique : rien à chiffrer.
};

const outboxSchema: RxJsonSchema<OutboxDoc> = {
  title: "outbox",
  version: 0,
  primaryKey: "id",
  type: "object",
  properties: {
    id: { type: "string", maxLength: 36 },
    operation: { type: "string", enum: ["insert", "update", "delete"] },
    collection_name: { type: "string", maxLength: 40 },
    entity_id: { type: "string", maxLength: 36 },
    snapshot: { type: "object" },
    created_at: { type: "string", maxLength: 30 },
  },
  required: ["id", "operation", "collection_name", "entity_id", "created_at"],
  indexes: ["created_at"],
  // Le snapshot peut contenir des PHI → chiffré au repos tant qu'il est en file.
  encrypted: ["snapshot"],
};

// -------------------------------------------------------------------------
// Déclaration de la base
// -------------------------------------------------------------------------

export type PatientCollection = RxCollection<PatientDoc>;
export type VisitCollection = RxCollection<VisitDoc>;
export type CenterProfileCollection = RxCollection<CenterProfileDoc>;
export type OutboxCollection = RxCollection<OutboxDoc>;

export interface DmeCollections {
  patient: PatientCollection;
  visit: VisitCollection;
  center_profile: CenterProfileCollection;
  outbox: OutboxCollection;
}

export type DmeDatabase = RxDatabase<DmeCollections>;

/** Nom logique de la base locale (unique par poste). */
const DB_NAME = "dme_local";

let dbPromise: Promise<DmeDatabase> | null = null;
let devPluginsLoaded = false;

/**
 * Charge le plugin de développement (validation de schéma + messages d'erreur
 * détaillés) uniquement hors production, pour ne pas alourdir le bundle.
 */
async function loadDevPlugins(): Promise<void> {
  if (devPluginsLoaded || import.meta.env.PROD) return;
  const { RxDBDevModePlugin } = await import("rxdb/plugins/dev-mode");
  addRxPlugin(RxDBDevModePlugin);
  devPluginsLoaded = true;
}

/**
 * Initialise (ou renvoie) la base locale chiffrée.
 *
 * @param encryptionKey mot de passe de chiffrement dérivé de la session
 *   (14.5). Sans lui, les PHI du poste restent illisibles (NFR-8, P13).
 *
 * Idempotent : les appels successifs renvoient la même instance tant que la
 * base n'est pas fermée via {@link closeDb}. C'est le **seam** d'intégration
 * de la session : 14.5 appellera `initDb(cle)` à la connexion et `closeDb()`
 * (verrouillage de la clé) à la déconnexion/expiration.
 */
export async function initDb(encryptionKey: string): Promise<DmeDatabase> {
  if (dbPromise) return dbPromise;
  if (!encryptionKey) {
    throw new Error(
      "initDb: clé de chiffrement requise (NFR-8) — base locale non ouverte sans clé.",
    );
  }

  const pending = (async () => {
    await loadDevPlugins();

    // Chiffrement au repos : on enveloppe le stockage Dexie avec le plugin
    // crypto-js. Le mot de passe est passé à `createRxDatabase`.
    const encryptedStorage = wrappedKeyEncryptionCryptoJsStorage({
      storage: getRxStorageDexie(),
    });

    const db = await createRxDatabase<DmeCollections>({
      name: DB_NAME,
      storage: encryptedStorage,
      password: encryptionKey,
      multiInstance: true,
      eventReduce: true,
      // La base est fermée (clé verrouillée) à la déconnexion, pas effacée :
      // les PHI chiffrées restent au repos jusqu'à purge explicite (14.5).
      ignoreDuplicate: false,
    });

    await db.addCollections({
      patient: { schema: patientSchema },
      visit: { schema: visitSchema },
      center_profile: { schema: centerProfileSchema },
      outbox: { schema: outboxSchema },
    });

    return db;
  })();

  // Si l'ouverture échoue (p. ex. mauvaise clé → mot de passe incompatible),
  // on ne laisse PAS un `dbPromise` empoisonné : il bloquerait toute tentative
  // ultérieure (y compris une réouverture avec la bonne clé) jusqu'au
  // rechargement. On remet l'état à zéro pour permettre une nouvelle tentative.
  pending.catch(() => {
    if (dbPromise === pending) dbPromise = null;
  });

  dbPromise = pending;
  return dbPromise;
}

/**
 * Renvoie l'instance de base déjà initialisée.
 * @throws si {@link initDb} n'a pas encore été appelé (clé non fournie).
 */
export async function getDb(): Promise<DmeDatabase> {
  if (!dbPromise) {
    throw new Error(
      "getDb: base non initialisée — appeler initDb(encryptionKey) à la connexion.",
    );
  }
  return dbPromise;
}

/** Indique si la base est actuellement ouverte (déverrouillée). */
export function isDbOpen(): boolean {
  return dbPromise !== null;
}

/**
 * Ferme la base et **verrouille** la clé de chiffrement (NFR-8, P13).
 *
 * À la réouverture, `initDb` exigera de nouveau la clé ; les PHI au repos
 * restent chiffrées entre-temps. 14.5 appelle cette fonction à la
 * déconnexion/expiration de session.
 */
export async function closeDb(): Promise<void> {
  if (!dbPromise) return;
  const db = await dbPromise;
  // RxDB 15 : `destroy()` ferme la base et libère la clé (sans effacer les
  // données au repos, qui restent chiffrées) ; `remove()` purge le stockage.
  await db.destroy();
  dbPromise = null;
}

/**
 * Détruit intégralement la base locale (purge des PHI). Utilisé lors d'une
 * révocation de compte (NFR-7) où la session et les PHI locales doivent être
 * purgées. Distinct de {@link closeDb} qui ne fait que verrouiller.
 */
export async function destroyDb(): Promise<void> {
  const db = dbPromise ? await dbPromise : null;
  if (db) {
    await db.remove();
    dbPromise = null;
  }
}

// -------------------------------------------------------------------------
// Couche repository minimale (schémas + outbox)
// -------------------------------------------------------------------------
//
// Toute écriture synchronisable passe par ces helpers afin de garantir, de
// façon cohérente, les invariants de sync (NFR-6) :
//   - `version` est posé à 1 à l'insertion et incrémenté à chaque mise à jour ;
//   - `updated_at` est rafraîchi à chaque écriture (base LWW) ;
//   - la suppression est **logique** (`deleted_at`) et non physique (NFR-4) ;
//   - une entrée d'outbox (avec UUIDv7 de mutation) est inscrite à chaque fois.

function nowIso(): string {
  return new Date().toISOString();
}

/** Inscrit une mutation dans l'outbox (clé d'idempotence = UUIDv7). */
async function enqueueOutbox(
  db: DmeDatabase,
  operation: OutboxOperation,
  collection: keyof DmeCollections,
  entity_id: string,
  snapshot: unknown,
): Promise<OutboxDoc> {
  const entry: OutboxDoc = {
    id: uuidv7(),
    operation,
    collection_name: String(collection),
    entity_id,
    snapshot,
    created_at: nowIso(),
  };
  await db.outbox.insert(entry);
  return entry;
}

/** Champs fournis par l'appelant pour créer un patient. */
export type NewPatientInput = Omit<
  PatientDoc,
  "id" | "updated_at" | "version" | "deleted_at" | "created_at"
> & { id?: string; created_at?: string };

/** Crée un patient (écriture locale + outbox). */
export async function createPatient(
  input: NewPatientInput,
): Promise<RxDocument<PatientDoc>> {
  const db = await getDb();
  const ts = nowIso();
  const doc: PatientDoc = {
    ...input,
    id: input.id ?? uuidv7(),
    created_at: input.created_at ?? ts,
    updated_at: ts,
    version: 1,
    deleted_at: null,
  };
  const inserted = await db.patient.insert(doc);
  await enqueueOutbox(db, "insert", "patient", doc.id, doc);
  return inserted;
}

/** Champs modifiables d'un patient. */
export type PatientPatch = Partial<
  Omit<PatientDoc, "id" | "tenant_id" | "version" | "created_at" | "deleted_at">
>;

/** Met à jour un patient (bump version + updated_at, puis outbox). */
export async function updatePatient(
  id: string,
  patch: PatientPatch,
): Promise<RxDocument<PatientDoc> | null> {
  const db = await getDb();
  const existing = await db.patient.findOne(id).exec();
  if (!existing || existing.deleted_at) return null;
  const updated = await existing.incrementalModify((data) => {
    Object.assign(data, patch);
    data.updated_at = nowIso();
    data.version = data.version + 1;
    return data;
  });
  await enqueueOutbox(db, "update", "patient", id, updated.toJSON());
  return updated;
}

/** Supprime logiquement un patient (NFR-4) + outbox. */
export async function softDeletePatient(
  id: string,
): Promise<RxDocument<PatientDoc> | null> {
  const db = await getDb();
  const existing = await db.patient.findOne(id).exec();
  if (!existing || existing.deleted_at) return null;
  const updated = await existing.incrementalModify((data) => {
    data.deleted_at = nowIso();
    data.updated_at = data.deleted_at;
    data.version = data.version + 1;
    return data;
  });
  await enqueueOutbox(db, "delete", "patient", id, updated.toJSON());
  return updated;
}

/** Champs fournis pour créer une visite. */
export type NewVisitInput = Omit<
  VisitDoc,
  "id" | "updated_at" | "version" | "deleted_at" | "created_at"
> & { id?: string; created_at?: string };

/** Crée une visite (écriture locale + outbox). */
export async function createVisit(
  input: NewVisitInput,
): Promise<RxDocument<VisitDoc>> {
  const db = await getDb();
  const ts = nowIso();
  const doc: VisitDoc = {
    ...input,
    id: input.id ?? uuidv7(),
    created_at: input.created_at ?? ts,
    updated_at: ts,
    version: 1,
    deleted_at: null,
  };
  const inserted = await db.visit.insert(doc);
  await enqueueOutbox(db, "insert", "visit", doc.id, doc);
  return inserted;
}

/** Champs modifiables d'une visite. */
export type VisitPatch = Partial<
  Omit<VisitDoc, "id" | "tenant_id" | "version" | "created_at" | "deleted_at">
>;

/** Met à jour une visite (bump version + updated_at, puis outbox). */
export async function updateVisit(
  id: string,
  patch: VisitPatch,
): Promise<RxDocument<VisitDoc> | null> {
  const db = await getDb();
  const existing = await db.visit.findOne(id).exec();
  if (!existing || existing.deleted_at) return null;
  const updated = await existing.incrementalModify((data) => {
    Object.assign(data, patch);
    data.updated_at = nowIso();
    data.version = data.version + 1;
    return data;
  });
  await enqueueOutbox(db, "update", "visit", id, updated.toJSON());
  return updated;
}

/** Supprime logiquement une visite (NFR-4) + outbox. */
export async function softDeleteVisit(
  id: string,
): Promise<RxDocument<VisitDoc> | null> {
  const db = await getDb();
  const existing = await db.visit.findOne(id).exec();
  if (!existing || existing.deleted_at) return null;
  const updated = await existing.incrementalModify((data) => {
    data.deleted_at = nowIso();
    data.updated_at = data.deleted_at;
    data.version = data.version + 1;
    return data;
  });
  await enqueueOutbox(db, "delete", "visit", id, updated.toJSON());
  return updated;
}

/**
 * Enregistre (upsert) le profil du centre **en local uniquement**, sans entrée
 * d'outbox. Le profil est 1:1 avec le tenant : on conserve son `id` et on
 * incrémente la version à chaque écriture.
 *
 * ⚠️ Contrairement aux entités `patient`/`visit`, le profil n'est **pas**
 * synchronisé via le protocole outbox : il possède son propre endpoint REST
 * dédié `PATCH /api/center/profile` (cf. `api.ts` → `updateCenterProfile`), qui
 * est la source de vérité côté serveur (ADR G-6, Option 2). N'inscrire aucune
 * mutation `center_profile` dans l'outbox : ces entrées ne sont jamais
 * consommées par le push de sync (le contrat figé ne connaît que
 * `patient`/`visit`), donc les y ajouter ferait croître l'outbox sans borne à
 * chaque édition du profil (fuite G-6). L'outbox reste ainsi borné malgré des
 * éditions répétées du profil.
 */
export async function upsertCenterProfile(
  input: Omit<CenterProfileDoc, "updated_at" | "version" | "deleted_at"> & {
    version?: number;
  },
): Promise<RxDocument<CenterProfileDoc>> {
  const db = await getDb();
  // `existing` sert au bump de version (LWW local) ; aucune entrée d'outbox
  // n'est émise (synchro du profil assurée par REST, voir docstring).
  const existing = await db.center_profile.findOne(input.id).exec();
  const ts = nowIso();
  const next: CenterProfileDoc = {
    ...input,
    deleted_at: null,
    updated_at: ts,
    version: existing ? existing.version + 1 : (input.version ?? 1),
  };
  const saved = await db.center_profile.upsert(next);
  return saved;
}

// -------------------------------------------------------------------------
// Application de changements d'origine serveur (pull delta-sync) — 14.4
// -------------------------------------------------------------------------
//
// Les deltas renvoyés par `GET /api/sync/pull` sont des changements **déjà
// validés côté serveur**. Ils doivent être écrits dans la base locale SANS
// repasser par les helpers CRUD ci-dessus (qui inscriraient une entrée d'outbox
// et ré-enverraient la mutation en boucle). On fait donc une écriture directe,
// non journalisée dans l'outbox, en respectant la version/`updated_at` fournies
// par le serveur (source de vérité pour la convergence LWW, NFR-6).

/** Collections synchronisables par le protocole figé (patient/visit). */
export type RemoteEntity = "patient" | "visit";

/**
 * Applique un changement d'origine serveur dans la base locale, **sans** créer
 * d'entrée d'outbox (ces changements ne doivent pas être ré-émis — 14.4).
 *
 * - `op === "delete"` ou `deleted === true` → suppression logique : on pose
 *   `deleted_at` tout en conservant la ligne (NFR-4). Si l'entité n'existe pas
 *   encore localement, le payload est tout de même upserté avec son `deleted_at`.
 * - sinon → upsert de l'état serveur (clé = `entity_id`).
 *
 * La `version` et `updated_at` proviennent du serveur et font autorité : on les
 * écrit telles quelles pour que le prochain conflit LWW soit tranché correctement.
 */
export async function applyRemoteChange(input: {
  entity: RemoteEntity;
  entity_id: string;
  op: "upsert" | "delete";
  payload: Record<string, unknown>;
  version: number;
  updated_at: string;
  deleted: boolean;
}): Promise<void> {
  const db = await getDb();
  // Collection ciblée par nom. Le type précis diverge entre patient/visit ; on
  // passe par `unknown` car on écrit un document déjà validé côté serveur.
  const collection = db[input.entity] as unknown as RxCollection<
    Record<string, unknown>
  >;

  const isDelete = input.op === "delete" || input.deleted;

  // Base du document : l'état existant (le cas échéant) complété par le payload
  // serveur, puis surchargé par les champs de sync autoritaires.
  const existing = await collection.findOne(input.entity_id).exec();
  const base: Record<string, unknown> = existing
    ? { ...(existing.toJSON() as Record<string, unknown>) }
    : {};

  const next: Record<string, unknown> = {
    ...base,
    ...input.payload,
    id: input.entity_id,
    version: input.version,
    updated_at: input.updated_at,
    deleted_at: isDelete
      ? ((input.payload.deleted_at as string | undefined) ?? input.updated_at)
      : ((input.payload.deleted_at as string | null | undefined) ?? null),
  };

  await collection.upsert(next);
}

/** Liste les mutations en attente (ordre de création). Utilisé par 14.4. */
export async function listOutbox(): Promise<OutboxDoc[]> {
  const db = await getDb();
  const docs = await db.outbox.find({ sort: [{ created_at: "asc" }] }).exec();
  return docs.map((d) => d.toJSON() as OutboxDoc);
}

/** Retire une mutation de l'outbox une fois confirmée par le serveur (14.4). */
export async function removeOutboxEntry(id: string): Promise<void> {
  const db = await getDb();
  const doc = await db.outbox.findOne(id).exec();
  if (doc) await doc.remove();
}
