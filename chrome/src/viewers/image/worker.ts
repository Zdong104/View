// Image worker: decoding (createImageBitmap for stills, WebCodecs ImageDecoder for
// animated GIF/WebP/APNG/AVIF), the op pipeline, colour preview, thumbnails,
// encoding and images -> PDF, all off the main thread.
//
// Loading: the build has no worker entry convention, so this module is its own
// lazily imported chunk and starts itself as a module Worker from its own URL
// (`workerUrl` below = this chunk). On the main thread importing it only yields
// the URL; inside the worker the message handler is installed. Heavy encoders
// (gifenc, UPNG, pdf-lib) are imported on first use inside the worker.
import { colorIsNeutral, fitSize, sizeAfterOps, type ColorParams, type WorkerOp } from "./ops.ts";
import { exifOrientation, parseContainer, patchExif, sniff, type Fmt } from "./formats.ts";
import { applyOp, applyOps, bitmap, fit, toCanvas, type Src, sizeOf } from "./pipeline.ts";
import type { OutFmt } from "./encode.ts";
import type { PdfImage, PdfOptions } from "../../convert/pdf.ts";

export const workerUrl: string = import.meta.url;

export interface AnimInfo {
  n: number;
  /** ImageDecoder repetitionCount: Infinity = forever, n = extra plays. */
  repetitions: number;
  mime: string;
}

export interface OpenResult { w: number; h: number; fmt: Fmt; anim: AnimInfo | null }
export interface Frame { bmp: ImageBitmap; duration: number }

export type Request =
  | { t: "open"; id: number; blob: Blob; base?: ImageBitmap }
  | { t: "render"; id: number; ops: WorkerOp[]; max: number; animate: boolean }
  | { t: "thumb"; id: number; ops: WorkerOp[]; size: number }
  | { t: "colorStart"; id: number; ops: WorkerOp[]; max: number }
  | { t: "colorPreview"; id: number; p: ColorParams }
  | { t: "colorEnd"; id: number }
  | { t: "encode"; id: number; ops: WorkerOp[]; fmt: OutFmt; quality: number | null; maxPx: number; keepExif: boolean; animate: boolean }
  | { t: "pdf"; pages: { id: number; ops: WorkerOp[] }[]; opts: PdfOptions }
  | { t: "close"; id: number };

export interface EncodeResult { blob: Blob; kind: "anim" | "still" | "flat"; frames: number }

export interface Responses {
  open: OpenResult;
  render: { w: number; h: number; frames: Frame[] };
  thumb: { bmp: ImageBitmap };
  colorStart: Record<string, never>;
  colorPreview: { bmp: ImageBitmap };
  colorEnd: Record<string, never>;
  encode: EncodeResult;
  pdf: { blob: Blob };
  close: Record<string, never>;
}

export type Progress = { progress: number; total: number };

// ---------------------------------------------------------------------------------

interface Doc {
  blob: Blob;
  bytes?: Uint8Array;
  fmt: Fmt;
  base?: ImageBitmap;        // decoded upright still / SVG raster from the main thread
  svgBase?: ImageBitmap;     // kept: can't be re-decoded here
  anim: AnimInfo | null;
  w: number;
  h: number;
  color?: OffscreenCanvas;   // Adjust Color preview source
  used: number;
}

const docs = new Map<number, Doc>();
let clock = 0;
const MAX_BASES = 4;

const ANIM_MIME: Partial<Record<Fmt, string>> = { gif: "image/gif", webp: "image/webp", apng: "image/png", avif: "image/avif" };

async function bytesOf(d: Doc): Promise<Uint8Array> {
  d.bytes ??= new Uint8Array(await d.blob.arrayBuffer());
  return d.bytes;
}

function evict(): void {
  const withBase = [...docs.values()].filter((d) => d.base && d.base !== d.svgBase).sort((a, b) => a.used - b.used);
  for (const d of withBase.slice(0, Math.max(0, withBase.length - MAX_BASES))) { d.base!.close(); d.base = undefined; }
}

async function decodeStill(d: Doc): Promise<ImageBitmap> {
  d.used = ++clock;
  if (d.base) return d.base;
  d.base = d.fmt === "tiff"
    ? await (await import("./tiff.ts")).decodeTiff(await bytesOf(d)) // no native TIFF decoder
    : await createImageBitmap(d.blob, { imageOrientation: "from-image" });
  evict();
  return d.base;
}

async function probeAnim(d: Doc): Promise<AnimInfo | null> {
  const mime = ANIM_MIME[d.fmt];
  if (!mime || typeof ImageDecoder === "undefined" || !(await ImageDecoder.isTypeSupported(mime))) return null;
  const dec = new ImageDecoder({ data: await bytesOf(d), type: mime });
  try {
    await dec.tracks.ready;
    await dec.completed;
    const t = dec.tracks.selectedTrack;
    if (!t || !t.animated || t.frameCount < 2) return null;
    return { n: t.frameCount, repetitions: t.repetitionCount, mime };
  } catch {
    return null;
  } finally {
    dec.close();
  }
}

/** Every frame, fully composited, with its duration in ms. Closes each frame after use. */
async function* frames(d: Doc): AsyncGenerator<{ src: Src; duration: number }> {
  const dec = new ImageDecoder({ data: await bytesOf(d), type: d.anim!.mime });
  try {
    await dec.tracks.ready;
    for (let i = 0; i < d.anim!.n; i++) {
      const { image } = await dec.decode({ frameIndex: i, completeFramesOnly: true });
      try {
        yield { src: image, duration: Math.round((image.duration ?? 0) / 1000) };
      } finally {
        image.close();
      }
    }
  } finally {
    dec.close();
  }
}

async function firstFrame(d: Doc): Promise<Src> {
  if (d.svgBase) return d.svgBase;
  if (!d.anim) return decodeStill(d);
  for await (const f of frames(d)) return toCanvas(f.src);
  throw new Error("no frames");
}

async function open(id: number, blob: Blob, base?: ImageBitmap): Promise<OpenResult> {
  const head = new Uint8Array(await blob.slice(0, 65536).arrayBuffer());
  const fmt = sniff(head);
  const d: Doc = { blob, fmt, anim: null, w: 0, h: 0, used: ++clock, svgBase: base, base };
  docs.set(id, d);
  d.anim = await probeAnim(d);
  if (d.anim) {
    const f = await firstFrame(d);
    [d.w, d.h] = sizeOf(f);
  } else {
    const b = await decodeStill(d);
    [d.w, d.h] = [b.width, b.height];
  }
  return { w: d.w, h: d.h, fmt, anim: d.anim };
}

function doc(id: number): Doc {
  const d = docs.get(id);
  if (!d) throw new Error("image is not open");
  return d;
}

async function render(id: number, ops: WorkerOp[], max: number, animate: boolean): Promise<Responses["render"]> {
  const d = doc(id);
  const [w, h] = sizeAfterOps(ops, d.w, d.h);
  if (d.anim && animate) {
    const out: Frame[] = [];
    for await (const f of frames(d)) {
      out.push({ bmp: await bitmap(fit(applyOps(f.src, ops), max)), duration: f.duration });
    }
    return { w, h, frames: out };
  }
  const src = await firstFrame(d);
  const r = fit(applyOps(src, ops), max);
  return { w, h, frames: [{ bmp: await bitmap(r === src ? toCanvas(src) : r), duration: 0 }] };
}

async function thumb(id: number, ops: WorkerOp[], size: number): Promise<Responses["thumb"]> {
  const d = doc(id);
  if (!ops.length && !d.anim && !d.svgBase && !d.base && d.fmt !== "tiff") {
    // Fast path: let the decoder scale (no full-size bitmap kept around).
    const opts: ImageBitmapOptions = { imageOrientation: "from-image", resizeQuality: "medium" };
    if (d.w >= d.h) opts.resizeWidth = Math.min(size, d.w); else opts.resizeHeight = Math.min(size, d.h);
    return { bmp: await createImageBitmap(d.blob, opts) };
  }
  const src = await firstFrame(d);
  const r = fit(applyOps(src, ops), size);
  return { bmp: await bitmap(r === src ? toCanvas(src) : r) };
}

async function colorStart(id: number, ops: WorkerOp[], max: number): Promise<void> {
  const d = doc(id);
  const src = await firstFrame(d);
  const r = fit(applyOps(src, ops), max);
  d.color = r === src || !(r instanceof OffscreenCanvas) ? toCanvas(r) : r;
}

async function colorPreview(id: number, p: ColorParams): Promise<Responses["colorPreview"]> {
  const d = doc(id);
  if (!d.color) throw new Error("no colour preview");
  const copy = toCanvas(d.color);
  const out = colorIsNeutral(p) ? copy : applyOp(copy, { k: "color", p });
  return { bmp: await bitmap(out) };
}

async function exifFor(d: Doc, size: [number, number]): Promise<Uint8Array | null> {
  const info = parseContainer(await bytesOf(d));
  return info.exif && info.fmt !== "tiff" ? patchExif(info.exif, size) : null;
}

async function encode(id: number, ops: WorkerOp[], fmt: OutFmt, quality: number | null, maxPx: number,
  keepExif: boolean, animate: boolean, report: (p: Progress) => void): Promise<EncodeResult> {
  const enc = await import("./encode.ts");
  const d = doc(id);
  const info = parseContainer(await bytesOf(d));
  const [w, h] = fitSize(...sizeAfterOps(ops, d.w, d.h), maxPx);
  const exif = keepExif ? await exifFor(d, [w, h]) : null;
  if (d.anim && animate && enc.ANIM_OUT.has(fmt)) {
    const lossless = quality === null ? !!info.lossless : quality >= 1;
    const q = lossless ? 1 : quality ?? 0.9;
    const n = d.anim.n;
    const blob = await enc.encodeAnimated((async function* () {
      for await (const f of frames(d)) {
        yield { src: fit(applyOps(f.src, ops), maxPx), duration: f.duration };
      }
    })(), fmt as "gif" | "webp" | "apng", {
      quality: q, exif, repetitions: d.anim.repetitions, progress: (k) => report({ progress: k, total: n }),
    });
    return { blob, kind: "anim", frames: n };
  }
  const src = await firstFrame(d);
  const r = fit(applyOps(src, ops), maxPx);
  const q = quality ?? (fmt === "webp" && info.fmt === "webp" && info.lossless ? 1 : 0.92);
  const blob = await enc.encodeStill(r, fmt, { quality: q, exif });
  return { blob, kind: d.anim ? "flat" : "still", frames: 1 };
}

async function pdf(pages: { id: number; ops: WorkerOp[] }[], opts: PdfOptions, report: (p: Progress) => void): Promise<Blob> {
  const { imagesToPdf } = await import("../../convert/pdf.ts");
  async function* images(): AsyncGenerator<PdfImage> {
    for (const pg of pages) {
      const d = doc(pg.id);
      const bytes = await bytesOf(d);
      const info = parseContainer(bytes);
      const direct = !pg.ops.length && !d.anim && !d.svgBase &&
        ((info.fmt === "jpeg" && exifOrientation(info.exif) === 1 && info.colorModel !== "CMYK") || info.fmt === "png");
      if (direct) {
        yield { bytes, type: info.fmt === "jpeg" ? "jpg" : "png", width: info.width ?? d.w, height: info.height ?? d.h, dpi: info.dpi };
      } else {
        const src = await firstFrame(d);
        const r = applyOps(src, pg.ops);
        const c = r instanceof OffscreenCanvas ? r : toCanvas(r);
        const png = new Uint8Array(await (await c.convertToBlob({ type: "image/png" })).arrayBuffer());
        yield { bytes: png, type: "png", width: c.width, height: c.height, dpi: pg.ops.length ? null : info.dpi };
      }
    }
  }
  const out = await imagesToPdf(images(), opts, (k) => report({ progress: k, total: pages.length }));
  return new Blob([out as BlobPart], { type: "application/pdf" });
}

function close(id: number): void {
  const d = docs.get(id);
  if (!d) return;
  d.base?.close();
  if (d.svgBase !== d.base) d.svgBase?.close();
  docs.delete(id);
}

// ---------------------------------------------------------------------------------

interface Scope {
  postMessage(m: unknown, transfer?: Transferable[]): void;
  addEventListener(t: "message", fn: (e: MessageEvent<{ seq: number; req: Request }>) => void): void;
}

const isWorker = typeof document === "undefined" && typeof (globalThis as { WorkerGlobalScope?: unknown }).WorkerGlobalScope !== "undefined";

if (isWorker) {
  const scope = globalThis as unknown as Scope;
  scope.addEventListener("message", async (e) => {
    const { seq, req } = e.data;
    const report = (p: Progress) => scope.postMessage({ seq, progress: p });
    try {
      let res: unknown;
      switch (req.t) {
        case "open": res = await open(req.id, req.blob, req.base); break;
        case "render": res = await render(req.id, req.ops, req.max, req.animate); break;
        case "thumb": res = await thumb(req.id, req.ops, req.size); break;
        case "colorStart": await colorStart(req.id, req.ops, req.max); res = {}; break;
        case "colorPreview": res = await colorPreview(req.id, req.p); break;
        case "colorEnd": delete doc(req.id).color; res = {}; break;
        case "encode": res = await encode(req.id, req.ops, req.fmt, req.quality, req.maxPx, req.keepExif, req.animate, report); break;
        case "pdf": res = { blob: await pdf(req.pages, req.opts, report) }; break;
        case "close": close(req.id); res = {}; break;
      }
      const transfer: Transferable[] = [];
      const r = res as { frames?: Frame[]; bmp?: ImageBitmap };
      if (Array.isArray(r.frames)) for (const f of r.frames) transfer.push(f.bmp);
      if (r.bmp) transfer.push(r.bmp);
      scope.postMessage({ seq, res }, transfer);
    } catch (err) {
      scope.postMessage({ seq, error: (err as Error)?.message ?? String(err) });
    }
  });
}
