// Image viewer dialogs and panels: Adjust Color (side panel with live preview),
// Adjust Size, Export As / Export Selected Images options, Export as PDF options.
// Dialogs use the shared ui/dialog.ts; saving goes through the shell (requestSave).
import { PAGE_SIZE_LABELS, type PdfOptions, type PageSize } from "../../convert/pdf.ts";
import { showDialog } from "../../ui/dialog.ts";
import { button, el } from "../../ui/kit.ts";
import type { OutFmt } from "./encode.ts";
import { COLOR_DEFAULTS, COLOR_SLIDERS, type ColorParams } from "./ops.ts";

export interface FormatSpec { id: OutFmt | "pdf"; label: string; ext: string; quality: boolean }

// image_view.EXPORT_FORMATS (AVIF only where the browser can encode it).
export const EXPORT_FORMATS: FormatSpec[] = [
  { id: "png", label: "PNG", ext: ".png", quality: false },
  { id: "jpeg", label: "JPEG", ext: ".jpg", quality: true },
  { id: "webp", label: "WebP", ext: ".webp", quality: true },
  { id: "avif", label: "AVIF", ext: ".avif", quality: true },
  { id: "tiff", label: "TIFF", ext: ".tif", quality: false },
  { id: "bmp", label: "BMP", ext: ".bmp", quality: false },
  { id: "gif", label: "GIF", ext: ".gif", quality: false },
  { id: "apng", label: "Animated PNG", ext: ".apng", quality: false },
];
export const BATCH_FORMATS: FormatSpec[] = [...EXPORT_FORMATS, { id: "pdf", label: "PDF", ext: ".pdf", quality: false }];

let avif: Promise<boolean> | undefined;
/** Chrome has no AVIF encoder today (convertToBlob falls back to PNG); probe once. */
export function canEncodeAvif(): Promise<boolean> {
  avif ??= (async () => {
    try {
      const c = new OffscreenCanvas(1, 1);
      c.getContext("2d")!.fillRect(0, 0, 1, 1); // convertToBlob needs a context
      return (await c.convertToBlob({ type: "image/avif" })).type === "image/avif";
    } catch {
      return false;
    }
  })();
  return avif;
}

export async function availableFormats(list: FormatSpec[]): Promise<FormatSpec[]> {
  const a = await canEncodeAvif();
  return list.filter((f) => f.id !== "avif" || a);
}

const row = (label: string, control: HTMLElement, hint?: string) =>
  el("label.img-form-row", {}, el("span.img-form-label", {}, label, hint ? el("small", { text: hint }) : null), control);

function numberInput(value: number, min: number, max: number, step = 1): HTMLInputElement {
  return el("input.img-input", { type: "number", value, min, max, step });
}

export interface ExportChoice { fmt: FormatSpec; quality: number; maxPx: number }

/** Format + Quality (+ Maximum Size) dialog (image_view._format_dialog). */
export async function exportOptions(o: { title: string; okLabel: string; formats: FormatSpec[]; description?: string;
  maxSize?: boolean; initial?: OutFmt | "pdf" }): Promise<ExportChoice | null> {
  const fmts = await availableFormats(o.formats);
  const sel = el("select.img-input", { "aria-label": "Format" });
  for (const f of fmts) sel.append(el("option", { value: f.id, text: f.label }));
  if (o.initial && fmts.some((f) => f.id === o.initial)) sel.value = o.initial;
  const q = numberInput(90, 1, 100);
  const size = numberInput(0, 0, 20000, 100);
  const sync = () => { q.disabled = !fmts.find((f) => f.id === sel.value)?.quality; };
  sel.addEventListener("change", sync);
  sync();
  const body = el("div.img-form", {},
    o.description ? el("p.img-form-desc", { text: o.description }) : null,
    row("Format", sel), row("Quality", q),
    o.maxSize ? row("Maximum Size", size, "Longest side in pixels, 0 = original") : null);
  const res = await showDialog({ title: o.title, body, actions: [{ label: "Cancel", value: "cancel" }, { label: o.okLabel, value: "ok", suggested: true }] });
  if (res !== "ok") return null;
  const fmt = fmts.find((f) => f.id === sel.value)!;
  return { fmt, quality: Math.min(100, Math.max(1, Number(q.value) || 90)), maxPx: Math.max(0, Math.round(Number(size.value) || 0)) };
}

/** Adjust Size: width, height, scale %, keep aspect ratio (image_view._adjust_size). */
export async function sizeDialog(bw: number, bh: number): Promise<[number, number] | null> {
  const w = numberInput(bw, 1, 30000), h = numberInput(bh, 1, 30000), pct = numberInput(100, 1, 1000);
  const lock = el("input", { type: "checkbox", checked: true });
  const ratio = bw / bh;
  let busy = false;
  const sync = (src: HTMLInputElement) => {
    if (busy) return;
    busy = true;
    if (src === pct) {
      w.value = String(Math.max(1, Math.round((bw * Number(pct.value)) / 100)));
      h.value = String(Math.max(1, Math.round((bh * Number(pct.value)) / 100)));
    } else if (lock.checked) {
      if (src === w) h.value = String(Math.max(1, Math.round(Number(w.value) / ratio)));
      else w.value = String(Math.max(1, Math.round(Number(h.value) * ratio)));
      pct.value = String(Math.round((100 * Number(w.value)) / bw));
    }
    busy = false;
  };
  for (const i of [w, h, pct]) i.addEventListener("input", () => sync(i));
  const body = el("div.img-form", {}, el("p.img-form-desc", { text: `Current size: ${bw} × ${bh} px` }),
    row("Scale", pct, "Percent"), row("Width", w, "Pixels"), row("Height", h, "Pixels"),
    el("label.img-form-row.img-check", {}, el("span.img-form-label", { text: "Keep Aspect Ratio" }), lock));
  const res = await showDialog({ title: "Adjust Size", body, actions: [{ label: "Cancel", value: "cancel" }, { label: "Apply", value: "ok", suggested: true }] });
  if (res !== "ok") return null;
  const nw = Math.max(1, Math.round(Number(w.value))), nh = Math.max(1, Math.round(Number(h.value)));
  return nw && nh ? [nw, nh] : null;
}

/** Export as PDF: page size and margin (convert.show_export_dialog). */
export async function pdfOptions(n: number): Promise<PdfOptions | null> {
  const size = el("select.img-input", { "aria-label": "Page Size" });
  for (const [id, label] of PAGE_SIZE_LABELS) size.append(el("option", { value: id, text: label }));
  const margin = numberInput(0, 0, 50);
  const body = el("div.img-form", {}, el("p.img-form-desc", { text: `${n} image${n === 1 ? "" : "s"}, one per page` }),
    row("Page Size", size), row("Margin", margin, "Millimetres"));
  const res = await showDialog({ title: "Export as PDF", body, actions: [{ label: "Cancel", value: "cancel" }, { label: "Export…", value: "ok", suggested: true }] });
  if (res !== "ok") return null;
  return { pageSize: size.value as PageSize, marginMm: Math.max(0, Number(margin.value) || 0), landscapeAuto: true };
}

/** The Adjust Color side panel (image_view._build_color_panel). */
export class ColorPanel {
  readonly el: HTMLElement;
  private inputs = new Map<keyof ColorParams, HTMLInputElement>();
  private auto: HTMLButtonElement;
  onChange: () => void = () => {};
  onClose: (apply: boolean) => void = () => {};

  constructor() {
    this.auto = button({ label: "Auto Levels", title: "Auto Levels", toggle: true, onClick: () => this.onChange() });
    const sliders = COLOR_SLIDERS.map(([key, label, lo, hi]) => {
      const input = el("input.img-slider", { type: "range", min: lo, max: hi, step: 0.01, value: 0, "aria-label": label, "data-key": key });
      input.addEventListener("input", () => this.onChange());
      input.addEventListener("dblclick", () => { input.value = "0"; this.onChange(); });
      this.inputs.set(key, input);
      return el("label.img-color-row", {}, el("span", { text: label }), input);
    });
    this.el = el("aside.img-color", { hidden: true, "aria-label": "Adjust Color" },
      el("div.img-color-head", {}, el("h2", { text: "Adjust Color" }), this.auto),
      ...sliders,
      el("div.img-color-actions", {},
        button({ label: "Reset", title: "Reset", onClick: () => { this.set(COLOR_DEFAULTS); this.onChange(); } }),
        button({ label: "Cancel", title: "Cancel (Esc)", onClick: () => this.onClose(false) }),
        button({ label: "Apply", title: "Apply", suggested: true, onClick: () => this.onClose(true) })));
  }

  get open(): boolean { return !this.el.hidden; }

  set(p: ColorParams): void {
    for (const [k, i] of this.inputs) i.value = String(p[k]);
    this.auto.setAttribute("aria-pressed", String(p.auto));
  }

  params(): ColorParams {
    const p = { ...COLOR_DEFAULTS } as ColorParams;
    for (const [k, i] of this.inputs) (p as unknown as Record<string, number>)[k] = Math.round(Number(i.value) * 1000) / 1000;
    p.auto = this.auto.getAttribute("aria-pressed") === "true";
    return p;
  }
}
