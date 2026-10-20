// TIFF decoding (Chrome has no TIFF decoder): UTIF.js (utif2, MIT) in the image
// worker. Takes the first full-resolution page (multi-page TIFFs open their first
// page, like the desktop), applies the EXIF/TIFF Orientation tag, returns pixels
// as an ImageBitmap so the rest of the pipeline treats it like any still.
import UTIF from "utif2";

interface Ifd { width: number; height: number; data: Uint8Array; [tag: string]: unknown }

/** Bitmap of the first image page of a TIFF file. */
export async function decodeTiff(bytes: Uint8Array): Promise<ImageBitmap> {
  const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  const ifds = UTIF.decode(buf) as unknown as Ifd[];
  // Skip reduced-resolution subfiles (NewSubfileType bit 0 = thumbnail/preview).
  const page = ifds.find((i) => i["t256"] && !((((i["t254"] as number[] | undefined)?.[0]) ?? 0) & 1)) ?? ifds[0];
  if (!page) throw new Error("This TIFF file has no image");
  UTIF.decodeImage(buf, page as never);
  const rgba = UTIF.toRGBA8(page as never);
  const w = page.width, h = page.height;
  if (!w || !h || rgba.length < w * h * 4) throw new Error("This TIFF file uses a compression View can't decode");
  const img = new ImageData(new Uint8ClampedArray(rgba.buffer as ArrayBuffer, rgba.byteOffset, w * h * 4), w, h);
  const orientation = ((page["t274"] as number[] | undefined)?.[0]) ?? 1;
  const bmp = await createImageBitmap(img);
  return orientation === 1 ? bmp : orient(bmp, orientation);
}

/** Apply an EXIF orientation (2..8) by drawing onto a canvas. */
function orient(bmp: ImageBitmap, o: number): Promise<ImageBitmap> {
  const swap = o >= 5;
  const w = swap ? bmp.height : bmp.width, h = swap ? bmp.width : bmp.height;
  const c = new OffscreenCanvas(w, h);
  const g = c.getContext("2d")!;
  const W = bmp.width, H = bmp.height;
  const m: Record<number, [number, number, number, number, number, number]> = {
    2: [-1, 0, 0, 1, W, 0], 3: [-1, 0, 0, -1, W, H], 4: [1, 0, 0, -1, 0, H],
    5: [0, 1, 1, 0, 0, 0], 6: [0, 1, -1, 0, H, 0], 7: [0, -1, -1, 0, H, W], 8: [0, -1, 1, 0, 0, W],
  };
  g.setTransform(...(m[o] ?? [1, 0, 0, 1, 0, 0]));
  g.drawImage(bmp, 0, 0);
  bmp.close();
  return createImageBitmap(c);
}
