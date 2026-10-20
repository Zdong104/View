// Op pipeline on OffscreenCanvas (image_ops.apply_ops). Runs in the image worker:
// geometry via drawImage transforms, pixel ops (Instant Alpha, colour) via
// getImageData + the pure functions in ops.ts, markup as pre-rendered layers.
import { adjustColor, alphaFill, fitSize, type WorkerOp } from "./ops.ts";

export type Src = ImageBitmap | OffscreenCanvas | VideoFrame;

const dims = (s: Src): [number, number] =>
  "displayWidth" in s ? [s.displayWidth, s.displayHeight] : [s.width, s.height];

export function canvas(w: number, h: number): [OffscreenCanvas, OffscreenCanvasRenderingContext2D] {
  const c = new OffscreenCanvas(Math.max(1, w), Math.max(1, h));
  const ctx = c.getContext("2d", { willReadFrequently: false })!;
  ctx.imageSmoothingQuality = "high";
  return [c, ctx];
}

/** A canvas copy of `src` (pixel ops need a canvas they may write to). */
export function toCanvas(src: Src): OffscreenCanvas {
  const [w, h] = dims(src);
  const [c, ctx] = canvas(w, h);
  ctx.drawImage(src, 0, 0);
  return c;
}

function pixelOp(src: Src, fn: (img: ImageData) => void): OffscreenCanvas {
  const c = src instanceof OffscreenCanvas ? src : toCanvas(src);
  const ctx = c.getContext("2d")!;
  const img = ctx.getImageData(0, 0, c.width, c.height);
  fn(img);
  ctx.putImageData(img, 0, 0);
  return c;
}

export function applyOp(src: Src, op: WorkerOp): Src {
  const [w, h] = dims(src);
  switch (op.k) {
    case "rotate": {
      const [c, ctx] = op.deg === 180 ? canvas(w, h) : canvas(h, w);
      ctx.translate(c.width / 2, c.height / 2);
      ctx.rotate((op.deg * Math.PI) / 180);
      ctx.drawImage(src, -w / 2, -h / 2);
      return c;
    }
    case "flip": {
      const [c, ctx] = canvas(w, h);
      if (op.vertical) { ctx.translate(0, h); ctx.scale(1, -1); } else { ctx.translate(w, 0); ctx.scale(-1, 1); }
      ctx.drawImage(src, 0, 0);
      return c;
    }
    case "resize": {
      // Halve repeatedly for big reductions so "high" smoothing never skips source pixels.
      let cur: Src = src;
      let [cw, ch] = [w, h];
      while (cw / 2 >= op.w && ch / 2 >= op.h) {
        const [c, ctx] = canvas(Math.round(cw / 2), Math.round(ch / 2));
        ctx.drawImage(cur, 0, 0, c.width, c.height);
        cur = c; cw = c.width; ch = c.height;
      }
      const [c, ctx] = canvas(op.w, op.h);
      ctx.drawImage(cur, 0, 0, op.w, op.h);
      return c;
    }
    case "crop":
    case "ellipse": {
      const cw = op.x1 - op.x0, chh = op.y1 - op.y0;
      const [c, ctx] = canvas(cw, chh);
      ctx.drawImage(src, op.x0, op.y0, cw, chh, 0, 0, cw, chh);
      if (op.k === "ellipse") {
        ctx.globalCompositeOperation = "destination-in";
        ctx.beginPath();
        ctx.ellipse(cw / 2, chh / 2, cw / 2, chh / 2, 0, 0, Math.PI * 2);
        ctx.fill();
      }
      return c;
    }
    case "alpha":
      return pixelOp(src, (img) => alphaFill(img, op.x, op.y, op.tol));
    case "color":
      return pixelOp(src, (img) => adjustColor(img, op.p));
    case "layer": {
      const c = src instanceof OffscreenCanvas ? src : toCanvas(src);
      c.getContext("2d")!.drawImage(op.bmp, 0, 0, w, h);
      return c;
    }
  }
}

export function applyOps(src: Src, ops: readonly WorkerOp[]): Src {
  let cur = src;
  for (const op of ops) cur = applyOp(cur, op);
  return cur;
}

/** `src` scaled to fit `max` on its longest side (same object when it fits). */
export function fit(src: Src, max: number): Src {
  const [w, h] = dims(src);
  const [tw, th] = fitSize(w, h, max);
  if (tw === w && th === h) return src;
  return applyOp(src, { k: "resize", w: tw, h: th });
}

export const sizeOf = dims;

export function pixels(src: Src): ImageData {
  const c = src instanceof OffscreenCanvas ? src : toCanvas(src);
  return c.getContext("2d")!.getImageData(0, 0, c.width, c.height);
}

export async function bitmap(src: Src): Promise<ImageBitmap> {
  if (src instanceof OffscreenCanvas) return src.transferToImageBitmap();
  return createImageBitmap(src);
}
