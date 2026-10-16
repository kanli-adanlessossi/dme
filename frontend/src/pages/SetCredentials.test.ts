// Tests de la page `SetCredentials` (tâche 17.1) — REQ-3.2, P3.
//
// Comportement observable vérifié :
//   - validation locale : mot de passe < 12 caractères → message dédié, pas
//     d'appel réseau ; mots de passe non concordants → message, pas d'appel ;
//   - succès : appelle `setCredentials(token, password)` puis affiche l'écran de
//     confirmation (invite à se connecter) ;
//   - 410 (jeton expiré/déjà consommé) → message GÉNÉRIQUE unique (ne révèle pas
//     l'existence du compte/jeton, P3) ;
//   - 400 (jeton invalide) → même message générique ;
//   - hors ligne (fetch lève) → message « nécessite Internet » ;
//   - bouton retour câblé (`onBack`).
//
// Fichier `.ts` : rendu via react-dom + createElement ; `../lib/session` mocké.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import i18n from "../i18n";
import { ApiError } from "../lib/api";

const setCredentials = vi.fn<
  (token: string, password: string) => Promise<unknown>
>();
vi.mock("../lib/session", () => ({
  setCredentials: (token: string, password: string) =>
    setCredentials(token, password),
}));

import SetCredentials from "./SetCredentials";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function render(onBack?: () => void): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(
        I18nextProvider,
        { i18n },
        createElement(SetCredentials, { onBack }),
      ),
    );
    await Promise.resolve();
  });
}

function queryButton(text: string): HTMLButtonElement | undefined {
  return Array.from(container!.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").includes(text),
  );
}

function setInput(name: string, value: string) {
  const input = container!.querySelector<HTMLInputElement>(
    `input[name="${name}"]`,
  )!;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function submitForm() {
  const form = container!.querySelector("form")!;
  await act(async () => {
    form.dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true }),
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

const GOOD_PASSWORD = "motdepasse-robuste"; // >= 12 caractères.

beforeEach(() => {
  setCredentials.mockReset();
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  container = null;
  root = null;
});

describe("SetCredentials — validation locale (avant réseau)", () => {
  it("refuse un mot de passe trop court sans appeler le réseau", async () => {
    await render();
    setInput("setup_token", "st_abc");
    setInput("password", "court");
    setInput("confirm", "court");
    await submitForm();

    expect(setCredentials).not.toHaveBeenCalled();
    expect(container!.querySelector('[role="alert"]')!.textContent).toContain(
      "12 caractères",
    );
  });

  it("refuse des mots de passe non concordants sans appeler le réseau", async () => {
    await render();
    setInput("setup_token", "st_abc");
    setInput("password", GOOD_PASSWORD);
    setInput("confirm", GOOD_PASSWORD + "x");
    await submitForm();

    expect(setCredentials).not.toHaveBeenCalled();
    expect(container!.querySelector('[role="alert"]')!.textContent).toContain(
      "ne correspondent pas",
    );
  });
});

describe("SetCredentials — succès", () => {
  it("appelle setCredentials(token, password) et affiche la confirmation", async () => {
    setCredentials.mockResolvedValue({ email: "a@b.tg", status: "credentials_set" });
    await render();

    setInput("setup_token", "  st_abc  ");
    setInput("password", GOOD_PASSWORD);
    setInput("confirm", GOOD_PASSWORD);
    await submitForm();

    expect(setCredentials).toHaveBeenCalledWith("st_abc", GOOD_PASSWORD);
    const status = container!.querySelector('[role="status"]');
    expect(status).not.toBeNull();
    expect(status!.textContent).toContain("Mot de passe enregistré");
  });
});

describe("SetCredentials — jeton invalide/expiré (P3)", () => {
  it("affiche un message GÉNÉRIQUE sur 410 (ne révèle pas le compte)", async () => {
    setCredentials.mockRejectedValue(new ApiError(410, "gone"));
    await render();

    setInput("setup_token", "st_old");
    setInput("password", GOOD_PASSWORD);
    setInput("confirm", GOOD_PASSWORD);
    await submitForm();

    const text = container!.querySelector('[role="alert"]')!.textContent ?? "";
    expect(text).toContain("invalide, expiré ou déjà utilisé");
    expect(text.toLowerCase()).not.toContain("compte introuvable");
  });

  it("affiche le même message générique sur 400 (jeton invalide)", async () => {
    setCredentials.mockRejectedValue(new ApiError(400, "bad"));
    await render();

    setInput("setup_token", "st_bad");
    setInput("password", GOOD_PASSWORD);
    setInput("confirm", GOOD_PASSWORD);
    await submitForm();

    expect(container!.querySelector('[role="alert"]')!.textContent).toContain(
      "invalide, expiré ou déjà utilisé",
    );
  });

  it("affiche le message hors-ligne quand fetch lève (pas d'ApiError)", async () => {
    setCredentials.mockRejectedValue(new TypeError("Failed to fetch"));
    await render();

    setInput("setup_token", "st_x");
    setInput("password", GOOD_PASSWORD);
    setInput("confirm", GOOD_PASSWORD);
    await submitForm();

    expect(container!.querySelector('[role="alert"]')!.textContent).toContain(
      "connexion Internet",
    );
  });
});

describe("SetCredentials — navigation retour", () => {
  it("appelle onBack au clic sur « Retour à la connexion »", async () => {
    const onBack = vi.fn();
    await render(onBack);

    const back = queryButton("Retour à la connexion")!;
    expect(back).toBeTruthy();
    act(() => {
      back.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
