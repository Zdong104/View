// The image canvas (image_view.ImageCanvas): one displayed bitmap with fit/actual
// zoom, pan, ctrl+wheel and touch pinch, a rectangular/elliptical selection with
// handles, and Instant Alpha clicks. Geometry is in pixels of the rendered image
// (after the op list); the displayed bitmap may be smaller (it is scaled to fit).
// The canvas, Live Text layer and markup overlay share one on-screen box.
import { el } from "../../ui/kit.ts";

export type CanvasTool = "pan" | "rect" | "ellipse" | "alpha";
export type Sel = [x0: number, y0: number, x1: number, y1: number];

const MAX_ZOOM = 32;
const HANDLE = 7; // CSS px hit radius of selection handles
const HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"] as const;

export class ImageCanvas {
  readonly viewport = el("div.img-viewport", { tabindex: 0, role: "img", "aria-label": "Image" });
  readonly canvas = el("canvas.img-canvas");
  readonly textLayer = el("div.img-text");
  readonly overlay = el("div.img-overlay");
  private selEl = el("div.img-sel", { hidden: true });
  private ctx2d = this.canvas.getContext("2d")!;

  iw = 0;
  ih = 0;
  fit = true;
  zoom = 1;   // CSS px per image px
  ox = 0;     // image-centre offset from the view centre (CSS px)
  oy = 0;
  tool: CanvasTool = "pan";
  sel: Sel | null = null;
  hasContent = false;

  onView: () => void = () => {};
  onSel: () => void = () => {};
  onAlpha: (x: number, y: number) => void = () => {};
  /** True while the markup layer owns pointer input (drawing tools). */
  markupActive: (target: EventTarget | null) => boolean = () => false;

  private drag: null | { mode: "pan"; x: number; y: number; ox: number; oy: number }
    | { mode: "sel"; h: string; x: number; y: number; r: Sel }
    | { mode: "alpha"; x: number; y: number } = null;
  private touches = new Map<number, { x: number; y: number }>();
  private pinch: { d0: number; z0: number; mx: number; my: number; ox: number; oy: number } | null = null;

  constructor(signal: AbortSignal) {
    this.selEl.append(...HANDLES.map((h) => el("div.img-handle", { "data-h": h })));
    this.viewport.append(this.canvas, this.textLayer, this.overlay, this.selEl);
    const on = <K extends keyof HTMLElementEventMap>(t: K, fn: (e: HTMLElementEventMap[K]) => void, o: AddEventListenerOptions = {}) =>
      this.viewport.addEventListener(t, fn as EventListener, { signal, ...o });
    on("pointerdown", (e) => this.down(e));
    on("pointermove", (e) => this.move(e));
    on("pointerup", (e) => this.up(e));
    on("pointercancel", (e) => { this.touches.delete(e.pointerId); this.pinch = null; this.drag = null; });
    on("wheel", (e) => this.wheel(e), { passive: false });
    on("dblclick", (e) => this.dbl(e));
    let resizeFrame = 0;  // lay out on the next frame, not inside the observer
    new ResizeObserver(() => {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => this.layout());
    }).observe(this.viewport);
  }

  // ---- content ---------------------------------------------------------------

  /** Show `bmp` as the rendered image of size iw × ih (bmp may be scaled down). */
  setContent(bmp: CanvasImageSource & { width: number; height: number }, iw: number, ih: number, keepView: boolean): void {
    if (iw !== this.iw || ih !== this.ih) keepView = false;
    this.iw = iw; this.ih = ih;
    this.hasContent = true;
    if (!keepView) {
      this.fit = true;
      this.ox = this.oy = 0;
      this.sel = null;
    }
    this.draw(bmp);
    this.layout();
  }

  /** Draw a frame (animation) or a preview into the canvas without touching the view. */
  draw(bmp: CanvasImageSource & { width: number; height: number }): void {
    if (this.canvas.width !== bmp.width || this.canvas.height !== bmp.height) {
      this.canvas.width = bmp.width;
      this.canvas.height = bmp.height;
    } else {
      this.ctx2d.clearRect(0, 0, bmp.width, bmp.height);
    }
    this.ctx2d.drawImage(bmp, 0, 0);
    this.updateRendering();
  }

  clear(): void {
    this.hasContent = false;
    this.iw = this.ih = 0;
    this.canvas.width = this.canvas.height = 0;
    this.sel = null;
    this.layout();
  }

  setTool(t: CanvasTool): void {
    this.tool = t;
    if (t !== "rect" && t !== "ellipse") this.setSel(null);
    this.viewport.dataset.tool = t;
    this.layout();
  }

  setSel(s: Sel | null): void {
    this.sel = s;
    this.layout();
    this.onSel();
  }

  // ---- geometry ------------------------------------------------------------------

  private get vw() { return this.viewport.clientWidth; }
  private get vh() { return this.viewport.clientHeight; }

  /** Zoom that fits the image in the view, never above 100 %. */
  fitZoom(): number {
    if (!this.iw || !this.ih || !this.vw || !this.vh) return 1;
    return Math.min(this.vw / this.iw, this.vh / this.ih, 1);
  }

  effZoom(): number { return this.fit ? this.fitZoom() : this.zoom; }

  canPan(): boolean {
    const s = this.effZoom();
    return this.iw * s > this.vw + 0.5 || this.ih * s > this.vh + 0.5;
  }

  private clamp(): void {
    const s = this.effZoom();
    const mx = Math.max(0, (this.iw * s - this.vw) / 2), my = Math.max(0, (this.ih * s - this.vh) / 2);
    this.ox = Math.min(mx, Math.max(-mx, this.ox));
    this.oy = Math.min(my, Math.max(-my, this.oy));
  }

  zoomTo(z: number, ax = this.vw / 2, ay = this.vh / 2): void {
    if (!this.hasContent) return;
    const fz = this.fitZoom();
    const z0 = this.effZoom();
    z = Math.max(fz < 1 ? fz : 0.25, Math.min(MAX_ZOOM, z));
    if (Math.abs(z - fz) / fz < 0.005) {
      this.fit = true;
      this.ox = this.oy = 0;
    } else {
      const ratio = z / z0;
      const cx = this.vw / 2 + this.ox, cy = this.vh / 2 + this.oy;
      this.ox = ax - (ax - cx) * ratio - this.vw / 2;
      this.oy = ay - (ay - cy) * ratio - this.vh / 2;
      this.fit = false;
      this.zoom = z;
      this.clamp();
    }
    this.layout();
  }

  zoomBy(f: number, ax?: number, ay?: number): void { this.zoomTo(this.effZoom() * f, ax, ay); }

  zoomFit(): void {
    this.fit = true;
    this.ox = this.oy = 0;
    this.layout();
  }

  toImg(x: number, y: number): [number, number] {
    const s = this.effZoom();
    return [(x - this.vw / 2 - this.ox) / s + this.iw / 2, (y - this.vh / 2 - this.oy) / s + this.ih / 2];
  }

  toView(ix: number, iy: number): [number, number] {
    const s = this.effZoom();
    return [(ix - this.iw / 2) * s + this.vw / 2 + this.ox, (iy - this.ih / 2) * s + this.vh / 2 + this.oy];
  }

  private updateRendering(): void {
    // Show pixels when zoomed past 2 device-independent px per bitmap px (like Preview).
    const k = this.canvas.width ? (this.iw / this.canvas.width) * this.effZoom() : 1;
    this.canvas.style.imageRendering = k >= 2 ? "pixelated" : "auto";
  }

  /** Position the canvas, text layer, overlay and selection for the current view. */
  layout(): void {
    this.clamp();
    const s = this.effZoom();
    const [x, y] = this.toView(0, 0);
    for (const e of [this.canvas, this.textLayer, this.overlay]) {
      e.style.left = `${x}px`;
      e.style.top = `${y}px`;
      e.style.width = `${this.iw * s}px`;
      e.style.height = `${this.ih * s}px`;
    }
    this.canvas.hidden = !this.hasContent;
    this.updateRendering();
    const sel = this.sel;
    this.selEl.hidden = !sel;
    if (sel) {
      const [a, b] = this.toView(sel[0], sel[1]);
      const [c, d] = this.toView(sel[2], sel[3]);
      Object.assign(this.selEl.style, { left: `${a}px`, top: `${b}px`, width: `${c - a}px`, height: `${d - b}px` });
      this.selEl.classList.toggle("ellipse", this.tool === "ellipse");
    }
    this.viewport.dataset.zoom = s.toFixed(4);
    this.viewport.classList.toggle("can-pan", this.canPan());
    this.onView();
  }

  // ---- input -------------------------------------------------------------------

  private local(e: { clientX: number; clientY: number }): [number, number] {
    const r = this.viewport.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  private hitSel(x: number, y: number): string | null {
    const sel = this.sel;
    if (!sel) return null;
    const [x0, y0] = this.toView(sel[0], sel[1]);
    const [x1, y1] = this.toView(sel[2], sel[3]);
    const xm = (x0 + x1) / 2, ym = (y0 + y1) / 2;
    const pts: Record<string, [number, number]> = {
      nw: [x0, y0], n: [xm, y0], ne: [x1, y0], e: [x1, ym], se: [x1, y1], s: [xm, y1], sw: [x0, y1], w: [x0, ym],
    };
    for (const [h, [px, py]] of Object.entries(pts)) if (Math.abs(x - px) <= HANDLE && Math.abs(y - py) <= HANDLE) return h;
    return x > x0 && x < x1 && y > y0 && y < y1 ? "move" : null;
  }

  private clampImg(p: [number, number]): [number, number] {
    return [Math.min(Math.max(p[0], 0), this.iw), Math.min(Math.max(p[1], 0), this.ih)];
  }

  private down(e: PointerEvent): void {
    if (e.pointerType === "touch") {
      this.touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (this.touches.size === 2) {
        const [a, b] = [...this.touches.values()] as [{ x: number; y: number }, { x: number; y: number }];
        const [mx, my] = this.local({ clientX: (a.x + b.x) / 2, clientY: (a.y + b.y) / 2 });
        this.pinch = { d0: Math.hypot(a.x - b.x, a.y - b.y) || 1, z0: this.effZoom(), mx, my, ox: this.ox, oy: this.oy };
        this.drag = null;
        return;
      }
    }
    if (e.button !== 0 || !this.hasContent || this.pinch) return;
    const t = e.target as HTMLElement;
    if (e.defaultPrevented || (this.overlay.contains(t) && this.markupActive(t))) return;
    if (t.closest?.(".img-hint, .img-status")) return; // OSD controls inside the viewport
    this.viewport.focus({ preventScroll: true });
    const [x, y] = this.local(e);
    if (this.tool === "rect" || this.tool === "ellipse") {
      const h = this.hitSel(x, y);
      if (h) this.drag = { mode: "sel", h, x, y, r: [...this.sel!] as Sel };
      else {
        const p = this.clampImg(this.toImg(x, y));
        this.sel = [p[0], p[1], p[0], p[1]];
        this.drag = { mode: "sel", h: "se", x, y, r: [...this.sel] as Sel };
      }
    } else if (this.tool === "alpha") {
      this.drag = { mode: "alpha", x, y };
    } else if (this.canPan()) {
      this.drag = { mode: "pan", x, y, ox: this.ox, oy: this.oy };
      this.viewport.classList.add("panning");
    } else return;
    this.viewport.setPointerCapture(e.pointerId);
    e.preventDefault();
  }

  private move(e: PointerEvent): void {
    if (e.pointerType === "touch" && this.touches.has(e.pointerId)) {
      this.touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (this.pinch && this.touches.size >= 2) {
        const [a, b] = [...this.touches.values()] as [{ x: number; y: number }, { x: number; y: number }];
        const [mx, my] = this.local({ clientX: (a.x + b.x) / 2, clientY: (a.y + b.y) / 2 });
        const p = this.pinch;
        this.ox = p.ox + (mx - p.mx);
        this.oy = p.oy + (my - p.my);
        this.fit = false;
        this.zoom = p.z0;
        this.zoomTo(p.z0 * (Math.hypot(a.x - b.x, a.y - b.y) / p.d0), mx, my);
        return;
      }
    }
    const d = this.drag;
    if (!d) return;
    const [x, y] = this.local(e);
    if (d.mode === "pan") {
      this.ox = d.ox + (x - d.x);
      this.oy = d.oy + (y - d.y);
      this.layout();
    } else if (d.mode === "sel") {
      const s = this.effZoom();
      const dx = (x - d.x) / s, dy = (y - d.y) / s;
      let [x0, y0, x1, y1] = d.r;
      if (d.h === "move") {
        const w = x1 - x0, h = y1 - y0;
        const nx = Math.min(Math.max(x0 + dx, 0), this.iw - w), ny = Math.min(Math.max(y0 + dy, 0), this.ih - h);
        this.sel = [nx, ny, nx + w, ny + h];
      } else {
        if (d.h.includes("w")) x0 += dx;
        if (d.h.includes("e")) x1 += dx;
        if (d.h.includes("n")) y0 += dy;
        if (d.h.includes("s")) y1 += dy;
        const a = this.clampImg([Math.min(x0, x1), Math.min(y0, y1)]);
        const b = this.clampImg([Math.max(x0, x1), Math.max(y0, y1)]);
        this.sel = [a[0], a[1], b[0], b[1]];
      }
      this.layout();
      this.onSel();
    }
  }

  private up(e: PointerEvent): void {
    if (e.pointerType === "touch") {
      this.touches.delete(e.pointerId);
      if (this.touches.size < 2) this.pinch = null;
    }
    const d = this.drag;
    this.drag = null;
    this.viewport.classList.remove("panning");
    if (!d) return;
    const [x, y] = this.local(e);
    if (d.mode === "alpha" && Math.abs(x - d.x) + Math.abs(y - d.y) < 4) {
      const [ix, iy] = this.toImg(x, y);
      if (ix >= 0 && iy >= 0 && ix < this.iw && iy < this.ih) this.onAlpha(ix, iy);
    } else if (d.mode === "sel" && this.sel) {
      const s = this.effZoom();
      if ((this.sel[2] - this.sel[0]) * s < 4 || (this.sel[3] - this.sel[1]) * s < 4) this.setSel(null);
    }
  }

  private wheel(e: WheelEvent): void {
    if (!this.hasContent) return;
    const [x, y] = this.local(e);
    if (e.ctrlKey || e.metaKey) {
      // Touchpad pinch arrives as ctrl+wheel with small fractional deltas; a mouse
      // wheel notch with Ctrl as a large integer one.
      e.preventDefault();
      const dy = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
      const f = Math.abs(dy) >= 50 && Number.isInteger(dy) ? (dy < 0 ? 1.25 : 0.8) : Math.exp(-dy / 100);
      this.zoomBy(f, x, y);
      return;
    }
    if (!this.canPan()) return;
    e.preventDefault();
    const k = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? this.vh : 1;
    this.ox -= e.deltaX * k;
    this.oy -= e.deltaY * k;
    this.layout();
  }

  private dbl(e: MouseEvent): void {
    if (this.tool !== "pan" || !this.hasContent) return;
    if ((e.target as Element).classList?.contains("ocr-hit")) return; // word selection
    if (this.overlay.contains(e.target as Node) && this.markupActive(e.target)) return;
    const [x, y] = this.local(e);
    if (this.fit && this.fitZoom() < 1) this.zoomTo(1, x, y);
    else this.zoomFit();
  }
}
