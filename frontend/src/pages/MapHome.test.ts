// Tests d'intégration du repli WebGL dans `MapHome` (tâche 15.3) — exigence 7.5.
//
// On vérifie que `MapHome` rend le repli statique (`RegionFallbackList`) quand
// `useWebGLAvailable()` renvoie `false`, la carte (`TogoMap`) quand `true`, et
// ni l'un ni l'autre tant que la détection est indéterminée (`null`) — pas de
// flash de contenu erroné.
//
// `TogoMap` (MapLibre/WebGL/réseau) et `CenterSheet` (réseau) sont mockés : ce
// test porte sur l'aiguillage de `MapHome`, pas sur leurs internes. Le hook de
// détection WebGL est mocké pour piloter les trois états déterministes.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import i18n from "../i18n";

// Valeur de retour pilotable du hook de détection WebGL.
let webglValue: boolean | null = null;
vi.mock("../lib/webgl", () => ({
  useWebGLAvailable: () => webglValue,
  isWebGLAvailable: () => webglValue === true,
}));

// Stubs légers : la carte et la feuille sont testées ailleurs ; ici on ne veut
// que des marqueurs identifiables dans le DOM, sans MapLibre ni réseau.
vi.mock("../components/TogoMap", () => ({
  default: () => createElement("div", { "data-testid": "togo-map" }, "carte"),
}));
vi.mock("../components/CenterSheet", () => ({
  default: () => null,
}));

import MapHome from "./MapHome";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function renderMapHome(): void {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root!.render(
      createElement(I18nextProvider, { i18n }, createElement(MapHome)),
    );
  });
}

beforeEach(() => {
  webglValue = null;
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

describe("MapHome — aiguillage WebGL (exigence 7.5)", () => {
  it("rend le repli statique quand WebGL est indisponible", () => {
    webglValue = false;
    renderMapHome();

    // Le repli expose 5 boutons de région et aucune carte.
    expect(container!.querySelector('[data-testid="togo-map"]')).toBeNull();
    const buttons = container!.querySelectorAll("button");
    expect(buttons.length).toBe(5);
  });

  it("rend la carte quand WebGL est disponible", () => {
    webglValue = true;
    renderMapHome();

    expect(
      container!.querySelector('[data-testid="togo-map"]'),
    ).not.toBeNull();
    // Pas de boutons de région de repli dans ce chemin.
    expect(container!.querySelectorAll("button").length).toBe(0);
  });

  it("ne rend ni carte ni repli pendant la détection (null)", () => {
    webglValue = null;
    renderMapHome();

    expect(container!.querySelector('[data-testid="togo-map"]')).toBeNull();
    expect(container!.querySelectorAll("button").length).toBe(0);
  });
});
