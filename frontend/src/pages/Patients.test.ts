// Tests de l'interface CRUD Patients `Patients` (tâche 17.3) — REQ 4.1, 5.1, 5.6 ; P9.
//
// Comportement observable vérifié :
//   - LOCAL-FIRST (P9) : la liste provient de la base locale (query réactive
//     RxDB mockée) et les écritures passent par les helpers `lib/db`
//     (createPatient/softDeletePatient) — JAMAIS par l'API tenant. Le setup
//     global fait échouer tout `fetch` : aucun appel réseau n'est toléré ;
//   - consentement (REQ-5.6) : le bouton Enregistrer reste désactivé tant que la
//     case de consentement n'est pas cochée ; une fois cochée + champs requis
//     remplis, `createPatient` est appelé avec `consent_to_store: true` ;
//   - suppression logique : le bouton Supprimer appelle `softDeletePatient` ;
//   - session absente → message « connectez-vous », aucune lecture de la base.
//
// Fichier `.ts` : rendu via react-dom + createElement. `../lib/db`,
// `../lib/session` et les composants d'état de sync (SyncStatusBar/ConflictBanner,
// qui touchent le moteur de sync) sont mockés.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";
import { BehaviorSubject } from "rxjs";

import i18n from "../i18n";
import type { PatientDoc } from "../lib/db";
import type { SessionState } from "../lib/session";

// --- Base locale mockée : query réactive + helpers d'écriture espionnés ------
const patients$ = new BehaviorSubject<Array<{ toJSON: () => PatientDoc }>>([]);

function emitPatients(docs: PatientDoc[]): void {
  patients$.next(docs.map((d) => ({ toJSON: () => d })));
}

const createPatient = vi.fn<(...a: unknown[]) => Promise<unknown>>(() =>
  Promise.resolve({ id: "new" }),
);
const updatePatient = vi.fn<(...a: unknown[]) => Promise<unknown>>(() =>
  Promise.resolve({}),
);
const softDeletePatient = vi.fn<(...a: unknown[]) => Promise<unknown>>(() =>
  Promise.resolve({}),
);

const fakeDb = {
  patient: {
    find: () => ({ $: patients$.asObservable() }),
  },
};

vi.mock("../lib/db", () => ({
  getDb: () => Promise.resolve(fakeDb),
  createPatient: (...a: unknown[]) => createPatient(...a),
  updatePatient: (...a: unknown[]) => updatePatient(...a),
  softDeletePatient: (...a: unknown[]) => softDeletePatient(...a),
}));

// --- Session mockée (tenant déverrouillé) ----------------------------------
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

// --- Composants d'état de sync neutralisés (ils touchent le moteur de sync) --
vi.mock("../components/SyncStatusBar", () => ({ default: () => null }));
vi.mock("../components/ConflictBanner", () => ({ default: () => null }));

import Patients from "./Patients";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PATIENT: PatientDoc = {
  id: "018f2a00-0000-7000-8000-000000000001",
  tenant_id: "t1",
  updated_at: "2024-02-01T00:00:00.000Z",
  version: 1,
  deleted_at: null,
  mrn: "MRN-001",
  given_name: "Koffi",
  family_name: "Mensah",
  dob: null,
  sex: null,
  phone: "+22890000000",
  national_id: null,
  consent_to_store: true,
  created_by: null,
  created_at: "2024-02-01T00:00:00.000Z",
};

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function render(): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(I18nextProvider, { i18n }, createElement(Patients)),
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

function setInput(current: string, value: string) {
  const inputs = Array.from(
    container!.querySelectorAll<HTMLInputElement>("input"),
  );
  const input = inputs.find((i) => i.value === current)!;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function checkConsent() {
  const checkbox = container!.querySelector<HTMLInputElement>(
    'input[type="checkbox"]',
  )!;
  act(() => {
    checkbox.dispatchEvent(new MouseEvent("click", { bubbles: true }));
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
  createPatient.mockClear();
  updatePatient.mockClear();
  softDeletePatient.mockClear();
  patients$.next([]);
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

describe("Patients — accès", () => {
  it("exige une session déverrouillée (pas de liste sinon)", async () => {
    currentState = {
      authenticated: false,
      user: null,
      expiresAt: null,
      unlocked: false,
    };
    await render();
    expect(container!.textContent ?? "").toContain("Connectez-vous");
  });
});

describe("Patients — lecture local-first (P9)", () => {
  it("affiche les patients issus de la base locale réactive", async () => {
    emitPatients([PATIENT]);
    await render();
    expect(container!.textContent ?? "").toContain("Koffi Mensah");
    expect(container!.textContent ?? "").toContain("MRN-001");
  });

  it("met à jour la liste quand la base émet (réactivité locale)", async () => {
    await render();
    expect(container!.textContent ?? "").toContain("Aucun patient");
    await act(async () => {
      emitPatients([PATIENT]);
      await Promise.resolve();
    });
    expect(container!.textContent ?? "").toContain("Koffi Mensah");
  });
});

describe("Patients — création avec consentement (REQ-5.6)", () => {
  it("désactive l'enregistrement tant que le consentement n'est pas donné", async () => {
    await render();
    await clickButton("Nouveau patient");

    setInput("", "MRN-9"); // premier champ texte vide (MRN)
    // Même avec des champs remplis, sans consentement le bouton est désactivé.
    const saveBtn = Array.from(container!.querySelectorAll("button")).find(
      (b) => b.getAttribute("type") === "submit",
    )!;
    expect(saveBtn.disabled).toBe(true);
    expect(createPatient).not.toHaveBeenCalled();
  });

  it("crée via la base locale avec consent_to_store=true (jamais via l'API)", async () => {
    await render();
    await clickButton("Nouveau patient");

    // Remplit MRN, Prénom, Nom (dans l'ordre des champs texte du formulaire).
    const textInputs = Array.from(
      container!.querySelectorAll<HTMLInputElement>('input[type="text"]'),
    );
    // Les champs sont rendus via `type` par défaut (text) sauf téléphone (tel).
    // On repère par placeholder/label indirectement : ici on remplit par ordre.
    const setByIndex = (idx: number, val: string) => {
      const input = textInputs[idx];
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value",
      )!.set!;
      act(() => {
        setter.call(input, val);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    };
    setByIndex(0, "MRN-9");
    setByIndex(1, "Ama");
    setByIndex(2, "Dossou");

    checkConsent();
    await submitForm();

    expect(createPatient).toHaveBeenCalledTimes(1);
    const arg = createPatient.mock.calls[0][0] as Record<string, unknown>;
    expect(arg.consent_to_store).toBe(true);
    expect(arg.mrn).toBe("MRN-9");
    expect(arg.given_name).toBe("Ama");
    expect(arg.family_name).toBe("Dossou");
    expect(arg.tenant_id).toBe("t1");
  });
});

describe("Patients — suppression logique", () => {
  it("appelle softDeletePatient (suppression locale, non destructive)", async () => {
    emitPatients([PATIENT]);
    await render();
    await clickButton("Supprimer");
    expect(softDeletePatient).toHaveBeenCalledWith(PATIENT.id);
  });
});
