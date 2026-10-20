// Image edit model: an ordered op list applied to the upright base image, so
// display, save, export, batch convert and print share one code path. Port of
// preview/image_ops.py (op tuples -> tagged objects). Everything here is pure
// (no DOM, no canvas) so it runs in the worker and in unit tests.
import type { Annotation } from "../../markup/api.ts";

export interface ColorParams {
  auto: boolean;
  exposure: number;   // EV, -2..2
  contrast: number;   // -1..1
  highlights: number; // -1..1
  shadows: number;    // -1..1
  saturation: number; // -1..1
  temperature: number;// -1..1
  tint: number;       // -1..1
  sepia: number;      // 0..1
  sharpness: number;  // 0..1
}

export const COLOR_DEFAULTS: Readonly<ColorParams> = {
  auto: false, exposure: 0, contrast: 0, highlights: 0, shadows: 0, saturation: 0,
  temperature: 0, tint: 0, sepia: 0, sharpness: 0,
};

/** Slider spec of the Adjust Color panel (key, label, min, max), as on the desktop. */
export const COLOR_SLIDERS: readonly [Exclude<keyof ColorParams, "auto">, string, number, number][] = [
  ["exposure", "Exposure", -2, 2], ["contrast", "Contrast", -1, 1],
  ["highlights", "Highlights", -1, 1], ["shadows", "Shadows", -1, 1],
  ["saturation", "Saturation", -1, 1], ["temperature", "Temperature", -1, 1],
  ["tint", "Tint", -1, 1], ["sepia", "Sepia", 0, 1], ["sharpness", "Sharpness", 0, 1],
];

export const colorIsNeutral = (p: Partial<ColorParams>): boolean =>
  (Object.keys(COLOR_DEFAULTS) as (keyof ColorParams)[]).every((k) => (p[k] ?? COLOR_DEFAULTS[k]) === COLOR_DEFAULTS[k]);

export type Op =
  | { k: "rotate"; deg: 90 | 180 | 270 }          // clockwise
  | { k: "flip"; vertical?: boolean }             // mirror (horizontal by default)
  | { k: "resize"; w: number; h: number }
  | { k: "crop"; x0: number; y0: number; x1: number; y1: number } // in the image at that point
  | { k: "ellipse"; x0: number; y0: number; x1: number; y1: number } // crop + transparent outside
  | { k: "alpha"; x: number; y: number; tol: number } // Instant Alpha flood fill, tol 0..100
  | { k: "color"; p: ColorParams }
  // Markup flattened into the image (annotations in the image at that point, 1 unit = 1 px).
  | { k: "markup"; anns: Annotation[] };

/** What the worker receives: markup ops are pre-rasterized on the main thread. */
export type WorkerOp = Exclude<Op, { k: "markup" }> | { k: "layer"; bmp: ImageBitmap };

/** Append `op`, merging consecutive rotations / flips (image_view._append_op). */
export function appendOp(ops: readonly Op[], op: Op): Op[] {
  const last = ops[ops.length - 1];
  if (last?.k === "rotate" && op.k === "rotate") {
    const r = (last.deg + op.deg) % 360;
    return [...ops.slice(0, -1), ...(r ? [{ k: "rotate", deg: r as 90 | 180 | 270 } as Op] : [])];
  }
  if (last?.k === "flip" && op.k === "flip" && !!last.vertical === !!op.vertical) return ops.slice(0, -1);
  return [...ops, op];
}

/** Size of the image after `op`. */
export function sizeAfter(op: Op | WorkerOp, w: number, h: number): [number, number] {
  switch (op.k) {
    case "rotate": return op.deg === 180 ? [w, h] : [h, w];
    case "resize": return [Math.max(1, op.w), Math.max(1, op.h)];
    case "crop": case "ellipse": return [Math.max(1, op.x1 - op.x0), Math.max(1, op.y1 - op.y0)];
    default: return [w, h];
  }
}

export function sizeAfterOps(ops: readonly (Op | WorkerOp)[], w: number, h: number): [number, number] {
  let s: [number, number] = [w, h];
  for (const op of ops) s = sizeAfter(op, s[0], s[1]);
  return s;
}

export const opsNeedAlpha = (ops: readonly (Op | WorkerOp)[]): boolean =>
  ops.some((o) => o.k === "alpha" || o.k === "ellipse");

/** Short op names for Get Info ("Edits": rotate, crop, color). */
export const opName = (o: Op): string => (o.k === "flip" && o.vertical ? "flip vertical" : o.k);

// ---- pixel operations (RGBA8, row-major, like ImageData) --------------------------

export interface Pixels { data: Uint8ClampedArray; width: number; height: number }

/**
 * Instant Alpha: make the 4-connected region of colours within `tol` (0..100 % of the
 * channel range, compared per channel against the seed colour) transparent. Fully
 * transparent pixels never join the region (image_ops._alpha_fill). Scanline fill.
 */
export function alphaFill(img: Pixels, sx: number, sy: number, tol: number): void {
  const { data, width: w, height: h } = img;
  const x0 = Math.min(Math.max(Math.trunc(sx), 0), w - 1);
  const y0 = Math.min(Math.max(Math.trunc(sy), 0), h - 1);
  const si = (y0 * w + x0) * 4;
  const r0 = data[si]!, g0 = data[si + 1]!, b0 = data[si + 2]!;
  const t = tol * 2.55;
  const seen = new Uint8Array(w * h);
  const ok = (p: number): boolean => {
    if (seen[p]) return false;
    const i = p * 4;
    return data[i + 3]! > 0 && Math.abs(data[i]! - r0) <= t && Math.abs(data[i + 1]! - g0) <= t &&
      Math.abs(data[i + 2]! - b0) <= t;
  };
  const stack: number[] = [y0 * w + x0];
  let first = true; // the seed is always part of the region (cand[y, x] = True)
  while (stack.length) {
    const p = stack.pop()!;
    const y = (p / w) | 0;
    let l = p - y * w, r = l;
    if (!first && !ok(p)) continue;
    first = false;
    while (l > 0 && ok(y * w + l - 1)) l--;
    while (r < w - 1 && ok(y * w + r + 1)) r++;
    for (let x = l; x <= r; x++) {
      const q = y * w + x;
      seen[q] = 1;
      data[q * 4 + 3] = 0;
    }
    for (const ny of [y - 1, y + 1]) {
      if (ny < 0 || ny >= h) continue;
      let inRun = false;
      for (let x = l; x <= r; x++) {
        const q = ny * w + x;
        const good = ok(q);
        if (good && !inRun) stack.push(q);
        inRun = good;
      }
    }
  }
}

/** 0.5 / 99.5 percentiles per channel (Auto Levels), from 256-bin histograms. */
function levels(d: Uint8ClampedArray): [lo: number[], hi: number[]] {
  const hist = [new Uint32Array(256), new Uint32Array(256), new Uint32Array(256)];
  const n = d.length / 4;
  for (let i = 0; i < d.length; i += 4) {
    hist[0]![d[i]!]!++; hist[1]![d[i + 1]!]!++; hist[2]![d[i + 2]!]!++;
  }
  const pick = (hst: Uint32Array, q: number) => {
    const target = q * (n - 1);
    let acc = 0;
    for (let v = 0; v < 256; v++) {
      acc += hst[v]!;
      if (acc > target) return v / 255;
    }
    return 1;
  };
  return [hist.map((hs) => pick(hs, 0.005)), hist.map((hs) => pick(hs, 0.995))];
}

/** Colour adjustment in place (image_ops.adjust_color, same formulas and order). */
export function adjustColor(img: Pixels, p: ColorParams): void {
  if (colorIsNeutral(p)) return;
  const d = img.data;
  const [lo, hi] = p.auto ? levels(d) : [[0, 0, 0], [1, 1, 1]];
  const span = [0, 1, 2].map((c) => Math.max(hi[c]! - lo[c]!, 1e-3));
  const ev = 2 ** p.exposure;
  const c1 = 1 + p.contrast;
  const sat = 1 + p.saturation;
  const tR = 1 + 0.25 * p.temperature, tB = 1 - 0.25 * p.temperature;
  const tG = 1 - 0.2 * p.tint;
  const sep = p.sepia;
  const clip = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);
  for (let i = 0; i < d.length; i += 4) {
    let r = d[i]! / 255, g = d[i + 1]! / 255, b = d[i + 2]! / 255;
    if (p.auto) {
      r = clip((r - lo[0]!) / span[0]!); g = clip((g - lo[1]!) / span[1]!); b = clip((b - lo[2]!) / span[2]!);
    }
    if (p.exposure) { r *= ev; g *= ev; b *= ev; }
    if (p.contrast) { r = (r - 0.5) * c1 + 0.5; g = (g - 0.5) * c1 + 0.5; b = (b - 0.5) * c1 + 0.5; }
    if (p.shadows || p.highlights) {
      const lum = clip(r * 0.299 + g * 0.587 + b * 0.114);
      const add = p.shadows * 0.4 * (1 - lum) ** 2 + p.highlights * 0.4 * lum ** 2;
      r += add; g += add; b += add;
    }
    if (p.saturation) {
      const gray = r * 0.299 + g * 0.587 + b * 0.114;
      r = gray + (r - gray) * sat; g = gray + (g - gray) * sat; b = gray + (b - gray) * sat;
    }
    if (p.temperature) { r *= tR; b *= tB; }
    if (p.tint) g *= tG;
    if (sep) {
      const sr = clip(r * 0.393 + g * 0.769 + b * 0.189);
      const sg = clip(r * 0.349 + g * 0.686 + b * 0.168);
      const sb = clip(r * 0.272 + g * 0.534 + b * 0.131);
      r = r * (1 - sep) + sr * sep; g = g * (1 - sep) + sg * sep; b = b * (1 - sep) + sb * sep;
    }
    d[i] = Math.floor(clip(r) * 255 + 0.5);
    d[i + 1] = Math.floor(clip(g) * 255 + 0.5);
    d[i + 2] = Math.floor(clip(b) * 255 + 0.5);
  }
  if (p.sharpness) unsharpMask(img, 2, Math.trunc(p.sharpness * 120), 2);
}

/** Separable Gaussian blur of the RGB channels into a new buffer (sigma = radius, as PIL). */
function gaussianRgb(img: Pixels, sigma: number): Float32Array {
  const { data, width: w, height: h } = img;
  const rad = Math.max(1, Math.ceil(sigma * 3));
  const k = new Float32Array(rad * 2 + 1);
  let sum = 0;
  for (let i = -rad; i <= rad; i++) sum += (k[i + rad] = Math.exp(-(i * i) / (2 * sigma * sigma)));
  for (let i = 0; i < k.length; i++) k[i]! /= sum;
  const tmp = new Float32Array(w * h * 3);
  const out = new Float32Array(w * h * 3);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let r = 0, g = 0, b = 0;
      for (let j = -rad; j <= rad; j++) {
        const xx = Math.min(w - 1, Math.max(0, x + j));
        const i = (y * w + xx) * 4, kk = k[j + rad]!;
        r += data[i]! * kk; g += data[i + 1]! * kk; b += data[i + 2]! * kk;
      }
      const o = (y * w + x) * 3;
      tmp[o] = r; tmp[o + 1] = g; tmp[o + 2] = b;
    }
  }
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let r = 0, g = 0, b = 0;
      for (let j = -rad; j <= rad; j++) {
        const yy = Math.min(h - 1, Math.max(0, y + j));
        const i = (yy * w + x) * 3, kk = k[j + rad]!;
        r += tmp[i]! * kk; g += tmp[i + 1]! * kk; b += tmp[i + 2]! * kk;
      }
      const o = (y * w + x) * 3;
      out[o] = r; out[o + 1] = g; out[o + 2] = b;
    }
  }
  return out;
}

/** PIL ImageFilter.UnsharpMask(radius, percent, threshold) on RGB. */
export function unsharpMask(img: Pixels, radius: number, percent: number, threshold: number): void {
  if (percent <= 0) return;
  const blur = gaussianRgb(img, radius);
  const d = img.data;
  const n = img.width * img.height;
  for (let p = 0; p < n; p++) {
    for (let c = 0; c < 3; c++) {
      const v = d[p * 4 + c]!;
      const diff = v - blur[p * 3 + c]!;
      if (Math.abs(diff) >= threshold) d[p * 4 + c] = v + (diff * percent) / 100;
    }
  }
}

/** Longest-side limit: (w, h) scaled down to fit `max` (unchanged when smaller or max = 0). */
export function fitSize(w: number, h: number, max: number): [number, number] {
  if (!max || Math.max(w, h) <= max) return [w, h];
  const s = max / Math.max(w, h);
  return [Math.max(1, Math.round(w * s)), Math.max(1, Math.round(h * s))];
}
