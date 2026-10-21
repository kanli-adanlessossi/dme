// Tests de l'éditeur `CenterAdmin` (tâche 17.2) — REQ-6.1, 3.2, P7.
//
// Comportement observable vérifié :
//   - session absente/verrouillée → message « connectez-vous », pas de formulaire ;
//   - tenant_admin : profil chargé, champs éditables, enregistrement appelle
//     `updateCenterProfile` avec le patch attendu (nom, contact, localisation) ;
//   - rôle cadré au tenant non-admin (clinician) → consultation seule : pas de
//     bouton « Enregistrer », champs désactivés (exigence 6.1) ;
//   - contrainte Togo (P7) : une épingle hors Togo signalée par le picker affiche
//     un message et ne doit pas être enregistrée ; un 422 serveur affiche le
//     message « hors Togo » dédié ;
//   - 404 (pas de centre) → message dédié ; erreur réseau → alerte + Réessayer.
//
// Fichier `.ts` (contrainte vitest `include: *.ts`) : rendu via react-dom +
// createElement. `../lib/api`, `../lib/session` et le composant carte
// `../components/CenterPinPicker` (MapLibre/WebGL, indisponible en jsdom) sont
// mockés. Le picker mocké expose des boutons de test déclenchant onChange/onReject.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import i18n from "../i18n";
import { ApiError, type CenterProfile } from "../lib/api";
import type { SessionState } from "../lib/session";

// --- Mock de l'API profil du centre ---------------------------------------
const fetchCenterProfile =
  vi.fn<(...a: unknown[]) => Promise<CenterProfile>>();
const updateCenterProfile =
  vi.fn<(...a: unknown[]) => Promise<CenterProfile>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchCenterProfile: (...a: unknown[]) => fetchCenterProfile(...a),
    updateCenterProfile: (...a: unknown[]) => updateCenterProfile(...a),
  };
});

// --- Mock de la session (fournit le rôle + le token provider) --------------
let currentState: SessionState = {
  authenticated: true,
  user: { email: "admin@centre.tg", role: "tenant_admin", tenant_id: "t1" },
  expiresAt: Date.now() + 3_600_000,
  unlocked: true,
};
vi.mock("../lib/session", () => ({
  getSessionState: () => currentState,
  subscribeSession: (l: (s: SessionState) => void) => {
    l(currentState);
    return () => {};
  },
  getToken: () => "test-token",
}));

// --- Mock du sélecteur d'épingle (évite MapLibre/WebGL en jsdom) ------------
// Le picker mocké rend deux boutons : « pin-inside » (place un point valide au
// Togo) et « pin-outside » (déclenche onReject, comme un clic hors frontière).
vi.mock("../components/CenterPinPicker", () => ({
  default: (props: {
    onChange: (p: { lng: number; lat: number }) => void;
    onReject?: () => void;
  }) =>
    createElement("div", { "data-testid": "pin-picker" }, [
      createElement(
        "button",
        {
          key: "in",
          type: "button",
          onClick: () => props.onChange({ lng: 1.3, lat: 6.2 }),
        },
        "pin-inside",
      ),
      createElement(
        "button",
        {
          key: "out",
          type: "button",
          onClick: () => props.onReject?.(),
        },
        "pin-outside",
      ),
    ]),
}));

import CenterAdmin from "./CenterAdmin";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PROFILE: CenterProfile = {
  id: "018f2a00-0000-7000-8000-000000000001",
  tenant_id: "t1",
  name: "Centre de santé de Lomé",
  region_code: "MARITIME",
  address: "12 Rue du Commerce, Lomé",
  location: { longitude: 1.2255, latitude: 6.1319 },
  phone: "+22890000000",
  email: "contact@centre-lome.tg",
  opening_hours: [{ day: "lundi", open: "08:00", close: "17:00" }],
  service_types: ["Maternité"],
  is_public: true,
};

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function render(): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(I18nextProvider, { i18n }, createElement(CenterAdmin)),
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

/**
 * Pilote la connectivité navigateur : (dé)positionne `navigator.onLine` à la
 * source canonique lue par `useOnline()` et diffuse l'événement `window`
 * correspondant (`online`/`offline`) que le hook écoute. Enveloppé dans `act`
 * car le listener du hook déclenche un `setState` (re-rendu React).
 */
function setOnline(value: boolean) {
  Object.defineProperty(window.navigator, "onLine", {
    value,
    configurable: true,
    writable: true,
  });
  act(() => {
    window.dispatchEvent(new Event(value ? "online" : "offline"));
  });
}

function setInput(name: string, value: string) {
  // Repère le champ par son libellé via la structure label>span+input ; ici on
  // cible par type/valeur. Plus simple : on prend tous les inputs texte.
  const inputs = Array.from(
    container!.querySelectorAll<HTMLInputElement>("input"),
  );
  const input = inputs.find((i) => i.value === name)!;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
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
    form.dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true }),
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  fetchCenterProfile.mockReset();
  updateCenterProfile.mockReset();
  currentState = {
    authenticated: true,
    user: { email: "admin@centre.tg", role: "tenant_admin", tenant_id: "t1" },
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

describe("CenterAdmin — accès (REQ-3.2)", () => {
  it("exige une session déverrouillée (pas de formulaire sinon)", async () => {
    currentState = {
      authenticated: false,
      user: null,
      expiresAt: null,
      unlocked: false,
    };
    await render();
    expect(container!.querySelector("form")).toBeNull();
    expect(container!.textContent ?? "").toContain("Connectez-vous");
    expect(fetchCenterProfile).not.toHaveBeenCalled();
  });
});

describe("CenterAdmin — tenant_admin édite (REQ-6.1)", () => {
  it("charge le profil et pré-remplit les champs", async () => {
    fetchCenterProfile.mockResolvedValue(PROFILE);
    await render();

    expect(fetchCenterProfile).toHaveBeenCalledTimes(1);
    const text = container!.textContent ?? "";
    expect(text).toContain("Maritime"); // région (lecture seule)
    const values = Array.from(
      container!.querySelectorAll<HTMLInputElement>("input"),
    ).map((i) => i.value);
    expect(values).toContain("Centre de santé de Lomé");
    expect(values).toContain("12 Rue du Commerce, Lomé");
    expect(values).toContain("+22890000000");
  });

  it("enregistre via updateCenterProfile avec le patch attendu", async () => {
    fetchCenterProfile.mockResolvedValue(PROFILE);
    updateCenterProfile.mockResolvedValue({
      ...PROFILE,
      name: "Centre rénové",
    });
    await render();

    setInput("Centre de santé de Lomé", "Centre rénové");
    await submitForm();

    expect(updateCenterProfile).toHaveBeenCalledTimes(1);
    const patch = updateCenterProfile.mock.calls[0][0] as Record<
      string,
      unknown
    >;
    expect(patch.name).toBe("Centre rénové");
    // La localisation initiale (dans le Togo) est transmise en {longitude,latitude}.
    expect(patch.location).toEqual({ longitude: 1.2255, latitude: 6.1319 });
    // Confirmation de succès affichée.
    expect(container!.textContent ?? "").toContain("Profil enregistré");
  });

  it("place une épingle valide (dans le Togo) et l'inclut au patch (P7)", async () => {
    fetchCenterProfile.mockResolvedValue({ ...PROFILE, location: null as never });
    updateCenterProfile.mockResolvedValue(PROFILE);
    await render();

    await clickButton("pin-inside");
    await submitForm();

    const patch = updateCenterProfile.mock.calls[0][0] as Record<
      string,
      unknown
    >;
    expect(patch.location).toEqual({ longitude: 1.3, latitude: 6.2 });
  });
});

describe("CenterAdmin — contrainte Togo (P7)", () => {
  it("signale un clic hors Togo rejeté par le picker, sans enregistrer", async () => {
    fetchCenterProfile.mockResolvedValue(PROFILE);
    await render();

    await clickButton("pin-outside");

    // Message « hors Togo » affiché ; aucune écriture déclenchée par le rejet.
    const alert = container!.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    expect((alert!.textContent ?? "").toLowerCase()).toContain("togo");
    expect(updateCenterProfile).not.toHaveBeenCalled();
  });

  it("affiche le message hors-Togo quand le serveur renvoie 422", async () => {
    fetchCenterProfile.mockResolvedValue(PROFILE);
    updateCenterProfile.mockRejectedValue(new ApiError(422, "hors Togo"));
    await render();

    await submitForm();

    const alert = container!.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    expect((alert!.textContent ?? "").toLowerCase()).toContain("togo");
  });
});

describe("CenterAdmin — rôle non-admin (lecture seule, REQ-6.1)", () => {
  it("n'affiche pas de bouton Enregistrer pour un clinicien", async () => {
    currentState = {
      authenticated: true,
      user: { email: "doc@centre.tg", role: "clinician", tenant_id: "t1" },
      expiresAt: Date.now() + 3_600_000,
      unlocked: true,
    };
    fetchCenterProfile.mockResolvedValue(PROFILE);
    await render();

    expect(queryButton("Enregistrer")).toBeUndefined();
    expect(container!.textContent ?? "").toContain("Seul l'administrateur");
    // Les champs sont désactivés via le `fieldset disabled` (le navigateur
    // propage `disabled` à tous les contrôles descendants ; jsdom reflète
    // l'attribut sur le fieldset lui-même).
    const fieldset = container!.querySelector("fieldset");
    expect(fieldset).not.toBeNull();
    expect(fieldset!.hasAttribute("disabled")).toBe(true);
    // L'éditeur de services ne propose pas de champ d'ajout en lecture seule.
    expect(queryButton("Ajouter")).toBeUndefined();
  });
});

// -------------------------------------------------------------------------
// UX a priori hors-ligne (#4) — REQ-4 (critères 1, 2) ; NFR-5.
//
// Technique M1 : `useOnline()` lit `navigator.onLine` + écoute les événements
// `window` online/offline ; `../lib/sync` n'a donc PAS besoin d'être mocké. On
// pilote la connectivité via `setOnline()` (défini plus haut).
// -------------------------------------------------------------------------
describe("CenterAdmin — signalement hors-ligne (#4, REQ-4.1/4.2, NFR-5)", () => {
  it("hors-ligne : affiche la bannière (role=status) et désactive « Enregistrer »", async () => {
    fetchCenterProfile.mockResolvedValue(PROFILE);
    await render();

    // En ligne au départ : le bouton est actif, pas de bannière hors-ligne.
    const saveBefore = queryButton("Enregistrer")!;
    expect(saveBefore.disabled).toBe(false);

    // Bascule hors-ligne (événement navigateur).
    setOnline(false);

    // Bannière informative présente : role="status" + message français dédié.
    const statuses = Array.from(
      container!.querySelectorAll('[role="status"]'),
    );
    const banner = statuses.find((el) =>
      (el.textContent ?? "").includes("hors ligne"),
    );
    expect(banner).toBeTruthy();
    expect(banner!.textContent ?? "").toContain(
      "La modification du profil de votre centre",
    );

    // Le bouton « Enregistrer » est désormais désactivé (ADR-1 : pas d'action
    // active-qui-échoue).
    const saveAfter = queryButton("Enregistrer")!;
    expect(saveAfter.disabled).toBe(true);
  });

  it("chargement déjà hors-ligne (L1) : message hors-ligne, pas « erreur de chargement »", async () => {
    // Le poste est hors-ligne AVANT le montage : le GET du profil échoue.
    setOnline(false);
    fetchCenterProfile.mockRejectedValue(new ApiError(503, "indisponible"));
    await render();

    const text = container!.textContent ?? "";
    // Message hors-ligne dédié (distinguer absence de réseau d'une erreur
    // serveur générique).
    expect(text).toContain("Une connexion Internet est nécessaire");
    // PAS le message d'erreur de chargement générique.
    expect(text).not.toContain("Impossible de charger le profil");
    // Présenté comme information (role="status"), pas comme alerte serveur.
    const status = container!.querySelector('[role="status"]');
    expect(status).not.toBeNull();
    expect(container!.querySelector('[role="alert"]')).toBeNull();
  });

  it("retour en ligne : réactive « Enregistrer » et l'enregistrement fonctionne", async () => {
    fetchCenterProfile.mockResolvedValue(PROFILE);
    updateCenterProfile.mockResolvedValue({ ...PROFILE, name: "Centre rénové" });
    await render();

    // Hors-ligne : bouton désactivé.
    setOnline(false);
    expect(queryButton("Enregistrer")!.disabled).toBe(true);

    // Retour en ligne : bouton réactivé.
    setOnline(true);
    expect(queryButton("Enregistrer")!.disabled).toBe(false);

    // L'enregistrement fonctionne normalement une fois reconnecté.
    setInput("Centre de santé de Lomé", "Centre rénové");
    await submitForm();

    expect(updateCenterProfile).toHaveBeenCalledTimes(1);
    const patch = updateCenterProfile.mock.calls[0][0] as Record<
      string,
      unknown
    >;
    expect(patch.name).toBe("Centre rénové");
    expect(container!.textContent ?? "").toContain("Profil enregistré");
  });

  it("filet a posteriori : fetch échoue pendant l'édition en ligne → errors.offline", async () => {
    // En ligne (navigator.onLine=true) mais le réseau tombe au moment du PATCH :
    // `updateCenterProfile` lève une erreur non-ApiError (fetch rejeté), mappée
    // sur le message `errors.offline` (filet de sécurité a posteriori).
    fetchCenterProfile.mockResolvedValue(PROFILE);
    updateCenterProfile.mockRejectedValue(new TypeError("Failed to fetch"));
    await render();

    await submitForm();

    const alert = container!.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    expect(alert!.textContent ?? "").toContain(
      "L'enregistrement nécessite une connexion Internet",
    );
  });
});

describe("CenterAdmin — profil absent / erreur", () => {
  it("affiche un message dédié sur 404", async () => {
    fetchCenterProfile.mockRejectedValue(new ApiError(404, "introuvable"));
    await render();
    expect(container!.querySelector("form")).toBeNull();
    expect(container!.textContent ?? "").toContain("Aucun profil de centre");
  });

  it("affiche une alerte + Réessayer sur erreur réseau", async () => {
    fetchCenterProfile.mockRejectedValue(new ApiError(503, "indisponible"));
    await render();
    expect(container!.querySelector('[role="alert"]')).not.toBeNull();
    expect(queryButton("Réessayer")).toBeTruthy();
  });
});
