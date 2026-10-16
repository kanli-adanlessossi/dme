// Tests de la détection WebGL (tâche 15.1, seam pour 15.3) — exigence 7.5.
//
// `isWebGLAvailable` conditionne le repli `RegionFallbackList` (15.3). On vérifie
// qu'elle renvoie `true` quand un contexte WebGL est fourni, `false` quand il est
// absent, et qu'elle ne lève jamais (même si `getContext` lève).

import { afterEach, describe, expect, it, vi } from "vitest";

import { isWebGLAvailable } from "./webgl";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("isWebGLAvailable", () => {
  it("renvoie true quand un contexte WebGL est disponible", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
      ((type: string) =>
        type === "webgl2" ? ({} as unknown) : null) as never,
    );
    expect(isWebGLAvailable()).toBe(true);
  });

  it("renvoie false quand aucun contexte n'est disponible", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
      (() => null) as never,
    );
    expect(isWebGLAvailable()).toBe(false);
  });

  it("ne lève pas si getContext lève (navigateur restreint)", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
      (() => {
        throw new Error("WebGL désactivé");
      }) as never,
    );
    expect(() => isWebGLAvailable()).not.toThrow();
    expect(isWebGLAvailable()).toBe(false);
  });
});
