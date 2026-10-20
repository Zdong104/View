// Sidebar views (preview/pdf_view.py side_toggle): Thumbnails | Table of
// Contents | Highlights and Notes. The thumbnails list is thumbs.ts; this file
// owns the switcher and the two lists, which the viewer fills.
import { button, el, linked } from "../../ui/kit.ts";

export type PanelName = "thumbs" | "toc" | "annots";

export interface TocEntry {
  title: string;
  depth: number;
  /** Position in the page model, or null (no destination / page deleted). */
  pos: number | null;
  /** Display-frame y (points) of the destination on that page. */
  y?: number;
  url?: string;
  bold?: boolean;
  italic?: boolean;
}

export interface AnnotEntry {
  title: string;
  text: string;
  pos: number;
  /** Markup annotation id (selectable), absent for annotations pdf.js draws. */
  id?: string;
  y?: number;
}

export interface PanelsDelegate {
  shown(name: PanelName): void;
  gotoToc(e: TocEntry): void;
  gotoAnnot(e: AnnotEntry): void;
}

export class SidePanels {
  readonly root: HTMLDivElement;
  current: PanelName = "thumbs";
  private views: Record<PanelName, HTMLElement>;
  private buttons: Record<PanelName, HTMLButtonElement>;
  private toc: TocEntry[] = [];
  private tocRows: HTMLElement[] = [];
  private d: PanelsDelegate;

  constructor(thumbs: HTMLElement, d: PanelsDelegate) {
    this.d = d;
    const tocEl = el("div.pdf-side-list", { role: "tree", "aria-label": "Table of Contents", "data-panel": "toc" });
    const annEl = el("div.pdf-side-list", { role: "list", "aria-label": "Highlights and Notes", "data-panel": "annots" });
    this.views = { thumbs, toc: tocEl, annots: annEl };
    const mk = (name: PanelName, icon: Parameters<typeof button>[0]["icon"], title: string) => {
      const b = button({ icon, title, onClick: () => this.show(name) });
      b.dataset.panel = name;
      b.setAttribute("role", "tab");
      return b;
    };
    this.buttons = {
      thumbs: mk("thumbs", "grid", "Thumbnails"),
      toc: mk("toc", "menu", "Table of Contents"),
      annots: mk("annots", "note", "Highlights and Notes"),
    };
    const tabs = linked(this.buttons.thumbs, this.buttons.toc, this.buttons.annots);
    tabs.setAttribute("role", "tablist");
    this.root = el("div.pdf-side", {}, el("div.pdf-side-tabs", {}, tabs),
      el("div.pdf-side-stack", {}, thumbs, tocEl, annEl));
    this.show("thumbs");
  }

  show(name: PanelName): void {
    this.current = name;
    for (const [n, v] of Object.entries(this.views)) v.hidden = n !== name;
    for (const [n, b] of Object.entries(this.buttons)) {
      b.setAttribute("aria-pressed", String(n === name));
      b.setAttribute("aria-selected", String(n === name));
    }
    this.root.dataset.panel = name;
    this.d.shown(name);
  }

  setToc(entries: TocEntry[]): void {
    this.toc = entries;
    const v = this.views.toc;
    v.replaceChildren();
    this.tocRows = [];
    if (!entries.length) { v.append(el("p.pdf-side-empty", { text: "No table of contents" })); return; }
    for (const e of entries) {
      const row = el("button.pdf-side-row.pdf-toc-row", { type: "button", role: "treeitem", "aria-level": e.depth + 1 },
        el("span.pdf-toc-title", { text: e.title || "Untitled" }),
        e.pos !== null ? el("span.pdf-toc-page", { text: String(e.pos + 1) }) : null);
      row.style.paddingLeft = `${8 + Math.min(e.depth, 6) * 14}px`;
      if (e.depth === 0 || e.bold) row.classList.add("strong");
      if (e.italic) row.classList.add("italic");
      if (e.pos === null && !e.url) row.disabled = true;
      row.addEventListener("click", () => this.d.gotoToc(e));
      v.append(row);
      this.tocRows.push(row);
    }
  }

  /** Highlight the section containing page `pos` (the last entry starting at or before it). */
  markCurrent(pos: number): void {
    let best = -1, bestPos = -1;
    this.toc.forEach((e, i) => {
      if (e.pos !== null && e.pos <= pos && e.pos >= bestPos) { best = i; bestPos = e.pos; }
    });
    this.tocRows.forEach((r, i) => r.classList.toggle("current", i === best));
  }

  setAnnots(entries: AnnotEntry[] | null): void {
    const v = this.views.annots;
    v.replaceChildren();
    if (entries === null) { v.append(el("p.pdf-side-empty", { text: "Looking for highlights and notes…" })); return; }
    if (!entries.length) { v.append(el("p.pdf-side-empty", { text: "No highlights or notes" })); return; }
    for (const e of entries) {
      const row = el("button.pdf-side-row.pdf-annot-row", { type: "button", role: "listitem" },
        el("span.pdf-annot-title", { text: e.title }),
        e.text ? el("span.pdf-annot-text", { text: e.text }) : null);
      if (e.id) row.dataset.annotId = e.id;
      row.addEventListener("click", () => this.d.gotoAnnot(e));
      v.append(row);
    }
  }
}
