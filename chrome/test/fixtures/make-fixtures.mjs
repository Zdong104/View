// Generate small, deterministic test fixtures (never use private files).
//   node test/fixtures/make-fixtures.mjs
import { PDFDocument, StandardFonts, rgb } from "@cantoo/pdf-lib";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";

const here = dirname(fileURLToPath(import.meta.url));
const FIXED = new Date("2026-01-01T00:00:00Z");

async function pdf(pages) {
  const doc = await PDFDocument.create();
  doc.setTitle("View test fixture");
  doc.setAuthor("View");
  doc.setProducer("make-fixtures.mjs");
  doc.setCreator("make-fixtures.mjs");
  doc.setCreationDate(FIXED);
  doc.setModificationDate(FIXED);
  const font = await doc.embedFont(StandardFonts.Helvetica);
  for (let i = 1; i <= pages; i++) {
    const page = doc.addPage([595, 842]); // A4
    page.drawText(`View fixture page ${i} of ${pages}`, { x: 72, y: 760, size: 24, font, color: rgb(0.1, 0.1, 0.1) });
    page.drawText("The quick brown fox jumps over the lazy dog.", { x: 72, y: 720, size: 12, font });
    page.drawRectangle({ x: 72, y: 400, width: 200, height: 120, color: rgb(0.21, 0.52, 0.89) });
  }
  return doc.save({ useObjectStreams: false });
}

function crc32(buf) {
  let c, crc = 0xffffffff;
  for (const b of buf) {
    c = (crc ^ b) & 0xff;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    crc = (crc >>> 8) ^ c;
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/** A w×h RGBA PNG with a diagonal gradient. */
function png(w, h) {
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(type, "ascii"), data]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td));
    return Buffer.concat([len, td, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 6; // 8-bit RGBA
  const raw = Buffer.alloc((w * 4 + 1) * h);
  for (let y = 0; y < h; y++) {
    raw[y * (w * 4 + 1)] = 0;
    for (let x = 0; x < w; x++) {
      const o = y * (w * 4 + 1) + 1 + x * 4;
      raw[o] = (x * 255) / w; raw[o + 1] = (y * 255) / h; raw[o + 2] = 200; raw[o + 3] = 255;
    }
  }
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}

/**
 * Baseline RGB TIFF, little endian, one strip. compression 1 = none, 8 = Deflate.
 * Pixel (x, y): red ramps with x, green with y, blue 200; the top-left 4x4 block is pure red.
 */
function tiff(w, h, { compression = 1, orientation = 1 } = {}) {
  const raw = Buffer.alloc(w * h * 3);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const o = (y * w + x) * 3;
    const corner = x < 4 && y < 4;
    raw[o] = corner ? 255 : (x * 255) / w; raw[o + 1] = corner ? 0 : (y * 255) / h; raw[o + 2] = corner ? 0 : 200;
  }
  const data = compression === 8 ? deflateSync(raw) : raw;
  const tags = [
    [256, 3, 1, w], [257, 3, 1, h], [258, 3, 3, "bps"], [259, 3, 1, compression], [262, 3, 1, 2],
    [273, 4, 1, "data"], [274, 3, 1, orientation], [277, 3, 1, 3], [278, 3, 1, h], [279, 4, 1, data.length], [284, 3, 1, 1],
  ];
  const ifdSize = 2 + tags.length * 12 + 4;
  const bpsOff = 8 + ifdSize, dataOff = bpsOff + 6;
  const head = Buffer.alloc(dataOff);
  head.write("II", 0, "ascii"); head.writeUInt16LE(42, 2); head.writeUInt32LE(8, 4);
  head.writeUInt16LE(tags.length, 8);
  tags.forEach(([tag, type, count, val], i) => {
    const o = 10 + i * 12;
    head.writeUInt16LE(tag, o); head.writeUInt16LE(type, o + 2); head.writeUInt32LE(count, o + 4);
    const v = val === "bps" ? bpsOff : val === "data" ? dataOff : val;
    if (type === 3 && count === 1) head.writeUInt16LE(v, o + 8); else head.writeUInt32LE(v, o + 8);
  });
  head.writeUInt32LE(0, 8 + 2 + tags.length * 12); // no next IFD
  for (let i = 0; i < 3; i++) head.writeUInt16LE(8, bpsOff + i * 2);
  return Buffer.concat([head, data]);
}

const stl = `solid tetra
${[[[0,0,0],[1,0,0],[0,1,0]],[[0,0,0],[0,1,0],[0,0,1]],[[0,0,0],[0,0,1],[1,0,0]],[[1,0,0],[0,0,1],[0,1,0]]]
  .map((t) => `  facet normal 0 0 0\n    outer loop\n${t.map((v) => `      vertex ${v.join(" ")}`).join("\n")}\n    endloop\n  endfacet`).join("\n")}
endsolid tetra
`;

writeFileSync(join(here, "sample.pdf"), await pdf(3));
writeFileSync(join(here, "sample.png"), png(64, 48));
writeFileSync(join(here, "sample.tif"), tiff(40, 30, { compression: 8 }));
writeFileSync(join(here, "sample-rot.tif"), tiff(40, 30, { orientation: 6 }));
writeFileSync(join(here, "sample.csv"), "name,value\nalpha,1\nbeta,2\n中文,3\n");
writeFileSync(join(here, "sample.stl"), stl);
writeFileSync(join(here, "sample.txt"), "Hello from View.\n");
console.log("fixtures written to", here);
