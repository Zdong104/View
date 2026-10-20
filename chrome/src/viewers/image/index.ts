// Image viewer and editor (port of preview/image_view.py): zoom/pan/pinch, rotate,
// flip, crop (rect/ellipse), Instant Alpha, Adjust Color, Adjust Size, markup
// (ctx.services.markup, flattened on save), Live Text (ctx.services.ocr), EXIF/GPS
// in Get Info, animated GIF/WebP/APNG kept animated on save, a multi-image
// sidebar with batch convert and Export as PDF, slideshow, copy and print.
//
// Each image is an Item with an op list (ops.ts) applied in the worker
// (worker.ts); markup is live in a per-item MarkupController and is committed into
// the op list (as a rasterized layer) before any other edit, like the desktop.
import type { Annotation, MarkupController, MarkupSurface } from "../../markup/api.ts";
import { kindFromMime, kindFromName } from "../../shared/kinds.ts";
import { displayChord } from "../../shell/shortcuts.ts";
import { button, el, linked, loadStyle, menuButton } from "../../ui/kit.ts";
import type {
  DocumentSource, ExportFormat, InfoSection, SaveRequest, SaveResult, Viewer, ViewerContext, ViewerModule,
} from "../contract.ts";
import { ImageWorker } from "./client.ts";
import type { OutFmt } from "./encode.ts";
import { FMT_LABEL, sniff, type Fmt } from "./formats.ts";
import { imageInfo } from "./info.ts";
import { LiveText } from "./livetext.ts";
import {
  appendOp, COLOR_DEFAULTS, colorIsNeutral, opName, opsNeedAlpha, sizeAfter, sizeAfterOps, type Op, type WorkerOp,
} from "./ops.ts";
import {
  BATCH_FORMATS, ColorPanel, EXPORT_FORMATS, exportOptions, type ExportChoice, type FormatSpec, pdfOptions, sizeDialog,
} from "./panels.ts";
import { ImageCanvas, type CanvasTool } from "./view.ts";
import type { EncodeResult, Frame, OpenResult } from "./worker.ts";
import type { PdfOptions } from "../../convert/pdf.ts";
import { pickFolder, writeInto, zipFiles } from "../../convert/output.ts";

const DISPLAY_MAX = 16384;   // longest side of the displayed bitmap
const DISPLAY_AREA = 50e6;   // ... and its area (beyond that the display is scaled down)
const ANIM_MAX = 2048;       // animations are played at most this size
const THUMB_PX = 208;        // sidebar thumbnails (104 CSS px rows on 2x displays)
const PREVIEW_PX = 1024;     // Adjust Color live preview
const SLIDESHOW_MS = 4000;

interface Snap { ops: Op[]; anns: Annotation[] }

interface Item {
  id: number;
  src: DocumentSource;
  name: string;
  /** The document the shell opened (the shell owns its handle and File section). */
  primary: boolean;
  ops: Op[];
  history: Snap[];
  future: Snap[];
  mk: MarkupController | null;
  mkOff?: () => void;
  opened?: Promise<OpenResult>;
  info?: OpenResult;
  error?: string;
  rendered?: [number, number];
  durations?: number[];
  row?: HTMLElement;
  thumb?: HTMLCanvasElement;
  thumbDone?: boolean;
  bytes?: Promise<Uint8Array>;
}

interface ColorState { it: Item; baseOps: Op[]; ready: boolean; busy: boolean; pending: boolean }

const EXT_FMT: Record<string, OutFmt> = {
  png: "png", apng: "apng", jpg: "jpeg", jpeg: "jpeg", jpe: "jpeg", jfif: "jpeg", webp: "webp",
  gif: "gif", bmp: "bmp", tif: "tiff", tiff: "tiff",
};
const OUT_SPEC = (f: OutFmt): FormatSpec => EXPORT_FORMATS.find((s) => s.id === f)!;

const stemOf = (name: string) => { const i = name.lastIndexOf("."); return i > 0 ? name.slice(0, i) : name; };
const extOfName = (name: string) => { const i = name.lastIndexOf("."); return i > 0 ? name.slice(i + 1).toLowerCase() : ""; };
const sameOps = (a: Op[], b: Op[]) => a.length === b.length && a.every((o, i) => o === b[i] || JSON.stringify(o) === JSON.stringify(b[i]));
const isImage = (s: DocumentSource) => kindFromName(s.name) === "image" || kindFromMime(s.mime) === "image" || /\.svg$/i.test(s.name);

function memSource(name: string, blob: Blob, handle?: FileSystemFileHandle): DocumentSource {
  return {
    name, handle, mime: blob.type || null, size: blob.size, lastModified: Date.now(), streamable: false,
    blob: async () => blob,
    head: async (n = 8192) => new Uint8Array(await blob.slice(0, n).arrayBuffer()),
  };
}

async function rasterizeSvg(blob: Blob): Promise<ImageBitmap> {
  const url = URL.createObjectURL(blob.type === "image/svg+xml" ? blob : new Blob([blob], { type: "image/svg+xml" }));
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    let w = img.naturalWidth, h = img.naturalHeight;
    if (!w || !h) w = h = 2048;
    else { const s = Math.max(1, 2048 / Math.max(w, h)); w = Math.round(w * s); h = Math.round(h * s); }
    const c = el("canvas", { width: w, height: h });
    c.getContext("2d")!.drawImage(img, 0, 0, w, h);
    return await createImageBitmap(c);
  } finally {
    URL.revokeObjectURL(url);
  }
}

function decodeError(fmt: Fmt, name: string): string {
  if (fmt === "heic") return "HEIC images can't be opened in Chrome (the only decoders are LGPL-licensed). Convert it to JPEG or PNG first.";
  if (fmt === "jxl") return "JPEG XL images can't be opened in Chrome yet.";
  return `View couldn't decode “${name}”.`;
}

class ImageViewer implements Viewer {
  private ctx!: ViewerContext;
  private worker!: ImageWorker;
  private items: Item[] = [];
  private current: Item | null = null;
  private selected = new Set<Item>();
  private anchor: Item | null = null;
  private nextId = 1;
  private gen = 0;

  private root = el("div.img-viewer", { "data-viewer": "image", "data-state": "loading" });
  private view!: ImageCanvas;
  private live!: LiveText;
  private color = new ColorPanel();
  private colorState: ColorState | null = null;
  private markupBar = el("div.img-markup-bar", { hidden: true });
  private hint = el("div.img-hint", { hidden: true });
  private hintLabel = el("span");
  private tol = el("input.img-slider", { type: "range", min: 0, max: 100, step: 1, value: 25, "aria-label": "Tolerance", title: "Tolerance" });
  private cropBtn = button({ label: "Crop", title: "Crop to Selection (Ctrl+K)", onClick: () => void this.crop() });
  private status = el("div.img-status", { hidden: true, role: "status" });
  private errorEl = el("div.img-error", { hidden: true });
  private list = el("div.img-list", { role: "listbox", "aria-label": "Images", "aria-multiselectable": "true", tabindex: 0 });
  private listLabel = el("span.img-list-label");
  private zoomLabel = button({ label: "100%", title: "Toggle Fit / Actual Size" });
  private markupBtn = button({ icon: "markup", title: "Markup (Ctrl+Shift+A)", toggle: true, onClick: () => void this.toggleMarkup() });

  private frames: Frame[] | null = null;
  private animTimer = 0;
  private markupVisible = false;
  private surface: MarkupSurface | null = null;
  private ocrTimer = 0;
  private ocrAbort: AbortController | null = null;
  private slideTimer = 0;
  private layers = new WeakMap<Op, Promise<ImageBitmap>>();
  private printEl: HTMLElement | null = null;

  // ---- lifecycle ------------------------------------------------------------------

  async mount(container: HTMLElement, source: DocumentSource, ctx: ViewerContext): Promise<void> {
    this.ctx = ctx;
    const [worker] = await Promise.all([ImageWorker.start(), loadStyle("image")]);
    this.worker = worker;
    ctx.signal.addEventListener("abort", () => worker.terminate());
    this.view = new ImageCanvas(ctx.signal);
    this.live = new LiveText(this.view.textLayer);
    this.buildUi(container);
    this.buildHeader();
    this.buildSidebar();
    this.buildShortcuts();
    const it = this.newItem(source, true);
    this.items.push(it);
    this.selected = new Set([it]);
    this.renderList();
    await this.show(it);
    this.view.viewport.focus({ preventScroll: true });
  }

  destroy(): void {
    this.stopAnim();
    this.stopSlideshow();
    clearTimeout(this.ocrTimer);
    this.ocrAbort?.abort();
    this.surface?.detach();
    for (const it of this.items) { it.mkOff?.(); it.mk?.destroy(); }
    this.frames?.forEach((f) => f.bmp.close());
    this.printEl?.remove();
    this.root.remove();
  }

  private buildUi(container: HTMLElement): void {
    this.hint.append(this.hintLabel, this.tol, this.cropBtn,
      button({ label: "Done", title: "Done (Esc)", onClick: () => this.setTool("pan") }));
    const main = el("div.img-main", {}, this.markupBar, this.view.viewport);
    this.view.viewport.append(this.hint, this.status, this.errorEl);
    this.root.append(main, this.color.el);
    container.append(this.root);
    const v = this.view;
    v.onView = () => {
      const s = v.effZoom();
      this.zoomLabel.querySelector(".btn-label")!.textContent = `${Math.round(s * 100)}%`;
      this.surface?.relayout();
    };
    v.onSel = () => { this.cropBtn.disabled = !v.sel; };
    v.onAlpha = (x, y) => void this.instantAlpha(x, y);
    v.markupActive = (t) => {
      const mk = this.current?.mk;
      if (!this.markupVisible || !mk) return false;
      if (mk.tool !== "select") return true;
      return !!t && t !== v.overlay && !(t instanceof SVGSVGElement); // an annotation was hit
    };
    this.color.onChange = () => void this.colorChanged();
    this.color.onClose = (apply) => void this.colorClose(apply);
    this.zoomLabel.addEventListener("click", () => this.zoomToggle());
    document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement) this.stopSlideshow(); }, { signal: this.ctx.signal });
  }

  private buildHeader(): void {
    const { start, tools } = this.ctx.header;
    const left = button({ icon: "rotateLeft", title: "Rotate Left (Ctrl+L)", onClick: () => void this.transform({ k: "rotate", deg: 270 }) });
    start.append(left, button({ icon: "rotate", title: "Rotate Right (Ctrl+R)", onClick: () => void this.transform({ k: "rotate", deg: 90 }) }));
    const acc = (k: string) => displayChord(k);
    const menu = menuButton({ icon: "tools", title: "Tools" }, [
      ["Move and Zoom", () => this.setTool("pan")],
      ["Rectangular Selection", () => this.setTool("rect")],
      ["Elliptical Selection", () => this.setTool("ellipse")],
      ["Instant Alpha", () => this.setTool("alpha")],
      "-",
      ["Crop to Selection", () => void this.crop(), acc("Mod+K")],
      ["Adjust Color…", () => void this.colorOpen(), acc("Mod+Shift+C")],
      ["Adjust Size…", () => void this.adjustSize()],
      ["Flip Horizontal", () => void this.transform({ k: "flip" })],
      ["Flip Vertical", () => void this.transform({ k: "flip", vertical: true })],
      "-",
      ["Undo", () => void this.undo(), acc("Mod+Z")],
      ["Redo", () => void this.redo(), acc("Mod+Shift+Z")],
      ["Revert to Saved", () => void this.revert()],
      "-",
      ["Recognize Text", () => void this.runOcr(true)],
      ["Show Recognized Text", () => this.live.showBoxes(!this.live.boxesShown)],
      ["Copy All Text", () => void this.copyAllText()],
      "-",
      ["Export as PDF…", () => void this.ctx.requestSave({ mode: "export", format: "pdf" })],
      ["Export Selected Images…", () => void this.batchDialog()],
      ["Slideshow", () => this.toggleSlideshow(), "F5"],
    ]);
    menu.classList.add("img-tools");
    tools.append(menu,
      linked(
        button({ icon: "zoomOut", title: "Zoom Out (−)", onClick: () => this.view.zoomBy(0.8) }),
        this.zoomLabel,
        button({ icon: "zoomIn", title: "Zoom In (+)", onClick: () => this.view.zoomBy(1.25) }),
        button({ icon: "zoomFit", title: "Fit to Window (0)", onClick: () => this.view.zoomFit() }),
      ),
      this.markupBtn,
      button({ icon: "document", title: "Export as PDF…", onClick: () => void this.ctx.requestSave({ mode: "export", format: "pdf" }) }));
    this.zoomLabel.classList.add("img-zoom-label");
  }

  private buildSidebar(): void {
    const bar = el("div.img-listbar", {},
      button({ icon: "add", title: "Add Images…", onClick: () => void this.addDialog() }),
      button({ icon: "remove", title: "Remove from List (Delete)", onClick: () => this.removeSelected() }),
      this.listLabel,
      button({ icon: "share", title: "Export Selected Images…", onClick: () => void this.batchDialog() }));
    this.ctx.sidebar.el.append(el("div.img-side", {}, this.list, bar));
    this.list.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); e.stopPropagation(); this.step(e.key === "ArrowDown" ? 1 : -1); }
    }, { signal: this.ctx.signal });
  }

  private buildShortcuts(): void {
    const sc = this.ctx.shortcuts;
    const add = (keys: string[], description: string, run: (e: KeyboardEvent) => boolean | void) => {
      for (const k of keys) sc.add({ keys: k, description, run });
    };
    add(["+", "=", "Mod+=", "Mod++"], "Zoom in", () => this.view.zoomBy(1.25));
    add(["-", "Mod+-"], "Zoom out", () => this.view.zoomBy(0.8));
    add(["0", "Mod+0"], "Fit to window", () => this.view.zoomFit());
    add(["1", "Mod+1"], "Actual size", () => this.view.zoomTo(1));
    add(["Mod+L"], "Rotate left", () => void this.transform({ k: "rotate", deg: 270 }));
    add(["Mod+R"], "Rotate right", () => void this.transform({ k: "rotate", deg: 90 }));
    add(["Mod+K"], "Crop to selection", () => void this.crop());
    add(["Mod+Shift+C"], "Adjust color", () => void this.colorOpen());
    add(["Mod+Z"], "Undo", () => void this.undo());
    add(["Mod+Shift+Z", "Mod+Y"], "Redo", () => void this.redo());
    add(["Mod+A"], "Select all images", () => this.selectAll());
    add(["Mod+C"], "Copy image", () => {
      if (this.live.hasSelection() || String(document.getSelection() ?? "").length) return false; // copying Live Text
      void this.copyImage();
    });
    add(["Mod+Shift+A"], "Markup", () => void this.toggleMarkup());
    add(["Delete"], "Delete", () => this.deleteKey());
    add(["ArrowLeft"], "Previous image", () => this.step(-1));
    add(["ArrowRight"], "Next image", () => this.step(1));
    add(["Escape"], "Cancel", () => this.cancel());
    add(["F5"], "Slideshow", () => this.toggleSlideshow());
  }

  // ---- items ------------------------------------------------------------------------

  private newItem(src: DocumentSource, primary = false): Item {
    return { id: this.nextId++, src, name: src.name, primary, ops: [], history: [], future: [], mk: null };
  }

  private ensureOpen(it: Item): Promise<OpenResult> {
    it.opened ??= (async () => {
      const blob = await it.src.blob();
      const head = new Uint8Array(await blob.slice(0, 4096).arrayBuffer());
      const fmt = sniff(head);
      try {
        const base = fmt === "svg" || /svg/i.test(it.src.mime ?? "") ? await rasterizeSvg(blob) : undefined;
        const info = await this.worker.call({ t: "open", id: it.id, blob, base }, { transfer: base ? [base] : [] });
        it.info = info;
        return info;
      } catch (e) {
        console.warn(e);
        it.error = decodeError(fmt, it.name);
        throw new Error(it.error);
      }
    })();
    return it.opened;
  }

  private bytesOf(it: Item): Promise<Uint8Array> {
    it.bytes ??= it.src.blob().then(async (b) => new Uint8Array(await b.arrayBuffer()));
    return it.bytes;
  }

  private finalOps(it: Item): Op[] {
    const anns = it.mk?.toJSON() ?? [];
    return anns.length ? [...it.ops, { k: "markup", anns }] : it.ops;
  }

  private itemDirty(it: Item): boolean {
    return it.ops.length > 0 || (it.mk?.list().length ?? 0) > 0;
  }

  /** Markup ops become pre-rendered layers (MarkupController.render) for the worker. */
  private async workerOps(it: Item, ops: Op[]): Promise<WorkerOp[]> {
    const info = it.info ?? await this.ensureOpen(it);
    let [w, h] = [info.w, info.h];
    const out: WorkerOp[] = [];
    for (const op of ops) {
      if (op.k === "markup") {
        let p = this.layers.get(op);
        if (!p) {
          const [lw, lh] = [w, h];
          p = (async () => {
            const c = el("canvas", { width: lw, height: lh });
            const m = await this.ctx.services.markup({ docKind: "image", initial: op.anns });
            try { m.render(c.getContext("2d")!, "img", 1); } finally { m.destroy(); }
            return createImageBitmap(c);
          })();
          this.layers.set(op, p);
        }
        out.push({ k: "layer", bmp: await p });
      } else out.push(op);
      [w, h] = sizeAfter(op, w, h);
    }
    return out;
  }

  // ---- showing / rendering ---------------------------------------------------------

  private async show(it: Item, keepView = false): Promise<void> {
    if (this.current !== it) {
      if (this.colorState) await this.colorClose(false);
      this.clearOcr();
      this.stopAnim();
      this.surface?.detach();
      this.surface = null;
      this.current = it;
      this.view.setSel(null);
      keepView = false;
    }
    this.updateRows();
    this.errorEl.hidden = true;
    this.root.dataset.state = "loading";
    this.ctx.refreshTitle();
    try {
      await this.ensureOpen(it);
    } catch (e) {
      if (this.current !== it) return;
      this.view.clear();
      this.errorEl.textContent = (e as Error).message;
      this.errorEl.hidden = false;
      this.root.dataset.state = "error";
      this.ctx.refreshTitle();
      return;
    }
    if (this.current === it) await this.render(it, keepView);
  }

  private displayMax(w: number, h: number, anim: boolean): number {
    if (anim) return ANIM_MAX;
    const long = Math.max(w, h), short = Math.max(1, Math.min(w, h));
    return Math.min(DISPLAY_MAX, Math.floor(Math.sqrt((DISPLAY_AREA * long) / short)));
  }

  private async render(it: Item, keepView = true): Promise<void> {
    const gen = ++this.gen;
    const info = it.info!;
    this.root.dataset.state = "loading";
    try {
      const ops = await this.workerOps(it, it.ops);
      const [fw, fh] = sizeAfterOps(it.ops, info.w, info.h);
      const r = await this.worker.call({ t: "render", id: it.id, ops, max: this.displayMax(fw, fh, !!info.anim), animate: !!info.anim });
      if (gen !== this.gen || this.current !== it) { r.frames.forEach((f) => f.bmp.close()); return; }
      this.stopAnim();
      this.frames?.forEach((f) => f.bmp.close());
      this.frames = r.frames;
      it.rendered = [r.w, r.h];
      if (info.anim) it.durations = r.frames.map((f) => f.duration);
      this.view.setContent(r.frames[0]!.bmp, r.w, r.h, keepView);
      if (r.frames.length > 1) this.playAnim();
      this.drawThumb(it, r.frames[0]!.bmp);
      await this.attachMarkup(it);
      this.scheduleOcr(it);
      Object.assign(this.root.dataset, {
        state: "ready", size: `${r.w}x${r.h}`, ops: String(it.ops.length), frames: String(r.frames.length),
      });
    } catch (e) {
      if (gen !== this.gen) return;
      this.root.dataset.state = "error";
      this.ctx.toast(`Couldn't render the image: ${(e as Error).message}`, { timeout: 6000 });
    }
    this.ctx.refreshTitle();
  }

  private playAnim(): void {
    const frames = this.frames;
    if (!frames || frames.length < 2) return;
    let i = 0;
    const tick = () => {
      if (this.frames !== frames || this.colorState) return;
      i = (i + 1) % frames.length;
      this.view.draw(frames[i]!.bmp);
      this.animTimer = window.setTimeout(tick, Math.max(20, frames[i]!.duration || 100));
    };
    this.animTimer = window.setTimeout(tick, Math.max(20, frames[0]!.duration || 100));
  }

  private stopAnim(): void {
    clearTimeout(this.animTimer);
    this.animTimer = 0;
  }

  // ---- edits / history ---------------------------------------------------------------

  private snap(it: Item): Snap {
    return { ops: [...it.ops], anns: it.mk?.toJSON() ?? [] };
  }

  /** Flatten live markup into the op list (before edits that change pixels or geometry). */
  private async commitMarkup(it: Item): Promise<void> {
    const anns = it.mk?.toJSON() ?? [];
    if (!anns.length) return;
    it.history.push(this.snap(it));
    it.future = [];
    it.ops = [...it.ops, { k: "markup", anns }];
    await this.resetMarkup(it, []);
  }

  private async edit(it: Item, fn: (ops: Op[]) => Op[]): Promise<void> {
    await this.ensureOpen(it);
    await this.commitMarkup(it);
    const next = fn(it.ops);
    if (sameOps(next, it.ops)) return;
    it.history.push(this.snap(it));
    it.future = [];
    it.ops = next;
    await this.afterEdit(it);
  }

  private async afterEdit(it: Item, render = true): Promise<void> {
    this.ctx.setDirty(this.isDirty());
    this.updateRow(it);
    if (it === this.current) {
      this.view.setSel(null);
      if (render) {
        this.clearOcr();
        await this.render(it, true);
      }
    } else if (render) {
      void this.loadThumb(it, true);
    }
    this.ctx.refreshTitle();
  }

  private targets(): Item[] {
    const sel = this.items.filter((i) => this.selected.has(i));
    return sel.length > 1 ? sel : this.current ? [this.current] : [];
  }

  private async transform(op: Op): Promise<void> {
    for (const it of this.targets()) {
      if (it.error) continue;
      await this.edit(it, (ops) => appendOp(ops, op)).catch(() => {});
    }
  }

  private async undo(): Promise<void> {
    const it = this.current;
    if (!it) return;
    if (it.mk?.canUndo) { it.mk.undo(); return; }
    const s = it.history.pop();
    if (!s) return;
    it.future.push(this.snap(it));
    await this.restore(it, s);
  }

  private async redo(): Promise<void> {
    const it = this.current;
    if (!it) return;
    if (it.mk?.canRedo) { it.mk.redo(); return; }
    const s = it.future.pop();
    if (!s) return;
    it.history.push(this.snap(it));
    await this.restore(it, s);
  }

  private async restore(it: Item, s: Snap): Promise<void> {
    const changed = !sameOps(s.ops, it.ops);
    it.ops = [...s.ops];
    await this.resetMarkup(it, s.anns);
    await this.afterEdit(it, changed);
  }

  private async revert(): Promise<void> {
    const it = this.current;
    if (!it || !this.itemDirty(it)) return;
    it.history.push(this.snap(it));
    it.future = [];
    const changed = it.ops.length > 0;
    it.ops = [];
    await this.resetMarkup(it, []);
    await this.afterEdit(it, changed);
    this.ctx.toast("Reverted to the saved version", { actionLabel: "Undo", onAction: () => void this.undo() });
  }

  // ---- tools ---------------------------------------------------------------------------

  private setTool(t: CanvasTool): void {
    this.view.setTool(t);
    const hints: Partial<Record<CanvasTool, string>> = {
      rect: "Drag to select an area · Ctrl+K crops",
      ellipse: "Drag to select an ellipse · Ctrl+K crops",
      alpha: "Click a colour to make it transparent",
    };
    this.hintLabel.textContent = hints[t] ?? "";
    this.tol.hidden = t !== "alpha";
    this.cropBtn.hidden = t !== "rect" && t !== "ellipse";
    this.cropBtn.disabled = !this.view.sel;
    this.hint.hidden = t === "pan";
    this.view.viewport.focus({ preventScroll: true });
  }

  private async crop(): Promise<void> {
    const it = this.current, sel = this.view.sel;
    if (!it?.rendered) return;
    if (!sel) { this.ctx.toast("Select an area first (Tools ▸ Rectangular Selection)"); return; }
    const [w, h] = it.rendered;
    const x0 = Math.max(0, Math.round(sel[0])), y0 = Math.max(0, Math.round(sel[1]));
    const x1 = Math.min(w, Math.round(sel[2])), y1 = Math.min(h, Math.round(sel[3]));
    if (x1 - x0 < 1 || y1 - y0 < 1) return;
    const k = this.view.tool === "ellipse" ? "ellipse" : "crop";
    this.view.setSel(null);
    this.setTool("pan");
    await this.edit(it, (ops) => [...ops, { k, x0, y0, x1, y1 }]);
  }

  private async instantAlpha(x: number, y: number): Promise<void> {
    const it = this.current;
    if (!it?.rendered) return;
    await this.edit(it, (ops) => [...ops, { k: "alpha", x: Math.trunc(x), y: Math.trunc(y), tol: Number(this.tol.value) }]);
  }

  private async adjustSize(): Promise<void> {
    const it = this.current;
    if (!it?.rendered) return;
    const [bw, bh] = it.rendered;
    const r = await sizeDialog(bw, bh);
    if (!r || (r[0] === bw && r[1] === bh)) return;
    await this.edit(it, (ops) => [...ops, { k: "resize", w: r[0], h: r[1] }]);
  }

  private zoomToggle(): void {
    const v = this.view;
    if (v.fit && v.fitZoom() < 1) v.zoomTo(1); else v.zoomFit();
  }

  // ---- adjust colour ----------------------------------------------------------------

  private async colorOpen(): Promise<void> {
    const it = this.current;
    if (!it?.rendered || this.colorState) return;
    await this.commitMarkup(it);
    const last = it.ops[it.ops.length - 1];
    const params = last?.k === "color" ? { ...COLOR_DEFAULTS, ...last.p } : { ...COLOR_DEFAULTS };
    const baseOps = last?.k === "color" ? it.ops.slice(0, -1) : [...it.ops];
    const c: ColorState = { it, baseOps, ready: false, busy: false, pending: false };
    this.colorState = c;
    this.stopAnim(); // the live preview is single-frame; playback resumes on close
    this.color.set(params);
    this.color.el.hidden = false;
    this.root.classList.add("color-open");
    try {
      await this.worker.call({ t: "colorStart", id: it.id, ops: await this.workerOps(it, baseOps), max: PREVIEW_PX });
    } catch (e) {
      this.ctx.toast(`Couldn't adjust colour: ${(e as Error).message}`);
      await this.colorClose(false);
      return;
    }
    c.ready = true;
    await this.colorChanged();
    (this.color.el.querySelector("input") as HTMLElement | null)?.focus();
  }

  private async colorChanged(): Promise<void> {
    const c = this.colorState;
    if (!c?.ready) return;
    if (c.busy) { c.pending = true; return; }
    c.busy = true;
    c.pending = false;
    try {
      const r = await this.worker.call({ t: "colorPreview", id: c.it.id, p: this.color.params() });
      if (this.colorState === c && this.current === c.it) {
        this.view.draw(r.bmp);
        this.root.dataset.preview = String(Number(this.root.dataset.preview ?? 0) + 1);
      }
      r.bmp.close();
    } finally {
      c.busy = false;
    }
    if (c.pending && this.colorState === c) await this.colorChanged();
  }

  private async colorClose(apply: boolean): Promise<void> {
    const c = this.colorState;
    if (!c) return;
    const p = this.color.params();
    this.colorState = null;
    this.color.el.hidden = true;
    this.root.classList.remove("color-open");
    void this.worker.call({ t: "colorEnd", id: c.it.id }).catch(() => {});
    const next = [...c.baseOps, ...(colorIsNeutral(p) ? [] : [{ k: "color", p } as Op])];
    if (apply && !sameOps(next, c.it.ops)) await this.edit(c.it, () => next);
    else if (this.current === c.it) await this.render(c.it, true);
  }

  // ---- markup ------------------------------------------------------------------------

  private async toggleMarkup(show = !this.markupVisible): Promise<void> {
    this.markupVisible = show;
    this.markupBtn.setAttribute("aria-pressed", String(show));
    this.root.classList.toggle("markup-on", show);
    const it = this.current;
    if (show && it && !it.mk) await this.resetMarkup(it, []);
    else if (it) await this.attachMarkup(it);
  }

  /** Replace the item's controller with a fresh one holding `anns` (undo history resets). */
  private async resetMarkup(it: Item, anns: Annotation[]): Promise<void> {
    if (it === this.current) { this.surface?.detach(); this.surface = null; }
    it.mkOff?.();
    it.mk?.destroy();
    it.mk = null;
    it.mkOff = undefined;
    if (anns.length || (this.markupVisible && it === this.current)) {
      const mk = await this.ctx.services.markup({ docKind: "image", initial: anns });
      it.mk = mk;
      it.mkOff = mk.on("change", () => {
        it.future = [];
        this.ctx.setDirty(this.isDirty());
        this.updateRow(it);
        this.ctx.refreshTitle();
      });
    }
    if (it === this.current) await this.attachMarkup(it);
  }

  private async attachMarkup(it: Item): Promise<void> {
    if (it !== this.current) return;
    this.surface?.detach();
    this.surface = null;
    if (!it.mk && this.markupVisible && it.rendered) { await this.resetMarkup(it, []); return; }
    const mk = it.mk;
    this.markupBar.hidden = !this.markupVisible || !mk;
    if (!mk || !it.rendered) { this.markupBar.replaceChildren(); return; }
    const [w, h] = it.rendered;
    const v = this.view;
    this.surface = mk.attach({
      surface: "img", width: w, height: h, overlay: v.overlay,
      scale: () => v.effZoom(), rotation: () => 0,
      textRuns: async () => this.live.runs(),
    });
    if (this.markupVisible) this.markupBar.replaceChildren(mk.toolbar());
  }

  // ---- Live Text ------------------------------------------------------------------------

  private clearOcr(): void {
    clearTimeout(this.ocrTimer);
    this.ocrAbort?.abort();
    this.ocrAbort = null;
    this.live.clear();
  }

  /** Recognise text shortly after an image is shown (like Live Text). */
  private scheduleOcr(it: Item): void {
    this.clearOcr();
    if (it.info?.anim) return;
    this.ocrTimer = window.setTimeout(() => void this.runOcr(false), 700);
  }

  private async runOcr(force: boolean): Promise<void> {
    const it = this.current;
    if (!it?.rendered || !this.view.hasContent) return;
    this.clearOcr();
    const abort = new AbortController();
    this.ocrAbort = abort;
    try {
      const svc = await this.ctx.services.ocr();
      const bmp = await createImageBitmap(this.view.canvas);
      const k = it.rendered[0] / Math.max(1, bmp.width);
      const res = await svc.recognize(bmp, { signal: abort.signal });
      if (abort.signal.aborted || this.current !== it) return;
      this.live.set(res, k);
      if (force) {
        this.ctx.toast(res.lines.length ? `Recognized ${res.lines.length} line${res.lines.length === 1 ? "" : "s"} of text` : "No text found");
        if (res.lines.length) this.live.showBoxes(true);
      }
    } catch (e) {
      if (force && !abort.signal.aborted) this.ctx.toast(`Text recognition is unavailable: ${(e as Error).message}`);
    }
  }

  private async copyAllText(): Promise<void> {
    if (!this.live.hasText()) { this.ctx.toast("No recognized text"); return; }
    await navigator.clipboard.writeText(this.live.text());
    this.ctx.toast("All text copied");
  }

  // ---- sidebar list ---------------------------------------------------------------------

  private renderList(): void {
    this.list.replaceChildren(...this.items.map((it) => this.rowFor(it)));
    this.updateRows();
    for (const it of this.items) if (!it.thumbDone) void this.loadThumb(it);
  }

  private rowFor(it: Item): HTMLElement {
    if (it.row) return it.row;
    const thumb = el("canvas.img-thumb", { width: 1, height: 1 });
    const row = el("div.img-row", { role: "option", draggable: "true", "data-id": it.id },
      thumb, el("span.img-row-name"));
    it.row = row;
    it.thumb = thumb;
    row.addEventListener("click", (e) => this.rowClick(it, e));
    row.addEventListener("dragstart", (e) => {
      e.dataTransfer?.setData("application/x-view-image", String(it.id));
      if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
    });
    row.addEventListener("dragover", (e) => {
      if (!e.dataTransfer?.types.includes("application/x-view-image")) return;
      e.preventDefault();
      const after = e.offsetY > row.clientHeight / 2;
      row.classList.toggle("drop-before", !after);
      row.classList.toggle("drop-after", after);
    });
    row.addEventListener("dragleave", () => row.classList.remove("drop-before", "drop-after"));
    row.addEventListener("drop", (e) => {
      const id = Number(e.dataTransfer?.getData("application/x-view-image"));
      if (!id) return;
      e.preventDefault();
      e.stopPropagation();
      const after = row.classList.contains("drop-after");
      row.classList.remove("drop-before", "drop-after");
      this.moveItems(id, this.items.indexOf(it) + (after ? 1 : 0));
    });
    this.updateRow(it);
    return row;
  }

  private updateRow(it: Item): void {
    if (!it.row) return;
    it.row.querySelector(".img-row-name")!.textContent = it.name + (this.itemDirty(it) ? " •" : "");
    it.row.setAttribute("aria-selected", String(this.selected.has(it)));
    it.row.classList.toggle("current", it === this.current);
  }

  private updateRows(): void {
    for (const it of this.items) this.updateRow(it);
    const n = this.items.length, k = this.selected.size;
    this.listLabel.textContent = k > 1 ? `${k} of ${n} selected` : `${n} image${n === 1 ? "" : "s"}`;
    this.root.dataset.items = String(n);
    this.root.dataset.selected = String(k);
  }

  private rowClick(it: Item, e: MouseEvent): void {
    if (e.shiftKey && this.anchor) {
      const a = this.items.indexOf(this.anchor), b = this.items.indexOf(it);
      this.selected = new Set(this.items.slice(Math.min(a, b), Math.max(a, b) + 1));
    } else if (e.ctrlKey || e.metaKey) {
      if (this.selected.has(it) && this.selected.size > 1) this.selected.delete(it);
      else this.selected.add(it);
      this.anchor = it;
    } else {
      this.selected = new Set([it]);
      this.anchor = it;
    }
    this.updateRows();
    if (this.selected.has(it)) void this.show(it);
  }

  private moveItems(id: number, target: number): void {
    const src = this.items.find((i) => i.id === id);
    if (!src) return;
    const moving = this.selected.has(src) && this.selected.size > 1 ? this.items.filter((i) => this.selected.has(i)) : [src];
    const set = new Set(moving);
    const next = [...this.items.slice(0, target).filter((i) => !set.has(i)), ...moving, ...this.items.slice(target).filter((i) => !set.has(i))];
    this.items = next;
    this.renderList();
    this.ctx.refreshTitle();
  }

  private drawThumb(it: Item, bmp: ImageBitmap): void {
    const c = it.thumb;
    if (!c) return;
    const s = Math.min(1, THUMB_PX / Math.max(bmp.width, bmp.height));
    c.width = Math.max(1, Math.round(bmp.width * s));
    c.height = Math.max(1, Math.round(bmp.height * s));
    const g = c.getContext("2d")!;
    g.imageSmoothingQuality = "high";
    g.drawImage(bmp, 0, 0, c.width, c.height);
    it.thumbDone = true;
  }

  private async loadThumb(it: Item, force = false): Promise<void> {
    if ((it.thumbDone && !force) || !it.thumb) return;
    it.thumbDone = true;
    try {
      await this.ensureOpen(it);
      const r = await this.worker.call({ t: "thumb", id: it.id, ops: await this.workerOps(it, it.ops), size: THUMB_PX });
      this.drawThumb(it, r.bmp);
      r.bmp.close();
    } catch {
      it.row?.classList.add("broken");
    }
  }

  private step(delta: number, wrap = false): void {
    const n = this.items.length;
    if (n < 2 || !this.current) return;
    const cur = this.items.indexOf(this.current);
    const t = wrap ? (cur + delta + n) % n : Math.max(0, Math.min(n - 1, cur + delta));
    if (t === cur && this.selected.size <= 1) return;
    const it = this.items[t]!;
    this.selected = new Set([it]);
    this.anchor = it;
    it.row?.scrollIntoView({ block: "nearest" });
    void this.show(it);
  }

  private selectAll(): void {
    if (this.items.length < 2) return;
    this.selected = new Set(this.items);
    this.updateRows();
    this.ctx.sidebar.setVisible(true);
  }

  private removeSelected(): void {
    const n = this.items.length;
    const sel = this.items.filter((i) => this.selected.has(i));
    const pos = sel.length ? sel : this.current ? [this.current] : [];
    if (!pos.length || n <= 1) return;
    if (pos.length >= n) { this.ctx.toast("At least one image has to stay open"); return; }
    if (pos.some((i) => this.itemDirty(i)) && !confirm("Remove images with unsaved changes from the list?")) return;
    const first = this.items.indexOf(pos[0]!);
    for (const it of pos) {
      it.mkOff?.();
      it.mk?.destroy();
      void this.worker.call({ t: "close", id: it.id }).catch(() => {});
    }
    const drop = new Set(pos);
    this.items = this.items.filter((i) => !drop.has(i));
    const next = this.items[Math.min(first, this.items.length - 1)]!;
    this.selected = new Set([next]);
    this.anchor = next;
    this.renderList();
    this.ctx.setDirty(this.isDirty());
    void this.show(next);
  }

  private deleteKey(): void {
    const mk = this.current?.mk;
    if (mk?.selection.length) { mk.remove(mk.selection); return; }
    this.removeSelected();
  }

  private cancel(): boolean | void {
    const mk = this.current?.mk;
    if (mk?.selection.length) { mk.select([]); return; }
    if (this.live.hasSelection()) { this.live.clearSelection(); return; }
    if (this.slideTimer) { this.stopSlideshow(); return; }
    if (this.colorState) { void this.colorClose(false); return; }
    if (this.view.sel) { this.view.setSel(null); return; }
    if (this.view.tool !== "pan") { this.setTool("pan"); return; }
    return false;
  }

  private async addDialog(): Promise<void> {
    let handles: FileSystemFileHandle[];
    try {
      handles = await window.showOpenFilePicker({
        multiple: true,
        types: [{ description: "Images", accept: { "image/*": [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico", ".avif", ".svg", ".apng"] } }],
      });
    } catch {
      return;
    }
    await this.addDropped(await Promise.all(handles.map(async (h) => {
      const f = await h.getFile();
      return memSource(f.name, f, h);
    })));
  }

  acceptsDrop(sources: DocumentSource[]): boolean {
    return sources.length > 0 && sources.every(isImage);
  }

  async addDropped(sources: DocumentSource[]): Promise<void> {
    const fresh = sources.filter(isImage).map((s) => this.newItem(s));
    if (!fresh.length) return;
    this.items.push(...fresh);
    this.renderList();
    if (this.items.length > 1) this.ctx.sidebar.setVisible(true);
    this.ctx.toast(`Added ${fresh.length} image${fresh.length === 1 ? "" : "s"}`);
    this.ctx.refreshTitle();
  }

  // ---- slideshow / copy / print --------------------------------------------------------

  private toggleSlideshow(): void {
    if (this.slideTimer) { this.stopSlideshow(); return; }
    if (this.items.length < 2) { this.ctx.toast("Open several images for a slideshow"); return; }
    this.root.classList.add("slideshow");
    void this.root.requestFullscreen?.().catch(() => {});
    this.setTool("pan");
    this.view.zoomFit();
    this.slideTimer = window.setInterval(() => this.step(1, true), SLIDESHOW_MS);
    this.ctx.toast("Slideshow — Esc or F5 to stop");
  }

  private stopSlideshow(): void {
    if (!this.slideTimer) return;
    clearInterval(this.slideTimer);
    this.slideTimer = 0;
    this.root.classList.remove("slideshow");
    if (document.fullscreenElement === this.root) void document.exitFullscreen().catch(() => {});
  }

  private async copyImage(): Promise<void> {
    const it = this.current;
    if (!it?.rendered) return;
    try {
      const r = await this.encodeItem(it, "png", null, 0, false, false);
      await navigator.clipboard.write([new ClipboardItem({ "image/png": r.blob })]);
      this.ctx.toast("Image copied");
    } catch (e) {
      this.ctx.toast(`Couldn't copy the image: ${(e as Error).message}`);
    }
  }

  async print(): Promise<void> {
    const sel = this.items.filter((i) => this.selected.has(i));
    const items = sel.length > 1 ? sel : this.current ? [this.current] : [];
    this.printEl?.remove();
    const box = el("div.img-print");
    this.printEl = box;
    const urls: string[] = [];
    try {
      for (const it of items) {
        if (it.error) continue;
        const r = await this.encodeItem(it, "png", null, 0, false, false);
        const url = URL.createObjectURL(r.blob);
        urls.push(url);
        const img = el("img", { src: url, alt: it.name });
        box.append(el("div.img-print-page", {}, img));
        await img.decode().catch(() => {});
      }
      document.body.append(box);
      document.documentElement.classList.add("img-printing");
      window.print();
    } finally {
      document.documentElement.classList.remove("img-printing");
      box.remove();
      urls.forEach((u) => URL.revokeObjectURL(u));
    }
  }

  // ---- saving / exporting ----------------------------------------------------------------

  private setStatus(text: string | null): void {
    this.status.hidden = !text;
    this.status.textContent = text ?? "";
  }

  /** Encode an item with every edit (markup included). quality: 0..1, or null for the default. */
  async encodeItem(it: Item, fmt: OutFmt, quality: number | null, maxPx: number, keepExif = true, animate = true): Promise<EncodeResult> {
    const info = await this.ensureOpen(it);
    const ops = await this.workerOps(it, this.finalOps(it));
    const big = (info.anim?.n ?? 0) >= 12;
    try {
      return await this.worker.call({ t: "encode", id: it.id, ops, fmt, quality, maxPx, keepExif, animate }, {
        progress: big ? (p) => this.setStatus(`Saving frame ${p.progress} of ${p.total}…`) : undefined,
      });
    } finally {
      this.setStatus(null);
    }
  }

  /** Where Ctrl+S puts an item: same file and format, or a PNG when the format can't hold the edits. */
  private planOut(it: Item): { fmt: OutFmt; name: string; same: boolean } {
    const ext = extOfName(it.name);
    let fmt = EXT_FMT[ext];
    if (fmt === "png" && it.info?.fmt === "apng") fmt = "apng";
    if (!fmt || (opsNeedAlpha(this.finalOps(it)) && (fmt === "jpeg" || fmt === "bmp"))) {
      return { fmt: "png", name: `${stemOf(it.name)}.png`, same: false };
    }
    return { fmt, name: it.name, same: true };
  }

  private types(spec: FormatSpec): FilePickerAcceptType[] {
    const mime = spec.id === "pdf" ? "application/pdf" : spec.id === "apng" ? "image/apng" : `image/${spec.id}`;
    return [{ description: spec.label, accept: { [mime]: [spec.ext] as `.${string}`[] } }];
  }

  /** After a successful in-place write: the saved bytes become the item's new original. */
  private async adopt(it: Item, blob: Blob, handle?: FileSystemFileHandle, name = it.name): Promise<void> {
    const old = it.id;
    it.mkOff?.();
    it.mk?.destroy();
    Object.assign(it, {
      id: this.nextId++, src: memSource(name, blob, handle ?? it.src.handle), name, ops: [], history: [], future: [],
      mk: null, mkOff: undefined, opened: undefined, info: undefined, bytes: undefined, error: undefined, rendered: undefined,
    });
    void this.worker.call({ t: "close", id: old }).catch(() => {});
    this.ctx.setDirty(this.isDirty());
    this.updateRow(it);
    if (it === this.current) await this.show(it, true);
    else void this.loadThumb(it, true);
  }

  private async writeHandle(it: Item, handle: FileSystemFileHandle, blob: Blob): Promise<boolean> {
    try {
      const h = handle as FileSystemFileHandle & { requestPermission?(d: { mode: string }): Promise<string>; queryPermission?(d: { mode: string }): Promise<string> };
      if ((await h.queryPermission?.({ mode: "readwrite" })) !== "granted" &&
        (await h.requestPermission?.({ mode: "readwrite" })) !== "granted") return false;
      const w = await handle.createWritable();
      await w.write(blob);
      await w.close();
      await this.adopt(it, blob, handle);
      return true;
    } catch (e) {
      this.ctx.toast(`Couldn't save “${it.name}”: ${(e as Error).message}`, { timeout: 6000 });
      return false;
    }
  }

  async save(req: SaveRequest): Promise<SaveResult | null> {
    if (req.mode === "export") return this.exportSave(req.format ?? "image");
    const it = this.current;
    if (!it || it.error) return null;
    if (req.mode === "save") {
      // Other edited images that were opened with a writable handle are saved in place
      // here (the shell only knows the handle of the document it opened).
      for (const o of this.items) {
        if (o === it || o.primary || !this.itemDirty(o) || !o.src.handle || !this.planOut(o).same) continue;
        const r = await this.encodeItem(o, this.planOut(o).fmt, null, 0);
        await this.writeHandle(o, o.src.handle, r.blob);
      }
      if (!this.itemDirty(it)) return null;
    }
    const plan = this.planOut(it);
    if (req.mode === "save" && !it.primary && it.src.handle && plan.same) {
      const r = await this.encodeItem(it, plan.fmt, null, 0);
      if (await this.writeHandle(it, it.src.handle, r.blob)) this.ctx.toast(`Saved “${it.name}”`);
      return null;
    }
    const r = await this.encodeItem(it, plan.fmt, null, 0);
    const replaces = req.mode === "save" && it.primary && plan.same;
    if (!plan.same) this.ctx.toast(`${FMT_LABEL[it.info?.fmt ?? "unknown"]} can't keep these edits, so they are saved as PNG`);
    // The bytes become the item's original only in onSaved(), after the shell really
    // wrote them (a save dialog can be cancelled; an in-place write can lose permission
    // and fall back to the dialog).
    this.pendingSave = { it, blob: r.blob, name: plan.name };
    return { blob: r.blob, suggestedName: plan.name, types: this.types(OUT_SPEC(plan.fmt)), replacesOriginal: replaces };
  }

  private pendingSave: { it: Item; blob: Blob; name: string } | null = null;

  /**
   * Shell hook (Viewer.onSaved): called only after a successful write of the
   * last save()/Save As result. The written bytes become the item's new original.
   */
  onSaved(info?: { handle?: FileSystemFileHandle; name?: string }): void {
    const p = this.pendingSave;
    this.pendingSave = null;
    if (!p || !this.items.includes(p.it)) return;
    void this.adopt(p.it, p.blob, info?.handle, info?.handle?.name ?? info?.name ?? p.name);
  }

  exportFormats(): ExportFormat[] {
    return [
      { id: "image", label: "Image (PNG, JPEG, WebP, TIFF, BMP, GIF…)" },
      { id: "pdf", label: "PDF" },
    ];
  }

  private pendingFile: { blob: Blob; name: string; spec: FormatSpec } | null = null;

  private async exportSave(format: string): Promise<SaveResult | null> {
    this.pendingSave = null; // exports never replace the image being edited
    if (format === "file" && this.pendingFile) {
      const p = this.pendingFile;
      this.pendingFile = null;
      return { blob: p.blob, suggestedName: p.name, types: this.types(p.spec), replacesOriginal: false };
    }
    const it = this.current;
    if (format === "pdf") {
      const opts = await pdfOptions(this.exportItems().length);
      if (!opts) return null;
      const blob = await this.exportPdf(opts);
      return { blob, suggestedName: `${stemOf(this.exportItems()[0]!.name)}.pdf`, types: this.types(BATCH_FORMATS.at(-1)!), replacesOriginal: false };
    }
    if (!it?.rendered) return null;
    const anim = !!it.info?.anim;
    const ch = await exportOptions({ title: "Export Image", okLabel: "Export…", formats: EXPORT_FORMATS,
      initial: anim ? (EXT_FMT[extOfName(it.name)] ?? "png") : "png" });
    if (!ch) return null;
    const fmt = ch.fmt.id as OutFmt;
    const r = await this.encodeItem(it, fmt, ch.fmt.quality ? ch.quality / 100 : null, 0);
    if (r.kind === "flat") this.ctx.toast(`First frame only: ${ch.fmt.label} can't hold an animation`);
    return { blob: r.blob, suggestedName: `${stemOf(it.name)}${ch.fmt.ext}`, types: this.types(ch.fmt), replacesOriginal: false };
  }

  private exportItems(): Item[] {
    const sel = this.items.filter((i) => this.selected.has(i));
    return (sel.length > 1 ? sel : this.items).filter((i) => !i.error);
  }

  /** Combine the selected (or all) images into one PDF, edits included. */
  async exportPdf(opts: PdfOptions, items = this.exportItems()): Promise<Blob> {
    const pages = [];
    for (const it of items) {
      await this.ensureOpen(it);
      pages.push({ id: it.id, ops: await this.workerOps(it, this.finalOps(it)) });
    }
    try {
      const r = await this.worker.call({ t: "pdf", pages, opts }, { progress: (p) => this.setStatus(`Adding image ${p.progress} of ${p.total}…`) });
      return r.blob;
    } finally {
      this.setStatus(null);
    }
  }

  /** One output file per image (batch convert), named after the image. */
  async convertOne(it: Item, ch: ExportChoice): Promise<{ name: string; blob: Blob; flat: boolean }> {
    const name = `${stemOf(it.name)}${ch.fmt.ext}`;
    if (ch.fmt.id === "pdf") {
      if (ch.maxPx) {
        const [w, h] = sizeAfterOps(this.finalOps(it), (await this.ensureOpen(it)).w, it.info!.h);
        const s = ch.maxPx / Math.max(w, h);
        if (s < 1) {
          const pages = [{ id: it.id, ops: await this.workerOps(it, [...this.finalOps(it), { k: "resize", w: Math.max(1, Math.round(w * s)), h: Math.max(1, Math.round(h * s)) }]) }];
          return { name, blob: (await this.worker.call({ t: "pdf", pages, opts: { pageSize: "fit", marginMm: 0 } })).blob, flat: false };
        }
      }
      return { name, blob: await this.exportPdf({ pageSize: "fit", marginMm: 0 }, [it]), flat: false };
    }
    const r = await this.encodeItem(it, ch.fmt.id as OutFmt, ch.fmt.quality ? ch.quality / 100 : null, ch.maxPx);
    return { name, blob: r.blob, flat: r.kind === "flat" };
  }

  /** Batch convert into a folder (unique names, never overwriting). */
  async batchExport(dir: FileSystemDirectoryHandle, ch: ExportChoice, items = this.exportItems()): Promise<string[]> {
    const used = new Set<string>();
    const written: string[] = [];
    let flat = 0;
    const errors: string[] = [];
    for (const [k, it] of items.entries()) {
      this.setStatus(`Exporting ${k + 1} of ${items.length}…`);
      try {
        const out = await this.convertOne(it, ch);
        written.push(await writeInto(dir, out.name, out.blob, used));
        if (out.flat) flat++;
      } catch (e) {
        errors.push(`${it.name}: ${(e as Error).message}`);
      }
    }
    this.setStatus(null);
    if (errors.length) this.ctx.toast(`Exported ${written.length} of ${items.length}; ${errors.length} failed (${errors[0]})`, { timeout: 6000 });
    else this.ctx.toast(`Exported ${written.length} image${written.length === 1 ? "" : "s"}` +
      (flat ? ` (${flat} animation${flat === 1 ? "" : "s"} saved as first frame only)` : ""));
    return written;
  }

  /** Export Selected Images… (image_view._export_selected). */
  private async batchDialog(): Promise<void> {
    const items = this.exportItems();
    if (!items.length) return;
    const ch = await exportOptions({ title: "Export Selected Images", okLabel: "Export…", formats: BATCH_FORMATS, maxSize: true,
      description: `${items.length} image${items.length === 1 ? "" : "s"}` });
    if (!ch) return;
    if (items.length === 1) {
      const out = await this.convertOne(items[0]!, ch);
      this.pendingFile = { blob: out.blob, name: out.name, spec: ch.fmt };
      await this.ctx.requestSave({ mode: "export", format: "file" });
      return;
    }
    const dir = await pickFolder();
    if (dir === null) return;
    if (dir !== "unsupported") { await this.batchExport(dir, ch, items); return; }
    const files = [];
    for (const it of items) files.push(await this.convertOne(it, ch));
    this.pendingFile = { blob: await zipFiles(files), name: "Images.zip", spec: { id: "png", label: "ZIP archive", ext: ".zip", quality: false } };
    await this.ctx.requestSave({ mode: "export", format: "file" });
  }

  // ---- title / info ------------------------------------------------------------------------

  isDirty(): boolean {
    return this.items.some((i) => this.itemDirty(i));
  }

  getTitle(): string {
    return this.current?.name ?? "Image";
  }

  getSubtitle(): string {
    const it = this.current;
    if (!it?.rendered) return "";
    let s = `${it.rendered[0]} × ${it.rendered[1]}`;
    if (this.items.length > 1) s += `  ·  ${this.items.indexOf(it) + 1} of ${this.items.length}`;
    return s;
  }

  async getInfo(): Promise<InfoSection[]> {
    const it = this.current;
    if (!it) return [];
    const out: InfoSection[] = [];
    if (!it.primary) {
      out.push({ title: "Current Image", rows: [["File name", it.name],
        ["Size", `${(it.src.size ?? (await it.src.blob()).size).toLocaleString()} bytes`]] });
    }
    const fin = this.finalOps(it);
    out.push(...await imageInfo(await this.bytesOf(it), {
      size: it.info ? [it.info.w, it.info.h] : null,
      edited: it.rendered ?? null,
      edits: fin.map(opName),
      frames: it.info?.anim?.n ?? 1,
      durations: it.durations ?? null,
      repetitions: it.info?.anim?.repetitions ?? null,
    }));
    return out;
  }
}

const mod: ViewerModule = { kind: "image", create: () => new ImageViewer() };
export default mod;
