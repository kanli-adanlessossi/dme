// Tests du chargement du GeoJSON des régions (tâche 15.1) — exigences 7.1/7.6.
//
// `fetchTogoRegionsGeoJSON` est le point d'entrée de la carte vers l'API
// publique. Deux garanties comptent :
//   - l'URL interrogée se termine par `/geo/togo-regions.geojson`, afin que la
//     règle de cache du service worker (`map-geojson`, tâche 14.2) s'applique et
//     que la carte reste disponible hors ligne (NFR-11) ;
//   - une réponse non-OK lève une `ApiError` typée (gérée par `TogoMap`).

import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  fetchTogoRegionsGeoJSON,
  TOGO_REGIONS_GEOJSON_URL,
} from "./api";

/** FeatureCollection minimal conforme au contrat public. */
const SAMPLE = {
  type: "FeatureCollection" as const,
  features: [
    {
      type: "Feature" as const,
      properties: { code: "MARITIME", name_fr: "Maritime" },
      geometry: { type: "Point", coordinates: [1.2, 6.2] },
    },
  ],
};

describe("fetchTogoRegionsGeoJSON", () => {
  it("interroge l'URL mise en cache par le service worker", () => {
    // La route SW `map-geojson` matche les URLs finissant par ce suffixe.
    expect(TOGO_REGIONS_GEOJSON_URL.endsWith("/geo/togo-regions.geojson")).toBe(
      true,
    );
  });

  it("renvoie la FeatureCollection quand la réponse est OK", async () => {
    const fetchMock = vi.fn((_input: unknown, _init?: unknown) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => SAMPLE,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const data = await fetchTogoRegionsGeoJSON();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const calledUrl = fetchMock.mock.calls[0][0] as string;
    expect(calledUrl).toBe(TOGO_REGIONS_GEOJSON_URL);
    expect(data.type).toBe("FeatureCollection");
    expect(data.features[0].properties.code).toBe("MARITIME");
  });

  it("lève une ApiError sur réponse non-OK", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({}),
      })),
    );

    await expect(fetchTogoRegionsGeoJSON()).rejects.toBeInstanceOf(ApiError);
    await expect(fetchTogoRegionsGeoJSON()).rejects.toMatchObject({
      status: 503,
    });
  });
});
