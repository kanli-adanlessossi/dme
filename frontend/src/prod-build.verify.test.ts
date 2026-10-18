// Vérification de l'artefact de production + CSP applicative (sous-tâche 3.3 —
// écart G-3). NFR-5, NFR-11, NFR-12.
//
// Ce test VERROUILLE les invariants vérifiés manuellement en 3.3 pour qu'une
// régression future (changement de build Vite/PWA ou de `nginx.conf`) casse la
// CI plutôt que la production :
//
//  1. `vite build` produit l'app shell PWA attendu dans `dist/` : index.html,
//     sw.js, manifest.webmanifest et des assets horodatés sous assets/.
//  2. Le service worker précache un manifeste NON VIDE couvrant l'app shell
//     (index.html + JS/CSS horodatés + icônes) — base du rechargement hors
//     ligne : une fois le SW enregistré, un rechargement est servi depuis le
//     Cache Storage.
//  3. `index.html` référence des assets horodatés qui existent réellement sous
//     assets/ (les refs du shell résolvent).
//  4. `nginx.conf` porte une CSP applicative APPLICATIVE (enforcing) SANS joker
//     `*`, les en-têtes `nosniff` / `X-Frame-Options: DENY`, et N'émet PAS HSTS
//     (réservé au terminateur TLS).
//
// Lecture d'artefacts réels (aucun mock) : si `dist/` n'a pas été construit, les
// tests de build sont ignorés avec un message explicite plutôt que d'échouer à
// tort (ils s'exécutent après `npm run build`, p. ex. en CI ou via l'image
// Docker dont le stage build lance `npm run build`).

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = resolve(here, "..");
const distDir = resolve(frontendRoot, "dist");
const nginxConf = resolve(frontendRoot, "nginx.conf");

const distBuilt = existsSync(resolve(distDir, "index.html"));
// `describe.skipIf` garde la suite verte quand le build n'a pas encore tourné.
const describeBuild = distBuilt ? describe : describe.skip;

describeBuild("Artefact de build de production (G-3, NFR-5/NFR-11)", () => {
  it("émet l'app shell PWA : index.html, sw.js, manifest.webmanifest", () => {
    expect(existsSync(resolve(distDir, "index.html"))).toBe(true);
    expect(existsSync(resolve(distDir, "sw.js"))).toBe(true);
    expect(existsSync(resolve(distDir, "manifest.webmanifest"))).toBe(true);
  });

  it("émet des assets horodatés (hash dans le nom) sous assets/", () => {
    const assetsDir = resolve(distDir, "assets");
    expect(existsSync(assetsDir)).toBe(true);
    const files = readdirSync(assetsDir);
    // Au moins un JS et un CSS d'entrée, chacun avec un hash façon Vite.
    const hashed = /-[A-Za-z0-9_-]{8,}\.(?:js|css)$/;
    expect(files.some((f) => hashed.test(f) && f.endsWith(".js"))).toBe(true);
    expect(files.some((f) => hashed.test(f) && f.endsWith(".css"))).toBe(true);
  });

  it("injecte un manifeste de précache NON VIDE couvrant l'app shell", () => {
    const sw = readFileSync(resolve(distDir, "sw.js"), "utf8");
    // Le placeholder `self.__WB_MANIFEST` DOIT avoir été remplacé au build.
    expect(sw.includes("__WB_MANIFEST")).toBe(false);
    const urls = [...sw.matchAll(/"url":"([^"]+)"/g)].map((m) => m[1]);
    expect(urls.length).toBeGreaterThan(0);
    // L'app shell doit être précaché pour un rechargement hors ligne.
    expect(urls).toContain("index.html");
    expect(urls.some((u) => /^assets\/.*\.js$/.test(u))).toBe(true);
    expect(urls.some((u) => /^assets\/.*\.css$/.test(u))).toBe(true);
    // Le SW conserve son câblage de précache et de navigation (fallback shell).
    expect(/precache/i.test(sw)).toBe(true);
  });

  it("index.html référence des assets horodatés qui existent réellement", () => {
    const html = readFileSync(resolve(distDir, "index.html"), "utf8");
    const refs = [...html.matchAll(/\/assets\/([^"']+)/g)].map((m) => m[1]);
    expect(refs.length).toBeGreaterThan(0);
    for (const ref of refs) {
      expect(existsSync(resolve(distDir, "assets", ref))).toBe(true);
    }
  });
});

describe("CSP applicative servie par Nginx (G-3, DoD 3.3)", () => {
  const conf = readFileSync(nginxConf, "utf8");
  const cspDirectives = [
    ...conf.matchAll(/add_header\s+Content-Security-Policy\s+"([^"]+)"/g),
  ].map((m) => m[1]);

  it("définit au moins une CSP applicative", () => {
    expect(cspDirectives.length).toBeGreaterThan(0);
  });

  it("n'utilise AUCUN joker '*' dans la CSP (DoD sans joker)", () => {
    for (const csp of cspDirectives) {
      expect(csp.includes("*")).toBe(false);
    }
  });

  it("verrouille les directives sensibles (default-src 'self', pas d'ancêtre)", () => {
    for (const csp of cspDirectives) {
      expect(csp).toContain("default-src 'self'");
      expect(csp).toContain("frame-ancestors 'none'");
      expect(csp).toContain("object-src 'none'");
    }
  });

  it("porte les en-têtes de sécurité et N'émet PAS HSTS", () => {
    expect(conf).toMatch(/add_header\s+X-Content-Type-Options\s+"nosniff"/);
    expect(conf).toMatch(/add_header\s+X-Frame-Options\s+"DENY"/);
    // HSTS relève du terminateur TLS (reverse proxy), pas de cette image.
    expect(/Strict-Transport-Security/i.test(conf)).toBe(false);
  });
});
