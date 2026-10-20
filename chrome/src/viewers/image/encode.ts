// Encoders for the image worker (image_ops.save_pil / save_animated). Stills use
// the canvas encoders (PNG, JPEG, WebP, AVIF where the browser has one) plus our
// own BMP/TIFF writers; animations use gifenc (GIF), UPNG.js (APNG) and our WebP
// RIFF muxer over per-frame canvas WebP encodes. EXIF is re-attached afterwards.
import { applyPalette, GIFEncoder, quantize } from "gifenc";
import UPNG from "upng-js";
import { apngSetLoop, encodeBmp, encodeTiff, type Fmt, insertExif, muxAnimatedWebp } from "./formats.ts";
import { canvas, pixels, type Src, sizeOf } from "./pipeline.ts";

export type OutFmt = "png" | "jpeg" | "webp" | "avif" | "bmp" | "tiff" | "gif" | "apng";

export const OUT_MIME: Record<OutFmt, string> = {
  png: "image/png", jpeg: "image/jpeg", webp: "image/webp", avif: "image/avif",
  bmp: "image/bmp", tiff: "image/tiff", gif: "image/gif", apng: "image/apng",
};

/** Formats that can hold an animation. */
export const ANIM_OUT: ReadonlySet<OutFmt> = new Set(["gif", "webp", "apng"]);

export interface EncodeOpts {
  /** 0..1 for JPEG/WebP/AVIF; WebP 1 = lossless. */
  quality: number;
  /** TIFF stream to re-attach (JPEG, PNG, APNG, WebP). */
  exif?: Uint8Array | null;
}

const toBytes = async (b: Blob) => new Uint8Array(await b.arrayBuffer());

function opaque(d: Uint8ClampedArray): boolean {
  for (let i = 3; i < d.length; i += 4) if (d[i] !== 255) return false;
  return true;
}

/** Draw `src` on white (JPEG has no alpha; the desktop pastes onto white too). */
function onWhite(src: Src): OffscreenCanvas {
  const [w, h] = sizeOf(src);
  const [c, ctx] = canvas(w, h);
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, w, h);
  ctx.drawImage(src, 0, 0);
  return c;
}

async function canvasEncode(src: Src, mime: string, quality: number): Promise<Blob> {
  const c = src instanceof OffscreenCanvas ? src : (() => {
    const [w, h] = sizeOf(src);
    const [cc, ctx] = canvas(w, h);
    ctx.drawImage(src, 0, 0);
    return cc;
  })();
  const blob = await c.convertToBlob({ type: mime, quality });
  if (blob.type !== mime) throw new Error(`This browser can't encode ${mime.replace("image/", "").toUpperCase()}`);
  return blob;
}

function gifFrame(img: ImageData): { index: Uint8Array; palette: number[][]; transparentIndex: number } {
  const alpha = !opaque(img.data);
  const format = alpha ? "rgba4444" : "rgb565";
  const palette = quantize(img.data, 256, { format, oneBitAlpha: alpha ? 127 : false, clearAlpha: alpha });
  const index = applyPalette(img.data, palette, format);
  return { index, palette, transparentIndex: alpha ? palette.findIndex((c) => c[3] === 0) : -1 };
}

export async function encodeStill(src: Src, fmt: OutFmt, o: EncodeOpts): Promise<Blob> {
  const exif = o.exif ?? null;
  const withExif = async (blob: Blob, f: Fmt) =>
    exif ? new Blob([insertExif(f, await toBytes(blob), exif) as BlobPart], { type: blob.type }) : blob;
  switch (fmt) {
    case "png": return withExif(await canvasEncode(src, "image/png", 1), "png");
    case "jpeg": return withExif(await canvasEncode(onWhite(src), "image/jpeg", o.quality), "jpeg");
    case "webp": return withExif(await canvasEncode(src, "image/webp", o.quality), "webp");
    case "avif": return canvasEncode(src, "image/avif", o.quality);
    case "bmp": return new Blob([encodeBmp(pixels(src)) as BlobPart], { type: "image/bmp" });
    case "tiff": return new Blob([encodeTiff(pixels(src)) as BlobPart], { type: "image/tiff" });
    case "gif": {
      const img = pixels(src);
      const enc = GIFEncoder();
      const f = gifFrame(img);
      enc.writeFrame(f.index, img.width, img.height, {
        palette: f.palette, transparent: f.transparentIndex >= 0, transparentIndex: Math.max(0, f.transparentIndex), repeat: -1,
      });
      enc.finish();
      return new Blob([enc.bytes() as BlobPart], { type: "image/gif" });
    }
    case "apng": {
      const img = pixels(src);
      const png = new Uint8Array(UPNG.encode([img.data.slice().buffer], img.width, img.height, 0));
      return new Blob([(exif ? insertExif("png", png, exif) : png) as BlobPart], { type: "image/png" });
    }
  }
}

export interface AnimOpts extends EncodeOpts {
  /** ImageDecoder repetitionCount of the source: Infinity = forever, n = extra plays. */
  repetitions: number;
  progress?: (done: number) => void;
}

/**
 * Encode an animation frame by frame. `frames` yields fully composited frames of
 * identical size with their duration in ms (the caller owns and closes them).
 * GIF and WebP stream; UPNG needs every frame at once, so APNG collects pixels.
 */
export async function encodeAnimated(frames: AsyncIterable<{ src: Src; duration: number }>, fmt: "gif" | "webp" | "apng",
  o: AnimOpts): Promise<Blob> {
  const reps = o.repetitions;
  const plays = reps === Infinity ? 0 : reps + 1; // APNG/WebP: total plays, 0 = forever
  let n = 0;
  if (fmt === "gif") {
    const enc = GIFEncoder();
    for await (const { src, duration } of frames) {
      const img = pixels(src);
      const f = gifFrame(img);
      enc.writeFrame(f.index, img.width, img.height, {
        palette: f.palette, delay: duration, repeat: reps === Infinity ? 0 : reps === 0 ? -1 : reps,
        transparent: f.transparentIndex >= 0, transparentIndex: Math.max(0, f.transparentIndex),
        dispose: f.transparentIndex >= 0 ? 2 : 1,
      });
      n++;
      o.progress?.(n);
    }
    enc.finish();
    return new Blob([enc.bytes() as BlobPart], { type: "image/gif" });
  }
  if (fmt === "webp") {
    const out: { file: Uint8Array; duration: number }[] = [];
    let size: [number, number] = [1, 1];
    for await (const { src, duration } of frames) {
      size = sizeOf(src);
      out.push({ file: await toBytes(await canvasEncode(src, "image/webp", o.quality)), duration });
      n++;
      o.progress?.(n);
    }
    return new Blob([muxAnimatedWebp(out, size[0], size[1], plays, o.exif ?? undefined) as BlobPart], { type: "image/webp" });
  }
  const bufs: ArrayBuffer[] = [];
  const dels: number[] = [];
  let size: [number, number] = [1, 1];
  for await (const { src, duration } of frames) {
    const img = pixels(src);
    size = [img.width, img.height];
    bufs.push(img.data.buffer as ArrayBuffer);
    dels.push(Math.min(65535, Math.max(1, Math.round(duration || 100))));
    n++;
      o.progress?.(n);
  }
  let png: Uint8Array = new Uint8Array(UPNG.encode(bufs, size[0], size[1], 0, dels));
  if (bufs.length > 1) png = apngSetLoop(png, plays);
  if (o.exif) png = insertExif("apng", png, o.exif);
  return new Blob([png as BlobPart], { type: "image/png" });
}
