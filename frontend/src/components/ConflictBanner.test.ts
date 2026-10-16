// Tests du bandeau de conflits `ConflictBanner` (tâche 17.3) — NFR-6, P12.
//
// Comportement observable vérifié :
//   - aucun conflit → rien n'est rendu (bandeau absent) ;
//   - un conflit sensible (notes de visite) est mis en avant pour revue humaine
//     (titre « sensible », étiquette « Sensible ») — P12 : conflit signalé, non
//     écrasé silencieusement ;
//   - l'acquittement appelle `engine.clearConflicts()` et le bandeau disparaît
//     quand l'observable repasse à vide ;
//   - les détails listent l'entité perdante sans planter.
//
// Fichier `.ts` : rendu via react-dom + createElement. `../lib/sync` est mocké
// pour fournir un moteur contrôlable (observable de conflits + spy clearConflicts).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";
import { BehaviorSubject } from "rxjs";

import i18n from "../i18n";
import type { SyncConflict } from "../lib/api";

// --- Mock du moteur de sync : observable de conflits + spy clearConflicts ----
const conflicts$ = new BehaviorSubject<SyncConflict[]>([]);
const clearConflicts = vi.fn(() => conflicts$.next([]));

vi.mock("../lib/sync", () => ({
  getSyncEngine: () => ({
    conflicts: conflicts$.asObservable(),
    clearConflicts,
  }),
}));

import ConflictBanner from "./ConflictBanner";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SENSITIVE_CONFLICT: SyncConflict = {
  entity: "visit",
  entity_id: "018f2a00-0000-7000-8000-00000000a001",
  loser_payload: { reason: "Suivi tension", notes: "Note perdante" },
  winner_version: 4,
  sensitive: true,
};

const PLAIN_CONFLICT: SyncConflict = {
  entity: "patient",
  entity_id: "018f2a00-0000-7000-8000-00000000b001",
  loser_payload: { given_name: "Koffi", family_name: "Mensah", mrn: "MRN-1" },
  winner_version: 2,
  sensitive: false,
};

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function render(): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(I18nextProvider, { i18n }, createElement(ConflictBanner)),
    );
    await Promise.resolve();
  });
}

async function setConflicts(list: SyncConflict[]): Promise<void> {
  await act(async () => {
    conflicts$.next(list);
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
  });
}

beforeEach(() => {
  clearConflicts.mockClear();
  conflicts$.next([]);
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  container = null;
  root = null;
});

describe("ConflictBanner — rien à revoir", () => {
  it("ne rend rien quand il n'y a aucun conflit", async () => {
    await render();
    expect(container!.querySelector('[role="alert"]')).toBeNull();
    expect(container!.textContent).toBe("");
  });
});

describe("ConflictBanner — conflit sensible mis en avant (P12)", () => {
  it("affiche un titre sensible et une étiquette pour une note de visite", async () => {
    await render();
    await setConflicts([SENSITIVE_CONFLICT]);

    const alert = container!.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    const text = (alert!.textContent ?? "").toLowerCase();
    expect(text).toContain("sensible");

    // Les détails exposent l'entité perdante (motif de visite) pour la revue.
    await clickButton("Voir les détails");
    expect((container!.textContent ?? "")).toContain("Suivi tension");
    expect((container!.textContent ?? "")).toContain("Sensible");
  });
});

describe("ConflictBanner — acquittement (clearConflicts)", () => {
  it("appelle clearConflicts et disparaît quand la file se vide", async () => {
    await render();
    await setConflicts([SENSITIVE_CONFLICT, PLAIN_CONFLICT]);
    expect(container!.querySelector('[role="alert"]')).not.toBeNull();

    await clickButton("J'ai revu");

    expect(clearConflicts).toHaveBeenCalledTimes(1);
    // Le mock de clearConflicts vide l'observable → le bandeau disparaît.
    expect(container!.querySelector('[role="alert"]')).toBeNull();
  });
});
