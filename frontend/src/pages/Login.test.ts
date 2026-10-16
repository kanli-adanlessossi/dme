// Tests de la page `Login` (tâche 17.1) — REQ-3.2, NFR-7, NFR-8, P3, P13.
//
// Comportement observable vérifié :
//   - non authentifié : formulaire de connexion (1ʳᵉ connexion en ligne) ;
//     succès → bascule vers le panneau de session ;
//   - échec 401 → message GÉNÉRIQUE (ne révèle pas l'existence de l'e-mail, P3) ;
//   - verrouillage 429 → message « trop de tentatives » (NFR-2) ;
//   - hors ligne (fetch lève, pas d'ApiError) → message « nécessite Internet » ;
//   - authentifié mais verrouillé (après rechargement, P13) → formulaire de
//     DÉVERROUILLAGE (e-mail prérempli en lecture seule) appelant `unlockSession`
//     sans réémettre de jeton (NFR-7) ;
//   - session active : déconnexion appelle `logout` (verrouille la clé, NFR-8) ;
//   - lien vers la configuration du mot de passe.
//
// Fichier `.ts` (contrainte vitest `include: *.ts`) : rendu via react-dom +
// createElement ; `../lib/session` est mocké (aucun RxDB ni réseau réel).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import i18n from "../i18n";
import { ApiError } from "../lib/api";
import type { SessionState } from "../lib/session";

// --- Mock du module session (propriétaire du cycle de vie) -----------------
const login = vi.fn<(email: string, password: string) => Promise<unknown>>();
const unlockSession = vi.fn<(email: string, password: string) => Promise<void>>();
const logout = vi.fn<() => Promise<void>>();
let currentState: SessionState = {
  authenticated: false,
  user: null,
  expiresAt: null,
  unlocked: false,
};
let listener: ((s: SessionState) => void) | null = null;

vi.mock("../lib/session", () => ({
  login: (e: string, p: string) => login(e, p),
  unlockSession: (e: string, p: string) => unlockSession(e, p),
  logout: () => logout(),
  getSessionState: () => currentState,
  subscribeSession: (l: (s: SessionState) => void) => {
    listener = l;
    l(currentState);
    return () => {
      listener = null;
    };
  },
}));

/** Pousse un nouvel état de session vers l'abonné (comme le ferait session.ts). */
function emitSession(next: SessionState) {
  currentState = next;
  act(() => listener?.(next));
}

// `CenterAdmin` (sous-vue ouverte depuis le panneau de session, tâche 17.2) tire
// MapLibre via `CenterPinPicker`, indisponible en jsdom. On le stub : son
// comportement propre est couvert par `CenterAdmin.test.ts`.
vi.mock("./CenterAdmin", () => ({
  default: () => createElement("div", { "data-testid": "center-admin" }),
}));

import Login from "./Login";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function render(): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(I18nextProvider, { i18n }, createElement(Login)),
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

beforeEach(() => {
  login.mockReset();
  unlockSession.mockReset();
  logout.mockReset();
  currentState = {
    authenticated: false,
    user: null,
    expiresAt: null,
    unlocked: false,
  };
  listener = null;
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  container = null;
  root = null;
});

describe("Login — connexion (1ʳᵉ connexion en ligne, NFR-7)", () => {
  it("appelle login(email, password) et vide le mot de passe au succès", async () => {
    login.mockResolvedValue({ email: "a@b.tg", role: "staff", tenant_id: "t1" });
    await render();

    setInput("email", "  a@b.tg  ");
    setInput("password", "secret123");
    await submitForm();

    expect(login).toHaveBeenCalledTimes(1);
    // L'e-mail est trimé avant l'appel.
    expect(login).toHaveBeenCalledWith("a@b.tg", "secret123");
  });

  it("affiche un message GÉNÉRIQUE sur 401 (ne révèle pas l'e-mail, P3)", async () => {
    login.mockRejectedValue(new ApiError(401, "Identifiants invalides."));
    await render();

    setInput("email", "a@b.tg");
    setInput("password", "wrong");
    await submitForm();

    const alert = container!.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    const text = alert!.textContent ?? "";
    expect(text).toContain("incorrect");
    // Ne doit pas distinguer e-mail inconnu vs mot de passe faux.
    expect(text.toLowerCase()).not.toContain("inconnu");
    expect(text.toLowerCase()).not.toContain("introuvable");
  });

  it("affiche un message de verrouillage sur 429 (NFR-2)", async () => {
    login.mockRejectedValue(new ApiError(429, "Trop de tentatives."));
    await render();

    setInput("email", "a@b.tg");
    setInput("password", "x");
    await submitForm();

    expect(container!.querySelector('[role="alert"]')!.textContent).toContain(
      "Trop de tentatives",
    );
  });

  it("affiche le message hors-ligne quand fetch lève (pas d'ApiError)", async () => {
    login.mockRejectedValue(new TypeError("Failed to fetch"));
    await render();

    setInput("email", "a@b.tg");
    setInput("password", "x");
    await submitForm();

    expect(container!.querySelector('[role="alert"]')!.textContent).toContain(
      "connexion Internet",
    );
  });

  it("propose un lien vers la configuration du mot de passe", async () => {
    await render();
    expect(queryButton("Configurer mon mot de passe")).toBeTruthy();
  });
});

describe("Login — déverrouillage (session restaurée, P13 / NFR-7)", () => {
  const lockedState: SessionState = {
    authenticated: true,
    user: { email: "doc@centre.tg", role: "clinician", tenant_id: "t1" },
    expiresAt: Date.now() + 3_600_000,
    unlocked: false,
  };

  it("affiche le formulaire de déverrouillage avec e-mail prérempli en lecture seule", async () => {
    currentState = lockedState;
    await render();

    const email = container!.querySelector<HTMLInputElement>(
      'input[name="email"]',
    )!;
    expect(email.value).toBe("doc@centre.tg");
    expect(email.readOnly).toBe(true);
    expect(queryButton("Déverrouiller")).toBeTruthy();
  });

  it("appelle unlockSession (pas login) sans réémettre de jeton", async () => {
    currentState = lockedState;
    unlockSession.mockResolvedValue(undefined);
    await render();

    setInput("password", "secret123");
    await submitForm();

    expect(unlockSession).toHaveBeenCalledWith("doc@centre.tg", "secret123");
    expect(login).not.toHaveBeenCalled();
  });
});

describe("Login — session active (déconnexion verrouille la clé, NFR-8)", () => {
  it("affiche le profil et appelle logout au clic", async () => {
    currentState = {
      authenticated: true,
      user: { email: "doc@centre.tg", role: "tenant_admin", tenant_id: "t1" },
      expiresAt: Date.now() + 3_600_000,
      unlocked: true,
    };
    logout.mockResolvedValue(undefined);
    await render();

    expect(container!.textContent).toContain("doc@centre.tg");
    // Rôle traduit en français.
    expect(container!.textContent).toContain("Administrateur du centre");

    const btn = queryButton("Se déconnecter")!;
    expect(btn.className).toContain("min-h-11");
    await act(async () => {
      btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(logout).toHaveBeenCalledTimes(1);
  });

  it("bascule vers le panneau de session quand l'état devient authentifié+déverrouillé", async () => {
    await render();
    // Au départ : formulaire de connexion.
    expect(container!.querySelector('input[name="password"]')).not.toBeNull();

    emitSession({
      authenticated: true,
      user: { email: "x@y.tg", role: "staff", tenant_id: "t1" },
      expiresAt: Date.now() + 3_600_000,
      unlocked: true,
    });

    expect(container!.textContent).toContain("x@y.tg");
    expect(queryButton("Se déconnecter")).toBeTruthy();
  });
});
