// DB text-detection post-processing (port of RapidOCR's DBPostProcess and
// TextDetector.sorted_boxes), without OpenCV: threshold + 2x2 dilation,
// 8-connected components instead of cv2.findContours (only the outer border
// matters: every later step works on the convex hull), rotating-calipers
// min-area rectangles, the "fast" box score and an analytic unclip.
import { miniBox, orderClockwise, roundHalfEven, unclip, type Pt, type Quad } from "./geometry.ts";

export interface DbOptions {
  thresh: number;
  boxThresh: number;
  maxCandidates: number;
  unclipRatio: number;
  useDilation: boolean;
}

/** RapidOCR config.yaml Det defaults (the desktop keeps them). */
export const DB_DEFAULTS: DbOptions = { thresh: 0.3, boxThresh: 0.5, maxCandidates: 1000, unclipRatio: 1.6, useDilation: true };
const MIN_SIZE = 3;

/**
 * pred: the probability map (H x W, row-major). Returns boxes (TL, TR, BR, BL)
 * in destination pixels (the image that was resized to H x W) and their scores,
 * sorted top-to-bottom, left-to-right.
 */
export function dbPostprocess(pred: Float32Array, width: number, height: number, destW: number, destH: number,
  opts: DbOptions = DB_DEFAULTS): { boxes: Quad[]; scores: number[] } {
  const n = width * height;
  let mask: Uint8Array<ArrayBuffer> = new Uint8Array(n);
  for (let i = 0; i < n; i++) mask[i] = pred[i]! > opts.thresh ? 1 : 0;
  if (opts.useDilation) mask = dilate2x2(mask, width, height);

  const raw: { box: Quad; score: number }[] = [];
  for (const pts of components(mask, width, height, opts.maxCandidates)) {
    const { box, short } = miniBox(pts);
    if (short < MIN_SIZE) continue;
    const score = boxScoreFast(pred, width, height, box);
    if (opts.boxThresh > score) continue;
    const grown = miniBox(unclip(box, opts.unclipRatio));
    if (grown.short < MIN_SIZE + 2) continue;
    const scaled = grown.box.map((p): Pt => [
      Math.min(Math.max(roundHalfEven((p[0] / width) * destW), 0), destW),
      Math.min(Math.max(roundHalfEven((p[1] / height) * destH), 0), destH),
    ]);
    raw.push({ box: scaled as Quad, score });
  }

  // filter_det_res
  const kept: { box: Quad; score: number }[] = [];
  for (const r of raw) {
    const b = orderClockwise(r.box).map((p): Pt => [
      Math.trunc(Math.min(Math.max(p[0], 0), destW - 1)),
      Math.trunc(Math.min(Math.max(p[1], 0), destH - 1)),
    ]) as Quad;
    const rw = Math.trunc(Math.hypot(b[0][0] - b[1][0], b[0][1] - b[1][1]));
    const rh = Math.trunc(Math.hypot(b[0][0] - b[3][0], b[0][1] - b[3][1]));
    if (rw <= 3 || rh <= 3) continue;
    kept.push({ box: b, score: r.score });
  }
  const sorted = sortBoxes(kept);
  return { boxes: sorted.map((k) => k.box), scores: sorted.map((k) => k.score) };
}

/** cv2.dilate with a 2x2 kernel (anchor at (1,1)): out(x,y) = max of src over (x-1..x, y-1..y). */
export function dilate2x2(src: Uint8Array, w: number, h: number): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(src.length);
  for (let y = 0; y < h; y++) {
    const row = y * w, up = (y - 1) * w;
    for (let x = 0; x < w; x++) {
      let v = src[row + x]!;
      if (!v && x > 0) v = src[row + x - 1]!;
      if (!v && y > 0) v = src[up + x]! || (x > 0 ? src[up + x - 1]! : 0);
      out[row + x] = v;
    }
  }
  return out;
}

/**
 * 8-connected components of a 0/1 mask, in raster order of their first pixel.
 * For each, returns the leftmost and rightmost pixel of every row it covers:
 * enough for the convex hull, which is all minAreaRect needs.
 */
export function components(mask: Uint8Array, w: number, h: number, max = Infinity): Pt[][] {
  const labels = new Int32Array(mask.length);
  const out: Pt[][] = [];
  const stack: number[] = [];
  for (let start = 0; start < mask.length && out.length < max; start++) {
    if (!mask[start] || labels[start]) continue;
    const id = out.length + 1;
    labels[start] = id;
    stack.push(start);
    // Per-row extremes, keyed by row.
    const minX = new Map<number, number>(), maxX = new Map<number, number>();
    while (stack.length) {
      const i = stack.pop()!;
      const x = i % w, y = (i - x) / w;
      const lo = minX.get(y);
      if (lo === undefined || x < lo) minX.set(y, x);
      const hi = maxX.get(y);
      if (hi === undefined || x > hi) maxX.set(y, x);
      for (let dy = -1; dy <= 1; dy++) {
        const yy = y + dy;
        if (yy < 0 || yy >= h) continue;
        for (let dx = -1; dx <= 1; dx++) {
          const xx = x + dx;
          if (xx < 0 || xx >= w || (dx === 0 && dy === 0)) continue;
          const j = yy * w + xx;
          if (mask[j] && !labels[j]) {
            labels[j] = id;
            stack.push(j);
          }
        }
      }
    }
    const pts: Pt[] = [];
    for (const [y, x0] of minX) {
      pts.push([x0, y]);
      const x1 = maxX.get(y)!;
      if (x1 !== x0) pts.push([x1, y]);
    }
    out.push(pts);
  }
  return out;
}

/** DBPostProcess.box_score_fast: mean probability inside the (integer) box polygon. */
export function boxScoreFast(pred: Float32Array, w: number, h: number, box: Pt[]): number {
  const clampI = (v: number, hi: number) => Math.min(Math.max(v, 0), hi);
  const xs = box.map((p) => p[0]), ys = box.map((p) => p[1]);
  const xmin = clampI(Math.floor(Math.min(...xs)), w - 1), xmax = clampI(Math.ceil(Math.max(...xs)), w - 1);
  const ymin = clampI(Math.floor(Math.min(...ys)), h - 1), ymax = clampI(Math.ceil(Math.max(...ys)), h - 1);
  // cv2.fillPoly on the box shifted to (xmin, ymin) and cast to int32 (truncation).
  const poly = box.map((p): Pt => [Math.trunc(p[0] - xmin), Math.trunc(p[1] - ymin)]);
  let sum = 0, cnt = 0;
  for (let y = 0; y <= ymax - ymin; y++) {
    const span = convexSpan(poly, y);
    if (!span) continue;
    const a = Math.max(0, span[0]), b = Math.min(xmax - xmin, span[1]);
    const row = (y + ymin) * w + xmin;
    for (let x = a; x <= b; x++) {
      sum += pred[row + x]!;
      cnt++;
    }
  }
  return cnt ? sum / cnt : 0;
}

/** Integer x-range covered by a convex polygon on row y (boundary pixels included, as fillPoly does). */
function convexSpan(poly: Pt[], y: number): [number, number] | null {
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < poly.length; i++) {
    const [x0, y0] = poly[i]!;
    const [x1, y1] = poly[(i + 1) % poly.length]!;
    if (y < Math.min(y0, y1) || y > Math.max(y0, y1)) continue;
    if (y0 === y1) {
      lo = Math.min(lo, x0, x1);
      hi = Math.max(hi, x0, x1);
    } else {
      const x = x0 + ((y - y0) * (x1 - x0)) / (y1 - y0);
      lo = Math.min(lo, x);
      hi = Math.max(hi, x);
    }
  }
  if (lo > hi) return null;
  return [Math.round(lo), Math.round(hi)];
}

/** TextDetector.sorted_boxes: rows by top-left y (a 10 px step starts a new row), then x. */
export function sortBoxes<T extends { box: Quad }>(items: T[]): T[] {
  const byY = items.map((it, i) => ({ it, i })).sort((a, b) => a.it.box[0][1] - b.it.box[0][1] || a.i - b.i);
  let line = 0;
  const withLine = byY.map((e, k) => {
    if (k > 0 && e.it.box[0][1] - byY[k - 1]!.it.box[0][1] >= 10) line++;
    return { ...e, line, k };
  });
  withLine.sort((a, b) => a.line - b.line || a.it.box[0][0] - b.it.box[0][0] || a.k - b.k);
  return withLine.map((e) => e.it);
}
