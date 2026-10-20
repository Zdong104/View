// Thumbnails sidebar (port of the desktop's thumbnail ListView): a virtualized
// list (only rows near the viewport have DOM and canvases) with multi-select
// (click, Ctrl+click, Shift+click, arrows, Ctrl+A), drag to reorder (pointer
// events, auto-scroll), a context menu and a selection footer. Thumbnails are
// rendered one at a time while the main canvas is idle and cached as
// ImageBitmaps (LRU).
import * as pdfjs from "pdfjs-dist";
import { el } from "../../ui/kit.ts";
import { displaySize } from "./geometry.ts";
import type { PageRef } from "./model.ts";
import type { PdfSource } from "./sources.ts";

export const THUMB_W = 112;
/** dataTransfer type of pages dragged within the viewer. */
export const PAGES_TYPE = "application/x-view-pdf-pages";
const THUMB_MAX_H = 158;
const ROW_PAD = 10;
const LABEL_H = 18;
const CACHE_MAX = 300;

export interface ThumbsDelegate {
  readonly pages: PageRef[];
  readonly current: number;
  source(src: number): PdfSource;
  /** Main canvas has nothing left to render. */
  idle(): boolean;
  goto(pos: number): void;
  move(ids: number[], dest: number): void;
  contextMenu(x: number, y: number): void;
  selectionChanged(): void;
  /** A prepared PDF of these pages for dragging out of the window: [file name, blob: URL], or null. */
  dragOut?(ids: number[]): [string, string] | null;
  /** Draw markup of a page onto a thumbnail (markup layer render()). */
  paintMarkup?(ctx: CanvasRenderingContext2D, ref: PageRef, unitsToPx: number): void;
}

interface Row { ref: PageRef; el: HTMLDivElement; canvas: HTMLCanvasElement; key: string }

export class Thumbnails {
  readonly root: HTMLDivElement;
  readonly list: HTMLDivElement;
  private inner: HTMLDivElement;
  private footer: HTMLDivElement;
  private indicator: HTMLDivElement;
  private d: ThumbsDelegate;
  private signal: AbortSignal;
  private tops: number[] = [];
  private heights: number[] = [];
  private rows = new Map<number, Row>(); // ref.id -> row
  private cache = new Map<string, ImageBitmap>();
  private busy = false;
  private frame = 0;
  private timer = 0;
  /** Selected page ids. `explicit` = selected by the user in the sidebar (desktop _explicit_sel). */
  selected = new Set<number>();
  explicit = false;
  private anchor = -1;
  /** Bumped when form values change, to re-render thumbnails. */
  formsVersion = 0;
  /** Last drag-over insertion index for files dropped from outside. */
  dropHint: number | null = null;

  constructor(d: ThumbsDelegate, signal: AbortSignal) {
    this.d = d;
    this.signal = signal;
    this.inner = el("div.pdf-thumbs-inner");
    this.indicator = el("div.pdf-thumb-drop", { hidden: true });
    this.list = el("div.pdf-thumbs-list", { tabindex: 0, role: "listbox", "aria-multiselectable": "true", "aria-label": "Pages" }, this.inner, this.indicator);
    this.footer = el("div.pdf-thumbs-footer", { hidden: true, role: "status" });
    this.root = el("div.pdf-thumbs", {}, this.list, this.footer);
    const o = { signal };
    this.list.addEventListener("scroll", () => this.schedule(), o);
    this.list.addEventListener("keydown", (e) => this.onKey(e), o);
    this.list.addEventListener("pointerdown", () => this.list.focus({ preventScroll: true }), o);
    this.list.addEventListener("click", (e) => {
      const pos = this.rowFromEvent(e);
      if (pos >= 0) this.clickRow(pos, e);
    }, o);
    this.list.addEventListener("dragstart", (e) => this.onDragStart(e), o);
    this.list.addEventListener("dragend", () => { this.indicator.hidden = true; this.list.classList.remove("dragging"); this.dragIds = null; }, o);
    this.list.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const pos = this.rowFromEvent(e);
      if (pos < 0) return;
      const ref = this.d.pages[pos]!;
      if (!this.selected.has(ref.id)) this.select([ref.id], pos);
      this.d.contextMenu(e.clientX, e.clientY);
    }, o);
    // Files dragged in from outside: remember where, the shell's drop handler
    // then calls addDropped() which inserts at dropHint.
    this.list.addEventListener("dragover", (e) => {
      const types = e.dataTransfer?.types ?? [];
      const own = types.includes(PAGES_TYPE) && this.dragIds;
      if (!own && !types.includes("Files")) return;
      const at = this.insertionIndex(e.clientY);
      this.showIndicator(at);
      this.autoScroll(e.clientY);
      if (own) {
        e.preventDefault();
        e.dataTransfer!.dropEffect = "move";
      } else {
        this.dropHint = at;
      }
    }, o);
    this.list.addEventListener("dragleave", (e) => {
      if (!this.list.contains(e.relatedTarget as Node | null)) this.indicator.hidden = true;
    }, o);
    this.list.addEventListener("drop", (e) => {
      this.indicator.hidden = true;
      if (e.dataTransfer?.types.includes(PAGES_TYPE) && this.dragIds) {
        e.preventDefault();
        e.stopPropagation();
        const ids = this.dragIds;
        this.dragIds = null;
        this.d.move(ids, this.insertionIndex(e.clientY));
        return;
      }
      setTimeout(() => { this.dropHint = null; }, 1000);
    }, o);
    const ro = new ResizeObserver(() => this.schedule());
    ro.observe(this.list);
    signal.addEventListener("abort", () => {
      ro.disconnect();
      cancelAnimationFrame(this.frame);
      clearTimeout(this.timer);
      for (const b of this.cache.values()) b.close();
      this.cache.clear();
    }, { once: true });
  }

  // ---- layout ---------------------------------------------------------------

  private boxSize(ref: PageRef): [number, number] {
    const s = this.d.source(ref.src);
    const info = s.infos[ref.index] ?? s.fallback;
    const [w, h] = displaySize(info.rotate + ref.rotation, info.w, info.h);
    let bw = THUMB_W, bh = Math.round(THUMB_W * h / w);
    if (bh > THUMB_MAX_H) { bh = THUMB_MAX_H; bw = Math.round(THUMB_MAX_H * w / h); }
    return [bw, bh];
  }

  /** Recompute row positions (after any page-model or size change). */
  relayout(): void {
    const pages = this.d.pages;
    this.tops = new Array(pages.length);
    this.heights = new Array(pages.length);
    let y = 6;
    pages.forEach((p, i) => {
      const h = this.boxSize(p)[1] + LABEL_H + ROW_PAD * 2;
      this.tops[i] = y; this.heights[i] = h;
      y += h;
    });
    this.inner.style.height = `${y + 6}px`;
    const ids = new Set(pages.map((p) => p.id));
    for (const id of [...this.selected]) if (!ids.has(id)) this.selected.delete(id);
    for (const r of this.rows.values()) r.el.remove();
    this.rows.clear();
    this.schedule();
    this.updateFooter();
  }

  schedule(): void {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.update(); });
  }

  private rowAt(y: number): number {
    const t = this.tops;
    let lo = 0, hi = t.length - 1;
    if (hi < 0) return -1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (t[mid]! <= y) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  private visible(extra: number): number[] {
    const top = this.list.scrollTop - extra, bottom = this.list.scrollTop + this.list.clientHeight + extra;
    const out: number[] = [];
    for (let i = Math.max(0, this.rowAt(Math.max(0, top))); i >= 0 && i < this.tops.length && this.tops[i]! < bottom; i++) out.push(i);
    return out;
  }

  update(): void {
    if (!this.root.isConnected || this.list.clientHeight === 0) return;
    const pages = this.d.pages;
    const want = this.visible(this.list.clientHeight);
    const keep = new Set(want.map((p) => pages[p]!.id));
    for (const [id, r] of this.rows) if (!keep.has(id)) { r.el.remove(); this.rows.delete(id); }
    for (const p of want) {
      const ref = pages[p]!;
      let row = this.rows.get(ref.id);
      if (!row) {
        row = this.makeRow(ref, p);
        this.rows.set(ref.id, row);
        this.inner.append(row.el);
      }
      this.decorate(row, p);
    }
    this.pump();
  }

  private makeRow(ref: PageRef, pos: number): Row {
    const [bw, bh] = this.boxSize(ref);
    const canvas = el("canvas.pdf-thumb-canvas", { width: Math.round(bw * devicePixelRatio), height: Math.round(bh * devicePixelRatio) });
    canvas.style.width = `${bw}px`;
    canvas.style.height = `${bh}px`;
    const label = el("div.pdf-thumb-label");
    const rowEl = el("div.pdf-thumb", { role: "option", "data-page-id": ref.id },
      el("div.pdf-thumb-box", {}, canvas), label);
    rowEl.style.top = `${this.tops[pos]}px`;
    rowEl.style.height = `${this.heights[pos]}px`;
    const row: Row = { ref, el: rowEl, canvas, key: "" };
    const bmp = this.cache.get(this.keyOf(ref));
    if (bmp) this.paint(row, bmp, this.keyOf(ref));
    return row;
  }

  private decorate(row: Row, pos: number): void {
    row.el.draggable = true;
    row.el.style.top = `${this.tops[pos]}px`;
    (row.el.lastElementChild as HTMLElement).textContent = String(pos + 1);
    row.el.dataset.pos = String(pos);
    const sel = this.selected.has(row.ref.id);
    row.el.classList.toggle("selected", sel);
    row.el.setAttribute("aria-selected", String(sel));
    row.el.classList.toggle("current", pos === this.d.current);
  }

  /** Refresh selection/current highlighting without re-rendering. */
  refreshMarks(): void {
    const pages = this.d.pages;
    for (const r of this.rows.values()) {
      const pos = pages.indexOf(r.ref);
      if (pos >= 0) this.decorate(r, pos);
    }
  }

  private keyOf(ref: PageRef): string {
    return `${ref.src}:${ref.index}:${ref.rotation}:${this.formsVersion}`;
  }

  /** Drop cached bitmaps (markup/forms changed). */
  invalidate(): void {
    this.formsVersion++;
    this.schedule();
  }

  private paint(row: Row, bmp: ImageBitmap, key: string): void {
    const c = row.canvas.getContext("2d")!;
    c.drawImage(bmp, 0, 0, row.canvas.width, row.canvas.height);
    row.key = key;
  }

  /** Render the next visible thumbnail, only while the main canvas is idle. */
  private pump(): void {
    if (this.busy) return;
    if (!this.d.idle()) {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.pump(), 120) as unknown as number;
      return;
    }
    const pages = this.d.pages;
    for (const p of this.visible(this.list.clientHeight / 2)) {
      const ref = pages[p]!;
      const row = this.rows.get(ref.id);
      const key = this.keyOf(ref);
      if (!row || row.key === key) continue;
      const hit = this.cache.get(key);
      if (hit) { this.paint(row, hit, key); continue; }
      this.busy = true;
      void this.render(ref, row.canvas.width, row.canvas.height).then((bmp) => {
        if (bmp) {
          this.cache.set(key, bmp);
          if (this.cache.size > CACHE_MAX) {
            const first = this.cache.keys().next().value!;
            this.cache.get(first)?.close();
            this.cache.delete(first);
          }
          const r = this.rows.get(ref.id);
          if (r && this.keyOf(r.ref) === key) this.paint(r, bmp, key);
        }
      }).finally(() => { this.busy = false; if (!this.signal.aborted) this.pump(); });
      return;
    }
  }

  private async render(ref: PageRef, w: number, h: number): Promise<ImageBitmap | null> {
    try {
      const src = this.d.source(ref.src);
      const page = await src.doc.getPage(ref.index + 1);
      const rot = (page.rotate + ref.rotation) % 360;
      const base = page.getViewport({ scale: 1, rotation: rot });
      const vp = page.getViewport({ scale: Math.min(w / base.width, h / base.height), rotation: rot });
      const canvas = el("canvas", { width: w, height: h });
      const c = canvas.getContext("2d", { alpha: false })!;
      c.fillStyle = "#fff";
      c.fillRect(0, 0, w, h);
      await page.render({ canvas, canvasContext: c, viewport: vp, annotationMode: pdfjs.AnnotationMode.ENABLE_STORAGE }).promise;
      this.d.paintMarkup?.(c, ref, vp.scale);
      return await createImageBitmap(canvas);
    } catch (e) {
      if (!(e instanceof pdfjs.RenderingCancelledException)) console.warn("thumbnail", e);
      return null;
    }
  }

  // ---- selection ----------------------------------------------------------------

  /** Pages the page commands act on: explicit sidebar selection, else the current page. */
  targets(sidebarVisible: boolean): PageRef[] {
    const pages = this.d.pages;
    const sel = sidebarVisible && this.selected.size ? pages.filter((p) => this.selected.has(p.id)) : [];
    return sel.length ? sel : pages[this.d.current] ? [pages[this.d.current]!] : [];
  }

  /** Explicitly selected pages in document order. */
  explicitSelection(): PageRef[] {
    return this.explicit ? this.d.pages.filter((p) => this.selected.has(p.id)) : [];
  }

  select(ids: number[], anchor: number, explicit = true): void {
    this.selected = new Set(ids);
    this.anchor = anchor;
    this.explicit = explicit && ids.length > 0;
    this.refreshMarks();
    this.updateFooter();
    this.d.selectionChanged();
  }

  clear(): boolean {
    if (!this.selected.size) return false;
    this.select([], -1, false);
    return true;
  }

  private updateFooter(): void {
    const n = this.explicit ? this.selected.size : 0;
    const total = this.d.pages.length;
    this.footer.hidden = n === 0;
    if (n) {
      this.footer.textContent = n < total
        ? `${n} page${n > 1 ? "s" : ""} selected · Ctrl+S saves ${n > 1 ? "them" : "it"} as a new PDF`
        : `All ${n} pages selected`;
    }
  }

  private rowFromEvent(e: MouseEvent): number {
    const row = (e.target as HTMLElement).closest?.(".pdf-thumb") as HTMLElement | null;
    return row ? Number(row.dataset.pos) : -1;
  }

  private clickRow(pos: number, e: { ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): void {
    const pages = this.d.pages;
    const ref = pages[pos];
    if (!ref) return;
    if (e.shiftKey && this.anchor >= 0) {
      const [a, b] = this.anchor < pos ? [this.anchor, pos] : [pos, this.anchor];
      const ids = pages.slice(a, b + 1).map((p) => p.id);
      if (e.ctrlKey || e.metaKey) for (const id of this.selected) ids.push(id);
      this.select(ids, this.anchor);
    } else if (e.ctrlKey || e.metaKey) {
      const s = new Set(this.selected);
      if (s.has(ref.id)) s.delete(ref.id); else s.add(ref.id);
      this.select([...s], pos);
    } else {
      this.select([ref.id], pos);
    }
    this.d.goto(pos);
  }

  private onKey(e: KeyboardEvent): void {
    const pages = this.d.pages;
    const cur = this.d.current;
    const mod = e.ctrlKey || e.metaKey;
    let to = -1;
    if (e.key === "ArrowDown" && e.altKey) { this.nudge(1); e.preventDefault(); return; }
    if (e.key === "ArrowUp" && e.altKey) { this.nudge(-1); e.preventDefault(); return; }
    if (e.key === "ArrowDown") to = cur + 1;
    else if (e.key === "ArrowUp") to = cur - 1;
    else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = pages.length - 1;
    else if (mod && e.key.toLowerCase() === "a") {
      this.select(pages.map((p) => p.id), 0);
      e.preventDefault(); e.stopPropagation();
      return;
    } else if (e.key === " " || e.key === "Enter") {
      this.clickRow(cur, { ctrlKey: mod, metaKey: false, shiftKey: e.shiftKey });
      e.preventDefault();
      return;
    } else return;
    to = Math.max(0, Math.min(to, pages.length - 1));
    e.preventDefault();
    e.stopPropagation();
    this.clickRow(to, { ctrlKey: false, metaKey: false, shiftKey: e.shiftKey });
    this.reveal(to);
  }

  /** Alt+Up/Down: move the selected pages by one position. */
  private nudge(dir: number): void {
    const pages = this.d.pages;
    const ids = this.targets(true).map((p) => p.id);
    const idx = ids.map((id) => pages.findIndex((p) => p.id === id)).sort((a, b) => a - b);
    if (!idx.length) return;
    const dest = dir < 0 ? Math.max(0, idx[0]! - 1) : Math.min(pages.length, idx[idx.length - 1]! + 2);
    this.d.move(ids, dest);
  }

  reveal(pos: number): void {
    const t = this.tops[pos], h = this.heights[pos];
    if (t === undefined || h === undefined) return;
    const l = this.list;
    if (t < l.scrollTop) l.scrollTop = t - 6;
    else if (t + h > l.scrollTop + l.clientHeight) l.scrollTop = t + h - l.clientHeight + 6;
  }

  // ---- drag to reorder -------------------------------------------------------------

  private insertionIndex(clientY: number): number {
    const r = this.list.getBoundingClientRect();
    const y = clientY - r.top + this.list.scrollTop;
    const i = this.rowAt(y);
    if (i < 0) return 0;
    return y > this.tops[i]! + this.heights[i]! / 2 ? i + 1 : i;
  }

  private showIndicator(at: number): void {
    const n = this.tops.length;
    const y = at >= n ? (this.tops[n - 1] ?? 0) + (this.heights[n - 1] ?? 0) : this.tops[at] ?? 0;
    this.indicator.hidden = false;
    this.indicator.style.top = `${y - 1}px`;
  }

  /** Pages being dragged from this list (reorder on drop here, a PDF file when dropped elsewhere). */
  private dragIds: number[] | null = null;

  private onDragStart(e: DragEvent): void {
    const pos = this.rowFromEvent(e);
    const dt = e.dataTransfer;
    if (pos < 0 || !dt) return;
    const pages = this.d.pages;
    const ref = pages[pos]!;
    if (!this.selected.has(ref.id)) this.select([ref.id], pos);
    const ids = pages.filter((p) => this.selected.has(p.id)).map((p) => p.id);
    this.dragIds = ids;
    dt.effectAllowed = "copyMove";
    dt.setData(PAGES_TYPE, ids.join(","));
    // Dropped on the desktop / a file manager: Chrome downloads this URL. It
    // must exist now (dragstart is synchronous), so the viewer prepares the
    // PDF of the selection in the background when the selection changes.
    const out = this.d.dragOut?.(ids);
    if (out) dt.setData("DownloadURL", `application/pdf:${out[0]}:${out[1]}`);
    const ghost = el("div.pdf-thumb-ghost", { text: ids.length > 1 ? `${ids.length} pages` : `Page ${pos + 1}` });
    document.body.append(ghost);
    dt.setDragImage(ghost, -8, -8);
    setTimeout(() => ghost.remove());
    this.list.classList.add("dragging");
  }

  private autoScroll(y: number): void {
    const r = this.list.getBoundingClientRect();
    if (y < r.top + 30) this.list.scrollTop -= 14;
    else if (y > r.bottom - 30) this.list.scrollTop += 14;
  }
}
