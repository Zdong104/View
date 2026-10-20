// Images -> PDF (port of preview/convert.py images_to_pdf + img2pdf's layout).
// One page per image, in order. Unedited JPEG and PNG files are embedded as they
// are (lossless, like img2pdf); everything else arrives here as PNG bytes.
// No DOM: runs in the image worker and in unit tests.
import { PDFDocument } from "@cantoo/pdf-lib";

export type PageSize = "fit" | "a4" | "letter";

export const PAGE_SIZES: Record<Exclude<PageSize, "fit">, [number, number]> = { // points
  a4: [595.276, 841.890],
  letter: [612.0, 792.0],
};

export const PAGE_SIZE_LABELS: [PageSize, string][] = [["fit", "Fit to Image"], ["a4", "A4"], ["letter", "US Letter"]];

const MM_TO_PT = 72 / 25.4;
const DEFAULT_DPI = 150; // used when a file carries no real resolution

export interface PdfOptions {
  pageSize: PageSize;
  marginMm: number;
  /** Turn A4/Letter pages landscape for landscape images (default true). */
  landscapeAuto?: boolean;
}

export interface PdfImage {
  bytes: Uint8Array;
  type: "jpg" | "png";
  width: number;
  height: number;
  /** Resolution from the file, when it has one. */
  dpi?: [number, number] | null;
}

/** [pageW, pageH, imageW, imageH] in points (convert._make_layout). */
export function layout(wpx: number, hpx: number, dpi: [number, number] | null | undefined, o: PdfOptions): [number, number, number, number] {
  const margin = Math.max(0, o.marginMm || 0) * MM_TO_PT;
  if (o.pageSize === "fit") {
    const d0 = dpi?.[0] || DEFAULT_DPI;
    const dx = Math.abs(d0 - 96) < 0.5 || d0 <= 1 ? DEFAULT_DPI : d0;
    const dy = dpi?.[1] && Math.abs(dpi[1] - 96) >= 0.5 && dpi[1] > 1 ? dpi[1] : dx;
    const w = (wpx * 72) / dx, h = (hpx * 72) / dy;
    return [w + 2 * margin, h + 2 * margin, w, h];
  }
  let [pw, ph] = PAGE_SIZES[o.pageSize];
  if ((o.landscapeAuto ?? true) && wpx > hpx) [pw, ph] = [ph, pw];
  const aw = Math.max(1, pw - 2 * margin), ah = Math.max(1, ph - 2 * margin);
  const s = Math.min(aw / wpx, ah / hpx);
  return [pw, ph, wpx * s, hpx * s];
}

export async function imagesToPdf(images: Iterable<PdfImage> | AsyncIterable<PdfImage>, o: PdfOptions,
  progress?: (done: number) => void): Promise<Uint8Array> {
  const doc = await PDFDocument.create();
  doc.setProducer("View");
  doc.setCreator("View");
  let n = 0;
  for await (const im of images) {
    const emb = im.type === "jpg" ? await doc.embedJpg(im.bytes) : await doc.embedPng(im.bytes);
    const [pw, ph, iw, ih] = layout(im.width, im.height, im.dpi, o);
    const page = doc.addPage([pw, ph]);
    page.drawImage(emb, { x: (pw - iw) / 2, y: (ph - ih) / 2, width: iw, height: ih });
    n++;
    progress?.(n);
  }
  if (!n) throw new Error("No images to convert");
  return doc.save();
}
