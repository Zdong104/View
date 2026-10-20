// 3D model viewer (STL / OBJ / PLY). Port of preview/stl_view.py.
// Parsing and statistics run in a module Worker (vendor/model/mesh-worker.js, see
// copy.json) that hands back transferable typed arrays; three.js renders one
// non-indexed position buffer with flat shading from screen-space derivatives,
// plus optional crease-aware smooth normals once the worker has computed them.
import type { IconName } from "../../ui/icons.ts";
import { button, el, icon, linked, loadStyle, menuButton } from "../../ui/kit.ts";
import { extOf } from "../../shared/kinds.ts";
import type { DocumentSource, ExportFormat, InfoSection, SaveRequest, SaveResult, Viewer, ViewerContext, ViewerModule } from "../contract.ts";
import { DEFAULT_PITCH, DEFAULT_YAW, OrbitCamera, VIEWS, type ViewName, type Vec3 } from "./camera.ts";
import { fmtCoord, fmtInt, fmtLen, fmtNum } from "./format.ts";
import type { MeshStats } from "./mesh-core.d.ts";
import { ModelScene, type DisplayState } from "./scene.ts";

interface MeshInfo {
  fmt: string;
  up: "z" | "y";
  header: string;
  triangles: number;
  bboxMin: number[];
  bboxMax: number[];
  size: number[];
  origin: number[];
  fileSize: number;
  seconds: number;
}

type WorkerMessage =
  | { type: "mesh"; positions: Float32Array; info: MeshInfo }
  | { type: "analyzed"; positions: Float32Array; stats: MeshStats; normals: Int8Array | null; seconds: number }
  | { type: "error"; message: string };

type MenuItem = readonly [string, () => void, string?] | "-";

const ANIMATION_MS = 380;
const easeOutCubic = (t: number) => 1 - (1 - t) ** 3;

const isDarkTheme = (): boolean => {
  const t = document.documentElement.dataset.theme;
  return t === "dark" || (t !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
};

class ModelViewer implements Viewer {
  private source!: DocumentSource;
  private ctx!: ViewerContext;
  private root!: HTMLElement;
  private worker: Worker | null = null;
  private scene: ModelScene | null = null;
  private status!: HTMLElement;
  private statsEl!: HTMLElement;
  private resizeObs: ResizeObserver | null = null;

  private info: MeshInfo | null = null;
  private stats: MeshStats | null = null;
  private error: string | null = null;
  private analyzedPromise!: Promise<void>;
  private analyzedDone!: () => void;
  private t0 = performance.now();
  private firstFrameSeconds: number | null = null;
  private totalSeconds: number | null = null;

  private cam = new OrbitCamera();
  private display: DisplayState = { wireframe: false, flat: false, grid: true, dark: isDarkTheme(), up: "z" };
  private autoRotate = false;
  private showStats = false;
  private needsFit = true;
  private anim: { t0: number; from: [number, number, number, Vec3]; to: [number, number, number, Vec3]; dyaw: number } | null = null;
  private dirty = false;
  private raf = 0;
  private lastFrame = 0;
  private framed = false;

  private viewItems: MenuItem[] = [];
  private toggles: { wire?: HTMLButtonElement; flat?: HTMLButtonElement; grid?: HTMLButtonElement } = {};

  async mount(container: HTMLElement, source: DocumentSource, ctx: ViewerContext): Promise<void> {
    this.source = source;
    this.ctx = ctx;
    await loadStyle("model");
    this.analyzedPromise = new Promise((r) => { this.analyzedDone = r; });
    this.status = el("div.model-status", {}, el("div.model-spinner"), el("h2", { text: "Opening Model…" }));
    this.status.setAttribute("role", "status");
    this.statsEl = el("div.model-stats", { hidden: true });
    this.root = el("div.model-view", {
      "data-viewer": "model", "data-state": "loading", tabindex: 0,
      role: "application", "aria-label": `3D model ${source.name}`,
    }, this.status, this.statsEl);
    container.append(this.root);

    this.buildHeader();
    this.registerShortcuts();
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const themeChanged = () => { this.display.dark = isDarkTheme(); this.requestRender(); };
    mq.addEventListener("change", themeChanged, { signal: ctx.signal });
    const mo = new MutationObserver(themeChanged);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    ctx.signal.addEventListener("abort", () => mo.disconnect(), { once: true });

    // Throws on HTTP / file-access errors: the shell shows its own status page.
    const blob = await source.blob();
    const buffer = await blob.arrayBuffer();
    this.t0 = performance.now();
    const worker = new Worker(chrome.runtime.getURL("vendor/model/mesh-worker.js"), { type: "module" });
    this.worker = worker;
    worker.onmessage = (ev: MessageEvent<WorkerMessage>) => this.onWorker(ev.data);
    worker.onerror = (ev) => this.fail(ev.message || "The model worker crashed.");
    worker.postMessage({ type: "parse", buffer, ext: extOf(source.name) }, [buffer]);
  }

  // ---- worker results ------------------------------------------------------------

  private onWorker(m: WorkerMessage): void {
    if (m.type === "error") return this.fail(m.message);
    if (m.type === "mesh") return this.onMesh(m.positions, m.info);
    // analyzed
    this.scene?.restorePositions(m.positions);
    this.stats = m.stats;
    if (m.normals) this.scene?.setNormals(m.normals);
    this.totalSeconds = (performance.now() - this.t0) / 1000;
    this.root.dataset.analyzed = "true";
    this.root.dataset.closed = String(m.stats.closed);
    this.root.dataset.volume = m.stats.volume.toPrecision(8);
    this.root.dataset.area = m.stats.area.toPrecision(8);
    this.root.dataset.vertices = String(m.stats.vertices);
    this.root.dataset.smooth = String(!!m.normals);
    this.fillStats();
    this.analyzedDone();
    this.ctx.refreshTitle();
    this.requestRender();
  }

  private onMesh(positions: Float32Array, info: MeshInfo): void {
    this.info = info;
    this.display.up = info.up;
    let scene: ModelScene;
    try {
      scene = new ModelScene({ antialias: info.triangles <= 1_500_000 });
    } catch (e) {
      this.analyzedDone();
      return this.showMessage("3D Rendering Unavailable",
        `View couldn't start WebGL, which is needed to display 3D models.\n${(e as Error).message}\n\nThe model's details are still available in Get Info (Ctrl+I).`);
    }
    this.scene = scene;
    scene.setOrigin(info.origin as Vec3);
    this.root.prepend(scene.canvas);
    this.attachInput(scene.canvas);
    this.cam.setModel(info.size, info.up);
    this.root.dataset.triangles = String(info.triangles);
    this.root.dataset.format = info.fmt;
    this.root.dataset.state = "parsed";
    this.status.hidden = true;

    // Once the buffer is on the GPU, hand the array to the worker for the statistics
    // (zero copy); it comes back with the results.
    scene.setPositions(positions, info.size, () => queueMicrotask(() => {
      this.worker?.postMessage({ type: "analyze", positions, smooth: true }, [positions.buffer]);
    }));

    let resizeFrame = 0;  // resize on the next frame, not inside the observer
    this.resizeObs = new ResizeObserver(() => {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => this.onResize());
    });
    this.resizeObs.observe(this.root);
    this.onResize();
    this.resetView(false);
    this.fillStats();
    this.ctx.refreshTitle();
    this.root.focus({ preventScroll: true });
  }

  private fail(message: string): void {
    this.error = message;
    this.root.dataset.state = "error";
    this.analyzedDone();
    this.showMessage("Can't Open This Model", message);
    this.ctx.refreshTitle();
  }

  private showMessage(title: string, text: string): void {
    this.status.className = "model-status error";
    this.status.replaceChildren(el("h2", { text: title }), el("p", { text }));
    this.status.hidden = false;
  }

  // ---- rendering -----------------------------------------------------------------

  private onResize(): void {
    const scene = this.scene;
    if (!scene) return;
    const w = this.root.clientWidth, h = this.root.clientHeight;
    if (w < 1 || h < 1) return;
    scene.resize(w, h);
    if (this.needsFit) { // refit until the user moves the camera, once the real size is known
      this.cam.dist = this.cam.fit(w, h, this.cam.yaw, this.cam.pitch);
    }
    this.requestRender();
  }

  requestRender(): void {
    if (this.dirty || !this.scene) return;
    this.dirty = true;
    this.raf = requestAnimationFrame((now) => this.frame(now));
  }

  private frame(now: number): void {
    this.dirty = false;
    const scene = this.scene;
    if (!scene) return;
    const dt = this.lastFrame ? Math.min((now - this.lastFrame) / 1000, 0.1) : 0;
    this.lastFrame = now;
    let more = false;
    const a = this.anim;
    if (a) {
      const k = Math.min((now - a.t0) / ANIMATION_MS, 1);
      const v = easeOutCubic(k);
      this.cam.yaw = (a.from[0] + a.dyaw * v + 360) % 360;
      this.cam.pitch = a.from[1] + (a.to[1] - a.from[1]) * v;
      this.cam.dist = a.from[2] * (a.to[2] / a.from[2]) ** v;
      this.cam.target = a.from[3].map((f, i) => f + (a.to[3][i]! - f) * v) as Vec3;
      if (k >= 1) { // land exactly on the target
        Object.assign(this.cam, { yaw: a.to[0], pitch: a.to[1], dist: a.to[2], target: [...a.to[3]] as Vec3 });
        this.anim = null;
      } else more = true;
    }
    if (this.autoRotate) { this.cam.orbit(18 * dt, 0); more = true; }
    scene.render(this.cam, this.display);
    if (!this.framed) {
      scene.finish();
      this.framed = true;
      this.firstFrameSeconds = (performance.now() - this.t0) / 1000;
      this.root.dataset.rendered = "true";
      if (this.root.dataset.state === "parsed") this.root.dataset.state = "rendered";
    }
    const d = this.root.dataset;
    d.yaw = this.cam.yaw.toFixed(2);
    d.pitch = this.cam.pitch.toFixed(2);
    d.dist = this.cam.dist.toPrecision(6);
    d.target = this.cam.target.map((x) => x.toFixed(3)).join(",");
    d.up = this.display.up;
    d.wireframe = String(this.display.wireframe);
    d.flat = String(this.display.flat);
    d.grid = String(this.display.grid);
    d.frames = String((Number(d.frames) || 0) + 1);
    if (more) this.requestRender(); else this.lastFrame = 0;
  }

  // ---- camera commands -----------------------------------------------------------

  private viewport(): { w: number; h: number } {
    return { w: Math.max(this.root.clientWidth, 1), h: Math.max(this.root.clientHeight, 1) };
  }

  private moved(): void {
    this.needsFit = false;
    this.anim = null;
    this.requestRender();
  }

  private animateTo(yaw: number, pitch: number, dist: number, target: Vec3, animate: boolean): void {
    const cam = this.cam;
    if (!animate || document.hidden) {
      Object.assign(cam, { yaw, pitch, dist, target: [...target] as Vec3 });
      this.anim = null;
    } else {
      const dyaw = ((yaw - cam.yaw + 540) % 360) - 180; // shortest way round
      this.anim = {
        t0: performance.now(), dyaw,
        from: [cam.yaw, cam.pitch, cam.dist, [...cam.target] as Vec3],
        to: [yaw, pitch, dist, [...target] as Vec3],
      };
    }
    this.requestRender();
  }

  resetView(animate = true): void {
    if (!this.info) return;
    this.needsFit = true;
    const { w, h } = this.viewport();
    this.animateTo(DEFAULT_YAW, DEFAULT_PITCH, this.cam.fit(w, h, DEFAULT_YAW, DEFAULT_PITCH), [0, 0, 0], animate);
  }

  setView(name: ViewName): void {
    if (!this.info) return;
    const [, yaw, pitch] = VIEWS[name];
    const { w, h } = this.viewport();
    this.needsFit = false;
    this.animateTo(yaw, pitch, this.cam.fit(w, h, yaw, pitch), [0, 0, 0], true);
  }

  private setUp(axis: "z" | "y"): void {
    if (!this.info || axis === this.display.up) return;
    this.display.up = axis;
    this.cam.setModel(this.info.size, axis);
    this.resetView(true);
    this.rebuildViewMenu();
  }

  // ---- input ---------------------------------------------------------------------

  private attachInput(canvas: HTMLCanvasElement): void {
    const signal = this.ctx.signal;
    const pointers = new Map<number, { x: number; y: number }>();
    let mode: "orbit" | "pan" | null = null;
    let pinch: { dist: number; startDist: number; mx: number; my: number } | null = null;

    const two = () => { const [a, b] = [...pointers.values()]; return { d: Math.hypot(a!.x - b!.x, a!.y - b!.y), mx: (a!.x + b!.x) / 2, my: (a!.y + b!.y) / 2 }; };

    canvas.addEventListener("pointerdown", (ev) => {
      canvas.setPointerCapture(ev.pointerId);
      this.anim = null;
      this.root.focus({ preventScroll: true });
      pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (pointers.size === 2) {
        const t = two();
        pinch = { dist: this.cam.dist, startDist: t.d, mx: t.mx, my: t.my };
        mode = null;
      } else if (pointers.size === 1) {
        mode = ev.button === 1 || ev.button === 2 || ev.shiftKey ? "pan" : "orbit";
        canvas.classList.add(mode === "pan" ? "panning" : "dragging");
      }
    }, { signal });
    canvas.addEventListener("pointermove", (ev) => {
      const p = pointers.get(ev.pointerId);
      if (!p) return;
      const dx = ev.clientX - p.x, dy = ev.clientY - p.y;
      p.x = ev.clientX; p.y = ev.clientY;
      if (pinch && pointers.size === 2) {
        const t = two();
        const r = this.cam.radius;
        this.cam.dist = Math.max(r * 0.05, Math.min(r * 60, pinch.dist * (pinch.startDist / Math.max(t.d, 1))));
        this.cam.pan(t.mx - pinch.mx, t.my - pinch.my, this.viewport().h);
        pinch.mx = t.mx; pinch.my = t.my;
        this.moved();
      } else if (mode === "pan") {
        this.cam.pan(dx, dy, this.viewport().h);
        this.moved();
      } else if (mode === "orbit") {
        this.cam.orbit(-dx * 0.4, dy * 0.4);
        this.moved();
      }
    }, { signal });
    const end = (ev: PointerEvent) => {
      pointers.delete(ev.pointerId);
      if (pointers.size < 2) pinch = null;
      if (!pointers.size) { mode = null; canvas.classList.remove("dragging", "panning"); }
    };
    canvas.addEventListener("pointerup", end, { signal });
    canvas.addEventListener("pointercancel", end, { signal });
    canvas.addEventListener("contextmenu", (ev) => ev.preventDefault(), { signal });
    canvas.addEventListener("dblclick", () => this.resetView(true), { signal });

    // Wheel: touchpad two-finger scroll orbits, pinch (ctrl+wheel) and a mouse wheel zoom.
    let lastKind: "wheel" | "pad" = "pad";
    let lastAt = 0;
    canvas.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      this.anim = null;
      const legacy = (ev as WheelEvent & { wheelDeltaY?: number }).wheelDeltaY;
      // Notched mouse wheels report lines, or whole 120-unit steps with no sideways motion;
      // touchpads stream many small events (every few ms), so one that follows closely is a pad event.
      const mouseWheel = ev.deltaMode === 1 || (ev.deltaX === 0 && Math.abs(ev.deltaY) >= 40 && !!legacy && legacy % 120 === 0);
      const now = ev.timeStamp;
      const kind: "wheel" | "pad" = mouseWheel && !(lastKind === "pad" && now - lastAt < 30) ? "wheel" : "pad";
      lastKind = kind;
      lastAt = now;
      const notch = ev.deltaMode === 1 ? ev.deltaY / 3 : ev.deltaY / 100;
      if (kind === "wheel") this.cam.zoom(1.15 ** notch);
      else if (ev.ctrlKey) this.cam.zoom(Math.exp(ev.deltaY * 0.01)); // pinch
      else this.cam.orbit(ev.deltaX * 0.35, -ev.deltaY * 0.35);
      this.moved();
    }, { signal, passive: false });
  }

  private registerShortcuts(): void {
    const sc = this.ctx.shortcuts;
    const zoom = (f: number) => () => { this.cam.zoom(f); this.moved(); };
    const orbit = (dy: number, dp: number) => () => { this.cam.orbit(dy, dp); this.moved(); };
    sc.add({ keys: "R", description: "Reset view", run: () => this.resetView(true) });
    sc.add({ keys: "W", description: "Toggle wireframe", run: () => this.toggle("wireframe") });
    sc.add({ keys: "Mod+0", description: "Reset view", run: () => this.resetView(true) });
    sc.add({ keys: "Mod+=", description: "Zoom in", run: zoom(1 / 1.2) });
    sc.add({ keys: "Mod++", description: "Zoom in", run: zoom(1 / 1.2) });
    sc.add({ keys: "Mod+-", description: "Zoom out", run: zoom(1.2) });
    sc.add({ keys: "+", description: "Zoom in", run: zoom(1 / 1.2) });
    sc.add({ keys: "=", description: "Zoom in", run: zoom(1 / 1.2) });
    sc.add({ keys: "-", description: "Zoom out", run: zoom(1.2) });
    sc.add({ keys: "ArrowLeft", description: "Orbit left", run: orbit(-5, 0) });
    sc.add({ keys: "ArrowRight", description: "Orbit right", run: orbit(5, 0) });
    sc.add({ keys: "ArrowUp", description: "Orbit up", run: orbit(0, 5) });
    sc.add({ keys: "ArrowDown", description: "Orbit down", run: orbit(0, -5) });
  }

  // ---- header --------------------------------------------------------------------

  private toggle(what: "wireframe" | "flat" | "grid"): void {
    this.display[what] = !this.display[what];
    const b = { wireframe: this.toggles.wire, flat: this.toggles.flat, grid: this.toggles.grid }[what];
    b?.setAttribute("aria-pressed", String(this.display[what]));
    this.requestRender();
  }

  private rebuildViewMenu(): void {
    const mark = (on: boolean, label: string): string => (on ? `✓ ${label}` : label);
    const items = this.viewItems;
    items.length = 0;
    for (const [name, [label]] of Object.entries(VIEWS) as [ViewName, readonly [string, number, number]][]) {
      items.push([label, () => this.setView(name)]);
    }
    items.push("-");
    items.push([mark(this.display.up === "z", "Z Axis Up"), () => this.setUp("z")]);
    items.push([mark(this.display.up === "y", "Y Axis Up"), () => this.setUp("y")]);
    items.push("-");
    items.push([mark(this.autoRotate, "Auto-Rotate"), () => { this.autoRotate = !this.autoRotate; this.rebuildViewMenu(); this.requestRender(); }]);
    items.push([mark(this.showStats, "Show Statistics"), () => { this.showStats = !this.showStats; this.fillStats(); this.rebuildViewMenu(); }]);
  }

  private buildHeader(): void {
    const { start, tools } = this.ctx.header;
    const reset = button({ icon: "zoomFit", title: "Reset View (R)", onClick: () => this.resetView(true) });
    reset.dataset.action = "reset";
    this.rebuildViewMenu();
    const views = menuButton({ icon: "menu", title: "View" }, this.viewItems);
    views.replaceChildren(icon("views"));
    views.dataset.action = "views";
    start.append(reset, views);

    const mk = (key: "wire" | "flat" | "grid", what: "wireframe" | "flat" | "grid", ic: IconName, title: string) => {
      const b = button({ title, toggle: true, pressed: this.display[what], onClick: () => { this.display[what] = b.getAttribute("aria-pressed") === "true"; this.requestRender(); } });
      b.prepend(icon(ic));
      b.dataset.action = what;
      this.toggles[key] = b;
      return b;
    };
    tools.append(linked(
      mk("wire", "wireframe", "wireframe", "Wireframe (W)"),
      mk("flat", "flat", "shading", "Flat Shading"),
      mk("grid", "grid", "grid", "Ground Grid"),
    ));
  }

  // ---- statistics overlay and Get Info -------------------------------------------

  private volumeText(s: MeshStats): string {
    let txt = fmtNum(s.volume, "mm³");
    if (s.volume >= 1000) txt += `  (${(s.volume / 1000).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} cm³)`;
    return s.closed ? txt : `≈ ${txt}`;
  }

  private openText(s: MeshStats): string {
    const bits: string[] = [];
    if (s.boundaryEdges) bits.push(`${fmtInt(s.boundaryEdges)} open edges`);
    if (s.nonmanifoldEdges) bits.push(`${fmtInt(s.nonmanifoldEdges)} non-manifold edges`);
    return `No — ${bits.join(", ") || "not closed"}`;
  }

  private dims(): string {
    return (this.info?.size ?? []).map(fmtLen).join(" × ");
  }

  private fillStats(): void {
    this.statsEl.hidden = !this.showStats || !this.info;
    const info = this.info;
    if (!info) return;
    const s = this.stats;
    if (!s) {
      this.statsEl.textContent = `${fmtInt(info.triangles)} triangles\n${this.dims()} mm\nComputing…`;
      return;
    }
    const lines = [
      `${fmtInt(s.triangles)} triangles · ${fmtInt(s.vertices)} vertices`,
      `${this.dims()} mm`,
      `Area ${fmtNum(s.area, "mm²")}`,
      `Volume ${this.volumeText(s)}`,
    ];
    if (!s.closed) lines.push("Not watertight");
    this.statsEl.textContent = lines.join("\n");
  }

  getTitle(): string {
    return this.source.name;
  }

  getSubtitle(): string {
    if (this.error) return "";
    if (!this.info) return "";
    return `${fmtInt(this.info.triangles)} triangles · ${this.dims()} mm`;
  }

  async getInfo(): Promise<InfoSection[]> {
    // The statistics arrive a moment after the first frame: wait for them (bounded).
    await Promise.race([this.analyzedPromise, new Promise((r) => setTimeout(r, 20000))]);
    const info = this.info;
    if (!info) return this.error ? [{ title: "Model", rows: [["Error", this.error]] }] : [];
    const s = this.stats;
    const [sx, sy, sz] = info.size as [number, number, number];
    const rows: InfoSection["rows"] = [["Format", info.fmt]];
    if (info.header) rows.push(["STL header", info.header]);
    rows.push(
      ["Triangles", fmtInt(info.triangles)],
      ["Vertices", s ? fmtInt(s.vertices) : "Computing…"],
      ["Dimensions", `${fmtLen(sx)} × ${fmtLen(sy)} × ${fmtLen(sz)} mm`],
      ["", `${(sx / 25.4).toFixed(2)} × ${(sy / 25.4).toFixed(2)} × ${(sz / 25.4).toFixed(2)} in`],
      ["Bounding box min", info.bboxMin.map(fmtCoord).join(", ")],
      ["Bounding box max", info.bboxMax.map(fmtCoord).join(", ")],
    );
    if (s) {
      rows.push(
        ["Surface area", fmtNum(s.area, "mm²")],
        ["Volume", this.volumeText(s)],
        ["Watertight", s.closed ? "Yes (closed mesh)" : this.openText(s)],
        ["Centre of mass", s.centerOfMass.map(fmtCoord).join(", ") + (s.closed ? "" : " (approx.)")],
      );
    } else {
      rows.push(["Surface area", "Computing…"], ["Volume", "Computing…"], ["Watertight", "Computing…"]);
    }
    if (this.firstFrameSeconds !== null) {
      let t = `${this.firstFrameSeconds.toFixed(2)} s to first frame`;
      if (this.totalSeconds !== null) t += `, ${this.totalSeconds.toFixed(2)} s total`;
      rows.push(["Load time", t]);
    }
    return [{ title: "Model", rows }];
  }

  // ---- export --------------------------------------------------------------------

  exportFormats(): ExportFormat[] {
    return this.scene ? [{ id: "png", label: "PNG Snapshot…" }] : [];
  }

  async save(req: SaveRequest): Promise<SaveResult | null> {
    if (req.mode !== "export" || !this.scene) return null;
    this.scene.render(this.cam, this.display); // the drawing buffer is only valid right after a render
    const blob = await new Promise<Blob | null>((r) => this.scene!.canvas.toBlob(r, "image/png"));
    if (!blob) return null;
    const stem = this.source.name.replace(/\.[^.]*$/, "") || "model";
    return {
      blob, suggestedName: `${stem}.png`, replacesOriginal: false,
      types: [{ description: "PNG image", accept: { "image/png": [".png"] } }],
    };
  }

  destroy(): void {
    cancelAnimationFrame(this.raf);
    this.worker?.terminate();
    this.worker = null;
    this.resizeObs?.disconnect();
    this.scene?.dispose();
    this.scene = null;
    this.root?.remove();
  }
}

const mod: ViewerModule = { kind: "model", create: () => new ModelViewer() };
export default mod;
