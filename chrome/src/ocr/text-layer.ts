// Selectable Live Text layer over an image or page (port of preview/ocr.py
// TextOverlay), for the image and PDF viewers. An SVG whose viewBox is the
// OCR image's pixel space: put `layer.el` inside the (position: relative)
// element that shows the image and it fills it, whatever the zoom.
//
// Dragging over text selects it (character-accurate, from OcrLine.charQuads),
// double-click selects a word, triple-click a line; Ctrl+C copies the
// selection (or call selectedText()). Pointer events that start outside text
// pass through to the viewer (pan, markup, ...).
import type { OcrLine, OcrResult } from "./api.ts";
import { corners, isWide, joinText, readingOrder } from "./layout.ts";

export { linesToText } from "./layout.ts";

export interface TextLayerOptions {
  /** Answer the document's copy event (Ctrl+C) while text is selected. Default true. */
  handleCopy?: boolean;
  /** Called when the selection or box visibility changes. */
  onChange?: () => void;
}

export interface TextLayer {
  readonly el: SVGSVGElement;
  hasText(): boolean;
  hasSelection(): boolean;
  selectedText(): string;
  /** All text in reading order. */
  text(): string;
  selectAll(): void;
  clear(): void;
  /** Faint outlines around every recognized line (the desktop shows them after "Recognize Text"). */
  setShowBoxes(on: boolean): void;
  readonly showBoxes: boolean;
  /** Is (x, y), in image pixels, over recognized text? */
  hit(x: number, y: number, margin?: number): boolean;
  destroy(): void;
}

type P = [number, number];
type Loc = [item: number, char: number];

interface Item {
  line: OcrLine;
  sep: string;
  chars: string[];
  /** Reading-direction quad TL, TR, BR, BL (from the character quads when present). */
  quad: P[];
  /** Hit polygon (the detected box). */
  box: P[];
  /** Per char boundary (n + 1): the edge's two ends [top, bottom]. */
  edges: [P, P][];
  /** Fraction along quad's TL->TR axis at each char boundary. */
  cum: number[];
}

const SVG = "http://www.w3.org/2000/svg";
const lerp = (a: P, b: P, t: number): P => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];

function charCum(chars: string[]): number[] {
  const ws = chars.map((c) => (isWide(c) ? 1 : " .,;:!'|il".includes(c) ? 0.35 : 0.6));
  const total = ws.reduce((a, b) => a + b, 0) || 1;
  const cum = [0];
  for (const w of ws) cum.push(cum[cum.length - 1]! + w / total);
  return cum;
}

function makeItem(line: OcrLine, sep: string): Item {
  const chars = Array.from(line.text);
  const box = corners(line);
  const n = chars.length;
  const cq = line.charQuads;
  let quad = box, edges: [P, P][];
  if (cq && cq.length === n * 8 && n > 0) {
    const pt = (k: number, c: number): P => [cq[k * 8 + c * 2]!, cq[k * 8 + c * 2 + 1]!];
    quad = [pt(0, 0), pt(n - 1, 1), pt(n - 1, 2), pt(0, 3)];
    edges = [];
    for (let k = 0; k < n; k++) edges.push([pt(k, 0), pt(k, 3)]);
    edges.push([pt(n - 1, 1), pt(n - 1, 2)]);
  } else {
    const cum = charCum(chars);
    edges = cum.map((t): [P, P] => [lerp(box[0]!, box[1]!, t), lerp(box[3]!, box[2]!, t)]);
  }
  // Project each boundary's midpoint on the reading axis.
  const [a, b] = [quad[0]!, quad[1]!];
  const ax = b[0] - a[0], ay = b[1] - a[1], d2 = ax * ax + ay * ay || 1;
  let prev = 0;
  const cum = edges.map(([top, bot], k) => {
    const mx = (top[0] + bot[0]) / 2, my = (top[1] + bot[1]) / 2;
    const t = k === 0 ? 0 : k === n ? 1 : Math.min(1, Math.max(prev, ((mx - a[0]) * ax + (my - a[1]) * ay) / d2));
    prev = t;
    return t;
  });
  return { line, sep, chars, quad, box, edges, cum };
}

function inPoly(x: number, y: number, poly: P[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i]!, [xj, yj] = poly[j]!;
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi || 1e-12) + xi) inside = !inside;
  }
  return inside;
}

function segDist2(px: number, py: number, a: P, b: P): number {
  const dx = b[0] - a[0], dy = b[1] - a[1], d2 = dx * dx + dy * dy;
  const t = d2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / d2));
  const qx = a[0] + t * dx - px, qy = a[1] + t * dy - py;
  return qx * qx + qy * qy;
}

const edgeDist2 = (x: number, y: number, poly: P[]) =>
  Math.min(...poly.map((p, k) => segDist2(x, y, p, poly[(k + 1) % poly.length]!)));

const cmp = (a: Loc, b: Loc) => a[0] - b[0] || a[1] - b[1];
const pathOf = (q: P[]) => `M${q.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("L")}Z`;

let styled = false;
function injectStyle(doc: Document) {
  if (styled) return;
  styled = true;
  const s = doc.createElement("style");
  s.textContent = `
.ocr-layer{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible;touch-action:none}
.ocr-layer .ocr-hit{fill:transparent;stroke:transparent;stroke-width:6px;vector-effect:non-scaling-stroke;pointer-events:all;cursor:text}
.ocr-layer .ocr-boxes{fill:none;stroke:var(--warning);stroke-opacity:.75;stroke-width:1.2px;vector-effect:non-scaling-stroke;display:none}
.ocr-layer.show-boxes .ocr-boxes{display:inline}
.ocr-layer .ocr-sel{fill:var(--accent);fill-opacity:.35;stroke:none}`;
  doc.head.append(s);
}

export function createTextLayer(result: OcrResult, opts: TextLayerOptions = {}): TextLayer {
  const items = readingOrder(result.lines).map(([i, sep]) => makeItem(result.lines[i]!, sep));
  injectStyle(document);
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "ocr-layer");
  svg.setAttribute("viewBox", `0 0 ${result.width} ${result.height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.dataset.ocrLines = String(items.length);
  const mk = (cls: string, d = "") => {
    const p = document.createElementNS(SVG, "path");
    p.setAttribute("class", cls);
    if (d) p.setAttribute("d", d);
    return p;
  };
  const boxes = mk("ocr-boxes", items.map((it) => pathOf(it.box)).join(""));
  const sel = mk("ocr-sel");
  svg.append(boxes, sel);
  for (const it of items) svg.append(mk("ocr-hit", pathOf(it.box)));

  let anchor: Loc | null = null, focus: Loc | null = null, dragging = false, showBoxes = false;

  const changed = () => {
    sel.setAttribute("d", selectionPath());
    svg.dataset.selection = hasSelection() ? "true" : "false";
    opts.onChange?.();
  };

  function hasSelection() {
    return anchor !== null && focus !== null && cmp(anchor, focus) !== 0;
  }

  function range(): [Loc, Loc] | null {
    if (!hasSelection()) return null;
    return cmp(anchor!, focus!) <= 0 ? [anchor!, focus!] : [focus!, anchor!];
  }

  function slice(it: Item, a: number, b: number): P[] {
    const [ta, ba] = it.edges[a]!, [tb, bb] = it.edges[b]!;
    return [ta, tb, bb, ba];
  }

  function selectionPath(): string {
    const r = range();
    if (!r) return "";
    const [[i0, c0], [i1, c1]] = r;
    let d = "";
    for (let i = i0; i <= i1; i++) {
      const it = items[i]!;
      const a = i === i0 ? c0 : 0, b = i === i1 ? c1 : it.chars.length;
      if (b > a) d += pathOf(slice(it, a, b));
    }
    return d;
  }

  function locate(x: number, y: number): Loc | null {
    if (!items.length) return null;
    let best = 0, bestD = Infinity;
    for (let k = 0; k < items.length; k++) {
      if (inPoly(x, y, items[k]!.box)) { best = k; break; }
      const d = edgeDist2(x, y, items[k]!.box);
      if (d < bestD) { best = k; bestD = d; }
    }
    const it = items[best]!;
    const [a, b] = [it.quad[0]!, it.quad[1]!];
    const ax = b[0] - a[0], ay = b[1] - a[1], d2 = ax * ax + ay * ay;
    const t = Math.max(0, Math.min(1, d2 === 0 ? 0 : ((x - a[0]) * ax + (y - a[1]) * ay) / d2));
    let ci = 0;
    for (let k = 1; k < it.cum.length; k++) if (Math.abs(it.cum[k]! - t) < Math.abs(it.cum[ci]! - t)) ci = k;
    return [best, ci];
  }

  function selectedText(): string {
    const r = range();
    if (!r) return "";
    const [[i0, c0], [i1, c1]] = r;
    let out = "";
    for (let i = i0; i <= i1; i++) {
      const it = items[i]!;
      const piece = it.chars.slice(i === i0 ? c0 : 0, i === i1 ? c1 : it.chars.length).join("");
      if (piece) out = out ? joinText(out, piece, it.sep) : piece;
    }
    return out;
  }

  function selectWord(loc: Loc) {
    const [i, c0] = loc;
    const s = items[i]!.chars;
    if (!s.length) return;
    const c = Math.min(c0, s.length - 1);
    const cls = (ch: string) => (isWide(ch) ? "c" : /[\p{L}\p{N}_]/u.test(ch) ? "w" : "s");
    const k = cls(s[c]!);
    let a = c, b = c + 1;
    while (k !== "c" && a > 0 && cls(s[a - 1]!) === k) a--;
    while (k !== "c" && b < s.length && cls(s[b]!) === k) b++;
    anchor = [i, a];
    focus = [i, b];
  }

  const toImage = (e: PointerEvent): P => {
    const m = svg.getScreenCTM();
    if (!m) return [0, 0];
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
    return [p.x, p.y];
  };

  // Click counting ourselves: PointerEvent.detail is not a click count everywhere.
  let lastDown = { t: -1e9, x: 0, y: 0, n: 0 };
  const onDown = (e: PointerEvent) => {
    if (e.button !== 0 || !(e.target as Element).classList?.contains("ocr-hit")) return;
    e.stopPropagation();
    e.preventDefault();
    const near = e.timeStamp - lastDown.t < 500 && Math.hypot(e.clientX - lastDown.x, e.clientY - lastDown.y) < 5;
    lastDown = { t: e.timeStamp, x: e.clientX, y: e.clientY, n: near ? lastDown.n + 1 : 1 };
    const [x, y] = toImage(e);
    const loc = locate(x, y);
    if (!loc) return;
    if (lastDown.n === 2) selectWord(loc);
    else if (lastDown.n >= 3) {
      anchor = [loc[0], 0];
      focus = [loc[0], items[loc[0]]!.chars.length];
    } else {
      anchor = focus = loc;
      dragging = true;
      svg.setPointerCapture(e.pointerId);
    }
    changed();
  };
  const onMove = (e: PointerEvent) => {
    if (!dragging || anchor === null) return;
    const f = locate(...toImage(e));
    if (f && (!focus || cmp(f, focus) !== 0)) {
      focus = f;
      changed();
    }
  };
  const onUp = (e: PointerEvent) => {
    if (!dragging) return;
    dragging = false;
    if (svg.hasPointerCapture(e.pointerId)) svg.releasePointerCapture(e.pointerId);
    if (!hasSelection()) anchor = focus = null;
    changed();
  };
  // A press elsewhere on the host (not on text) drops the selection, as on the desktop.
  const onDocDown = (e: PointerEvent) => {
    if (!hasSelection() || dragging) return;
    const t = e.target as Node;
    if (svg.parentElement?.contains(t) && !(t instanceof Element && t.classList.contains("ocr-hit"))) {
      anchor = focus = null;
      changed();
    }
  };
  const onCopy = (e: ClipboardEvent) => {
    if (!hasSelection()) return;
    const native = document.getSelection();
    if (native && !native.isCollapsed && native.toString()) return; // the user selected page text instead
    e.clipboardData?.setData("text/plain", selectedText());
    e.preventDefault();
  };

  svg.addEventListener("pointerdown", onDown);
  svg.addEventListener("pointermove", onMove);
  svg.addEventListener("pointerup", onUp);
  svg.addEventListener("pointercancel", onUp);
  document.addEventListener("pointerdown", onDocDown, true);
  if (opts.handleCopy !== false) document.addEventListener("copy", onCopy);

  return {
    el: svg,
    hasText: () => items.length > 0,
    hasSelection,
    selectedText,
    text: () => {
      let out = "";
      for (const it of items) out = out ? joinText(out, it.line.text, it.sep) : it.line.text;
      return out;
    },
    selectAll() {
      if (!items.length) return;
      anchor = [0, 0];
      focus = [items.length - 1, items[items.length - 1]!.chars.length];
      changed();
    },
    clear() {
      anchor = focus = null;
      dragging = false;
      changed();
    },
    setShowBoxes(on: boolean) {
      if (on === showBoxes) return;
      showBoxes = on;
      svg.classList.toggle("show-boxes", on);
      opts.onChange?.();
    },
    get showBoxes() {
      return showBoxes;
    },
    hit(x: number, y: number, margin = 2) {
      return items.some((it) => inPoly(x, y, it.box) || (margin > 0 && edgeDist2(x, y, it.box) <= margin * margin));
    },
    destroy() {
      document.removeEventListener("pointerdown", onDocDown, true);
      document.removeEventListener("copy", onCopy);
      svg.remove();
    },
  };
}
