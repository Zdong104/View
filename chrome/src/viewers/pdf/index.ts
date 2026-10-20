// PDF viewer (port of preview/pdf_view.py). Owns the page model (model.ts) of
// pages from one or more source PDFs (sources.ts), the virtualized page canvas
// (canvas.ts), the thumbnails sidebar (thumbs.ts), Find (search.ts), the save
// pipeline (build.ts, reduce.ts), print/export (output.ts), Live Text OCR via
// ctx.services.ocr() and the markup layer via ctx.services.markup().
import * as pdfjs from "pdfjs-dist";
import type { Annotation, MarkupController, MarkupSurface, WritePdfAnnotations } from "../../markup/api.ts";
import type { OcrService } from "../../ocr/api.ts";
import { kindFromMime, kindFromName } from "../../shared/kinds.ts";
import { button, el, linked, loadStyle } from "../../ui/kit.ts";
import type {
  DocumentSource, ExportFormat, InfoSection, SavedInfo, SaveRequest, SaveResult, Viewer, ViewerContext, ViewerModule,
} from "../contract.ts";
import type { BuildSource } from "./build.ts";
import type { JobResult, SaveJob } from "./job.ts";
import { runJob } from "./saver.ts";
import { PageCanvas, type CanvasDelegate, type LayoutMode, type Slot } from "./canvas.ts";
import { askEncrypt, askExportImages, askPassword, popupMenu, type MenuItem } from "./dialogs.ts";
import {
  defaultName, displaySize, displayToSurface, pageFromHash, pageRanges, pageSizeLabel, quadsToDisplayRect,
  stemOf, surfaceToDisplay,
} from "./geometry.ts";
import { PageModel, pageKey, pageRef, surfaceOf, type PageRef } from "./model.ts";
import { exportImages, printPages, renderPage, type MarkupPainter } from "./output.ts";
import {
  FindBar, extractPageText, findInPage, normalizeQuery, pageTextFromRuns, runsFromOcr,
  type PageText, type SurfaceLine,
} from "./search.ts";
import { infoOf, openSource, PasswordCancelled, type OpenInput, type PdfSource } from "./sources.ts";
import { Thumbnails } from "./thumbs.ts";
import { SidePanels, type AnnotEntry, type TocEntry } from "./panels.ts";

type ViewerLib = typeof import("pdfjs-dist/web/pdf_viewer.mjs");

const asset = (p: string) => chrome.runtime.getURL(`vendor/pdfjs/${p}`);
const PDF_TYPES: FilePickerAcceptType[] = [{ description: "PDF document", accept: { "application/pdf": [".pdf"] } }];
const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
const MOD = isMac ? "⌘" : "Ctrl+";

let viewerLib: Promise<ViewerLib> | undefined;
function loadViewerLib(): Promise<ViewerLib> {
  // pdf_viewer.mjs reads the core library from globalThis.pdfjsLib at evaluation time.
  (globalThis as { pdfjsLib?: unknown }).pdfjsLib = pdfjs;
  viewerLib ??= import("pdfjs-dist/web/pdf_viewer.mjs");
  return viewerLib;
}

interface OutlineItem { title: string; depth: number; index: number | null; dest: unknown[] | null; url?: string; bold?: boolean; italic?: boolean }

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

class PdfViewer implements Viewer {
  private ctx!: ViewerContext;
  private src!: DocumentSource;
  private lib!: ViewerLib;
  private root!: HTMLDivElement;
  private canvas!: PageCanvas;
  private thumbs!: Thumbnails;
  private panels!: SidePanels;
  private outline: Promise<OutlineItem[]> | null = null;
  private pdfAnnots = new Map<string, Promise<{ subtype: string; text: string; rect: number[] }[]>>();
  private find!: FindBar;
  private model!: PageModel;
  private sources: PdfSource[] = [];
  private layerCache = new Map<number, object>();
  private texts = new Map<string, Promise<PageText>>();
  private ocrLines = new Map<string, SurfaceLine[]>();
  private ocrResults = new Map<string, { result: import("../../ocr/api.ts").OcrResult; rot: number }>();
  private ocrStarted = new Set<string>();
  private search: { query: string; gen: number; hits: Map<number, number[][]>; cur: [number, number] | null; count: number; done: boolean } | null = null;
  private mk: MarkupController | null = null;
  private mkBar!: HTMLDivElement;
  private mkReplay = false;
  /** pdf.js ids of the opened PDF's annotations now owned by the markup layer. */
  private importedIds: string[] = [];
  private surfaces = new Map<Slot, MarkupSurface>();
  private pageEntry!: HTMLInputElement;
  private pageTotal!: HTMLSpanElement;
  private zoomLabel!: HTMLButtonElement;
  private status!: HTMLDivElement;
  private busyCount = 0;
  /** The replacesOriginal result handed to the shell, and the state it captured. */
  private pending: { result: SaveResult; key: string } | null = null;
  /** The document was written to a file since it was opened (the original bytes are stale). */
  private savedOnce = false;
  private mkVersion = 0;
  private sizeFrame = 0;
  private destroyed = false;

  // ---- lifecycle --------------------------------------------------------------------

  async mount(container: HTMLElement, source: DocumentSource, ctx: ViewerContext): Promise<void> {
    this.ctx = ctx;
    this.src = source;
    const [lib] = await Promise.all([loadViewerLib(), loadStyle("pdf")]);
    this.lib = lib;
    const input: OpenInput = source.streamable && source.url ? { url: source.url } : { blob: await source.blob() };
    let first: PdfSource;
    try {
      first = await openSource(0, input, source.name, askPassword, ctx.signal);
    } catch (e) {
      if (e instanceof PasswordCancelled) throw new Error(`“${source.name}” is protected with a password`);
      throw e;
    }
    if (ctx.signal.aborted) { void first.destroy(); return; }
    this.sources = [first];
    (first.doc.annotationStorage as { onSetModified: (() => void) | null }).onSetModified = () => {
      first.formsEdited = true;
      this.thumbs?.invalidate();
      this.refreshDirty();
    };
    this.model = new PageModel(Array.from({ length: first.doc.numPages }, (_, i) => pageRef(0, i)));

    this.root = el("div.pdf-root", { "data-viewer": "pdf", "data-pages": first.doc.numPages });
    this.mkBar = el("div.pdf-markupbar", { hidden: true });
    this.find = new FindBar({
      search: (q) => void this.runSearch(q),
      step: (d) => this.searchStep(d),
      close: () => this.closeFind(),
    }, ctx.signal);
    this.status = el("div.pdf-status", { hidden: true, role: "status", "aria-live": "polite" });
    const stage = el("div.pdf-stage");
    this.root.append(this.mkBar, this.find.el, stage, this.status);
    container.append(this.root);
    this.canvas = new PageCanvas(stage, lib, this.canvasDelegate(), ctx.signal);
    this.thumbs = new Thumbnails({
      get pages() { return self.model.pages; },
      get current() { return self.canvas.current; },
      source: (s) => this.sources[s]!,
      idle: () => this.canvas.idle,
      goto: (pos) => this.canvas.goto(pos),
      move: (ids, dest) => this.movePages(ids, dest),
      contextMenu: (x, y) => popupMenu({ x, y }, this.pageMenu()),
      selectionChanged: () => { this.refreshTitle(); this.scheduleDragOut(); },
      dragOut: (ids) => this.dragOut(ids),
      paintMarkup: (c, ref, s) => this.paintMarkup(c, ref, s),
    }, ctx.signal);
    const self = this;
    this.panels = new SidePanels(this.thumbs.root, {
      shown: (name) => {
        if (name === "thumbs") this.thumbs.schedule();
        else if (name === "toc") void this.refreshToc();
        else void this.refreshAnnots();
      },
      gotoToc: (e) => {
        if (e.url) window.open(e.url, "_blank", "noopener");
        else if (e.pos !== null) this.canvas.goto(e.pos, { y: e.y });
      },
      gotoAnnot: (e) => {
        this.canvas.goto(e.pos, { y: e.y });
        if (e.id && this.mk) this.mk.select([e.id]);
      },
    });
    ctx.sidebar.el.append(this.panels.root);
    this.buildHeader();
    this.registerShortcuts();
    this.canvas.relayout();
    this.thumbs.relayout();
    if (this.model.pages.length > 1 && innerWidth >= 720) ctx.sidebar.setVisible(true);
    this.thumbs.schedule();
    const start = pageFromHash(ctx.hash);
    if (start) requestAnimationFrame(() => this.canvas.goto(start - 1));
    else if (ctx.hash.length > 1) requestAnimationFrame(() => this.layerProps(0) && (this.linkService(0)).setHash(ctx.hash.slice(1)));
    this.canvas.scroll.focus({ preventScroll: true });
  }

  destroy(): void {
    this.destroyed = true;
    clearTimeout(this.dragOutTimer);
    if (this.dragOutCache) URL.revokeObjectURL(this.dragOutCache.url);
    for (const s of this.surfaces.values()) s.detach();
    this.surfaces.clear();
    this.mk?.destroy();
    for (const s of this.sources) void s.destroy();
    this.root?.remove();
  }

  // ---- canvas delegate ------------------------------------------------------------

  private canvasDelegate(): CanvasDelegate {
    const v = this;
    return {
      get pages() { return v.model.pages; },
      source: (s) => v.sources[s]!,
      layerProps: (s) => v.layerProps(s),
      imageResourcesPath: asset("images/"),
      onCurrentChanged: () => { v.syncPageEntry(); v.thumbs?.refreshMarks(); v.panels?.markCurrent(v.canvas.current); v.refreshTitle(); },
      onZoomChanged: () => v.syncZoomLabel(),
      onFirstRender: () => {
        v.root.dataset.rendered = "true";
        void v.prefetchSizes();
      },
      onPageInfo: () => {
        if (v.sizeFrame) return;
        v.sizeFrame = requestAnimationFrame(() => {
          v.sizeFrame = 0;
          v.canvas.refresh();
          v.thumbs.relayout();
        });
      },
      onSlotAttached: (slot) => {
        v.paintHighlights(slot);
        v.paintOcr(slot);
        v.attachMarkup(slot);
      },
      onSlotDetached: (slot) => {
        v.surfaces.get(slot)?.detach();
        v.surfaces.delete(slot);
        (slot.data.ocrLayer as { destroy(): void } | undefined)?.destroy();
      },
      onSlotRelayout: (slot) => {
        v.surfaces.get(slot)?.relayout();
        if (!slot.view) return;
        v.paintHighlights(slot);
        if (slot.data.ocrRot !== slot.rotation) { slot.data.ocrRot = slot.rotation; v.paintOcr(slot); }
      },
      onSlotPointerDown: (slot) => void v.autoOcr(slot),
      onDprChanged: () => { v.thumbs.relayout(); v.thumbs.invalidate(); },
    };
  }

  private linkServices = new Map<number, InstanceType<ViewerLib["PDFLinkService"]>>();
  private linkService(s: number): InstanceType<ViewerLib["PDFLinkService"]> {
    return this.linkServices.get(s)!;
  }

  private layerProps(s: number): object {
    let p = this.layerCache.get(s);
    if (p) return p;
    const src = this.sources[s]!;
    const lib = this.lib;
    const ls = new lib.PDFLinkService({ eventBus: new lib.EventBus(), externalLinkTarget: lib.LinkTarget.BLANK });
    const v = this;
    // Our canvas stands in for pdf.js's PDFViewer: page numbers are relative to this source.
    const go = (pageNumber: number, dest?: unknown[]) => {
      const pos = v.model.pages.findIndex((r) => r.src === s && r.index === pageNumber - 1);
      if (pos < 0) { v.ctx.toast("That page is not in the document any more"); return; }
      v.canvas.goto(pos, { y: v.destY(v.model.pages[pos]!, dest) });
    };
    ls.setViewer({
      get pagesCount() { return src.doc.numPages; },
      get currentPageNumber() { return (v.model.pages[v.canvas.current]?.index ?? 0) + 1; },
      set currentPageNumber(n: number) { go(n); },
      scrollPageIntoView({ pageNumber, destArray }: { pageNumber: number; destArray?: unknown[] }) { go(pageNumber, destArray); },
      pagesRotation: 0,
      isInPresentationMode: false,
      currentScaleValue: "auto",
    });
    ls.setDocument(src.doc, null);
    this.linkServices.set(s, ls);
    p = {
      annotationEditorUIManager: null,
      annotationStorage: src.doc.annotationStorage,
      downloadManager: null,
      enableScripting: false,
      fieldObjectsPromise: src.doc.getFieldObjects(),
      findController: null,
      hasJSActionsPromise: null,
      linkService: ls,
    };
    this.layerCache.set(s, p);
    return p;
  }

  /** Fetch every page size in the background so the layout stops shifting (like pdf.js). */
  private async prefetchSizes(): Promise<void> {
    for (const src of this.sources) {
      if (src.doc.numPages > 7500) continue;
      let changed = false;
      for (let i = 0; i < src.doc.numPages; i++) {
        if (this.destroyed) return;
        if (src.infos[i]) continue;
        const info = infoOf(await src.doc.getPage(i + 1));
        src.infos[i] = info;
        if (info.w !== src.fallback.w || info.h !== src.fallback.h || info.rotate !== src.fallback.rotate) changed = true;
      }
      if (changed) { this.canvas.refresh(); this.thumbs.relayout(); }
    }
  }

  // ---- title, header, status --------------------------------------------------------

  private refreshTitle(): void {
    this.ctx.refreshTitle();
  }

  getTitle(): string { return this.src?.name ?? ""; }

  getSubtitle(): string {
    const n = this.model?.pages.length ?? 0;
    return n ? `Page ${(this.canvas?.current ?? 0) + 1} of ${n}` : "";
  }

  isDirty(): boolean {
    if (!this.model) return false;
    return this.model.dirty || this.sources.some((s) => s.formsEdited) || !!this.mk?.dirty;
  }

  /**
   * Shell hook, called only after the bytes of a save() result were written.
   * The document is clean again if nothing changed since that save() started.
   */
  onSaved(info: SavedInfo): void {
    const p = this.pending;
    if (!p || info.result !== p.result) return;
    this.pending = null;
    if (!info.replacesOriginal) return;
    this.savedOnce = true;
    if (p.key === this.stateKey()) this.markSaved();
    this.refreshDirty();
  }

  /** Page model + forms + markup state, to tell whether edits happened while saving. */
  private stateKey(): string {
    return `${this.model.key()}|${this.sources.map((s) => (s.formsEdited ? 1 : 0)).join("")}|${this.mk?.toJSON().length ?? 0}:${this.mkVersion}`;
  }

  private markSaved(): void {
    this.model.markClean();
    for (const s of this.sources) { s.formsEdited = false; s.doc.annotationStorage.resetModified(); }
    this.mk?.markSaved();
  }

  private buildHeader(): void {
    const { start, tools } = this.ctx.header;
    this.pageEntry = el("input.pdf-page-entry", { type: "text", inputmode: "numeric", "aria-label": "Page number", title: "Go to Page (Ctrl+G)" });
    this.pageTotal = el("span.pdf-page-total");
    this.pageEntry.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const n = parseInt(this.pageEntry.value, 10);
        if (n > 0) this.canvas.goto(n - 1);
        this.canvas.scroll.focus({ preventScroll: true });
      } else if (e.key === "Escape") { this.syncPageEntry(); this.canvas.scroll.focus({ preventScroll: true }); }
    }, { signal: this.ctx.signal });
    this.pageEntry.addEventListener("blur", () => this.syncPageEntry(), { signal: this.ctx.signal });
    start.append(el("div.pdf-page-box", {}, this.pageEntry, this.pageTotal));

    const search = button({ icon: "search", title: "Find (Ctrl+F)", onClick: () => (this.find.open ? this.closeFind() : this.openFind()) });
    this.zoomLabel = button({ label: "100%", title: "Zoom", onClick: () => popupMenu(this.zoomLabel, [
      ["Fit Width", () => this.canvas.setMode("fit-width"), `${MOD}0`],
      ["Fit Page", () => this.canvas.setMode("fit-page"), `${MOD}9`],
      ["Actual Size", () => this.canvas.zoomTo(1), `${MOD}1`],
      "-",
      ...([50, 75, 100, 125, 150, 200, 300, 400] as const).map((p) => [`${p}%`, () => this.canvas.zoomTo(p / 100)] as const),
    ]) });
    this.zoomLabel.classList.add("pdf-zoom-label");
    const markupBtn = button({ icon: "markup", title: "Markup Toolbar (Ctrl+Shift+A)", toggle: true, onClick: () => void this.toggleMarkup() });
    markupBtn.dataset.role = "markup-toggle";
    const docMenu = button({ icon: "document", title: "Document Menu", onClick: () => popupMenu(docMenu, this.documentMenu()) });
    docMenu.dataset.role = "pdf-menu";
    tools.append(
      search,
      linked(
        button({ icon: "zoomOut", title: `Zoom Out (${MOD}−)`, onClick: () => this.canvas.zoomBy(0.8) }),
        this.zoomLabel,
        button({ icon: "zoomIn", title: `Zoom In (${MOD}+)`, onClick: () => this.canvas.zoomBy(1.25) }),
      ),
      markupBtn,
      docMenu,
    );
    this.syncPageEntry();
    this.syncZoomLabel();
  }

  private syncPageEntry(): void {
    if (!this.pageEntry) return;
    if (document.activeElement !== this.pageEntry) this.pageEntry.value = String(this.canvas.current + 1);
    this.pageTotal.textContent = `of ${this.model.pages.length}`;
    this.root.dataset.pages = String(this.model.pages.length);
    this.root.dataset.current = String(this.canvas.current + 1);
  }

  private syncZoomLabel(): void {
    if (this.zoomLabel) (this.zoomLabel.querySelector(".btn-label") as HTMLElement).textContent = `${Math.round(this.canvas.scale * 100)}%`;
    this.root.dataset.zoom = String(Math.round(this.canvas.scale * 1000) / 1000);
  }

  /** Show a progress pill while `work` runs. */
  private async busy<T>(label: string, work: (set: (msg: string) => void) => Promise<T>): Promise<T> {
    this.busyCount++;
    this.status.hidden = false;
    this.status.textContent = label;
    this.root.dataset.busy = "true";
    try {
      return await work((m) => { this.status.textContent = m; });
    } finally {
      if (--this.busyCount === 0) { this.status.hidden = true; delete this.root.dataset.busy; }
    }
  }

  private documentMenu(): MenuItem[] {
    const sel = this.thumbs.explicitSelection().length;
    const hasOcr = true;
    return [
      ["Save Selected Pages as New PDF…", () => void this.ctx.requestSave({ mode: "save", selectionOnly: true }), undefined, !sel],
      ["Merge PDFs into This Document…", () => void this.pickAndInsert(this.model.pages.length, true)],
      ["Print…", () => void this.print(), `${MOD}P`],
      "-",
      ["Export as Images…", () => void this.ctx.requestSave({ mode: "export", format: "images" })],
      ["Export with Password…", () => void this.ctx.requestSave({ mode: "export", format: "encrypted" })],
      ["Export as Searchable PDF (OCR)…", () => void this.ctx.requestSave({ mode: "export", format: "searchable" }), undefined, !hasOcr],
      ["Reduce File Size…", () => void this.ctx.requestSave({ mode: "export", format: "reduced" })],
      "-",
      ...this.pageMenu(),
      ["Recognize Text (OCR)", () => void this.recognize(this.targets())],
      "-",
      ...(["continuous", "two", "single"] as LayoutMode[]).map((m) => [
        `${this.canvas.layout === m ? "✓ " : ""}${m === "continuous" ? "Continuous Scroll" : m === "two" ? "Two Pages" : "Single Page"}`,
        () => this.canvas.setLayout(m)] as const),
      "-",
      ["Find…", () => this.openFind(), `${MOD}F`],
      ["Go to Page…", () => this.focusPageEntry(), `${MOD}G`],
      ["Undo", () => this.undo(), `${MOD}Z`, !this.model.canUndo],
      ["Redo", () => this.redo(), `${MOD}Shift+Z`, !this.model.canRedo],
    ];
  }

  private pageMenu(): MenuItem[] {
    const sel = this.thumbs.explicitSelection().length;
    return [
      ["Rotate Left", () => this.rotate(-1), `${MOD}[`],
      ["Rotate Right", () => this.rotate(1), `${MOD}]`],
      ["Delete Selected Pages", () => this.deletePages(this.targets()), "Delete"],
      ["Save Selected Pages as New PDF…", () => void this.ctx.requestSave({ mode: "save", selectionOnly: true }), undefined, !sel],
      ["Insert Blank Page", () => void this.insertBlank()],
      ["Insert Pages from File…", () => void this.pickAndInsert(this.canvas.current + 1, false)],
    ];
  }

  private registerShortcuts(): void {
    const sc = this.ctx.shortcuts;
    const editable = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    };
    const c = this.canvas;
    sc.add({ keys: "Mod+F", description: "Find", run: () => this.openFind() });
    sc.add({ keys: "Mod+G", description: "Go to page", run: () => this.focusPageEntry() });
    sc.add({ keys: "Mod+L", description: "Go to page", run: () => this.focusPageEntry() });
    sc.add({ keys: "Mod+=", description: "Zoom in", run: () => c.zoomBy(1.25) });
    sc.add({ keys: "Mod++", description: "Zoom in", run: () => c.zoomBy(1.25) });
    sc.add({ keys: "Mod+-", description: "Zoom out", run: () => c.zoomBy(0.8) });
    sc.add({ keys: "Mod+0", description: "Fit width", run: () => c.setMode("fit-width") });
    sc.add({ keys: "Mod+9", description: "Fit page", run: () => c.setMode("fit-page") });
    sc.add({ keys: "Mod+1", description: "Actual size", run: () => c.zoomTo(1) });
    sc.add({ keys: "Mod+[", description: "Rotate left", run: () => this.rotate(-1) });
    sc.add({ keys: "Mod+]", description: "Rotate right", run: () => this.rotate(1) });
    sc.add({ keys: "Mod+Z", description: "Undo", run: (e) => (editable(e) ? false : this.undo()) });
    sc.add({ keys: "Mod+Shift+Z", description: "Redo", run: (e) => (editable(e) ? false : this.redo()) });
    sc.add({ keys: "Mod+Y", description: "Redo", run: (e) => (editable(e) ? false : this.redo()) });
    sc.add({ keys: "Mod+Shift+A", description: "Markup toolbar", run: () => void this.toggleMarkup() });
    sc.add({ keys: "PageDown", description: "Next page", run: (e) => (editable(e) ? false : c.goto(c.current + 1)) });
    sc.add({ keys: "PageUp", description: "Previous page", run: (e) => (editable(e) ? false : c.goto(c.current - 1)) });
    sc.add({ keys: "Home", description: "First page", run: () => c.goto(0) });
    sc.add({ keys: "End", description: "Last page", run: () => c.goto(this.model.pages.length - 1) });
    const del = () => {
      // Markup first (a selected shape), pages otherwise, like the desktop.
      if (this.mk?.selection.length) { this.mk.remove(this.mk.selection); return; }
      const sel = this.thumbs.explicitSelection();
      if (!sel.length) return false;
      this.deletePages(sel);
    };
    sc.add({ keys: "Delete", description: "Delete selected pages", run: del });
    sc.add({ keys: "Backspace", description: "Delete selected pages", run: del });
    sc.add({ keys: "Escape", description: "Clear selection / close Find", run: () => {
      const s = getSelection();
      if (s && !s.isCollapsed && this.root.contains(s.anchorNode)) { s.removeAllRanges(); return; }
      if (this.mk?.selection.length) { this.mk.select([]); return; }
      if (this.thumbs.clear()) return;
      if (this.find.open) { this.closeFind(); return; }
      return false;
    } });
  }

  private focusPageEntry(): void {
    this.pageEntry.focus();
    this.pageEntry.select();
  }

  // ---- page commands ------------------------------------------------------------------

  /** Pages a page command acts on: sidebar selection (when visible), else the current page. */
  private targets(): PageRef[] {
    return this.thumbs.targets(this.ctx.sidebar.visible);
  }

  private structureChanged(select?: { ids: number[]; explicit: boolean }): void {
    if (this.panels.current === "toc") void this.refreshToc();
    else if (this.panels.current === "annots") void this.refreshAnnots();
    this.texts.clear();
    this.canvas.refresh();
    this.thumbs.relayout();
    if (select) this.thumbs.select(select.ids, -1, select.explicit);
    this.syncPageEntry();
    if (this.search) void this.runSearch(this.search.query);
    this.refreshTitle();
    this.ctx.setDirty(this.isDirty());
    this.scheduleDragOut();
  }

  private rotate(dir: 1 | -1): void {
    const refs = this.targets();
    const explicit = this.thumbs.explicit;
    if (this.model.rotate(refs.map((r) => r.id), dir)) {
      this.structureChanged(explicit ? { ids: refs.map((r) => r.id), explicit: true } : undefined);
    }
  }

  private deletePages(refs: PageRef[]): void {
    if (!refs.length) return;
    if (refs.length >= this.model.pages.length) { this.ctx.toast("A document needs at least one page"); return; }
    if (!this.model.remove(refs.map((r) => r.id))) return;
    this.structureChanged({ ids: [], explicit: false });
    this.ctx.toast(`Deleted ${plural(refs.length, "page")}`, { actionLabel: "Undo", onAction: () => this.undo() });
  }

  private movePages(ids: number[], dest: number): void {
    const at = this.model.move(ids, dest);
    if (at < 0) return;
    this.structureChanged({ ids, explicit: false });
    this.canvas.goto(at);
  }

  private undo(): void {
    this.mkReplay = true;
    try {
      const k = this.model.undo();
      if (k === "pages") this.structureChanged();
      else if (k) this.refreshDirty();
    } finally { this.mkReplay = false; }
  }

  private redo(): void {
    this.mkReplay = true;
    try {
      const k = this.model.redo();
      if (k === "pages") this.structureChanged();
      else if (k) this.refreshDirty();
    } finally { this.mkReplay = false; }
  }

  // ---- drag pages out ---------------------------------------------------------------

  private dragOutCache: { key: string; name: string; url: string } | null = null;
  private dragOutTimer = 0;
  /** Largest selection prepared for drag-out (sources read in full, built in the worker). */
  static readonly DRAG_OUT_MAX_BYTES = 64 << 20;
  static readonly DRAG_OUT_MAX_PAGES = 500;

  private dragKey(ids: number[]): string {
    return `${this.stateKey()}#${ids.join(",")}`;
  }

  /** The prepared drag-out file for exactly these pages, if ready. */
  private dragOut(ids: number[]): [string, string] | null {
    const c = this.dragOutCache;
    return c && c.key === this.dragKey(ids) ? [c.name, c.url] : null;
  }

  private scheduleDragOut(): void {
    clearTimeout(this.dragOutTimer);
    this.dragOutTimer = setTimeout(() => void this.prepareDragOut(), 250) as unknown as number;
  }

  /** Like the desktop's DragSource: a PDF of the selected pages, built before the drag starts. */
  private async prepareDragOut(): Promise<void> {
    const sel = this.thumbs.explicitSelection();
    if (!sel.length || this.destroyed) { delete this.root.dataset.dragOut; return; }
    const ids = sel.map((r) => r.id);
    const key = this.dragKey(ids);
    if (this.dragOutCache?.key === key) return;
    let bytes = 0;
    for (const s of new Set(sel.map((r) => r.src))) bytes += (await this.sources[s]!.doc.getDownloadInfo()).length;
    if (sel.length > PdfViewer.DRAG_OUT_MAX_PAGES || bytes > PdfViewer.DRAG_OUT_MAX_BYTES) {
      this.root.dataset.dragOut = "too-big";
      return;
    }
    this.root.dataset.dragOut = "preparing";
    try {
      const r = await this.build(sel, { quiet: true });
      if (this.dragKey(this.thumbs.explicitSelection().map((x) => x.id)) !== key) return;
      const all = this.model.pages;
      const nums = sel.map((x) => all.indexOf(x) + 1);
      const name = defaultName(this.src.name, `-page${nums.length > 1 ? "s" : ""}-${pageRanges(nums)}`);
      const old = this.dragOutCache;
      // A drop may still be downloading the previous file: revoke it later.
      if (old) setTimeout(() => URL.revokeObjectURL(old.url), 60_000);
      this.dragOutCache = { key, name, url: URL.createObjectURL(new Blob([r.bytes as Uint8Array<ArrayBuffer>], { type: "application/pdf" })) };
      this.root.dataset.dragOut = "ready";
    } catch (e) {
      console.warn("drag-out", e);
      delete this.root.dataset.dragOut;
    }
  }

  private refreshDirty(): void {
    this.scheduleDragOut();
    this.ctx.setDirty(this.isDirty());
    this.refreshTitle();
  }

  private async addSource(input: OpenInput, name: string): Promise<PdfSource> {
    const id = this.sources.length;
    const s = await openSource(id, input, name, askPassword, this.ctx.signal);
    this.sources[id] = s;
    (s.doc.annotationStorage as { onSetModified: (() => void) | null }).onSetModified = () => { s.formsEdited = true; this.thumbs?.invalidate(); this.refreshDirty(); };
    return s;
  }

  private async insertBlank(): Promise<void> {
    const cur = this.model.pages[this.canvas.current];
    if (!cur) return;
    const src = this.sources[cur.src]!;
    const info = src.infos[cur.index] ?? src.fallback;
    const { PDFDocument } = await import("@cantoo/pdf-lib");
    const d = await PDFDocument.create();
    d.addPage([info.w, info.h]).setRotation({ type: "degrees" as never, angle: info.rotate } as never);
    const s = await this.addSource({ data: await d.save() }, "Blank page");
    const ref = pageRef(s.id, 0, cur.rotation);
    const at = this.model.insert(this.canvas.current + 1, [ref]);
    this.structureChanged({ ids: [ref.id], explicit: false });
    this.canvas.goto(at);
  }

  private async pickAndInsert(at: number, pdfOnly: boolean): Promise<void> {
    let handles: FileSystemFileHandle[];
    try {
      handles = await window.showOpenFilePicker({
        multiple: true,
        types: pdfOnly ? PDF_TYPES : [{ description: "PDFs and images", accept: {
          "application/pdf": [".pdf"], "image/*": [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"] } }],
      });
    } catch {
      return;
    }
    const files = await Promise.all(handles.map((h) => h.getFile()));
    await this.insertFiles(files.map((f) => ({ name: f.name, mime: f.type, blob: f })), at);
  }

  private isPdfLike(name: string, mime: string | null): "pdf" | "image" | null {
    const k = (mime && kindFromMime(mime)) || kindFromName(name);
    return k === "pdf" || k === "image" ? k : null;
  }

  acceptsDrop(sources: DocumentSource[]): boolean {
    return !!this.model && sources.length > 0 && sources.every((s) => this.isPdfLike(s.name, s.mime));
  }

  async addDropped(sources: DocumentSource[]): Promise<void> {
    const at = this.thumbs.dropHint ?? this.canvas.current + 1;
    const files = await Promise.all(sources.map(async (s) => ({ name: s.name, mime: s.mime, blob: await s.blob() })));
    await this.insertFiles(files, at);
  }

  /** preview/pdf_view.insert_files: PDFs are inserted as is, images become one-page PDFs. */
  private async insertFiles(files: { name: string; mime: string | null; blob: Blob }[], at: number): Promise<void> {
    const cur = this.model.pages[Math.min(this.canvas.current, this.model.pages.length - 1)]!;
    const ci = this.sources[cur.src]!.infos[cur.index] ?? this.sources[cur.src]!.fallback;
    const refs: PageRef[] = [];
    const errors: string[] = [];
    await this.busy("Inserting…", async () => {
      for (const f of files) {
        try {
          const head = new Uint8Array(await f.blob.slice(0, 5).arrayBuffer());
          const isPdf = String.fromCharCode(...head) === "%PDF-" || this.isPdfLike(f.name, f.mime) === "pdf";
          const s = isPdf
            ? await this.addSource({ blob: f.blob }, f.name)
            : await this.addSource({ data: await imageToPdf(f.blob, ci.w, ci.h) }, f.name);
          for (let i = 0; i < s.doc.numPages; i++) refs.push(pageRef(s.id, i));
        } catch (e) {
          errors.push(e instanceof PasswordCancelled ? `“${f.name}” is password-protected` : `“${f.name}”: ${(e as Error).message}`);
        }
      }
    });
    if (refs.length) {
      const pos = this.model.insert(at, refs);
      this.structureChanged({ ids: refs.map((r) => r.id), explicit: false });
      this.canvas.goto(pos);
      this.ctx.toast(`Inserted ${plural(refs.length, "page")}`, { actionLabel: "Undo", onAction: () => this.undo() });
    }
    if (errors.length) this.ctx.toast(`Could not insert ${errors.join("; ")}`, { timeout: 6000 });
  }

  // ---- side panels ----------------------------------------------------------------

  /** Display-frame y (points) of an explicit destination ([page, /XYZ, left, top, zoom]). */
  destY(ref: PageRef, dest: unknown): number | undefined {
    if (!Array.isArray(dest) || typeof dest[3] !== "number" || (dest[1] as { name?: string })?.name !== "XYZ") return undefined;
    const src = this.sources[ref.src]!;
    const info = src.infos[ref.index] ?? src.fallback;
    return surfaceToDisplay(ref.rotation + info.rotate, info.w, info.h,
      typeof dest[2] === "number" ? dest[2] - info.x0 : 0, info.y0 + info.h - dest[3])[1];
  }

  /** Outline of the opened document (resolved to page indices once). */
  private loadOutline() {
    this.outline ??= (async () => {
      const doc = this.sources[0]!.doc;
      const out: OutlineItem[] = [];
      type Item = { title: string; dest: string | unknown[] | null; url: string | null; items: Item[]; bold?: boolean; italic?: boolean };
      const walk = async (items: Item[], depth: number) => {
        for (const it of items) {
          let dest: unknown[] | null = null, index: number | null = null;
          try {
            const d = typeof it.dest === "string" ? await doc.getDestination(it.dest) : it.dest;
            if (Array.isArray(d)) {
              dest = d;
              const p = d[0];
              index = typeof p === "number" ? p : p && typeof p === "object" ? await doc.getPageIndex(p as never) : null;
            }
          } catch { /* broken destination */ }
          out.push({ title: it.title, depth, index, dest, url: it.url ?? undefined, bold: it.bold, italic: it.italic });
          if (it.items?.length) await walk(it.items, depth + 1);
        }
      };
      await walk(((await doc.getOutline()) ?? []) as Item[], 0);
      return out;
    })();
    return this.outline;
  }

  private async refreshToc(): Promise<void> {
    const items = await this.loadOutline().catch(() => []);
    const entries: TocEntry[] = items.map((it) => {
      const pos = it.index === null ? -1 : this.model.pages.findIndex((r) => r.src === 0 && r.index === it.index);
      return { title: it.title, depth: it.depth, pos: pos >= 0 ? pos : null, url: it.url, bold: it.bold, italic: it.italic,
        y: pos >= 0 ? this.destY(this.model.pages[pos]!, it.dest) : undefined };
    });
    this.panels.setToc(entries);
    this.panels.markCurrent(this.canvas.current);
  }

  /** Annotations pdf.js draws on a page (not links, widgets, popups or markup the layer owns). */
  private pageAnnots(ref: PageRef) {
    const key = pageKey(ref);
    let p = this.pdfAnnots.get(key);
    if (!p) {
      p = (async () => {
        const page = await this.sources[ref.src]!.doc.getPage(ref.index + 1);
        const items = await page.getAnnotations({ intent: "display" }) as { subtype?: string; id?: string; contentsObj?: { str?: string }; rect?: number[] }[];
        return items.filter((a) => a.subtype && !["Link", "Widget", "Popup"].includes(a.subtype)
          && !(ref.src === 0 && a.id && this.importedIds.includes(a.id)))
          .map((a) => ({ subtype: a.subtype!, text: a.contentsObj?.str ?? "", rect: a.rect ?? [0, 0, 0, 0] }));
      })();
      this.pdfAnnots.set(key, p);
    }
    return p;
  }

  private annotsGen = 0;
  private async refreshAnnots(): Promise<void> {
    const gen = ++this.annotsGen;
    this.panels.setAnnots(null);
    const names: Record<string, string> = { highlight: "Highlight", underline: "Underline", strike: "Strikethrough",
      note: "Note", text: "Text", signature: "Signature" };
    const pages = this.model.pages;
    const posOf = new Map(pages.map((p, i) => [surfaceOf(p), i]));
    const out: AnnotEntry[] = [];
    for (const a of this.mk?.list() ?? []) {
      const pos = posOf.get(a.surface);
      if (pos === undefined || !names[a.type]) continue;
      const text = ("text" in a && typeof a.text === "string" ? a.text : "") || a.comment || "";
      const y = "y" in a ? a.y : "quads" in a ? Math.min(...a.quads.filter((_, i) => i % 2 === 1)) : undefined;
      out.push({ title: `${names[a.type]} · page ${pos + 1}`, text: text.trim().replace(/\s+/g, " ").slice(0, 80), pos, id: a.id,
        y: y === undefined ? undefined : this.surfaceY(pages[pos]!, y) });
    }
    for (let pos = 0; pos < pages.length; pos++) {
      const ref = pages[pos]!;
      const list = await this.pageAnnots(ref).catch(() => []);
      if (gen !== this.annotsGen) return;
      for (const a of list) {
        const info = this.sources[ref.src]!.infos[ref.index] ?? this.sources[ref.src]!.fallback;
        out.push({ title: `${a.subtype.replace(/([a-z])([A-Z])/g, "$1 $2")} · page ${pos + 1}`, text: a.text.trim().slice(0, 80), pos,
          y: this.surfaceY(ref, info.y0 + info.h - a.rect[3]!) });
      }
    }
    out.sort((a, b) => a.pos - b.pos || (a.y ?? 0) - (b.y ?? 0));
    if (gen === this.annotsGen) this.panels.setAnnots(out);
  }

  private surfaceY(ref: PageRef, v: number): number {
    const src = this.sources[ref.src]!;
    const info = src.infos[ref.index] ?? src.fallback;
    return surfaceToDisplay(ref.rotation + info.rotate, info.w, info.h, 0, v)[1];
  }

  // ---- find ------------------------------------------------------------------------------

  private openFind(): void {
    this.find.show();
  }

  private closeFind(): void {
    this.find.hide();
    this.search = null;
    this.find.setLabel("");
    for (const s of this.canvas.allSlots()) this.paintHighlights(s);
    this.canvas.scroll.focus({ preventScroll: true });
  }

  private pageText(ref: PageRef): Promise<PageText> {
    const key = pageKey(ref);
    let p = this.texts.get(key);
    if (!p) {
      p = (async () => {
        const src = this.sources[ref.src]!;
        const page = await src.doc.getPage(ref.index + 1);
        const t = await extractPageText(page, infoOf(page));
        const ocr = this.ocrLines.get(key);
        if (!t.hasText && ocr?.length) return pageTextFromRuns(runsFromOcr(ocr), false);
        return t;
      })();
      p.catch(() => this.texts.delete(key));
      this.texts.set(key, p);
    }
    return p;
  }

  private async runSearch(raw: string): Promise<void> {
    const query = normalizeQuery(raw);
    const gen = (this.search?.gen ?? 0) + 1;
    const st = { query: raw, gen, hits: new Map<number, number[][]>(), cur: null as [number, number] | null, count: 0, done: false };
    this.search = query ? st : null;
    for (const s of this.canvas.allSlots()) this.paintHighlights(s);
    if (!query) { this.find.setLabel(""); this.root.dataset.matches = "0"; return; }
    this.find.setLabel("Searching…");
    const pages = this.model.pages;
    const n = pages.length;
    const start = this.canvas.current;
    let last = performance.now();
    let textless = 0;
    for (let k = 0; k < n; k++) {
      if (this.search !== st || this.destroyed) return;
      const ref = pages[(start + k) % n]!;
      let t: PageText;
      try { t = await this.pageText(ref); } catch { continue; }
      if (!t.hasText && !this.ocrLines.has(pageKey(ref))) textless++;
      const hits = findInPage(t, query);
      if (hits.length) {
        st.hits.set(ref.id, hits);
        st.count += hits.length;
        if (!st.cur) { st.cur = [ref.id, 0]; this.showMatch(); }
        const slot = this.canvas.allSlots().find((s) => s.ref.id === ref.id);
        if (slot) this.paintHighlights(slot);
      }
      if (performance.now() - last > 100) { this.updateFindLabel(); last = performance.now(); }
    }
    st.done = true;
    this.updateFindLabel();
    if (!st.count && textless) {
      this.ctx.toast(`${plural(textless, "page")} ${textless === 1 ? "has" : "have"} no text`, {
        actionLabel: "Recognize Text", onAction: () => void this.recognize(this.model.pages.filter((p) => !this.ocrLines.has(pageKey(p)))),
      });
    }
  }

  /** Matches in document order: [ref id, hit index]. */
  private flatMatches(): [number, number][] {
    const st = this.search;
    if (!st) return [];
    const out: [number, number][] = [];
    for (const p of this.model.pages) {
      const h = st.hits.get(p.id);
      if (h) for (let i = 0; i < h.length; i++) out.push([p.id, i]);
    }
    return out;
  }

  private updateFindLabel(): void {
    const st = this.search;
    if (!st) return;
    this.root.dataset.matches = String(st.count);
    if (!st.count) { this.find.setLabel(st.done ? "No matches" : "Searching…"); return; }
    const flat = this.flatMatches();
    const i = st.cur ? flat.findIndex(([id, k]) => id === st.cur![0] && k === st.cur![1]) : -1;
    this.find.setLabel(`${i + 1} of ${st.count}${st.done ? "" : "…"}`);
  }

  private searchStep(d: 1 | -1): void {
    const st = this.search;
    if (!st) { void this.runSearch(this.find.input.value); return; }
    const flat = this.flatMatches();
    if (!flat.length) return;
    const i = st.cur ? flat.findIndex(([id, k]) => id === st.cur![0] && k === st.cur![1]) : -1;
    st.cur = flat[(i + d + flat.length) % flat.length]!;
    this.showMatch();
    this.updateFindLabel();
  }

  private showMatch(): void {
    const st = this.search;
    if (!st?.cur) return;
    const [id, k] = st.cur;
    const pos = this.model.indexOfId(id);
    const ref = this.model.pages[pos];
    const boxes = st.hits.get(id)?.[k];
    if (!ref || !boxes) return;
    const src = this.sources[ref.src]!;
    const info = src.infos[ref.index] ?? src.fallback;
    const [x, y, w, h] = boxes as [number, number, number, number];
    const r = quadsToDisplayRect(info.rotate + ref.rotation, info.w, info.h, [x, y, x + w, y, x + w, y + h, x, y + h]);
    this.canvas.revealRect(pos, ...r);
    for (const s of this.canvas.allSlots()) this.paintHighlights(s);
  }

  private paintHighlights(slot: Slot): void {
    slot.hl.replaceChildren();
    const hits = this.search?.hits.get(slot.ref.id);
    if (!hits) return;
    const { w, h } = slot.info;
    const rot = slot.rotation;
    const [dw, dh] = displaySize(rot, w, h);
    const cur = this.search!.cur;
    hits.forEach((boxes, k) => {
      for (let i = 0; i + 3 < boxes.length; i += 4) {
        const [x, y, bw, bh] = boxes.slice(i, i + 4) as [number, number, number, number];
        const [rx, ry, rw, rh] = quadsToDisplayRect(rot, w, h, [x, y, x + bw, y, x + bw, y + bh, x, y + bh]);
        const d = el("div.pdf-hl-box");
        if (cur && cur[0] === slot.ref.id && cur[1] === k) d.classList.add("current");
        Object.assign(d.style, { left: `${rx / dw * 100}%`, top: `${ry / dh * 100}%`, width: `${rw / dw * 100}%`, height: `${rh / dh * 100}%` });
        slot.hl.append(d);
      }
    });
  }

  // ---- Live Text (OCR) ------------------------------------------------------------------

  private ocrService: Promise<OcrService> | null = null;

  private async recognize(refs: PageRef[]): Promise<boolean> {
    if (!refs.length) return true;
    this.ocrService ??= this.ctx.services.ocr();
    let done = 0;
    try {
      const ocr = await this.ocrService;
      await this.busy("Recognizing text…", async (set) => {
        for (const ref of refs) {
          set(`Recognizing text on page ${this.model.pages.indexOf(ref) + 1} (${done + 1} of ${refs.length})…`);
          const t = await this.pageText(ref).catch(() => null);
          if (t?.hasText) { done++; continue; }
          await this.ocrPage(ocr, ref);
          done++;
        }
      });
      this.ctx.toast(`Recognized text on ${plural(done, "page")}`);
      return true;
    } catch (e) {
      this.ctx.toast(`Text recognition failed: ${(e as Error).message}`, { timeout: 6000 });
      return false;
    }
  }

  private async ocrPage(ocr: OcrService, ref: PageRef): Promise<SurfaceLine[]> {
    const src = this.sources[ref.src]!;
    const k = 200 / 72;
    const canvas = await renderPage(src, ref, k);
    const bmp = await createImageBitmap(canvas);
    canvas.width = canvas.height = 0;
    const res = await ocr.recognize(bmp, { signal: this.ctx.signal });
    bmp.close();
    const info = src.infos[ref.index] ?? src.fallback;
    const rot = info.rotate + ref.rotation;
    const map = (pts: number[]) => {
      const out: number[] = [];
      for (let i = 0; i + 1 < pts.length; i += 2) out.push(...displayToSurface(rot, info.w, info.h, pts[i]! / k, pts[i + 1]! / k));
      return out;
    };
    const lines = res.lines.map((l) => ({ text: l.text, box: map(l.box), charQuads: l.charQuads ? map(l.charQuads) : undefined }));
    const key = pageKey(ref);
    this.ocrLines.set(key, lines);
    this.ocrResults.set(key, { result: res, rot: ((rot % 360) + 360) % 360 });
    this.texts.delete(key);
    for (const s of this.canvas.allSlots()) if (pageKey(s.ref) === key) this.paintOcr(s);
    return lines;
  }

  /** Clicking a scanned page recognizes its text right away, as the desktop does. */
  private async autoOcr(slot: Slot): Promise<void> {
    const key = pageKey(slot.ref);
    if (this.ocrStarted.has(key) || this.ocrLines.has(key)) return;
    this.ocrStarted.add(key);
    const t = await this.pageText(slot.ref).catch(() => null);
    if (!t || t.hasText) return;
    if (!await this.recognize([slot.ref])) this.ocrStarted.delete(key);
  }

  private textLayerMod: Promise<typeof import("../../ocr/text-layer.ts")> | null = null;

  /**
   * Live Text over a scanned page: stream F's selectable SVG layer, in the
   * pixel space of the bitmap OCR saw (the page as displayed then). If the page
   * was rotated since, the layer is turned by the difference.
   */
  private paintOcr(slot: Slot): void {
    (slot.data.ocrLayer as { destroy(): void } | undefined)?.destroy();
    slot.data.ocrLayer = undefined;
    slot.ocr.replaceChildren();
    const r = this.ocrResults.get(pageKey(slot.ref));
    if (!r) return;
    this.textLayerMod ??= import("../../ocr/text-layer.ts");
    void this.textLayerMod.then(({ createTextLayer }) => {
      if (slot.dead || this.ocrResults.get(pageKey(slot.ref)) !== r) return;
      const layer = createTextLayer(r.result);
      slot.data.ocrLayer = layer;
      const [dw, dh] = displaySize(slot.rotation, slot.info.w, slot.info.h);
      const d = ((slot.rotation - r.rot) % 360 + 360) % 360;
      const wrap = el("div.pdf-ocr-wrap", {}, layer.el);
      if (d % 180) {
        Object.assign(wrap.style, { width: `${dh / dw * 100}%`, height: `${dw / dh * 100}%`,
          left: `${(1 - dh / dw) * 50}%`, top: `${(1 - dw / dh) * 50}%` });
      }
      if (d) wrap.style.transform = `rotate(${d}deg)`;
      slot.ocr.replaceChildren(wrap);
    });
  }

  // ---- markup ----------------------------------------------------------------------------

  private async toggleMarkup(): Promise<void> {
    if (!this.mk) {
      // Existing markup of the opened PDF becomes editable: import it, hide it
      // from pdf.js rendering, and strip it on save (it is written back by
      // writePdfAnnotations). Inserted documents keep theirs as page content.
      const { importPdfAnnotations } = await import("../../markup/pdf-import.ts");
      const doc0 = this.sources[0]!.doc;
      const imported = await this.busy("Loading markup…", () => importPdfAnnotations(doc0, { surfaceOf: (i) => `p${i}` }))
        .catch((e: unknown) => { console.warn("markup import", e); return { annotations: [], sourceIds: [] as string[] }; });
      if (this.destroyed) return;
      if (imported.sourceIds.length) {
        this.importedIds = imported.sourceIds;
        const hidden = { ids: new Set(imported.sourceIds), hash: `view-markup-${imported.sourceIds.length}` };
        Object.defineProperty(doc0.annotationStorage, "modifiedIds", { get: () => hidden, configurable: true });
        await this.hideImportedHtml(imported.annotations.map((a) => a.surface), imported.sourceIds);
        this.pdfAnnots.clear();
        this.canvas.invalidateAll();
        this.thumbs.invalidate();
      }
      const mk = await this.ctx.services.markup({ docKind: "pdf", initial: imported.annotations });
      if (this.destroyed) { mk.destroy(); return; }
      this.mk = mk;
      this.mkBar.append(mk.toolbar());
      let thumbTimer = 0;
      let annotsTimer = 0;
      mk.on("change", () => {
        this.mkVersion++;
        if (this.panels.current === "annots") {
          clearTimeout(annotsTimer);
          annotsTimer = setTimeout(() => void this.refreshAnnots(), 300) as unknown as number;
        }
        if (!this.mkReplay) this.model.pushExternal(() => mk.undo(), () => mk.redo());
        this.refreshDirty();
        clearTimeout(thumbTimer);
        thumbTimer = setTimeout(() => this.thumbs.invalidate(), 600) as unknown as number;
      });
      for (const s of this.canvas.allSlots()) this.attachMarkup(s);
    }
    this.mkBar.hidden = !this.mkBar.hidden;
    if (this.mkBar.hidden) this.mk.tool = "select";
    this.root.dataset.markup = String(!this.mkBar.hidden);
    const btn = this.ctx.header.tools.querySelector('[data-role="markup-toggle"]');
    btn?.setAttribute("aria-pressed", String(!this.mkBar.hidden));
  }

  /**
   * pdf.js also builds HTML for annotations (note icons, pop-ups) in its
   * annotation layer: hide it for the ones the markup layer now owns, and
   * for their /Popup annotations.
   */
  private async hideImportedHtml(surfaces: string[], ids: string[]): Promise<void> {
    const hide = new Set(ids);
    const pages = new Set(surfaces.map((s) => /^p(\d+)$/.exec(s)?.[1]).filter((x): x is string => !!x).map(Number));
    for (const i of pages) {
      const page = await this.sources[0]!.doc.getPage(i + 1);
      const items = await page.getAnnotations({ intent: "display" }) as { id?: string; subtype?: string; parentId?: string }[];
      for (const a of items) if (a.subtype === "Popup" && a.id && a.parentId && hide.has(a.parentId)) hide.add(a.id);
    }
    const sel = [...hide].flatMap((id) => [`[data-annotation-id="${CSS.escape(id)}"]`, `[data-annotation-id="popup_${CSS.escape(id)}"]`])
      .map((x) => `.pdf-root .annotationLayer ${x}`);
    this.root.querySelector("style.pdf-hidden-annots")?.remove();
    if (sel.length) this.root.append(el("style.pdf-hidden-annots", { text: `${sel.join(",\n")} { display: none !important; }` }));
  }

  private attachMarkup(slot: Slot): void {
    if (!this.mk || !slot.view || this.surfaces.has(slot)) return;
    const ref = slot.ref;
    const surface = this.mk.attach({
      surface: surfaceOf(ref),
      width: slot.info.w,
      height: slot.info.h,
      overlay: slot.mk,
      scale: () => this.canvas.ppt,
      rotation: () => slot.rotation,
      textRuns: async () => (await this.pageText(slot.ref)).runs,
    });
    this.surfaces.set(slot, surface);
  }

  /** Draw a page's markup onto a raster in display orientation. */
  private paintMarkup(c: CanvasRenderingContext2D, ref: PageRef, s: number, rotation?: number, w?: number, h?: number): void {
    const mk = this.mk;
    if (!mk) return;
    const surface = surfaceOf(ref);
    if (!mk.list(surface).length) return;
    const info = this.sources[ref.src]!.infos[ref.index] ?? this.sources[ref.src]!.fallback;
    const rot = ((rotation ?? info.rotate + ref.rotation) % 360 + 360) % 360;
    const W = w ?? info.w, H = h ?? info.h;
    c.save();
    if (rot === 90) { c.translate(H * s, 0); c.rotate(Math.PI / 2); }
    else if (rot === 180) { c.translate(W * s, H * s); c.rotate(Math.PI); }
    else if (rot === 270) { c.translate(0, W * s); c.rotate(-Math.PI / 2); }
    mk.render(c, surface, s);
    c.restore();
  }

  private get painter(): MarkupPainter | undefined {
    return this.mk ? (c, ref, s, rot, w, h) => this.paintMarkup(c, ref, s, rot, w, h) : undefined;
  }

  // ---- save / export -----------------------------------------------------------------------

  exportFormats(): ExportFormat[] {
    return [
      { id: "images", label: "Images (PNG or JPEG)…" },
      { id: "encrypted", label: "PDF with Password…" },
      { id: "reduced", label: "Reduced-Size PDF…" },
      { id: "searchable", label: "Searchable PDF (OCR)…" },
    ];
  }

  private async getBuildSource(i: number): Promise<BuildSource> {
    const s = this.sources[i]!;
    // Form values live in pdf.js's annotation storage: saveDocument() writes them in.
    const bytes = s.doc.annotationStorage.size > 0 ? await s.doc.saveDocument() : await s.bytes();
    return { bytes, password: s.password };
  }

  /**
   * Build a PDF of `refs` in the save worker (job.ts). Source bytes are
   * gathered here (pdf.js writes edited form values into them first).
   */
  private async build(refs: PageRef[], o: Omit<SaveJob, "sources" | "pages" | "base" | "markup"> & { markup?: boolean; quiet?: boolean } = {}): Promise<JobResult> {
    const base = refs.some((r) => r.src === 0) ? 0 : refs[0]!.src;
    const surfaces = refs.map((r, i) => [surfaceOf(r), i] as [string, number]);
    const ids = new Set(surfaces.map(([s]) => s));
    const withMarkup = o.markup !== false;
    const annotations: Annotation[] = withMarkup && this.mk ? this.mk.toJSON().filter((a) => ids.has(a.surface)) : [];
    const run = async (set: (m: string) => void) => {
      const sources: SaveJob["sources"] = {};
      for (const i of new Set(refs.map((r) => r.src))) sources[i] = await this.getBuildSource(i);
      const { markup: _, quiet: __, ...rest } = o;
      const job: SaveJob = {
        ...rest, sources, base,
        pages: refs.map((r) => ({ src: r.src, index: r.index, rotation: r.rotation })),
        markup: withMarkup ? { annotations, surfaces, removeIds: this.importedIds, flatten: true } : undefined,
      };
      const r = await runJob(job, { progress: set, warn: (m) => this.ctx.toast(m, { timeout: 6000 }) });
      this.root.dataset.savedVia = r.via ?? "";
      return r;
    };
    return o.quiet ? run(() => {}) : this.busy("Saving…", run);
  }

  async save(req: SaveRequest): Promise<SaveResult | null> {
    if (!this.model) return null;
    const pdf = (bytes: Uint8Array, name: string, replacesOriginal: boolean): SaveResult =>
      ({ blob: new Blob([bytes as Uint8Array<ArrayBuffer>], { type: "application/pdf" }), suggestedName: name, types: PDF_TYPES, replacesOriginal });
    const all = this.model.pages;
    const signed = !!this.mk?.list().some((a) => a.type === "signature");
    const edited = defaultName(this.src.name, signed ? "-signed" : "-edited");
    if (req.mode === "export") return this.exportAs(req.format ?? "images");
    const sel = this.thumbs.explicitSelection();
    if (req.mode === "save" && (req.selectionOnly || (sel.length && sel.length < all.length))) {
      const refs = sel.length ? sel : [all[this.canvas.current]!];
      const nums = refs.map((r) => all.indexOf(r) + 1);
      return pdf((await this.build(refs)).bytes, defaultName(this.src.name, `-page${nums.length > 1 ? "s" : ""}-${pageRanges(nums)}`), false);
    }
    if (req.mode === "save" && !this.isDirty() && (this.src.handle || this.savedOnce)) {
      this.ctx.toast("No changes to save");
      return null;
    }
    const key = this.stateKey();
    // Unchanged from the opened file: hand back its exact bytes.
    const original = this.model.identity && !this.mk?.list().length && !this.importedIds.length
      && this.sources[0]!.doc.annotationStorage.size === 0;
    const bytes = original
      ? await this.sources[0]!.bytes()
      : (await this.build(all, { keepPassword: true, basePassword: this.sources[0]!.password })).bytes;
    const name = req.mode === "save" && this.src.handle ? this.src.name : edited;
    const result = pdf(bytes, name, true);
    this.pending = { result, key };
    return result;
  }

  private async exportAs(format: string): Promise<SaveResult | null> {
    const all = this.model.pages;
    const pdf = (bytes: Uint8Array, suffix: string): SaveResult =>
      ({ blob: new Blob([bytes as Uint8Array<ArrayBuffer>], { type: "application/pdf" }), suggestedName: defaultName(this.src.name, suffix), types: PDF_TYPES, replacesOriginal: false });
    switch (format) {
      case "images": {
        const sel = this.thumbs.explicitSelection();
        const refs = sel.length ? sel : all;
        const opts = await askExportImages(refs.length);
        if (!opts) return null;
        const res = await this.busy("Exporting…", (set) => exportImages(
          refs.map((ref) => ({ src: this.sources[ref.src]!, ref })), stemOf(this.src.name), opts.format, opts.dpi,
          (d, n) => set(`Exporting page ${Math.min(d + 1, n)} of ${n}…`), this.painter, this.src.handle));
        if (res) this.ctx.toast(`Exported ${plural(res.count, "image")} to “${res.where}”`);
        return null;
      }
      case "encrypted": {
        const enc = await askEncrypt();
        if (!enc) return null;
        return pdf((await this.build(all, { encrypt: enc })).bytes, "-protected");
      }
      case "reduced": {
        const before = (await this.sources[0]!.bytes()).length;
        const { bytes, reduced: n } = await this.build(all, { reduce: true });
        if (bytes.length >= before) {
          this.ctx.toast("This PDF is already compact — nothing to reduce");
          return null;
        }
        const { humanSize } = await import("../../ui/kit.ts");
        this.ctx.toast(`${humanSize(bytes.length)} (was ${humanSize(before)})${n ? `, ${plural(n, "image")} recompressed` : ""}`, { timeout: 5000 });
        return pdf(bytes, "-reduced");
      }
      case "searchable": {
        const need: PageRef[] = [];
        for (const r of all) {
          const t = await this.pageText(r).catch(() => null);
          if (t && !t.hasText && !this.ocrLines.has(pageKey(r))) need.push(r);
        }
        if (need.length) await this.recognize(need);
        const layers: [number, SurfaceLine[]][] = [];
        all.forEach((r, i) => { const l = this.ocrLines.get(pageKey(r)); if (l?.length) layers.push([i, l]); });
        return pdf((await this.build(all, { textLayers: layers })).bytes, "-searchable");
      }
    }
    return null;
  }

  // ---- print / info ----------------------------------------------------------------------

  async print(): Promise<void> {
    const pages = this.model.pages;
    const first = this.canvas.dispSize(pages[0]!);
    await this.busy("Preparing to print…", (set) => printPages(
      pages.map((ref) => ({ src: this.sources[ref.src]!, ref })), first,
      (d, n) => set(`Preparing page ${Math.min(d + 1, n)} of ${n} for printing…`), this.ctx.signal, this.painter));
  }

  async getInfo(): Promise<InfoSection[]> {
    const src = this.sources[0];
    if (!src) return [];
    const doc = src.doc;
    const { info } = await doc.getMetadata() as { info: Record<string, unknown> };
    const rows: InfoSection["rows"] = [];
    const add = (label: string, v: unknown) => { if (v !== null && v !== undefined && v !== "") rows.push([label, String(v)]); };
    const s = (k: string) => (typeof info[k] === "string" ? (info[k] as string).trim() : null);
    const date = (k: string) => {
      const d = pdfjs.PDFDateString.toDateObject(s(k) ?? "");
      return d ? d.toLocaleString() : null;
    };
    add("Title", s("Title"));
    add("Author", s("Author"));
    add("Subject", s("Subject"));
    add("Keywords", s("Keywords"));
    add("Creator", s("Creator"));
    add("Encoding software", s("Producer"));
    add("PDF version", s("PDFFormatVersion"));
    add("Created", date("CreationDate"));
    add("Modified", date("ModDate"));
    add("Pages", this.model.pages.length);
    const cur = this.model.pages[this.canvas.current];
    if (cur) add("Page size", pageSizeLabel(...this.canvas.dispSize(cur)));
    if (info.EncryptFilterName || src.password) {
      const perms = await doc.getPermissions();
      const F = pdfjs.PermissionFlag;
      const allowed = perms === null ? ["everything"] : ([["printing", F.PRINT], ["copying", F.COPY], ["modifying", F.MODIFY_CONTENTS],
        ["annotating", F.MODIFY_ANNOTATIONS], ["form filling", F.FILL_INTERACTIVE_FORMS]] as const)
        .filter(([, f]) => perms.has(f)).map(([n]) => n);
      add("Security", `Encrypted${src.password ? " (password to open)" : ""}${allowed.length ? ` — allows ${allowed.join(", ")}` : " — no permissions"}`);
    } else add("Security", "None");
    add("Linearized (fast web view)", info.IsLinearized ? "Yes" : "No");
    const mark = await doc.getMarkInfo().catch(() => null);
    add("Tagged", mark?.Marked ? "Yes" : "No");
    const fields = await doc.getFieldObjects().catch(() => null);
    if (fields) add("Form fields", Object.keys(fields).length);
    if (info.IsXFAPresent) add("XFA form", "Yes");
    const out: InfoSection[] = [{ title: "Document", rows }];
    const others = this.sources.slice(1).filter((x) => this.model.pages.some((p) => p.src === x.id) && x.name !== "Blank page");
    if (others.length) out.push({ title: "Inserted", rows: [["Documents", [...new Set(others.map((o) => o.name))].join(", ")]] });
    return out;
  }
}

/** preview/pdf_ops.image_to_pdf_bytes: a one-page PDF of an image, scaled down to fit fitW × fitH points. */
async function imageToPdf(blob: Blob, fitW: number, fitH: number): Promise<Uint8Array> {
  const { PDFDocument } = await import("@cantoo/pdf-lib");
  const doc = await PDFDocument.create();
  const head = new Uint8Array(await blob.slice(0, 8).arrayBuffer());
  const isJpeg = head[0] === 0xff && head[1] === 0xd8;
  const isPng = head[0] === 0x89 && head[1] === 0x50;
  let img;
  if (isJpeg || isPng) {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    img = isJpeg ? await doc.embedJpg(bytes) : await doc.embedPng(bytes);
  } else {
    const bmp = await createImageBitmap(blob);
    const c = new OffscreenCanvas(bmp.width, bmp.height);
    const g = c.getContext("2d")!;
    g.fillStyle = "#fff";
    g.fillRect(0, 0, bmp.width, bmp.height);
    g.drawImage(bmp, 0, 0);
    bmp.close();
    img = await doc.embedPng(new Uint8Array(await (await c.convertToBlob({ type: "image/png" })).arrayBuffer()));
  }
  const natW = img.width * 72 / 96, natH = img.height * 72 / 96;
  const s = Math.min(1, fitW / natW, fitH / natH);
  const page = doc.addPage([natW * s, natH * s]);
  page.drawImage(img, { x: 0, y: 0, width: natW * s, height: natH * s });
  return doc.save();
}

const mod: ViewerModule = { kind: "pdf", create: () => new PdfViewer() };
export default mod;
