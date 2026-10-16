// Tests de l'interface CRUD Visites `Visits` (tâche 17.3) — REQ 4.1, 5.1 ; P9.
//
// Comportement observable vérifié :
//   - LOCAL-FIRST (P9) : visites et patients proviennent de la base locale
//     (queries réactives RxDB mockées) ; les écritures passent par les helpers
//     `lib/db` (createVisit/softDeleteVisit) — JAMAIS par l'API tenant ;
//   - création d'une visite rattachée à un patient : `createVisit` reçoit le
//     patient_id, le motif et les notes (notes = donnée sensible) ;
//   - suppression logique : `softDeleteVisit` appelé ;
//   - sans patient enregistré → message invitant à créer un patient d'abord.
//
// Fichier `.ts` : rendu via react-dom + createElement, modules mockés.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";
import { BehaviorSubject } from "rxjs";

import i18n from "../i18n";
import type { PatientDoc, VisitDoc } from "../lib/db";
import type { SessionState } from "../lib/session";

// --- Base locale mockée : deux queries réactives (patient/visit) ------------
const patients$ = new BehaviorSubject<Array<{ toJSON: () => PatientDoc }>>([]);
const visits$ = new BehaviorSubject<Array<{ toJSON: () => VisitDoc }>>([]);

function emitPatients(docs: PatientDoc[]): void {
  patients$.next(docs.map((d) => ({ toJSON: () => d })));
}
function emitVisits(docs: VisitDoc[]): void {
  visits$.next(docs.map((d) => ({ toJSON: () => d })));
}

const createVisit = vi.fn<(...a: unknown[]) => Promise<unknown>>(() =>
  Promise.resolve({ id: "v-new" }),
);
const updateVisit = vi.fn<(...a: unknown[]) => Promise<unknown>>(() =>
  Promise.resolve({}),
);
const softDeleteVisit = vi.fn<(...a: unknown[]) => Promise<unknown>>(() =>
  Promise.resolve({}),
);

const fakeDb = {
  patient: { find: () => ({ $: patients$.asObservable() }) },
  visit: { find: () => ({ $: visits$.asObservable() }) },
};

vi.mock("../lib/db", () => ({
  getDb: () => Promise.resolve(fakeDb),
  createVisit: (...a: unknown[]) => createVisit(...a),
  updateVisit: (...a: unknown[]) => updateVisit(...a),
  softDeleteVisit: (...a: unknown[]) => softDeleteVisit(...a),
}));

let currentState: SessionState = {
  authenticated: true,
  user: { email: "doc@centre.tg", role: "clinician", tenant_id: "t1" },
  expiresAt: Date.now() + 3_600_000,
  unlocked: true,
};
vi.mock("../lib/session", () => ({
  getSessionState: () => currentState,
  subscribeSession: (l: (s: SessionState) => void) => {
    l(currentState);
    return () => {};
  },
}));

vi.mock("../components/SyncStatusBar", () => ({ default: () => null }));
vi.mock("../components/ConflictBanner", () => ({ default: () => null }));

import Visits from "./Visits";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PATIENT: PatientDoc = {
  id: "018f2a00-0000-7000-8000-0000000000p1",
  tenant_id: "t1",
  updated_at: "2024-02-01T00:00:00.000Z",
  version: 1,
  deleted_at: null,
  mrn: "MRN-001",
  given_name: "Koffi",
  family_name: "Mensah",
  dob: null,
  sex: null,
  phone: null,
  national_id: null,
  consent_to_store: true,
  created_by: null,
  created_at: "2024-02-01T00:00:00.000Z",
};

const VISIT: VisitDoc = {
  id: "018f2a00-0000-7000-8000-0000000000v1",
  tenant_id: "t1",
  updated_at: "2024-02-02T00:00:00.000Z",
  version: 1,
  deleted_at: null,
  patient_id: PATIENT.id,
  visited_at: "2024-02-02T09:00:00.000Z",
  reason: "Consultation générale",
  notes: "Tension normale.",
  created_by: null,
  created_at: "2024-02-02T00:00:00.000Z",
};

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function render(): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(I18nextProvider, { i18n }, createElement(Visits)),
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

function queryButton(text: string): HTMLButtonElement | undefined {
  return Array.from(container!.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").includes(text),
  );
}

async function clickButton(text: string) {
  const btn = queryButton(text)!;
  await act(async () => {
    btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function submitForm() {
  const form = container!.querySelector("form")!;
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  createVisit.mockClear();
  updateVisit.mockClear();
  softDeleteVisit.mockClear();
  patients$.next([]);
  visits$.next([]);
  currentState = {
    authenticated: true,
    user: { email: "doc@centre.tg", role: "clinician", tenant_id: "t1" },
    expiresAt: Date.now() + 3_600_000,
    unlocked: true,
  };
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  container = null;
  root = null;
});

describe("Visits — accès + prérequis patient", () => {
  it("exige une session déverrouillée", async () => {
    currentState = {
      authenticated: false,
      user: null,
      expiresAt: null,
      unlocked: false,
    };
    await render();
    expect(container!.textContent ?? "").toContain("Connectez-vous");
  });

  it("invite à créer un patient quand aucun n'existe", async () => {
    await render();
    expect(container!.textContent ?? "").toContain("Ajoutez d'abord un patient");
    expect(queryButton("Nouvelle visite")).toBeUndefined();
  });
});

describe("Visits — lecture local-first (P9)", () => {
  it("affiche les visites issues de la base locale réactive", async () => {
    emitPatients([PATIENT]);
    emitVisits([VISIT]);
    await render();
    const text = container!.textContent ?? "";
    expect(text).toContain("Koffi Mensah");
    expect(text).toContain("Consultation générale");
  });
});

describe("Visits — création local-first (jamais via l'API)", () => {
  it("crée une visite via la base locale avec patient_id/motif/notes", async () => {
    emitPatients([PATIENT]);
    await render();
    await clickButton("Nouvelle visite");

    // Renseigne motif + notes (notes = donnée sensible).
    const textInput = container!.querySelector<HTMLInputElement>(
      'input[type="text"]',
    )!;
    const textarea = container!.querySelector<HTMLTextAreaElement>("textarea")!;
    const setInputValue = (
      el: HTMLInputElement | HTMLTextAreaElement,
      proto: typeof HTMLInputElement | typeof HTMLTextAreaElement,
      val: string,
    ) => {
      const setter = Object.getOwnPropertyDescriptor(
        proto.prototype,
        "value",
      )!.set!;
      act(() => {
        setter.call(el, val);
        el.dispatchEvent(new Event("input", { bubbles: true }));
      });
    };
    setInputValue(textInput, window.HTMLInputElement, "Suivi tension");
    setInputValue(textarea, window.HTMLTextAreaElement, "Note confidentielle");

    await submitForm();

    expect(createVisit).toHaveBeenCalledTimes(1);
    const arg = createVisit.mock.calls[0][0] as Record<string, unknown>;
    expect(arg.patient_id).toBe(PATIENT.id);
    expect(arg.reason).toBe("Suivi tension");
    expect(arg.notes).toBe("Note confidentielle");
    expect(arg.tenant_id).toBe("t1");
  });
});

describe("Visits — suppression logique", () => {
  it("appelle softDeleteVisit (suppression locale non destructive)", async () => {
    emitPatients([PATIENT]);
    emitVisits([VISIT]);
    await render();
    await clickButton("Supprimer");
    expect(softDeleteVisit).toHaveBeenCalledWith(VISIT.id);
  });
});
