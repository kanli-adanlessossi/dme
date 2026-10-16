// Tests de la feuille inférieure `CenterSheet` (vérification tâche 15.4) —
// exigences 7.2, 7.3, 7.4 et NFR-9 (mobile-first, cibles tactiles >=44px,
// rôles/étiquettes accessibles).
//
// Les tests 15.1–15.3 couvraient le CHARGEMENT des centres (`centers.test.ts`,
// `fetchRegionCenters`) et l'aiguillage WebGL (`MapHome.test.ts`), mais aucun
// ne RENDAIT la feuille elle-même. Cette suite comble ce vide, en vérifiant le
// comportement observable du composant :
//   - état vide (exigence 7.3) : message d'état vide, PAS une erreur ;
//   - liste de centres (exigence 7.2) : un bouton par centre ;
//   - sélection d'un centre (exigence 7.4) : `onSelectCenter(id)` est appelé ;
//   - erreur réseau : message + bouton « Réessayer » (jamais une page cassée) ;
//   - accessibilité / mobile (NFR-9) : rôle `dialog` + `aria-modal`, bouton de
//     fermeture avec cible tactile >=44px (classes `h-11 w-11`), entrées de
//     centre à hauteur tactile (`min-h-14`).
//
// Fichier en `.ts` (glob vitest `src/**/*.{test,spec}.ts`) : on rend via
// `react-dom` avec `React.createElement` (pas de JSX, pas de @testing-library),
// comme les autres tests du dépôt. Le réseau est piloté par `fetchRegionCenters`
// mocké depuis `../lib/api` ; aucun appel réel (tests hermétiques).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import i18n from "../i18n";
import { ApiError, type PublicCenterSummary } from "../lib/api";

// `fetchRegionCenters` piloté par test : on contrôle la réponse (liste, vide,
// erreur) sans toucher le réseau. `ApiError` et les types sont réexportés tels
// quels pour que `CenterSheet` garde son comportement 404→liste vide.
const fetchRegionCenters = vi.fn<
  (code: string, signal?: AbortSignal) => Promise<PublicCenterSummary[]>
>();
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchRegionCenters: (code: string, signal?: AbortSignal) =>
      fetchRegionCenters(code, signal),
  };
});

import CenterSheet from "./CenterSheet";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CENTERS: PublicCenterSummary[] = [
  {
    id: "018f2a00-0000-7000-8000-000000000001",
    name: "Centre de santé de Lomé",
    region_code: "MARITIME",
    address: "12 Rue du Commerce, Lomé",
    location: { longitude: 1.2255, latitude: 6.1319 },
  },
  {
    id: "018f2a00-0000-7000-8000-000000000002",
    name: "Clinique du Golfe",
    region_code: "MARITIME",
    address: null,
    location: null,
  },
];

let container: HTMLDivElement | null = null;
let root: Root | null = null;

/** Rend `CenterSheet` et laisse l'effet de chargement se résoudre (microtasks). */
async function renderSheet(props: {
  region: "MARITIME" | "SAVANES" | null;
  onClose?: () => void;
  onSelectCenter?: (id: string) => void;
}): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(
        I18nextProvider,
        { i18n },
        createElement(CenterSheet, {
          region: props.region,
          onClose: props.onClose ?? (() => {}),
          onSelectCenter: props.onSelectCenter ?? (() => {}),
        }),
      ),
    );
    // Laisse résoudre la promesse de `fetchRegionCenters` et le re-rendu.
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  fetchRegionCenters.mockReset();
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  container = null;
  root = null;
  vi.restoreAllMocks();
});

describe("CenterSheet — fermeture / région nulle", () => {
  it("ne rend rien quand aucune région n'est sélectionnée", async () => {
    await renderSheet({ region: null });
    expect(container!.querySelector('[role="dialog"]')).toBeNull();
    expect(fetchRegionCenters).not.toHaveBeenCalled();
  });
});

describe("CenterSheet — accessibilité et mobile-first (NFR-9)", () => {
  it("expose un dialog modal avec étiquette et un bouton de fermeture à cible >=44px", async () => {
    fetchRegionCenters.mockResolvedValue(CENTERS);
    await renderSheet({ region: "MARITIME" });

    const dialog = container!.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog!.getAttribute("aria-modal")).toBe("true");
    expect(dialog!.getAttribute("aria-label")).toContain("Maritime");

    // Bouton de fermeture dédié (en-tête) : classes de cible tactile 44px.
    const closeBtn = Array.from(container!.querySelectorAll("button")).find(
      (b) => b.className.includes("h-11") && b.className.includes("w-11"),
    );
    expect(closeBtn, "bouton de fermeture 44px introuvable").toBeTruthy();
    expect(closeBtn!.getAttribute("aria-label")).toBeTruthy();
  });

  it("rend chaque centre comme un bouton à hauteur tactile (min-h-14)", async () => {
    fetchRegionCenters.mockResolvedValue(CENTERS);
    await renderSheet({ region: "MARITIME" });

    const centerButtons = Array.from(
      container!.querySelectorAll("button"),
    ).filter((b) => b.className.includes("min-h-14"));
    expect(centerButtons).toHaveLength(CENTERS.length);
    for (const b of centerButtons) {
      // Chaque entrée est étiquetée pour les lecteurs d'écran (exigence 7.4).
      expect(b.getAttribute("aria-label")).toBeTruthy();
    }
  });
});

describe("CenterSheet — liste des centres (exigence 7.2) et navigation (7.4)", () => {
  it("liste les centres de la région sélectionnée", async () => {
    fetchRegionCenters.mockResolvedValue(CENTERS);
    await renderSheet({ region: "MARITIME" });

    expect(fetchRegionCenters).toHaveBeenCalledWith(
      "MARITIME",
      expect.anything(),
    );
    const text = container!.textContent ?? "";
    expect(text).toContain("Centre de santé de Lomé");
    expect(text).toContain("Clinique du Golfe");
    // Centre sans adresse → libellé de repli, pas une case vide.
    expect(text).toContain("Adresse non renseignée");
  });

  it("appelle onSelectCenter avec l'id du centre choisi (exigence 7.4)", async () => {
    fetchRegionCenters.mockResolvedValue(CENTERS);
    const onSelectCenter = vi.fn<(id: string) => void>();
    await renderSheet({ region: "MARITIME", onSelectCenter });

    const centerButton = Array.from(
      container!.querySelectorAll("button"),
    ).find((b) => b.className.includes("min-h-14"));
    expect(centerButton).toBeTruthy();
    act(() => {
      centerButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onSelectCenter).toHaveBeenCalledTimes(1);
    expect(onSelectCenter).toHaveBeenCalledWith(CENTERS[0].id);
  });
});

describe("CenterSheet — état vide (exigence 7.3)", () => {
  it("affiche le message d'état vide, pas une erreur, pour une région sans centre", async () => {
    fetchRegionCenters.mockResolvedValue([]);
    await renderSheet({ region: "SAVANES" });

    expect(container!.querySelector('[role="alert"]')).toBeNull();
    const text = container!.textContent ?? "";
    expect(text).toContain(
      "Aucun centre médical publié dans cette région pour le moment.",
    );
  });

  it("traite un 404 comme une liste vide (jamais une erreur)", async () => {
    fetchRegionCenters.mockRejectedValue(new ApiError(404, "introuvable"));
    await renderSheet({ region: "SAVANES" });

    expect(container!.querySelector('[role="alert"]')).toBeNull();
    expect(container!.textContent ?? "").toContain(
      "Aucun centre médical publié dans cette région pour le moment.",
    );
  });
});

describe("CenterSheet — erreur réseau (jamais une page cassée)", () => {
  it("affiche un message d'erreur et un bouton Réessayer sur échec non-404", async () => {
    fetchRegionCenters.mockRejectedValue(new ApiError(503, "indisponible"));
    await renderSheet({ region: "MARITIME" });

    const alert = container!.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    const retry = Array.from(container!.querySelectorAll("button")).find((b) =>
      (b.textContent ?? "").includes("Réessayer"),
    );
    expect(retry, "bouton Réessayer introuvable").toBeTruthy();
    // Cible tactile du bouton Réessayer (min-h-11 ≈ 44px, NFR-9).
    expect(retry!.className).toContain("min-h-11");
  });
});
