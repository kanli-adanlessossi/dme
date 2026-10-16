// Génère des icônes PWA de remplacement (placeholder) sans dépendance externe.
// Produit des PNG valides (carré, couleur de thème sky-700 #0369a1 avec une
// croix médicale blanche) aux tailles requises par le manifeste. Remplaçables
// par de vraies icônes de marque plus tard. Lancer : `node scripts/gen-icons.mjs`.
import { createRequire } from "node:module";
import { writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import zlib from "node:zlib";

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
void require; // conservé pour compat éventuelle ; non utilisé

const OUT_DIR = join(__dirname, "..", "public", "icons");
mkdirSync(OUT_DIR, { recursive: true });

// Couleurs (RGBA)
const BG = [3, 105, 161, 255]; // sky-700
const FG = [255, 255, 255, 255]; // croix médicale blanche

/** Construit un buffer PNG RGBA minimal pour une image size x size. */
function makePng(size, draw) {
  // Image brute : filtre 0 par ligne + size*4 octets.
  const bytesPerPixel = 4;
  const stride = size * bytesPerPixel;
  const raw = Buffer.alloc((stride + 1) * size);
  for (let y = 0; y < size; y++) {
    const rowStart = y * (stride + 1);
    raw[rowStart] = 0; // type de filtre : aucun
    for (let x = 0; x < size; x++) {
      const [r, g, b, a] = draw(x, y, size);
      const p = rowStart + 1 + x * bytesPerPixel;
      raw[p] = r;
      raw[p + 1] = g;
      raw[p + 2] = b;
      raw[p + 3] = a;
    }
  }

  const sig = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // profondeur de bits
  ihdr[9] = 6; // type de couleur : RGBA
  ihdr[10] = 0; // compression
  ihdr[11] = 0; // filtre
  ihdr[12] = 0; // entrelacement

  const idat = zlib.deflateSync(raw, { level: 9 });

  return Buffer.concat([
    sig,
    chunk("IHDR", ihdr),
    chunk("IDAT", idat),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function chunk(type, data) {
  const typeBuf = Buffer.from(type, "ascii");
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])) >>> 0, 0);
  return Buffer.concat([len, typeBuf, data, crcBuf]);
}

// Table CRC32 (PNG).
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) {
    c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

// Dessine une croix médicale centrée (barre horizontale + verticale).
function drawIcon(x, y, size) {
  const t = size * 0.18; // demi-épaisseur de la croix
  const arm = size * 0.32; // demi-longueur du bras
  const cx = size / 2;
  const cy = size / 2;
  const dx = Math.abs(x - cx);
  const dy = Math.abs(y - cy);
  const inVertical = dx <= t && dy <= arm;
  const inHorizontal = dy <= t && dx <= arm;
  return inVertical || inHorizontal ? FG : BG;
}

// Dessine un fond maskable : marge de sécurité autour de la croix (plus petite).
function drawMaskable(x, y, size) {
  const t = size * 0.13;
  const arm = size * 0.23;
  const cx = size / 2;
  const cy = size / 2;
  const dx = Math.abs(x - cx);
  const dy = Math.abs(y - cy);
  const inVertical = dx <= t && dy <= arm;
  const inHorizontal = dy <= t && dx <= arm;
  return inVertical || inHorizontal ? FG : BG;
}

const targets = [
  { name: "icon-192.png", size: 192, draw: drawIcon },
  { name: "icon-512.png", size: 512, draw: drawIcon },
  { name: "maskable-512.png", size: 512, draw: drawMaskable },
  { name: "apple-touch-icon.png", size: 180, draw: drawIcon },
];

for (const { name, size, draw } of targets) {
  const png = makePng(size, draw);
  writeFileSync(join(OUT_DIR, name), png);
  console.log(`écrit ${name} (${size}x${size}, ${png.length} octets)`);
}

// Favicon SVG simple (vectoriel, net à toute taille).
const faviconSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="6" fill="#0369a1"/>
  <path d="M13 7h6v6h6v6h-6v6h-6v-6H7v-6h6z" fill="#ffffff"/>
</svg>
`;
writeFileSync(join(__dirname, "..", "public", "favicon.svg"), faviconSvg);
console.log("écrit favicon.svg");
