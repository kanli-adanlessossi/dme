// Contrainte géographique « dans le Togo » côté client (P7) — exigence 6.2.
//
// Rôle (tâche 17.2)
// -----------------
// Le sélecteur d'épingle sur carte (`CenterPinPicker`) doit empêcher — ou à
// défaut rejeter — toute coordonnée placée hors de la frontière du Togo, en
// miroir de la validation serveur `ST_Within` (`backend/app/services/center.py`,
// tâche 11.2). Côté client, on ne dispose pas de PostGIS : on réplique le
// **repli boîte englobante** utilisé par le backend quand la géométrie des
// régions n'est pas disponible.
//
// Alignement avec le backend
// ---------------------------
// Les bornes RECOPIENT fidèlement `_TOGO_LON_MIN/MAX` et `_TOGO_LAT_MIN/MAX` de
// `app/services/center.py` :
//   longitude ∈ [-0.15, 1.80], latitude ∈ [6.10, 11.14].
// La validation client est une **première ligne de défense** (UX : empêcher un
// clic hors zone, feedback immédiat hors ligne) ; le serveur reste l'autorité
// (`ST_Within`, puis contrainte `CHECK` en base). Ne pas diverger de ces bornes
// sans mettre à jour le backend en conséquence.

/** Point géographique WGS84 (longitude/latitude en degrés décimaux). */
export interface LngLat {
  lng: number;
  lat: number;
}

/**
 * Boîte englobante du Togo (repli, miroir de `app/services/center.py`).
 * `[ouest, sud, est, nord]` en degrés décimaux WGS84.
 */
export const TOGO_LON_MIN = -0.15;
export const TOGO_LON_MAX = 1.8;
export const TOGO_LAT_MIN = 6.1;
export const TOGO_LAT_MAX = 11.14;

/**
 * Emprise `[ouest, sud, est, nord]` prête pour `maplibregl` (`maxBounds`,
 * `fitBounds`). Même boîte que la validation, afin que la carte ne laisse pas
 * déborder la vue hors de la zone autorisée.
 */
export const TOGO_BBOX: [number, number, number, number] = [
  TOGO_LON_MIN,
  TOGO_LAT_MIN,
  TOGO_LON_MAX,
  TOGO_LAT_MAX,
];

/**
 * Vrai si le point `(lng, lat)` se situe dans la boîte englobante du Togo (P7).
 *
 * Les bornes sont **inclusives** (un point exactement sur la frontière de la
 * boîte est accepté), cohérent avec les comparaisons `<=` du backend. Les
 * valeurs non finies (`NaN`, `Infinity`) sont rejetées.
 */
export function isInsideTogo(lng: number, lat: number): boolean {
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return false;
  return (
    lng >= TOGO_LON_MIN &&
    lng <= TOGO_LON_MAX &&
    lat >= TOGO_LAT_MIN &&
    lat <= TOGO_LAT_MAX
  );
}

/** Variante acceptant un objet `LngLat` (confort pour les appelants carte). */
export function isPointInsideTogo(point: LngLat): boolean {
  return isInsideTogo(point.lng, point.lat);
}

/**
 * Ramène un point dans la boîte du Togo en bornant chaque coordonnée (clamp).
 *
 * Utilisé par le sélecteur d'épingle pour « rattraper » un drag qui déborde
 * légèrement plutôt que de rejeter silencieusement : l'épingle reste visible et
 * sur la frontière la plus proche. Pour un clic franchement hors zone, l'UI
 * préfère un rejet explicite (message) — voir `CenterPinPicker`.
 */
export function clampToTogo(point: LngLat): LngLat {
  return {
    lng: Math.min(Math.max(point.lng, TOGO_LON_MIN), TOGO_LON_MAX),
    lat: Math.min(Math.max(point.lat, TOGO_LAT_MIN), TOGO_LAT_MAX),
  };
}
