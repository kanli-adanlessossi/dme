// Tests du repli statique `RegionFallbackList` (tâche 15.3) — exigence 7.5.
//
// Quand WebGL est indisponible, `RegionFallbackList` remplace la carte. On
// vérifie, sans @testing-library (non installé), en rendant via `react-dom` :
//   - les 5 régions du Togo sont rendues comme boutons (data `FALLBACK_REGIONS`
//     + libellés français de `REGION_LABELS`) ;
//   - sélectionner une région déclenche `onRegionSelect` avec le bon code —
//     c'est le MÊME seam que `TogoMap`, donc le même flux côté `MapHome` ;
//   - l'ordre géographique nord→sud est stable (Savanes → Maritime).
//
// Le fichier est en `.ts` (glob vitest `src/**/*.{test,spec}.ts`) : on utilise
// `React.createElement` plutôt que du JSX, et un provider i18n pour fournir `t`.

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import i18n from "../i18n";
import { REGION_LABELS, type RegionCode } from "../lib/api";
import RegionFallbackList, { FALLBACK_REGIONS } from "./RegionFallbackList";

// React 18 attend ce drapeau pour que `act` prenne effet dans les tests.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function render(ui: Parameters<Root["render"]>[0]): void {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root!.render(createElement(I18nextProvider, { i18n }, ui));
  });
}

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  container = null;
  root = null;
  vi.restoreAllMocks();
});

describe("FALLBACK_REGIONS", () => {
  it("liste les 5 régions dans l'ordre géographique nord→sud", () => {
    expect(FALLBACK_REGIONS).toEqual([
      "SAVANES",
      "KARA",
      "CENTRALE",
      "PLATEAUX",
      "MARITIME",
    ]);
  });
});

describe("RegionFallbackList", () => {
  it("rend un bouton par région avec son libellé français", () => {
    render(
      createElement(RegionFallbackList, { onRegionSelect: () => {} }),
    );

    const buttons = Array.from(
      container!.querySelectorAll("button"),
    );
    expect(buttons).toHaveLength(5);

    const labels = buttons.map((b) => b.textContent ?? "");
    for (const code of FALLBACK_REGIONS) {
      expect(labels.some((text) => text.includes(REGION_LABELS[code]))).toBe(
        true,
      );
    }
  });

  it("appelle onRegionSelect avec le code de la région cliquée", () => {
    const onRegionSelect = vi.fn<(code: RegionCode) => void>();
    render(createElement(RegionFallbackList, { onRegionSelect }));

    // La 1re région affichée est Savanes (ordre nord→sud).
    const first = container!.querySelector("button");
    expect(first).not.toBeNull();
    act(() => {
      first!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onRegionSelect).toHaveBeenCalledTimes(1);
    expect(onRegionSelect).toHaveBeenCalledWith("SAVANES");
  });

  it("marque la région sélectionnée via aria-pressed", () => {
    render(
      createElement(RegionFallbackList, {
        onRegionSelect: () => {},
        selectedRegion: "KARA",
      }),
    );

    const pressed = Array.from(
      container!.querySelectorAll('button[aria-pressed="true"]'),
    );
    expect(pressed).toHaveLength(1);
    expect(pressed[0].textContent).toContain(REGION_LABELS.KARA);
  });
});
