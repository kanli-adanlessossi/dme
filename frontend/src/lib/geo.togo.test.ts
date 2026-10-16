// Tests de la contrainte « dans le Togo » (tâche 17.2) — vérifie P7.
//
// `isInsideTogo` est la première ligne de défense côté client (sélecteur
// d'épingle), en miroir de la validation serveur `ST_Within` / repli boîte
// englobante (`backend/app/services/center.py`, tâche 11.2). Deux garanties :
//   - un point à l'intérieur de la boîte du Togo est accepté ;
//   - un point hors de la boîte est rejeté (c'est la propriété P7 que l'UI doit
//     empêcher avant même l'appel serveur).
//
// On couvre des exemples (villes togolaises réelles, points étrangers) ET une
// propriété universelle (fast-check) : tout point hors de la boîte est rejeté,
// quelle que soit sa position dans le plan.

import { describe, expect, it } from "vitest";
import fc from "fast-check";

import {
  clampToTogo,
  isInsideTogo,
  isPointInsideTogo,
  TOGO_LAT_MAX,
  TOGO_LAT_MIN,
  TOGO_LON_MAX,
  TOGO_LON_MIN,
} from "./geo";

describe("isInsideTogo — exemples (P7)", () => {
  // Coordonnées (lon, lat) de points réels au Togo.
  const INSIDE: Array<[string, number, number]> = [
    ["Lomé", 1.2255, 6.1319],
    ["Kara", 1.1861, 9.5511],
    ["Sokodé", 1.1446, 8.9833],
    ["Dapaong", 0.2074, 10.8626],
    ["Atakpamé", 1.1278, 7.5333],
  ];

  it.each(INSIDE)("accepte %s (dans le Togo)", (_name, lng, lat) => {
    expect(isInsideTogo(lng, lat)).toBe(true);
  });

  // Points clairement hors du Togo.
  const OUTSIDE: Array<[string, number, number]> = [
    ["Paris", 2.3522, 48.8566],
    ["Accra (Ghana)", -0.1869, 5.6037], // latitude trop basse
    ["Cotonou (Bénin)", 2.4252, 6.3703], // longitude trop à l'est
    ["Ouagadougou (Burkina)", -1.5197, 12.3714],
    ["Océan au sud", 1.0, 5.0],
  ];

  it.each(OUTSIDE)("rejette %s (hors Togo)", (_name, lng, lat) => {
    expect(isInsideTogo(lng, lat)).toBe(false);
  });

  it("accepte les points exactement sur la frontière de la boîte (bornes inclusives)", () => {
    expect(isInsideTogo(TOGO_LON_MIN, TOGO_LAT_MIN)).toBe(true);
    expect(isInsideTogo(TOGO_LON_MAX, TOGO_LAT_MAX)).toBe(true);
  });

  it("rejette les valeurs non finies (NaN / Infinity)", () => {
    expect(isInsideTogo(Number.NaN, 7)).toBe(false);
    expect(isInsideTogo(1, Number.POSITIVE_INFINITY)).toBe(false);
  });

  it("isPointInsideTogo accepte la forme objet", () => {
    expect(isPointInsideTogo({ lng: 1.2255, lat: 6.1319 })).toBe(true);
    expect(isPointInsideTogo({ lng: 2.3522, lat: 48.8566 })).toBe(false);
  });
});

describe("isInsideTogo — propriété universelle (P7)", () => {
  it("rejette TOUT point dont une coordonnée sort de la boîte du Togo", () => {
    fc.assert(
      fc.property(
        fc.double({ min: -180, max: 180, noNaN: true }),
        fc.double({ min: -90, max: 90, noNaN: true }),
        (lng, lat) => {
          const outside =
            lng < TOGO_LON_MIN ||
            lng > TOGO_LON_MAX ||
            lat < TOGO_LAT_MIN ||
            lat > TOGO_LAT_MAX;
          if (outside) {
            // La propriété P7 : hors boîte => jamais accepté.
            expect(isInsideTogo(lng, lat)).toBe(false);
          }
        },
      ),
      { numRuns: 500 },
    );
  });

  it("accepte TOUT point à l'intérieur de la boîte", () => {
    fc.assert(
      fc.property(
        fc.double({ min: TOGO_LON_MIN, max: TOGO_LON_MAX, noNaN: true }),
        fc.double({ min: TOGO_LAT_MIN, max: TOGO_LAT_MAX, noNaN: true }),
        (lng, lat) => {
          expect(isInsideTogo(lng, lat)).toBe(true);
        },
      ),
      { numRuns: 500 },
    );
  });
});

describe("clampToTogo — ramène dans la boîte", () => {
  it("laisse inchangé un point déjà à l'intérieur", () => {
    const p = { lng: 1.0, lat: 8.0 };
    expect(clampToTogo(p)).toEqual(p);
  });

  it("borne un point hors zone sur la frontière la plus proche", () => {
    const clamped = clampToTogo({ lng: 50, lat: -10 });
    expect(clamped.lng).toBe(TOGO_LON_MAX);
    expect(clamped.lat).toBe(TOGO_LAT_MIN);
    // Et le résultat du clamp est, par construction, dans le Togo.
    expect(isInsideTogo(clamped.lng, clamped.lat)).toBe(true);
  });

  it("tout clamp produit un point accepté par isInsideTogo (propriété)", () => {
    fc.assert(
      fc.property(
        fc.double({ min: -180, max: 180, noNaN: true }),
        fc.double({ min: -90, max: 90, noNaN: true }),
        (lng, lat) => {
          const c = clampToTogo({ lng, lat });
          expect(isInsideTogo(c.lng, c.lat)).toBe(true);
        },
      ),
      { numRuns: 500 },
    );
  });
});
