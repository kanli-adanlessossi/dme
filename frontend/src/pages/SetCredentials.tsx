// Configuration du mot de passe via jeton d'invitation à usage unique (REQ-3.2,
// tâche 17.1).
//
// Rôle
// ----
// Consomme le jeton de configuration à usage unique et expirant émis à
// l'intégration d'un tenant/utilisateur (tâches 7.4 / 8.1) :
// `POST /api/auth/set-credentials` fixe le mot de passe (haché Argon2id côté
// serveur) et invalide le jeton. Aucun mot de passe n'est jamais transmis en
// clair à l'intégration — seul ce flux l'établit.
//
// Contrat d'erreur (design « Configuration des identifiants »)
// ------------------------------------------------------------
//   - Jeton invalide / expiré / déjà consommé → **400/410 générique** : message
//     unique, qui ne révèle jamais si le compte ou le jeton existe (P3).
//   - Hors ligne → `fetch` lève (pas d'`ApiError`) : message dédié invitant à se
//     reconnecter au réseau (cette opération exige d'être en ligne).
//
// Validation côté client (miroir du backend `min_length=12`) : longueur minimale
// et confirmation du mot de passe, pour un retour immédiat avant tout aller-retour
// réseau. Le serveur reste l'autorité.
//
// Mobile-first (NFR-9) : champs pleine largeur, cibles tactiles >=44px, libellés
// en français (NFR-10). En cas de succès, un écran de confirmation propose de
// revenir à la connexion (la 1ʳᵉ connexion établit ensuite la session locale).

import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ParseKeys } from "i18next";

import { ApiError } from "../lib/api";
import { setCredentials } from "../lib/session";

/** Longueur minimale du mot de passe (miroir de `min_length=12` côté backend). */
const MIN_PASSWORD_LENGTH = 12;

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

/** Mappe une erreur de `set-credentials` vers une clé i18n (contrat d'erreur). */
function setupErrorKey(err: unknown): ParseKeys {
  if (err instanceof ApiError) {
    // 400 (jeton invalide) et 410 (expiré / déjà consommé) : message générique
    // unique, sans fuite d'existence (P3).
    if (err.status === 400 || err.status === 410) {
      return "pages.account.setCredentials.invalidToken";
    }
    return "pages.account.setCredentials.genericError";
  }
  // `fetch` a levé (réseau indisponible) : l'opération exige d'être en ligne.
  return "pages.account.setCredentials.offlineError";
}

export default function SetCredentials({
  onBack,
}: {
  /** Retour vers la page de connexion (bascule par état local dans Login). */
  onBack?: () => void;
}) {
  const { t } = useTranslation();

  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errorKey, setErrorKey] = useState<ParseKeys | null>(null);
  const [done, setDone] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setErrorKey(null);

    // Validation locale (retour immédiat) — le serveur reste l'autorité.
    if (password.length < MIN_PASSWORD_LENGTH) {
      setErrorKey("pages.account.setCredentials.tooShort");
      return;
    }
    if (password !== confirm) {
      setErrorKey("pages.account.setCredentials.mismatch");
      return;
    }

    setSubmitting(true);
    try {
      await setCredentials(token.trim(), password);
      setDone(true);
      setPassword("");
      setConfirm("");
      setToken("");
    } catch (err) {
      setErrorKey(setupErrorKey(err));
    } finally {
      setSubmitting(false);
    }
  }

  // Écran de confirmation : mot de passe établi, on invite à se connecter.
  if (done) {
    return (
      <section className="flex flex-col gap-4">
        <h1 className="text-xl font-semibold md:text-2xl">
          {t("pages.account.setCredentials.title")}
        </h1>
        <p
          role="status"
          className="rounded-lg border border-emerald-200 bg-emerald-50 px-3
            py-2 text-sm text-emerald-800"
        >
          {t("pages.account.setCredentials.success")}
        </p>
        {onBack && (
          <button type="button" onClick={onBack} className={PRIMARY_BTN_CLASS}>
            {t("pages.account.setCredentials.goToLogin")}
          </button>
        )}
      </section>
    );
  }

  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold md:text-2xl">
        {t("pages.account.setCredentials.title")}
      </h1>
      <p className="text-sm text-slate-600">
        {t("pages.account.setCredentials.intro")}
      </p>

      <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-slate-700">
            {t("pages.account.setCredentials.tokenLabel")}
          </span>
          <input
            type="text"
            name="setup_token"
            autoComplete="off"
            required
            value={token}
            onChange={(ev) => setToken(ev.target.value)}
            placeholder={t("pages.account.setCredentials.tokenPlaceholder")}
            className={FIELD_CLASS}
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-slate-700">
            {t("pages.account.setCredentials.passwordLabel")}
          </span>
          <input
            type="password"
            name="password"
            autoComplete="new-password"
            required
            minLength={MIN_PASSWORD_LENGTH}
            value={password}
            onChange={(ev) => setPassword(ev.target.value)}
            placeholder={t("pages.account.setCredentials.passwordPlaceholder")}
            className={FIELD_CLASS}
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-slate-700">
            {t("pages.account.setCredentials.confirmLabel")}
          </span>
          <input
            type="password"
            name="confirm"
            autoComplete="new-password"
            required
            minLength={MIN_PASSWORD_LENGTH}
            value={confirm}
            onChange={(ev) => setConfirm(ev.target.value)}
            placeholder={t("pages.account.setCredentials.confirmPlaceholder")}
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
            ? t("pages.account.setCredentials.submitting")
            : t("pages.account.setCredentials.submit")}
        </button>
      </form>

      {onBack && (
        <div className="flex justify-center border-t border-slate-100 pt-4">
          <button type="button" onClick={onBack} className={LINK_BTN_CLASS}>
            {t("pages.account.setCredentials.backToLogin")}
          </button>
        </div>
      )}
    </section>
  );
}
