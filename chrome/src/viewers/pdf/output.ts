// Rasterizing pages: Export as Images, Print, and the bitmaps handed to OCR.
// Pages render with their rotation, current form values (annotation storage)
// and markup (markup layer render()), like preview/pdf_ops.render_page_surface.
import * as pdfjs from "pdfjs-dist";
import { el } from "../../ui/kit.ts";
import type { PageRef } from "./model.ts";
import type { PdfSource } from "./sources.ts";

export type MarkupPainter = (ctx: CanvasRenderingContext2D, ref: PageRef, unitsToPx: number, rotation: number, w: number, h: number) => void;

/** Render a page at `scale` px per point (display frame, white background). */
export async function renderPage(src: PdfSource, ref: PageRef, scale: number, opts: { print?: boolean; markup?: MarkupPainter } = {}): Promise<HTMLCanvasElement> {
  const page = await src.doc.getPage(ref.index + 1);
  const rot = (page.rotate + ref.rotation) % 360;
  const vp = page.getViewport({ scale, rotation: rot });
  const canvas = el("canvas", { width: Math.max(1, Math.round(vp.width)), height: Math.max(1, Math.round(vp.height)) });
  const c = canvas.getContext("2d", { alpha: false })!;
  c.fillStyle = "#fff";
  c.fillRect(0, 0, canvas.width, canvas.height);
  await page.render({
    canvas,
    canvasContext: c,
    viewport: vp,
    // Display intent even for print: it honours annotations hidden because the
    // markup layer owns them (they are drawn by `markup` below).
    intent: "display",
    annotationMode: pdfjs.AnnotationMode.ENABLE_STORAGE,
  }).promise;
  const [x0, y0, x1, y1] = page.view as [number, number, number, number];
  opts.markup?.(c, ref, scale, rot, x1 - x0, y1 - y0);
  page.cleanup();
  return canvas;
}

export const toBlob = (c: HTMLCanvasElement, type: string, quality?: number) =>
  new Promise<Blob>((res, rej) => c.toBlob((b) => (b ? res(b) : rej(new Error("Couldn't encode the image"))), type, quality));

/**
 * Print like pdf.js PDFPrintService: render every page at 150 dpi into a
 * print-only container of <img>s, then window.print().
 */
export async function printPages(
  items: { src: PdfSource; ref: PageRef }[],
  firstSize: [number, number],
  progress: (done: number, total: number) => void,
  signal: AbortSignal,
  markup?: MarkupPainter,
): Promise<void> {
  const box = el("div.pdf-print", { "aria-hidden": "true" });
  const style = el("style", { text: `@page { size: ${firstSize[0]}pt ${firstSize[1]}pt; margin: 0; }` });
  const urls: string[] = [];
  try {
    for (let i = 0; i < items.length; i++) {
      if (signal.aborted) return;
      progress(i, items.length);
      const { src, ref } = items[i]!;
      const canvas = await renderPage(src, ref, 150 / 72, { print: true, markup });
      const url = URL.createObjectURL(await toBlob(canvas, "image/png"));
      canvas.width = canvas.height = 0;
      urls.push(url);
      box.append(el("div.pdf-print-page", {}, el("img", { src: url, alt: "" })));
    }
    progress(items.length, items.length);
    document.head.append(style);
    document.body.append(box);
    await Promise.all([...box.querySelectorAll("img")].map((img) => img.decode().catch(() => {})));
    box.dataset.ready = String(items.length);
    window.print();
  } finally {
    // window.print() blocks until the dialog closes in Chrome.
    setTimeout(() => {
      box.remove();
      style.remove();
      for (const u of urls) URL.revokeObjectURL(u);
    }, 0);
  }
}

interface DirHandle {
  getFileHandle(name: string, o?: { create?: boolean }): Promise<FileSystemFileHandle>;
}

/** Export pages as PNG/JPEG files into a folder the user picks (zip download fallback). */
export async function exportImages(
  items: { src: PdfSource; ref: PageRef }[],
  stem: string,
  format: "png" | "jpeg",
  dpi: number,
  progress: (done: number, total: number) => void,
  markup?: MarkupPainter,
  startIn?: FileSystemHandle,
): Promise<{ count: number; where: string } | null> {
  const picker = (window as unknown as { showDirectoryPicker?: (o?: object) => Promise<DirHandle & { name: string }> }).showDirectoryPicker;
  const ext = format === "png" ? "png" : "jpg";
  const digits = Math.max(3, String(items.length).length);
  const name = (i: number) => `${stem}-${String(i + 1).padStart(digits, "0")}.${ext}`;
  let dir: (DirHandle & { name: string }) | null = null;
  if (picker) {
    try {
      dir = await picker({ mode: "readwrite", id: "view-export", ...(startIn ? { startIn } : {}) });
    } catch (e) {
      if ((e as DOMException).name === "AbortError") return null;
      throw e;
    }
  }
  const zipped: Record<string, Uint8Array> = {};
  for (let i = 0; i < items.length; i++) {
    progress(i, items.length);
    const { src, ref } = items[i]!;
    const canvas = await renderPage(src, ref, dpi / 72, { markup });
    const blob = await toBlob(canvas, `image/${format}`, format === "jpeg" ? 0.9 : undefined);
    canvas.width = canvas.height = 0;
    if (dir) {
      const h = await dir.getFileHandle(name(i), { create: true });
      const w = await h.createWritable();
      await w.write(blob);
      await w.close();
    } else {
      zipped[name(i)] = new Uint8Array(await blob.arrayBuffer());
    }
  }
  progress(items.length, items.length);
  if (dir) return { count: items.length, where: dir.name };
  const { zipSync } = await import("fflate");
  const zip = zipSync(zipped, { level: 0 });
  const { downloadBlob } = await import("../../shell/save.ts");
  downloadBlob(new Blob([zip as Uint8Array<ArrayBuffer>], { type: "application/zip" }), `${stem}-images.zip`);
  return { count: items.length, where: "Downloads" };
}
