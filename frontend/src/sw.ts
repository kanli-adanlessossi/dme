/// <reference lib="webworker" />
// Service worker (Workbox) — stratégie injectManifest de vite-plugin-pwa.
//
// Rôle (NFR-5, NFR-11) :
//  - précacher l'« app shell » (JS/CSS/HTML/icônes du build) pour que
//    l'application se recharge et fonctionne SANS réseau après la 1ʳᵉ visite ;
//  - mettre en cache à l'exécution les ressources de carte (GeoJSON des régions
//    du Togo + tuiles/polices MapLibre) afin que la carte publique tolère les
//    coupures de connectivité (NFR-11 : premier rendu < 3 s en 3G, sans
//    dépendre du réseau aux visites suivantes).
//
// `self.__WB_MANIFEST` est remplacé au build par la liste des ressources
// précachées (manifeste injecté par vite-plugin-pwa).
import { clientsClaim } from "workbox-core";
import { precacheAndRoute, cleanupOutdatedCaches } from "workbox-precaching";
import { registerRoute, NavigationRoute } from "workbox-routing";
import {
  CacheFirst,
  StaleWhileRevalidate,
} from "workbox-strategies";
import { CacheableResponsePlugin } from "workbox-cacheable-response";
import { ExpirationPlugin } from "workbox-expiration";
import { createHandlerBoundToURL } from "workbox-precaching";

declare let self: ServiceWorkerGlobalScope;

// Prise de contrôle immédiate des pages ouvertes dès l'activation.
self.skipWaiting();
clientsClaim();

// Précache de l'app shell (injecté au build) + nettoyage des anciens caches.
precacheAndRoute(self.__WB_MANIFEST);
cleanupOutdatedCaches();

// Navigation SPA : servir index.html depuis le précache pour tout chemin
// applicatif (hors API et fichiers statiques), permettant le rechargement
// hors ligne sur n'importe quelle route.
const navigationRoute = new NavigationRoute(
  createHandlerBoundToURL("index.html"),
  {
    // Ne pas intercepter les requêtes vers l'API ni les fichiers horodatés.
    denylist: [/^\/api\//, /\/[^/?]+\.[^/]+$/],
  },
);
registerRoute(navigationRoute);

// GeoJSON des régions du Togo : rarement modifié, essentiel à la carte.
// StaleWhileRevalidate : réponse immédiate depuis le cache (rendu rapide, NFR-11)
// et rafraîchissement en arrière-plan quand le réseau est disponible.
registerRoute(
  ({ url }) => url.pathname.endsWith("/geo/togo-regions.geojson"),
  new StaleWhileRevalidate({
    cacheName: "map-geojson",
    plugins: [
      new CacheableResponsePlugin({ statuses: [0, 200] }),
      new ExpirationPlugin({ maxEntries: 8, maxAgeSeconds: 30 * 24 * 60 * 60 }),
    ],
  }),
);

// Tuiles de carte (raster/vectorielles) et sprites/polices MapLibre.
// CacheFirst : les tuiles sont immuables ; on privilégie le hors ligne et la
// vitesse, avec une expiration bornant la taille du cache.
registerRoute(
  ({ url, request }) =>
    request.destination === "image" &&
    (/[?&](x|y|z)=/.test(url.search) ||
      /\/tiles?\//.test(url.pathname) ||
      /\.(?:pbf|mvt)$/.test(url.pathname)),
  new CacheFirst({
    cacheName: "map-tiles",
    plugins: [
      new CacheableResponsePlugin({ statuses: [0, 200] }),
      new ExpirationPlugin({
        maxEntries: 500,
        maxAgeSeconds: 30 * 24 * 60 * 60,
        purgeOnQuotaError: true,
      }),
    ],
  }),
);

// Lectures de l'annuaire public (régions, centres d'une région, profil d'un
// centre) : `GET /api/public/regions...` et `/api/public/centers/...`.
// StaleWhileRevalidate : réponse immédiate depuis le cache (pages annuaire et
// profil disponibles hors ligne après la 1ʳᵉ visite, NFR-11) et rafraîchissement
// en arrière-plan quand le réseau est là. On exclut explicitement le GeoJSON,
// déjà géré par sa propre route `map-geojson` ci-dessus. Projection publique
// sûre uniquement (P4) : aucune PHI ne transite par ces endpoints.
registerRoute(
  ({ url, request }) =>
    request.method === "GET" &&
    /\/api\/public\//.test(url.pathname) &&
    !url.pathname.endsWith("/geo/togo-regions.geojson"),
  new StaleWhileRevalidate({
    cacheName: "public-directory",
    plugins: [
      new CacheableResponsePlugin({ statuses: [0, 200] }),
      new ExpirationPlugin({
        maxEntries: 200,
        maxAgeSeconds: 7 * 24 * 60 * 60,
        purgeOnQuotaError: true,
      }),
    ],
  }),
);

// Styles, sprites et glyphes MapLibre (JSON/PNG/polices) servis depuis un CDN
// ou l'API : StaleWhileRevalidate pour rester disponible hors ligne.
registerRoute(
  ({ url }) =>
    /\/(sprites?|glyphs?|fonts?|styles?)\//.test(url.pathname) ||
    /\.(?:json|png|woff2?|ttf)$/.test(url.pathname),
  new StaleWhileRevalidate({
    cacheName: "map-assets",
    plugins: [
      new CacheableResponsePlugin({ statuses: [0, 200] }),
      new ExpirationPlugin({
        maxEntries: 200,
        maxAgeSeconds: 30 * 24 * 60 * 60,
        purgeOnQuotaError: true,
      }),
    ],
  }),
);

// Permet au client (workbox-window) de déclencher l'activation d'une mise à jour.
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});
