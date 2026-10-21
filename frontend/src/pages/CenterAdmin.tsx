// Éditeur du profil du centre pour le tenant_admin (tâche 17.2) — REQ 6.1, 3.2.
//
// Rôle
// ----
// Permet au `tenant_admin` de modifier le profil de SON centre : nom, adresse,
// coordonnées (épingle sur carte contrainte au Togo), contact (téléphone /
// courriel), horaires d'ouverture et services proposés (exigence 6.1). Les
// autres rôles cadrés au tenant (`clinician`, `staff`) peuvent CONSULTER le
// profil mais n'ont aucune affordance d'édition ; un `platform_admin`
// (transversal, sans tenant) ou un visiteur non authentifié n'accède pas à la
// page (message dédié).
//
// Architecture de données (local-first, mais profil géré en ligne)
// ----------------------------------------------------------------
// Les PHI (`patient`/`visit`) passent par la base locale + sync (tâches 14/17.3).
// Le **profil du centre**, lui, n'est PAS dans le protocole de sync push/pull
// (voir le TODO en tête de `sync.ts`) : il possède son propre endpoint REST
// `GET/PATCH /api/center/profile` (contrat figé en tâche 11.1). On aligne donc
// cet éditeur sur `session.ts` (jeton Bearer) et `api.ts` (appels typés), comme
// le prévoit explicitement la tâche. Le profil reste ainsi la source consolidée
// côté serveur, qui applique l'autorité de tenancy (P2), la validation géo (P7)
// et le référentiel de services.
//
// Contrainte géographique (P7, exigence 6.2)
// ------------------------------------------
// L'épingle est posée via `CenterPinPicker`, borné au Togo (`lib/geo.ts`, miroir
// du backend). On revalide `isInsideTogo` avant l'envoi : si (improbablement) la
// coordonnée est hors zone, on bloque localement ; le serveur reste l'autorité
// finale et renvoie 422 le cas échéant (message dédié).
//
// Mobile-first (NFR-9) : colonne unique, champs pleine largeur, cibles tactiles
// >=44px, français par défaut (NFR-10).

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ParseKeys } from "i18next";

import {
  ApiError,
  fetchCenterProfile,
  REGION_LABELS,
  updateCenterProfile,
  type CenterProfile,
  type CenterProfileUpdate,
  type OpeningHours,
} from "../lib/api";
import { isInsideTogo, type LngLat } from "../lib/geo";
import {
  getSessionState,
  getToken,
  subscribeSession,
  type SessionState,
} from "../lib/session";
import { useOnline } from "../lib/useOnline";
import CenterPinPicker from "../components/CenterPinPicker";

/** Classe-outil : champ pleine largeur, cible tactile >=44px. */
const FIELD_CLASS =
  "min-h-11 w-full rounded-lg border border-slate-300 bg-white px-3 "
  + "text-base text-slate-800 outline-none focus:border-sky-500 "
  + "focus:ring-2 focus:ring-sky-200 disabled:opacity-60";

const PRIMARY_BTN_CLASS =
  "inline-flex min-h-11 w-full items-center justify-center rounded-lg "
  + "bg-sky-600 px-4 text-base font-medium text-white active:bg-sky-700 "
  + "disabled:opacity-60";

/** Jours de la semaine (ordre d'affichage FR) pour l'éditeur d'horaires. */
const WEEK_DAYS = [
  "lundi",
  "mardi",
  "mercredi",
  "jeudi",
  "vendredi",
  "samedi",
  "dimanche",
] as const;

// -------------------------------------------------------------------------
// État de chargement / édition
// -------------------------------------------------------------------------

type LoadState =
  | { phase: "loading" }
  | { phase: "notFound" }
  | { phase: "error" }
  | { phase: "ready"; profile: CenterProfile };

/** Forme éditable (dérivée du profil chargé). */
interface Draft {
  name: string;
  address: string;
  phone: string;
  email: string;
  location: LngLat | null;
  opening_hours: OpeningHours[];
  service_types: string[];
}

function toDraft(p: CenterProfile): Draft {
  return {
    name: p.name,
    address: p.address,
    phone: p.phone,
    email: p.email,
    location: p.location
      ? { lng: p.location.longitude, lat: p.location.latitude }
      : null,
    opening_hours: p.opening_hours.map((h) => ({ ...h })),
    service_types: [...p.service_types],
  };
}

/** Traduit une erreur de mise à jour vers une clé i18n (contrat d'erreur). */
function saveErrorKey(err: unknown): ParseKeys {
  if (err instanceof ApiError) {
    if (err.status === 422) return "pages.centerAdmin.errors.outsideTogo";
    if (err.status === 403) return "pages.centerAdmin.errors.forbidden";
    if (err.status === 404) return "pages.centerAdmin.errors.notFound";
    return "pages.centerAdmin.errors.generic";
  }
  // `fetch` a levé : réseau indisponible pour cette écriture en ligne.
  return "pages.centerAdmin.errors.offline";
}

export default function CenterAdmin() {
  const { t } = useTranslation();
  const [session, setSession] = useState<SessionState>(() => getSessionState());

  useEffect(() => subscribeSession(setSession), []);

  const canEdit = session.user?.role === "tenant_admin";
  // Accès : il faut une session déverrouillée appartenant à un tenant.
  const hasTenantAccess =
    session.authenticated &&
    session.unlocked &&
    session.user !== null &&
    session.user.tenant_id !== null;

  if (!hasTenantAccess) {
    return (
      <section className="flex flex-col gap-3">
        <h1 className="text-xl font-semibold md:text-2xl">
          {t("pages.centerAdmin.title")}
        </h1>
        <p
          className="rounded-lg border border-slate-200 bg-white p-4 text-sm
            text-slate-600"
          role="status"
        >
          {t("pages.centerAdmin.signInRequired")}
        </p>
      </section>
    );
  }

  return <CenterAdminEditor canEdit={canEdit} />;
}

// -------------------------------------------------------------------------
// Éditeur (session tenant active)
// -------------------------------------------------------------------------

function CenterAdminEditor({ canEdit }: { canEdit: boolean }) {
  const { t } = useTranslation();

  // État de connectivité lu à la source navigateur (ADR-2) : sert à signaler
  // a priori que l'édition du profil (endpoint REST en ligne — ADR G-6) est
  // indisponible hors-ligne, et à désactiver l'enregistrement (ADR-1).
  const online = useOnline();

  const [state, setState] = useState<LoadState>({ phase: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const [draft, setDraft] = useState<Draft | null>(null);

  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<ParseKeys | null>(null);
  const [saved, setSaved] = useState(false);
  const [pinRejected, setPinRejected] = useState(false);

  // --- Chargement du profil (GET /api/center/profile) --------------------
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setState({ phase: "loading" });

    fetchCenterProfile(getToken, controller.signal)
      .then((profile) => {
        if (!active) return;
        setState({ phase: "ready", profile });
        setDraft(toDraft(profile));
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || !active) return;
        if (err instanceof ApiError && err.status === 404) {
          setState({ phase: "notFound" });
          return;
        }
        setState({ phase: "error" });
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [reloadToken]);

  const regionLabel = useMemo(
    () =>
      state.phase === "ready"
        ? REGION_LABELS[state.profile.region_code]
        : "",
    [state],
  );

  if (state.phase === "loading") {
    return (
      <Shell>
        <p
          className="py-8 text-center text-sm text-slate-500"
          role="status"
          aria-live="polite"
        >
          {t("pages.centerAdmin.loading")}
        </p>
      </Shell>
    );
  }

  if (state.phase === "notFound") {
    return (
      <Shell>
        <p
          className="rounded-lg border border-slate-200 bg-white p-4
            text-center text-sm text-slate-500"
          role="status"
        >
          {t("pages.centerAdmin.notFound")}
        </p>
      </Shell>
    );
  }

  if (state.phase === "error") {
    // Cas « ouverte déjà hors-ligne » (#4, L1) : le GET du profil a échoué.
    // Hors-ligne, on distingue l'absence de réseau d'une vraie erreur serveur
    // en affichant le message hors-ligne (role="status", informatif — ADR G-6 /
    // cohérent avec la bannière 3.2) plutôt que « erreur de chargement »
    // générique (role="alert"). Le bouton « Réessayer » est conservé dans les
    // deux cas : hors-ligne, l'utilisateur pourra recharger au retour du réseau.
    return (
      <Shell>
        <div
          className="py-8 text-center"
          role={online ? "alert" : "status"}
        >
          <p
            className={online ? "text-sm text-red-700" : "text-sm text-amber-800"}
          >
            {online
              ? t("pages.centerAdmin.loadError")
              : t("pages.centerAdmin.offlineLoad")}
          </p>
          <button
            type="button"
            onClick={() => setReloadToken((n) => n + 1)}
            className="mt-3 inline-flex min-h-11 items-center rounded-lg border
              border-slate-300 bg-white px-4 text-sm font-medium text-sky-700
              active:bg-slate-100"
          >
            {t("pages.centerAdmin.retry")}
          </button>
        </div>
      </Shell>
    );
  }

  // state.phase === "ready"
  const profile = state.profile;
  const d = draft ?? toDraft(profile);

  function update<K extends keyof Draft>(key: K, value: Draft[K]) {
    setSaved(false);
    setDraft((prev) => ({ ...(prev ?? toDraft(profile)), [key]: value }));
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canEdit || saving) return;
    setSaveErr(null);
    setSaved(false);

    // Garde-fou P7 côté client : refuse une coordonnée hors Togo avant l'envoi.
    if (d.location && !isInsideTogo(d.location.lng, d.location.lat)) {
      setSaveErr("pages.centerAdmin.errors.outsideTogo");
      return;
    }

    const patch: CenterProfileUpdate = {
      name: d.name,
      address: d.address,
      phone: d.phone,
      email: d.email,
      opening_hours: d.opening_hours,
      service_types: d.service_types,
    };
    if (d.location) {
      patch.location = { longitude: d.location.lng, latitude: d.location.lat };
    }

    setSaving(true);
    try {
      const updated = await updateCenterProfile(patch, getToken);
      setState({ phase: "ready", profile: updated });
      setDraft(toDraft(updated));
      setSaved(true);
    } catch (err) {
      setSaveErr(saveErrorKey(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Shell>
      {/* Rôle non-admin : lecture seule explicite (exigence 6.1). */}
      {!canEdit && (
        <p
          className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2
            text-sm text-amber-800"
          role="status"
        >
          {t("pages.centerAdmin.readOnly")}
        </p>
      )}

      {/*
        Signalement a priori hors-ligne (ADR-1 / ADR G-6). Le profil du centre
        passe par un endpoint REST en ligne : hors-ligne, l'édition est
        indisponible — on le dit AVANT l'action (bouton désactivé plus bas),
        plutôt que de laisser un bouton actif échouer au clic. La seconde phrase
        distingue le profil des PHI (patients/visites) qui, eux, fonctionnent
        hors-ligne (local-first), pour éviter que l'utilisateur croie à une
        incohérence de l'application. Réservé aux admins : un rôle en lecture
        seule n'enregistre rien, le signalement n'aurait pas de sens pour lui.
      */}
      {canEdit && !online && (
        <div
          role="status"
          className="flex flex-col gap-1 rounded-lg border border-amber-200
            bg-amber-50 px-3 py-2 text-sm text-amber-800"
        >
          <p className="font-medium">{t("pages.centerAdmin.offlineBanner")}</p>
          <p className="text-amber-700">
            {t("pages.centerAdmin.offlineHint")}
          </p>
        </div>
      )}

      <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
        <fieldset
          disabled={!canEdit || saving || !online}
          className="flex flex-col gap-4 border-0 p-0"
        >
          {/* Nom + région (région en lecture seule : fixée à l'intégration). */}
          <TextField
            label={t("pages.centerAdmin.nameLabel")}
            value={d.name}
            onChange={(v) => update("name", v)}
            required
          />

          <div className="flex flex-col gap-1">
            <span className="text-sm font-medium text-slate-700">
              {t("pages.centerAdmin.regionLabel")}
            </span>
            <p className="text-sm text-slate-600">{regionLabel}</p>
          </div>

          <TextField
            label={t("pages.centerAdmin.addressLabel")}
            value={d.address}
            onChange={(v) => update("address", v)}
          />

          <TextField
            label={t("pages.centerAdmin.phoneLabel")}
            value={d.phone}
            onChange={(v) => update("phone", v)}
            type="tel"
            inputMode="tel"
            placeholder={t("formats.phonePrefix")}
          />

          <TextField
            label={t("pages.centerAdmin.emailLabel")}
            value={d.email}
            onChange={(v) => update("email", v)}
            type="email"
            inputMode="email"
          />

          {/* Épingle sur carte contrainte au Togo (P7). */}
          <div className="flex flex-col gap-2">
            <span className="text-sm font-medium text-slate-700">
              {t("pages.centerAdmin.locationLabel")}
            </span>
            <p className="text-xs text-slate-500">
              {t("pages.centerAdmin.locationHint")}
            </p>
            <CenterPinPicker
              value={d.location}
              onChange={(point) => {
                setPinRejected(false);
                update("location", point);
              }}
              onReject={() => {
                setPinRejected(true);
              }}
            />
            {pinRejected && (
              <p
                role="alert"
                className="rounded-lg border border-red-200 bg-red-50 px-3 py-2
                  text-sm text-red-700"
              >
                {t("pages.centerAdmin.errors.outsideTogo")}
              </p>
            )}
            <p className="font-mono text-xs text-slate-600">
              {d.location
                ? `${d.location.lat.toFixed(5)}, ${d.location.lng.toFixed(5)}`
                : t("pages.centerAdmin.noLocation")}
            </p>
          </div>

          {/* Horaires d'ouverture (par jour). */}
          <OpeningHoursEditor
            value={d.opening_hours}
            onChange={(v) => update("opening_hours", v)}
            disabled={!canEdit}
          />

          {/* Services proposés (étiquettes libres, résolues côté serveur). */}
          <ServicesEditor
            value={d.service_types}
            onChange={(v) => update("service_types", v)}
            disabled={!canEdit}
          />
        </fieldset>

        {saveErr && (
          <p
            role="alert"
            className="rounded-lg border border-red-200 bg-red-50 px-3 py-2
              text-sm text-red-700"
          >
            {t(saveErr)}
          </p>
        )}

        {saved && (
          <p
            role="status"
            className="rounded-lg border border-emerald-200 bg-emerald-50 px-3
              py-2 text-sm text-emerald-800"
          >
            {t("pages.centerAdmin.saved")}
          </p>
        )}

        {canEdit && (
          <button
            type="submit"
            disabled={saving || !online}
            aria-describedby={!online ? "center-admin-offline-save" : undefined}
            className={PRIMARY_BTN_CLASS}
          >
            {saving
              ? t("pages.centerAdmin.saving")
              : t("pages.centerAdmin.save")}
          </button>
        )}

        {/* Aide associée au bouton désactivé hors-ligne (affordance honnête). */}
        {canEdit && !online && (
          <p
            id="center-admin-offline-save"
            className="text-center text-xs text-slate-500"
          >
            {t("pages.centerAdmin.offlineSaveHint")}
          </p>
        )}
      </form>
    </Shell>
  );
}

/** Ossature commune (titre + contenu). */
function Shell({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold md:text-2xl">
        {t("pages.centerAdmin.title")}
      </h1>
      {children}
    </section>
  );
}

// -------------------------------------------------------------------------
// Sous-composants d'édition
// -------------------------------------------------------------------------

function TextField({
  label,
  value,
  onChange,
  type = "text",
  inputMode,
  placeholder,
  required,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  inputMode?: "text" | "tel" | "email";
  placeholder?: string;
  required?: boolean;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-sm font-medium text-slate-700">{label}</span>
      <input
        type={type}
        inputMode={inputMode}
        required={required}
        value={value}
        placeholder={placeholder}
        onChange={(ev) => onChange(ev.target.value)}
        className={FIELD_CLASS}
      />
    </label>
  );
}

/** Éditeur d'horaires : une ligne par jour avec ouverture/fermeture. */
function OpeningHoursEditor({
  value,
  onChange,
  disabled,
}: {
  value: OpeningHours[];
  onChange: (next: OpeningHours[]) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();

  function byDay(day: string): OpeningHours | undefined {
    return value.find((h) => h.day === day);
  }

  function setDay(day: string, patch: Partial<OpeningHours>): void {
    const existing = byDay(day);
    let next: OpeningHours[];
    if (existing) {
      next = value.map((h) => (h.day === day ? { ...h, ...patch } : h));
    } else {
      next = [...value, { day, open: "", close: "", ...patch }];
    }
    // Retire les lignes vides (jour fermé = pas d'horaire enregistré).
    onChange(next.filter((h) => h.open !== "" || h.close !== ""));
  }

  return (
    <div className="flex flex-col gap-2">
      <span className="text-sm font-medium text-slate-700">
        {t("pages.centerAdmin.hoursLabel")}
      </span>
      <ul className="flex flex-col gap-2">
        {WEEK_DAYS.map((day) => {
          const h = byDay(day);
          return (
            <li key={day} className="flex items-center gap-2">
              <span className="w-24 shrink-0 text-sm capitalize text-slate-600">
                {day}
              </span>
              <input
                type="time"
                aria-label={t("pages.centerAdmin.openAria", { day })}
                disabled={disabled}
                value={h?.open ?? ""}
                onChange={(ev) => setDay(day, { open: ev.target.value })}
                className="min-h-11 flex-1 rounded-lg border border-slate-300
                  bg-white px-2 text-sm text-slate-800 outline-none
                  focus:border-sky-500 disabled:opacity-60"
              />
              <span aria-hidden="true" className="text-slate-400">
                –
              </span>
              <input
                type="time"
                aria-label={t("pages.centerAdmin.closeAria", { day })}
                disabled={disabled}
                value={h?.close ?? ""}
                onChange={(ev) => setDay(day, { close: ev.target.value })}
                className="min-h-11 flex-1 rounded-lg border border-slate-300
                  bg-white px-2 text-sm text-slate-800 outline-none
                  focus:border-sky-500 disabled:opacity-60"
              />
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Éditeur de services : étiquettes ajoutables/supprimables (texte libre). */
function ServicesEditor({
  value,
  onChange,
  disabled,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");

  function add(): void {
    const name = input.trim();
    if (!name) return;
    // Évite les doublons (comparaison insensible à la casse).
    if (value.some((s) => s.toLowerCase() === name.toLowerCase())) {
      setInput("");
      return;
    }
    onChange([...value, name]);
    setInput("");
  }

  function remove(name: string): void {
    onChange(value.filter((s) => s !== name));
  }

  return (
    <div className="flex flex-col gap-2">
      <span className="text-sm font-medium text-slate-700">
        {t("pages.centerAdmin.servicesLabel")}
      </span>

      {value.length > 0 ? (
        <ul className="flex flex-wrap gap-2">
          {value.map((svc) => (
            <li
              key={svc}
              className="inline-flex items-center gap-1 rounded-full bg-sky-50
                px-3 py-1 text-sm text-sky-800"
            >
              {svc}
              {!disabled && (
                <button
                  type="button"
                  onClick={() => remove(svc)}
                  aria-label={t("pages.centerAdmin.removeServiceAria", {
                    service: svc,
                  })}
                  className="ml-1 inline-flex h-6 w-6 items-center justify-center
                    rounded-full text-sky-700 active:bg-sky-100"
                >
                  ×
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-slate-400">
          {t("pages.centerAdmin.noServices")}
        </p>
      )}

      {!disabled && (
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={input}
            placeholder={t("pages.centerAdmin.servicePlaceholder")}
            onChange={(ev) => setInput(ev.target.value)}
            onKeyDown={(ev) => {
              if (ev.key === "Enter") {
                ev.preventDefault();
                add();
              }
            }}
            className={FIELD_CLASS}
          />
          <button
            type="button"
            onClick={add}
            className="inline-flex min-h-11 shrink-0 items-center justify-center
              rounded-lg border border-sky-300 bg-white px-4 text-sm font-medium
              text-sky-700 active:bg-sky-50"
          >
            {t("pages.centerAdmin.addService")}
          </button>
        </div>
      )}
    </div>
  );
}
