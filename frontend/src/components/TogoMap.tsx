// Carte interactive des régions du Togo (tâche 15.1) — exigences 7.1, 7.2, 7.6.
//
// Enveloppe MapLibre GL JS autour du GeoJSON des 5 régions du Togo servi par
// `GET /api/public/geo/togo-regions.geojson` (mis en cache par le service
// worker, NFR-11). Chaque région est une couche de remplissage portant une
// propriété `code` ; au clic/tap, la région est surlignée et `onRegionSelect`
// est appelé avec son code — c'est le seam pour la feuille inférieure (15.2) et
// le routage vers `RegionCenters` (tâche 16).
//
// Hors périmètre de 15.1 (seams laissés propres) :
//  - feuille inférieure + liste des centres  → tâche 15.2 (via `onRegionSelect`)
//  - repli non-WebGL `RegionFallbackList`     → tâche 15.3 (via `lib/webgl.ts`)
//
// MapLibre n'exige aucune clé d'API : on utilise un style minimal sans fond de
// carte raster (seules les régions sont rendues), ce qui garantit un premier
// rendu rapide (NFR-11) et un fonctionnement hors ligne total une fois le
// GeoJSON en cache.

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import maplibregl, {
  type MapGeoJSONFeature,
  type MapLayerMouseEvent,
  type StyleSpecification,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import {
  fetchTogoRegionsGeoJSON,
  type RegionCode,
  type RegionsFeatureCollection,
} from "../lib/api";

/** Identifiants de source/couches internes à MapLibre. */
const SOURCE_ID = "togo-regions";
const FILL_LAYER_ID = "togo-regions-fill";
const OUTLINE_LAYER_ID = "togo-regions-outline";

/** Emprise approximative du Togo [ouest, sud, est, nord] pour cadrer la vue. */
const TOGO_BOUNDS: [number, number, number, number] = [
  -0.2, 6.0, 1.9, 11.2,
];

/** Couleurs des régions (remplissage normal, survol, sélection). */
const FILL_COLOR = "#bae6fd"; // sky-200
const FILL_COLOR_HOVER = "#7dd3fc"; // sky-300
const FILL_COLOR_SELECTED = "#0ea5e9"; // sky-500
const OUTLINE_COLOR = "#0369a1"; // sky-700

/**
 * Style MapLibre minimal (aucune tuile externe, aucune clé d'API).
 * Le fond est une couleur unie ; seules les régions GeoJSON sont dessinées.
 */
function baseStyle(): StyleSpecification {
  return {
    version: 8,
    // Aucun `glyphs` : les noms de régions sont rendus en marqueurs HTML
    // (voir `addRegionLabels`), évitant tout serveur de polices externe et
    // garantissant un rendu entièrement hors ligne (NFR-11).
    sources: {},
    layers: [
      {
        id: "background",
        type: "background",
        paint: { "background-color": "#f0f9ff" }, // sky-50
      },
    ],
  };
}

export interface TogoMapProps {
  /**
   * Appelé au clic/tap sur une région, avec son code canonique.
   * Seam pour 15.2 (feuille inférieure) et tâche 16 (routage).
   */
  onRegionSelect?: (code: RegionCode) => void;
  /** Région initialement sélectionnée (contrôlée par le parent), optionnel. */
  selectedRegion?: RegionCode | null;
  /** Classe CSS additionnelle appliquée au conteneur. */
  className?: string;
}

/** Extrait le code région d'une feature cliquée, de façon défensive. */
function featureCode(feature: MapGeoJSONFeature | undefined): RegionCode | null {
  const code = feature?.properties?.code;
  return typeof code === "string" ? (code as RegionCode) : null;
}

/**
 * Centroïde approximatif (moyenne des sommets) d'une géométrie Polygon ou
 * MultiPolygon, utilisé pour ancrer le libellé HTML d'une région. Suffisant
 * pour positionner un nom ; aucun besoin d'un centroïde géodésique exact.
 */
function geometryCentroid(
  geometry: GeoJSON.Geometry,
): [number, number] | null {
  const rings: number[][][] =
    geometry.type === "Polygon"
      ? (geometry.coordinates as number[][][])
      : geometry.type === "MultiPolygon"
        ? (geometry.coordinates as number[][][][]).flat()
        : [];
  let sumLng = 0;
  let sumLat = 0;
  let count = 0;
  for (const ring of rings) {
    for (const [lng, lat] of ring) {
      sumLng += lng;
      sumLat += lat;
      count += 1;
    }
  }
  if (count === 0) return null;
  return [sumLng / count, sumLat / count];
}

/** Construit l'élément DOM d'un libellé de région (non interactif). */
function labelElement(text: string): HTMLDivElement {
  const el = document.createElement("div");
  el.textContent = text;
  el.setAttribute("aria-hidden", "true");
  // Styles inline : indépendant de Tailwind, lisible sur fond clair, laisse
  // passer les clics vers la couche de régions dessous.
  el.style.pointerEvents = "none";
  el.style.fontSize = "13px";
  el.style.fontWeight = "600";
  el.style.color = "#0c4a6e"; // sky-900
  el.style.textShadow =
    "0 0 2px #f0f9ff, 0 0 2px #f0f9ff, 0 0 2px #f0f9ff";
  el.style.whiteSpace = "nowrap";
  return el;
}

/**
 * Pose un libellé HTML (nom français) au centroïde de chaque région. Rendu via
 * `maplibregl.Marker` plutôt qu'une couche `symbol`, ce qui évite tout serveur
 * de glyphs et fonctionne hors ligne (NFR-11). Les marqueurs créés sont
 * renvoyés pour pouvoir être retirés au démontage.
 */
function addRegionLabels(
  map: maplibregl.Map,
  data: RegionsFeatureCollection,
): maplibregl.Marker[] {
  const markers: maplibregl.Marker[] = [];
  for (const feature of data.features) {
    const center = geometryCentroid(feature.geometry);
    if (!center) continue;
    const marker = new maplibregl.Marker({
      element: labelElement(feature.properties.name_fr),
    })
      .setLngLat(center)
      .addTo(map);
    markers.push(marker);
  }
  return markers;
}

export default function TogoMap({
  onRegionSelect,
  selectedRegion = null,
  className,
}: TogoMapProps) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  // Marqueurs HTML des libellés de région, suivis pour retrait au démontage.
  const labelMarkersRef = useRef<maplibregl.Marker[]>([]);
  // `selectedRef` suit la sélection pour les expressions de style sans recréer
  // les handlers d'évènements à chaque rendu.
  const selectedRef = useRef<RegionCode | null>(selectedRegion);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // --- Chargement du GeoJSON + initialisation de la carte -----------------
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const controller = new AbortController();
    let disposed = false;
    let map: maplibregl.Map | null = null;

    async function init(hostEl: HTMLDivElement): Promise<void> {
      let data: RegionsFeatureCollection;
      try {
        data = await fetchTogoRegionsGeoJSON(controller.signal);
      } catch (err) {
        if (!disposed && !controller.signal.aborted) {
          setError(
            err instanceof Error ? err.message : t("pages.map.loadError"),
          );
          setLoading(false);
        }
        return;
      }
      if (disposed) return;

      map = new maplibregl.Map({
        container: hostEl,
        style: baseStyle(),
        bounds: TOGO_BOUNDS,
        fitBoundsOptions: { padding: 16 },
        attributionControl: { compact: true },
        // Désactive la rotation : navigation simple et prévisible au doigt.
        dragRotate: false,
        pitchWithRotate: false,
        touchZoomRotate: true,
      });
      mapRef.current = map;

      // Commande de zoom tactile (grande cible, en haut à droite).
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }));
      map.touchZoomRotate.disableRotation();

      map.on("load", () => {
        if (!map || disposed) return;

        map.addSource(SOURCE_ID, {
          type: "geojson",
          data: data as unknown as GeoJSON.FeatureCollection,
          promoteId: "code", // feature-state keyé par le code région
        });

        // Couche de remplissage : couleur selon survol/sélection (REQ 7.2).
        map.addLayer({
          id: FILL_LAYER_ID,
          type: "fill",
          source: SOURCE_ID,
          paint: {
            "fill-color": [
              "case",
              ["boolean", ["feature-state", "selected"], false],
              FILL_COLOR_SELECTED,
              ["boolean", ["feature-state", "hover"], false],
              FILL_COLOR_HOVER,
              FILL_COLOR,
            ],
            "fill-opacity": 0.85,
          },
        });

        // Contour des régions pour les distinguer nettement (REQ 7.1).
        map.addLayer({
          id: OUTLINE_LAYER_ID,
          type: "line",
          source: SOURCE_ID,
          paint: {
            "line-color": OUTLINE_COLOR,
            "line-width": [
              "case",
              ["boolean", ["feature-state", "selected"], false],
              3,
              1.2,
            ],
          },
        });

        // Libellés des régions (nom français) en marqueurs HTML : pas de
        // dépendance à un serveur de glyphs, rendu hors ligne (NFR-11).
        labelMarkersRef.current = addRegionLabels(map, data);

        wireInteractions(map);
        applySelection(map, selectedRef.current);

        if (!disposed) setLoading(false);
      });

      map.on("error", (e) => {
        // Erreurs non bloquantes de MapLibre (p. ex. glyphs hors ligne) : on les
        // journalise sans casser la carte.
        console.warn("MapLibre:", e.error?.message ?? e);
      });
    }

    void init(container);

    return () => {
      disposed = true;
      controller.abort();
      for (const marker of labelMarkersRef.current) marker.remove();
      labelMarkersRef.current = [];
      map?.remove();
      mapRef.current = null;
    };
    // Initialisation unique : les mises à jour de sélection passent par un autre
    // effet. `t` est stable au sein d'une locale.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Interactions : survol + clic ---------------------------------------
  function wireInteractions(map: maplibregl.Map): void {
    let hoveredCode: RegionCode | null = null;

    const setHover = (code: RegionCode | null, hover: boolean) => {
      if (!code) return;
      map.setFeatureState(
        { source: SOURCE_ID, id: code },
        { hover },
      );
    };

    map.on("mousemove", FILL_LAYER_ID, (e: MapLayerMouseEvent) => {
      const code = featureCode(e.features?.[0]);
      if (code === hoveredCode) return;
      setHover(hoveredCode, false);
      hoveredCode = code;
      setHover(hoveredCode, true);
      map.getCanvas().style.cursor = code ? "pointer" : "";
    });

    map.on("mouseleave", FILL_LAYER_ID, () => {
      setHover(hoveredCode, false);
      hoveredCode = null;
      map.getCanvas().style.cursor = "";
    });

    // Clic/tap : surligne la région et notifie le parent (REQ 7.2).
    map.on("click", FILL_LAYER_ID, (e: MapLayerMouseEvent) => {
      const code = featureCode(e.features?.[0]);
      if (!code) return;
      selectedRef.current = code;
      applySelection(map, code);
      onRegionSelect?.(code);
    });
  }

  // --- Application de l'état de sélection sur les features -----------------
  function applySelection(
    map: maplibregl.Map,
    code: RegionCode | null,
  ): void {
    const codes: RegionCode[] = [
      "SAVANES",
      "KARA",
      "CENTRALE",
      "PLATEAUX",
      "MARITIME",
    ];
    for (const c of codes) {
      map.setFeatureState(
        { source: SOURCE_ID, id: c },
        { selected: c === code },
      );
    }
  }

  // Synchronise la sélection contrôlée par le parent (prop) avec la carte.
  useEffect(() => {
    selectedRef.current = selectedRegion;
    const map = mapRef.current;
    if (map && map.getSource(SOURCE_ID)) {
      applySelection(map, selectedRegion);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRegion]);

  return (
    <div
      className={`relative w-full overflow-hidden rounded-lg border
        border-slate-200 bg-sky-50 ${className ?? ""}`}
    >
      {/* Conteneur de la carte : hauteur mobile-first, responsive. */}
      <div
        ref={containerRef}
        role="application"
        aria-label={t("pages.map.ariaLabel")}
        className="h-[60vh] min-h-72 w-full md:h-[70vh]"
      />

      {loading && !error && (
        <div
          className="absolute inset-0 flex items-center justify-center
            bg-sky-50/80 text-sm text-slate-500"
          role="status"
          aria-live="polite"
        >
          {t("pages.map.loading")}
        </div>
      )}

      {error && (
        <div
          className="absolute inset-0 flex items-center justify-center p-4
            text-center text-sm text-red-700"
          role="alert"
        >
          {t("pages.map.loadError")}
        </div>
      )}
    </div>
  );
}
