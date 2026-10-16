// Client API typé dérivé du contrat OpenAPI (NFR-13).
// La base d'URL provient de la configuration Vite. Ce module expose le minimum
// nécessaire au moteur de sync (14.4) : les types du contrat figé
// (`backend/app/schemas/sync.py`) et deux appels HTTP typés (push/pull).
export const API_BASE_URL: string =
  import.meta.env.VITE_API_URL ?? "http://localhost:8000";

// -------------------------------------------------------------------------
// Contrat de synchronisation figé (NFR-13) — miroir de app/schemas/sync.py
// -------------------------------------------------------------------------
//
// Ces types RECOPIENT fidèlement le contrat backend. L'enum d'entités ne couvre
// que `patient`/`visit` et l'opération que `upsert`/`delete` : l'outbox local
// (operation insert/update/delete + collection `center_profile`) est mappé vers
// ce contrat dans `sync.ts`. Ne pas diverger de ce contrat.

export type SyncEntity = "patient" | "visit";
export type SyncOp = "upsert" | "delete";

/** Enveloppe d'une mutation poussée depuis l'outbox (unité d'idempotence). */
export interface Mutation {
  /** UUID de la mutation — clé d'idempotence (= id de l'entrée d'outbox). */
  mutation_id: string;
  entity: SyncEntity;
  entity_id: string;
  op: SyncOp;
  /** État de l'entité pour un upsert ; peut être vide pour un delete. */
  payload: Record<string, unknown>;
  client_version: number;
  /** ISO 8601. */
  client_updated_at: string;
  /** ISO 8601. */
  event_at: string;
}

export type MutationResultStatus =
  | "applied"
  | "duplicate"
  | "conflict"
  | "rejected";

export interface SyncConflict {
  entity: SyncEntity;
  entity_id: string;
  loser_payload: Record<string, unknown>;
  winner_version: number;
  sensitive: boolean;
}

export interface MutationResult {
  mutation_id: string;
  entity: SyncEntity;
  entity_id: string;
  status: MutationResultStatus;
  server_version: number | null;
  conflict: SyncConflict | null;
  message: string | null;
}

export interface SyncPushRequest {
  mutations: Mutation[];
}

export interface SyncPushResponse {
  results: MutationResult[];
  conflicts: SyncConflict[];
}

export interface SyncChange {
  entity: SyncEntity;
  entity_id: string;
  op: SyncOp;
  payload: Record<string, unknown>;
  version: number;
  updated_at: string;
  deleted: boolean;
}

export interface SyncPullResponse {
  changes: SyncChange[];
  /** Curseur opaque : à conserver et renvoyer tel quel au prochain pull. */
  cursor: string;
  has_more: boolean;
  revoked: boolean;
}

// -------------------------------------------------------------------------
// Injection du jeton d'authentification
// -------------------------------------------------------------------------
//
// La session (14.5) n'est pas encore câblée. Pour éviter un couplage prématuré,
// `sync.ts` fournit un « token provider » : une fonction renvoyant le jeton
// Bearer courant (ou `null` si déconnecté). 14.5 branchera `session.ts` ici.

/** Fournisseur de jeton Bearer courant (ou `null` si hors session). */
export type TokenProvider = () => string | null | Promise<string | null>;

/** Erreur HTTP structurée levée par les appels de sync. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Indique si l'accès est refusé/révoqué (401/403) — utile à la révocation. */
export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 401 || err.status === 403);
}

async function authHeaders(
  getToken: TokenProvider,
): Promise<Record<string, string>> {
  const token = await getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

/** `POST /api/sync/push` — applique un lot idempotent de mutations. */
export async function syncPush(
  body: SyncPushRequest,
  getToken: TokenProvider,
  signal?: AbortSignal,
): Promise<SyncPushResponse> {
  const res = await fetch(`${API_BASE_URL}/api/sync/push`, {
    method: "POST",
    headers: await authHeaders(getToken),
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    throw new ApiError(res.status, `sync/push a échoué (HTTP ${res.status}).`);
  }
  return (await res.json()) as SyncPushResponse;
}

/** `GET /api/sync/pull?since=<curseur>` — récupère les deltas du tenant. */
export async function syncPull(
  since: string | null,
  getToken: TokenProvider,
  signal?: AbortSignal,
): Promise<SyncPullResponse> {
  const qs = since ? `?since=${encodeURIComponent(since)}` : "";
  const res = await fetch(`${API_BASE_URL}/api/sync/pull${qs}`, {
    method: "GET",
    headers: await authHeaders(getToken),
    signal,
  });
  if (!res.ok) {
    throw new ApiError(res.status, `sync/pull a échoué (HTTP ${res.status}).`);
  }
  return (await res.json()) as SyncPullResponse;
}

// -------------------------------------------------------------------------
// Carte publique — GeoJSON des régions du Togo (tâche 15.1, exigences 7.1/7.6)
// -------------------------------------------------------------------------
//
// La lecture publique passe par `api.ts` (NFR-13) ; le service worker (tâche
// 14.2, règle `map-geojson`) sert une version en cache en cas de coupure
// (NFR-11). L'URL ci-dessous DOIT se terminer par `/geo/togo-regions.geojson`
// pour correspondre à la route de cache du service worker.

/** Code canonique d'une des 5 régions du Togo (propriété `code` du GeoJSON). */
export type RegionCode =
  | "SAVANES"
  | "KARA"
  | "CENTRALE"
  | "PLATEAUX"
  | "MARITIME";

/** Nom français de chaque région, pour l'affichage (titres, listes). */
export const REGION_LABELS: Record<RegionCode, string> = {
  SAVANES: "Savanes",
  KARA: "Kara",
  CENTRALE: "Centrale",
  PLATEAUX: "Plateaux",
  MARITIME: "Maritime",
};

/** Propriétés portées par chaque feature de région (voir data/togo-regions.geojson). */
export interface RegionFeatureProperties {
  code: RegionCode;
  name_fr: string;
  [key: string]: unknown;
}

/**
 * FeatureCollection GeoJSON des limites de régions, typée pour MapLibre.
 * Reprend la forme renvoyée par `GET /api/public/geo/togo-regions.geojson`.
 */
export interface RegionsFeatureCollection {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    properties: RegionFeatureProperties;
    geometry: GeoJSON.Geometry;
  }>;
}

/** URL (absolue) du GeoJSON des régions, mise en cache par le service worker. */
export const TOGO_REGIONS_GEOJSON_URL = `${API_BASE_URL}/api/public/geo/togo-regions.geojson`;

/**
 * `GET /api/public/geo/togo-regions.geojson` — limites des 5 régions.
 *
 * Lecture publique (sans auth). En cas de coupure réseau, le service worker
 * répond depuis le cache `map-geojson` (NFR-11), donc la carte reste disponible
 * hors ligne après la première visite.
 */
export async function fetchTogoRegionsGeoJSON(
  signal?: AbortSignal,
): Promise<RegionsFeatureCollection> {
  const res = await fetch(TOGO_REGIONS_GEOJSON_URL, {
    method: "GET",
    headers: { Accept: "application/geo+json, application/json" },
    signal,
  });
  if (!res.ok) {
    throw new ApiError(
      res.status,
      `Chargement du GeoJSON des régions a échoué (HTTP ${res.status}).`,
    );
  }
  return (await res.json()) as RegionsFeatureCollection;
}

// -------------------------------------------------------------------------
// Carte publique — centres d'une région (tâche 15.2, exigences 7.2/7.3/7.4)
// -------------------------------------------------------------------------
//
// Au clic sur une région (seam `onRegionSelect` de `TogoMap`, tâche 15.1), la
// feuille inférieure (`CenterSheet`) charge les centres publics actifs de cette
// région via `GET /api/public/regions/{code}/centers` (tâche 12). Lecture
// publique sans auth (NFR-13) ; le service worker sert une version en cache en
// cas de coupure (NFR-11). Projection sûre : uniquement des champs non sensibles
// (P4) — jamais de PHI.
//
// Ces types RECOPIENT fidèlement `PublicCenterSummary`/`GeoPoint` du contrat
// backend (`app/schemas/public.py`, `app/schemas/admin.py`). `address` et
// `location` sont optionnels : un centre peut être intégré sans profil complet.

/** Point WGS84 (miroir de `GeoPoint`, app/schemas/admin.py). */
export interface GeoPoint {
  longitude: number;
  latitude: number;
}

/**
 * Résumé public d'un centre pour la liste par région (miroir de
 * `PublicCenterSummary`, app/schemas/public.py). Champs non sensibles seulement.
 */
export interface PublicCenterSummary {
  id: string;
  name: string;
  region_code: RegionCode;
  address: string | null;
  location: GeoPoint | null;
}

/**
 * Région publique renvoyée par `GET /api/public/regions` (miroir de
 * `PublicRegion`, app/schemas/public.py). `center_count` = nombre de centres
 * publics actifs de la région (0 si vide, exigence 8.1).
 */
export interface PublicRegion {
  code: RegionCode;
  name_fr: string;
  center_count: number;
}

/** Horaires d'ouverture d'un jour (miroir de `OpeningHours`, app/schemas/center.py). */
export interface OpeningHours {
  day: string;
  open: string;
  close: string;
}

/**
 * Profil public complet d'un centre (miroir de `PublicCenterDetail`,
 * app/schemas/public.py). Étend le résumé avec le contact et les services —
 * champs non sensibles exposés publiquement (exigence 7.4). Jamais de PHI (P4).
 */
export interface PublicCenterDetail extends PublicCenterSummary {
  phone: string | null;
  email: string | null;
  opening_hours: OpeningHours[];
  service_types: string[];
}

/** URL (absolue) de la liste des régions publiques. */
export function publicRegionsUrl(): string {
  return `${API_BASE_URL}/api/public/regions`;
}

/** URL (absolue) de la liste des centres publics actifs d'une région. */
export function regionCentersUrl(code: RegionCode): string {
  return `${API_BASE_URL}/api/public/regions/${encodeURIComponent(code)}/centers`;
}

/** URL (absolue) du profil public d'un centre. */
export function publicCenterUrl(centerId: string): string {
  return `${API_BASE_URL}/api/public/centers/${encodeURIComponent(centerId)}`;
}

/**
 * `GET /api/public/regions` — les 5 régions + nombre de centres publics actifs.
 *
 * Lecture publique (sans auth). En cas de coupure réseau, le service worker peut
 * servir une version en cache (NFR-11). Une réponse non-OK lève une `ApiError`.
 */
export async function fetchPublicRegions(
  signal?: AbortSignal,
): Promise<PublicRegion[]> {
  const res = await fetch(publicRegionsUrl(), {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!res.ok) {
    throw new ApiError(
      res.status,
      `Chargement des régions a échoué (HTTP ${res.status}).`,
    );
  }
  return (await res.json()) as PublicRegion[];
}

/**
 * `GET /api/public/centers/{id}` — profil public d'un centre (projection sûre).
 *
 * Lecture publique (sans auth). Un centre inconnu ou appartenant à un tenant
 * désactivé renvoie 404 (P8) : l'appelant lève alors une `ApiError` de statut
 * 404. En cas de coupure réseau, le service worker peut servir une version en
 * cache (NFR-11). Projection sûre : uniquement des champs non sensibles (P4).
 */
export async function fetchPublicCenter(
  centerId: string,
  signal?: AbortSignal,
): Promise<PublicCenterDetail> {
  const res = await fetch(publicCenterUrl(centerId), {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!res.ok) {
    throw new ApiError(
      res.status,
      `Chargement du profil du centre a échoué (HTTP ${res.status}).`,
    );
  }
  return (await res.json()) as PublicCenterDetail;
}

/**
 * `GET /api/public/regions/{code}/centers` — centres publics actifs d'une région.
 *
 * Lecture publique (sans auth). Une région sans centre renvoie `[]` (jamais une
 * erreur, exigence 7.3). En cas de coupure réseau, le service worker peut servir
 * une version en cache (NFR-11). Une réponse non-OK lève une `ApiError` typée.
 */
export async function fetchRegionCenters(
  code: RegionCode,
  signal?: AbortSignal,
): Promise<PublicCenterSummary[]> {
  const res = await fetch(regionCentersUrl(code), {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!res.ok) {
    throw new ApiError(
      res.status,
      `Chargement des centres de la région a échoué (HTTP ${res.status}).`,
    );
  }
  return (await res.json()) as PublicCenterSummary[];
}

// -------------------------------------------------------------------------
// Profil du centre (authentifié, tenant_admin) — GET/PATCH /api/center/profile
// -------------------------------------------------------------------------
//
// Contrat figé en tâche 11.1 (`backend/app/schemas/center.py`). Contrairement
// aux PHI (`patient`/`visit`), le profil du centre n'est PAS synchronisé via le
// protocole push/pull (voir le TODO en tête de `sync.ts`) : il a son propre
// endpoint REST. L'éditeur `CenterAdmin` (tâche 17.2) consomme donc directement
// ces deux appels, authentifiés par le `TokenProvider` de la session (14.5).
//
// Garanties du contrat reflétées ici :
//   - lecture ouverte aux rôles cadrés au tenant ; mise à jour réservée au
//     `tenant_admin` (403 sinon) — géré côté serveur ;
//   - `tenant_id`/`id` jamais acceptés dans le corps (apposés par le serveur, P2) ;
//   - coordonnées hors de la frontière du Togo → **422** (P7).

/** Profil complet du centre (miroir de `CenterProfileResponse`, center.py). */
export interface CenterProfile {
  id: string;
  tenant_id: string;
  name: string;
  region_code: RegionCode;
  address: string;
  location: GeoPoint;
  phone: string;
  email: string;
  opening_hours: OpeningHours[];
  service_types: string[];
  is_public: boolean;
}

/**
 * Mise à jour partielle du profil (miroir de `CenterProfileUpdateRequest`).
 * Seuls les champs fournis sont transmis (le serveur applique `exclude_unset`).
 * Ni `id` ni `tenant_id` : apposés par le serveur depuis le jeton (P2).
 */
export interface CenterProfileUpdate {
  name?: string;
  address?: string;
  location?: GeoPoint;
  phone?: string;
  email?: string;
  opening_hours?: OpeningHours[];
  service_types?: string[];
}

/** URL (absolue) du profil du centre du tenant authentifié. */
export function centerProfileUrl(): string {
  return `${API_BASE_URL}/api/center/profile`;
}

/**
 * `GET /api/center/profile` — profil du centre du tenant authentifié.
 *
 * Authentifié (Bearer via `getToken`). Un tenant sans centre intégré → 404
 * (`ApiError` de statut 404). Un rôle non autorisé → 403.
 */
export async function fetchCenterProfile(
  getToken: TokenProvider,
  signal?: AbortSignal,
): Promise<CenterProfile> {
  const res = await fetch(centerProfileUrl(), {
    method: "GET",
    headers: await authHeaders(getToken),
    signal,
  });
  if (!res.ok) {
    throw new ApiError(
      res.status,
      `Chargement du profil du centre a échoué (HTTP ${res.status}).`,
    );
  }
  return (await res.json()) as CenterProfile;
}

/**
 * `PATCH /api/center/profile` — met à jour le profil (réservé au tenant_admin).
 *
 * Authentifié (Bearer via `getToken`). Coordonnées hors Togo → **422** (P7) ;
 * type de service inconnu → 422 ; rôle non autorisé → 403 ; profil absent → 404.
 * Toutes ces réponses non-OK lèvent une `ApiError` typée (statut préservé).
 */
export async function updateCenterProfile(
  patch: CenterProfileUpdate,
  getToken: TokenProvider,
  signal?: AbortSignal,
): Promise<CenterProfile> {
  const res = await fetch(centerProfileUrl(), {
    method: "PATCH",
    headers: await authHeaders(getToken),
    body: JSON.stringify(patch),
    signal,
  });
  if (!res.ok) {
    throw new ApiError(
      res.status,
      `Mise à jour du profil du centre a échoué (HTTP ${res.status}).`,
    );
  }
  return (await res.json()) as CenterProfile;
}
