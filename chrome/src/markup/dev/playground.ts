// Dev-only page (dist/dev/markup-playground.html): a blank surface with the markup
// layer attached, for building and e2e-testing markup without a PDF/image viewer.
//   ?kind=pdf      docKind "pdf", surface "p0" with a fake text layer (for text markup)
//   ?scale=1.5     zoom (CSS px per surface unit)
//   ?rot=90        display rotation
// Globals for tests: markup (the controller), exportPdf(flatten) -> base64 PDF,
// inkPixels() -> painted pixel count of render() onto a canvas, lastWarnings (export).
import { PDFDocument } from "@cantoo/pdf-lib";
import { createMarkup } from "../index.ts";
import type { TextRun } from "../api.ts";
import { exportPdfAnnotations } from "../pdf-export.ts";
import { el } from "../../ui/kit.ts";

const q = new URLSearchParams(location.search);
const kind = q.get("kind") === "pdf" ? "pdf" : "image";
const scale = Number(q.get("scale") ?? 1) || 1;
const rot = Number(q.get("rot") ?? 0) || 0;
const W = 600, H = 400;
const surface = kind === "pdf" ? "p0" : "img";

const ctrl = createMarkup({ docKind: kind, author: "Tester" });
const swap = rot === 90 || rot === 270;
const overlay = el("div", { id: "surface", style: `position:absolute;left:40px;top:80px;width:${(swap ? H : W) * scale}px;height:${(swap ? W : H) * scale}px;background:#fff;box-shadow:0 1px 4px #0003` });
document.body.append(el("div", { style: "position:absolute;left:0;top:0;right:0;background:var(--header-bg)" }, ctrl.toolbar()), overlay);

// fake text layer: monospace lines with known character boxes
const LINES = [[20, 60, "The quick brown fox jumps over the lazy dog."], [20, 100, "Second line of sample text for markup."]] as const;
const FONT = 16;
const mctx = document.createElement("canvas").getContext("2d")!;
mctx.font = `${FONT}px monospace`;
const cw = mctx.measureText("M").width;
const runs: TextRun[] = [];
if (kind === "pdf") {
  for (const [x, y, text] of LINES) {
    overlay.append(el("div", { text, style: `position:absolute;left:${x * scale}px;top:${y * scale}px;font:${FONT * scale}px/1 monospace;color:#222;white-space:pre` }));
    const quads: number[] = [];
    for (let i = 0; i < text.length; i++) {
      const x0 = x + i * cw, x1 = x0 + cw, y0 = y, y1 = y + FONT;
      quads.push(x0, y0, x1, y0, x1, y1, x0, y1);
    }
    runs.push({ text, quads });
  }
}

ctrl.attach({
  surface, width: W, height: H, overlay, scale: () => scale, rotation: () => rot,
  ...(kind === "pdf" ? { textRuns: async () => runs } : {}),
});

const g = globalThis as Record<string, unknown>;
g.markup = ctrl;
g.charWidth = cw;
g.exportPdf = async (flatten = false) => {
  const doc = await PDFDocument.create();
  doc.addPage([W, H]);
  const { warnings } = await exportPdfAnnotations(doc, ctrl.toJSON(), (s) => (s === surface ? 0 : null), { flatten });
  g.lastWarnings = warnings;
  const bytes = await doc.save();
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
};
g.inkPixels = () => {
  const c = document.createElement("canvas");
  c.width = W; c.height = H;
  const ctx = c.getContext("2d")!;
  ctrl.render(ctx, surface, 1);
  const d = ctx.getImageData(0, 0, W, H).data;
  let n = 0;
  for (let i = 3; i < d.length; i += 4) if (d[i]! > 0) n++;
  return n;
};
document.documentElement.dataset.state = "ready";
