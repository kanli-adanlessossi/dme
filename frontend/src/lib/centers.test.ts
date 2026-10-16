// Tests du chargement des centres d'une région (tâche 15.2) — exigences 7.2/7.3.
//
// `fetchRegionCenters` est le point d'entrée de la feuille inférieure
// (`CenterSheet`) vers l'API publique. Garanties vérifiées :
//   - l'URL interrogée suit le contrat `/api/public/regions/{code}/centers`
//     (tâche 12) et encode le code région ;
//   - une réponse OK renvoie la liste des résumés publics (projection sûre) ;
//   - une région vide renvoie `[]` (exigence 7.3), jamais une erreur ;
//   - une réponse non-OK lève une `ApiError` typée (gérée par `CenterSheet`).

import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  fetchRegionCenters,
  regionCentersUrl,
  REGION_LABELS,
  type PublicCenterSummary,
} from "./api";

/** Résumé public minimal conforme au contrat `PublicCenterSummary`. */
const SAMPLE: PublicCenterSummary[] = [
  {
    id: "018f2a00-0000-7000-8000-000000000001",
    name: "Centre de santé de Lomé",
    region_code: "MARITIME",
    address: "12 Rue du Commerce, Lomé",
    location: { longitude: 1.2255, latitude: 6.1319 },
  },
];

describe("regionCentersUrl", () => {
  it("suit le contrat public et encode le code région", () => {
    const url = regionCentersUrl("PLATEAUX");
    expect(url.endsWith("/api/public/regions/PLATEAUX/centers")).toBe(true);
  });
});

describe("REGION_LABELS", () => {
  it("fournit un nom français pour chacun des 5 codes", () => {
    expect(Object.keys(REGION_LABELS)).toHaveLength(5);
    expect(REGION_LABELS.MARITIME).toBe("Maritime");
    expect(REGION_LABELS.SAVANES).toBe("Savanes");
  });
});

describe("fetchRegionCenters", () => {
  it("interroge l'URL de la région et renvoie les centres", async () => {
    const fetchMock = vi.fn((_input: unknown, _init?: unknown) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => SAMPLE,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const centers = await fetchRegionCenters("MARITIME");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const calledUrl = fetchMock.mock.calls[0][0] as string;
    expect(calledUrl).toBe(regionCentersUrl("MARITIME"));
    expect(centers).toHaveLength(1);
    expect(centers[0].name).toBe("Centre de santé de Lomé");
    expect(centers[0].region_code).toBe("MARITIME");
  });

  it("renvoie une liste vide pour une région sans centre (exigence 7.3)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => [] })),
    );

    const centers = await fetchRegionCenters("SAVANES");

    expect(centers).toEqual([]);
  });

  it("lève une ApiError sur réponse non-OK", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}) })),
    );

    await expect(fetchRegionCenters("KARA")).rejects.toBeInstanceOf(ApiError);
    await expect(fetchRegionCenters("KARA")).rejects.toMatchObject({
      status: 503,
    });
  });
});
