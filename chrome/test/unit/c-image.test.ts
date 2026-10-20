// Stream C: pure image ops (op list, Instant Alpha, colour), container handling
// (sniff, EXIF patch/insert, WebP mux, APNG loop, BMP/TIFF writers) and the
// images -> PDF layout.
import assert from "node:assert/strict";
import { test } from "node:test";
import { deflateSync } from "node:zlib";
import exifr from "exifr";
import { PDFDocument } from "@cantoo/pdf-lib";
import {
  adjustColor, alphaFill, appendOp, COLOR_DEFAULTS, colorIsNeutral, fitSize, opsNeedAlpha, sizeAfterOps, type Op,
} from "../../src/viewers/image/ops.ts";
import {
  apngSetLoop, crc32, encodeBmp, encodeTiff, exifOrientation, iccDescription, insertExif, muxAnimatedWebp,
  parseContainer, patchExif, sniff,
} from "../../src/viewers/image/formats.ts";
import { imagesToPdf, layout } from "../../src/convert/pdf.ts";
import { uniqueName } from "../../src/convert/output.ts";

const px = (w: number, h: number, rgba: [number, number, number, number]) => {
  const data = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < data.length; i += 4) data.set(rgba, i);
  return { data, width: w, height: h };
};
const at = (img: { data: Uint8ClampedArray; width: number }, x: number, y: number) => [...img.data.subarray((y * img.width + x) * 4, (y * img.width + x) * 4 + 4)];

/** Minimal TIFF: IFD0 with Orientation (SHORT) and an Exif IFD with PixelX/YDimension (LONG). */
function tiff(orientation: number): Uint8Array {
  const b = new Uint8Array(8 + 2 + 2 * 12 + 4 + 2 + 2 * 12 + 4);
  const dv = new DataView(b.buffer);
  b.set([0x49, 0x49, 42, 0]); dv.setUint32(4, 8, true);
  dv.setUint16(8, 2, true);
  dv.setUint16(10, 0x0112, true); dv.setUint16(12, 3, true); dv.setUint32(14, 1, true); dv.setUint16(18, orientation, true);
  const exifIfd = 8 + 2 + 24 + 4;
  dv.setUint16(22, 0x8769, true); dv.setUint16(24, 4, true); dv.setUint32(26, 1, true); dv.setUint32(30, exifIfd, true);
  dv.setUint16(exifIfd, 2, true);
  dv.setUint16(exifIfd + 2, 0xa002, true); dv.setUint16(exifIfd + 4, 4, true); dv.setUint32(exifIfd + 6, 1, true); dv.setUint32(exifIfd + 10, 100, true);
  dv.setUint16(exifIfd + 14, 0xa003, true); dv.setUint16(exifIfd + 16, 4, true); dv.setUint32(exifIfd + 18, 1, true); dv.setUint32(exifIfd + 22, 50, true);
  return b;
}

function chunk(type: string, data: Uint8Array): Uint8Array {
  const out = new Uint8Array(12 + data.length);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, data.length);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  out.set(data, 8);
  dv.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)));
  return out;
}
const cat = (...a: Uint8Array[]) => { const o = new Uint8Array(a.reduce((n, x) => n + x.length, 0)); let p = 0; for (const x of a) { o.set(x, p); p += x.length; } return o; };

/** A real RGBA PNG (w×h, one colour). */
function png(w: number, h: number, extra: Uint8Array[] = []): Uint8Array {
  const ihdr = new Uint8Array(13);
  const dv = new DataView(ihdr.buffer);
  dv.setUint32(0, w); dv.setUint32(4, h); ihdr[8] = 8; ihdr[9] = 6;
  const raw = new Uint8Array(h * (1 + w * 4));
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) raw.set([200, 30, 30, 255], y * (1 + w * 4) + 1 + x * 4);
  return cat(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", ihdr), ...extra,
    chunk("IDAT", new Uint8Array(deflateSync(raw))), chunk("IEND", new Uint8Array()));
}

test("op list: rotations and flips merge, sizes follow the ops", () => {
  let ops: Op[] = [];
  ops = appendOp(ops, { k: "rotate", deg: 90 });
  ops = appendOp(ops, { k: "rotate", deg: 90 });
  assert.deepEqual(ops, [{ k: "rotate", deg: 180 }]);
  ops = appendOp(ops, { k: "rotate", deg: 180 });
  assert.deepEqual(ops, [], "a full turn cancels out");
  ops = appendOp(appendOp(ops, { k: "flip" }), { k: "flip" });
  assert.deepEqual(ops, []);
  ops = appendOp(appendOp(ops, { k: "flip" }), { k: "flip", vertical: true });
  assert.equal(ops.length, 2, "horizontal and vertical flips don't merge");
  const all: Op[] = [{ k: "rotate", deg: 90 }, { k: "crop", x0: 10, y0: 0, x1: 40, y1: 20 }, { k: "resize", w: 15, h: 10 }];
  assert.deepEqual(sizeAfterOps(all, 100, 50), [15, 10]);
  assert.deepEqual(sizeAfterOps(all.slice(0, 1), 100, 50), [50, 100]);
  assert.equal(opsNeedAlpha(all), false);
  assert.equal(opsNeedAlpha([{ k: "ellipse", x0: 0, y0: 0, x1: 2, y1: 2 }]), true);
  assert.deepEqual(fitSize(4000, 3000, 1000), [1000, 750]);
  assert.deepEqual(fitSize(400, 300, 0), [400, 300]);
});

test("Instant Alpha clears only the connected region within tolerance", () => {
  const img = px(10, 10, [255, 255, 255, 255]);
  for (let y = 0; y < 10; y++) img.data.set([0, 0, 0, 255], (y * 10 + 5) * 4); // a black wall at x = 5
  img.data.set([250, 250, 250, 255], (2 * 10 + 2) * 4); // within tolerance of white
  alphaFill(img, 1, 1, 25);
  assert.equal(at(img, 0, 0)[3], 0);
  assert.equal(at(img, 2, 2)[3], 0, "near-white pixel joins the region");
  assert.equal(at(img, 5, 3)[3], 255, "the wall stays");
  assert.equal(at(img, 8, 8)[3], 255, "white behind the wall is not connected");
  const t = px(3, 1, [9, 9, 9, 0]);
  alphaFill(t, 0, 0, 100);
  assert.deepEqual(t.data[7], 0); // seed always counts, transparent neighbours never extend the fill
});

test("colour adjustment matches the desktop formulas", () => {
  assert.ok(colorIsNeutral(COLOR_DEFAULTS));
  const a = px(1, 1, [200, 50, 100, 255]);
  adjustColor(a, { ...COLOR_DEFAULTS, saturation: -1 });
  const g = Math.floor((200 * 0.299 + 50 * 0.587 + 100 * 0.114) + 0.5);
  assert.deepEqual(at(a, 0, 0), [g, g, g, 255]);
  const b = px(1, 1, [60, 60, 60, 128]);
  adjustColor(b, { ...COLOR_DEFAULTS, exposure: 1 });
  assert.deepEqual(at(b, 0, 0), [120, 120, 120, 128], "exposure +1 EV doubles, alpha untouched");
  const c = px(2, 1, [100, 100, 100, 255]);
  c.data.set([200, 200, 200, 255], 4);
  adjustColor(c, { ...COLOR_DEFAULTS, auto: true });
  assert.deepEqual([at(c, 0, 0)[0], at(c, 1, 0)[0]], [0, 255], "auto levels stretch to the full range");
});

test("sniffing and header facts", () => {
  const p = png(3, 2, [chunk("pHYs", new Uint8Array([0, 0, 0x0b, 0x13, 0, 0, 0x0b, 0x13, 1]))]);
  assert.equal(sniff(p), "png");
  const info = parseContainer(p);
  assert.deepEqual([info.width, info.height, info.colorModel, info.alpha, Math.round(info.dpi![0])], [3, 2, "RGB", true, 72]);
  const actl = new Uint8Array(8); new DataView(actl.buffer).setUint32(0, 2);
  assert.equal(sniff(png(1, 1, [chunk("acTL", actl)])), "apng");
  assert.equal(sniff(new Uint8Array([0xff, 0xd8, 0xff, 0xe0])), "jpeg");
  assert.equal(sniff(new TextEncoder().encode("GIF89a")), "gif");
  assert.equal(sniff(new TextEncoder().encode('<?xml version="1.0"?>\n<svg xmlns="x">')), "svg");
  assert.equal(sniff(new TextEncoder().encode("\0\0\0\x18ftypheic")), "heic");
});

test("EXIF: orientation reset, pixel dimensions updated, re-inserted into JPEG/PNG/WebP", async () => {
  const t = tiff(6);
  assert.equal(exifOrientation(t), 6);
  const p = patchExif(t, [30, 20]);
  assert.equal(exifOrientation(p), 1);
  assert.equal(exifOrientation(t), 6, "the original is not modified");
  const tags = await exifr.parse(p, { tiff: true, exif: true, translateValues: false });
  assert.equal(tags.ExifImageWidth, 30);
  assert.equal(tags.ExifImageHeight, 20);

  const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 16, ...new TextEncoder().encode("JFIF\0"), 1, 1, 0, 0, 1, 0, 1, 0, 0, 0xff, 0xdb, 0, 2, 0xff, 0xd9]);
  const j = insertExif("jpeg", jpeg, p);
  const ji = parseContainer(j);
  assert.equal(exifOrientation(ji.exif), 1);
  assert.equal(j[20], 0xff); assert.equal(j[21], 0xe1, "APP1 follows the JFIF APP0");
  assert.equal(parseContainer(insertExif("jpeg", j, t)).exif!.length, t.length, "an existing Exif segment is replaced");

  const pn = insertExif("png", png(2, 2), p);
  assert.equal(exifOrientation(parseContainer(pn).exif), 1);

  // simple lossy WebP: VP8 chunk only -> VP8X with the EXIF flag + EXIF chunk
  const vp8 = new Uint8Array(10); vp8.set([0x9d, 0x01, 0x2a], 3); vp8[6] = 7; vp8[8] = 5;
  const riff = (body: Uint8Array) => cat(new TextEncoder().encode("RIFF"), new Uint8Array(new Uint32Array([body.length + 4]).buffer), new TextEncoder().encode("WEBP"), body);
  const webp = riff(cat(new TextEncoder().encode("VP8 "), new Uint8Array(new Uint32Array([10]).buffer), vp8));
  const w2 = insertExif("webp", webp, p);
  const wi = parseContainer(w2);
  assert.deepEqual([wi.width, wi.height, wi.lossless], [7, 5, false]);
  assert.equal(exifOrientation(wi.exif), 1);
  assert.equal(new DataView(w2.buffer).getUint32(4, true), w2.length - 8, "RIFF size updated");
});

test("animated WebP mux and APNG loop count", () => {
  const vp8l = new Uint8Array(5); vp8l[0] = 0x2f; new DataView(vp8l.buffer).setUint32(1, (3 - 1) | ((2 - 1) << 14), true);
  const frame = cat(new TextEncoder().encode("RIFF"), new Uint8Array(new Uint32Array([4 + 8 + 6]).buffer), new TextEncoder().encode("WEBP"),
    new TextEncoder().encode("VP8L"), new Uint8Array(new Uint32Array([5]).buffer), vp8l, new Uint8Array(1));
  const out = muxAnimatedWebp([{ file: frame, duration: 100 }, { file: frame, duration: 250 }], 3, 2, 0, tiff(1));
  const info = parseContainer(out);
  assert.deepEqual([info.width, info.height, info.animated, info.lossless, info.loop], [3, 2, true, true, 0]);
  assert.ok(info.exif);
  const s = String.fromCharCode(...out);
  assert.equal(s.split("ANMF").length - 1, 2);
  const firstAnmf = s.indexOf("ANMF");
  assert.equal(out[firstAnmf + 8 + 12]! | (out[firstAnmf + 8 + 13]! << 8), 100, "frame duration in ms");

  const actl = new Uint8Array(8); new DataView(actl.buffer).setUint32(0, 3);
  const a = apngSetLoop(png(1, 1, [chunk("acTL", actl)]), 4);
  const ai = parseContainer(a);
  assert.equal(ai.loop, 4);
  const at = String.fromCharCode(...a).indexOf("acTL") - 4;
  const len = new DataView(a.buffer).getUint32(at);
  assert.equal(new DataView(a.buffer).getUint32(at + 8 + len), crc32(a.subarray(at + 4, at + 8 + len)), "CRC rewritten");
});

test("BMP and TIFF writers", async () => {
  const img = px(3, 2, [10, 20, 30, 255]);
  const bmp = encodeBmp(img);
  const bi = parseContainer(bmp);
  assert.deepEqual([bi.fmt, bi.width, bi.height, bi.alpha], ["bmp", 3, 2, false]);
  assert.deepEqual([...bmp.subarray(54, 57)], [30, 20, 10], "BGR, bottom-up");
  assert.equal(bmp.length, 54 + 12 * 2, "rows padded to 4 bytes");
  const ba = parseContainer(encodeBmp(px(2, 2, [1, 2, 3, 128])));
  assert.equal(ba.alpha, true);

  const tif = encodeTiff(px(4, 3, [1, 2, 3, 100]));
  assert.equal(sniff(tif), "tiff");
  const tags = await exifr.parse(tif, { tiff: true, ifd0: true, translateValues: false, mergeOutput: true } as never);
  assert.equal(tags.ImageWidth, 4);
  assert.equal(tags.ImageHeight, 3);
  assert.equal(tags.SamplesPerPixel, 4);
});

test("ICC description (v2 'desc')", () => {
  const icc = new Uint8Array(132 + 12 + 12 + 6);
  const dv = new DataView(icc.buffer);
  dv.setUint32(128, 1);
  icc.set(new TextEncoder().encode("desc"), 132); dv.setUint32(136, 144); dv.setUint32(140, 18);
  icc.set(new TextEncoder().encode("desc"), 144); dv.setUint32(152, 5);
  icc.set(new TextEncoder().encode("sRGB\0"), 156);
  assert.equal(iccDescription(icc), "sRGB");
});

test("images -> PDF: layouts as convert.py and embedded pages", async () => {
  assert.deepEqual(layout(300, 150, [300, 300], { pageSize: "fit", marginMm: 0 }).map(Math.round), [72, 36, 72, 36]);
  assert.deepEqual(layout(150, 150, [96, 96], { pageSize: "fit", marginMm: 0 }).map(Math.round), [72, 72, 72, 72], "96 dpi means unknown -> 150");
  const [pw, ph] = layout(400, 200, null, { pageSize: "a4", marginMm: 10 });
  assert.deepEqual([Math.round(pw), Math.round(ph)], [842, 595], "landscape A4 for a wide image");
  const bytes = await imagesToPdf([
    { bytes: png(30, 20), type: "png", width: 30, height: 20, dpi: [72, 72] },
    { bytes: png(10, 40), type: "png", width: 10, height: 40 },
  ], { pageSize: "fit", marginMm: 0 });
  const doc = await PDFDocument.load(bytes);
  assert.equal(doc.getPageCount(), 2);
  assert.deepEqual(Object.values(doc.getPage(0).getSize()).map(Math.round), [30, 20]);
  assert.deepEqual(Object.values(doc.getPage(1).getSize()).map(Math.round), [5, 19]);
});

test("unique output names never overwrite", async () => {
  const taken = new Set(["a.png", "a 2.png"]);
  assert.equal(await uniqueName("a.png", (n) => taken.has(n)), "a 3.png");
  assert.equal(await uniqueName("b.png", (n) => taken.has(n)), "b.png");
});
