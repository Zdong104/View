// The virtualized grid of the table viewer: renders only the rows and columns in view
// (DOM rows from a pool, rows fetched from the worker in blocks), selection, keyboard
// navigation, inline cell editing, column resize, clipboard and context menus.
import { el } from "../../ui/kit.ts";
import type { SelectionStats, TableClient, TableState } from "./client.ts";
import { popupMenu, type MenuItem } from "./menu.ts";

export const ROW_H = 24;
export const HEAD_H = 28;
const BLOCK = 100;
const MAX_BODY_H = 30_000_000; // Chrome clamps element heights at ~33.5M px
const MIN_COL = 40;
const MAX_BLOCKS = 300;

export const colLetter = (i: number): string => {
  let s = "";
  for (let n = i + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
};

export interface GridHost {
  readonly client: TableClient;
  toast(message: string, opts?: { timeout?: number }): void;
  /** The worker published a new state (after an edit, undo, sort ...). */
  stateChanged(s: TableState): void;
  selectionChanged(): void;
  isBusy(): boolean;
  /** The first block of rows arrived (first paint with data). */
  firstData?(): void;
  /** Runs a long worker operation behind the busy indicator. */
  busy<T>(label: string, fn: () => Promise<T>): Promise<T>;
}

interface RowEl extends HTMLDivElement { _r: number; _c0: number; _c1: number; _lay: number; _ver: number }
interface CellEl extends HTMLDivElement { _t: string }
interface Block { rows: string[][]; epoch: number }
interface Rect { r0: number; c0: number; r1: number; c1: number }
type Zone = "cell" | "gutter" | "head" | "corner" | "none";

const TYPE_ALIGN = new Set(["number"]);
const trunc = (t: string) => (t.length > 400 ? t.slice(0, 400) + "…" : t);

/** Parse clipboard text as TSV (RFC-4180-style quotes, like spreadsheets copy it). */
export function parseTsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], field = "", q = false;
  const n = text.length;
  for (let i = 0; i < n; i++) {
    const ch = text[i]!;
    if (q) {
      if (ch === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else q = false; } else field += ch;
    } else if (ch === '"' && field === "") q = true;
    else if (ch === "\t") { row.push(field); field = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += ch;
  }
  if (field !== "" || row.length || !rows.length) { row.push(field); rows.push(row); }
  return rows;
}

export class Grid {
  readonly el: HTMLDivElement;
  private readonly scroll: HTMLDivElement;
  private readonly sizer: HTMLDivElement;
  private readonly head: HTMLDivElement;
  private readonly body: HTMLDivElement;

  st: TableState;
  private types: string[] = [];
  private colW: number[] = [];
  private colX: number[] = [0];
  private gutW = 48;
  private lay = 0; // layout version (widths / column set)
  private ver = 0; // data version (anything cells show)
  private hver = 0;
  private headKey = "";

  private blocks = new Map<number, Block>();
  private fetching = new Set<number>();
  private rowEls = new Map<number, RowEl>();
  private freeRows: RowEl[] = [];
  private raf = 0;
  private destroyed = false;
  private gotFirst = false;

  cur = { r: 0, c: 0 };
  anchor = { r: 0, c: 0 };
  private needle = "";
  private curHitRow = -1;
  private editor: { ta: HTMLTextAreaElement; r: number; c: number; orig: string | null } | null = null;
  private headEditor: HTMLInputElement | null = null;
  private drag: { mode: "cell" | "row" | "col"; x: number; y: number; timer: number } | null = null;

  private readonly host: GridHost;

  constructor(host: GridHost, initial: TableState) {
    this.host = host;
    this.st = initial;
    this.head = el("div.tbl-head");
    this.body = el("div.tbl-body");
    this.sizer = el("div.tbl-sizer", {}, this.head, this.body);
    this.scroll = el("div.tbl-scroll", { tabindex: 0, role: "grid", "aria-label": "Table", "aria-rowcount": initial.nView });
    this.scroll.append(this.sizer);
    this.el = el("div.tbl-grid", {}, this.scroll);
    this.colW = new Array(initial.ncols).fill(120);
    this.layoutCols();

    const s = this.scroll;
    s.addEventListener("scroll", () => this.schedule(), { passive: true });
    s.addEventListener("keydown", (e) => this.onKey(e));
    s.addEventListener("pointerdown", (e) => this.onPointerDown(e));
    s.addEventListener("dblclick", (e) => this.onDblClick(e));
    s.addEventListener("contextmenu", (e) => this.onContextMenu(e));
    this.el.addEventListener("copy", (e) => this.onCopy(e, false));
    this.el.addEventListener("cut", (e) => this.onCopy(e, true));
    this.el.addEventListener("paste", (e) => this.onPaste(e));
    new ResizeObserver(() => this.schedule()).observe(s);
  }

  // ---- state ----------------------------------------------------------------------

  get editable(): boolean { return this.st.loaded && !this.host.isBusy(); }
  get isEditing(): boolean { return !!this.editor || !!this.headEditor; }

  /** Adopt a new worker state; `patch` lists cell edits already applied locally to cached blocks. */
  applyState(s: TableState, patch?: [number, number, string][]): void {
    const old = this.st;
    this.st = s;
    if (old.struct !== s.struct) this.blocks.clear();
    else if (old.epoch !== s.epoch && patch) {
      for (const [r, c, t] of patch) {
        const bi = (r / BLOCK) | 0;
        const b = this.blocks.get(bi);
        const row = b?.rows[r - bi * BLOCK];
        if (b && b.epoch === old.epoch && row) row[c] = t;
      }
      for (const b of this.blocks.values()) if (b.epoch === old.epoch) b.epoch = s.epoch;
    }
    if (this.colW.length !== s.ncols) {
      const w = this.colW.slice(0, s.ncols);
      while (w.length < s.ncols) w.push(120);
      this.colW = w;
      this.types.length = Math.min(this.types.length, s.ncols);
    }
    this.layoutCols();
    this.clampCursor();
    this.ver++; this.hver++;
    this.scroll.setAttribute("aria-rowcount", String(s.nView));
    this.schedule();
  }

  /** Initial column widths and alignment from a sample of the data. */
  async autoSize(): Promise<void> {
    const m = await this.host.client.call<{ widths: number[]; types: string[] }>("measure");
    this.types = m.types;
    this.colW = m.widths.map((chars, c) => this.widthFor(chars, c));
    this.layoutCols();
    this.ver++; this.hver++;
    this.schedule();
  }

  private widthFor(chars: number, c: number): number {
    const title = this.st.headers[c] || colLetter(c);
    const w = Math.max(chars * 7.4 + 28, Math.min(title.length * 7.8 + 56, 240));
    return Math.round(Math.max(56, Math.min(w, 380)));
  }

  async fitColumn(c: number): Promise<void> {
    const m = await this.host.client.call<{ widths: number[]; types: string[] }>("measure");
    this.colW[c] = this.widthFor(m.widths[c] ?? 8, c);
    this.layoutCols();
    this.ver++; this.hver++;
    this.schedule();
  }

  private layoutCols(): void {
    const x = new Array<number>(this.colW.length + 1);
    x[0] = 0;
    for (let i = 0; i < this.colW.length; i++) x[i + 1] = x[i]! + this.colW[i]!;
    this.colX = x;
    const digits = String(Math.max(1, this.st.nView)).length;
    this.gutW = Math.max(44, digits * 8 + 24);
    this.lay++;
    this.sizer.style.width = `${this.gutW + x[x.length - 1]!}px`;
    this.head.style.width = this.sizer.style.width;
    this.body.style.width = this.sizer.style.width;
    const bodyH = this.bodyHeight();
    this.body.style.height = `${bodyH}px`;
    this.sizer.style.height = `${bodyH + HEAD_H}px`;
  }

  private bodyHeight(): number { return Math.min(this.st.nView * ROW_H + 8, MAX_BODY_H); }
  private virtualHeight(): number { return this.st.nView * ROW_H + 8; }

  /** Ratio of virtual to real scroll distance (1 unless the table is taller than Chrome allows). */
  private scaleK(): number {
    const vh = Math.max(1, this.scroll.clientHeight - HEAD_H);
    const real = this.bodyHeight(), virt = this.virtualHeight();
    return virt > real ? (virt - vh) / Math.max(1, real - vh) : 1;
  }
  private vTop(): number { return this.scroll.scrollTop * this.scaleK(); }
  private rowY(r: number): number { return this.scroll.scrollTop + (r * ROW_H - this.vTop()); }

  // ---- rendering ---------------------------------------------------------------------

  private schedule(): void {
    if (this.raf || this.destroyed) return;
    this.raf = requestAnimationFrame(() => { this.raf = 0; this.render(); });
  }

  /** Force a synchronous render (tests, scroll-into-view). */
  render(): void {
    if (this.destroyed) return;
    const sc = this.scroll;
    const n = this.st.nView, nc = this.st.ncols;
    const viewH = sc.clientHeight, viewW = sc.clientWidth;
    const vTop = this.vTop();
    const bodyViewH = Math.max(0, viewH - HEAD_H);
    const first = Math.max(0, Math.floor(vTop / ROW_H) - 2);
    const last = Math.min(n - 1, Math.ceil((vTop + bodyViewH) / ROW_H) + 2);
    // visible column window
    const x0 = sc.scrollLeft, x1 = x0 + Math.max(0, viewW - this.gutW);
    let c0 = this.colAt(x0) - 1, c1 = this.colAt(x1) + 1;
    c0 = Math.max(0, c0); c1 = Math.min(nc - 1, c1);

    // rows
    for (const [r, row] of this.rowEls) {
      if (r < first || r > last) { row.remove(); this.rowEls.delete(r); this.freeRows.push(row); }
    }
    const needBlocks = new Set<number>();
    for (let r = first; r <= last; r++) {
      let row = this.rowEls.get(r);
      if (!row) {
        row = this.freeRows.pop() ?? this.makeRow();
        row._ver = -1;
        this.rowEls.set(r, row);
        this.body.append(row);
      }
      row.style.top = `${this.rowY(r)}px`;
      this.fillRow(row, r, c0, c1);
      const bi = (r / BLOCK) | 0;
      needBlocks.add(bi);
    }
    // fetch visible blocks (+ one ahead/behind)
    const lo = ((first / BLOCK) | 0) - 1, hi = ((last / BLOCK) | 0) + 1;
    for (let bi = Math.max(0, lo); bi <= hi && bi * BLOCK < n; bi++) {
      const b = this.blocks.get(bi);
      if (!b || b.epoch !== this.st.epoch) this.fetchBlock(bi);
    }
    this.evict(lo, hi);
    this.renderHead(c0, c1);
    this.sizer.parentElement?.setAttribute("data-first-row", String(first));
  }

  private makeRow(): RowEl {
    const row = el("div.tbl-row") as RowEl;
    row.append(el("div.tbl-rn"));
    row._r = -1; row._c0 = -1; row._c1 = -1; row._lay = -1; row._ver = -1;
    return row;
  }

  private fillRow(row: RowEl, r: number, c0: number, c1: number): void {
    const data = this.getRow(r);
    const rebuild = row._lay !== this.lay || row._c0 !== c0 || row._c1 !== c1;
    if (rebuild) {
      while (row.childElementCount > 1) row.lastElementChild!.remove();
      for (let c = c0; c <= c1; c++) {
        const cell = el("div.tbl-cell", { role: "gridcell" }) as CellEl;
        cell.dataset.c = String(c);
        cell.style.left = `${this.gutW + this.colX[c]!}px`;
        cell.style.width = `${this.colW[c]}px`;
        cell._t = "\u0000";
        row.append(cell);
      }
      const g = row.firstElementChild as HTMLElement;
      g.style.width = `${this.gutW}px`;
      row._lay = this.lay; row._c0 = c0; row._c1 = c1; row._ver = -1;
    }
    if (row._r !== r) { row._r = r; row._ver = -1; (row.firstElementChild as HTMLElement).textContent = String(r + 1); }
    if (row._ver !== this.ver || rebuild) {
      row._ver = this.ver;
      row.classList.toggle("loading", !data);
      for (let i = 1; i < row.childElementCount; i++) {
        const cell = row.children[i] as CellEl;
        const c = c0 + i - 1;
        const t = data?.[c] ?? "";
        if (cell._t !== t) { cell._t = t; cell.textContent = trunc(t); }
        this.styleCell(cell, r, c, t);
      }
      this.styleGutter(row, r);
    }
  }

  private selRect(): Rect {
    return {
      r0: Math.min(this.anchor.r, this.cur.r), r1: Math.max(this.anchor.r, this.cur.r),
      c0: Math.min(this.anchor.c, this.cur.c), c1: Math.max(this.anchor.c, this.cur.c),
    };
  }

  private styleCell(cell: CellEl, r: number, c: number, t: string): void {
    const s = this.selRect();
    const inSel = r >= s.r0 && r <= s.r1 && c >= s.c0 && c <= s.c1;
    const multi = s.r0 !== s.r1 || s.c0 !== s.c1;
    let cls = "tbl-cell";
    if (this.types[c] && TYPE_ALIGN.has(this.types[c]!)) cls += " num";
    if (inSel && multi) cls += " sel";
    if (r === this.cur.r && c === this.cur.c) cls += " cur";
    if (this.needle && t && t.toLowerCase().includes(this.needle)) cls += r === this.curHitRow ? " hit hit-cur" : " hit";
    if (cell.className !== cls) cell.className = cls;
  }

  private styleGutter(row: RowEl, r: number): void {
    const s = this.selRect();
    row.firstElementChild!.classList.toggle("sel", r >= s.r0 && r <= s.r1);
    row.classList.toggle("odd", (r & 1) === 1);
  }

  /** Cheap re-class of rendered cells after the selection moved. */
  private paintSelection(): void {
    for (const [r, row] of this.rowEls) {
      for (let i = 1; i < row.childElementCount; i++) {
        const cell = row.children[i] as CellEl;
        this.styleCell(cell, r, row._c0 + i - 1, cell._t === "\u0000" ? "" : cell._t);
      }
      this.styleGutter(row, r);
    }
    this.hver++;
    this.schedule();
  }

  private renderHead(c0: number, c1: number): void {
    const key = `${c0}-${c1}-${this.lay}-${this.hver}-${this.st.sortCol}-${this.st.sortAsc}`;
    if (key === this.headKey) return;
    this.headKey = key;
    const s = this.selRect();
    const colSel = s.r0 === 0 && s.r1 >= this.st.nView - 1;
    const frag = document.createDocumentFragment();
    const corner = el("div.tbl-corner", { style: `width:${this.gutW}px` });
    frag.append(corner);
    for (let c = c0; c <= c1; c++) {
      const sorted = this.st.sortCol === c;
      const title = this.st.hasHeader ? this.st.headers[c] || "" : "";
      const th = el("div.tbl-th", {
        role: "columnheader",
        "data-c": c,
        title: `${colLetter(c)}${title ? " · " + title : ""}`,
        "aria-sort": sorted ? (this.st.sortAsc ? "ascending" : "descending") : undefined,
        style: `left:${this.gutW + this.colX[c]!}px;width:${this.colW[c]}px`,
      },
      el(title ? "span.th-title" : "span.th-title.dim", { text: title || colLetter(c) }),
      el(sorted ? "button.th-sort.on" : "button.th-sort", {
        type: "button", tabindex: -1, "data-c": c,
        title: sorted ? (this.st.sortAsc ? "Sorted ascending (click for descending)" : "Sorted descending (click to clear)") : "Sort",
        text: sorted ? (this.st.sortAsc ? "▲" : "▼") : "⇅",
      }),
      el("div.th-rz", { "data-c": c }));
      if (colSel && c >= s.c0 && c <= s.c1) th.classList.add("sel");
      frag.append(th);
    }
    this.head.replaceChildren(frag);
  }

  // ---- row blocks ----------------------------------------------------------------------

  private getRow(r: number): string[] | undefined {
    const bi = (r / BLOCK) | 0;
    return this.blocks.get(bi)?.rows[r - bi * BLOCK];
  }

  /** Cached cell text (undefined until its block arrived). */
  cellText(r: number, c: number): string | undefined { return this.getRow(r)?.[c]; }

  private fetchBlock(bi: number): void {
    if (this.fetching.has(bi)) return;
    this.fetching.add(bi);
    void this.host.client.rows(bi * BLOCK, BLOCK).then((res) => {
      this.fetching.delete(bi);
      if (this.destroyed) return;
      if (res.struct < this.st.struct) { this.schedule(); return; }
      this.blocks.set(bi, { rows: res.rows, epoch: res.epoch });
      if (!this.gotFirst) { this.gotFirst = true; requestAnimationFrame(() => this.host.firstData?.()); }
      this.ver++;
      this.schedule();
    }, () => { this.fetching.delete(bi); });
  }

  private evict(lo: number, hi: number): void {
    if (this.blocks.size <= MAX_BLOCKS) return;
    const mid = (lo + hi) / 2;
    const keys = [...this.blocks.keys()].sort((a, b) => Math.abs(b - mid) - Math.abs(a - mid));
    for (const k of keys.slice(0, this.blocks.size - MAX_BLOCKS)) this.blocks.delete(k);
  }

  /** True when every row in [r0, r1] is cached. */
  private cached(r0: number, r1: number): boolean {
    for (let bi = (r0 / BLOCK) | 0; bi <= ((r1 / BLOCK) | 0); bi++) {
      const b = this.blocks.get(bi);
      if (!b || b.epoch !== this.st.epoch) return false;
    }
    return true;
  }

  // ---- geometry ------------------------------------------------------------------------

  private colAt(x: number): number {
    const cx = this.colX;
    let lo = 0, hi = cx.length - 2;
    if (hi < 0) return 0;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (cx[mid]! <= x) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  private hitTest(clientX: number, clientY: number): { zone: Zone; r: number; c: number } {
    const rect = this.scroll.getBoundingClientRect();
    const x = clientX - rect.left, y = clientY - rect.top;
    const sc = this.scroll;
    if (x > sc.clientWidth || y > sc.clientHeight) return { zone: "none", r: 0, c: 0 };
    const c = Math.max(0, Math.min(this.st.ncols - 1, this.colAt(x - this.gutW + sc.scrollLeft)));
    const r = Math.max(0, Math.min(this.st.nView - 1, Math.floor((this.vTop() + y - HEAD_H) / ROW_H)));
    const inHead = y < HEAD_H, inGut = x < this.gutW;
    return { zone: inHead ? (inGut ? "corner" : "head") : inGut ? "gutter" : "cell", r, c };
  }

  /** Scroll so that (r, c) is fully visible. */
  ensureVisible(r: number, c: number): void {
    const sc = this.scroll;
    const k = this.scaleK();
    const bodyViewH = sc.clientHeight - HEAD_H;
    const vTop = sc.scrollTop * k;
    if (r * ROW_H < vTop) sc.scrollTop = (r * ROW_H) / k;
    else if ((r + 1) * ROW_H > vTop + bodyViewH) sc.scrollTop = ((r + 1) * ROW_H - bodyViewH) / k;
    const left = this.colX[c] ?? 0, right = this.colX[c + 1] ?? left;
    const viewW = sc.clientWidth - this.gutW;
    if (left < sc.scrollLeft) sc.scrollLeft = left;
    else if (right > sc.scrollLeft + viewW) sc.scrollLeft = Math.min(right - viewW, left);
    this.render();
  }

  // ---- selection ---------------------------------------------------------------------

  private clampCursor(): void {
    const n = this.st.nView, nc = this.st.ncols;
    const cl = (p: { r: number; c: number }) => {
      p.r = Math.max(0, Math.min(p.r, Math.max(0, n - 1)));
      p.c = Math.max(0, Math.min(p.c, Math.max(0, nc - 1)));
    };
    cl(this.cur); cl(this.anchor);
  }

  setCursor(r: number, c: number, extend = false, scroll = true): void {
    this.cur = { r, c };
    if (!extend) this.anchor = { r, c };
    this.clampCursor();
    if (scroll) this.ensureVisible(this.cur.r, this.cur.c);
    this.paintSelection();
    this.host.selectionChanged();
  }

  selectRect(r0: number, c0: number, r1: number, c1: number): void {
    this.anchor = { r: r0, c: c0 }; this.cur = { r: r1, c: c1 };
    this.clampCursor();
    this.paintSelection();
    this.host.selectionChanged();
  }

  selectAll(): void { this.selectRect(0, 0, this.st.nView - 1, this.st.ncols - 1); }

  get selection(): Rect { return this.selRect(); }

  focus(): void { this.scroll.focus({ preventScroll: true }); }

  /** Highlight matches of `needle` (case-insensitive); `curRow` is the active hit's row. */
  setHighlight(needle: string, curRow = -1): void {
    this.needle = needle.toLowerCase();
    this.curHitRow = curRow;
    this.ver++;
    this.schedule();
  }

  /** The sample rows around the viewport, for tests. */
  get renderedRows(): number { return this.rowEls.size; }
  get scrollEl(): HTMLElement { return this.scroll; }

  // ---- keyboard --------------------------------------------------------------------

  private onKey(e: KeyboardEvent): void {
    if (this.isEditing || e.target !== this.scroll) return;
    const n = this.st.nView, nc = this.st.ncols;
    const { r, c } = this.cur;
    const ext = e.shiftKey;
    const ctrl = e.ctrlKey || e.metaKey;
    const go = (nr: number, nc2: number) => { e.preventDefault(); this.setCursor(nr, nc2, ext); };
    if (ctrl) {
      switch (e.key) {
        case "Home": return go(0, 0);
        case "End": return go(n - 1, nc - 1);
        case "ArrowUp": return go(0, c);
        case "ArrowDown": return go(n - 1, c);
        case "ArrowLeft": return go(r, 0);
        case "ArrowRight": return go(r, nc - 1);
      }
      return;
    }
    if (e.altKey || n === 0 || nc === 0) return;
    const page = Math.max(1, Math.floor((this.scroll.clientHeight - HEAD_H) / ROW_H) - 1);
    switch (e.key) {
      case "ArrowUp": return go(r - 1, c);
      case "ArrowDown": return go(r + 1, c);
      case "ArrowLeft": return go(r, c - 1);
      case "ArrowRight": return go(r, c + 1);
      case "Home": return go(r, 0);
      case "End": return go(r, nc - 1);
      case "PageUp": return go(r - page, c);
      case "PageDown": return go(r + page, c);
      case "Tab":
        e.preventDefault();
        if (e.shiftKey) { if (c > 0) this.setCursor(r, c - 1); else if (r > 0) this.setCursor(r - 1, nc - 1); }
        else if (c < nc - 1) this.setCursor(r, c + 1);
        else if (r < n - 1) this.setCursor(r + 1, 0);
        return;
      case "Enter": case "F2":
        e.preventDefault();
        if (this.editable) void this.startEdit();
        return;
      case "Delete": case "Backspace":
        e.preventDefault();
        if (this.editable) void this.clearSelection();
        return;
      case "Escape":
        this.setCursor(r, c, false, false);
        return;
      case " ":
        if (e.shiftKey) { e.preventDefault(); this.selectRect(r, 0, r, nc - 1); return; }
    }
    if (this.editable && !e.isComposing && e.key.length === 1) {
      e.preventDefault();
      void this.startEdit(e.key);
    } else if (this.editable && (e.isComposing || e.key === "Process")) {
      void this.startEdit("");
    }
  }

  // ---- mouse --------------------------------------------------------------------------

  private onPointerDown(e: PointerEvent): void {
    if (this.isEditing && (e.target as HTMLElement).closest(".tbl-edit")) return;
    const t = e.target as HTMLElement;
    if (e.button === 2) return; // handled by contextmenu
    if (e.button !== 0) return;
    const rz = t.closest<HTMLElement>(".th-rz");
    if (rz) { this.startResize(e, Number(rz.dataset.c)); return; }
    const sortBtn = t.closest<HTMLElement>(".th-sort");
    if (sortBtn) { e.preventDefault(); this.focus(); void this.cycleSort(Number(sortBtn.dataset.c)); return; }
    const h = this.hitTest(e.clientX, e.clientY);
    if (h.zone === "none") return;
    this.commitEdit();
    this.focus();
    e.preventDefault(); // keep focus on the grid, no text selection
    const nc = this.st.ncols, n = this.st.nView;
    if (h.zone === "corner") { this.selectAll(); return; }
    if (h.zone === "head") {
      if (e.shiftKey) this.selectRect(0, this.anchor.c, n - 1, h.c);
      else this.selectRect(0, h.c, n - 1, h.c);
      this.beginDrag("col", e);
      return;
    }
    if (h.zone === "gutter") {
      if (e.shiftKey) this.selectRect(this.anchor.r, 0, h.r, nc - 1);
      else this.selectRect(h.r, 0, h.r, nc - 1);
      this.beginDrag("row", e);
      return;
    }
    if (n === 0) return;
    this.setCursor(h.r, h.c, e.shiftKey, false);
    this.beginDrag("cell", e);
  }

  private beginDrag(mode: "cell" | "row" | "col", e: PointerEvent): void {
    const d = { mode, x: e.clientX, y: e.clientY, timer: 0 };
    this.drag = d;
    const move = (ev: PointerEvent) => { d.x = ev.clientX; d.y = ev.clientY; this.dragTo(); };
    const up = () => {
      removeEventListener("pointermove", move); removeEventListener("pointerup", up);
      clearInterval(d.timer); this.drag = null;
    };
    addEventListener("pointermove", move);
    addEventListener("pointerup", up);
    d.timer = window.setInterval(() => this.autoscroll(), 40);
  }

  private dragTo(): void {
    const d = this.drag;
    if (!d) return;
    const sc = this.scroll.getBoundingClientRect();
    const x = Math.max(sc.left + this.gutW + 1, Math.min(d.x, sc.left + this.scroll.clientWidth - 1));
    const y = Math.max(sc.top + HEAD_H + 1, Math.min(d.y, sc.top + this.scroll.clientHeight - 1));
    const h = this.hitTest(x, y);
    if (h.zone === "none") return;
    const nc = this.st.ncols, n = this.st.nView;
    if (d.mode === "cell") this.setCursor(h.r, h.c, true, false);
    else if (d.mode === "row") this.selectRect(this.anchor.r, 0, h.r, nc - 1);
    else this.selectRect(0, this.anchor.c, n - 1, h.c);
  }

  private autoscroll(): void {
    const d = this.drag;
    if (!d) return;
    const rect = this.scroll.getBoundingClientRect();
    const edge = 24;
    let dy = 0, dx = 0;
    if (d.y > rect.top + this.scroll.clientHeight - edge) dy = d.y - (rect.top + this.scroll.clientHeight - edge);
    else if (d.y < rect.top + HEAD_H + edge) dy = d.y - (rect.top + HEAD_H + edge);
    if (d.x > rect.left + this.scroll.clientWidth - edge) dx = d.x - (rect.left + this.scroll.clientWidth - edge);
    else if (d.x < rect.left + this.gutW + edge) dx = d.x - (rect.left + this.gutW + edge);
    if (!dx && !dy) return;
    this.scroll.scrollBy(Math.sign(dx) * Math.min(60, Math.abs(dx)), Math.sign(dy) * Math.min(120, Math.abs(dy) * 2));
    this.dragTo();
  }

  private startResize(e: PointerEvent, c: number): void {
    e.preventDefault();
    const x0 = e.clientX, w0 = this.colW[c] ?? 120;
    const move = (ev: PointerEvent) => {
      this.colW[c] = Math.max(MIN_COL, Math.round(w0 + ev.clientX - x0));
      this.layoutCols(); this.ver++; this.hver++;
      this.schedule();
    };
    const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); };
    addEventListener("pointermove", move);
    addEventListener("pointerup", up);
  }

  private onDblClick(e: MouseEvent): void {
    const t = e.target as HTMLElement;
    const rz = t.closest<HTMLElement>(".th-rz");
    if (rz) { void this.fitColumn(Number(rz.dataset.c)); return; }
    if (t.closest(".th-sort")) return;
    const h = this.hitTest(e.clientX, e.clientY);
    if (h.zone === "head") { if (this.editable && this.st.hasHeader) this.editHeader(h.c); return; }
    if (h.zone === "cell" && this.editable) void this.startEdit();
  }

  // ---- inline editing ----------------------------------------------------------------

  async startEdit(initial?: string): Promise<void> {
    if (!this.editable || this.st.nView === 0) return;
    const { r, c } = this.cur;
    let orig: string | null | undefined = this.cellText(r, c);
    if (orig === undefined && initial === undefined) {
      const res = await this.host.client.rows(r, 1);
      orig = res.rows[0]?.[c] ?? "";
    }
    // Typing over a not-yet-loaded cell: open the editor right away (keys must not be lost)
    // and always commit what was typed.
    if (orig === undefined) orig = null;
    this.commitEdit();
    this.ensureVisible(r, c);
    const w = Math.max(this.colW[c] ?? 120, 140);
    const ta = el("textarea.tbl-edit", {
      rows: 1, spellcheck: false, autocomplete: "off", "aria-label": `Edit ${colLetter(c)}${r + 1}`,
      style: `left:${this.gutW + (this.colX[c] ?? 0)}px;top:${this.rowY(r)}px;width:${w}px;height:${ROW_H}px`,
    });
    ta.value = initial ?? orig ?? "";
    const grow = () => { ta.style.height = `${Math.min(220, Math.max(ROW_H, ta.scrollHeight))}px`; };
    ta.addEventListener("input", grow);
    ta.addEventListener("keydown", (ev) => this.onEditKey(ev));
    ta.addEventListener("blur", () => { if (this.editor?.ta === ta) this.commitEdit(false); });
    this.body.append(ta);
    this.editor = { ta, r, c, orig };
    ta.focus();
    const end = ta.value.length;
    if (initial === undefined) ta.setSelectionRange(0, end); else ta.setSelectionRange(end, end);
    grow();
    this.host.selectionChanged();
  }

  private onEditKey(e: KeyboardEvent): void {
    e.stopPropagation(); // keep grid shortcuts out of the editor
    if (e.isComposing) return;
    const ed = this.editor;
    if (!ed) return;
    if (e.key === "Escape") { e.preventDefault(); this.cancelEdit(); return; }
    if (e.key === "Enter") {
      if (e.altKey || e.ctrlKey || e.metaKey) { // newline inside the cell
        e.preventDefault();
        const ta = ed.ta, a = ta.selectionStart, b = ta.selectionEnd;
        ta.setRangeText("\n", a, b, "end");
        ta.dispatchEvent(new Event("input"));
        return;
      }
      e.preventDefault();
      const { r, c } = ed;
      void this.commitEdit(); // closes the editor synchronously; the worker round trip follows
      this.setCursor(r + (e.shiftKey ? -1 : 1), c);
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      const { r, c } = ed;
      const nc = this.st.ncols;
      void this.commitEdit();
      if (e.shiftKey) this.setCursor(c > 0 ? r : r - 1, c > 0 ? c - 1 : nc - 1);
      else this.setCursor(c < nc - 1 ? r : r + 1, c < nc - 1 ? c + 1 : 0);
    }
  }

  /** Close the cell editor, saving the text when it changed. */
  async commitEdit(refocus = true): Promise<void> {
    const ed = this.editor;
    if (!ed) return;
    this.editor = null;
    const text = ed.ta.value;
    ed.ta.remove();
    if (refocus) this.focus();
    if (ed.orig === null || text !== ed.orig) await this.setCells([[ed.r, ed.c, text]], "Edit Cell");
    this.host.selectionChanged();
  }

  cancelEdit(): void {
    const ed = this.editor;
    if (!ed) return;
    this.editor = null;
    ed.ta.remove();
    this.focus();
    this.host.selectionChanged();
  }

  editHeader(c: number): void {
    this.commitEdit();
    const th = this.head.querySelector<HTMLElement>(`.tbl-th[data-c="${c}"]`);
    if (!th) return;
    const input = el("input.th-edit", { type: "text", value: this.st.headers[c] ?? "", "aria-label": "Column name" });
    th.replaceChildren(input);
    this.headEditor = input;
    input.focus(); input.select();
    let done = false;
    const finish = async (save: boolean) => {
      if (done) return;
      done = true;
      this.headEditor = null;
      if (save && input.value !== (this.st.headers[c] ?? "")) {
        const s = await this.host.client.call<TableState>("setHeader", { col: c, text: input.value });
        this.host.stateChanged(s);
      }
      this.headKey = ""; this.hver++; this.schedule();
      this.focus();
    };
    input.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      if (ev.key === "Enter") { ev.preventDefault(); void finish(true); }
      else if (ev.key === "Escape") { ev.preventDefault(); void finish(false); }
    });
    input.addEventListener("blur", () => void finish(true));
  }

  // ---- worker operations ----------------------------------------------------------------

  private adopt(s: TableState, patch?: [number, number, string][]): void {
    this.applyState(s, patch);
    this.host.stateChanged(s);
  }

  async setCells(changes: [number, number, string][], label: string): Promise<void> {
    if (!changes.length) return;
    const s = await this.host.client.call<TableState>("setCells", { changes, label });
    this.adopt(s, changes);
  }

  async clearSelection(): Promise<void> {
    if (!this.editable) return;
    const { r0, c0, r1, c1 } = this.selRect();
    if ((r1 - r0 + 1) * (c1 - c0 + 1) > 20000) {
      const s = await this.host.busy("Clearing…", () => this.host.client.call<TableState>("clear", { r0, c0, r1, c1 }));
      this.adopt(s);
      return;
    }
    const ch: [number, number, string][] = [];
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) ch.push([r, c, ""]);
    await this.setCells(ch, "Clear Cells");
  }

  async insertRows(below: boolean): Promise<void> {
    if (!this.editable) return;
    const { r0, r1 } = this.selRect();
    const count = r1 - r0 + 1;
    const vpos = this.st.nView === 0 ? 0 : below ? r1 + 1 : r0;
    const s = await this.host.client.call<TableState>("insertRows", { vpos, count });
    this.adopt(s);
    this.selectRect(vpos, this.cur.c, vpos + count - 1, this.cur.c);
    this.ensureVisible(vpos, this.cur.c);
  }

  async deleteRows(): Promise<void> {
    if (!this.editable || this.st.nView === 0) return;
    const { r0, r1 } = this.selRect();
    const s = await this.host.client.call<TableState>("deleteRows", { ranges: [[r0, r1]] });
    this.adopt(s);
    this.setCursor(Math.min(r0, s.nView - 1), this.cur.c);
  }

  async insertCols(right: boolean): Promise<void> {
    if (!this.editable) return;
    const { c0, c1 } = this.selRect();
    const count = c1 - c0 + 1;
    const at = right ? c1 + 1 : c0;
    this.colW.splice(at, 0, ...new Array<number>(count).fill(120));
    this.types.splice(at, 0, ...new Array<string>(count).fill("empty"));
    const s = await this.host.client.call<TableState>("insertCols", { at, count });
    this.adopt(s);
    this.selectRect(0, at, s.nView - 1, at + count - 1);
  }

  async deleteCols(): Promise<void> {
    if (!this.editable) return;
    const { c0, c1 } = this.selRect();
    const cols: number[] = [];
    for (let c = c0; c <= c1; c++) cols.push(c);
    const res = await this.host.client.call<{ ok: boolean; state: TableState }>("deleteCols", { cols });
    if (!res.ok) { this.host.toast("A table needs at least one column"); return; }
    this.colW.splice(c0, cols.length);
    this.types.splice(c0, cols.length);
    this.adopt(res.state);
    this.setCursor(this.cur.r, Math.min(c0, res.state.ncols - 1));
  }

  async undo(): Promise<void> {
    if (!this.editable || !this.st.canUndo) return;
    this.adopt(await this.host.client.call<TableState>("undo"));
  }
  async redo(): Promise<void> {
    if (!this.editable || !this.st.canRedo) return;
    this.adopt(await this.host.client.call<TableState>("redo"));
  }

  /** Header click: ascending, then descending, then unsorted. */
  async cycleSort(c: number): Promise<void> {
    if (!this.st.loaded || this.host.isBusy()) return;
    const s = this.st;
    if (s.sortCol !== c) return this.sortBy(c, true);
    if (s.sortAsc) return this.sortBy(c, false);
    return this.sortBy(c, null);
  }

  async sortBy(c: number, asc: boolean | null): Promise<void> {
    if (!this.st.loaded || this.host.isBusy()) return;
    const res = await this.host.busy("Sorting…", () => asc === null
      ? this.host.client.call<{ state: TableState }>("clearSort")
      : this.host.client.call<{ cancelled: boolean; state: TableState }>("sort", { col: c, asc }));
    this.adopt(res.state);
    this.setCursor(0, this.cur.c, false, false);
    this.scroll.scrollTop = 0;
    this.render();
  }

  // ---- clipboard ---------------------------------------------------------------------

  private selectionTextSync(): string | null {
    const { r0, c0, r1, c1 } = this.selRect();
    if ((r1 - r0 + 1) * (c1 - c0 + 1) > 200000 || !this.cached(r0, r1)) return null;
    const q = (s: string) => (/[\t\n\r"]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s);
    const lines: string[] = [];
    for (let r = r0; r <= r1; r++) {
      const row = this.getRow(r) ?? [];
      const cells: string[] = [];
      for (let c = c0; c <= c1; c++) cells.push(q(row[c] ?? ""));
      lines.push(cells.join("\t"));
    }
    return lines.join("\n");
  }

  async copyText(): Promise<string> {
    const { r0, c0, r1, c1 } = this.selRect();
    return this.host.client.call<string>("selectionText", { r0, c0, r1, c1 });
  }

  private onCopy(e: ClipboardEvent, cut: boolean): void {
    if (this.isEditing || (e.target as HTMLElement).closest?.(".tbl-search")) return;
    if (this.st.nView === 0) return;
    e.preventDefault();
    const sync = this.selectionTextSync();
    if (sync !== null) {
      e.clipboardData?.setData("text/plain", sync);
      this.afterCopy(cut);
      return;
    }
    void this.copyText().then(async (t) => {
      try { await navigator.clipboard.writeText(t); } catch { this.host.toast("Couldn't copy: the clipboard is not available"); return; }
      this.afterCopy(cut);
    });
  }

  private afterCopy(cut: boolean): void {
    const { r0, c0, r1, c1 } = this.selRect();
    const n = (r1 - r0 + 1) * (c1 - c0 + 1);
    this.host.toast(`${cut ? "Cut" : "Copied"} ${n.toLocaleString()} cell${n === 1 ? "" : "s"}`, { timeout: 1500 });
    if (cut && this.editable) void this.clearSelection();
  }

  /** Context-menu / toolbar Copy (no clipboard event is fired for those). */
  async copyToClipboard(cut = false): Promise<void> {
    if (this.st.nView === 0) return;
    const t = this.selectionTextSync() ?? await this.copyText();
    try { await navigator.clipboard.writeText(t); } catch { this.host.toast("Couldn't copy: the clipboard is not available"); return; }
    this.afterCopy(cut);
  }

  private onPaste(e: ClipboardEvent): void {
    if (this.isEditing || (e.target as HTMLElement).closest?.(".tbl-search")) return;
    e.preventDefault();
    const text = e.clipboardData?.getData("text/plain") ?? "";
    void this.pasteText(text);
  }

  async pasteFromClipboard(): Promise<void> {
    try { await this.pasteText(await navigator.clipboard.readText()); }
    catch { this.host.toast("Use Ctrl+V to paste"); }
  }

  async pasteText(text: string): Promise<void> {
    if (!this.editable || !text) return;
    let grid = parseTsv(text);
    if (grid.length > 1 && grid[grid.length - 1]!.length === 1 && grid[grid.length - 1]![0] === "") grid.pop();
    const { r0, c0, r1, c1 } = this.selRect();
    const single = grid.length === 1 && grid[0]!.length === 1;
    if (single && (r1 > r0 || c1 > c0)) {
      const v = grid[0]![0]!;
      grid = Array.from({ length: r1 - r0 + 1 }, () => new Array<string>(c1 - c0 + 1).fill(v));
    }
    const wide = grid.reduce((m, r) => Math.max(m, r.length), 0);
    const s = await this.host.busy("Pasting…", () => this.host.client.call<TableState>("paste", { r0, c0, grid }));
    this.adopt(s);
    this.selectRect(r0, c0, r0 + grid.length - 1, c0 + wide - 1);
  }

  // ---- context menu -----------------------------------------------------------------

  private onContextMenu(e: MouseEvent): void {
    e.preventDefault();
    const h = this.hitTest(e.clientX, e.clientY);
    if (h.zone === "none") return;
    this.commitEdit();
    const s = this.selRect();
    const inside = h.r >= s.r0 && h.r <= s.r1 && h.c >= s.c0 && h.c <= s.c1;
    if (h.zone === "head") {
      if (!(inside && s.r0 === 0 && s.r1 >= this.st.nView - 1)) this.selectRect(0, h.c, this.st.nView - 1, h.c);
    } else if (h.zone === "gutter") {
      if (!(inside && s.c0 === 0 && s.c1 >= this.st.ncols - 1)) this.selectRect(h.r, 0, h.r, this.st.ncols - 1);
    } else if (h.zone === "cell" && !inside) this.setCursor(h.r, h.c, false, false);
    this.focus();
    popupMenu({ x: e.clientX, y: e.clientY }, this.menuItems(h.zone === "head" ? h.c : null));
  }

  menuItems(headCol: number | null = null): MenuItem[] {
    const ed = this.editable;
    const items: MenuItem[] = [];
    if (headCol !== null) {
      items.push(
        { label: "Sort Ascending", run: () => void this.sortBy(headCol, true), disabled: !this.st.loaded },
        { label: "Sort Descending", run: () => void this.sortBy(headCol, false), disabled: !this.st.loaded },
        { label: "Clear Sorting", run: () => void this.sortBy(headCol, null), disabled: this.st.sortCol === null },
        { label: "Fit Column Width", run: () => void this.fitColumn(headCol) },
        { label: "Rename Column…", run: () => this.editHeader(headCol), disabled: !ed || !this.st.hasHeader },
        "-",
      );
    }
    items.push(
      { label: "Cut", run: () => void this.copyToClipboard(true), disabled: !ed, accel: "Ctrl+X" },
      { label: "Copy", run: () => void this.copyToClipboard(), accel: "Ctrl+C" },
      { label: "Paste", run: () => void this.pasteFromClipboard(), disabled: !ed, accel: "Ctrl+V" },
      { label: "Clear Contents", run: () => void this.clearSelection(), disabled: !ed, accel: "Del" },
      "-",
      ...this.rowColItems(),
      "-",
      { label: "Select All", run: () => this.selectAll(), accel: "Ctrl+A" },
    );
    return items;
  }

  rowColItems(): MenuItem[] {
    const ed = this.editable;
    return [
      { label: "Insert Row Above", run: () => void this.insertRows(false), disabled: !ed },
      { label: "Insert Row Below", run: () => void this.insertRows(true), disabled: !ed },
      { label: "Delete Row(s)", run: () => void this.deleteRows(), disabled: !ed || this.st.nView === 0 },
      "-",
      { label: "Insert Column Left", run: () => void this.insertCols(false), disabled: !ed },
      { label: "Insert Column Right", run: () => void this.insertCols(true), disabled: !ed },
      { label: "Delete Column(s)", run: () => void this.deleteCols(), disabled: !ed },
    ];
  }

  // ---- misc ---------------------------------------------------------------------------

  /** Statistics for the selection (numeric cells), or null. */
  async stats(): Promise<SelectionStats | null> {
    const { r0, c0, r1, c1 } = this.selRect();
    return this.host.client.call<SelectionStats | null>("selectionStats", { r0, c0, r1, c1 });
  }

  /** Re-fetch everything visible (after reload). */
  reset(s: TableState): void {
    this.blocks.clear();
    this.fetching.clear();
    for (const row of this.rowEls.values()) row.remove();
    this.rowEls.clear();
    this.cur = { r: 0, c: 0 }; this.anchor = { r: 0, c: 0 };
    this.needle = ""; this.curHitRow = -1;
    this.colW = new Array(s.ncols).fill(120);
    this.types = [];
    this.st = s;
    this.layoutCols();
    this.scroll.scrollTo(0, 0);
    this.ver++; this.hver++; this.headKey = "";
    this.schedule();
  }

  destroy(): void {
    this.destroyed = true;
    cancelAnimationFrame(this.raf);
    clearInterval(this.drag?.timer);
    this.el.remove();
  }
}
