// Live Text: the OCR result (ctx.services.ocr()) shown with the shared selectable
// SVG text layer from src/ocr/text-layer.ts, which fills the on-screen image box at
// any zoom and handles drag/word/line selection and Ctrl+C. OCR runs on the
// displayed bitmap; `k` maps its pixels to rendered-image pixels (markup runs).
import type { TextRun } from "../../markup/api.ts";
import type { OcrResult } from "../../ocr/api.ts";
import { createTextLayer, type TextLayer } from "../../ocr/text-layer.ts";

export class LiveText {
  private host: HTMLElement;
  private layer: TextLayer | null = null;
  private result: OcrResult | null = null;
  private k = 1;

  constructor(host: HTMLElement) { this.host = host; }

  set(result: OcrResult, k: number): void {
    this.clear();
    this.result = result;
    this.k = k;
    this.layer = createTextLayer(result);
    this.host.append(this.layer.el);
    this.host.dataset.lines = String(result.lines.length);
  }

  clear(): void {
    this.layer?.destroy();
    this.layer?.el.remove();
    this.layer = null;
    this.result = null;
    delete this.host.dataset.lines;
  }

  hasText(): boolean { return !!this.layer?.hasText(); }
  hasSelection(): boolean { return !!this.layer?.hasSelection(); }
  clearSelection(): void { this.layer?.clear(); }
  showBoxes(v: boolean): void { this.layer?.setShowBoxes(v); }
  get boxesShown(): boolean { return !!this.layer?.showBoxes; }
  text(): string { return this.layer?.text() ?? ""; }

  /** Text runs (rendered-image px) for the markup layer's text-markup tools. */
  runs(): TextRun[] {
    return (this.result?.lines ?? []).filter((l) => l.charQuads?.length).map((l) => ({
      text: l.text, quads: l.charQuads!.map((v) => v * this.k),
    }));
  }
}
