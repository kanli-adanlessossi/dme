// Interface CRUD Visites — local-first (tâche 17.3) — REQ 4.1, 4.2, 5.1 ; P9.
//
// Rôle
// ----
// Gestion des visites cliniques, **rattachées à un patient**. Comme pour les
// patients, toutes les lectures/écritures passent par la base locale RxDB
// (`lib/db.ts`) ; la page ne contacte JAMAIS l'API tenant (c'est `sync.ts` qui
// réplique l'outbox). L'écran fonctionne donc hors ligne (P9).
//
// Notes de visite = entité sensible (design « conflits sensibles », NFR-6) : un
// conflit de synchronisation sur une visite est signalé pour revue humaine via
// le `ConflictBanner` (P12), affiché en tête de l'aire tenant.
//
// Suppression logique (NFR-4) via `softDeleteVisit`.
//
// Mobile-first (NFR-9), français par défaut (NFR-10).

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Subscription } from "rxjs";

import {
  createVisit,
  getDb,
  softDeleteVisit,
  updateVisit,
  type PatientDoc,
  type VisitDoc,
} from "../lib/db";
import {
  getSessionState,
  subscribeSession,
  type SessionState,
} from "../lib/session";
import ConflictBanner from "../components/ConflictBanner";
import SyncStatusBar from "../components/SyncStatusBar";

const FIELD_CLASS =
  "min-h-11 w-full rounded-lg border border-slate-300 bg-white px-3 "
  + "text-base text-slate-800 outline-none focus:border-sky-500 "
  + "focus:ring-2 focus:ring-sky-200 disabled:opacity-60";

const PRIMARY_BTN_CLASS =
  "inline-flex min-h-11 w-full items-center justify-center rounded-lg "
  + "bg-sky-600 px-4 text-base font-medium text-white active:bg-sky-700 "
  + "disabled:opacity-60";

export default function Visits() {
  const { t } = useTranslation();
  const [session, setSession] = useState<SessionState>(() => getSessionState());

  useEffect(() => subscribeSession(setSession), []);

  const hasTenantAccess =
    session.authenticated &&
    session.unlocked &&
    session.user !== null &&
    session.user.tenant_id !== null;

  if (!hasTenantAccess) {
    return (
      <section className="flex flex-col gap-3">
        <h1 className="text-xl font-semibold md:text-2xl">
          {t("pages.visits.title")}
        </h1>
        <p
          className="rounded-lg border border-slate-200 bg-white p-4 text-sm
            text-slate-600"
          role="status"
        >
          {t("pages.visits.signInRequired")}
        </p>
      </section>
    );
  }

  return (
    <VisitsWorkspace
      tenantId={session.user!.tenant_id as string}
      userEmail={session.user!.email}
    />
  );
}

// -------------------------------------------------------------------------
// Espace de travail
// -------------------------------------------------------------------------

function VisitsWorkspace({
  tenantId,
  userEmail,
}: {
  tenantId: string;
  userEmail: string;
}) {
  const { t } = useTranslation();

  const [patients, setPatients] = useState<PatientDoc[]>([]);
  const [visits, setVisits] = useState<VisitDoc[]>([]);
  const [selectedPatient, setSelectedPatient] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);

  // Patients vivants (pour le sélecteur et l'affichage du nom).
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
      });
    })();
    return () => {
      active = false;
      sub?.unsubscribe();
    };
  }, []);

  // Visites vivantes du patient sélectionné (ou toutes si aucun filtre).
  useEffect(() => {
    let sub: Subscription | null = null;
    let active = true;
    setLoading(true);
    (async () => {
      const db = await getDb();
      if (!active) return;
      const selector: Record<string, unknown> = { deleted_at: null };
      if (selectedPatient) selector.patient_id = selectedPatient;
      const q = db.visit.find({
        selector,
        sort: [{ updated_at: "desc" }],
      });
      sub = q.$.subscribe((docs) => {
        setVisits(docs.map((d) => d.toJSON() as VisitDoc));
        setLoading(false);
      });
    })();
    return () => {
      active = false;
      sub?.unsubscribe();
    };
  }, [selectedPatient]);

  function patientName(patientId: string): string {
    const p = patients.find((x) => x.id === patientId);
    return p ? `${p.given_name} ${p.family_name}` : patientId;
  }

  async function onDelete(id: string) {
    await softDeleteVisit(id);
  }

  const canAdd = patients.length > 0;

  return (
    <section className="flex flex-col gap-4">
      <SyncStatusBar />
      <ConflictBanner />

      <div className="flex items-center justify-between gap-2">
        <h1 className="text-xl font-semibold md:text-2xl">
          {t("pages.visits.title")}
        </h1>
        {canAdd && (
          <button
            type="button"
            onClick={() => setShowForm((v) => !v)}
            className="inline-flex min-h-11 shrink-0 items-center justify-center
              rounded-lg border border-sky-300 bg-white px-4 text-sm font-medium
              text-sky-700 active:bg-sky-50"
          >
            {showForm
              ? t("pages.visits.closeForm")
              : t("pages.visits.addVisit")}
          </button>
        )}
      </div>

      {!canAdd && (
        <p
          className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2
            text-sm text-amber-800"
          role="status"
        >
          {t("pages.visits.noPatients")}
        </p>
      )}

      {/* Filtre par patient. */}
      {canAdd && (
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-slate-700">
            {t("pages.visits.filterLabel")}
          </span>
          <select
            value={selectedPatient}
            onChange={(e) => setSelectedPatient(e.target.value)}
            className={FIELD_CLASS}
          >
            <option value="">{t("pages.visits.allPatients")}</option>
            {patients.map((p) => (
              <option key={p.id} value={p.id}>
                {p.given_name} {p.family_name}
              </option>
            ))}
          </select>
        </label>
      )}

      {showForm && canAdd && (
        <VisitForm
          tenantId={tenantId}
          userEmail={userEmail}
          patients={patients}
          defaultPatient={selectedPatient}
          onCreated={() => setShowForm(false)}
        />
      )}

      {loading ? (
        <p className="py-8 text-center text-sm text-slate-500" role="status">
          {t("pages.visits.loading")}
        </p>
      ) : visits.length === 0 ? (
        <p
          className="rounded-lg border border-slate-200 bg-white p-4 text-center
            text-sm text-slate-500"
          role="status"
        >
          {t("pages.visits.empty")}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {visits.map((v) => (
            <VisitRow
              key={v.id}
              visit={v}
              patientName={patientName(v.patient_id)}
              onDelete={onDelete}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

// -------------------------------------------------------------------------
// Ligne de visite
// -------------------------------------------------------------------------

function VisitRow({
  visit,
  patientName,
  onDelete,
}: {
  visit: VisitDoc;
  patientName: string;
  onDelete: (id: string) => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);

  if (editing) {
    return (
      <li className="rounded-lg border border-slate-200 bg-white p-3">
        <VisitEditForm visit={visit} onDone={() => setEditing(false)} />
      </li>
    );
  }

  const when = visit.visited_at
    ? new Date(visit.visited_at).toLocaleDateString("fr-FR")
    : t("pages.visits.noDate");

  return (
    <li className="flex items-start gap-2 rounded-lg border border-slate-200
      bg-white p-3">
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-slate-800">{patientName}</p>
        <p className="text-xs text-slate-500">{when}</p>
        {visit.reason && (
          <p className="truncate text-sm text-slate-700">{visit.reason}</p>
        )}
        {visit.notes && (
          <p className="mt-0.5 line-clamp-2 text-xs text-slate-500">
            {visit.notes}
          </p>
        )}
      </div>
      <button
        type="button"
        onClick={() => setEditing(true)}
        aria-label={t("pages.visits.editAria")}
        className="inline-flex min-h-11 min-w-11 items-center justify-center
          rounded-lg border border-slate-300 bg-white px-3 text-sm text-sky-700
          active:bg-slate-100"
      >
        {t("pages.visits.edit")}
      </button>
      <button
        type="button"
        onClick={() => void onDelete(visit.id)}
        aria-label={t("pages.visits.deleteAria")}
        className="inline-flex min-h-11 min-w-11 items-center justify-center
          rounded-lg border border-red-300 bg-white px-3 text-sm text-red-700
          active:bg-red-50"
      >
        {t("pages.visits.delete")}
      </button>
    </li>
  );
}

// -------------------------------------------------------------------------
// Formulaire de création
// -------------------------------------------------------------------------

function VisitForm({
  tenantId,
  userEmail,
  patients,
  defaultPatient,
  onCreated,
}: {
  tenantId: string;
  userEmail: string;
  patients: PatientDoc[];
  defaultPatient: string;
  onCreated: () => void;
}) {
  const { t } = useTranslation();

  const [patientId, setPatientId] = useState(
    defaultPatient || patients[0]?.id || "",
  );
  const [visitedAt, setVisitedAt] = useState("");
  const [reason, setReason] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = patientId !== "";

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit || submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      await createVisit({
        tenant_id: tenantId,
        patient_id: patientId,
        visited_at: visitedAt ? new Date(visitedAt).toISOString() : null,
        reason: reason.trim() || null,
        notes: notes.trim() || null,
        created_by: userEmail,
      });
      onCreated();
    } catch {
      setError(t("pages.visits.saveError"));
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
        {t("pages.visits.newVisit")}
      </h2>

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium text-slate-700">
          {t("pages.visits.patientLabel")}
        </span>
        <select
          value={patientId}
          onChange={(e) => setPatientId(e.target.value)}
          required
          className={FIELD_CLASS}
        >
          {patients.map((p) => (
            <option key={p.id} value={p.id}>
              {p.given_name} {p.family_name}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium text-slate-700">
          {t("pages.visits.dateLabel")}
        </span>
        <input
          type="date"
          value={visitedAt}
          onChange={(e) => setVisitedAt(e.target.value)}
          className={FIELD_CLASS}
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium text-slate-700">
          {t("pages.visits.reasonLabel")}
        </span>
        <input
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className={FIELD_CLASS}
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium text-slate-700">
          {t("pages.visits.notesLabel")}
        </span>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={3}
          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2
            text-base text-slate-800 outline-none focus:border-sky-500
            focus:ring-2 focus:ring-sky-200"
        />
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
        {submitting ? t("pages.visits.saving") : t("pages.visits.save")}
      </button>
    </form>
  );
}

// -------------------------------------------------------------------------
// Formulaire d'édition
// -------------------------------------------------------------------------

function VisitEditForm({
  visit,
  onDone,
}: {
  visit: VisitDoc;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [reason, setReason] = useState(visit.reason ?? "");
  const [notes, setNotes] = useState(visit.notes ?? "");
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      await updateVisit(visit.id, {
        reason: reason.trim() || null,
        notes: notes.trim() || null,
      });
      onDone();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="flex flex-col gap-3" onSubmit={onSubmit} noValidate>
      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium text-slate-700">
          {t("pages.visits.reasonLabel")}
        </span>
        <input
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className={FIELD_CLASS}
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium text-slate-700">
          {t("pages.visits.notesLabel")}
        </span>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={3}
          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2
            text-base text-slate-800 outline-none focus:border-sky-500
            focus:ring-2 focus:ring-sky-200"
        />
      </label>
      <div className="flex items-center gap-2">
        <button type="submit" disabled={submitting} className={PRIMARY_BTN_CLASS}>
          {submitting ? t("pages.visits.saving") : t("pages.visits.save")}
        </button>
        <button
          type="button"
          onClick={onDone}
          className="inline-flex min-h-11 shrink-0 items-center justify-center
            rounded-lg border border-slate-300 bg-white px-4 text-sm
            font-medium text-slate-700 active:bg-slate-100"
        >
          {t("pages.visits.cancel")}
        </button>
      </div>
    </form>
  );
}
