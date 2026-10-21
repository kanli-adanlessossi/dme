// Verrouillage « zéro sink JS dangereux » (#9) — mitigation anti-XSS concrète.
//
// Exigence 2 (critère 6) ; NFR-2, NFR-8. Ce test scanne les sources
// `frontend/src/**` et ÉCHOUE si un sink DOM dangereux réapparaît dans le code
// réel :
//
//   innerHTML, outerHTML, dangerouslySetInnerHTML, eval(, insertAdjacentHTML,
//   document.write
//
// Il verrouille la propriété « 0 sink » constatée aujourd'hui (le rendu React
// auto-échappant est la défense de fond). C'est la mitigation vérifiable exigée
// par le critère 2.6 : la spec ne se clôt pas sur le seul ADR.
//
// Pourquoi exclure commentaires ET chaînes de caractères (pas seulement les
// fichiers de test) :
//   - le commentaire « Styles inline » de `TogoMap.tsx` contient le mot-clé
//     « inline » et serait un faux positif sur un scan naïf ;
//   - CE fichier contient lui-même les motifs recherchés (dans la liste SINKS
//     ci-dessous, sous forme de chaînes) ; sans exclusion des littéraux il se
//     signalerait lui-même. On neutralise donc commentaires et chaînes AVANT de
//     chercher les sinks, de sorte que seul le code exécutable compte.
//
// LIMITES (à ne pas sur-promettre — cohérent avec la limite « XSS actif »,
// exigence 2.7) :
//   1. Ce test couvre le CODE DU PROJET (`frontend/src/**`) UNIQUEMENT. Il ne
//      scanne PAS les dépendances tierces (`node_modules`) : un sink introduit
//      par une bibliothèque (p. ex. un composant faisant de l'`innerHTML`) n'est
//      PAS détecté ici.
//   2. Il réduit la surface d'injection, il ne la supprime pas. Ce n'est PAS une
//      garantie absolue contre le XSS : la défense de fond reste la prévention de
//      l'injection (CSP sans joker, `style-src` durci, auto-échappement React) et
//      la protection du stockage des jetons NE couvre PAS un XSS actif.
//   3. Le stripper de commentaires/chaînes est volontairement conservateur : en
//      cas de doute il préfère NE PAS masquer du code (éviter un faux négatif qui
//      laisserait passer un vrai sink) quitte à exiger que les motifs dans la
//      doc/les chaînes soient bien reconnus comme tels.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
// Racine scannée : `frontend/src` (ce fichier est dans `frontend/src/lib`).
const srcRoot = resolve(here, "..");

// Extensions de code à inspecter (sources TS/JS et composants React).
const CODE_EXTENSIONS = new Set([".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"]);

// Motifs de sink dangereux. Chacun est recherché dans le CODE RÉEL (après avoir
// retiré commentaires et chaînes de caractères). `eval` est borné à l'appel
// `eval(` pour ne pas capter un identifiant anodin contenant « eval ».
const SINKS: ReadonlyArray<{ name: string; re: RegExp }> = [
  { name: "innerHTML", re: /\binnerHTML\b/ },
  { name: "outerHTML", re: /\bouterHTML\b/ },
  { name: "dangerouslySetInnerHTML", re: /\bdangerouslySetInnerHTML\b/ },
  { name: "eval(", re: /\beval\s*\(/ },
  { name: "insertAdjacentHTML", re: /\binsertAdjacentHTML\b/ },
  { name: "document.write", re: /\bdocument\s*\.\s*write\b/ },
];

/** Liste récursivement les fichiers de code sous `dir` (hors node_modules). */
function listCodeFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    // Ignorer les artefacts et dépendances : on ne verrouille QUE le code projet.
    if (entry === "node_modules" || entry === "dist" || entry.startsWith("."))
      continue;
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      out.push(...listCodeFiles(full));
    } else if (CODE_EXTENSIONS.has(extname(entry))) {
      out.push(full);
    }
  }
  return out;
}

/**
 * Retire les commentaires (ligne `//`, bloc `/* *\/`) et les chaînes de
 * caractères (`'...'`, `"..."`, templates `` `...` ``) d'une source, en les
 * remplaçant par un espace. Objectif : ne laisser QUE le code exécutable, afin
 * qu'un motif de sink présent dans un commentaire (« Styles inline ») ou dans un
 * littéral (les motifs de CE test) ne compte pas comme un vrai sink.
 *
 * C'est un scanner par automate à états, pas un parseur complet : il suffit pour
 * neutraliser commentaires/chaînes sans dépendance externe. En cas d'ambiguïté,
 * il reste conservateur (il ne masque pas plus que nécessaire).
 */
function stripCommentsAndStrings(src: string): string {
  let out = "";
  let i = 0;
  const n = src.length;

  type State =
    | "code"
    | "lineComment"
    | "blockComment"
    | "single"
    | "double"
    | "template";
  let state: State = "code";

  while (i < n) {
    const c = src[i];
    const next = i + 1 < n ? src[i + 1] : "";

    switch (state) {
      case "code": {
        if (c === "/" && next === "/") {
          state = "lineComment";
          i += 2;
        } else if (c === "/" && next === "*") {
          state = "blockComment";
          i += 2;
        } else if (c === "'") {
          state = "single";
          i += 1;
        } else if (c === '"') {
          state = "double";
          i += 1;
        } else if (c === "`") {
          state = "template";
          i += 1;
        } else {
          out += c;
          i += 1;
        }
        break;
      }
      case "lineComment": {
        // Conserver le saut de ligne pour préserver la numérotation de lignes.
        if (c === "\n") {
          out += c;
          state = "code";
        }
        i += 1;
        break;
      }
      case "blockComment": {
        if (c === "*" && next === "/") {
          state = "code";
          i += 2;
        } else {
          // Préserver les sauts de ligne internes au bloc.
          if (c === "\n") out += c;
          i += 1;
        }
        break;
      }
      case "single":
      case "double": {
        const quote = state === "single" ? "'" : '"';
        if (c === "\\") {
          // Échappement : sauter le caractère suivant.
          i += 2;
        } else if (c === quote) {
          state = "code";
          i += 1;
        } else {
          if (c === "\n") out += c; // garder la numérotation si chaîne mal fermée.
          i += 1;
        }
        break;
      }
      case "template": {
        if (c === "\\") {
          i += 2;
        } else if (c === "`") {
          state = "code";
          i += 1;
        } else {
          // Note : on ne descend PAS dans les interpolations `${...}`. C'est le
          // choix conservateur : le code interpolé est masqué comme le reste du
          // template. Un sink écrit DANS une interpolation ne serait donc pas vu
          // par ce scanner — limite assumée, cohérente avec la portée réduite du
          // test (cf. en-tête, limite 3).
          if (c === "\n") out += c;
          i += 1;
        }
        break;
      }
    }
  }
  return out;
}

/** Numéro de ligne (1-indexé) de l'offset `idx` dans `src`. */
function lineOf(src: string, idx: number): number {
  let line = 1;
  for (let k = 0; k < idx && k < src.length; k++) {
    if (src[k] === "\n") line += 1;
  }
  return line;
}

describe("#9 — aucun sink JS dangereux dans frontend/src/** (verrouillage 0 sink)", () => {
  const files = listCodeFiles(srcRoot);

  it("inspecte un ensemble non vide de fichiers de code (garde-fou)", () => {
    // Si ce scan ne voit aucun fichier, le test ne prouve rien : garde-fou
    // contre un faux « vert » dû à un chemin de scan erroné.
    expect(files.length).toBeGreaterThan(0);
  });

  it("ne contient aucun sink dangereux dans le code réel (hors commentaires et chaînes)", () => {
    const findings: string[] = [];

    for (const file of files) {
      const raw = readFileSync(file, "utf8");
      const code = stripCommentsAndStrings(raw);
      const rel = relative(srcRoot, file).replace(/\\/g, "/");

      for (const sink of SINKS) {
        const re = new RegExp(sink.re.source, "g");
        let m: RegExpExecArray | null;
        while ((m = re.exec(code)) !== null) {
          findings.push(`${rel}:${lineOf(code, m.index)} → ${sink.name}`);
        }
      }
    }

    // Message détaillé : chaque occurrence listée (fichier:ligne → sink) pour que
    // l'échec pointe directement le code fautif à retirer.
    expect(
      findings,
      findings.length
        ? `Sink(s) dangereux détecté(s) dans le code projet :\n  ${findings.join("\n  ")}`
        : "",
    ).toEqual([]);
  });
});

// --- Auto-test du scanner (évite un faux « vert ») --------------------------
// Un scanner anti-sink n'a de valeur que s'il DÉTECTE réellement un sink. On
// vérifie donc qu'il voit un sink dans du code, et qu'il l'ignore quand le même
// motif n'est QUE dans un commentaire ou une chaîne (sinon le test de projet
// ci-dessus serait un faux positif permanent).
describe("#9 — le scanner anti-sink détecte et discrimine correctement", () => {
  // On construit les échantillons par concaténation pour que CE fichier ne
  // contienne pas lui-même la séquence littérale d'un sink hors chaîne.
  const SINK_WORD = "inner" + "HTML";
  const EVAL_CALL = "ev" + "al(x)";

  function scanCount(src: string): number {
    const code = stripCommentsAndStrings(src);
    let count = 0;
    for (const sink of SINKS) {
      const re = new RegExp(sink.re.source, "g");
      while (re.exec(code) !== null) count += 1;
    }
    return count;
  }

  it("détecte un sink présent dans le code réel", () => {
    expect(scanCount(`el.${SINK_WORD} = value;`)).toBe(1);
    expect(scanCount(`${EVAL_CALL};`)).toBe(1);
  });

  it("ignore un sink présent uniquement dans un commentaire de ligne", () => {
    expect(scanCount(`// voir ${SINK_WORD} ici\nconst a = 1;`)).toBe(0);
  });

  it("ignore un sink présent uniquement dans un commentaire de bloc", () => {
    expect(scanCount(`/* ${SINK_WORD} dans un bloc */\nconst a = 1;`)).toBe(0);
  });

  it("ignore un sink présent uniquement dans une chaîne de caractères", () => {
    expect(scanCount(`const s = "${SINK_WORD}";`)).toBe(0);
    expect(scanCount(`const s = '${SINK_WORD}';`)).toBe(0);
    expect(scanCount("const s = `" + SINK_WORD + "`;")).toBe(0);
  });

  it("détecte un sink hors chaîne même si une chaîne voisine contient le motif", () => {
    // La chaîne est neutralisée, mais l'affectation réelle doit rester vue.
    expect(scanCount(`const label = "${SINK_WORD}"; el.${SINK_WORD} = label;`)).toBe(1);
  });
});
