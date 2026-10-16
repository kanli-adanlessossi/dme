// Interface CRUD Patients — local-first (tâche 17.3) — REQ 4.1, 4.2, 5.1, 5.6 ; P9.
//
// Rôle
// ----
// Gestion des patients du centre par un utilisateur cadré au tenant. **Toutes**
// les lectures et écritures passent par la base locale RxDB (`lib/db.ts`) : les
// pages ne parlent JAMAIS à l'API tenant directement — `sync.ts` est le seul
// composant qui synchronise l'outbox avec le serveur (voir le patron local-first
// du design). Conséquence : l'écran fonctionne hors ligne (P9), chaque écriture
// est journalisée dans l'outbox et répliquée plus tard.
//
// Consentement (REQ-5.6)
// ----------------------
// Le formulaire de création capture obligatoirement `consent_to_store` : on ne
// peut pas créer un patient sans cocher le consentement au stockage de ses
// données (le bouton reste désactivé tant que la case n'est pas cochée).
//
// Suppression logique (NFR-4)
// ---------------------------
// La suppression est logique (`softDeletePatient` pose `deleted_at`) : la ligne
// est conservée et la liste masque les patients supprimés.
//
// Mobile-first (NFR-9) : colonne unique, cibles tactiles >=44px, français (NFR-10).

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Subscription } from "rxjs";

import {
  createPatient,
  getDb,
  softDeletePatient,
  updatePatient,
  type PatientDoc,
} from "../lib/db";
import {
  getSessionState,
  subscribeSession,
  type SessionState,
} from "../lib/session";
import ConflictBanner from "../components/ConflictBanner";
import SyncStatusBar from "../components/SyncStatusBar";

/** Classe-outil : champ pleine largeur, cible tactile >=44px. */
const FIELD_CLASS =
  "min-h-11 w-full rounded-lg border border-slate-300 bg-white px-3 "
  + "text-base text-slate-800 outline-none focus:border-sky-500 "
  + "focus:ring-2 focus:ring-sky-200 disabled:opacity-60";

const PRIMARY_BTN_CLASS =
  "inline-flex min-h-11 w-full items-center justify-center rounded-lg "
  + "bg-sky-600 px-4 text-base font-medium text-white active:bg-sky-700 "
  + "disabled:opacity-60";

export default function Patients() {
  const { t } = useTranslation();
  const [session, setSession] = useState<SessionState>(() => getSessionState());

  useEffect(() => subscribeSession(setSession), []);

  // Accès : session déverrouillée appartenant à un tenant (local-first).
  const hasTenantAccess =
    session.authenticated &&
    session.unlocked &&
    session.user !== null &&
    session.user.tenant_id !== null;

  if (!hasTenantAccess) {
    return (
      <section className="flex flex-col gap-3">
        <h1 className="text-xl font-semibold md:text-2xl">
          {t("pages.patients.title")}
        </h1>
        <p
          className="rounded-lg border border-slate-200 bg-white p-4 text-sm
            text-slate-600"
          role="status"
        >
          {t("pages.patients.signInRequired")}
        </p>
      </section>
    );
  }

  return (
    <PatientsWorkspace
      tenantId={session.user!.tenant_id as string}
      userEmail={session.user!.email}
    />
  );
}

// -------------------------------------------------------------------------
// Espace de travail (session tenant active)
// -------------------------------------------------------------------------

function PatientsWorkspace({
  tenantId,
  userEmail,
}: {
  tenantId: string;
  userEmail: string;
}) {
  const { t } = useTranslation();

  const [patients, setPatients] = useState<PatientDoc[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [query, setQuery] = useState("");

  // Abonnement réactif à la base LOCALE : la liste se met à jour à chaque
  // écriture locale (ou application d'un delta serveur), sans appel réseau (P9).
  useEffect(() => {
    let sub: Subscription | null = null;
    let active = true;
    (async () => {
      const db = await getDb();
      if (!active) return;
      const q = db.patient.find({
        selector: { deleted_at: null },
        sort: [{ updated_at: "desc" }],
      });
      sub = q.$.subscribe((docs) => {
        setPatients(docs.map((d) => d.toJSON() as PatientDoc));
        setLoading(false);
      });
    })();
    return () => {
      active = false;
      sub?.unsubscribe();
    };
  }, []);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return patients;
    return patients.filter((p) =>
      `${p.given_name} ${p.family_name} ${p.mrn}`
        .toLowerCase()
        .includes(needle),
    );
  }, [patients, query]);

  async function onDelete(id: string) {
    await softDeletePatient(id);
  }

  return (
    <section className="flex flex-col gap-4">
      {/* État de synchronisation + conflits à revoir, dans l'aire tenant. */}
      <SyncStatusBar />
      <ConflictBanner />

      <div className="flex items-center justify-between gap-2">
        <h1 className="text-xl font-semibold md:text-2xl">
          {t("pages.patients.title")}
        </h1>
        <button
          type="button"
          onClick={() => setShowForm((v) => !v)}
          className="inline-flex min-h-11 shrink-0 items-center justify-center
            rounded-lg border border-sky-300 bg-white px-4 text-sm font-medium
            text-sky-700 active:bg-sky-50"
        >
          {showForm
            ? t("pages.patients.closeForm")
            : t("pages.patients.addPatient")}
        </button>
      </div>

      {showForm && (
        <PatientForm
          tenantId={tenantId}
          userEmail={userEmail}
          onCreated={() => setShowForm(false)}
        />
      )}

      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("pages.patients.searchPlaceholder")}
        aria-label={t("pages.patients.searchPlaceholder")}
        className={FIELD_CLASS}
      />

      {loading ? (
        <p className="py-8 text-center text-sm text-slate-500" role="status">
          {t("pages.patients.loading")}
        </p>
      ) : filtered.length === 0 ? (
        <p
          className="rounded-lg border border-slate-200 bg-white p-4 text-center
            text-sm text-slate-500"
          role="status"
        >
          {t("pages.patients.empty")}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {filtered.map((p) => (
            <PatientRow key={p.id} patient={p} onDelete={onDelete} />
          ))}
        </ul>
      )}
    </section>
  );
}

// -------------------------------------------------------------------------
// Ligne de patient (lecture + suppression logique)
// -------------------------------------------------------------------------

function PatientRow({
  patient,
  onDelete,
}: {
  patient: PatientDoc;
  onDelete: (id: string) => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);

  if (editing) {
    return (
      <li className="rounded-lg border border-slate-200 bg-white p-3">
        <PatientEditForm
          patient={patient}
          onDone={() => setEditing(false)}
        />
      </li>
    );
  }

  return (
    <li className="flex items-center gap-2 rounded-lg border border-slate-200
      bg-white p-3">
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-slate-800">
          {patient.given_name} {patient.family_name}
        </p>
        <p className="truncate text-xs text-slate-500">
          {t("pages.patients.mrnLabel")}: {patient.mrn}
          {patient.phone ? ` · ${patient.phone}` : ""}
        </p>
      </div>
      <button
        type="button"
        onClick={() => setEditing(true)}
        aria-label={t("pages.patients.editAria", {
          name: `${patient.given_name} ${patient.family_name}`,
        })}
        className="inline-flex min-h-11 min-w-11 items-center justify-center
          rounded-lg border border-slate-300 bg-white px-3 text-sm
          text-sky-700 active:bg-slate-100"
      >
        {t("pages.patients.edit")}
      </button>
      <button
        type="button"
        onClick={() => void onDelete(patient.id)}
        aria-label={t("pages.patients.deleteAria", {
          name: `${patient.given_name} ${patient.family_name}`,
        })}
        className="inline-flex min-h-11 min-w-11 items-center justify-center
          rounded-lg border border-red-300 bg-white px-3 text-sm text-red-700
          active:bg-red-50"
      >
        {t("pages.patients.delete")}
      </button>
    </li>
  );
}

// -------------------------------------------------------------------------
// Formulaire de création (consentement obligatoire — REQ-5.6)
// -------------------------------------------------------------------------

function PatientForm({
  tenantId,
  userEmail,
  onCreated,
}: {
  tenantId: string;
  userEmail: string;
  onCreated: () => void;
}) {
  const { t } = useTranslation();

  const [mrn, setMrn] = useState("");
  const [givenName, setGivenName] = useState("");
  const [familyName, setFamilyName] = useState("");
  const [phone, setPhone] = useState("");
  const [consent, setConsent] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Le consentement est requis à la création (REQ-5.6) : pas de consentement,
  // pas d'enregistrement possible.
  const canSubmit =
    consent &&
    mrn.trim() !== "" &&
    givenName.trim() !== "" &&
    familyName.trim() !== "";

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit || submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      await createPatient({
        tenant_id: tenantId,
        mrn: mrn.trim(),
        given_name: givenName.trim(),
        family_name: familyName.trim(),
        dob: null,
        sex: null,
        phone: phone.trim() || null,
        national_id: null,
        consent_to_store: consent,
        created_by: userEmail,
      });
      onCreated();
    } catch {
      setError(t("pages.patients.saveError"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      className="flex flex-col gap-3 rounded-lg border border-slate-200 bg-white
        p-4"
      onSubmit={onSubmit}
      noValidate
    >
      <h2 className="text-base font-semibold text-slate-800">
        {t("pages.patients.newPatient")}
      </h2>

      <Field
        label={t("pages.patients.mrnLabel")}
        value={mrn}
        onChange={setMrn}
        required
      />
      <Field
        label={t("pages.patients.givenNameLabel")}
        value={givenName}
        onChange={setGivenName}
        required
      />
      <Field
        label={t("pages.patients.familyNameLabel")}
        value={familyName}
        onChange={setFamilyName}
        required
      />
      <Field
        label={t("pages.patients.phoneLabel")}
        value={phone}
        onChange={setPhone}
        type="tel"
        inputMode="tel"
        placeholder={t("formats.phonePrefix")}
      />

      {/* Consentement obligatoire (REQ-5.6). */}
      <label className="flex items-start gap-2 rounded-lg bg-slate-50 p-3">
        <input
          type="checkbox"
          checked={consent}
          onChange={(e) => setConsent(e.target.checked)}
          className="mt-0.5 h-5 w-5 shrink-0"
        />
        <span className="text-sm text-slate-700">
          {t("pages.patients.consentLabel")}
        </span>
      </label>

      {error && (
        <p
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 px-3 py-2
            text-sm text-red-700"
        >
          {error}
        </p>
      )}

      <button type="submit" disabled={!canSubmit || submitting} className={PRIMARY_BTN_CLASS}>
        {submitting ? t("pages.patients.saving") : t("pages.patients.save")}
      </button>
    </form>
  );
}

// -------------------------------------------------------------------------
// Formulaire d'édition (mise à jour locale)
// -------------------------------------------------------------------------

function PatientEditForm({
  patient,
  onDone,
}: {
  patient: PatientDoc;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [givenName, setGivenName] = useState(patient.given_name);
  const [familyName, setFamilyName] = useState(patient.family_name);
  const [phone, setPhone] = useState(patient.phone ?? "");
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      await updatePatient(patient.id, {
        given_name: givenName.trim(),
        family_name: familyName.trim(),
        phone: phone.trim() || null,
      });
      onDone();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="flex flex-col gap-3" onSubmit={onSubmit} noValidate>
      <Field
        label={t("pages.patients.givenNameLabel")}
        value={givenName}
        onChange={setGivenName}
        required
      />
      <Field
        label={t("pages.patients.familyNameLabel")}
        value={familyName}
        onChange={setFamilyName}
        required
      />
      <Field
        label={t("pages.patients.phoneLabel")}
        value={phone}
        onChange={setPhone}
        type="tel"
        inputMode="tel"
      />
      <div className="flex items-center gap-2">
        <button type="submit" disabled={submitting} className={PRIMARY_BTN_CLASS}>
          {submitting ? t("pages.patients.saving") : t("pages.patients.save")}
        </button>
        <button
          type="button"
          onClick={onDone}
          className="inline-flex min-h-11 shrink-0 items-center justify-center
            rounded-lg border border-slate-300 bg-white px-4 text-sm
            font-medium text-slate-700 active:bg-slate-100"
        >
          {t("pages.patients.cancel")}
        </button>
      </div>
    </form>
  );
}

// -------------------------------------------------------------------------
// Champ texte réutilisable
// -------------------------------------------------------------------------

function Field({
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
