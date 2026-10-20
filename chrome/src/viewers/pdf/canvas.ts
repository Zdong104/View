// The scrolling page canvas (port of preview/pdf_canvas.py): one scroller for
// all pages of the page model, which may come from several source PDFs (so
// pdf.js's single-document PDFViewer can't be used). Layout is computed for
// every page (cheap arithmetic), but DOM and pdf.js page views exist only for
// pages near the viewport, so 1,000-page files open and scroll as fast as
// 3-page ones. Each materialized page is a pdf.js PDFPageView (canvas, text
// layer for selection, annotation layer with links and AcroForm widgets).
//
// Layout modes: continuous, two (pairs side by side), single (one page).
// Zoom modes: fit-width, fit-page, free; Ctrl+wheel / touchpad pinch / touch
// pinch anchored at the pointer.
import * as pdfjs from "pdfjs-dist";
import type { PDFPageProxy } from "pdfjs-dist";
import { el } from "../../ui/kit.ts";
import { displaySize } from "./geometry.ts";
import type { PageRef } from "./model.ts";
import { infoOf, type PageInfo, type PdfSource } from "./sources.ts";

type ViewerLib = typeof import("pdfjs-dist/web/pdf_viewer.mjs");
type PageView = InstanceType<ViewerLib["PDFPageView"]>;

export const GAP = 14;
export const MARGIN = 18;
export const ZOOM_MIN = 0.1;
export const ZOOM_MAX = 8;
/** CSS px per PDF point at 100 %. */
export const CSS_PER_PT = 96 / 72;

const INITIAL = 0, FINISHED = 3; // pdf.js RenderingStates

export type ZoomMode = "fit-width" | "fit-page" | "free";
export type LayoutMode = "continuous" | "two" | "single";

export interface CanvasDelegate {
  readonly pages: PageRef[];
  source(src: number): PdfSource;
  /** pdf.js layerProperties (annotation storage, link service…) for a source. */
  layerProps(src: number): object;
  imageResourcesPath: string;
  onCurrentChanged(): void;
  onZoomChanged(): void;
  onFirstRender(): void;
  /** A real page size arrived (the layout used the source's page-1 size until now). */
  onPageInfo(ref: PageRef, info: PageInfo): void;
  onSlotAttached(slot: Slot): void;
  onSlotDetached(slot: Slot): void;
  /** Size/zoom/rotation of a slot changed. */
  onSlotRelayout(slot: Slot): void;
  /** devicePixelRatio changed (thumbnails re-render). */
  onDprChanged?(): void;
  /** Pointer pressed on a page (OCR offer, etc.). */
  onSlotPointerDown?(slot: Slot, ev: PointerEvent): void;
}

/** One materialized page. */
export class Slot {
  ref: PageRef;
  readonly el: HTMLDivElement;
  readonly pageHost: HTMLDivElement;
  /** Search highlights (percent-positioned, zoom independent). */
  readonly hl: HTMLDivElement;
  /** Live Text (OCR) layer for pages without a text layer. */
  readonly ocr: HTMLDivElement;
  /** Markup overlay (markup/api.ts MarkupHost.overlay). */
  readonly mk: HTMLDivElement;
  view: PageView | null = null;
  page: PDFPageProxy | null = null;
  info: PageInfo;
  dead = false;
  /** Free for the delegate (markup surface, OCR state…). */
  data: Record<string, unknown> = {};

  constructor(ref: PageRef, info: PageInfo) {
    this.ref = ref;
    this.info = info;
    this.pageHost = el("div.pdf-pagehost");
    this.hl = el("div.pdf-hl", { "aria-hidden": "true" });
    this.ocr = el("div.pdf-ocr");
    this.mk = el("div.pdf-mk");
    this.el = el("div.pdf-slot", { "data-page-id": ref.id }, this.pageHost, this.hl, this.ocr, this.mk);
  }

  /** Total clockwise display rotation: the page's /Rotate plus the user's. */
  get rotation(): number {
    return (this.info.rotate + this.ref.rotation) % 360;
  }
}

export class PageCanvas {
  readonly scroll: HTMLDivElement;
  readonly content: HTMLDivElement;
  scale = 1;
  mode: ZoomMode = "fit-width";
  layout: LayoutMode = "continuous";
  current = 0;

  private d: CanvasDelegate;
  private lib: ViewerLib;
  private eventBus: InstanceType<ViewerLib["EventBus"]>;
  private l10n: InstanceType<ViewerLib["GenericL10n"]>;
  private signal: AbortSignal;

  private xs = new Float64Array(0);
  private ys = new Float64Array(0);
  private ws = new Float64Array(0);
  private hs = new Float64Array(0);
  private rowTops: number[] = [];
  private rowHeights: number[] = [];
  private rowPages: number[][] = [];
  private posOfId = new Map<number, number>();
  private contentW = 0;
  private contentH = 0;
  private viewW = 0;
  private viewH = 0;

  private slots = new Map<number, Slot>(); // ref.id -> slot
  private rendering: Promise<void> | null = null;
  private settleUntil = 0;
  private settleTimer = 0;
  private frame = 0;
  private resizeFrame = 0;
  private rendered = false;
  private flipAcc = 0;
  /** scrollTop right after goto(): until the user scrolls, the target stays "current" (it may not reach the probe line near the end). */
  private pinned: number | null = null;

  constructor(parent: HTMLElement, lib: ViewerLib, delegate: CanvasDelegate, signal: AbortSignal) {
    this.d = delegate;
    this.lib = lib;
    this.signal = signal;
    this.eventBus = new lib.EventBus();
    // No language: pdf.js uses its built-in English strings (no locale fetch).
    this.l10n = new (lib.GenericL10n as unknown as new () => InstanceType<ViewerLib["GenericL10n"]>)();
    this.content = el("div.pdfViewer.pdf-content");
    this.scroll = el("div.pdf-scroll", { tabindex: 0, role: "document", "aria-label": "Pages" }, this.content);
    parent.append(this.scroll);
    const on = <K extends keyof HTMLElementEventMap>(t: EventTarget, k: K, f: (e: HTMLElementEventMap[K]) => void, o: AddEventListenerOptions = {}) =>
      t.addEventListener(k, f as EventListener, { ...o, signal });
    on(this.scroll, "scroll", () => this.schedule());
    on(this.scroll, "wheel", (e) => this.onWheel(e), { passive: false });
    on(this.scroll, "pointerdown", (e) => {
      const slotEl = (e.target as HTMLElement).closest?.(".pdf-slot") as HTMLElement | null;
      const slot = slotEl ? this.slots.get(Number(slotEl.dataset.pageId)) : undefined;
      if (slot) this.d.onSlotPointerDown?.(slot, e);
    });
    this.setupTouchPinch();
    this.watchDpr();
    // Relayout on the next frame, not inside the observer: fit-width can toggle the
    // scrollbar, which would resize the scroller again in the same frame and make
    // Chrome report "ResizeObserver loop completed with undelivered notifications".
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(this.resizeFrame);
      this.resizeFrame = requestAnimationFrame(() => this.onResize());
    });
    ro.observe(this.scroll);
    signal.addEventListener("abort", () => {
      ro.disconnect();
      cancelAnimationFrame(this.frame);
      cancelAnimationFrame(this.resizeFrame);
      clearTimeout(this.settleTimer);
      for (const s of this.slots.values()) this.destroySlot(s);
    }, { once: true });
  }

  // ---- geometry ----------------------------------------------------------------

  /** CSS px per PDF point. */
  get ppt(): number { return this.scale * CSS_PER_PT; }

  private infoOf(ref: PageRef): PageInfo {
    const s = this.d.source(ref.src);
    return s.infos[ref.index] ?? s.fallback;
  }

  /** Display size (points) of a page reference. */
  dispSize(ref: PageRef): [number, number] {
    const i = this.infoOf(ref);
    return displaySize(i.rotate + ref.rotation, i.w, i.h);
  }

  get pageCount(): number { return this.d.pages.length; }

  rectOf(pos: number): [number, number, number, number] | null {
    if (pos < 0 || pos >= this.xs.length || this.ws[pos] === 0) return null;
    return [this.xs[pos]!, this.ys[pos]!, this.ws[pos]!, this.hs[pos]!];
  }

  slotAt(pos: number): Slot | undefined {
    const ref = this.d.pages[pos];
    return ref ? this.slots.get(ref.id) : undefined;
  }

  allSlots(): Slot[] { return [...this.slots.values()]; }

  posOf(ref: PageRef): number { return this.posOfId.get(ref.id) ?? -1; }

  fitScale(mode: ZoomMode): number {
    const pages = this.d.pages;
    const vw = this.viewW, vh = this.viewH;
    if (!pages.length || vw <= 2 * MARGIN + GAP || vh <= 2 * MARGIN) return this.scale;
    let maxw = 0;
    if (this.layout === "two") {
      for (let i = 0; i < pages.length; i += 2) {
        const a = this.dispSize(pages[i]!)[0];
        maxw = Math.max(maxw, a + (pages[i + 1] ? this.dispSize(pages[i + 1]!)[0] : a));
      }
    } else {
      // Mixed sizes: the widest page fits. Sources keep few distinct sizes, so
      // this is O(n) of cheap lookups.
      for (const p of pages) maxw = Math.max(maxw, this.dispSize(p)[0]);
    }
    let z = (vw - 2 * MARGIN - (this.layout === "two" ? GAP : 0)) / (maxw * CSS_PER_PT);
    if (mode === "fit-page" || (this.layout === "single" && mode !== "fit-width")) {
      const cur = Math.min(this.current, pages.length - 1);
      let h = this.dispSize(pages[cur]!)[1];
      if (this.layout === "two") {
        const a = cur - (cur % 2);
        h = Math.max(this.dispSize(pages[a]!)[1], pages[a + 1] ? this.dispSize(pages[a + 1]!)[1] : 0);
      }
      z = Math.min(z, (vh - 2 * MARGIN) / (h * CSS_PER_PT));
    }
    return Math.max(ZOOM_MIN, Math.min(z, 4));
  }

  /** Recompute all page rects (no DOM besides the content size). */
  relayout(): void {
    const pages = this.d.pages;
    const n = pages.length;
    if (this.xs.length !== n) {
      this.xs = new Float64Array(n); this.ys = new Float64Array(n);
      this.ws = new Float64Array(n); this.hs = new Float64Array(n);
    } else {
      this.ws.fill(0);
    }
    this.posOfId.clear();
    pages.forEach((p, i) => this.posOfId.set(p.id, i));
    this.current = Math.max(0, Math.min(this.current, n - 1));
    const z = this.ppt;
    const groups: number[][] = [];
    if (this.layout === "single") { if (n) groups.push([this.current]); }
    else if (this.layout === "two") for (let i = 0; i < n; i += 2) groups.push(i + 1 < n ? [i, i + 1] : [i]);
    else for (let i = 0; i < n; i++) groups.push([i]);
    const sizes = (i: number) => this.dispSize(pages[i]!);
    const dpr = devicePixelRatio || 1;
    const snap = (v: number) => Math.round(v * dpr) / dpr;
    const floorDev = (v: number) => Math.floor(v * dpr + 1e-6) / dpr;
    let maxRow = 0;
    const widths = groups.map((g) => {
      let w = GAP * (g.length - 1);
      for (const i of g) w += sizes(i)[0] * z;
      maxRow = Math.max(maxRow, w);
      return w;
    });
    this.contentW = Math.max(this.viewW, maxRow + 2 * MARGIN);
    let y = MARGIN;
    this.rowTops = []; this.rowHeights = []; this.rowPages = groups;
    groups.forEach((g, gi) => {
      let rh = 0;
      for (const i of g) rh = Math.max(rh, sizes(i)[1] * z);
      let x = (this.contentW - widths[gi]!) / 2;
      for (const i of g) {
        const [w, h] = sizes(i);
        // Whole device pixels: pdf.js draws the canvas at devicePixelRatio ×
        // zoom and sizes the page box so the canvas maps 1:1; the box's origin
        // must sit on a device pixel too, or the page is resampled (blurry).
        this.xs[i] = snap(x); this.ys[i] = snap(y + (rh - h * z) / 2);
        this.ws[i] = floorDev(w * z); this.hs[i] = floorDev(h * z);
        x += w * z + GAP;
      }
      this.rowTops.push(y); this.rowHeights.push(rh);
      y += rh + GAP;
    });
    this.contentH = Math.max(groups.length ? y - GAP + MARGIN : 0, this.layout === "single" ? this.viewH : 0);
    this.content.style.width = `${this.contentW}px`;
    this.content.style.height = `${this.contentH}px`;
    for (const s of this.slots.values()) {
      const pos = this.posOfId.get(s.ref.id);
      if (pos === undefined || this.ws[pos] === 0) { this.destroySlot(s); continue; }
      s.ref = pages[pos]!;
      this.placeSlot(s, pos);
    }
  }

  private placeSlot(s: Slot, pos: number): void {
    const st = s.el.style;
    st.left = `${this.xs[pos]}px`; st.top = `${this.ys[pos]}px`;
    st.width = `${this.ws[pos]}px`; st.height = `${this.hs[pos]}px`;
    s.el.style.setProperty("--pdf-ppt", String(this.ppt));
    s.el.dataset.pos = String(pos);
    const v = s.view;
    if (v && (Math.abs(v.scale - this.scale) > 1e-6 || v.rotation !== s.ref.rotation)) {
      v.update({ scale: this.scale, rotation: s.ref.rotation });
    }
    this.d.onSlotRelayout(s);
  }

  private rowAt(cy: number): number {
    const t = this.rowTops;
    let lo = 0, hi = t.length - 1;
    if (hi < 0) return 0;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (t[mid]! <= cy) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  /** Page position nearest to a content point. */
  pageAt(cx: number, cy: number): number {
    const g = this.rowPages[this.rowAt(cy)];
    if (!g?.length) return 0;
    let best = g[0]!, bd = Infinity;
    for (const i of g) {
      const d = cx < this.xs[i]! ? this.xs[i]! - cx : cx > this.xs[i]! + this.ws[i]! ? cx - this.xs[i]! - this.ws[i]! : 0;
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  // ---- materialization & rendering -------------------------------------------------

  schedule(): void {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.update(); });
  }

  /** Visible positions, nearest-to-center first, and the keep-alive range. */
  private visibleRange(extra: number): number[] {
    const top = this.scroll.scrollTop - extra, bottom = this.scroll.scrollTop + this.viewH + extra;
    const out: number[] = [];
    if (!this.rowTops.length) return out;
    for (let r = this.rowAt(Math.max(0, top)); r < this.rowTops.length && this.rowTops[r]! < bottom; r++) {
      if (this.rowTops[r]! + this.rowHeights[r]! < top) continue;
      out.push(...this.rowPages[r]!);
    }
    const mid = this.scroll.scrollTop + this.viewH / 2;
    return out.sort((a, b) => Math.abs(this.ys[a]! + this.hs[a]! / 2 - mid) - Math.abs(this.ys[b]! + this.hs[b]! / 2 - mid));
  }

  update(): void {
    const pages = this.d.pages;
    if (!pages.length || !this.viewH) return;
    // current page: the one under a probe line 40 % down the viewport
    const st = this.scroll.scrollTop;
    let cur: number;
    if (this.layout === "single" || (this.pinned !== null && Math.abs(st - this.pinned) < 2)) cur = this.current;
    else if (st >= this.contentH - this.viewH - 1 && st > 0) cur = this.rowPages[this.rowPages.length - 1]![0]!;
    else cur = this.pageAt(this.scroll.scrollLeft + this.viewW / 2, st + Math.min(this.viewH * 0.4, 200));
    if (cur !== this.current) { this.current = cur; this.d.onCurrentChanged(); }

    const near = this.visibleRange(this.viewH);
    const keep = new Set(this.visibleRange(this.viewH * 3).map((p) => pages[p]!.id));
    for (const p of near) {
      const ref = pages[p]!;
      if (!this.slots.has(ref.id)) this.createSlot(ref, p);
    }
    for (const s of this.slots.values()) if (!keep.has(s.ref.id)) this.destroySlot(s);
    // Cap the cache when zoomed far out (many tiny pages visible).
    if (this.slots.size > Math.max(40, near.length * 2)) {
      const nearIds = new Set(near.map((p) => pages[p]!.id));
      for (const s of this.slots.values()) if (!nearIds.has(s.ref.id)) this.destroySlot(s);
    }
    // detail canvases for zoomed-in pages
    for (const p of this.visibleRange(0)) {
      const s = this.slots.get(pages[p]!.id);
      if (!s?.view || s.view.renderingState !== FINISHED) continue;
      const x = this.xs[p]!, y = this.ys[p]!, w = this.ws[p]!, h = this.hs[p]!;
      const sl = this.scroll.scrollLeft;
      const area = { minX: Math.max(0, sl - x), minY: Math.max(0, st - y),
        maxX: Math.min(w, sl + this.viewW - x), maxY: Math.min(h, st + this.viewH - y) };
      const full = area.minX <= 0 && area.minY <= 0 && area.maxX >= w && area.maxY >= h;
      s.view.updateVisibleArea(full ? null : area);
    }
    this.pump();
  }

  private createSlot(ref: PageRef, pos: number): void {
    const slot = new Slot(ref, this.infoOf(ref));
    this.slots.set(ref.id, slot);
    this.content.append(slot.el);
    this.placeSlot(slot, pos);
    void this.loadView(slot);
  }

  private async loadView(slot: Slot): Promise<void> {
    const src = this.d.source(slot.ref.src);
    let page: PDFPageProxy;
    try {
      page = await src.doc.getPage(slot.ref.index + 1);
    } catch (e) {
      console.error(e);
      return;
    }
    if (slot.dead) return;
    slot.page = page;
    const info = infoOf(page);
    const known = src.infos[slot.ref.index];
    src.infos[slot.ref.index] = info;
    slot.info = info;
    if (!known && (info.w !== src.fallback.w || info.h !== src.fallback.h || info.rotate !== src.fallback.rotate)) {
      this.d.onPageInfo(slot.ref, info);
    }
    const view = new this.lib.PDFPageView({
      container: slot.pageHost,
      eventBus: this.eventBus,
      id: slot.ref.index + 1,
      scale: this.scale,
      defaultViewport: page.getViewport({ scale: this.scale * CSS_PER_PT }),
      textLayerMode: 1,
      annotationMode: pdfjs.AnnotationMode.ENABLE_FORMS,
      imageResourcesPath: this.d.imageResourcesPath,
      l10n: this.l10n,
      layerProperties: this.d.layerProps(slot.ref.src),
      abortSignal: this.signal,
    });
    view.rotation = slot.ref.rotation;
    view.setPdfPage(page);
    slot.view = view;
    this.d.onSlotAttached(slot);
    this.pump();
  }

  private destroySlot(s: Slot): void {
    if (s.dead) return;
    s.dead = true;
    this.slots.delete(s.ref.id);
    this.d.onSlotDetached(s);
    s.view?.cancelRendering();
    s.view?.destroy();
    s.el.remove();
  }

  /** Render the highest-priority page that needs it (one at a time, like pdf.js). */
  private pump(): void {
    if (this.rendering) return;
    const now = performance.now();
    if (now < this.settleUntil) {
      clearTimeout(this.settleTimer);
      this.settleTimer = setTimeout(() => this.pump(), this.settleUntil - now + 5) as unknown as number;
      return;
    }
    const pages = this.d.pages;
    const pick = (list: number[]) => {
      for (const p of list) {
        const s = this.slots.get(pages[p]!.id);
        const v = s?.view;
        if (!v) continue;
        if (v.renderingState === INITIAL) return v;
        if (v.renderingState === FINISHED && v.detailView && v.detailView.renderingState === INITIAL) return v.detailView;
      }
      return null;
    };
    const job = pick(this.visibleRange(0)) ?? pick(this.visibleRange(this.viewH));
    if (!job) return;
    this.rendering = job.draw().catch((e: unknown) => {
      if (!(e instanceof pdfjs.RenderingCancelledException)) console.error(e);
    }).finally(() => {
      this.rendering = null;
      if (!this.rendered && job.renderingState === FINISHED) {
        this.rendered = true;
        this.d.onFirstRender();
      }
      this.pump();
    });
  }

  /** True when no page render is pending (thumbnails yield to the main view). */
  get idle(): boolean {
    if (this.rendering) return false;
    for (const s of this.slots.values()) if (s.view && s.view.renderingState === INITIAL) return false;
    return true;
  }

  // ---- navigation & zoom -----------------------------------------------------------

  private onResize(): void {
    const w = this.scroll.clientWidth, h = this.scroll.clientHeight;
    if (w === this.viewW && h === this.viewH) return;
    const anchor = this.topAnchor();
    this.viewW = w; this.viewH = h;
    if (this.mode !== "free") this.scale = this.fitScale(this.mode);
    this.relayout();
    this.restoreAnchor(anchor);
    this.d.onZoomChanged();
    this.update();
  }

  /** [pos, offset px into the page] of the page at the top of the viewport. */
  private topAnchor(): [number, number] {
    const st = this.scroll.scrollTop;
    const pos = this.layout === "single" ? this.current : this.pageAt(this.scroll.scrollLeft + this.viewW / 2, st);
    const r = this.rectOf(pos);
    return [pos, r ? (st - r[1]) / Math.max(1, r[3]) : 0];
  }

  private restoreAnchor([pos, frac]: [number, number]): void {
    const r = this.rectOf(pos);
    if (r) this.scroll.scrollTop = r[1] + frac * r[3];
  }

  /** Re-layout after sizes/rotations/structure changed, keeping the view steady. */
  refresh(): void {
    const anchor = this.topAnchor();
    if (this.mode !== "free") this.scale = this.fitScale(this.mode);
    this.relayout();
    this.restoreAnchor(anchor);
    this.d.onZoomChanged();
    this.schedule();
  }

  goto(pos: number, opts: { y?: number; atEnd?: boolean } = {}): void {
    const n = this.d.pages.length;
    if (!n) return;
    pos = Math.max(0, Math.min(pos, n - 1));
    if (this.layout === "single") {
      this.current = pos;
      if (this.mode !== "free") this.scale = this.fitScale(this.mode);
      this.relayout();
      this.d.onZoomChanged();
    }
    const r = this.rectOf(pos);
    if (!r) return;
    let top = r[1] - MARGIN / 2;
    if (opts.y !== undefined) top = r[1] + opts.y * this.ppt - this.viewH / 3;
    if (opts.atEnd) top = r[1] + r[3] - this.viewH + MARGIN;
    this.scroll.scrollTop = top;
    this.pinned = this.scroll.scrollTop;
    if (this.current !== pos) { this.current = pos; this.d.onCurrentChanged(); }
    this.update();
  }

  /** Scroll so a display-frame rect (points) of a page is visible. */
  revealRect(pos: number, x: number, y: number, w: number, h: number): void {
    if (this.layout === "single" && pos !== this.current) this.goto(pos);
    const r = this.rectOf(pos);
    if (!r) return;
    const z = this.ppt;
    const cx = r[0] + x * z, cy = r[1] + y * z, bw = w * z, bh = h * z;
    const s = this.scroll;
    if (cy < s.scrollTop + 20 || cy + bh > s.scrollTop + this.viewH - 20) s.scrollTop = cy + bh / 2 - this.viewH / 2;
    if (cx < s.scrollLeft || cx + bw > s.scrollLeft + this.viewW) s.scrollLeft = cx + bw / 2 - this.viewW / 2;
    this.update();
  }

  setMode(mode: ZoomMode): void {
    this.mode = mode;
    if (mode !== "free") this.zoomTo(this.fitScale(mode), undefined, true);
    if (mode === "fit-page") this.goto(this.current);
  }

  setLayout(l: LayoutMode): void {
    const cur = this.current;
    this.layout = l;
    if (this.mode !== "free") this.scale = this.fitScale(this.mode);
    this.relayout();
    this.d.onZoomChanged();
    this.goto(cur);
  }

  zoomBy(f: number, wx?: number, wy?: number): void {
    this.zoomTo(this.scale * f, wx === undefined ? undefined : [wx, wy!]);
  }

  /** Zoom keeping the content point under widget point `at` (default: center) fixed. */
  zoomTo(z: number, at?: [number, number], keepMode = false): void {
    if (!keepMode) this.mode = "free";
    z = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
    const [wx, wy] = at ?? [this.viewW / 2, this.viewH / 2];
    const cx = this.scroll.scrollLeft + wx, cy = this.scroll.scrollTop + wy;
    const pos = this.pageAt(cx, cy);
    const r = this.rectOf(pos);
    const fx = r ? (cx - r[0]) / r[2] : 0, fy = r ? (cy - r[1]) / r[3] : 0;
    if (Math.abs(z - this.scale) < 1e-6) return;
    this.scale = z;
    this.settleUntil = performance.now() + 160;
    this.relayout();
    const r2 = this.rectOf(pos);
    if (r2) {
      this.scroll.scrollLeft = r2[0] + fx * r2[2] - wx;
      this.scroll.scrollTop = r2[1] + fy * r2[3] - wy;
    }
    this.d.onZoomChanged();
    this.schedule();
  }

  private onWheel(e: WheelEvent): void {
    if (e.ctrlKey) {
      // Ctrl+wheel and touchpad pinch (Chrome reports pinch as wheel + ctrlKey)
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 0.15 : e.deltaMode === 2 ? 1 : 0.01;
      const b = this.scroll.getBoundingClientRect();
      this.zoomBy(Math.exp(-e.deltaY * unit), e.clientX - b.left, e.clientY - b.top);
      return;
    }
    if (this.layout === "single" && e.deltaY) {
      const s = this.scroll;
      const atEnd = s.scrollTop >= s.scrollHeight - s.clientHeight - 0.5;
      const atTop = s.scrollTop <= 0.5;
      if ((e.deltaY > 0 && atEnd) || (e.deltaY < 0 && atTop)) {
        e.preventDefault();
        this.flipAcc += e.deltaY / (e.deltaMode === 1 ? 1 : 40);
        if (Math.abs(this.flipAcc) >= 1) {
          const step = this.flipAcc > 0 ? 1 : -1;
          this.flipAcc = 0;
          this.goto(this.current + step, { atEnd: step < 0 });
        }
        return;
      }
      this.flipAcc = 0;
    }
  }

  private setupTouchPinch(): void {
    const touches = new Map<number, [number, number]>();
    let start: { dist: number; scale: number } | null = null;
    const dist = () => {
      const [a, b] = [...touches.values()];
      return a && b ? Math.hypot(a[0] - b[0], a[1] - b[1]) : 0;
    };
    const o = { signal: this.signal };
    this.scroll.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "touch") return;
      touches.set(e.pointerId, [e.clientX, e.clientY]);
      if (touches.size === 2) start = { dist: dist(), scale: this.scale };
    }, o);
    this.scroll.addEventListener("pointermove", (e) => {
      if (!touches.has(e.pointerId)) return;
      touches.set(e.pointerId, [e.clientX, e.clientY]);
      if (start && touches.size === 2 && start.dist > 0) {
        const [a, b] = [...touches.values()] as [[number, number], [number, number]];
        const r = this.scroll.getBoundingClientRect();
        this.zoomTo(start.scale * dist() / start.dist, [(a[0] + b[0]) / 2 - r.left, (a[1] + b[1]) / 2 - r.top]);
      }
    }, o);
    const end = (e: PointerEvent) => { touches.delete(e.pointerId); if (touches.size < 2) start = null; };
    this.scroll.addEventListener("pointerup", end, o);
    this.scroll.addEventListener("pointercancel", end, o);
  }

  /** Re-snap and re-render when the window moves to a monitor with another devicePixelRatio. */
  private watchDpr(): void {
    if (this.signal.aborted) return;
    const mq = matchMedia(`(resolution: ${devicePixelRatio}dppx)`);
    mq.addEventListener("change", () => {
      const anchor = this.topAnchor();
      this.relayout();
      // update() recomputes pdf.js's output scale from the new devicePixelRatio.
      for (const s of this.slots.values()) s.view?.update({ scale: this.scale, rotation: s.ref.rotation });
      this.restoreAnchor(anchor);
      this.d.onDprChanged?.();
      this.schedule();
      this.watchDpr();
    }, { once: true, signal: this.signal });
  }

  /** Redraw every materialized page (after forms were reset, etc.). */
  invalidateAll(): void {
    for (const s of this.slots.values()) s.view?.reset();
    this.pump();
  }
}
