// Typings for the two untyped encoder packages the image worker uses.

declare module "gifenc" {
  type Palette = number[][];
  interface FrameOpts {
    palette?: Palette;
    delay?: number; // ms (stored as 1/100 s)
    transparent?: boolean;
    transparentIndex?: number;
    repeat?: number; // -1 once, 0 forever, n extra plays
    dispose?: number;
    colorDepth?: number;
  }
  interface Encoder {
    writeFrame(index: Uint8Array, width: number, height: number, opts?: FrameOpts): void;
    finish(): void;
    bytes(): Uint8Array;
  }
  export function GIFEncoder(opts?: { initialCapacity?: number; auto?: boolean }): Encoder;
  export function quantize(rgba: Uint8Array | Uint8ClampedArray, maxColors: number,
    opts?: { format?: "rgb565" | "rgb444" | "rgba4444"; oneBitAlpha?: boolean | number; clearAlpha?: boolean;
      clearAlphaThreshold?: number; clearAlphaColor?: number }): Palette;
  export function applyPalette(rgba: Uint8Array | Uint8ClampedArray, palette: Palette,
    format?: "rgb565" | "rgb444" | "rgba4444"): Uint8Array;
}

declare module "upng-js" {
  const UPNG: {
    /** ps = 0: lossless. dels: per-frame delays in ms. Returns PNG/APNG bytes. */
    encode(bufs: ArrayBuffer[], w: number, h: number, ps: number, dels?: number[], forbidPlte?: boolean): ArrayBuffer;
  };
  export default UPNG;
}
