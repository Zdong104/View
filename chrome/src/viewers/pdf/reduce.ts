// Reduce File Size (best effort; the desktop uses Ghostscript, which is AGPL
// and can't ship here). Large image XObjects are decoded, downscaled to at
// most MAX_SIDE pixels and re-encoded as JPEG through OffscreenCanvas
// (the encoder runs off the main thread); a re-encode is kept only when it is
// clearly smaller. Then the file is written with object streams. Fonts and
// vector content are not touched.
import { unzlib } from "fflate";
import { PDFArray, PDFDict, PDFName, PDFNumber, PDFRawStream, type PDFDocument, type PDFObject } from "@cantoo/pdf-lib";

const MAX_SIDE = 1654;     // ~A4 long side at 150 dpi
const MIN_BYTES = 48 * 1024;
const QUALITY = 0.72;

const N = (n: string) => PDFName.of(n);

function filters(d: PDFDict): string[] {
  const f = d.lookup(N("Filter"));
  if (f instanceof PDFName) return [f.decodeText()];
  if (f instanceof PDFArray) return f.asArray().map((x) => (x instanceof PDFName ? x.decodeText() : "?"));
  return [];
}

/** Colour components of a supported colour space, or 0 when unsupported. */
function components(doc: PDFDocument, cs: PDFObject | undefined): number {
  if (cs instanceof PDFName) {
    const n = cs.decodeText();
    return n === "DeviceRGB" ? 3 : n === "DeviceGray" ? 1 : 0;
  }
  if (cs instanceof PDFArray && cs.size() >= 2) {
    const kind = cs.lookup(0);
    if (kind instanceof PDFName && kind.decodeText() === "ICCBased") {
      const s = cs.lookup(1);
      const n = (s as { dict?: PDFDict })?.dict?.lookup(N("N"));
      const k = n instanceof PDFNumber ? n.asNumber() : 0;
      return k === 3 || k === 1 ? k : 0;
    }
  }
  void doc;
  return 0;
}

const inflateAsync = (data: Uint8Array) => new Promise<Uint8Array>((res, rej) => unzlib(data, (e, d) => (e ? rej(e) : res(d))));

async function decode(doc: PDFDocument, s: PDFRawStream): Promise<ImageBitmap | null> {
  const d = s.dict;
  const f = filters(d);
  const w = (d.lookup(N("Width")) as PDFNumber | undefined)?.asNumber() ?? 0;
  const h = (d.lookup(N("Height")) as PDFNumber | undefined)?.asNumber() ?? 0;
  const bpc = (d.lookup(N("BitsPerComponent")) as PDFNumber | undefined)?.asNumber() ?? 8;
  if ((s as { transform?: unknown }).transform) return null; // still encrypted (decrypted lazily on write)
  if (!w || !h || bpc !== 8 || d.has(N("Decode")) || d.lookup(N("ImageMask"))) return null;
  const comps = components(doc, d.lookup(N("ColorSpace")));
  if (!comps) return null;
  const contents = s.getContents();
  if (f.length === 1 && f[0] === "DCTDecode") {
    return createImageBitmap(new Blob([contents as Uint8Array<ArrayBuffer>], { type: "image/jpeg" }));
  }
  if (f.length === 1 && f[0] === "FlateDecode") {
    const parms = d.lookup(N("DecodeParms"));
    if (parms instanceof PDFDict && (parms.lookup(N("Predictor")) as PDFNumber | undefined)?.asNumber()! > 1) return null;
    const raw = await inflateAsync(contents);
    if (raw.length < w * h * comps) return null;
    const rgba = new Uint8ClampedArray(w * h * 4);
    for (let i = 0, j = 0; i < w * h; i++, j += comps) {
      const r = raw[j]!, g = comps === 3 ? raw[j + 1]! : r, b = comps === 3 ? raw[j + 2]! : r;
      rgba[i * 4] = r; rgba[i * 4 + 1] = g; rgba[i * 4 + 2] = b; rgba[i * 4 + 3] = 255;
    }
    return createImageBitmap(new ImageData(rgba, w, h));
  }
  return null;
}

/** Re-encode large images in place. Returns the number of images replaced. */
export async function reduceImages(doc: PDFDocument, progress?: (msg: string) => void): Promise<number> {
  const ctx = doc.context;
  const images = ctx.enumerateIndirectObjects().filter(([, o]) =>
    o instanceof PDFRawStream && o.dict.lookup(N("Subtype")) === N("Image") && o.getContentsSize() >= MIN_BYTES) as [import("@cantoo/pdf-lib").PDFRef, PDFRawStream][];
  let done = 0, i = 0;
  for (const [ref, s] of images) {
    progress?.(`Compressing image ${++i} of ${images.length}…`);
    let bmp: ImageBitmap | null = null;
    try {
      bmp = await decode(doc, s);
      if (!bmp) continue;
      const k = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
      const w = Math.max(1, Math.round(bmp.width * k)), h = Math.max(1, Math.round(bmp.height * k));
      const canvas = new OffscreenCanvas(w, h);
      const c = canvas.getContext("2d")!;
      c.fillStyle = "#fff";
      c.fillRect(0, 0, w, h);
      c.imageSmoothingQuality = "high";
      c.drawImage(bmp, 0, 0, w, h);
      const jpeg = new Uint8Array(await (await canvas.convertToBlob({ type: "image/jpeg", quality: QUALITY })).arrayBuffer());
      if (jpeg.length > s.getContentsSize() * 0.9) continue;
      const dict = s.dict.clone(ctx);
      for (const key of ["Filter", "DecodeParms", "Length", "Width", "Height", "ColorSpace", "BitsPerComponent", "Decode"]) dict.delete(N(key));
      dict.set(N("Width"), PDFNumber.of(w));
      dict.set(N("Height"), PDFNumber.of(h));
      dict.set(N("ColorSpace"), N("DeviceRGB"));
      dict.set(N("BitsPerComponent"), PDFNumber.of(8));
      dict.set(N("Filter"), N("DCTDecode"));
      dict.set(N("Length"), PDFNumber.of(jpeg.length));
      ctx.assign(ref, PDFRawStream.of(dict, jpeg));
      done++;
    } catch (e) {
      console.warn("reduce: skipped image", e);
    } finally {
      bmp?.close();
    }
  }
  return done;
}
