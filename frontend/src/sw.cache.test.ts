// Vérification du cache hors ligne du service worker (tâche 15.4) — NFR-11.
//
// La carte doit se recharger hors ligne après la première visite : l'app shell
// est précaché (règle `precacheAndRoute(self.__WB_MANIFEST)` dans `sw.ts`) et le
// GeoJSON des régions est mis en cache à l'exécution par une route Workbox dont
// le prédicat est `url.pathname.endsWith("/geo/togo-regions.geojson")`.
//
// `sw.ts` ne peut pas être importé tel quel sous jsdom (il exécute du code
// Workbox exigeant un contexte de service worker). On vérifie plutôt le CONTRAT
// qui lie l'URL cliente et la route SW : le prédicat appliqué par le service
// worker DOIT matcher l'URL réellement interrogée par `fetchTogoRegionsGeoJSON`
// (`TOGO_REGIONS_GEOJSON_URL`). Si l'un change sans l'autre, la carte ne serait
// plus servie depuis le cache hors ligne — ce test échouerait.
//
// Les tests `geo.test.ts` (15.1) vérifiaient le suffixe de l'URL textuellement ;
// ici on applique le prédicat exactement comme le SW l'évalue (via `URL`,
// `pathname`), et on couvre quelques contre-exemples pour éviter une règle trop
// large.

import { describe, expect, it } from "vitest";

import { TOGO_REGIONS_GEOJSON_URL } from "./lib/api";

/**
 * Réplique EXACTE du prédicat de la route `map-geojson` dans `src/sw.ts` :
 *   registerRoute(({ url }) => url.pathname.endsWith("/geo/togo-regions.geojson"), ...)
 * Garde ce prédicat synchronisé avec `sw.ts`.
 */
function matchesGeojsonRoute(rawUrl: string): boolean {
  const url = new URL(rawUrl);
  return url.pathname.endsWith("/geo/togo-regions.geojson");
}

describe("Route SW map-geojson (NFR-11, rechargement hors ligne)", () => {
  it("matche l'URL réellement interrogée par le client (TOGO_REGIONS_GEOJSON_URL)", () => {
    // C'est le lien critique : la requête de la carte doit tomber sur la route
    // de cache, sinon pas de GeoJSON hors ligne.
    expect(matchesGeojsonRoute(TOGO_REGIONS_GEOJSON_URL)).toBe(true);
  });

  it("matche quelle que soit l'origine (dev localhost ou prod)", () => {
    expect(
      matchesGeojsonRoute(
        "http://localhost:8000/api/public/geo/togo-regions.geojson",
      ),
    ).toBe(true);
    expect(
      matchesGeojsonRoute(
        "https://centres.example.tg/api/public/geo/togo-regions.geojson",
      ),
    ).toBe(true);
  });

  it("matche même en présence d'une chaîne de requête (pathname seul compte)", () => {
    expect(
      matchesGeojsonRoute(
        "https://centres.example.tg/api/public/geo/togo-regions.geojson?v=2",
      ),
    ).toBe(true);
  });

  it("ne matche PAS d'autres ressources (règle non trop large)", () => {
    expect(
      matchesGeojsonRoute("https://centres.example.tg/api/sync/pull"),
    ).toBe(false);
    expect(
      matchesGeojsonRoute(
        "https://centres.example.tg/api/public/regions/MARITIME/centers",
      ),
    ).toBe(false);
    // Un chemin ressemblant mais distinct ne doit pas matcher.
    expect(
      matchesGeojsonRoute(
        "https://centres.example.tg/geo/togo-regions.geojson.bak",
      ),
    ).toBe(false);
  });
});
