// Sélecteur d'épingle de localisation du centre, contraint au Togo (tâche 17.2).
//
// Rôle
// ----
// Composant carte réutilisé par `CenterAdmin` (éditeur de profil tenant_admin)
// pour **placer/déplacer** l'épingle de localisation du centre. La contrainte
// géographique (P7, exigence 6.2) est appliquée côté client en miroir de la
// validation serveur `ST_Within` / repli boîte englobante (tâche 11.2) :
//
//  - la vue est bornée au Togo (`maxBounds` = `TOGO_BBOX`) : impossible de
//    naviguer franchement hors zone ;
//  - un clic/tap **hors** de la frontière du Togo est **rejeté** : l'épingle ne
//    bouge pas et un message est signalé via `onReject` (feedback UX hors ligne) ;
//  - l'épingle est déplaçable (drag) ; un drag qui déborde est **ramené**
//    (clamp) sur la frontière la plus proche plutôt que silencieusement perdu.
//
// Réutilisation (tâche 15) : même pile MapLibre GL et même style minimal sans
// tuile externe que `TogoMap` (premier rendu rapide, fonctionne hors ligne une
// fois le GeoJSON en cache, NFR-11). Le GeoJSON des régions est chargé en fond
// de repère visuel pour aider l'admin à situer le point.
//
// Mobile-first (NFR-9) : conteneur plein largeur, zoom tactile, commande de zoom
// à grande cible ; aucune rotation (navigation prévisible au doigt).

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import maplibregl, {
  type MapLayerMouseEvent,
  type StyleSpecification,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import {
  fetchTogoRegionsGeoJSON,
  type RegionsFeatureCollection,
} from "../lib/api";
import {
  clampToTogo,
  isInsideTogo,
  TOGO_BBOX,
  type LngLat,
} from "../lib/geo";

/** Identifiants de source/couches internes à MapLibre. */
const SOURCE_ID = "togo-regions";
const FILL_LAYER_ID = "togo-regions-fill";
const OUTLINE_LAYER_ID = "togo-regions-outline";

/** Couleurs du fond de repère (régions, non interactives ici). */
const FILL_COLOR = "#e0f2fe"; // sky-100 (discret : la vedette est l'épingle)
const OUTLINE_COLOR = "#38bdf8"; // sky-400

/** Style MapLibre minimal (aucune tuile externe, aucune clé d'API). */
function baseStyle(): StyleSpecification {
  return {
    version: 8,
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

export interface CenterPinPickerProps {
  /** Position courante de l'épingle (contrôlée par le parent), ou `null`. */
  value: LngLat | null;
  /** Appelé quand l'admin place/déplace l'épingle à une position valide (P7). */
  onChange: (point: LngLat) => void;
  /** Appelé quand un clic hors Togo est rejeté (pour afficher un message). */
  onReject?: () => void;
  /** Classe CSS additionnelle appliquée au conteneur. */
  className?: string;
}

export default function CenterPinPicker({
  value,
  onChange,
  onReject,
  className,
}: CenterPinPickerProps) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);

  // Les callbacks/valeur sont suivis par ref pour ne pas recréer la carte à
  // chaque rendu (l'init MapLibre est coûteuse et doit rester unique).
  const onChangeRef = useRef(onChange);
  const onRejectRef = useRef(onReject);
  const valueRef = useRef<LngLat | null>(value);
  onChangeRef.current = onChange;
  onRejectRef.current = onReject;

  const [loading, setLoading] = useState(true);

  // --- Création/déplacement de l'épingle ---------------------------------
  function placeMarker(map: maplibregl.Map, point: LngLat): void {
    if (markerRef.current) {
      markerRef.current.setLngLat([point.lng, point.lat]);
      return;
    }
    const marker = new maplibregl.Marker({ color: "#0ea5e9", draggable: true })
      .setLngLat([point.lng, point.lat])
      .addTo(map);

    // Drag : ramène dans le Togo si l'on déborde (clamp), puis notifie.
    marker.on("dragend", () => {
      const ll = marker.getLngLat();
      const next = clampToTogo({ lng: ll.lng, lat: ll.lat });
      marker.setLngLat([next.lng, next.lat]);
      valueRef.current = next;
      onChangeRef.current(next);
    });

    markerRef.current = marker;
  }

  // --- Initialisation unique de la carte ---------------------------------
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const controller = new AbortController();
    let disposed = false;
    let map: maplibregl.Map | null = null;

    async function init(hostEl: HTMLDivElement): Promise<void> {
      let data: RegionsFeatureCollection | null = null;
      try {
        data = await fetchTogoRegionsGeoJSON(controller.signal);
      } catch {
        // Le fond de repère est optionnel : on continue sans les régions, la
        // contrainte Togo (maxBounds + rejet au clic) reste pleinement active.
        data = null;
      }
      if (disposed) return;

      map = new maplibregl.Map({
        container: hostEl,
        style: baseStyle(),
        bounds: TOGO_BBOX,
        fitBoundsOptions: { padding: 16 },
        // Borne la navigation à la zone autorisée (P7, miroir du backend).
        maxBounds: TOGO_BBOX,
        attributionControl: { compact: true },
        dragRotate: false,
        pitchWithRotate: false,
        touchZoomRotate: true,
      });
      mapRef.current = map;
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }));
      map.touchZoomRotate.disableRotation();

      map.on("load", () => {
        if (!map || disposed) return;

        if (data) {
          map.addSource(SOURCE_ID, {
            type: "geojson",
            data: data as unknown as GeoJSON.FeatureCollection,
          });
          map.addLayer({
            id: FILL_LAYER_ID,
            type: "fill",
            source: SOURCE_ID,
            paint: { "fill-color": FILL_COLOR, "fill-opacity": 0.6 },
          });
          map.addLayer({
            id: OUTLINE_LAYER_ID,
            type: "line",
            source: SOURCE_ID,
            paint: { "line-color": OUTLINE_COLOR, "line-width": 1 },
          });
        }

        // Clic/tap : place l'épingle si le point est dans le Togo, sinon rejette.
        map.on("click", (e: MapLayerMouseEvent) => {
          const { lng, lat } = e.lngLat;
          if (!isInsideTogo(lng, lat)) {
            onRejectRef.current?.();
            return;
          }
          const next: LngLat = { lng, lat };
          valueRef.current = next;
          placeMarker(map!, next);
          onChangeRef.current(next);
        });

        // Épingle initiale si une position valide est déjà connue.
        if (valueRef.current && isInsideTogo(valueRef.current.lng, valueRef.current.lat)) {
          placeMarker(map, valueRef.current);
        }

        if (!disposed) setLoading(false);
      });

      map.on("error", (ev) => {
        console.warn("MapLibre (pin picker):", ev.error?.message ?? ev);
      });
    }

    void init(container);

    return () => {
      disposed = true;
      controller.abort();
      markerRef.current?.remove();
      markerRef.current = null;
      map?.remove();
      mapRef.current = null;
    };
    // Init unique ; la sync de `value` passe par l'effet ci-dessous.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Synchronise une position fournie par le parent (prop contrôlée) ----
  useEffect(() => {
    valueRef.current = value;
    const map = mapRef.current;
    if (!map) return;
    if (value && isInsideTogo(value.lng, value.lat)) {
      placeMarker(map, value);
    } else if (!value && markerRef.current) {
      markerRef.current.remove();
      markerRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value?.lng, value?.lat]);

  return (
    <div
      className={`relative w-full overflow-hidden rounded-lg border
        border-slate-200 bg-sky-50 ${className ?? ""}`}
    >
      <div
        ref={containerRef}
        role="application"
        aria-label={t("pages.centerAdmin.map.ariaLabel")}
        className="h-[45vh] min-h-64 w-full"
      />

      {loading && (
        <div
          className="pointer-events-none absolute inset-x-0 top-0 flex
            justify-center p-2"
          role="status"
          aria-live="polite"
        >
          <span className="rounded-full bg-white/90 px-3 py-1 text-xs
            text-slate-600 shadow">
            {t("pages.centerAdmin.map.loading")}
          </span>
        </div>
      )}
    </div>
  );
}
