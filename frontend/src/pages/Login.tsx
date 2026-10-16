// Connexion tenant (REQ-3.2) et gestion de session local-first (tâche 17.1).
//
// Rôle
// ----
// Point d'entrée authentifié de l'application. Trois états selon la session
// (`session.ts`, propriétaire du cycle de vie — tâche 14.5) :
//
//  1. **Non authentifié** → formulaire de connexion. La PREMIÈRE connexion est
//     EN LIGNE : `login()` appelle `POST /api/auth/login`, dérive la clé de
//     chiffrement du mot de passe et ouvre la base locale chiffrée. Une session
//     locale est alors persistée (jetons + profil) pour continuer à travailler
//     pendant une coupure réseau tant que le jeton est valide (NFR-7).
//  2. **Authentifié mais verrouillé** (après rechargement : les jetons ont
//     survécu, pas la clé dérivée — P13) → formulaire de déverrouillage.
//     `unlockSession()` re-dérive la clé depuis le mot de passe sans réémettre de
//     jeton (fonctionne hors ligne).
//  3. **Authentifié et déverrouillé** → profil + déconnexion. `logout()`
//     VERROUILLE la clé de chiffrement (ferme la base) : les PHI au repos
//     redeviennent illisibles sans nouvelle authentification (NFR-8, P13).
//
// Contrat d'erreur (design « Échecs d'authentification »)
// -------------------------------------------------------
//   - Échec de connexion → **401 générique** : message unique « e-mail ou mot de
//     passe incorrect », qui ne révèle jamais si l'e-mail existe (P3).
//   - Verrouillage de compte → **429** : message « trop de tentatives » (NFR-2).
//   - Hors ligne pendant la 1ʳᵉ connexion → `fetch` lève (pas d'`ApiError`) :
//     message dédié invitant à se reconnecter au réseau.
//
// Mobile-first (NFR-9) : champs pleine largeur, cibles tactiles >=44px
// (`min-h-11`), libellés en français (NFR-10). Pas de react-router : la bascule
// Connexion <-> Configuration se fait par état local, comme les autres pages.

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ParseKeys } from "i18next";

import { ApiError } from "../lib/api";
import {
  getSessionState,
  login,
  logout,
  subscribeSession,
  unlockSession,
  type Role,
  type SessionState,
} from "../lib/session";
import SetCredentials from "./SetCredentials";
import CenterAdmin from "./CenterAdmin";
import Patients from "./Patients";
import Visits from "./Visits";

/** Sous-vues accessibles depuis une session tenant déverrouillée. */
type TenantView = "account" | "center" | "patients" | "visits";

/** Classe-outil partagée : cible tactile >=44px, pleine largeur, accessible. */
const FIELD_CLASS =
  "min-h-11 w-full rounded-lg border border-slate-300 bg-white px-3 "
  + "text-base text-slate-800 outline-none focus:border-sky-500 "
  + "focus:ring-2 focus:ring-sky-200 disabled:opacity-60";

const PRIMARY_BTN_CLASS =
  "inline-flex min-h-11 w-full items-center justify-center rounded-lg "
  + "bg-sky-600 px-4 text-base font-medium text-white active:bg-sky-700 "
  + "disabled:opacity-60";

const LINK_BTN_CLASS =
  "inline-flex min-h-11 items-center justify-center rounded-lg px-2 "
  + "text-sm font-medium text-sky-700 active:bg-slate-100";

/** Clés i18n du libellé de rôle (typage fort, pas de template literal). */
const ROLE_LABEL_KEYS: Record<Role, ParseKeys> = {
  platform_admin: "pages.account.session.roles.platform_admin",
  tenant_admin: "pages.account.session.roles.tenant_admin",
  clinician: "pages.account.session.roles.clinician",
  staff: "pages.account.session.roles.staff",
};

/** Mappe une erreur de connexion vers une clé i18n (contrat d'erreur). */
function loginErrorKey(err: unknown): ParseKeys {
  if (err instanceof ApiError) {
    if (err.status === 429) return "pages.account.login.lockedError";
    if (err.status === 401) return "pages.account.login.invalidCredentials";
    return "pages.account.login.genericError";
  }
  // `fetch` a levé (réseau indisponible) : la 1ʳᵉ connexion exige d'être en ligne.
  return "pages.account.login.offlineError";
}

export default function Login() {
  const [session, setSession] = useState<SessionState>(() => getSessionState());
  const [showSetup, setShowSetup] = useState(false);
  // Sous-vue active de l'aire tenant (compte, profil, patients, visites).
  const [tenantView, setTenantView] = useState<TenantView>("account");

  // S'abonne à l'état de session : réagit au login/logout/révocation/expiration.
  useEffect(() => subscribeSession(setSession), []);

  // Déjà authentifié ET déverrouillé → aire tenant (profil + travail clinique).
  if (session.authenticated && session.unlocked && session.user) {
    const hasTenant = session.user.tenant_id !== null;
    const back = () => setTenantView("account");

    // Les sous-vues cliniques/profil ne sont atteignables qu'avec un tenant.
    if (hasTenant && tenantView === "center") {
      return <TenantSubView onBack={back}><CenterAdmin /></TenantSubView>;
    }
    if (hasTenant && tenantView === "patients") {
      return <TenantSubView onBack={back}><Patients /></TenantSubView>;
    }
    if (hasTenant && tenantView === "visits") {
      return <TenantSubView onBack={back}><Visits /></TenantSubView>;
    }
    return <SessionPanel session={session} onNavigate={setTenantView} />;
  }

  // Page de configuration du mot de passe (jeton d'invitation à usage unique).
  if (showSetup) {
    return <SetCredentials onBack={() => setShowSetup(false)} />;
  }

  // Authentifié mais verrouillé (après rechargement) → déverrouillage ;
  // sinon → connexion complète (1ʳᵉ connexion en ligne).
  const needsUnlock = session.authenticated && !session.unlocked;

  return (
    <AuthForm
      mode={needsUnlock ? "unlock" : "login"}
      presetEmail={needsUnlock ? session.user?.email ?? "" : ""}
      onShowSetup={() => setShowSetup(true)}
    />
  );
}

// -------------------------------------------------------------------------
// Formulaire de connexion / déverrouillage
// -------------------------------------------------------------------------

function AuthForm({
  mode,
  presetEmail,
  onShowSetup,
}: {
  mode: "login" | "unlock";
  presetEmail: string;
  onShowSetup: () => void;
}) {
  const { t } = useTranslation();

  const [email, setEmail] = useState(presetEmail);
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errorKey, setErrorKey] = useState<ParseKeys | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setErrorKey(null);
    setSubmitting(true);
    try {
      if (mode === "unlock") {
        await unlockSession(email.trim(), password);
      } else {
        await login(email.trim(), password);
      }
      // Succès : l'abonnement à la session fait basculer l'UI (SessionPanel).
      setPassword("");
    } catch (err) {
      setErrorKey(loginErrorKey(err));
    } finally {
      setSubmitting(false);
    }
  }

  const isUnlock = mode === "unlock";

  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold md:text-2xl">
        {t("pages.account.login.title")}
      </h1>
      <p className="text-sm text-slate-600">
        {isUnlock
          ? t("pages.account.login.unlockIntro")
          : t("pages.account.login.intro")}
      </p>

      <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-slate-700">
            {t("pages.account.login.emailLabel")}
          </span>
          <input
            type="email"
            name="email"
            autoComplete="username"
            inputMode="email"
            required
            value={email}
            // En mode déverrouillage, l'e-mail de la session est fixe.
            readOnly={isUnlock}
            onChange={(ev) => setEmail(ev.target.value)}
            placeholder={t("pages.account.login.emailPlaceholder")}
            className={FIELD_CLASS + (isUnlock ? " bg-slate-50" : "")}
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-slate-700">
            {t("pages.account.login.passwordLabel")}
          </span>
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(ev) => setPassword(ev.target.value)}
            placeholder={t("pages.account.login.passwordPlaceholder")}
            className={FIELD_CLASS}
          />
        </label>

        {errorKey && (
          <p
            role="alert"
            className="rounded-lg border border-red-200 bg-red-50 px-3 py-2
              text-sm text-red-700"
          >
            {t(errorKey)}
          </p>
        )}

        <button type="submit" disabled={submitting} className={PRIMARY_BTN_CLASS}>
          {submitting
            ? t("pages.account.login.submitting")
            : isUnlock
              ? t("pages.account.login.unlockSubmit")
              : t("pages.account.login.submit")}
        </button>
      </form>

      {/* Lien vers la configuration du mot de passe : masqué en déverrouillage. */}
      {!isUnlock && (
        <div className="flex flex-col items-center gap-1 border-t
          border-slate-100 pt-4 text-center">
          <p className="text-sm text-slate-600">
            {t("pages.account.login.firstTime")}
          </p>
          <button type="button" onClick={onShowSetup} className={LINK_BTN_CLASS}>
            {t("pages.account.login.goToSetup")}
          </button>
        </div>
      )}
    </section>
  );
}

// -------------------------------------------------------------------------
// Panneau de session active (profil + déconnexion)
// -------------------------------------------------------------------------

function SessionPanel({
  session,
  onNavigate,
}: {
  session: SessionState;
  onNavigate: (view: TenantView) => void;
}) {
  const { t } = useTranslation();
  const [loggingOut, setLoggingOut] = useState(false);

  const user = session.user!;
  // Les rôles cadrés au tenant (tenant_admin/clinician/staff) ont un centre ;
  // le platform_admin (transversal) n'en a pas (tenant_id null).
  const hasTenant = user.tenant_id !== null;

  async function onLogout() {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      // Verrouille la clé de chiffrement (ferme la base) : PHI au repos
      // inaccessibles sans nouvelle authentification (NFR-8, P13).
      await logout();
    } finally {
      setLoggingOut(false);
    }
  }

  const roleLabel = t(ROLE_LABEL_KEYS[user.role]);

  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold md:text-2xl">
        {t("pages.account.title")}
      </h1>

      <div className="flex flex-col gap-2 rounded-lg border border-slate-200
        bg-white p-4">
        <div className="flex flex-col gap-0.5">
          <span className="text-xs uppercase tracking-wide text-slate-500">
            {t("pages.account.session.signedInAs")}
          </span>
          <span className="break-all font-medium text-slate-800">
            {user.email}
          </span>
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="text-xs uppercase tracking-wide text-slate-500">
            {t("pages.account.session.roleLabel")}
          </span>
          <span className="text-slate-800">{roleLabel}</span>
        </div>
        <div className="flex items-center gap-2 pt-1">
          <span
            aria-hidden="true"
            className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${
              session.unlocked ? "bg-emerald-500" : "bg-slate-400"
            }`}
          />
          <span className="text-sm text-slate-600">
            {session.unlocked
              ? t("pages.account.session.unlocked")
              : t("pages.account.session.locked")}
          </span>
        </div>
      </div>

      {/* Aire de travail tenant : profil (17.2) + patients/visites (17.3). */}
      {hasTenant && (
        <nav className="flex flex-col gap-2" aria-label={t("pages.account.session.workAreaLabel")}>
          <button
            type="button"
            onClick={() => onNavigate("patients")}
            className="inline-flex min-h-11 w-full items-center justify-center
              rounded-lg border border-sky-300 bg-white px-4 text-base font-medium
              text-sky-700 active:bg-sky-50"
          >
            {t("pages.account.session.managePatients")}
          </button>
          <button
            type="button"
            onClick={() => onNavigate("visits")}
            className="inline-flex min-h-11 w-full items-center justify-center
              rounded-lg border border-sky-300 bg-white px-4 text-base font-medium
              text-sky-700 active:bg-sky-50"
          >
            {t("pages.account.session.manageVisits")}
          </button>
          <button
            type="button"
            onClick={() => onNavigate("center")}
            className="inline-flex min-h-11 w-full items-center justify-center
              rounded-lg border border-sky-300 bg-white px-4 text-base font-medium
              text-sky-700 active:bg-sky-50"
          >
            {t("pages.account.session.manageCenter")}
          </button>
        </nav>
      )}

      <button
        type="button"
        onClick={onLogout}
        disabled={loggingOut}
        className="inline-flex min-h-11 w-full items-center justify-center
          rounded-lg border border-red-300 bg-white px-4 text-base font-medium
          text-red-700 active:bg-red-50 disabled:opacity-60"
      >
        {loggingOut
          ? t("pages.account.session.loggingOut")
          : t("pages.account.session.logout")}
      </button>
    </section>
  );
}

/** Enveloppe une sous-vue tenant (profil/patients/visites) + retour au compte. */
function TenantSubView({
  onBack,
  children,
}: {
  onBack: () => void;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-3">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex min-h-11 w-fit items-center gap-1 text-sm
          font-medium text-sky-700 active:opacity-70"
      >
        <span aria-hidden="true">←</span>
        {t("pages.account.session.backToAccount")}
      </button>
      {children}
    </div>
  );
}
