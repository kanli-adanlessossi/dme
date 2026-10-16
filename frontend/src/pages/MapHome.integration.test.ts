// Test d'intégration bout-en-bout du flux de sélection (vérification tâche
// 15.4) — exigences 7.2, 7.3, 7.4, 7.5 et NFR-9.
//
// `MapHome.test.ts` (15.3) vérifie l'AIGUILLAGE WebGL mais mocke `TogoMap` et
// `CenterSheet` : il ne prouve pas que sélectionner une région OUVRE réellement
// la feuille, ni que choisir un centre NAVIGUE vers le profil. Cette suite
// comble ce vide en rendant le vrai `RegionFallbackList`, la vraie `CenterSheet`
// et le vrai `CenterProfile` ensemble.
//
// On pilote WebGL = `false` pour emprunter le chemin de repli (exigence 7.5),
// qui partage EXACTEMENT le même seam `onRegionSelect`/`setSelected` que la
// carte MapLibre — on vérifie ainsi la cliquabilité sans dépendre du rendu
// WebGL (impossible sous jsdom). Le réseau (`fetchRegionCenters`) est mocké.
//
// Flux vérifié :
//   1. repli rendu avec 5 régions (exigence 7.5) ;
//   2. clic sur une région → `CenterSheet` s'ouvre (role=dialog) et liste les
//      centres de cette région (exigence 7.2) ;
//   3. clic sur un centre → navigation vers `CenterProfile` (exigence 7.4),
//      la feuille se referme ;
//   4. une région vide ouvre la feuille sur l'état vide (exigence 7.3).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import i18n from "../i18n";
import type { PublicCenterSummary } from "../lib/api";

// WebGL indisponible → chemin de repli statique (même seam que la carte).
vi.mock("../lib/webgl", () => ({
  useWebGLAvailable: () => false,
  isWebGLAvailable: () => false,
}));

// `TogoMap` importe `maplibre-gl`, qui plante au chargement sous jsdom
// (`window.URL.createObjectURL` absent). Comme ce test emprunte le chemin de
// repli (WebGL=false), `TogoMap` n'est jamais rendu ; on le stub pour éviter
// l'import de MapLibre, exactement comme `MapHome.test.ts`.
vi.mock("../components/TogoMap", () => ({
  default: () => createElement("div", { "data-testid": "togo-map" }, "carte"),
}));

// Réseau piloté : `fetchRegionCenters` renvoie la liste d'une région et
// `fetchPublicCenter` le profil public complet (chargé par `CenterProfile`,
// tâche 16). Les deux sont mockés — aucun appel réseau réel.
const fetchRegionCenters = vi.fn<
  (code: string, signal?: AbortSignal) => Promise<PublicCenterSummary[]>
>();
const fetchPublicCenter = vi.fn<
  (
    id: string,
    signal?: AbortSignal,
  ) => Promise<import("../lib/api").PublicCenterDetail>
>();
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchRegionCenters: (code: string, signal?: AbortSignal) =>
      fetchRegionCenters(code, signal),
    fetchPublicCenter: (id: string, signal?: AbortSignal) =>
      fetchPublicCenter(id, signal),
  };
});

import MapHome from "./MapHome";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const MARITIME_CENTERS: PublicCenterSummary[] = [
  {
    id: "018f2a00-0000-7000-8000-000000000001",
    name: "Centre de santé de Lomé",
    region_code: "MARITIME",
    address: "12 Rue du Commerce, Lomé",
    location: { longitude: 1.2255, latitude: 6.1319 },
  },
];

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function renderHome(): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(I18nextProvider, { i18n }, createElement(MapHome)),
    );
    await Promise.resolve();
  });
}

/** Trouve un bouton de région de repli par son libellé français. */
function regionButton(label: string): HTMLButtonElement {
  const btn = Array.from(container!.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").includes(label),
  );
  if (!btn) throw new Error(`bouton de région introuvable : ${label}`);
  return btn as HTMLButtonElement;
}

async function clickAndSettle(el: Element): Promise<void> {
  await act(async () => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  fetchRegionCenters.mockReset();
  fetchPublicCenter.mockReset();
  // Profil renvoyé pour le centre sélectionné (projection publique, P4).
  fetchPublicCenter.mockResolvedValue({
    ...MARITIME_CENTERS[0],
    phone: "+22890000000",
    email: null,
    opening_hours: [],
    service_types: [],
  });
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

describe("MapHome — flux sélection → feuille → profil (exigences 7.2/7.4/7.5)", () => {
  it("rend le repli à 5 régions quand WebGL est indisponible (exigence 7.5)", async () => {
    fetchRegionCenters.mockResolvedValue([]);
    await renderHome();

    // Pas de feuille ouverte au départ ; le repli expose 5 régions.
    expect(container!.querySelector('[role="dialog"]')).toBeNull();
    expect(container!.querySelectorAll("button").length).toBe(5);
  });

  it("ouvre la feuille et liste les centres au clic sur une région (exigence 7.2)", async () => {
    fetchRegionCenters.mockResolvedValue(MARITIME_CENTERS);
    await renderHome();

    await clickAndSettle(regionButton("Maritime"));

    expect(fetchRegionCenters).toHaveBeenCalledWith(
      "MARITIME",
      expect.anything(),
    );
    const dialog = container!.querySelector('[role="dialog"]');
    expect(dialog, "la feuille ne s'est pas ouverte").not.toBeNull();
    expect(container!.textContent ?? "").toContain("Centre de santé de Lomé");
  });

  it("navigue vers le profil au clic sur un centre, puis ferme la feuille (exigence 7.4)", async () => {
    fetchRegionCenters.mockResolvedValue(MARITIME_CENTERS);
    await renderHome();

    await clickAndSettle(regionButton("Maritime"));

    // Entrée de centre dans la feuille (bouton à hauteur tactile).
    const centerButton = Array.from(
      container!.querySelectorAll('[role="dialog"] button'),
    ).find((b) => b.className.includes("min-h-14"));
    expect(centerButton, "entrée de centre introuvable").toBeTruthy();

    await clickAndSettle(centerButton!);

    // CenterProfile (tâche 16) charge et affiche le profil public du centre
    // choisi ; la feuille est refermée. `fetchPublicCenter` est appelé avec
    // l'id sélectionné (exigence 7.4).
    expect(container!.querySelector('[role="dialog"]')).toBeNull();
    expect(fetchPublicCenter).toHaveBeenCalledWith(
      MARITIME_CENTERS[0].id,
      expect.anything(),
    );
    const text = container!.textContent ?? "";
    expect(text).toContain(MARITIME_CENTERS[0].name);
  });

  it("ouvre la feuille sur l'état vide pour une région sans centre (exigence 7.3)", async () => {
    fetchRegionCenters.mockResolvedValue([]);
    await renderHome();

    await clickAndSettle(regionButton("Savanes"));

    expect(fetchRegionCenters).toHaveBeenCalledWith(
      "SAVANES",
      expect.anything(),
    );
    const dialog = container!.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog!.querySelector('[role="alert"]')).toBeNull();
    expect(container!.textContent ?? "").toContain(
      "Aucun centre médical publié dans cette région pour le moment.",
    );
  });
});
