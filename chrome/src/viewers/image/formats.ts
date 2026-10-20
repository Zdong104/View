// Byte-level image container handling, no DOM: sniffing, header facts for Get Info,
// EXIF/ICC extraction, EXIF re-insertion on save (a canvas encoder drops it), the
// animated WebP RIFF muxer, APNG loop patching, and small BMP/TIFF encoders.
// Pure functions over Uint8Array so they run in the worker and in unit tests.
import type { Pixels } from "./ops.ts";

export type Fmt = "png" | "apng" | "jpeg" | "gif" | "webp" | "bmp" | "ico" | "avif" | "svg" | "tiff"
  | "heic" | "jxl" | "unknown";

export const FMT_LABEL: Record<Fmt, string> = {
  png: "PNG", apng: "APNG", jpeg: "JPEG", gif: "GIF", webp: "WebP", bmp: "BMP", ico: "ICO",
  avif: "AVIF", svg: "SVG", tiff: "TIFF", heic: "HEIC", jxl: "JPEG XL", unknown: "Image",
};

export const FMT_MIME: Partial<Record<Fmt, string>> = {
  png: "image/png", apng: "image/png", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp",
  bmp: "image/bmp", ico: "image/x-icon", avif: "image/avif", svg: "image/svg+xml", tiff: "image/tiff",
};

// ---- small helpers ---------------------------------------------------------------

const ascii = (b: Uint8Array, o: number, n: number) => String.fromCharCode(...b.subarray(o, o + n));
const u16be = (b: Uint8Array, o: number) => (b[o]! << 8) | b[o + 1]!;
const u32be = (b: Uint8Array, o: number) => ((b[o]! << 24) >>> 0) + (b[o + 1]! << 16) + (b[o + 2]! << 8) + b[o + 3]!;
const u16le = (b: Uint8Array, o: number) => b[o]! | (b[o + 1]! << 8);
const u24le = (b: Uint8Array, o: number) => b[o]! | (b[o + 1]! << 8) | (b[o + 2]! << 16);
const u32le = (b: Uint8Array, o: number) => (b[o]! | (b[o + 1]! << 8) | (b[o + 2]! << 16)) + ((b[o + 3]! << 24) >>> 0);
const i32le = (b: Uint8Array, o: number) => b[o]! | (b[o + 1]! << 8) | (b[o + 2]! << 16) | (b[o + 3]! << 24);

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(b: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < b.length; i++) c = CRC_TABLE[(c ^ b[i]!) & 0xff]! ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function pngChunk(type: string, payload: Uint8Array): Uint8Array {
  const out = new Uint8Array(12 + payload.length);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, payload.length);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  out.set(payload, 8);
  dv.setUint32(8 + payload.length, crc32(out.subarray(4, 8 + payload.length)));
  return out;
}

function riffChunk(type: string, payload: Uint8Array): Uint8Array {
  const pad = payload.length & 1;
  const out = new Uint8Array(8 + payload.length + pad);
  for (let i = 0; i < 4; i++) out[i] = type.charCodeAt(i);
  new DataView(out.buffer).setUint32(4, payload.length, true);
  out.set(payload, 8);
  return out;
}

interface PngChunkRef { type: string; start: number; data: Uint8Array; end: number }
function* pngChunks(b: Uint8Array): Generator<PngChunkRef> {
  let o = 8;
  while (o + 12 <= b.length) {
    const len = u32be(b, o);
    const type = ascii(b, o + 4, 4);
    if (o + 12 + len > b.length) return;
    yield { type, start: o, data: b.subarray(o + 8, o + 8 + len), end: o + 12 + len };
    if (type === "IEND") return;
    o += 12 + len;
  }
}

interface RiffChunkRef { type: string; start: number; data: Uint8Array; end: number }
function* riffChunks(b: Uint8Array, from = 12, to = b.length): Generator<RiffChunkRef> {
  let o = from;
  while (o + 8 <= to) {
    const len = u32le(b, o + 4);
    const type = ascii(b, o, 4);
    const end = Math.min(to, o + 8 + len + (len & 1));
    yield { type, start: o, data: b.subarray(o + 8, Math.min(to, o + 8 + len)), end };
    o = end;
  }
}

// ---- sniffing -------------------------------------------------------------------

export function sniff(b: Uint8Array): Fmt {
  if (b[0] === 0x89 && ascii(b, 1, 3) === "PNG") {
    for (const c of pngChunks(b)) {
      if (c.type === "acTL") return "apng";
      if (c.type === "IDAT") break;
    }
    return "png";
  }
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "jpeg";
  if (ascii(b, 0, 4) === "GIF8") return "gif";
  if (ascii(b, 0, 4) === "RIFF" && ascii(b, 8, 4) === "WEBP") return "webp";
  if (ascii(b, 0, 2) === "BM") return "bmp";
  if (b[0] === 0 && b[1] === 0 && b[2] === 1 && b[3] === 0) return "ico";
  if ((b[0] === 0x49 && b[1] === 0x49 && b[2] === 42 && b[3] === 0) || (b[0] === 0x4d && b[1] === 0x4d && b[2] === 0 && b[3] === 42)) return "tiff";
  if (ascii(b, 4, 4) === "ftyp") {
    const brand = ascii(b, 8, 4);
    if (brand === "avif" || brand === "avis") return "avif";
    if (/^(heic|heix|hevc|heim|heis|mif1|msf1)$/.test(brand)) return "heic";
  }
  if ((b[0] === 0xff && b[1] === 0x0a) || ascii(b, 4, 4) === "JXL ") return "jxl";
  const head = new TextDecoder().decode(b.subarray(0, 512)).trimStart();
  if (/^(<\?xml[^>]*>\s*)?(<!--[\s\S]*?-->\s*)*(<!DOCTYPE svg[^>]*>\s*)?<svg[\s>]/i.test(head) || /<svg[\s>]/i.test(head)) return "svg";
  return "unknown";
}

// ---- header facts ------------------------------------------------------------------

export interface ContainerInfo {
  fmt: Fmt;
  width?: number;
  height?: number;
  bitDepth?: number;
  colorModel?: string;   // RGB, Gray, Indexed, CMYK, YCbCr
  alpha?: boolean;
  dpi?: [number, number];
  /** EXIF as a TIFF stream (starts with II* / MM*), as stored in the file. */
  exif?: Uint8Array;
  /** Raw ICC profile, or (PNG iCCP) the zlib-compressed one. */
  icc?: Uint8Array;
  iccCompressed?: boolean;
  /** Container says animated (APNG acTL, WebP ANIM flag); GIF frames are counted by the decoder. */
  animated?: boolean;
  /** WebP: the first (or only) frame is VP8L. */
  lossless?: boolean;
  loop?: number;
}

const isTiff = (b: Uint8Array) => (b[0] === 0x49 && b[1] === 0x49) || (b[0] === 0x4d && b[1] === 0x4d);

export function parseContainer(b: Uint8Array): ContainerInfo {
  const fmt = sniff(b);
  const info: ContainerInfo = { fmt };
  try {
    if (fmt === "png" || fmt === "apng") parsePng(b, info);
    else if (fmt === "jpeg") parseJpeg(b, info);
    else if (fmt === "gif") {
      info.width = u16le(b, 6); info.height = u16le(b, 8);
      info.colorModel = "Indexed"; info.bitDepth = 8;
    } else if (fmt === "webp") parseWebp(b, info);
    else if (fmt === "bmp") {
      info.width = i32le(b, 18); info.height = Math.abs(i32le(b, 22));
      const bpp = u16le(b, 28);
      info.bitDepth = bpp >= 24 ? 8 : bpp;
      info.colorModel = bpp <= 8 ? "Indexed" : "RGB";
      info.alpha = bpp === 32;
      const ppm = i32le(b, 38);
      if (ppm > 0) info.dpi = [ppm * 0.0254, i32le(b, 42) * 0.0254];
    } else if (fmt === "ico") {
      info.width = b[6] || 256; info.height = b[7] || 256; info.colorModel = "RGB"; info.alpha = true;
    } else if (fmt === "tiff") {
      info.exif = b;
    }
  } catch { /* truncated or odd file: keep what we have */ }
  return info;
}

function parsePng(b: Uint8Array, info: ContainerInfo): void {
  let seenIdat = false;
  for (const c of pngChunks(b)) {
    const d = c.data;
    if (c.type === "IHDR") {
      info.width = u32be(d, 0); info.height = u32be(d, 4); info.bitDepth = d[8];
      const ct = d[9]!;
      info.colorModel = ct === 0 || ct === 4 ? "Gray" : ct === 3 ? "Indexed" : "RGB";
      info.alpha = ct === 4 || ct === 6;
    } else if (c.type === "tRNS") info.alpha = true;
    else if (c.type === "pHYs" && d[8] === 1) info.dpi = [u32be(d, 0) * 0.0254, u32be(d, 4) * 0.0254];
    else if (c.type === "eXIf") info.exif = d.slice();
    else if (c.type === "iCCP") {
      const z = d.indexOf(0);
      if (z > 0) { info.icc = d.slice(z + 2); info.iccCompressed = true; }
    } else if (c.type === "acTL" && !seenIdat) { info.animated = u32be(d, 0) > 1; info.loop = u32be(d, 4); }
    else if (c.type === "IDAT") seenIdat = true;
  }
}

function parseJpeg(b: Uint8Array, info: ContainerInfo): void {
  const icc: [number, Uint8Array][] = [];
  let o = 2;
  while (o + 4 <= b.length) {
    if (b[o] !== 0xff) { o++; continue; }
    const m = b[o + 1]!;
    if (m === 0xd8 || (m >= 0xd0 && m <= 0xd7) || m === 0x01 || m === 0xff) { o += m === 0xff ? 1 : 2; continue; }
    if (m === 0xd9 || m === 0xda) break;
    const len = u16be(b, o + 2);
    const seg = b.subarray(o + 4, o + 2 + len);
    if (m === 0xe0 && ascii(seg, 0, 5) === "JFIF\0") {
      const units = seg[7], x = u16be(seg, 8), y = u16be(seg, 10);
      if (units === 1 && x > 1) info.dpi = [x, y];
      else if (units === 2 && x > 1) info.dpi = [x * 2.54, y * 2.54];
    } else if (m === 0xe1 && ascii(seg, 0, 6) === "Exif\0\0" && !info.exif) {
      info.exif = seg.slice(6);
    } else if (m === 0xe2 && ascii(seg, 0, 12) === "ICC_PROFILE\0") {
      icc.push([seg[12]!, seg.subarray(14)]);
    } else if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) {
      info.bitDepth = seg[0]; info.height = u16be(seg, 1); info.width = u16be(seg, 3);
      const nc = seg[5];
      info.colorModel = nc === 1 ? "Gray" : nc === 4 ? "CMYK" : "RGB";
      info.alpha = false;
    }
    o += 2 + len;
  }
  if (icc.length) info.icc = concat(icc.sort((a, c) => a[0] - c[0]).map((x) => x[1]));
}

function parseWebp(b: Uint8Array, info: ContainerInfo): void {
  info.colorModel = "RGB";
  info.bitDepth = 8;
  for (const c of riffChunks(b)) {
    const d = c.data;
    if (c.type === "VP8X") {
      const flags = d[0]!;
      info.alpha = !!(flags & 0x10);
      info.animated = !!(flags & 0x02);
      info.width = u24le(d, 4) + 1; info.height = u24le(d, 7) + 1;
    } else if (c.type === "VP8 " && info.lossless === undefined) {
      info.lossless = false;
      if (!info.width) { info.width = u16le(d, 6) & 0x3fff; info.height = u16le(d, 8) & 0x3fff; }
    } else if (c.type === "VP8L" && info.lossless === undefined) {
      info.lossless = true;
      const bits = u32le(d, 1);
      if (!info.width) {
        info.width = (bits & 0x3fff) + 1; info.height = ((bits >>> 14) & 0x3fff) + 1;
        info.alpha = !!((bits >>> 28) & 1);
      }
    } else if (c.type === "ANIM") info.loop = u16le(d, 4);
    else if (c.type === "ANMF" && info.lossless === undefined) {
      for (const s of riffChunks(b, c.start + 24, c.end)) {
        if (s.type === "VP8L") { info.lossless = true; break; }
        if (s.type === "VP8 ") { info.lossless = false; break; }
      }
    } else if (c.type === "EXIF") {
      // Some writers keep the JPEG "Exif\0\0" prefix inside the chunk.
      info.exif = (ascii(d, 0, 6) === "Exif\0\0" ? d.subarray(6) : d).slice();
    } else if (c.type === "ICCP") info.icc = d.slice();
  }
}

/** Inflate a zlib stream (PNG iCCP) with the built-in DecompressionStream. */
export async function inflate(z: Uint8Array): Promise<Uint8Array> {
  const s = new Blob([z as BlobPart]).stream().pipeThrough(new DecompressionStream("deflate"));
  return new Uint8Array(await new Response(s).arrayBuffer());
}

/** Profile description of an ICC profile ('desc' v2 text or v4 'mluc'). */
export function iccDescription(icc: Uint8Array): string | null {
  if (icc.length < 132) return null;
  const n = u32be(icc, 128);
  for (let i = 0; i < n; i++) {
    const t = 132 + i * 12;
    if (ascii(icc, t, 4) !== "desc") continue;
    const off = u32be(icc, t + 4);
    const type = ascii(icc, off, 4);
    if (type === "desc") {
      const len = u32be(icc, off + 8);
      return ascii(icc, off + 12, Math.max(0, len - 1)).trim() || null;
    }
    if (type === "mluc") {
      const len = u32be(icc, off + 20), so = u32be(icc, off + 24);
      let s = "";
      for (let k = 0; k + 1 < len; k += 2) s += String.fromCharCode(u16be(icc, off + so + k));
      return s.replace(/\0+$/, "").trim() || null;
    }
  }
  return null;
}

// ---- EXIF (TIFF stream) patching -----------------------------------------------------

/**
 * Copy of a TIFF/EXIF stream with Orientation forced to 1 (the pixels were made
 * upright on decode) and, when given, PixelX/YDimension updated to the saved size.
 */
export function patchExif(tiff: Uint8Array, size?: [number, number]): Uint8Array {
  const b = tiff.slice();
  if (b.length < 8 || !isTiff(b)) return b;
  const le = b[0] === 0x49;
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  const r16 = (o: number) => dv.getUint16(o, le), r32 = (o: number) => dv.getUint32(o, le);
  const set = (entry: number, v: number) => {
    const type = r16(entry + 2);
    if (type === 3) dv.setUint16(entry + 8, v, le);
    else if (type === 4) dv.setUint32(entry + 8, v, le);
  };
  const walk = (ifd: number, depth: number) => {
    if (depth > 4 || ifd + 2 > b.length) return;
    const n = r16(ifd);
    for (let i = 0; i < n; i++) {
      const e = ifd + 2 + i * 12;
      if (e + 12 > b.length) return;
      const tag = r16(e);
      if (tag === 0x0112 && depth === 0) set(e, 1);
      else if (tag === 0xa002 && size) set(e, size[0]);
      else if (tag === 0xa003 && size) set(e, size[1]);
      else if (tag === 0x8769) walk(r32(e + 8), depth + 1);
    }
  };
  try { walk(r32(4), 0); } catch { /* malformed: return what we patched */ }
  return b;
}

/** Orientation tag (1..8) of a TIFF/EXIF stream, 1 if absent. */
export function exifOrientation(tiff: Uint8Array | undefined): number {
  if (!tiff || tiff.length < 8 || !isTiff(tiff)) return 1;
  const le = tiff[0] === 0x49;
  const dv = new DataView(tiff.buffer, tiff.byteOffset, tiff.byteLength);
  try {
    const ifd = dv.getUint32(4, le);
    const n = dv.getUint16(ifd, le);
    for (let i = 0; i < n; i++) {
      const e = ifd + 2 + i * 12;
      if (dv.getUint16(e, le) === 0x0112) return dv.getUint16(e + 8, le);
    }
  } catch { /* fallthrough */ }
  return 1;
}

// ---- EXIF insertion ------------------------------------------------------------------

/** Re-attach EXIF (a TIFF stream) to freshly encoded bytes of format `fmt`. */
export function insertExif(fmt: Fmt, bytes: Uint8Array, tiff: Uint8Array): Uint8Array {
  if (!tiff.length) return bytes;
  if (fmt === "jpeg") return jpegWithExif(bytes, tiff);
  if (fmt === "png" || fmt === "apng") return pngWithExif(bytes, tiff);
  if (fmt === "webp") return webpWithExif(bytes, tiff);
  return bytes;
}

function jpegWithExif(b: Uint8Array, tiff: Uint8Array): Uint8Array {
  const payload = concat([new TextEncoder().encode("Exif\0\0"), tiff]);
  if (payload.length + 2 > 0xffff) return b; // can't fit one APP1 segment
  const seg = new Uint8Array(4 + payload.length);
  seg[0] = 0xff; seg[1] = 0xe1; seg[2] = (payload.length + 2) >> 8; seg[3] = (payload.length + 2) & 0xff;
  seg.set(payload, 4);
  // Keep SOI and a leading JFIF APP0, drop any existing Exif APP1, insert ours.
  const parts: Uint8Array[] = [b.subarray(0, 2)];
  let o = 2;
  let inserted = false;
  while (o + 4 <= b.length && b[o] === 0xff) {
    const m = b[o + 1]!;
    if (m < 0xe0 || m > 0xef) break;
    const len = u16be(b, o + 2);
    const isExif = m === 0xe1 && ascii(b, o + 4, 6) === "Exif\0\0";
    if (!(m === 0xe0) && !inserted) { parts.push(seg); inserted = true; }
    if (!isExif) parts.push(b.subarray(o, o + 2 + len));
    o += 2 + len;
  }
  if (!inserted) parts.push(seg);
  parts.push(b.subarray(o));
  return concat(parts);
}

function pngWithExif(b: Uint8Array, tiff: Uint8Array): Uint8Array {
  const parts: Uint8Array[] = [b.subarray(0, 8)];
  let done = false;
  for (const c of pngChunks(b)) {
    if (c.type === "eXIf") continue;
    if (!done && (c.type === "IDAT" || c.type === "acTL" || c.type === "fcTL")) { parts.push(pngChunk("eXIf", tiff)); done = true; }
    parts.push(b.subarray(c.start, c.end));
  }
  return concat(parts);
}

function webpWithExif(b: Uint8Array, tiff: Uint8Array): Uint8Array {
  const chunks = [...riffChunks(b)].filter((c) => c.type !== "EXIF");
  let vp8x = chunks.find((c) => c.type === "VP8X")?.data.slice();
  const body: Uint8Array[] = [];
  if (!vp8x) {
    // Simple format (VP8 / VP8L only): build the extended header.
    const info: ContainerInfo = { fmt: "webp" };
    parseWebp(b, info);
    vp8x = new Uint8Array(10);
    if (info.alpha) vp8x[0]! |= 0x10;
    const w = (info.width ?? 1) - 1, h = (info.height ?? 1) - 1;
    vp8x[4] = w & 0xff; vp8x[5] = (w >> 8) & 0xff; vp8x[6] = (w >> 16) & 0xff;
    vp8x[7] = h & 0xff; vp8x[8] = (h >> 8) & 0xff; vp8x[9] = (h >> 16) & 0xff;
  }
  vp8x[0]! |= 0x08;
  body.push(riffChunk("VP8X", vp8x));
  for (const c of chunks) if (c.type !== "VP8X") body.push(b.subarray(c.start, c.end));
  body.push(riffChunk("EXIF", tiff));
  return riffWebp(body);
}

function riffWebp(body: Uint8Array[]): Uint8Array {
  const payload = concat(body);
  const head = new Uint8Array(12);
  head.set([0x52, 0x49, 0x46, 0x46]); // RIFF
  new DataView(head.buffer).setUint32(4, payload.length + 4, true);
  head.set([0x57, 0x45, 0x42, 0x50], 8); // WEBP
  return concat([head, payload]);
}

// ---- animated WebP muxer -------------------------------------------------------------

/** Image chunks of a single-frame WebP (ALPH + VP8, or VP8L) for an ANMF payload. */
export function webpFrameData(file: Uint8Array): { data: Uint8Array; alpha: boolean; lossless: boolean } {
  const parts: Uint8Array[] = [];
  let alpha = false, lossless = false;
  for (const c of riffChunks(file)) {
    if (c.type === "ALPH") { parts.push(file.subarray(c.start, c.end)); alpha = true; }
    else if (c.type === "VP8 ") parts.push(file.subarray(c.start, c.end));
    else if (c.type === "VP8L") {
      parts.push(file.subarray(c.start, c.end));
      lossless = true;
      alpha ||= !!((u32le(c.data, 1) >>> 28) & 1);
    }
  }
  if (!parts.length) throw new Error("not a WebP image");
  return { data: concat(parts), alpha, lossless };
}

/**
 * Assemble an animated WebP from single-frame WebP files (all `w`×`h`, full-canvas
 * frames, no blending). `loop` 0 = forever. Durations in ms (WebP stores 24-bit ms).
 */
export function muxAnimatedWebp(frames: { file: Uint8Array; duration: number }[], w: number, h: number,
  loop: number, exif?: Uint8Array): Uint8Array {
  const put24 = (a: Uint8Array, o: number, v: number) => { a[o] = v & 0xff; a[o + 1] = (v >> 8) & 0xff; a[o + 2] = (v >> 16) & 0xff; };
  const anmf: Uint8Array[] = [];
  let anyAlpha = false;
  for (const f of frames) {
    const { data, alpha } = webpFrameData(f.file);
    anyAlpha ||= alpha;
    const head = new Uint8Array(16);
    put24(head, 6, w - 1); put24(head, 9, h - 1);
    put24(head, 12, Math.min(0xffffff, Math.max(0, Math.round(f.duration))));
    head[15] = 0x02; // do not blend, no dispose: each frame is the full composited canvas
    anmf.push(riffChunk("ANMF", concat([head, data])));
  }
  const vp8x = new Uint8Array(10);
  vp8x[0] = 0x02 | (anyAlpha ? 0x10 : 0) | (exif?.length ? 0x08 : 0);
  put24(vp8x, 4, w - 1); put24(vp8x, 7, h - 1);
  const anim = new Uint8Array(6); // background colour (transparent) + loop count
  anim[4] = loop & 0xff; anim[5] = (loop >> 8) & 0xff;
  return riffWebp([riffChunk("VP8X", vp8x), riffChunk("ANIM", anim), ...anmf,
    ...(exif?.length ? [riffChunk("EXIF", exif)] : [])]);
}

/** Set the APNG play count (acTL num_plays; 0 = forever). */
export function apngSetLoop(b: Uint8Array, loop: number): Uint8Array {
  const out = b.slice();
  for (const c of pngChunks(out)) {
    if (c.type === "IDAT") break;
    if (c.type !== "acTL") continue;
    const dv = new DataView(out.buffer, out.byteOffset);
    dv.setUint32(c.start + 12, loop);
    dv.setUint32(c.start + 16, crc32(out.subarray(c.start + 4, c.start + 16)));
  }
  return out;
}

// ---- encoders the canvas doesn't have -------------------------------------------------

const isOpaque = (d: Uint8ClampedArray) => { for (let i = 3; i < d.length; i += 4) if (d[i] !== 255) return false; return true; };

/** BMP: 24-bit when opaque, else 32-bit BI_BITFIELDS with a BITMAPV4HEADER (alpha mask). */
export function encodeBmp(px: Pixels): Uint8Array {
  const { width: w, height: h, data } = px;
  const alpha = !isOpaque(data);
  const bpp = alpha ? 4 : 3;
  const stride = Math.ceil((w * bpp) / 4) * 4;
  const hdr = alpha ? 108 : 40;
  const off = 14 + hdr;
  const out = new Uint8Array(off + stride * h);
  const dv = new DataView(out.buffer);
  out[0] = 0x42; out[1] = 0x4d;
  dv.setUint32(2, out.length, true);
  dv.setUint32(10, off, true);
  dv.setUint32(14, hdr, true);
  dv.setInt32(18, w, true);
  dv.setInt32(22, h, true); // bottom-up
  dv.setUint16(26, 1, true);
  dv.setUint16(28, bpp * 8, true);
  dv.setUint32(30, alpha ? 3 : 0, true);
  dv.setUint32(34, stride * h, true);
  dv.setInt32(38, 2835, true); dv.setInt32(42, 2835, true); // 72 dpi
  if (alpha) {
    dv.setUint32(54, 0x00ff0000, true); dv.setUint32(58, 0x0000ff00, true);
    dv.setUint32(62, 0x000000ff, true); dv.setUint32(66, 0xff000000, true);
    dv.setUint32(70, 0x73524742, true); // 'sRGB'
  }
  for (let y = 0; y < h; y++) {
    let o = off + (h - 1 - y) * stride;
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      out[o++] = data[i + 2]!; out[o++] = data[i + 1]!; out[o++] = data[i]!;
      if (alpha) out[o++] = data[i + 3]!;
    }
  }
  return out;
}

/** Baseline uncompressed TIFF (RGB, or RGBA with unassociated alpha), little endian. */
export function encodeTiff(px: Pixels, dpi = 72): Uint8Array {
  const { width: w, height: h, data } = px;
  const alpha = !isOpaque(data);
  const spp = alpha ? 4 : 3;
  const pixBytes = w * h * spp;
  const tags: [number, number, number, number | number[]][] = [ // tag, type, count, value
    [256, 4, 1, w], [257, 4, 1, h], [258, 3, spp, new Array(spp).fill(8)], [259, 3, 1, 1],
    [262, 3, 1, 2], [273, 4, 1, 0], [277, 3, 1, spp], [278, 4, 1, h], [279, 4, 1, pixBytes],
    [282, 5, 1, [dpi, 1]], [283, 5, 1, [dpi, 1]], [284, 3, 1, 1], [296, 3, 1, 2],
    ...(alpha ? [[338, 3, 1, 2] as [number, number, number, number]] : []),
  ];
  const ifdOff = 8;
  const ifdLen = 2 + tags.length * 12 + 4;
  let extra = ifdOff + ifdLen;
  const extraParts: [number, number[] , number][] = [];
  const entries: Uint8Array = new Uint8Array(ifdLen);
  const edv = new DataView(entries.buffer);
  edv.setUint16(0, tags.length, true);
  const typeSize: Record<number, number> = { 3: 2, 4: 4, 5: 8 };
  tags.forEach(([tag, type, count, value], i) => {
    const e = 2 + i * 12;
    edv.setUint16(e, tag, true); edv.setUint16(e + 2, type, true); edv.setUint32(e + 4, count, true);
    const vals = Array.isArray(value) ? value : [value];
    const size = typeSize[type]! * count;
    if (size <= 4) {
      vals.forEach((v, k) => (type === 3 ? edv.setUint16(e + 8 + k * 2, v, true) : edv.setUint32(e + 8, v, true)));
    } else {
      edv.setUint32(e + 8, extra, true);
      extraParts.push([type, vals, extra]);
      extra += size;
    }
  });
  const pixOff = extra;
  // StripOffsets (tag 273) points at the pixels.
  const stripEntry = 2 + tags.findIndex((t) => t[0] === 273) * 12;
  edv.setUint32(stripEntry + 8, pixOff, true);
  const out = new Uint8Array(pixOff + pixBytes);
  const dv = new DataView(out.buffer);
  out.set([0x49, 0x49, 42, 0]);
  dv.setUint32(4, ifdOff, true);
  out.set(entries, ifdOff);
  for (const [type, vals, at] of extraParts) {
    vals.forEach((v, k) => (type === 3 ? dv.setUint16(at + k * 2, v, true) : dv.setUint32(at + k * 4, v, true)));
  }
  let o = pixOff;
  for (let i = 0; i < data.length; i += 4) {
    out[o++] = data[i]!; out[o++] = data[i + 1]!; out[o++] = data[i + 2]!;
    if (alpha) out[o++] = data[i + 3]!;
  }
  return out;
}
