// One markup surface (a PDF page or the image): the SVG overlay, the pointer
// tools, selection chrome and move/resize handles (markup.py on_press /
// on_drag / on_release and _draw_chrome), plus trackpad drawing (markup.py
// TouchpadDrawing, via Pointer Lock).
import type { Annotation, MarkupHost, StrokePoints } from "./api.ts";
import type { Markup } from "./controller.ts";
import {
  bbox, type Char, charsOf, drawAnnotation, HANDLE_CURSORS, type HandleName, handles, LINE_TYPES, NOTE_SIZE,
  recognise, resize, selectText, SHAPES, speedPressure, textHeight, TEXTMARK_TYPES, translate, triples,
} from "./geom.ts";
import { measure, primsToSvg, svgEl } from "./render.ts";
import { RelativePen } from "../signature/trackpad.ts";

type Pt = { x: number; y: number };
type Op =
  | { mode: "none" }
  | { mode: "move"; origs: Annotation[]; start: Pt; moved: boolean }
  | { mode: "resize"; orig: Annotation; handle: HandleName; start: Pt; moved: boolean; shift: boolean }
  | { mode: "ink"; ann: Annotation & { type: "ink" }; stroke: number[]; lastClient: Pt; lastT: number; pressure: number | null; pen: boolean }
  | { mode: "shape"; ann: Annotation; p0: Pt; startClient: Pt }
  | { mode: "textmark"; ann: Annotation & { quads: number[] }; p0: Pt }
  | { mode: "place"; p0: Pt; startClient: Pt };

const TEXT_TOOLS = new Set(["highlight", "underline", "strike"]);
const HIT_PX = 5;

export class SurfaceView {
  readonly root: SVGSVGElement;
  private under: SVGSVGElement;
  private content: SVGGElement;
  private underContent: SVGGElement;
  private items: SVGGElement;
  private underItems: SVGGElement;
  private preview: SVGGElement;
  private chrome: SVGGElement;
  private nodes = new Map<string, { ann: Annotation; g: SVGGElement; ug: SVGGElement; key: string }>();
  private op: Op | null = null;
  private pointerId: number | null = null;
  private chars: Promise<Char[]> | null = null;
  private charsNow: Char[] | null = null;
  private pad: { pen: RelativePen; rect: { x: number; y: number; w: number; h: number }; ann: Annotation & { type: "ink" } | null; finger: Pt | null; stroke: number[] | null } | null = null;
  private ac = new AbortController();
  private scaleNow = 1;
  private rot = 0;

  readonly ctrl: Markup;
  readonly host: MarkupHost;

  constructor(ctrl: Markup, host: MarkupHost) {
    this.ctrl = ctrl;
    this.host = host;
    this.under = svgEl("svg", { class: "mk-layer mk-under", "aria-hidden": "true" });
    this.underContent = svgEl("g");
    this.underItems = svgEl("g");
    this.underContent.append(this.underItems);
    this.under.append(this.underContent);

    this.root = svgEl("svg", { class: "mk-layer", "data-surface": host.surface, role: "img", "aria-label": "Markup" });
    this.content = svgEl("g");
    this.items = svgEl("g", { class: "mk-items" });
    this.preview = svgEl("g", { class: "mk-preview", "pointer-events": "none" });
    this.chrome = svgEl("g", { class: "mk-chrome" });
    this.content.append(this.items, this.preview, this.chrome);
    this.root.append(this.content);
    host.overlay.append(this.under, this.root);
    host.overlay.dataset.markupSurface = host.surface;

    const sig = { signal: this.ac.signal };
    this.root.addEventListener("pointerdown", (e) => this.down(e), sig);
    this.root.addEventListener("pointermove", (e) => this.move(e), sig);
    this.root.addEventListener("pointerup", (e) => this.up(e), sig);
    this.root.addEventListener("pointercancel", () => this.cancel(), sig);
    this.root.addEventListener("dblclick", (e) => this.dbl(e), sig);
    this.relayout();
    this.toolChanged();
  }

  destroy(): void {
    this.pad?.pen.stop();
    this.ac.abort();
    this.root.remove();
    this.under.remove();
    delete this.host.overlay.dataset.markupSurface;
  }

  get scale(): number { return this.scaleNow; }

  relayout(): void {
    const { width: W, height: H } = this.host;
    this.scaleNow = Math.max(this.host.scale(), 1e-6);
    this.rot = ((Math.round(this.host.rotation() / 90) % 4) + 4) % 4 * 90;
    const swap = this.rot === 90 || this.rot === 270;
    const vb = `0 0 ${swap ? H : W} ${swap ? W : H}`;
    const tf = { 0: "", 90: `matrix(0 1 -1 0 ${H} 0)`, 180: `matrix(-1 0 0 -1 ${W} ${H})`, 270: `matrix(0 -1 1 0 0 ${W})` }[this.rot] ?? "";
    for (const [svg, g] of [[this.root, this.content], [this.under, this.underContent]] as const) {
      svg.setAttribute("viewBox", vb);
      svg.setAttribute("preserveAspectRatio", "none");
      if (tf) g.setAttribute("transform", tf); else g.removeAttribute("transform");
    }
    // Hit tolerances and handle sizes are in screen pixels: rebuild everything.
    this.nodes.forEach((n) => { n.g.remove(); n.ug.remove(); });
    this.nodes.clear();
    this.render();
  }

  toolChanged(): void {
    const t = this.ctrl.tool;
    this.root.classList.toggle("mk-drawing", t !== "select");
    this.root.classList.toggle("mk-text-tool", TEXT_TOOLS.has(t) || t === "text");
  }

  // ------------------------------------------------------------- render ---
  render(): void {
    const anns = this.ctrl.shown(this.host.surface);
    const editing = this.ctrl.editing?.id;
    const seen = new Set<string>();
    let prevNode: SVGGElement | null = null;
    let prevUnder: SVGGElement | null = null;
    for (const a of anns) {
      seen.add(a.id);
      const key = a.id === editing ? "editing" : "";
      let n = this.nodes.get(a.id);
      if (!n || n.ann !== a || n.key !== key) {
        n?.g.remove(); n?.ug.remove();
        const g = svgEl("g", { "data-id": a.id, "data-type": a.type });
        const ug = svgEl("g", { "data-id": a.id });
        if (a.type === "note") g.classList.add("mk-note");
        const prims = drawAnnotation(a, measure).filter((p) => !(key === "editing" && p.t === "text"));
        primsToSvg(prims, a.style.opacity ?? 1, g, ug);
        g.append(this.hitProxy(a));
        n = { ann: a, g, ug, key };
        this.nodes.set(a.id, n);
      }
      // keep z-order = model order
      if ((prevNode ? prevNode.nextSibling : this.items.firstChild) !== n.g) {
        if (prevNode) prevNode.after(n.g); else this.items.prepend(n.g);
      }
      if ((prevUnder ? prevUnder.nextSibling : this.underItems.firstChild) !== n.ug) {
        if (prevUnder) prevUnder.after(n.ug); else this.underItems.prepend(n.ug);
      }
      prevNode = n.g;
      prevUnder = n.ug;
    }
    for (const [id, n] of this.nodes) if (!seen.has(id)) { n.g.remove(); n.ug.remove(); this.nodes.delete(id); }

    this.preview.replaceChildren();
    const pv = this.ctrl.previews.get(this.host.surface);
    if (pv) {
      const g = svgEl("g");
      primsToSvg(drawAnnotation(pv, measure).filter((p) => !(pv.id === editing && p.t === "text")), pv.style.opacity ?? 1, g);
      this.preview.append(g);
    }
    if (this.pad) this.drawPad();
    this.drawChrome();
  }

  private hitProxy(a: Annotation): SVGElement {
    const tol = HIT_PX / this.scaleNow;
    let el: SVGElement;
    if (a.type === "line" || a.type === "arrow") {
      el = svgEl("path", { d: `M${a.x1} ${a.y1}L${a.x2} ${a.y2}`, stroke: "transparent", fill: "none",
        "stroke-width": a.style.width + 2 * tol, "stroke-linecap": "round", "pointer-events": "stroke" });
    } else if (a.type === "ink") {
      const d = a.strokes.map((s) => {
        const t = triples(s);
        return t.length === 1 ? `M${t[0]![0]} ${t[0]![1]}l0.01 0` : t.map((p, i) => `${i ? "L" : "M"}${p[0]} ${p[1]}`).join("");
      }).join("");
      el = svgEl("path", { d, stroke: "transparent", fill: "none", "stroke-width": a.style.width * 2 + 2 * tol,
        "stroke-linecap": "round", "stroke-linejoin": "round", "pointer-events": "stroke" });
    } else if (TEXTMARK_TYPES.has(a.type) && "quads" in a) {
      const q = a.quads;
      let d = "";
      for (let i = 0; i + 7 < q.length; i += 8) d += `M${q[i]} ${q[i + 1]}L${q[i + 2]} ${q[i + 3]}L${q[i + 4]} ${q[i + 5]}L${q[i + 6]} ${q[i + 7]}Z`;
      el = svgEl("path", { d, fill: "transparent", "pointer-events": "all" });
    } else {
      const b = bbox(a);
      el = svgEl("rect", { x: b.x - tol, y: b.y - tol, width: b.w + 2 * tol, height: b.h + 2 * tol, fill: "transparent", "pointer-events": "all" });
    }
    el.classList.add("mk-hit");
    return el;
  }

  private drawChrome(): void {
    this.chrome.replaceChildren();
    const sel = this.ctrl.selected().filter((a) => a.surface === this.host.surface);
    if (!sel.length) return;
    const px = 1 / this.scaleNow;
    for (const a of sel) {
      const { x, y, w, h } = bbox(a);
      if (!LINE_TYPES.has(a.type)) {
        const m = 3 * px;
        this.chrome.append(svgEl("rect", { class: "mk-sel-outline", x: x - m, y: y - m, width: w + 2 * m, height: h + 2 * m,
          "stroke-width": 1.5 * px, "pointer-events": "none" }));
      }
    }
    const a = sel[0]!;
    if (sel.length === 1) {
      for (const [name, hx, hy] of handles(a)) {
        const c = svgEl("circle", { class: "mk-handle", cx: hx, cy: hy, r: 5 * px, "stroke-width": 1.5 * px,
          "data-handle": name, "pointer-events": "all" });
        c.style.cursor = HANDLE_CURSORS[name];
        this.chrome.append(c);
      }
    }
    // delete badge outside the top-left corner of the (first) selected item
    const { x, y } = bbox(a);
    const bx = x - 12 * px, by = y - 12 * px, d = 3.2 * px;
    const badge = svgEl("circle", { class: "mk-delete", cx: bx, cy: by, r: 8 * px, "data-handle": "delete", "pointer-events": "all" });
    const title = svgEl("title");
    title.textContent = "Delete";
    badge.append(title);
    this.chrome.append(badge, svgEl("path", { class: "mk-delete-x", "stroke-width": 1.6 * px,
      d: `M${bx - d} ${by - d}L${bx + d} ${by + d}M${bx + d} ${by - d}L${bx - d} ${by + d}` }));
  }

  // -------------------------------------------------------- coordinates ---
  /** Client point -> surface units. */
  toSurface(cx: number, cy: number): Pt {
    const m = this.content.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    const p = new DOMPoint(cx, cy).matrixTransform(m.inverse());
    return { x: p.x, y: p.y };
  }

  /** Surface units -> client point. */
  toClient(x: number, y: number): Pt {
    const m = this.content.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    const p = new DOMPoint(x, y).matrixTransform(m);
    return { x: p.x, y: p.y };
  }

  /** Client-space bounding rect of a surface box. */
  clientRect(b: { x: number; y: number; w: number; h: number }): DOMRect {
    const pts = [this.toClient(b.x, b.y), this.toClient(b.x + b.w, b.y), this.toClient(b.x, b.y + b.h), this.toClient(b.x + b.w, b.y + b.h)];
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    return new DOMRect(Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
  }

  /** The part of the surface inside the viewport, in surface units. */
  visibleRect(): { x: number; y: number; w: number; h: number } {
    const { width: W, height: H } = this.host;
    const r = this.root.getBoundingClientRect();
    const l = Math.max(r.left, 0), t = Math.max(r.top, 0), rr = Math.min(r.right, innerWidth), b = Math.min(r.bottom, innerHeight);
    if (rr <= l || b <= t) return { x: 0, y: 0, w: W, h: H };
    const pts = [this.toSurface(l, t), this.toSurface(rr, t), this.toSurface(l, b), this.toSurface(rr, b)];
    const xs = pts.map((p) => Math.min(Math.max(p.x, 0), W)), ys = pts.map((p) => Math.min(Math.max(p.y, 0), H));
    const x = Math.min(...xs), y = Math.min(...ys);
    return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
  }

  private clampPt(p: Pt): Pt {
    return { x: Math.min(Math.max(p.x, 0), this.host.width), y: Math.min(Math.max(p.y, 0), this.host.height) };
  }

  // ------------------------------------------------------------ pointer ---
  private down(e: PointerEvent): void {
    if (e.button !== 0 || this.pad || this.op) return;
    const ctrl = this.ctrl;
    const tool = ctrl.tool;
    const target = e.target as Element;
    const p = this.toSurface(e.clientX, e.clientY);
    ctrl.lastSurface = this.host.surface;
    ctrl.editing?.finish();

    if (tool === "select") {
      const handle = target.closest("[data-handle]")?.getAttribute("data-handle");
      if (handle === "delete") { e.preventDefault(); ctrl.deleteSelected(); return; }
      const sel = ctrl.selected();
      if (handle && sel.length === 1) {
        this.begin(e, { mode: "resize", orig: sel[0]!, handle: handle as HandleName, start: p, moved: false, shift: e.shiftKey });
        return;
      }
      const id = target.closest("[data-id]")?.getAttribute("data-id");
      if (!id) { ctrl.select([]); return; }
      if (e.shiftKey) {
        const s = ctrl.selection;
        ctrl.select(s.includes(id) ? s.filter((x) => x !== id) : [...s, id]);
        e.preventDefault();
        return;
      }
      if (!ctrl.selection.includes(id)) ctrl.select([id]);
      this.begin(e, { mode: "move", origs: ctrl.selected(), start: p, moved: false });
      return;
    }

    const q = this.clampPt(p);
    const surface = this.host.surface;
    if (tool === "sketch" || tool === "draw") {
      const pen = e.pointerType === "pen" && e.pressure > 0;
      const pr = tool === "sketch" ? 0.5 : pen ? e.pressure : speedPressure(null, 0, 1000);
      const stroke = [q.x, q.y, pr];
      const ann = ctrl.make({ type: "ink", surface, strokes: [stroke], style: ctrl.styleFor("ink") }) as Annotation & { type: "ink" };
      ctrl.setPreview(surface, ann);
      this.begin(e, { mode: "ink", ann, stroke, lastClient: { x: e.clientX, y: e.clientY }, lastT: e.timeStamp, pressure: pr, pen });
      return;
    }
    if (TEXT_TOOLS.has(tool)) {
      const type = tool as "highlight" | "underline" | "strike";
      const ann = ctrl.make({ type, surface, quads: [], style: ctrl.styleFor(type) }) as Annotation & { quads: number[] };
      this.loadChars();
      this.begin(e, { mode: "textmark", ann, p0: q });
      return;
    }
    if (tool === "text" || tool === "note" || tool === "signature") {
      this.begin(e, { mode: "place", p0: q, startClient: { x: e.clientX, y: e.clientY } });
      return;
    }
    if ((SHAPES as readonly string[]).includes(tool)) {
      const type = tool as typeof SHAPES[number];
      const ann = type === "line" || type === "arrow"
        ? ctrl.make({ type, surface, x1: q.x, y1: q.y, x2: q.x, y2: q.y, style: ctrl.styleFor(type) })
        : ctrl.make({ type, surface, x: q.x, y: q.y, w: 0, h: 0, style: ctrl.styleFor(type) });
      ctrl.setPreview(surface, ann);
      this.begin(e, { mode: "shape", ann, p0: q, startClient: { x: e.clientX, y: e.clientY } });
    }
  }

  private begin(e: PointerEvent, op: Op): void {
    e.preventDefault();
    this.op = op;
    this.pointerId = e.pointerId;
    try { this.root.setPointerCapture(e.pointerId); } catch { /* synthetic events */ }
  }

  private move(e: PointerEvent): void {
    const op = this.op;
    if (!op || e.pointerId !== this.pointerId) return;
    const ctrl = this.ctrl;
    const { width: W, height: H } = this.host;
    if (op.mode === "move" || op.mode === "resize") {
      const p = this.toSurface(e.clientX, e.clientY);
      let dx = p.x - op.start.x, dy = p.y - op.start.y;
      if (!op.moved) {
        if (Math.hypot(dx, dy) * this.scaleNow < 2) return;
        op.moved = true;
      }
      if (op.mode === "move") {
        const movable = op.origs.filter((a) => !TEXTMARK_TYPES.has(a.type));
        if (!movable.length) return;
        // clamp the union box to the surface
        const bs = movable.map(bbox);
        const x0 = Math.min(...bs.map((b) => b.x)), y0 = Math.min(...bs.map((b) => b.y));
        const x1 = Math.max(...bs.map((b) => b.x + b.w)), y1 = Math.max(...bs.map((b) => b.y + b.h));
        dx = Math.min(Math.max(dx, -x0), W - x1);
        dy = Math.min(Math.max(dy, -y0), H - y1);
        for (const a of movable) ctrl.live.set(a.id, { ...a, ...translate(a, dx, dy) } as Annotation);
        ctrl.renderSurface(this.host.surface);
      } else {
        ctrl.setLive({ ...op.orig, ...resize(op.orig, op.handle, dx, dy, e.shiftKey || op.shift, W, H, measure) } as Annotation);
      }
      return;
    }
    const surface = this.host.surface;
    if (op.mode === "ink") {
      const evs = e.getCoalescedEvents?.().length ? e.getCoalescedEvents() : [e];
      for (const ev of evs) {
        if (Math.hypot(ev.clientX - op.lastClient.x, ev.clientY - op.lastClient.y) < 1.5) continue;
        const q = this.clampPt(this.toSurface(ev.clientX, ev.clientY));
        const dt = Math.max((ev.timeStamp - op.lastT) / 1000, 0.004);
        const v = Math.hypot(ev.clientX - op.lastClient.x, ev.clientY - op.lastClient.y) / dt;
        const pr = ctrl.tool === "sketch" ? 0.5 : op.pen ? (ev.pressure || 0.5) : speedPressure(op.pressure, v, 1000);
        op.pressure = pr;
        op.stroke.push(q.x, q.y, pr);
        op.lastClient = { x: ev.clientX, y: ev.clientY };
        op.lastT = ev.timeStamp;
      }
      op.ann = { ...op.ann, strokes: [op.stroke.slice()] };
      ctrl.setPreview(surface, op.ann);
      return;
    }
    const q = this.clampPt(this.toSurface(e.clientX, e.clientY));
    if (op.mode === "textmark") {
      const sel = this.charsNow?.length ? selectText(this.charsNow, op.p0.x, op.p0.y, q.x, q.y) : null;
      if (sel) op.ann = { ...op.ann, quads: sel.quads, text: sel.text } as typeof op.ann;
      else if (!this.charsNow?.length) {
        // No text layer (e.g. an image without OCR): mark the dragged rectangle.
        const x0 = Math.min(op.p0.x, q.x), x1 = Math.max(op.p0.x, q.x), y0 = Math.min(op.p0.y, q.y), y1 = Math.max(op.p0.y, q.y);
        op.ann = { ...op.ann, quads: x1 - x0 > 1 && y1 - y0 > 1 ? [x0, y0, x1, y0, x1, y1, x0, y1] : [] } as typeof op.ann;
      }
      ctrl.setPreview(surface, op.ann);
      return;
    }
    if (op.mode === "shape") {
      const a = op.ann;
      const { x: x0, y: y0 } = op.p0;
      let { x, y } = q;
      if (a.type === "line" || a.type === "arrow") {
        if (e.shiftKey) { // snap to 45°
          const ang = Math.round(Math.atan2(y - y0, x - x0) / (Math.PI / 4)) * Math.PI / 4;
          const L = Math.hypot(x - x0, y - y0);
          x = x0 + Math.cos(ang) * L; y = y0 + Math.sin(ang) * L;
        }
        op.ann = { ...a, x2: x, y2: y } as Annotation;
      } else {
        let w = x - x0, h = y - y0;
        if (e.shiftKey) { const m = Math.max(Math.abs(w), Math.abs(h)); w = Math.sign(w || 1) * m; h = Math.sign(h || 1) * m; }
        op.ann = { ...a, x: Math.min(x0, x0 + w), y: Math.min(y0, y0 + h), w: Math.abs(w), h: Math.abs(h) } as Annotation;
      }
      ctrl.setPreview(surface, op.ann);
    }
  }

  private up(e: PointerEvent): void {
    const op = this.op;
    if (!op || e.pointerId !== this.pointerId) return;
    this.move(e);
    this.op = null;
    this.pointerId = null;
    try { this.root.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    const ctrl = this.ctrl;
    const surface = this.host.surface;
    const { width: W, height: H } = this.host;
    if (op.mode === "none") return;
    if (op.mode === "move" || op.mode === "resize") {
      if (op.moved) ctrl.commitLive(); else ctrl.cancelLive();
      return;
    }
    ctrl.setPreview(surface, null);
    if (op.mode === "ink") {
      let a: Annotation = op.ann;
      if (ctrl.tool === "sketch" && op.ann.strokes.length === 1) {
        const r = recognise(op.ann.strokes[0]!);
        if (r) a = ctrl.make({ ...r, surface, style: ctrl.styleFor(r.type) } as Parameters<Markup["make"]>[0]);
      }
      ctrl.addFromTool(a);
      return;
    }
    if (op.mode === "textmark") {
      if (op.ann.quads.length) ctrl.addFromTool(op.ann);
      return;
    }
    if (op.mode === "place") {
      const { x, y } = op.p0;
      if (ctrl.tool === "note") {
        const a = ctrl.make({ type: "note", surface, x: Math.min(x, W - NOTE_SIZE), y: Math.min(y, H - NOTE_SIZE), text: "", style: ctrl.styleFor("note") });
        ctrl.edit(a, true);
      } else if (ctrl.tool === "text") {
        const w = Math.max(Math.min(220, W - x), 40);
        const base = { type: "text" as const, surface, x: Math.min(x, Math.max(W - w, 0)), y, w, h: 0, text: "Text", style: ctrl.styleFor("text") };
        const a = ctrl.make({ ...base, h: textHeight(base, measure) });
        ctrl.edit(a, true);
      } else if (ctrl.tool === "signature" && ctrl.lastSignature) {
        const sig = ctrl.lastSignature;
        const placed = ctrl.placeSignature(sig, surface) as (Annotation & { type: "signature" }) | null;
        if (placed) ctrl.update(placed.id, { x: Math.min(x, Math.max(W - placed.w, 0)), y: Math.min(y, Math.max(H - placed.h, 0)) } as Partial<Annotation>);
      }
      return;
    }
    if (op.mode === "shape") {
      let a = op.ann;
      if (Math.hypot(e.clientX - op.startClient.x, e.clientY - op.startClient.y) < 4) {
        // a click: default-size shape
        const { x: x0, y: y0 } = op.p0;
        if (a.type === "line" || a.type === "arrow") a = { ...a, x2: Math.min(x0 + 100, W), y2: y0 };
        else if ("w" in a) {
          const [w, h] = a.type === "star" ? [80, 80] : [100, 70];
          a = { ...a, x: Math.max(0, Math.min(x0, W - w)), y: Math.max(0, Math.min(y0, H - h)), w, h } as Annotation;
        }
      }
      ctrl.addFromTool(a);
    }
  }

  private cancel(): void {
    if (!this.op) return;
    this.op = null;
    this.pointerId = null;
    this.ctrl.cancelLive();
    this.ctrl.setPreview(this.host.surface, null);
  }

  private dbl(e: MouseEvent): void {
    if (this.ctrl.tool !== "select") return;
    const id = (e.target as Element).closest("[data-id]")?.getAttribute("data-id");
    const a = id ? this.ctrl.get(id) : undefined;
    if (a && (a.type === "text" || a.type === "note")) {
      e.preventDefault();
      this.ctrl.select([a.id]);
      this.ctrl.edit(a, false);
    }
  }

  private loadChars(): void {
    if (this.chars || !this.host.textRuns) return;
    this.chars = this.host.textRuns().then((runs) => (this.charsNow = charsOf(runs))).catch(() => (this.charsNow = []));
  }

  // ----------------------------------------------------------- trackpad ---
  /** Draw ink with the trackpad mapped onto the visible part of this surface. */
  async startTrackpad(): Promise<boolean> {
    if (this.pad) { this.stopTrackpad(); return false; }
    const vis = this.visibleRect();
    const mx = vis.w * 0.04, my = vis.h * 0.04;
    const rect = { x: vis.x + mx, y: vis.y + my, w: vis.w - 2 * mx, h: vis.h - 2 * my };
    const ctrl = this.ctrl;
    const surface = this.host.surface;
    let pressure: number | null = null;
    const pen = new RelativePen({
      target: this.root,
      gain: 1 / this.scaleNow,
      bounds: () => ({ x0: rect.x, y0: rect.y, x1: rect.x + rect.w, y1: rect.y + rect.h }),
      onDown: (x, y) => {
        const pad = this.pad;
        if (!pad) return;
        pressure = speedPressure(null, 0, 1000);
        pad.stroke = [x, y, pressure];
        const strokes = [...(pad.ann?.strokes ?? []), pad.stroke] as StrokePoints[];
        const st = ctrl.styleFor("ink");
        pad.ann = pad.ann ? { ...pad.ann, strokes } : ctrl.make({ type: "ink", surface, strokes, style: { ...st, width: Math.max(st.width * 0.75, 0.6) } }) as Annotation & { type: "ink" };
        ctrl.setPreview(surface, pad.ann);
      },
      onMove: (x, y, down, speed) => {
        const pad = this.pad;
        if (!pad) return;
        pad.finger = { x, y };
        if (down && pad.stroke && pad.ann) {
          const n = pad.stroke.length;
          if (Math.hypot(x - pad.stroke[n - 3]!, y - pad.stroke[n - 2]!) * this.scaleNow >= 1) {
            pressure = speedPressure(pressure, speed, 1000);
            pad.stroke.push(x, y, pressure);
            pad.ann = { ...pad.ann, strokes: [...pad.ann.strokes.slice(0, -1), pad.stroke.slice()] };
            ctrl.setPreview(surface, pad.ann);
            return;
          }
        }
        this.render();
      },
      onUp: () => { if (this.pad) this.pad.stroke = null; },
      onUndo: () => {
        const pad = this.pad;
        if (!pad?.ann) return;
        const strokes = pad.ann.strokes.slice(0, -1);
        pad.ann = strokes.length ? { ...pad.ann, strokes } : null;
        ctrl.setPreview(surface, pad.ann);
      },
      onEnd: () => {
        const pad = this.pad;
        this.pad = null;
        this.root.classList.remove("mk-locked");
        ctrl.setPreview(surface, null);
        if (pad?.ann?.strokes.length) ctrl.addFromTool(pad.ann);
        ctrl.emit("tool");
      },
    });
    this.pad = { pen, rect, ann: null, finger: null, stroke: null };
    this.root.classList.add("mk-locked");
    const ok = await pen.start();
    if (!ok) {
      this.pad = null;
      this.root.classList.remove("mk-locked");
      this.render();
      return false;
    }
    ctrl.emit("tool");
    return true;
  }

  stopTrackpad(): boolean {
    if (!this.pad) return false;
    this.pad.pen.stop();
    return true;
  }

  get trackpadActive(): boolean { return !!this.pad; }

  private drawPad(): void {
    const pad = this.pad!;
    const px = 1 / this.scaleNow;
    const { x, y, w, h } = pad.rect;
    this.preview.append(svgEl("rect", { class: "mk-pad-rect", x, y, width: w, height: h, "stroke-width": 1.5 * px,
      "stroke-dasharray": `${6 * px} ${4 * px}` }));
    if (pad.finger) {
      this.preview.append(svgEl("circle", { class: "mk-pad-finger", cx: pad.finger.x, cy: pad.finger.y, r: 5 * px, "stroke-width": 1.5 * px }));
    }
  }
}

