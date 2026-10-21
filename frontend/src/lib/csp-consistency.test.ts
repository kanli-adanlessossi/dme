// Verrouillage de cohérence de la CSP servie par Nginx (#9, tâche 4.3).
//
// Exigence 2 (critères 1, 4) ; NFR-2. Ce test lit `frontend/nginx.conf`, extrait
// toutes les valeurs d'en-tête `Content-Security-Policy` (enforcing) des 4 blocs
// `location` (index/fallback, sw.js, manifest.webmanifest, assets) et ÉCHOUE si
// elles ne sont pas STRICTEMENT identiques.
//
// POURQUOI (intention) : `add_header` n'est PAS hérité dès qu'un bloc `location`
// redéfinit ses propres `add_header` ; la CSP est donc répétée dans chaque bloc.
// Une divergence entre blocs (p. ex. un bloc qui garderait `'unsafe-inline'`, ou
// qui perdrait une directive) est une FAILLE DE SÉCURITÉ SILENCIEUSE : certaines
// ressources seraient servies sous une politique plus laxiste que les autres,
// sans aucun signal visible. Ce test transforme cet invariant implicite en
// garde-fou automatisé.
//
// Le test vérifie, en plus de l'identité :
//   - qu'il y a EXACTEMENT 4 occurrences enforcing (garde contre un bloc qui
//     perdrait son en-tête CSP) ;
//   - que `style-src` vaut EXACTEMENT `'self'` — pas de `'unsafe-inline'` ni de
//     joker (resserrement de la tâche 4.3, inventaire 4.1 : zéro style inline
//     légitime régi par `style-src`) ;
//   - que si des en-têtes `Content-Security-Policy-Report-Only` subsistent, ils
//     sont eux aussi tous identiques entre eux (le même invariant de cohérence
//     s'applique). La tâche 4.3 les a retirés comme redondants ; le test
//     n'EXIGE pas leur absence mais interdit toute divergence s'ils reviennent.
//
// LIMITE : ce test valide la COHÉRENCE et la valeur de la CSP dans le fichier de
// configuration ; il ne remplace pas `nginx -t` (syntaxe) ni une vérification
// runtime des en-têtes effectivement émis par un conteneur servant.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
// Ce fichier est dans `frontend/src/lib` → la conf est deux niveaux plus haut.
const nginxConfPath = resolve(here, "..", "..", "nginx.conf");

// Nombre de blocs `location` portant la CSP applicative (index/fallback, sw.js,
// manifest.webmanifest, assets). Un écart sur ce compte = un bloc a perdu (ou
// gagné) son en-tête — à signaler.
const EXPECTED_CSP_BLOCKS = 4;

/**
 * Extrait la valeur (chaîne entre guillemets) de chaque `add_header <name> "…"`.
 * On capture le premier argument entre doubles guillemets de la directive, qui
 * est la valeur de l'en-tête CSP.
 */
function extractHeaderValues(conf: string, headerName: string): string[] {
  // Échapper le tiret de `Content-Security-Policy-Report-Only` pour la regex.
  const escaped = headerName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // `add_header <name> "valeur" …` — la valeur ne contient pas de `"` interne
  // dans notre conf (les guillemets de `report-to` n'y figurent pas).
  const re = new RegExp(`add_header\\s+${escaped}\\s+"([^"]*)"`, "g");
  const values: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(conf)) !== null) {
    values.push(m[1]);
  }
  return values;
}

/** Extrait la valeur de la directive `style-src …` d'une CSP (sans le `;`). */
function styleSrcOf(csp: string): string | undefined {
  const m = csp.match(/(?:^|;)\s*style-src\s+([^;]*)/);
  return m ? m[1].trim() : undefined;
}

describe("#9 — CSP cohérente et resserrée dans frontend/nginx.conf (tâche 4.3)", () => {
  const conf = readFileSync(nginxConfPath, "utf8");

  // On ne veut PAS capter l'en-tête Report-Only quand on cherche l'enforcing.
  // `Content-Security-Policy "` (suivi d'un guillemet) ne matche jamais
  // `Content-Security-Policy-Report-Only "`, car après `Policy` vient `-` et non
  // un espace puis `"`. La regex ancre donc sur `Content-Security-Policy\s+"`.
  const enforcing = extractHeaderValues(conf, "Content-Security-Policy");
  const reportOnly = extractHeaderValues(
    conf,
    "Content-Security-Policy-Report-Only",
  );

  it("lit effectivement le fichier nginx.conf (garde-fou)", () => {
    // Si la conf est vide/illisible, le reste du test ne prouverait rien.
    expect(conf.length).toBeGreaterThan(0);
  });

  it(`contient exactement ${EXPECTED_CSP_BLOCKS} en-têtes CSP enforcing (un par bloc location)`, () => {
    // Garde contre un bloc qui perdrait silencieusement son en-tête CSP.
    expect(
      enforcing.length,
      `Attendu ${EXPECTED_CSP_BLOCKS} en-têtes Content-Security-Policy enforcing, trouvé ${enforcing.length}. ` +
        "Un bloc location a-t-il perdu (ou dupliqué) sa CSP ?",
    ).toBe(EXPECTED_CSP_BLOCKS);
  });

  it("sert une CSP enforcing IDENTIQUE sur les 4 blocs (pas de divergence silencieuse)", () => {
    const unique = new Set(enforcing);
    expect(
      unique.size,
      unique.size > 1
        ? `Divergence de CSP enforcing entre blocs location — FAILLE SILENCIEUSE. Valeurs distinctes :\n  ${[...unique].join("\n  ")}`
        : "",
    ).toBe(1);
  });

  it("verrouille `style-src 'self'` enforcing (sans 'unsafe-inline' ni joker)", () => {
    for (const csp of enforcing) {
      const styleSrc = styleSrcOf(csp);
      expect(styleSrc, `Directive style-src absente de la CSP : ${csp}`).toBeDefined();
      // Resserrement 4.3 : exactement 'self', rien d'autre.
      expect(styleSrc).toBe("'self'");
      expect(csp).not.toContain("'unsafe-inline'");
      expect(csp).not.toContain("style-src *");
    }
  });

  it("n'autorise aucune divergence entre d'éventuels en-têtes Report-Only restants", () => {
    // La tâche 4.3 les a retirés (redondants). S'ils reviennent, le même
    // invariant de cohérence s'applique : tous identiques entre eux.
    if (reportOnly.length > 0) {
      const unique = new Set(reportOnly);
      expect(
        unique.size,
        unique.size > 1
          ? `Divergence de CSP Report-Only entre blocs — à corriger. Valeurs distinctes :\n  ${[...unique].join("\n  ")}`
          : "",
      ).toBe(1);
    } else {
      // Cas nominal après 4.3 : plus aucun Report-Only.
      expect(reportOnly.length).toBe(0);
    }
  });
});
