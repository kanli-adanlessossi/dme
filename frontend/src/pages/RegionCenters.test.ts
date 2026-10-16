// Tests de la page annuaire `RegionCenters` (tâche 16) — exigences 8.1, 8.2, 7.4.
//
// Comportement observable vérifié :
//   - au chargement, « Toutes les régions » agrège les centres des 5 régions
//     (exigence 8.1) ;
//   - le filtre par région ne charge que les centres de cette région
//     (exigence 8.2) ;
//   - une région sans centre affiche l'état vide, PAS une erreur (exigence 7.3) ;
//   - une erreur réseau affiche une alerte + « Réessayer » ;
//   - cliquer un centre ouvre son profil public (exigence 7.4) — on vérifie que
//     `fetchPublicCenter` est appelé avec l'id choisi.
//
// `fetchPublicRegions`, `fetchRegionCenters` et `fetchPublicCenter` sont mockés
// depuis `../lib/api` (aucun appel réseau réel).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import i18n from "../i18n";
import {
  ApiError,
  type PublicCenterDetail,
  type PublicCenterSummary,
  type PublicRegion,
  type RegionCode,
} from "../lib/api";

const fetchPublicRegions = vi.fn<(signal?: AbortSignal) => Promise<PublicRegion[]>>();
const fetchRegionCenters = vi.fn<
  (code: RegionCode, signal?: AbortSignal) => Promise<PublicCenterSummary[]>
>();
const fetchPublicCenter = vi.fn<
  (id: string, signal?: AbortSignal) => Promise<PublicCenterDetail>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchPublicRegions: (signal?: AbortSignal) => fetchPublicRegions(signal),
    fetchRegionCenters: (code: RegionCode, signal?: AbortSignal) =>
      fetchRegionCenters(code, signal),
    fetchPublicCenter: (id: string, signal?: AbortSignal) =>
      fetchPublicCenter(id, signal),
  };
});

import RegionCenters from "./RegionCenters";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const REGIONS: PublicRegion[] = [
  { code: "SAVANES", name_fr: "Savanes", center_count: 0 },
  { code: "KARA", name_fr: "Kara", center_count: 1 },
  { code: "CENTRALE", name_fr: "Centrale", center_count: 0 },
  { code: "PLATEAUX", name_fr: "Plateaux", center_count: 0 },
  { code: "MARITIME", name_fr: "Maritime", center_count: 1 },
];

const KARA_CENTER: PublicCenterSummary = {
  id: "018f2a00-0000-7000-8000-00000000000a",
  name: "Hôpital de Kara",
  region_code: "KARA",
  address: "Avenue centrale, Kara",
  location: null,
};
const MARITIME_CENTER: PublicCenterSummary = {
  id: "018f2a00-0000-7000-8000-00000000000b",
  name: "Centre de santé de Lomé",
  region_code: "MARITIME",
  address: null,
  location: null,
};

/** Réponse par région utilisée par le mock d'agrégation. */
function centersFor(code: RegionCode): PublicCenterSummary[] {
  if (code === "KARA") return [KARA_CENTER];
  if (code === "MARITIME") return [MARITIME_CENTER];
  return [];
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function render(): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(
        I18nextProvider,
        { i18n },
        createElement(RegionCenters),
      ),
    );
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** Change la valeur du <select> de région et laisse les effets se résoudre. */
async function selectRegion(code: string): Promise<void> {
  const select = container!.querySelector("select") as HTMLSelectElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLSelectElement.prototype,
      "value",
    )!.set!;
    setter.call(select, code);
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  fetchPublicRegions.mockReset();
  fetchRegionCenters.mockReset();
  fetchPublicCenter.mockReset();
  fetchPublicRegions.mockResolvedValue(REGIONS);
  fetchRegionCenters.mockImplementation(async (code) => centersFor(code));
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

describe("RegionCenters — agrégat « Toutes les régions » (exigence 8.1)", () => {
  it("charge et liste les centres de toutes les régions au départ", async () => {
    await render();

    // Les 5 régions sont interrogées pour l'agrégat.
    expect(fetchRegionCenters).toHaveBeenCalledTimes(5);
    const text = container!.textContent ?? "";
    expect(text).toContain("Hôpital de Kara");
    expect(text).toContain("Centre de santé de Lomé");
  });
});

describe("RegionCenters — filtre par région (exigence 8.2)", () => {
  it("ne charge que les centres de la région choisie", async () => {
    await render();
    fetchRegionCenters.mockClear();

    await selectRegion("KARA");

    // Seule KARA est rechargée après sélection.
    expect(fetchRegionCenters).toHaveBeenCalledWith("KARA", expect.anything());
    const kalls = fetchRegionCenters.mock.calls.map((c) => c[0]);
    expect(kalls.every((c) => c === "KARA")).toBe(true);
    const text = container!.textContent ?? "";
    expect(text).toContain("Hôpital de Kara");
    expect(text).not.toContain("Centre de santé de Lomé");
  });

  it("affiche l'état vide (pas une erreur) pour une région sans centre (exigence 7.3)", async () => {
    await render();
    await selectRegion("SAVANES");

    expect(container!.querySelector('[role="alert"]')).toBeNull();
    expect(container!.textContent ?? "").toContain(
      "Aucun centre médical publié dans cette région pour le moment.",
    );
  });
});

describe("RegionCenters — erreur réseau", () => {
  it("affiche une alerte + Réessayer quand le chargement des centres échoue", async () => {
    fetchRegionCenters.mockRejectedValue(new ApiError(503, "indisponible"));
    await render();

    expect(container!.querySelector('[role="alert"]')).not.toBeNull();
    const retry = Array.from(container!.querySelectorAll("button")).find((b) =>
      (b.textContent ?? "").includes("Réessayer"),
    );
    expect(retry).toBeTruthy();
  });
});

describe("RegionCenters — navigation vers le profil (exigence 7.4)", () => {
  it("ouvre le profil public du centre cliqué", async () => {
    fetchPublicCenter.mockResolvedValue({
      ...MARITIME_CENTER,
      phone: null,
      email: null,
      opening_hours: [],
      service_types: [],
    });
    await render();

    const centerButton = Array.from(
      container!.querySelectorAll("button"),
    ).find((b) => (b.textContent ?? "").includes("Hôpital de Kara"));
    expect(centerButton).toBeTruthy();

    await act(async () => {
      centerButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchPublicCenter).toHaveBeenCalledWith(
      KARA_CENTER.id,
      expect.anything(),
    );
  });
});
