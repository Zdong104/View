// Pixel operations the PP-OCR pipeline needs, matching OpenCV's semantics
// (cv2.resize INTER_LINEAR, warpPerspective INTER_CUBIC + BORDER_REPLICATE,
// rot90, copyMakeBorder) on packed 3-channel 8-bit images. Like RapidOCR, the
// channel order is BGR: the PP-OCR models were trained on cv2-loaded images.
import { perspectiveTransform, type Pt } from "./geometry.ts";

export interface Img {
  w: number;
  h: number;
  /** w * h * 3 bytes, BGR. */
  data: Uint8Array;
}

/** RGBA (canvas ImageData) -> BGR, compositing transparency onto white (as preview/ocr.py does). */
export function fromRGBA(rgba: Uint8ClampedArray | Uint8Array, w: number, h: number): Img {
  const data = new Uint8Array(w * h * 3);
  for (let i = 0, j = 0; i < w * h * 4; i += 4, j += 3) {
    const a = rgba[i + 3]!;
    if (a === 255) {
      data[j] = rgba[i + 2]!;
      data[j + 1] = rgba[i + 1]!;
      data[j + 2] = rgba[i]!;
    } else {
      const k = a / 255, white = 255 * (1 - k);
      data[j] = Math.round(rgba[i + 2]! * k + white);
      data[j + 1] = Math.round(rgba[i + 1]! * k + white);
      data[j + 2] = Math.round(rgba[i]! * k + white);
    }
  }
  return { w, h, data };
}

/** Bilinear source taps for one axis, with OpenCV's half-pixel mapping and edge clamping. */
function taps(dst: number, src: number): { i0: Int32Array; i1: Int32Array; f: Float32Array } {
  const scale = src / dst;
  const i0 = new Int32Array(dst), i1 = new Int32Array(dst), f = new Float32Array(dst);
  for (let d = 0; d < dst; d++) {
    let x = (d + 0.5) * scale - 0.5;
    let s = Math.floor(x);
    x -= s;
    if (s < 0) { s = 0; x = 0; }
    if (s >= src - 1) { s = src - 1; x = 0; }
    i0[d] = s;
    i1[d] = Math.min(s + 1, src - 1);
    f[d] = x;
  }
  return { i0, i1, f };
}

/** cv2.resize(img, (w, h)) with INTER_LINEAR. */
export function resize(img: Img, w: number, h: number): Img {
  if (w === img.w && h === img.h) return img;
  const tx = taps(w, img.w), ty = taps(h, img.h);
  const out = new Uint8Array(w * h * 3);
  const src = img.data, sw = img.w * 3;
  for (let y = 0; y < h; y++) {
    const r0 = ty.i0[y]! * sw, r1 = ty.i1[y]! * sw, fy = ty.f[y]!, gy = 1 - fy;
    let o = y * w * 3;
    for (let x = 0; x < w; x++) {
      const c0 = tx.i0[x]! * 3, c1 = tx.i1[x]! * 3, fx = tx.f[x]!, gx = 1 - fx;
      for (let c = 0; c < 3; c++) {
        const top = src[r0 + c0 + c]! * gx + src[r0 + c1 + c]! * fx;
        const bot = src[r1 + c0 + c]! * gx + src[r1 + c1 + c]! * fx;
        out[o++] = Math.round(top * gy + bot * fy);
      }
    }
  }
  return { w, h, data: out };
}

/** cv2.copyMakeBorder(top, bottom, 0, 0, BORDER_CONSTANT, black). */
export function padVertical(img: Img, top: number, bottom: number): Img {
  const h = img.h + top + bottom;
  const data = new Uint8Array(img.w * h * 3);
  data.set(img.data, top * img.w * 3);
  return { w: img.w, h, data };
}

const A = -0.75; // OpenCV's bicubic coefficient
function cubic(x: number, out: Float32Array) {
  const w0 = ((A * (x + 1) - 5 * A) * (x + 1) + 8 * A) * (x + 1) - 4 * A;
  const w1 = ((A + 2) * x - (A + 3)) * x * x + 1;
  const w2 = ((A + 2) * (1 - x) - (A + 3)) * (1 - x) * (1 - x) + 1;
  out[0] = w0;
  out[1] = w1;
  out[2] = w2;
  out[3] = 1 - w0 - w1 - w2;
}

export interface Crop {
  img: Img;
  /** Homography from crop pixels to source-image pixels. */
  toSrc: number[];
}

/**
 * process_img.get_rotate_crop_image: warp the quad to an upright rectangle
 * (INTER_CUBIC, BORDER_REPLICATE). Rotating tall crops (rot90) is left to the caller.
 */
export function cropQuad(img: Img, quad: Pt[]): Crop {
  const d = (a: Pt, b: Pt) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const [p0, p1, p2, p3] = quad as [Pt, Pt, Pt, Pt];
  const w = Math.max(1, Math.trunc(Math.max(d(p0, p1), d(p2, p3))));
  const h = Math.max(1, Math.trunc(Math.max(d(p0, p3), d(p1, p2))));
  const rect: Pt[] = [[0, 0], [w, 0], [w, h], [0, h]];
  const H = perspectiveTransform(rect, quad);
  const out = new Uint8Array(w * h * 3);
  const src = img.data, sw = img.w, sh = img.h;
  const wx = new Float32Array(4), wy = new Float32Array(4);
  const xi = new Int32Array(4), yi = new Int32Array(4);
  let o = 0;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const den = H[6]! * x + H[7]! * y + H[8]!;
      const X = (H[0]! * x + H[1]! * y + H[2]!) / den;
      const Y = (H[3]! * x + H[4]! * y + H[5]!) / den;
      const fx = Math.floor(X), fy = Math.floor(Y);
      cubic(X - fx, wx);
      cubic(Y - fy, wy);
      for (let k = 0; k < 4; k++) {
        xi[k] = Math.min(Math.max(fx - 1 + k, 0), sw - 1);
        yi[k] = Math.min(Math.max(fy - 1 + k, 0), sh - 1);
      }
      for (let c = 0; c < 3; c++) {
        let acc = 0;
        for (let j = 0; j < 4; j++) {
          const row = yi[j]! * sw;
          let r = 0;
          for (let k = 0; k < 4; k++) r += src[(row + xi[k]!) * 3 + c]! * wx[k]!;
          acc += r * wy[j]!;
        }
        out[o++] = acc < 0 ? 0 : acc > 255 ? 255 : Math.round(acc);
      }
    }
  }
  return { img: { w, h, data: out }, toSrc: H };
}

/** np.rot90 (counter-clockwise). */
export function rot90(img: Img): Img {
  const { w, h } = img;
  const out = new Uint8Array(w * h * 3);
  // out has width h, height w; out[i][j] = in[j][w-1-i]
  for (let i = 0; i < w; i++) {
    for (let j = 0; j < h; j++) {
      const s = (j * w + (w - 1 - i)) * 3, d = (i * h + j) * 3;
      out[d] = img.data[s]!;
      out[d + 1] = img.data[s + 1]!;
      out[d + 2] = img.data[s + 2]!;
    }
  }
  return { w: h, h: w, data: out };
}

/** cv2.rotate(img, ROTATE_180). */
export function rot180(img: Img): Img {
  const n = img.w * img.h;
  const out = new Uint8Array(n * 3);
  for (let i = 0; i < n; i++) {
    const s = i * 3, d = (n - 1 - i) * 3;
    out[d] = img.data[s]!;
    out[d + 1] = img.data[s + 1]!;
    out[d + 2] = img.data[s + 2]!;
  }
  return { w: img.w, h: img.h, data: out };
}

/**
 * Write img as CHW float ((x / 255 - 0.5) / 0.5) into `out` at batch slot `slot`
 * of a [N, 3, H, W] tensor, leaving the columns beyond img.w zero (right padding).
 */
export function normalizeInto(img: Img, out: Float32Array, slot: number, H: number, W: number) {
  const plane = H * W, base = slot * 3 * plane;
  for (let y = 0; y < img.h; y++) {
    for (let x = 0; x < img.w; x++) {
      const s = (y * img.w + x) * 3, d = y * W + x;
      out[base + d] = img.data[s]! / 127.5 - 1;
      out[base + plane + d] = img.data[s + 1]! / 127.5 - 1;
      out[base + 2 * plane + d] = img.data[s + 2]! / 127.5 - 1;
    }
  }
}
