// Tests des lectures publiques régions/profil (tâche 16) — exigences 8.1, 7.4.
//
// `fetchPublicRegions` et `fetchPublicCenter` alimentent les pages annuaire
// (`RegionCenters`) et profil (`CenterProfile`). Garanties vérifiées :
//   - l'URL interrogée suit le contrat public (tâche 12) et encode l'id ;
//   - une réponse OK renvoie les données projetées (champs non sensibles, P4) ;
//   - un 404 sur le profil (centre inconnu / tenant désactivé, P8) lève une
//     `ApiError` de statut 404 — consommée par la page comme « introuvable » ;
//   - toute réponse non-OK lève une `ApiError` typée.

import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  fetchPublicCenter,
  fetchPublicRegions,
  publicCenterUrl,
  publicRegionsUrl,
  type PublicCenterDetail,
  type PublicRegion,
} from "./api";

const REGIONS: PublicRegion[] = [
  { code: "SAVANES", name_fr: "Savanes", center_count: 0 },
  { code: "MARITIME", name_fr: "Maritime", center_count: 3 },
];

const CENTER: PublicCenterDetail = {
  id: "018f2a00-0000-7000-8000-000000000001",
  name: "Centre de santé de Lomé",
  region_code: "MARITIME",
  address: "12 Rue du Commerce, Lomé",
  location: { longitude: 1.2255, latitude: 6.1319 },
  phone: "+22890000000",
  email: "contact@centre-lome.tg",
  opening_hours: [{ day: "lundi", open: "08:00", close: "17:00" }],
  service_types: ["maternité", "pharmacie"],
};

describe("publicRegionsUrl / publicCenterUrl", () => {
  it("suivent le contrat public et encodent l'id", () => {
    expect(publicRegionsUrl().endsWith("/api/public/regions")).toBe(true);
    expect(
      publicCenterUrl("a b/c").endsWith("/api/public/centers/a%20b%2Fc"),
    ).toBe(true);
  });
});

describe("fetchPublicRegions", () => {
  it("interroge /api/public/regions et renvoie les régions", async () => {
    const fetchMock = vi.fn((_input: unknown, _init?: unknown) =>
      Promise.resolve({ ok: true, status: 200, json: async () => REGIONS }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const regions = await fetchPublicRegions();

    expect(fetchMock.mock.calls[0][0]).toBe(publicRegionsUrl());
    expect(regions).toHaveLength(2);
    expect(regions[1].center_count).toBe(3);
  });

  it("lève une ApiError sur réponse non-OK", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}) })),
    );
    await expect(fetchPublicRegions()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("fetchPublicCenter", () => {
  it("interroge /api/public/centers/{id} et renvoie le profil complet", async () => {
    const fetchMock = vi.fn((_input: unknown, _init?: unknown) =>
      Promise.resolve({ ok: true, status: 200, json: async () => CENTER }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const center = await fetchPublicCenter(CENTER.id);

    expect(fetchMock.mock.calls[0][0]).toBe(publicCenterUrl(CENTER.id));
    expect(center.name).toBe("Centre de santé de Lomé");
    expect(center.service_types).toContain("pharmacie");
    expect(center.opening_hours[0].open).toBe("08:00");
  });

  it("lève une ApiError 404 pour un centre inconnu / tenant inactif (P8)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) })),
    );
    await expect(fetchPublicCenter("missing")).rejects.toMatchObject({
      status: 404,
    });
  });
});
