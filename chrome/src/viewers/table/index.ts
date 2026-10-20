// Table viewer (CSV / TSV): a virtualized grid over a worker-held document model.
// Port of preview/table_view.py. The heavy lifting (encoding and delimiter detection,
// record index, parsing, sort, search, undo, byte-exact saving) lives in worker.js;
// the grid in grid.ts renders only what is on screen.
import { button, el, loadStyle, humanSize } from "../../ui/kit.ts";
import { showDialog } from "../../ui/dialog.ts";
import type { DocumentSource, ExportFormat, InfoSection, SavedInfo, SaveRequest, SaveResult, Viewer, ViewerContext, ViewerModule } from "../contract.ts";
import { TableClient, type InfoResult, type SearchResult, type SerializeResult, type TableState, type WorkerEvent } from "./client.ts";
import { Grid, colLetter, type GridHost } from "./grid.ts";
import { popupMenu, type MenuItem } from "./menu.ts";

const EOL_NAMES: Record<string, string> = { "\n": "LF (Unix)", "\r\n": "CRLF (Windows)", "\r": "CR (classic Mac)" };
const ENCODINGS: [string, string][] = [
  ["Unicode (UTF-8)", "utf-8"], ["Unicode (UTF-16)", "utf-16le"], ["Western (Windows-1252)", "windows-1252"],
  ["Western (Latin-1)", "iso-8859-1"], ["Chinese (GB18030)", "gb18030"], ["Chinese (Big5)", "big5"],
  ["Japanese (Shift-JIS)", "shift_jis"], ["Korean (EUC-KR)", "euc-kr"],
];
const DELIMS: [string, string][] = [["Comma", ","], ["Tab", "\t"], ["Semicolon", ";"], ["Pipe", "|"]];

const fmtNum = (v: number) => (Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 6 }));

class TableViewer implements Viewer, GridHost {
  client!: TableClient;
  private grid!: Grid;
  private ctx!: ViewerContext;
  private source!: DocumentSource;
  private blob!: Blob;
  private root!: HTMLElement;
  private st!: TableState;
  private busyCount = 0;
  private statusEl = el("div.tbl-status-l");
  private infoEl = el("div.tbl-status-r");
  private busyEl = el("div.tbl-busy", { hidden: true }, el("div.tbl-busy-bar"), el("span.tbl-busy-label"));
  private findBar!: HTMLElement;
  private findInput!: HTMLInputElement;
  private findCount!: HTMLElement;
  private filterBtn!: HTMLButtonElement;
  private findBtn!: HTMLButtonElement;
  private matches: Int32Array<ArrayBufferLike> = new Int32Array(0);
  private matchIdx = -1;
  private findTimer = 0;
  private statusGen = 0;
  private statusTimer = 0;
  private t0 = performance.now();
  private early: TableState | null = null;
  private hasGrid = false;

  // ---- GridHost -------------------------------------------------------------------

  isBusy(): boolean { return this.busyCount > 0; }

  async busy<T>(label: string, fn: () => Promise<T>): Promise<T> {
    this.busyCount++;
    this.root.dataset.busy = label;
    this.setBusy(label, 0);
    try { return await fn(); } finally {
      if (--this.busyCount === 0) { delete this.root.dataset.busy; this.busyEl.hidden = true; }
    }
  }

  private setBusy(label: string, frac: number): void {
    this.busyEl.hidden = false;
    (this.busyEl.querySelector(".tbl-busy-label") as HTMLElement).textContent = `${label}${frac > 0 ? ` ${Math.round(frac * 100)}%` : ""}`;
    (this.busyEl.querySelector(".tbl-busy-bar") as HTMLElement).style.width = frac > 0 ? `${frac * 100}%` : "30%";
    this.busyEl.classList.toggle("indeterminate", frac <= 0);
  }

  stateChanged(s: TableState): void {
    this.st = s;
    this.root.dataset.rows = String(s.nView);
    this.root.dataset.cols = String(s.ncols);
    this.root.dataset.dirty = String(s.dirty);
    this.ctx.setDirty(s.dirty);
    this.ctx.refreshTitle();
    this.updateInfo();
  }

  selectionChanged(): void {
    clearTimeout(this.statusTimer);
    this.statusTimer = window.setTimeout(() => void this.updateStatus(), 50);
  }

  toast(message: string, o?: { timeout?: number }): void { this.ctx.toast(message, o); }

  firstData(): void {
    if (this.root.dataset.state === "ready") return;
    this.root.dataset.state = "ready";
    this.root.dataset.firstPaintMs = String(Math.round(performance.now()));
    this.root.dataset.mountMs = String(Math.round(performance.now() - this.t0));
  }

  // ---- lifecycle ------------------------------------------------------------------

  async mount(container: HTMLElement, source: DocumentSource, ctx: ViewerContext): Promise<void> {
    this.ctx = ctx;
    this.source = source;
    await loadStyle("table");
    this.root = el("div.tbl-root", { "data-viewer": "table", "data-state": "loading" });
    container.append(this.root);
    this.blob = await source.blob();

    this.client = new TableClient();
    this.client.onEvent = (e) => this.onEvent(e);
    ctx.signal.addEventListener("abort", () => this.client.terminate(), { once: true });
    this.st = await this.client.load(this.blob, source.name);
    if (this.early && this.early.epoch >= this.st.epoch) this.st = this.early;

    this.grid = new Grid(this, this.st);
    this.hasGrid = true;
    this.buildFindBar();
    this.root.append(this.findBar, this.grid.el,
      el("div.tbl-status", {}, this.statusEl, this.infoEl), this.busyEl);
    this.buildToolbar();
    this.buildShortcuts();
    this.stateChanged(this.st);
    await this.grid.autoSize();
    this.grid.render();
    this.grid.focus();
    this.root.dataset.loaded = String(this.st.loaded);
    void this.updateStatus();
  }

  private onEvent(e: WorkerEvent): void {
    if (!this.hasGrid) {
      // A small file finishes indexing before load() has answered: keep the newest state.
      if (e.event === "state") this.early = e.state;
      else if (e.event === "error") this.ctx.toast(`Table error: ${e.message}`, { timeout: 8000 });
      return;
    }
    switch (e.event) {
      case "state": {
        const wasLoaded = this.st.loaded;
        this.grid.applyState(e.state);
        this.stateChanged(e.state);
        this.root.dataset.loaded = String(e.state.loaded);
        if (e.state.loaded && !wasLoaded) void this.updateStatus();
        break;
      }
      case "scan": {
        const s = this.grid.st;
        const headers = s.headers.slice();
        while (headers.length < e.ncols) headers.push("");
        const nView = Math.max(0, e.nbase - (s.hasHeader && e.nbase > 0 ? 1 : 0));
        const next = { ...s, nbase: e.nbase, ncols: e.ncols, nView, headers };
        this.grid.applyState(next);
        this.st = next;
        this.root.dataset.rows = String(nView);
        this.root.dataset.scan = e.frac.toFixed(2);
        this.ctx.refreshTitle();
        this.infoEl.textContent = `Indexing… ${Math.round(e.frac * 100)}%`;
        break;
      }
      case "progress":
        if (this.busyCount > 0) this.setBusy(e.op === "sort" ? "Sorting…" : e.op === "search" ? "Searching…" : "Saving…", e.frac);
        break;
      case "error":
        this.ctx.toast(`Table error: ${e.message}`, { timeout: 8000 });
        this.root.dataset.state = "error";
        break;
    }
  }

  getTitle(): string { return this.source.name; }

  getSubtitle(): string {
    const s = this.st;
    if (!s) return "";
    const rows = `${s.nView.toLocaleString()}${s.loaded ? "" : "+"} row${s.nView === 1 ? "" : "s"}`;
    return `${rows} × ${s.ncols.toLocaleString()} column${s.ncols === 1 ? "" : "s"}`;
  }

  isDirty(): boolean { return !!this.st?.dirty; }

  destroy(): void {
    clearTimeout(this.findTimer);
    clearTimeout(this.statusTimer);
    this.client?.terminate();
    this.grid?.destroy();
    this.root?.remove();
  }

  // ---- status bar -------------------------------------------------------------------

  private updateInfo(): void {
    const s = this.st;
    if (!s.loaded && s.scanning) return;
    this.infoEl.textContent = `${s.encLabel} · ${s.delimName} · ${(EOL_NAMES[s.eol] ?? "LF").split(" ")[0]}`;
    this.infoEl.title = "Encoding · delimiter · line endings";
  }

  private async updateStatus(): Promise<void> {
    const gen = ++this.statusGen;
    const g = this.grid;
    if (!g || g.isEditing) return;
    const s = g.st;
    if (!s.nbase) { this.statusEl.textContent = s.loaded ? "Empty file" : ""; return; }
    if (s.nView === 0) { this.statusEl.textContent = "No rows"; return; }
    const { r0, c0, r1, c1 } = g.selection;
    const cnt = (r1 - r0 + 1) * (c1 - c0 + 1);
    if (cnt === 1) {
      let t = g.cellText(r0, c0);
      if (t === undefined) t = (await this.client.rows(r0, 1)).rows[0]?.[c0] ?? "";
      if (gen !== this.statusGen) return;
      const title = s.hasHeader ? s.headers[c0] : "";
      this.statusEl.textContent = `${colLetter(c0)}${r0 + 1}${title ? ` · ${title}` : ""}${t.length > 80 ? ` · ${t.length.toLocaleString()} characters` : ""}`;
      return;
    }
    let text = `${(r1 - r0 + 1).toLocaleString()} × ${(c1 - c0 + 1).toLocaleString()} cells selected`;
    this.statusEl.textContent = text;
    if (cnt <= 40000) {
      const st = await g.stats();
      if (gen !== this.statusGen || !st) return;
      text += `  ·  ${st.n.toLocaleString()} numeric  ·  Sum ${fmtNum(st.sum)}  ·  Avg ${fmtNum(st.sum / st.n)}  ·  Min ${fmtNum(st.min)}  ·  Max ${fmtNum(st.max)}`;
      this.statusEl.textContent = text;
    }
  }

  // ---- toolbar and menus ----------------------------------------------------------------

  private buildToolbar(): void {
    const tools = this.ctx.header.tools;
    this.findBtn = button({ icon: "search", title: "Find (Ctrl+F)", toggle: true, onClick: () => this.toggleFind() });
    // toggle() flips aria-pressed itself; keep it in sync with the bar afterwards
    const menuBtn = (label: string, title: string, items: () => MenuItem[]) => {
      const b = button({ label, title });
      b.setAttribute("aria-haspopup", "menu");
      b.addEventListener("click", () => popupMenu(b, items()));
      return b;
    };
    tools.append(this.findBtn,
      menuBtn("Edit", "Edit", () => this.editItems()),
      menuBtn("Rows & Columns", "Insert and delete rows and columns", () => this.grid.rowColItems()),
      menuBtn("Format", "Delimiter, encoding, header row, line endings", () => this.formatItems()));
  }

  private editItems(): MenuItem[] {
    const g = this.grid, ed = g.editable;
    return [
      { label: g.st.undoLabel ? `Undo ${g.st.undoLabel}` : "Undo", run: () => void g.undo(), disabled: !ed || !g.st.canUndo, accel: "Ctrl+Z" },
      { label: g.st.redoLabel ? `Redo ${g.st.redoLabel}` : "Redo", run: () => void g.redo(), disabled: !ed || !g.st.canRedo, accel: "Ctrl+Shift+Z" },
      "-",
      { label: "Cut", run: () => void g.copyToClipboard(true), disabled: !ed, accel: "Ctrl+X" },
      { label: "Copy", run: () => void g.copyToClipboard(), accel: "Ctrl+C" },
      { label: "Paste", run: () => void g.pasteFromClipboard(), disabled: !ed, accel: "Ctrl+V" },
      { label: "Clear Contents", run: () => void g.clearSelection(), disabled: !ed, accel: "Del" },
      { label: "Select All", run: () => g.selectAll(), accel: "Ctrl+A" },
      "-",
      { label: "Find…", run: () => this.openFind(), accel: "Ctrl+F" },
    ];
  }

  private formatItems(): MenuItem[] {
    const s = this.grid.st, ed = this.grid.editable;
    const items: MenuItem[] = [
      { label: "First Row Is Header", checked: s.hasHeader, disabled: !ed, run: () => void this.setHeader(!s.hasHeader) },
      { label: "Quote All Fields When Saving", checked: s.outQuoting === "all", disabled: !ed, run: () => void this.setOutput({ quoting: s.outQuoting === "all" ? "minimal" : "all" }) },
      "-", { label: "Delimiter", heading: true },
      ...DELIMS.map(([label, v]): MenuItem => ({ label, checked: s.delim === v, disabled: !s.loaded, run: () => void this.reload({ delimiter: v }) })),
      "-", { label: "Reopen With Encoding", heading: true },
      ...ENCODINGS.map(([label, v]): MenuItem => ({ label, checked: s.encoding === v, disabled: !s.loaded, run: () => void this.reload({ encoding: v }) })),
      "-", { label: "Line Endings When Saving", heading: true },
      ...([["Unix (LF)", "\n"], ["Windows (CRLF)", "\r\n"]] as const).map(([label, v]): MenuItem =>
        ({ label, checked: s.outEol === v, disabled: !ed, run: () => void this.setOutput({ eol: v }) })),
      "-",
      { label: "Clear Sorting", disabled: s.sortCol === null, run: () => void this.grid.sortBy(0, null) },
      { label: "Make Sort Permanent", disabled: !ed || s.sortCol === null, run: () => void this.sortPermanent() },
    ];
    return items;
  }

  private async setHeader(flag: boolean): Promise<void> {
    await this.grid.commitEdit();
    const s = await this.client.call<TableState>("setHasHeader", { flag });
    this.grid.applyState(s); this.stateChanged(s);
    this.grid.setCursor(0, 0);
  }

  private async setOutput(o: { eol?: string; quoting?: string }): Promise<void> {
    const s = await this.client.call<TableState>("setOutput", o);
    this.grid.applyState(s); this.stateChanged(s);
  }

  private async sortPermanent(): Promise<void> {
    const s = await this.client.call<TableState>("sortPermanent");
    this.grid.applyState(s); this.stateChanged(s);
  }

  /** Re-parse the file with a forced delimiter or encoding. */
  private async reload(o: { encoding?: string; delimiter?: string; header?: boolean }): Promise<void> {
    await this.grid.commitEdit();
    if (this.st.dirty) {
      const ok = await showDialog({
        title: "Discard changes?", body: el("p", { text: "Reopening the file with different settings discards your unsaved edits." }),
        actions: [{ label: "Cancel", value: "no" }, { label: "Discard and Reopen", value: "yes", destructive: true }],
      });
      if (ok !== "yes") return;
    }
    const s = await this.busy("Reopening…", () => this.client.load(this.blob, this.source.name, o));
    this.closeFind();
    this.grid.reset(s);
    this.stateChanged(s);
    await this.grid.autoSize();
    this.grid.render();
    this.grid.focus();
  }

  // ---- shortcuts -----------------------------------------------------------------------

  private buildShortcuts(): void {
    const sc = this.ctx.shortcuts;
    const inText = () => {
      const a = document.activeElement as HTMLElement | null;
      return !!a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA");
    };
    sc.add({ keys: "Mod+F", description: "Find", run: () => this.openFind() });
    sc.add({ keys: "Mod+G", description: "Find next", run: () => void this.step(1) });
    sc.add({ keys: "Mod+Shift+G", description: "Find previous", run: () => void this.step(-1) });
    sc.add({ keys: "F3", description: "Find next", run: () => void this.step(1) });
    sc.add({ keys: "Shift+F3", description: "Find previous", run: () => void this.step(-1) });
    sc.add({ keys: "Mod+Z", description: "Undo", run: () => { if (inText()) return false; void this.grid.undo(); } });
    sc.add({ keys: "Mod+Shift+Z", description: "Redo", run: () => { if (inText()) return false; void this.grid.redo(); } });
    sc.add({ keys: "Mod+Y", description: "Redo", run: () => { if (inText()) return false; void this.grid.redo(); } });
    sc.add({ keys: "Mod+A", description: "Select all cells", run: () => { if (inText()) return false; this.grid.selectAll(); } });
  }

  // ---- find --------------------------------------------------------------------------

  private buildFindBar(): void {
    this.findInput = el("input.tbl-find-input", { type: "search", placeholder: "Find in table", "aria-label": "Find in table", spellcheck: false });
    this.findCount = el("span.tbl-find-count", { "aria-live": "polite" });
    this.filterBtn = button({ label: "Only matching rows", title: "Show only rows that match", toggle: true, onClick: () => void this.toggleFilter() });
    this.findBar = el("div.tbl-find", { hidden: true, role: "search" },
      this.findInput, this.findCount,
      button({ label: "↑", title: "Previous match (Shift+Enter)", onClick: () => void this.step(-1) }),
      button({ label: "↓", title: "Next match (Enter)", onClick: () => void this.step(1) }),
      this.filterBtn,
      button({ icon: "close", title: "Close (Esc)", onClick: () => this.closeFind() }));
    this.findInput.addEventListener("input", () => {
      clearTimeout(this.findTimer);
      this.findTimer = window.setTimeout(() => void this.runSearch(), 250);
    });
    this.findInput.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); this.closeFind(); }
      else if (e.key === "Enter") {
        e.preventDefault();
        void (async () => {
          if (this.findTimer) { clearTimeout(this.findTimer); this.findTimer = 0; await this.runSearch(); }
          else await this.step(e.shiftKey ? -1 : 1);
        })();
      }
    });
  }

  private toggleFind(): void { if (this.findBar.hidden) this.openFind(); else this.closeFind(); }

  private openFind(): void {
    this.findBar.hidden = false;
    this.findBtn.setAttribute("aria-pressed", "true");
    const { r0, c0, r1, c1 } = this.grid.selection;
    const t = r0 === r1 && c0 === c1 ? this.grid.cellText(r0, c0) : undefined;
    if (t && t.length < 60 && !t.includes("\n") && !this.findInput.value) this.findInput.value = t;
    this.findInput.focus();
    this.findInput.select();
    if (this.findInput.value && !this.st.search) void this.runSearch();
  }

  private closeFind(): void {
    this.findBar.hidden = true;
    this.findBtn.setAttribute("aria-pressed", "false");
    this.grid.setHighlight("");
    this.grid.focus();
  }

  private updateFindLabel(): void {
    const n = this.matches.length;
    this.findCount.textContent = !this.findInput.value ? "" : n === 0 ? "No matches" : `${this.matchIdx + 1 > 0 ? this.matchIdx + 1 : "–"} of ${n.toLocaleString()}`;
    this.root.dataset.matches = String(n);
  }

  private async runSearch(): Promise<void> {
    this.findTimer = 0;
    const text = this.findInput.value;
    const res = await this.busy("Searching…", () => this.client.call<SearchResult>("search", { text }));
    if (res.cancelled) return;
    this.grid.applyState(res.state); this.stateChanged(res.state);
    this.matches = res.vpos;
    this.matchIdx = -1;
    this.grid.setHighlight(text);
    if (res.count) {
      // first match at or after the cursor
      const r = this.grid.cur.r;
      let lo = 0, hi = res.count;
      while (lo < hi) { const m = (lo + hi) >> 1; if (this.matches[m]! < r) lo = m + 1; else hi = m; }
      await this.gotoMatch(lo < res.count ? lo : 0);
    } else this.updateFindLabel();
  }

  private async step(dir: number): Promise<void> {
    const n = this.matches.length;
    if (!n) {
      if (this.findInput.value) return;
      return this.openFind();
    }
    await this.gotoMatch((this.matchIdx + dir + n) % n);
  }

  private async gotoMatch(i: number): Promise<void> {
    this.matchIdx = i;
    const r = this.matches[i]!;
    const needle = this.findInput.value.toLowerCase();
    const row = (await this.client.rows(r, 1)).rows[0] ?? [];
    let c = row.findIndex((t) => t.toLowerCase().includes(needle));
    if (c < 0) c = this.grid.cur.c;
    this.grid.setHighlight(needle, r);
    this.grid.setCursor(r, c);
    this.updateFindLabel();
  }

  private async toggleFilter(): Promise<void> {
    const on = this.filterBtn.getAttribute("aria-pressed") === "true";
    if (on && !this.matches.length && !this.findInput.value) return;
    const res = await this.busy("Filtering…", () => this.client.call<{ cancelled: boolean; vpos: Int32Array; state: TableState }>("filter", { on }));
    if (res.cancelled) return;
    this.matches = res.vpos;
    this.matchIdx = -1;
    this.grid.applyState(res.state); this.stateChanged(res.state);
    this.grid.setCursor(0, this.grid.cur.c, false, false);
    if (res.vpos.length) await this.gotoMatch(0); else this.updateFindLabel();
  }

  // ---- save / export / info ----------------------------------------------------------------

  exportFormats(): ExportFormat[] {
    return [{ id: "csv", label: "CSV (comma-separated)…" }, { id: "tsv", label: "TSV (tab-separated)…" }, { id: "json", label: "JSON…" }];
  }

  async save(req: SaveRequest): Promise<SaveResult | null> {
    if (!this.st.loaded) { this.ctx.toast("The file is still loading"); return null; }
    await this.grid.commitEdit();
    const base = this.source.name.replace(/\.[^./\\]+$/, "") || "table";
    const ext = (this.source.name.match(/\.([^./\\]+)$/) ?? [])[1] ?? (this.st.delim === "\t" ? "tsv" : "csv");
    let fmt = "csv", delim: string | undefined, name = this.source.name || `${base}.${ext}`;
    const exporting = req.mode === "export";
    if (exporting) {
      fmt = req.format ?? "csv";
      if (fmt === "csv") delim = ",";
      name = `${base}.${fmt}`;
    }
    const out = await this.busy("Saving…", () => this.client.call<SerializeResult>("serialize", { fmt, delim }));
    if (out.note) this.ctx.toast(out.note, { timeout: 7000 });
    // Dirty state is cleared in onSaved(), once the shell has really written the file.
    const types: FilePickerAcceptType[] = fmt === "json"
      ? [{ description: "JSON", accept: { "application/json": [".json"] } }]
      : fmt === "tsv" || (!exporting && this.st.delim === "\t")
        ? [{ description: "Tab-separated values", accept: { "text/tab-separated-values": [".tsv", ".tab"] } }]
        : [{ description: "Comma-separated values", accept: { "text/csv": [".csv"] } }];
    return { blob: out.blob, suggestedName: name, types, replacesOriginal: !exporting };
  }

  async onSaved(info: SavedInfo): Promise<void> {
    if (!info.replacesOriginal) return; // exports don't change what "saved" means
    const s = await this.client.call<TableState>("markSaved", { pending: true });
    this.grid.applyState(s); this.stateChanged(s);
  }

  async getInfo(): Promise<InfoSection[]> {
    const s = this.st;
    const rows: InfoSection["rows"] = [
      ["Rows", `${s.nView.toLocaleString()}${s.loaded ? "" : "…"}`],
      ["Columns", s.ncols.toLocaleString()],
      ["Delimiter", s.delimName],
      ["Encoding", s.encLabel],
      ["First row is header", s.hasHeader ? "Yes" : "No"],
      ["Line endings", EOL_NAMES[s.eol] ?? "LF (Unix)"],
      ["Quoting", s.quoting === "all" ? "All fields" : "When needed"],
    ];
    if (s.dirty) rows.push(["Status", "Edited (unsaved)"]);
    const sections: InfoSection[] = [{ title: "Table", rows }];
    if (s.loaded) {
      const info = await this.client.call<InfoResult>("info");
      const cols: InfoSection["rows"] = [];
      for (let c = 0; c < Math.min(s.ncols, 60); c++) {
        const e = info.empties ? `${(info.empties[c] ?? 0).toLocaleString()} empty` : "…";
        cols.push([(s.hasHeader && s.headers[c]) || colLetter(c), `${info.types[c] ?? "text"} · ${e}`]);
      }
      if (s.ncols > 60) cols.push(["…", `${(s.ncols - 60).toLocaleString()} more columns`]);
      sections.push({ title: "Columns", rows: cols });
    }
    rows.push(["Size", humanSize(this.blob.size)]);
    return sections;
  }
}

const mod: ViewerModule = { kind: "table", create: () => new TableViewer() };
export default mod;
