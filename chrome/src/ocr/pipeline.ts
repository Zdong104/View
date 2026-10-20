// The RapidOCR pipeline (rapidocr/main.py) with the desktop's settings
// (preview/ocr.py): bound the image size, pad very wide or very short images,
// DB detection, crop each box upright, 0/180 classification, CTC recognition,
// then map boxes (and per-character quads) back to source pixels. Model calls
// go through `Models`, so this file has no onnxruntime dependency.
import type { OcrLine, OcrResult } from "./api.ts";
import { buildCharset, charBounds, ctcDecode } from "./ctc.ts";
import { dbPostprocess, DB_DEFAULTS } from "./det-post.ts";
import { applyH, orderCorners, roundHalfEven, type Pt, type Quad } from "./geometry.ts";
import { cropQuad, normalizeInto, padVertical, resize, rot180, rot90, type Img } from "./imgproc.ts";

export interface Tensor {
  data: Float32Array;
  dims: readonly number[];
}

export interface Models {
  det(input: Float32Array, dims: number[]): Promise<Tensor>;
  cls(input: Float32Array, dims: number[]): Promise<Tensor>;
  rec(input: Float32Array, dims: number[]): Promise<Tensor>;
  /** The recognizer's "character" metadata (one entry per line). */
  characters: string;
}

export interface PipelineOptions {
  /** Global.max_side_len (desktop: 4000) and Global.min_side_len. */
  maxSide: number;
  minSide: number;
  /**
   * Upper bound for the detector's input side. RapidOCR with limit_type "max"
   * (the desktop setting) uses 960 / 1500 / 2000 by image size; this caps it
   * further when set below that.
   */
  detMaxSide: number;
  textScore: number;
  minHeight: number;
  widthHeightRatio: number;
  clsThresh: number;
  clsBatch: number;
  recBatch: number;
}

export const DESKTOP: PipelineOptions = {
  maxSide: 4000, minSide: 30, detMaxSide: 2000, textScore: 0.5, minHeight: 30, widthHeightRatio: 8,
  clsThresh: 0.9, clsBatch: 6, recBatch: 6,
};

export interface Timings {
  pre: number;
  det: number;
  cls: number;
  rec: number;
  total: number;
  lines: number;
}

const REC_H = 48, REC_W = 320, CLS_H = 48, CLS_W = 192;
const round32 = (v: number) => roundHalfEven(v / 32) * 32;

/** resize_image_within_bounds (Global pre-processing). */
function bound(img: Img, minSide: number, maxSide: number): { img: Img; rw: number; rh: number } {
  let cur = img;
  if (Math.max(cur.w, cur.h) > maxSide) {
    const r = maxSide / Math.max(cur.w, cur.h);
    cur = resize(cur, round32(Math.trunc(cur.w * r)), round32(Math.trunc(cur.h * r)));
  }
  if (Math.min(cur.w, cur.h) < minSide) {
    const r = minSide / Math.min(cur.w, cur.h);
    cur = resize(cur, round32(Math.trunc(cur.w * r)), round32(Math.trunc(cur.h * r)));
  }
  return { img: cur, rw: img.w / cur.w, rh: img.h / cur.h };
}

/** TextDetector.get_preprocess + DetPreProcess.resize for limit_type "max". */
function detSize(w: number, h: number, cap: number): [number, number] {
  const m = Math.max(w, h);
  let limit = m < 960 ? 960 : m < 1500 ? 1500 : 2000;
  limit = Math.min(limit, cap);
  const r = m > limit ? limit / m : 1;
  return [round32(Math.trunc(w * r)), round32(Math.trunc(h * r))];
}

interface Region {
  box: Quad;
  img: Img;
  toSrc: number[];
  rotated90: boolean;
  flipped: boolean;
}

export async function runPipeline(src: Img, models: Models, check: () => void,
  opts: PipelineOptions = DESKTOP): Promise<{ result: OcrResult; timings: Timings }> {
  const t0 = performance.now();
  const timings: Timings = { pre: 0, det: 0, cls: 0, rec: 0, total: 0, lines: 0 };
  const empty = (): { result: OcrResult; timings: Timings } => {
    timings.total = performance.now() - t0;
    return { result: { width: src.w, height: src.h, lines: [] }, timings };
  };
  if (src.w < 8 || src.h < 8) return empty();

  // Global pre-processing: size bounds, then vertical padding for wide/short images.
  const { img: bounded, rw, rh } = bound(src, opts.minSide, opts.maxSide);
  let img = bounded, padTop = 0;
  if (img.h <= opts.minHeight || img.w / img.h > opts.widthHeightRatio) {
    const newH = Math.max(Math.trunc(img.w / opts.widthHeightRatio), opts.minHeight) * 2;
    padTop = Math.trunc(Math.abs(newH - img.h) / 2);
    img = padVertical(img, padTop, padTop);
  }

  // Detection.
  const [dw, dh] = detSize(img.w, img.h, opts.detMaxSide);
  if (dw <= 0 || dh <= 0) return empty();
  const detImg = resize(img, dw, dh);
  const detIn = new Float32Array(3 * dw * dh);
  normalizeInto(detImg, detIn, 0, dh, dw);
  timings.pre = performance.now() - t0;
  check();
  let t = performance.now();
  const pred = await models.det(detIn, [1, 3, dh, dw]);
  check();
  const ph = pred.dims[2]!, pw = pred.dims[3]!;
  const { boxes } = dbPostprocess(pred.data, pw, ph, img.w, img.h, DB_DEFAULTS);
  timings.det = performance.now() - t;
  if (!boxes.length) return empty();

  // Upright crops.
  const regions: Region[] = boxes.map((box) => {
    const c = cropQuad(img, box);
    const tall = c.img.h / c.img.w >= 1.5;
    return { box, img: tall ? rot90(c.img) : c.img, toSrc: c.toSrc, rotated90: tall, flipped: false };
  });
  const order = regions.map((r, i) => ({ i, ratio: r.img.w / r.img.h })).sort((a, b) => a.ratio - b.ratio).map((o) => o.i);

  // Direction classification (0 / 180).
  t = performance.now();
  for (let b = 0; b < order.length; b += opts.clsBatch) {
    check();
    const ids = order.slice(b, b + opts.clsBatch);
    const input = new Float32Array(ids.length * 3 * CLS_H * CLS_W);
    ids.forEach((id, k) => {
      const im = regions[id]!.img;
      const rwid = Math.min(CLS_W, Math.ceil(CLS_H * (im.w / im.h)));
      normalizeInto(resize(im, rwid, CLS_H), input, k, CLS_H, CLS_W);
    });
    const out = await models.cls(input, [ids.length, 3, CLS_H, CLS_W]);
    ids.forEach((id, k) => {
      const p0 = out.data[k * 2]!, p1 = out.data[k * 2 + 1]!;
      if (p1 > p0 && p1 > opts.clsThresh) {
        const r = regions[id]!;
        r.img = rot180(r.img);
        r.flipped = true;
      }
    });
  }
  timings.cls = performance.now() - t;

  // Recognition, in batches of similar aspect ratio.
  t = performance.now();
  const charset = buildCharset(models.characters);
  const texts: { text: string; score: number; chars: string[]; bounds: [number, number][] }[] = new Array(regions.length);
  for (let b = 0; b < order.length; b += opts.recBatch) {
    check();
    const ids = order.slice(b, b + opts.recBatch);
    let maxRatio = REC_W / REC_H;
    for (const id of ids) maxRatio = Math.max(maxRatio, regions[id]!.img.w / regions[id]!.img.h);
    const W = Math.trunc(REC_H * maxRatio);
    const input = new Float32Array(ids.length * 3 * REC_H * W);
    const contentW: number[] = [];
    ids.forEach((id, k) => {
      const im = regions[id]!.img;
      const cw = Math.min(W, Math.ceil(REC_H * (im.w / im.h)));
      contentW.push(cw);
      normalizeInto(resize(im, cw, REC_H), input, k, REC_H, W);
    });
    const out = await models.rec(input, [ids.length, 3, REC_H, W]);
    const [n, T, C] = out.dims as [number, number, number];
    const dec = ctcDecode(out.data, n, T, C, charset);
    ids.forEach((id, k) => {
      const d = dec[k]!;
      texts[id] = { text: d.text, score: d.score, chars: d.chars, bounds: charBounds(d.spans, W / T, contentW[k]!) };
    });
  }
  timings.rec = performance.now() - t;

  // Back to source pixels: undo the padding, then the size bounding.
  const toOrig = (p: Pt): [number, number] => [
    Math.min(Math.max((p[0]) * rw, 0), src.w),
    Math.min(Math.max((p[1] - padTop) * rh, 0), src.h),
  ];
  const lines: OcrLine[] = [];
  regions.forEach((r, i) => {
    const tx = texts[i]!;
    if (!tx.text.trim() || tx.score < opts.textScore) return;
    const box = orderCorners(r.box.map(toOrig));
    const quads: number[][] = [];
    const cps: string[] = [];
    tx.chars.forEach((ch, k) => {
      const [u0, u1] = tx.bounds[k]!;
      const q = [regionPoint(r, u0, 0), regionPoint(r, u1, 0), regionPoint(r, u1, 1), regionPoint(r, u0, 1)]
        .flatMap((p) => toOrig(p));
      for (const cp of Array.from(ch)) {
        cps.push(cp);
        quads.push(q);
      }
    });
    // preview/ocr.py strips the text; drop the quads of stripped characters too.
    let a = 0, z = cps.length;
    while (a < z && /\s/u.test(cps[a]!)) a++;
    while (z > a && /\s/u.test(cps[z - 1]!)) z--;
    lines.push({
      text: cps.slice(a, z).join(""),
      score: tx.score,
      box: box.flatMap((p) => [p[0], p[1]]),
      charQuads: quads.slice(a, z).flat(),
    });
  });
  timings.lines = lines.length;
  timings.total = performance.now() - t0;
  return { result: { width: src.w, height: src.h, lines }, timings };
}

/** A point given in the recognizer's view of the region (u along the text, v across, 0..1) -> padded-image pixels. */
function regionPoint(r: Region, u: number, v: number): Pt {
  if (r.flipped) { u = 1 - u; v = 1 - v; }
  let s = u, t = v;
  if (r.rotated90) { s = 1 - v; t = u; }
  // The crop before rotation is (w x h) = rotated90 ? (img.h x img.w) : (img.w x img.h).
  const cw = r.rotated90 ? r.img.h : r.img.w, ch = r.rotated90 ? r.img.w : r.img.h;
  return applyH(r.toSrc, s * cw, t * ch);
}
