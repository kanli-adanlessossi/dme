// Session locale et authentification tolérante à l'hors-ligne — NFR-7 / NFR-8 / P13.
//
// Rôle (tâche 14.5)
// -----------------
// `session.ts` est le propriétaire du **cycle de vie de la session** sur le poste :
//
//  1. **Connexion en ligne** (`login`) : appelle `POST /api/auth/login`, conserve
//     une session locale (access + refresh + expiration + tenant_id + rôle +
//     identité) pour permettre de travailler pendant une coupure réseau (NFR-7).
//  2. **Clé de chiffrement dérivée** (NFR-8, P13) : la clé qui déverrouille la base
//     locale est **dérivée du mot de passe** via Web Crypto PBKDF2-SHA256 + un sel
//     par utilisateur. Elle n'est **jamais** persistée en clair. `initDb(cle)`
//     déverrouille la base chiffrée (`db.ts`).
//  3. **Fournisseur de jeton** : expose un `TokenProvider` (voir `api.ts`) renvoyant
//     l'access token courant tant qu'il est valide, branché au moteur de sync via
//     `configureSync({ getToken })` (`sync.ts`).
//  4. **Rafraîchissement opportuniste** : en ligne et proche de l'expiration, appelle
//     `POST /api/auth/refresh`. Un échec **hors ligne** ne déconnecte PAS : on
//     continue de travailler jusqu'à l'expiration réelle du jeton (NFR-7).
//  5. **Verrouillage** (`logout` / expiration) : ferme la base via `closeDb()` — la
//     clé est verrouillée, les PHI au repos restent chiffrées et inaccessibles sans
//     nouvelle authentification (P13) — et efface les jetons en mémoire + persistés.
//  6. **Révocation** (NFR-7) : `sync.ts` purge déjà les PHI (`destroyDb()`) quand le
//     pull renvoie `revoked=true` ou un 401/403. La session s'abonne à l'état de
//     sync et, dès qu'il passe à `revoked`, efface ses jetons (`purgeSession`) pour
//     ne laisser aucune session exploitable. Couplage minimal : la session observe,
//     la sync purge les PHI — aucune duplication de `destroyDb`.
//
// Modèle de menace / rechargement (design « Chiffrement des données locales »)
// ---------------------------------------------------------------------------
// La menace couverte est la **perte ou le vol du poste**. En conséquence :
//  - Les **jetons** peuvent être persistés (survie au rechargement pour l'usage
//    hors-ligne). Compromis assumé : sur un poste volé, les jetons seuls ne donnent
//    pas accès aux PHI, car la clé de chiffrement n'est PAS persistée.
//  - La **clé dérivée** et le **mot de passe** ne sont JAMAIS persistés.
//  - Conséquence directe du choix « clé dérivée du mot de passe » : après un
//    rechargement de l'onglet, la clé n'est plus en mémoire. La base reste donc
//    **verrouillée** jusqu'à une nouvelle saisie du mot de passe (re-`login`), qui
//    re-dérive la clé. C'est le comportement le plus conforme à P13 : un poste
//    volé puis rechargé ne peut pas rouvrir les PHI sans le mot de passe.
//    `restoreSession()` restaure l'**état** d'auth (jetons/rôle) pour l'UI, mais
//    laisse la base verrouillée tant que la clé n'a pas été re-dérivée.

import {
  API_BASE_URL,
  ApiError,
  type TokenProvider,
} from "./api";
import { closeDb, destroyDb, initDb, isDbOpen } from "./db";
import { configureSync } from "./sync";

// -------------------------------------------------------------------------
// Types du contrat d'authentification (miroir de app/schemas/auth.py)
// -------------------------------------------------------------------------

/** Rôles applicatifs (miroir de `Role` côté backend). */
export type Role = "platform_admin" | "tenant_admin" | "clinician" | "staff";

/** Réponse `POST /api/auth/login` et `/refresh` (miroir de `TokenResponse`). */
interface TokenResponse {
  access: string;
  refresh: string;
  token_type: string;
  /** Durée de vie de l'access token en secondes. */
  expires_in: number;
  /** `null` pour un platform_admin (opérations transversales). */
  tenant_id: string | null;
  role: Role;
}

/** Réponse `POST /api/auth/set-credentials` (miroir de `SetCredentialsResponse`). */
export interface SetCredentialsResult {
  email: string;
  status: string;
}

// -------------------------------------------------------------------------
// État de session exposé à l'UI
// -------------------------------------------------------------------------

/** Identité + contexte dérivés du jeton (jamais du corps — P2). */
export interface SessionUser {
  email: string;
  role: Role;
  /** `null` pour un platform_admin. */
  tenant_id: string | null;
}

/** Instantané d'état de session exposé à l'UI (NFR-7). */
export interface SessionState {
  /** Vrai tant qu'un jeton d'accès connu n'est pas expiré. */
  authenticated: boolean;
  /** Identité courante (ou `null` hors session). */
  user: SessionUser | null;
  /** Expiration de l'access token (ms epoch), ou `null` hors session. */
  expiresAt: number | null;
  /**
   * Vrai si la base locale est déverrouillée (clé dérivée en mémoire).
   * Faux après un rechargement tant que le mot de passe n'a pas été ressaisi,
   * même si l'état d'auth est restauré depuis les jetons persistés.
   */
  unlocked: boolean;
}

type SessionListener = (state: SessionState) => void;

// -------------------------------------------------------------------------
// Persistance : UNIQUEMENT ce qui est sûr (jetons + sel), jamais la clé/mot de passe
// -------------------------------------------------------------------------

const SESSION_KEY = "dme.session";
const SALT_PREFIX = "dme.kdf.salt."; // + email : sel PBKDF2 par utilisateur (non secret).

/** Forme persistée : jetons + méta pour l'usage hors-ligne. Pas de clé/mot de passe. */
interface PersistedSession {
  access: string;
  refresh: string;
  expiresAt: number;
  user: SessionUser;
}

function readPersisted(): PersistedSession | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as PersistedSession;
  } catch {
    return null;
  }
}

function writePersisted(value: PersistedSession): void {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(value));
  } catch {
    /* localStorage indisponible : la session ne survivra pas au rechargement. */
  }
}

function clearPersisted(): void {
  try {
    localStorage.removeItem(SESSION_KEY);
  } catch {
    /* ignore */
  }
}

// -------------------------------------------------------------------------
// Dérivation de la clé de chiffrement (Web Crypto PBKDF2-SHA256) — NFR-8 / P13
// -------------------------------------------------------------------------
//
// La clé de la base locale est dérivée du mot de passe + d'un sel aléatoire par
// utilisateur (persisté en clair : un sel n'est pas un secret). Le résultat est
// encodé en hexadécimal et passé à `initDb(cle)` comme mot de passe de chiffrement
// RxDB. Ni la clé ni le mot de passe ne sont persistés.

const PBKDF2_ITERATIONS = 210_000; // Recommandation OWASP PBKDF2-SHA256 (2023+).
const DERIVED_KEY_BITS = 256;

const textEncoder = new TextEncoder();

/** Récupère (ou crée et persiste) le sel PBKDF2 d'un utilisateur, en hex. */
function getOrCreateSalt(email: string): Uint8Array {
  const storageKey = SALT_PREFIX + email.toLowerCase();
  try {
    const existing = localStorage.getItem(storageKey);
    if (existing) return hexToBytes(existing);
  } catch {
    /* ignore : on régénère un sel éphémère ci-dessous. */
  }
  const salt = new Uint8Array(16);
  crypto.getRandomValues(salt);
  try {
    localStorage.setItem(storageKey, bytesToHex(salt));
  } catch {
    /* le sel ne survivra pas : la clé différera au prochain démarrage. */
  }
  return salt;
}

function bytesToHex(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

function hexToBytes(hex: string): Uint8Array {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

/**
 * Dérive la clé de chiffrement de la base locale à partir du mot de passe.
 * La valeur retournée (hex) sert de mot de passe de chiffrement à `initDb`.
 * N'est jamais persistée.
 */
async function deriveEncryptionKey(
  password: string,
  salt: Uint8Array,
): Promise<string> {
  const baseKey = await crypto.subtle.importKey(
    "raw",
    textEncoder.encode(password),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    {
      name: "PBKDF2",
      salt: salt as unknown as BufferSource,
      iterations: PBKDF2_ITERATIONS,
      hash: "SHA-256",
    },
    baseKey,
    DERIVED_KEY_BITS,
  );
  return bytesToHex(new Uint8Array(bits));
}

// -------------------------------------------------------------------------
// Gestionnaire de session (singleton)
// -------------------------------------------------------------------------

/** Marge avant expiration déclenchant un rafraîchissement opportuniste (ms). */
const REFRESH_SKEW_MS = 5 * 60 * 1000; // 5 minutes.

class SessionManager {
  /** Access token courant (en mémoire). */
  private access: string | null = null;
  private refreshToken: string | null = null;
  private expiresAt: number | null = null;
  private user: SessionUser | null = null;
  /** Clé dérivée en mémoire : présente seulement tant que la base est déverrouillée. */
  private unlocked = false;

  /** Rafraîchissement en vol : évite les appels concurrents. */
  private refreshing: Promise<void> | null = null;
  private readonly listeners = new Set<SessionListener>();
  /** Désabonnement de l'état de sync (révocation). */
  private syncSub: { unsubscribe(): void } | null = null;

  // --- Observabilité pour l'UI -------------------------------------------

  /** Abonne un écouteur à l'état de session ; renvoie une fonction de désabonnement. */
  subscribe(listener: SessionListener): () => void {
    this.listeners.add(listener);
    listener(this.getState());
    return () => this.listeners.delete(listener);
  }

  /** Instantané synchrone de l'état de session. */
  getState(): SessionState {
    return {
      authenticated: this.hasValidToken(),
      user: this.user,
      expiresAt: this.expiresAt,
      unlocked: this.unlocked && isDbOpen(),
    };
  }

  private emit(): void {
    const state = this.getState();
    for (const l of this.listeners) l(state);
  }

  // --- Fournisseur de jeton (branché à sync via configureSync) -----------

  /**
   * `TokenProvider` pour `sync.ts` : renvoie l'access token courant s'il est
   * encore valide, sinon tente un rafraîchissement opportuniste (en ligne) puis
   * renvoie le nouveau jeton. En cas d'échec hors ligne, renvoie le jeton
   * existant tant qu'il n'a pas réellement expiré (NFR-7), ou `null`.
   */
  readonly getToken: TokenProvider = async (): Promise<string | null> => {
    if (!this.access || this.expiresAt === null) return null;

    const now = Date.now();
    if (now >= this.expiresAt) {
      // Jeton réellement expiré : tenter un ultime rafraîchissement en ligne.
      await this.tryRefresh();
      if (this.access && this.expiresAt !== null && Date.now() < this.expiresAt) {
        return this.access;
      }
      // Impossible de rafraîchir et jeton expiré : session verrouillée (P13).
      await this.lock();
      return null;
    }

    // Proche de l'expiration : rafraîchissement opportuniste non bloquant pour le
    // chemin principal (on renvoie le jeton courant, encore valide).
    if (now >= this.expiresAt - REFRESH_SKEW_MS) {
      void this.tryRefresh();
    }
    return this.access;
  };

  // --- Connexion ---------------------------------------------------------

  /**
   * Connexion en ligne : authentifie, ouvre la base chiffrée (clé dérivée du mot
   * de passe) et branche le moteur de sync. Requiert le réseau.
   */
  async login(email: string, password: string): Promise<SessionUser> {
    const res = await postJson<TokenResponse>("/api/auth/login", {
      email,
      password,
    });

    // Dérive la clé de chiffrement AVANT de poser l'état : si l'ouverture de la
    // base échoue, on ne laisse pas une session « authentifiée mais verrouillée ».
    const salt = getOrCreateSalt(email);
    const encryptionKey = await deriveEncryptionKey(password, salt);
    await initDb(encryptionKey);

    this.applyTokens(res, email);
    this.unlocked = true;
    this.persist();
    this.wireSync();
    this.emit();
    return this.user as SessionUser;
  }

  /**
   * Déverrouille une session restaurée (jetons présents après rechargement) en
   * re-dérivant la clé à partir du mot de passe. Utilisé par les pages de
   * connexion lorsqu'un jeton valide existe déjà mais que la base est verrouillée.
   */
  async unlock(email: string, password: string): Promise<void> {
    if (!this.hasValidToken()) {
      // Pas de jeton valide : repasser par une connexion complète.
      await this.login(email, password);
      return;
    }
    const salt = getOrCreateSalt(email);
    const encryptionKey = await deriveEncryptionKey(password, salt);
    await initDb(encryptionKey);
    this.unlocked = true;
    this.wireSync();
    this.emit();
  }

  /**
   * Helper d'API : fixe le mot de passe initial via un jeton de configuration
   * (`POST /api/auth/set-credentials`). Les pages UI relèvent de la tâche 17 ;
   * ce helper évite de dupliquer le contrat ailleurs.
   */
  async setCredentials(
    setupToken: string,
    password: string,
  ): Promise<SetCredentialsResult> {
    return postJson<SetCredentialsResult>("/api/auth/set-credentials", {
      setup_token: setupToken,
      password,
    });
  }

  // --- Restauration au démarrage ----------------------------------------

  /**
   * Restaure l'**état d'auth** depuis les jetons persistés (survie au
   * rechargement, NFR-7). N'ouvre PAS la base : la clé dérivée n'est pas
   * persistée (P13), donc la base reste verrouillée jusqu'à `unlock`/`login`.
   * Si le jeton persisté a expiré, purge tout.
   */
  restoreSession(): SessionState {
    const persisted = readPersisted();
    if (!persisted) return this.getState();

    if (Date.now() >= persisted.expiresAt) {
      // Jeton expiré au démarrage : rien d'exploitable, on nettoie (P13).
      clearPersisted();
      return this.getState();
    }

    this.access = persisted.access;
    this.refreshToken = persisted.refresh;
    this.expiresAt = persisted.expiresAt;
    this.user = persisted.user;
    this.unlocked = false; // Clé non dérivée : base verrouillée tant que pas d'unlock.
    this.wireSync();
    this.emit();
    return this.getState();
  }

  // --- Déconnexion / verrouillage / purge --------------------------------

  /**
   * Déconnexion : VERROUILLE la clé (ferme la base via `closeDb()`, les PHI au
   * repos restent chiffrées) et efface les jetons en mémoire + persistés (P13).
   */
  async logout(): Promise<void> {
    await this.lock();
  }

  /**
   * Verrouillage : ferme la base (clé verrouillée) et efface la session. Utilisé
   * par `logout` et par l'expiration réelle du jeton. Les PHI au repos ne sont
   * PAS effacées (différent de la révocation).
   */
  private async lock(): Promise<void> {
    try {
      if (isDbOpen()) await closeDb();
    } finally {
      this.clearInMemory();
      clearPersisted();
      this.emit();
    }
  }

  /**
   * Purge de session sur **révocation** (NFR-7). `sync.ts` a déjà purgé les PHI
   * via `destroyDb()` ; ici on efface uniquement les jetons/état pour ne laisser
   * aucune session exploitable. On ne duplique pas la purge des PHI.
   *
   * @param alreadyPurgedBySync si `false`, on purge aussi les PHI par sécurité
   *   (appel direct hors du chemin de sync).
   */
  async purgeSession(alreadyPurgedBySync = true): Promise<void> {
    try {
      if (!alreadyPurgedBySync && isDbOpen()) {
        await destroyDb();
      }
    } finally {
      this.clearInMemory();
      clearPersisted();
      this.emit();
    }
  }

  // --- Internes ----------------------------------------------------------

  private applyTokens(res: TokenResponse, email: string): void {
    this.access = res.access;
    this.refreshToken = res.refresh;
    this.expiresAt = Date.now() + res.expires_in * 1000;
    this.user = { email, role: res.role, tenant_id: res.tenant_id };
  }

  private clearInMemory(): void {
    this.access = null;
    this.refreshToken = null;
    this.expiresAt = null;
    this.user = null;
    this.unlocked = false;
    if (this.syncSub) {
      this.syncSub.unsubscribe();
      this.syncSub = null;
    }
  }

  private hasValidToken(): boolean {
    return (
      this.access !== null &&
      this.expiresAt !== null &&
      Date.now() < this.expiresAt
    );
  }

  private persist(): void {
    if (!this.access || !this.refreshToken || this.expiresAt === null || !this.user) {
      return;
    }
    writePersisted({
      access: this.access,
      refresh: this.refreshToken,
      expiresAt: this.expiresAt,
      user: this.user,
    });
  }

  /**
   * Rafraîchissement opportuniste. Un échec **hors ligne** (réseau) ne déconnecte
   * pas : on garde la session jusqu'à l'expiration réelle (NFR-7). Un 401/403
   * (refresh invalide/révoqué) verrouille la session.
   */
  private async tryRefresh(): Promise<void> {
    if (this.refreshing) return this.refreshing;
    if (!this.refreshToken) return;

    this.refreshing = (async () => {
      const email = this.user?.email ?? "";
      try {
        const res = await postJson<TokenResponse>("/api/auth/refresh", {
          refresh: this.refreshToken,
        });
        this.applyTokens(res, email);
        this.persist();
        this.emit();
      } catch (err) {
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          // Refresh rejeté/révoqué : verrouiller (P13). Pas de purge PHI ici — la
          // révocation serveur passe par le pull de sync (destroyDb).
          await this.lock();
        }
        // Échec réseau (hors ligne) : on ne fait rien, le jeton courant reste
        // utilisable jusqu'à expiration réelle (NFR-7).
      } finally {
        this.refreshing = null;
      }
    })();
    return this.refreshing;
  }

  /**
   * Branche le moteur de sync sur ce fournisseur de jeton et s'abonne à son état
   * pour réagir à la révocation (phase `revoked` => purge des jetons de session,
   * les PHI ayant déjà été purgées par la sync).
   */
  private wireSync(): void {
    const engine = configureSync({ getToken: this.getToken });
    if (this.syncSub) this.syncSub.unsubscribe();
    this.syncSub = engine.state.subscribe((s) => {
      if (s.phase === "revoked") {
        void this.purgeSession(true);
      }
    });
    engine.start();
  }
}

// -------------------------------------------------------------------------
// Appel HTTP JSON typé (login/refresh/set-credentials)
// -------------------------------------------------------------------------

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${path} a échoué (HTTP ${res.status}).`);
  }
  return (await res.json()) as T;
}

// -------------------------------------------------------------------------
// Singleton + API de module
// -------------------------------------------------------------------------

const manager = new SessionManager();

/** Connexion en ligne : authentifie, dérive la clé et ouvre la base chiffrée. */
export function login(email: string, password: string): Promise<SessionUser> {
  return manager.login(email, password);
}

/** Déconnexion : verrouille la clé (ferme la base) et efface la session (P13). */
export function logout(): Promise<void> {
  return manager.logout();
}

/**
 * Déverrouille une session restaurée en re-dérivant la clé depuis le mot de passe
 * (après un rechargement où seuls les jetons ont survécu).
 */
export function unlockSession(email: string, password: string): Promise<void> {
  return manager.unlock(email, password);
}

/** Fixe le mot de passe initial via un jeton de configuration (helper d'API). */
export function setCredentials(
  setupToken: string,
  password: string,
): Promise<SetCredentialsResult> {
  return manager.setCredentials(setupToken, password);
}

/**
 * Restaure l'état d'auth depuis les jetons persistés (sans ouvrir la base : la
 * clé n'est pas persistée). À appeler au démarrage de l'app.
 */
export function restoreSession(): SessionState {
  return manager.restoreSession();
}

/** Purge de session sur révocation (efface les jetons ; PHI purgées par la sync). */
export function purgeSession(alreadyPurgedBySync = true): Promise<void> {
  return manager.purgeSession(alreadyPurgedBySync);
}

/** Instantané synchrone de l'état de session. */
export function getSessionState(): SessionState {
  return manager.getState();
}

/** S'abonne à l'état de session ; renvoie une fonction de désabonnement. */
export function subscribeSession(listener: SessionListener): () => void {
  return manager.subscribe(listener);
}

/**
 * Fournisseur de jeton courant (pour `configureSync({ getToken })`). Déjà branché
 * par la session au login/restore ; exposé pour les intégrations avancées.
 */
export const getToken: TokenProvider = (...args) => manager.getToken(...args);
