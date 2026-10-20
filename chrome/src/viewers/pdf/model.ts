// Page model of the PDF viewer (port of preview/pdf_ops.PageRef + the page
// commands of preview/pdf_view.py). The document on screen is a list of page
// references into one or more source PDFs; editing pages only edits this list,
// and save() builds a new file from it (build.ts). Pure: covered by
// test/unit/a-model.test.ts.

export interface PageRef {
  /** Stable identity of this page in the viewer (selection, slots, thumbnails). */
  readonly id: number;
  /** Index into the viewer's sources (0 = the opened document). */
  readonly src: number;
  /** 0-based page index inside that source. */
  readonly index: number;
  /** User rotation, clockwise, multiple of 90 (added to the page's /Rotate). */
  rotation: number;
}

let nextId = 1;
export function pageRef(src: number, index: number, rotation = 0): PageRef {
  return { id: nextId++, src, index, rotation };
}

/** Markup surface id of a page (markup/api.ts: "p<index>" in the original document). */
export function surfaceOf(ref: Pick<PageRef, "src" | "index">): string {
  return ref.src === 0 ? `p${ref.index}` : `s${ref.src}p${ref.index}`;
}

export function pageKey(ref: Pick<PageRef, "src" | "index">): string {
  return `${ref.src}:${ref.index}`;
}

type Entry =
  | { kind: "pages"; pages: PageRef[] }
  | { kind: "external"; undo(): void; redo(): void };

const copy = (pages: PageRef[]) => pages.map((p) => ({ ...p }));

export class PageModel {
  pages: PageRef[];
  private undoStack: Entry[] = [];
  private redoStack: Entry[] = [];
  private cleanKey: string;

  constructor(pages: PageRef[]) {
    this.pages = pages;
    this.cleanKey = this.key();
  }

  key(): string {
    return this.pages.map((p) => `${p.src}:${p.index}:${((p.rotation % 360) + 360) % 360}`).join(",");
  }

  get dirty(): boolean { return this.key() !== this.cleanKey; }
  markClean(): void { this.cleanKey = this.key(); }

  /** True when the pages are exactly the opened document's pages, in order, unrotated. */
  get identity(): boolean {
    return this.pages.every((p, i) => p.src === 0 && p.index === i && p.rotation % 360 === 0);
  }

  get canUndo(): boolean { return this.undoStack.length > 0; }
  get canRedo(): boolean { return this.redoStack.length > 0; }

  private push(): void {
    this.undoStack.push({ kind: "pages", pages: copy(this.pages) });
    if (this.undoStack.length > 100) this.undoStack.shift();
    this.redoStack.length = 0;
  }

  /** Record an edit made elsewhere (the markup layer) in the same history. */
  pushExternal(undo: () => void, redo: () => void): void {
    this.undoStack.push({ kind: "external", undo, redo });
    if (this.undoStack.length > 100) this.undoStack.shift();
    this.redoStack.length = 0;
  }

  /** Returns "pages" when the page list changed, "external" or null. */
  undo(): Entry["kind"] | null {
    const e = this.undoStack.pop();
    if (!e) return null;
    if (e.kind === "pages") {
      this.redoStack.push({ kind: "pages", pages: copy(this.pages) });
      this.pages = e.pages;
    } else {
      e.undo();
      this.redoStack.push(e);
    }
    return e.kind;
  }

  redo(): Entry["kind"] | null {
    const e = this.redoStack.pop();
    if (!e) return null;
    if (e.kind === "pages") {
      this.undoStack.push({ kind: "pages", pages: copy(this.pages) });
      this.pages = e.pages;
    } else {
      e.redo();
      this.undoStack.push(e);
    }
    return e.kind;
  }

  indexOfId(id: number): number {
    return this.pages.findIndex((p) => p.id === id);
  }

  /**
   * Move pages to `dest`, an insertion index in the current list (0..length).
   * Returns the new position of the first moved page, or -1 if nothing moved.
   */
  move(ids: Iterable<number>, dest: number): number {
    const set = new Set(ids);
    const moving = this.pages.filter((p) => set.has(p.id));
    if (!moving.length) return -1;
    const before = this.pages.slice(0, Math.max(0, dest)).filter((p) => set.has(p.id)).length;
    const rest = this.pages.filter((p) => !set.has(p.id));
    const at = Math.max(0, Math.min(dest - before, rest.length));
    const next = [...rest.slice(0, at), ...moving, ...rest.slice(at)];
    if (next.every((p, i) => p === this.pages[i])) return -1;
    this.push();
    this.pages = next;
    return at;
  }

  /** Delete pages; refuses to delete every page (returns false). */
  remove(ids: Iterable<number>): boolean {
    const set = new Set(ids);
    const next = this.pages.filter((p) => !set.has(p.id));
    if (next.length === this.pages.length || next.length === 0) return false;
    this.push();
    this.pages = next;
    return true;
  }

  /** Rotate pages by quarter turns (1 = 90° clockwise, -1 = counter-clockwise). */
  rotate(ids: Iterable<number>, turns: number): boolean {
    const set = new Set(ids);
    if (!this.pages.some((p) => set.has(p.id))) return false;
    this.push();
    this.pages = this.pages.map((p) => set.has(p.id)
      ? { ...p, rotation: (((p.rotation + 90 * turns) % 360) + 360) % 360 } : p);
    return true;
  }

  insert(at: number, refs: PageRef[]): number {
    if (!refs.length) return -1;
    const pos = Math.max(0, Math.min(at, this.pages.length));
    this.push();
    this.pages = [...this.pages.slice(0, pos), ...refs, ...this.pages.slice(pos)];
    return pos;
  }

  /** Forget the history (after a source was replaced). */
  clearHistory(): void {
    this.undoStack.length = 0;
    this.redoStack.length = 0;
  }
}
