// Dev-only page (dist/dev/ocr-bench.html): draws text images on a canvas, runs
// the OCR service on them and shows the selectable text layer, for e2e tests
// and timing (test/e2e/f.test.mjs).
import { createOcr, createTextLayer, linesToText, type TextLayer } from "../index.ts";
import type { OcrResult } from "../api.ts";

export interface Spec {
  width: number;
  height: number;
  /** [text, x, y (baseline), font size px, font family, rotation deg?] */
  lines: [string, number, number, number, string, number?][];
  background?: string;
  color?: string;
}

const ocr = createOcr();
let layer: TextLayer | null = null;
const host = document.createElement("div");
host.style.cssText = "position:relative;display:inline-block";
document.body.append(host);

function draw(spec: Spec): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = spec.width;
  c.height = spec.height;
  const g = c.getContext("2d")!;
  g.fillStyle = spec.background ?? "#fff";
  g.fillRect(0, 0, c.width, c.height);
  g.fillStyle = spec.color ?? "#111";
  for (const [text, x, y, size, family, rot] of spec.lines) {
    g.save();
    g.translate(x, y);
    if (rot) g.rotate((rot * Math.PI) / 180);
    g.font = `${size}px ${family}`;
    g.fillText(text, 0, 0);
    g.restore();
  }
  return c;
}

async function run(spec: Spec, show = false) {
  await document.fonts.ready;
  const canvas = draw(spec);
  const bmp = await createImageBitmap(canvas);
  const t = performance.now();
  const result: OcrResult = await ocr.recognize(bmp);
  const ms = performance.now() - t;
  bmp.close();
  if (show) {
    layer?.destroy();
    host.replaceChildren(canvas);
    canvas.style.cssText = `display:block;width:${spec.width / 2}px;height:${spec.height / 2}px`;
    layer = createTextLayer(result);
    host.append(layer.el);
  }
  return { ms, result, text: linesToText(result.lines), timings: ocr.lastTimings, png: show ? canvas.toDataURL() : undefined };
}

Object.assign(globalThis, { ocrBench: { run, ocr, layer: () => layer } });
document.documentElement.dataset.state = "ready";
