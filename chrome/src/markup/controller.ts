// MarkupController: the annotation model (undo/redo, selection, events,
// serialization), tool state and commands. Port of preview/markup.py
// MarkupController; drawing and pointer handling per surface live in
// surface.ts, the toolbar in toolbar.ts, text/note editors in editors.ts.
import type {
  Annotation, MarkupController, MarkupEvent, MarkupHost, MarkupOptions, MarkupStyle, MarkupSurface, Tool,
} from "./api.ts";
import {
  bbox, DEFAULT_STROKE, defaultFont, HIGHLIGHT_COLOR, NOTE_COLOR, TEXTMARK_TYPES, textHeight, translate,
} from "./geom.ts";
import { measure, renderAnnotations } from "./render.ts";
import { SurfaceView } from "./surface.ts";
import { Toolbar } from "./toolbar.ts";
import { injectStyle } from "./style.ts";
import { editNote, editText, type Editor } from "./editors.ts";
import type { Signature } from "../signature/api.ts";
import { createSignatureStore, type StoredSignature } from "../signature/index.ts";

/** A new annotation without id/timestamps (distributive over the Annotation union). */
export type NewAnn = Annotation extends infer A ? A extends Annotation ? Omit<A, "id" | "created" | "modified"> & { id?: string } : never : never;
type Ev = MarkupEvent | "style" | "surfaces";

const STICKY: ReadonlySet<Tool> = new Set(["sketch", "draw", "highlight", "underline", "strike"]);
const UNDO_LIMIT = 200;
/** Shared between controllers like the desktop's module-level _CLIPBOARD. */
let clipboard: Annotation[] = [];

export class Markup implements MarkupController {
  readonly docKind: "pdf" | "image";
  readonly author?: string;
  private anns: Annotation[];
  private undoStack: { anns: Annotation[]; state: number }[] = [];
  private redoStack: { anns: Annotation[]; state: number }[] = [];
  private stateId = 0;
  private nextState = 1;
  private savedState = 0;
  private _tool: Tool = "select";
  private _style: MarkupStyle = { stroke: DEFAULT_STROKE, fill: null, width: 2, opacity: 1, font: defaultFont() };
  highlightColor = HIGHLIGHT_COLOR;
  private _selection: string[] = [];
  private listeners = new Map<Ev, Set<() => void>>();
  readonly views = new Set<SurfaceView>();
  /** Uncommitted versions of annotations during a drag, by id. */
  readonly live = new Map<string, Annotation>();
  /** Annotation being created (shown, not in the model yet), per surface. */
  readonly previews = new Map<string, Annotation>();
  editing: Editor | null = null;
  lastSurface: string | null = null;
  lastSignature: StoredSignature | null = null;
  private _toolbar: Toolbar | null = null;
  readonly signatures = createSignatureStore();
  private ac = new AbortController();

  constructor(opts: MarkupOptions) {
    this.docKind = opts.docKind;
    this.author = opts.author;
    this.anns = [...(opts.initial ?? [])];
    injectStyle();
    const sig = { signal: this.ac.signal };
    addEventListener("keydown", (e) => this.onKey(e), sig);
    // Clicking outside the markup (and its popovers) clears the selection.
    addEventListener("pointerdown", (e) => {
      if (!this._selection.length) return;
      const t = e.target as Element | null;
      if (!t?.closest) return;
      if (t.closest(".mk-layer, .markup-toolbar, .popover, dialog, .mk-text-editor")) return;
      this.select([]);
    }, { ...sig, capture: true });
  }

  // ------------------------------------------------------------ events ---
  on(ev: MarkupEvent, cb: () => void): () => void { return this.listen(ev, cb); }
  listen(ev: Ev, cb: () => void): () => void {
    let s = this.listeners.get(ev);
    if (!s) this.listeners.set(ev, (s = new Set()));
    s.add(cb);
    return () => s!.delete(cb);
  }
  emit(ev: Ev): void { this.listeners.get(ev)?.forEach((cb) => cb()); }

  // ----------------------------------------------------------- surfaces ---
  attach(host: MarkupHost): MarkupSurface {
    const v = new SurfaceView(this, host);
    this.views.add(v);
    this.emit("surfaces");
    return {
      relayout: () => v.relayout(),
      detach: () => { v.destroy(); this.views.delete(v); this.emit("surfaces"); },
    };
  }

  view(surface: string): SurfaceView | undefined {
    for (const v of this.views) if (v.host.surface === surface) return v;
    return undefined;
  }

  hasTextRuns(): boolean {
    for (const v of this.views) if (v.host.textRuns) return true;
    return false;
  }

  renderAll(): void { this.views.forEach((v) => v.render()); }
  renderSurface(surface: string): void { this.view(surface)?.render(); }

  toolbar(): HTMLElement {
    this._toolbar ??= new Toolbar(this);
    return this._toolbar.el;
  }

  // -------------------------------------------------------------- state ---
  get tool(): Tool { return this._tool; }
  set tool(t: Tool) { this.setTool(t); }
  setTool(t: Tool): void {
    if (t === this._tool) return;
    this.editing?.finish();
    this._tool = t;
    if (t !== "select") this.select([]);
    this.views.forEach((v) => v.toolChanged());
    this.emit("tool");
  }

  get style(): MarkupStyle { return this._style; }
  set style(s: MarkupStyle) { this._style = { ...s, font: s.font ? { ...s.font } : this._style.font }; this.emit("style"); }

  get selection(): string[] { return this._selection; }
  select(ids: string[]): void {
    const have = new Set(this.anns.map((a) => a.id));
    const next = ids.filter((id) => have.has(id));
    if (next.length === this._selection.length && next.every((id, i) => id === this._selection[i])) return;
    this._selection = next;
    if (next.length) {
      const a = this.get(next[0]!);
      if (a) this.lastSurface = a.surface;
    }
    this.renderAll();
    this.emit("selection");
  }

  get(id: string): Annotation | undefined { return this.live.get(id) ?? this.anns.find((a) => a.id === id); }
  selected(): Annotation[] { return this._selection.map((id) => this.get(id)).filter((a): a is Annotation => !!a); }

  /** Annotations of a surface as shown (live drag versions applied). */
  shown(surface: string): Annotation[] {
    return this.anns.filter((a) => a.surface === surface).map((a) => this.live.get(a.id) ?? a);
  }

  // -------------------------------------------------------------- model ---
  private commit(next: Annotation[]): void {
    this.undoStack.push({ anns: this.anns, state: this.stateId });
    if (this.undoStack.length > UNDO_LIMIT) this.undoStack.shift();
    this.redoStack.length = 0;
    this.anns = next;
    this.stateId = this.nextState++;
    this.afterChange();
  }

  private afterChange(): void {
    this.live.clear();
    const have = new Set(this.anns.map((a) => a.id));
    const sel = this._selection.filter((id) => have.has(id));
    const selChanged = sel.length !== this._selection.length;
    this._selection = sel;
    this.renderAll();
    this.emit("change");
    if (selChanged) this.emit("selection");
  }

  make<T extends NewAnn>(a: T): T & { id: string; created: number; modified: number } {
    const now = Date.now();
    return {
      ...a, id: a.id ?? crypto.randomUUID(), created: now, modified: now,
      ...(this.author && !a.author ? { author: this.author } : {}),
    };
  }

  list(surface?: string): Annotation[] { return surface ? this.anns.filter((a) => a.surface === surface) : [...this.anns]; }

  add(a: NewAnn): Annotation {
    const full = this.make(a);
    this.commit([...this.anns, full]);
    return full;
  }

  /** Add from a tool: like markup.py _add (switches to Select and selects it unless the tool is sticky). */
  addFromTool(full: Annotation): void {
    this.commit([...this.anns, full]);
    this.lastSurface = full.surface;
    if (!STICKY.has(this._tool)) {
      this.setTool("select");
      this.select([full.id]);
    }
  }

  update(id: string, patch: Partial<Annotation>): void {
    this.commit(this.anns.map((a) => (a.id === id ? ({ ...a, ...patch, id, modified: Date.now() } as Annotation) : a)));
  }

  /** Several patches as one undo step. */
  updateMany(patches: Map<string, Partial<Annotation>>): void {
    if (!patches.size) return;
    const now = Date.now();
    this.commit(this.anns.map((a) => {
      const p = patches.get(a.id);
      return p ? ({ ...a, ...p, id: a.id, modified: now } as Annotation) : a;
    }));
  }

  remove(ids: string[]): void {
    const drop = new Set(ids);
    if (!this.anns.some((a) => drop.has(a.id))) return;
    this.commit(this.anns.filter((a) => !drop.has(a.id)));
  }

  setLive(a: Annotation): void {
    this.live.set(a.id, a);
    this.renderSurface(a.surface);
  }

  /** Commit the live drag versions as one undo step. */
  commitLive(): void {
    if (!this.live.size) return;
    const now = Date.now();
    const live = new Map(this.live);
    this.commit(this.anns.map((a) => {
      const l = live.get(a.id);
      return l ? ({ ...l, modified: now } as Annotation) : a;
    }));
  }

  cancelLive(): void {
    const surfaces = new Set([...this.live.values()].map((a) => a.surface));
    this.live.clear();
    surfaces.forEach((s) => this.renderSurface(s));
  }

  setPreview(surface: string, a: Annotation | null): void {
    if (a) this.previews.set(surface, a); else this.previews.delete(surface);
    this.renderSurface(surface);
  }

  undo(): void {
    this.editing?.finish();
    const prev = this.undoStack.pop();
    if (!prev) return;
    this.redoStack.push({ anns: this.anns, state: this.stateId });
    this.anns = prev.anns;
    this.stateId = prev.state;
    this.afterChange();
  }

  redo(): void {
    this.editing?.finish();
    const next = this.redoStack.pop();
    if (!next) return;
    this.undoStack.push({ anns: this.anns, state: this.stateId });
    this.anns = next.anns;
    this.stateId = next.state;
    this.afterChange();
  }

  get canUndo(): boolean { return this.undoStack.length > 0; }
  get canRedo(): boolean { return this.redoStack.length > 0; }

  remapSurfaces(map: Record<string, string | null>): void {
    // Undoable like any edit, so a viewer that keeps one history can undo it with ctrl.undo().
    if (!this.anns.some((a) => a.surface in map)) return;
    this.commit(this.anns.flatMap((a) => {
      if (!(a.surface in map)) return [a];
      const to = map[a.surface];
      return to === null || to === undefined ? [] : [{ ...a, surface: to }];
    }));
  }

  get dirty(): boolean { return this.stateId !== this.savedState; }
  markSaved(): void { this.savedState = this.stateId; this.emit("change"); }

  toJSON(): Annotation[] { return structuredClone(this.anns); }

  render(ctx: CanvasRenderingContext2D, surface: string, unitsToPx: number): void {
    renderAnnotations(ctx, this.list(surface), unitsToPx);
  }

  destroy(): void {
    this.editing?.cancel();
    this.views.forEach((v) => v.destroy());
    this.views.clear();
    this.ac.abort();
    this._toolbar?.destroy();
    this.listeners.clear();
  }

  // ----------------------------------------------------------- commands ---
  /** Current style for a new annotation of `type`. */
  styleFor(type: Annotation["type"]): MarkupStyle {
    const s = this._style;
    if (type === "highlight") return { stroke: this.highlightColor, fill: null, width: 0, opacity: s.opacity };
    if (type === "underline" || type === "strike") return { stroke: s.stroke, fill: null, width: 0, opacity: s.opacity };
    if (type === "note") return { stroke: NOTE_COLOR, fill: null, width: 0, opacity: 1 };
    if (type === "text") return { stroke: s.stroke, fill: s.fill, width: 0, opacity: s.opacity, font: { ...(s.font ?? defaultFont()) } };
    if (type === "ink" || type === "line" || type === "arrow") return { stroke: s.stroke, fill: null, width: s.width, opacity: s.opacity };
    return { stroke: s.stroke, fill: s.fill, width: s.width, opacity: s.opacity };
  }

  /** Change the current style and apply it to the selection (markup.py set_style). */
  applyStyle(p: Partial<MarkupStyle>): void {
    const sel = this.selected();
    const hl = this._tool === "highlight" || (sel.length > 0 && sel.every((a) => a.type === "highlight"));
    if (p.stroke !== undefined && hl) this.highlightColor = p.stroke;
    else this._style = { ...this._style, ...p, font: p.font ? { ...p.font } : this._style.font };
    const patches = new Map<string, Partial<Annotation>>();
    for (const a of sel) {
      const st: MarkupStyle = { ...a.style };
      if (p.stroke !== undefined) st.stroke = p.stroke;
      if (p.fill !== undefined && ["rect", "rrect", "oval", "bubble", "star", "text"].includes(a.type)) st.fill = p.fill;
      if (p.width !== undefined && !["text", "note", "signature", "highlight", "underline", "strike"].includes(a.type)) st.width = p.width;
      if (p.opacity !== undefined && a.type !== "note") st.opacity = p.opacity;
      const patch: Partial<Annotation> = { style: st };
      if (p.font && a.type === "text") {
        st.font = { ...p.font };
        (patch as { h: number }).h = textHeight({ ...a, style: st }, measure);
      }
      patches.set(a.id, patch);
    }
    this.updateMany(patches);
    this.emit("style");
  }

  deleteSelected(): boolean {
    if (!this._selection.length) return false;
    this.remove(this._selection);
    return true;
  }

  copySelected(): boolean {
    const sel = this.selected();
    if (!sel.length) return false;
    clipboard = structuredClone(sel);
    return true;
  }

  cutSelected(): boolean { return this.copySelected() && this.deleteSelected(); }

  paste(surface = this.lastSurface ?? this.pickSurface()): boolean {
    if (!clipboard.length || !surface) return false;
    const v = this.view(surface);
    const W = v?.host.width ?? Infinity, H = v?.host.height ?? Infinity;
    const added: Annotation[] = [];
    const next: Annotation[] = [];
    for (const c of clipboard) {
      let a = { ...structuredClone(c), surface } as Annotation;
      if (!TEXTMARK_TYPES.has(a.type)) {
        const b = bbox(a);
        const dx = b.x + b.w + 12 <= W ? 12 : -b.x;
        const dy = b.y + b.h + 12 <= H ? 12 : -b.y;
        a = { ...a, ...translate(a, dx, dy) } as Annotation;
      }
      next.push(a);
      added.push(this.make({ ...a, id: undefined } as NewAnn));
    }
    clipboard = next;
    this.commit([...this.anns, ...added]);
    this.setTool("select");
    this.select(added.map((a) => a.id));
    return true;
  }

  nudge(dx: number, dy: number): boolean {
    const sel = this.selected().filter((a) => !TEXTMARK_TYPES.has(a.type));
    if (!sel.length) return false;
    const patches = new Map<string, Partial<Annotation>>();
    for (const a of sel) {
      const v = this.view(a.surface);
      const b = bbox(a);
      const W = v?.host.width ?? Infinity, H = v?.host.height ?? Infinity;
      const ddx = Math.min(Math.max(dx, -b.x), W - b.x - b.w);
      const ddy = Math.min(Math.max(dy, -b.y), H - b.y - b.h);
      patches.set(a.id, translate(a, ddx, ddy));
    }
    this.updateMany(patches);
    return true;
  }

  escape(): boolean {
    for (const v of this.views) if (v.stopTrackpad()) return true;
    if (this._selection.length) { this.select([]); return true; }
    if (this._tool !== "select") { this.setTool("select"); return true; }
    return false;
  }

  /** Open the text box / note editor (double click, or right after placing). */
  edit(a: Annotation, isNew: boolean): void {
    const v = this.view(a.surface);
    if (!v) return;
    this.editing?.finish();
    if (a.type === "text") this.editing = editText(this, v, a, isNew);
    else if (a.type === "note") this.editing = editNote(this, v, a, isNew);
  }

  /** Surface the user most likely means: the most visible one, else the last used. */
  pickSurface(): string | null {
    let best: string | null = null, area = -1;
    for (const v of this.views) {
      const r = v.host.overlay.getBoundingClientRect();
      const w = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
      const h = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
      const s = w * h + (v.host.surface === this.lastSurface ? 1 : 0);
      if (s > area) { area = s; best = v.host.surface; }
    }
    return best;
  }

  /** Place a signature on the visible surface (pdf_view.py place_signature). */
  placeSignature(sig: Signature, surface = this.pickSurface()): Annotation | null {
    if (!surface) return null;
    const v = this.view(surface);
    if (!v) return null;
    const { width: W, height: H } = v.host;
    const st = sig as StoredSignature;
    const w = this.docKind === "pdf" ? Math.min(160, 0.35 * W) : Math.min(0.3 * W, 0.6 * H * sig.aspect);
    const h = w / Math.max(sig.aspect, 1e-3);
    const vis = v.visibleRect();
    let cy = H * 0.78;
    if (!(vis.y + h / 2 <= cy && cy <= vis.y + vis.h - h / 2)) cy = vis.y + vis.h * 0.7;
    const a = this.make({
      type: "signature", surface, signatureId: sig.id,
      x: Math.max(0, (W - w) / 2), y: Math.min(Math.max(cy - h / 2, 0), Math.max(H - h, 0)), w, h,
      strokes: structuredClone(sig.strokes),
      style: { stroke: st.color ?? "#141414", fill: null, width: (st.pen ?? 0.01) * w, opacity: 1 },
    } as NewAnn);
    this.lastSignature = st;
    this.commit([...this.anns, a]);
    this.setTool("select");
    this.select([a.id]);
    return a;
  }

  async newSignature(): Promise<void> {
    const sig = await this.signatures.capture();
    if (sig) this.placeSignature(sig);
  }

  startTrackpad(): Promise<boolean> {
    const s = this.pickSurface();
    const v = s ? this.view(s) : undefined;
    return v ? v.startTrackpad() : Promise.resolve(false);
  }

  // ----------------------------------------------------------- keyboard ---
  private onKey(e: KeyboardEvent): void {
    if (e.defaultPrevented || !this.views.size) return;
    const t = e.target as HTMLElement | null;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (document.querySelector("dialog[open]")) return;
    const mod = /Mac|iPhone|iPad/.test(navigator.platform) ? e.metaKey : e.ctrlKey;
    const k = e.key;
    let done = false;
    if (k === "Escape" && !mod) done = this.escape();
    else if ((k === "Delete" || k === "Backspace") && !mod) done = this.deleteSelected();
    else if (k.startsWith("Arrow") && !mod && this._selection.length) {
      const step = e.shiftKey ? 10 : 1;
      const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[k];
      if (d) done = this.nudge(d[0]!, d[1]!);
    } else if (mod && !e.altKey) {
      const key = k.toLowerCase();
      if (key === "z" && !e.shiftKey && this.canUndo) { this.undo(); done = true; }
      else if (((key === "z" && e.shiftKey) || key === "y") && this.canRedo) { this.redo(); done = true; }
      else if (key === "c" && !e.shiftKey) done = this.copySelected();
      else if (key === "x" && !e.shiftKey) done = this.cutSelected();
      else if (key === "v" && !e.shiftKey) done = this.paste();
    }
    if (done) { e.preventDefault(); e.stopPropagation(); }
  }
}
