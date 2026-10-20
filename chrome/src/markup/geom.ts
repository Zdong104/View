// Markup geometry: bounding boxes, hit testing, handles, move/resize, shape
// recognition and the drawing description of every annotation. Port of the
// geometry / rendering half of preview/markup.py.
//
// DOM-free on purpose: the SVG overlay, the canvas flattener (render()) and the
// PDF appearance streams (pdf-export.ts) all draw the same Prim[] list produced
// by drawAnnotation(), so an annotation looks the same on screen, on an image
// and in every PDF reader. Unit tests import this module under Node.
import { getStroke } from "perfect-freehand";
import type { Annotation, MarkupStyle, StrokePoints } from "./api.ts";

export const NOTE_SIZE = 20;
export const NOTE_COLOR = "#ffd940";
export const HIGHLIGHT_COLOR = "#ffdb1a";
export const DEFAULT_STROKE = "#d62828"; // (0.84, 0.16, 0.16) in markup.py
export const SHAPES = ["line", "arrow", "rect", "rrect", "oval", "bubble", "star"] as const;
export const BOX_TYPES = new Set(["rect", "rrect", "oval", "bubble", "star", "text", "signature"]);
export const LINE_TYPES = new Set(["line", "arrow"]);
export const TEXTMARK_TYPES = new Set(["highlight", "underline", "strike"]);

export interface Box { x: number; y: number; w: number; h: number }
export interface FontSpec { family: string; size: number; bold?: boolean; italic?: boolean }
/** Width of a single line of text in surface units. */
export type Measure = (text: string, font: FontSpec) => number;

export type Cmd =
  | { o: "M" | "L"; x: number; y: number }
  | { o: "C"; x1: number; y1: number; x2: number; y2: number; x: number; y: number }
  | { o: "Z" };

export interface PathPrim {
  t: "path";
  cmds: Cmd[];
  fill: string | null;
  stroke: string | null;
  width: number;
  cap?: "round" | "butt";
  /** Extra alpha on top of the annotation opacity (highlights). */
  alpha?: number;
  blend?: "multiply";
}
export interface TextPrim {
  t: "text";
  lines: { text: string; x: number; y: number }[];
  font: FontSpec;
  color: string;
}
export type Prim = PathPrim | TextPrim;

// ------------------------------------------------------------- colours ---

/** CSS colour (#rgb, #rrggbb, #rrggbbaa, rgb(), rgba()) -> [r, g, b, a] in 0..1. */
export function parseColor(c: string | null | undefined): [number, number, number, number] {
  if (!c) return [0, 0, 0, 1];
  const s = c.trim();
  let m = /^#([0-9a-f]{3,8})$/i.exec(s);
  if (m) {
    let h = m[1]!;
    if (h.length === 3 || h.length === 4) h = [...h].map((ch) => ch + ch).join("");
    const n = (i: number) => parseInt(h.slice(i, i + 2), 16) / 255;
    return [n(0), n(2), n(4), h.length >= 8 ? n(6) : 1];
  }
  m = /^rgba?\(([^)]+)\)$/i.exec(s);
  if (m) {
    const v = m[1]!.split(/[\s,/]+/).filter(Boolean).map(parseFloat);
    return [(v[0] ?? 0) / 255, (v[1] ?? 0) / 255, (v[2] ?? 0) / 255, v[3] ?? 1];
  }
  return [0, 0, 0, 1];
}

export function toHex(r: number, g: number, b: number): string {
  const h = (v: number) => Math.round(Math.min(1, Math.max(0, v)) * 255).toString(16).padStart(2, "0");
  return `#${h(r)}${h(g)}${h(b)}`;
}

// -------------------------------------------------------------- styles ---

export function defaultFont(): NonNullable<MarkupStyle["font"]> {
  return { family: "sans-serif", size: 14, bold: false, italic: false, align: "left" };
}

export function fontOf(a: { style: MarkupStyle }): FontSpec & { align: "left" | "center" | "right" } {
  const f = a.style.font ?? defaultFont();
  return { family: f.family, size: Math.max(1, f.size), bold: !!f.bold, italic: !!f.italic, align: f.align ?? "left" };
}

// ------------------------------------------------------------ geometry ---

type Ann = Annotation;
type Patch = Partial<Ann>;

/** Pressure-carrying points of a stroke as [x, y, p] triples. */
export function triples(s: StrokePoints): [number, number, number][] {
  const out: [number, number, number][] = [];
  for (let i = 0; i + 1 < s.length; i += 3) out.push([s[i]!, s[i + 1]!, s[i + 2] ?? 0.5]);
  return out;
}

function quadsBox(q: number[]): Box {
  if (q.length < 8) return { x: 0, y: 0, w: 0, h: 0 };
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let i = 0; i + 1 < q.length; i += 2) {
    x0 = Math.min(x0, q[i]!); x1 = Math.max(x1, q[i]!);
    y0 = Math.min(y0, q[i + 1]!); y1 = Math.max(y1, q[i + 1]!);
  }
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

export function bbox(a: Ann): Box {
  switch (a.type) {
    case "line": case "arrow":
      return { x: Math.min(a.x1, a.x2), y: Math.min(a.y1, a.y2), w: Math.abs(a.x2 - a.x1), h: Math.abs(a.y2 - a.y1) };
    case "ink": {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const s of a.strokes) for (let i = 0; i + 1 < s.length; i += 3) {
        x0 = Math.min(x0, s[i]!); x1 = Math.max(x1, s[i]!);
        y0 = Math.min(y0, s[i + 1]!); y1 = Math.max(y1, s[i + 1]!);
      }
      return x0 === Infinity ? { x: 0, y: 0, w: 0, h: 0 } : { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
    }
    case "highlight": case "underline": case "strike":
      return quadsBox(a.quads);
    case "note":
      return { x: a.x, y: a.y, w: NOTE_SIZE, h: NOTE_SIZE };
    default:
      return { x: a.x, y: a.y, w: a.w, h: a.h };
  }
}

/** Patch that moves an annotation by (dx, dy). */
export function translate(a: Ann, dx: number, dy: number): Patch {
  switch (a.type) {
    case "line": case "arrow":
      return { x1: a.x1 + dx, y1: a.y1 + dy, x2: a.x2 + dx, y2: a.y2 + dy } as Patch;
    case "ink":
      return { strokes: a.strokes.map((s) => s.map((v, i) => (i % 3 === 0 ? v + dx : i % 3 === 1 ? v + dy : v))) } as Patch;
    case "highlight": case "underline": case "strike":
      return { quads: a.quads.map((v, i) => (i % 2 === 0 ? v + dx : v + dy)) } as Patch;
    default:
      return { x: a.x + dx, y: a.y + dy } as Patch;
  }
}

/** Patch that scales an annotation's geometry into a new bounding box. */
export function setBox(a: Ann, nb: Box, measure?: Measure): Patch {
  const o = bbox(a);
  const sx = o.w > 1e-6 ? nb.w / o.w : 1;
  const sy = o.h > 1e-6 ? nb.h / o.h : 1;
  const mx = (v: number) => nb.x + (v - o.x) * sx;
  const my = (v: number) => nb.y + (v - o.y) * sy;
  switch (a.type) {
    case "line": case "arrow":
      return { x1: mx(a.x1), y1: my(a.y1), x2: mx(a.x2), y2: my(a.y2) } as Patch;
    case "ink":
      return { strokes: a.strokes.map((s) => s.map((v, i) => (i % 3 === 0 ? mx(v) : i % 3 === 1 ? my(v) : v))) } as Patch;
    case "highlight": case "underline": case "strike": case "note":
      return {};
    case "text": {
      const p = { x: nb.x, y: nb.y, w: nb.w, h: nb.h } as Patch;
      if (measure) (p as { h: number }).h = textHeight({ ...a, w: nb.w }, measure);
      return p;
    }
    case "signature": {
      // Keep the ink's aspect ratio and let the pen width follow the size.
      const k = a.w > 1e-6 ? nb.w / a.w : 1;
      return { x: nb.x, y: nb.y, w: nb.w, h: nb.h, style: { ...a.style, width: a.style.width * k } } as Patch;
    }
    default:
      return { x: nb.x, y: nb.y, w: nb.w, h: nb.h } as Patch;
  }
}

export function segDist(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax, dy = by - ay;
  const L = dx * dx + dy * dy;
  const t = L === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L));
  return Math.hypot(px - ax - t * dx, py - ay - t * dy);
}

function inQuad(q: number[], i: number, x: number, y: number, tol: number): boolean {
  const b = quadsBox(q.slice(i, i + 8));
  return x >= b.x - tol && x <= b.x + b.w + tol && y >= b.y - tol && y <= b.y + b.h + tol;
}

/** Does surface point (x, y) touch the annotation (tol in surface units)? */
export function hit(a: Ann, x: number, y: number, tol: number): boolean {
  switch (a.type) {
    case "line": case "arrow":
      return segDist(x, y, a.x1, a.y1, a.x2, a.y2) <= tol + a.style.width / 2;
    case "ink":
      for (const s of a.strokes) {
        const t = triples(s);
        if (t.length === 1 && Math.hypot(x - t[0]![0], y - t[0]![1]) <= tol + a.style.width) return true;
        for (let i = 1; i < t.length; i++) {
          if (segDist(x, y, t[i - 1]![0], t[i - 1]![1], t[i]![0], t[i]![1]) <= tol + a.style.width) return true;
        }
      }
      return false;
    case "highlight": case "underline": case "strike":
      for (let i = 0; i + 7 < a.quads.length; i += 8) if (inQuad(a.quads, i, x, y, tol)) return true;
      return false;
    default: {
      const b = bbox(a);
      return x >= b.x - tol && x <= b.x + b.w + tol && y >= b.y - tol && y <= b.y + b.h + tol;
    }
  }
}

export type HandleName = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w" | "p0" | "p1";
export const HANDLE_CURSORS: Record<HandleName, string> = {
  nw: "nwse-resize", se: "nwse-resize", ne: "nesw-resize", sw: "nesw-resize",
  n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize", p0: "crosshair", p1: "crosshair",
};

/** Resize handles in surface units. */
export function handles(a: Ann): [HandleName, number, number][] {
  if (a.type === "line" || a.type === "arrow") return [["p0", a.x1, a.y1], ["p1", a.x2, a.y2]];
  if (a.type === "note" || TEXTMARK_TYPES.has(a.type)) return [];
  const { x, y, w, h } = bbox(a);
  if (a.type === "signature") return [["nw", x, y], ["ne", x + w, y], ["sw", x, y + h], ["se", x + w, y + h]];
  if (a.type === "text") return [["w", x, y + h / 2], ["e", x + w, y + h / 2]];
  return [["nw", x, y], ["n", x + w / 2, y], ["ne", x + w, y], ["e", x + w, y + h / 2],
    ["se", x + w, y + h], ["s", x + w / 2, y + h], ["sw", x, y + h], ["w", x, y + h / 2]];
}

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

/** markup.py MarkupController._resize: patch for dragging `handle` of `orig` by (dx, dy). */
export function resize(orig: Ann, handle: HandleName, dx: number, dy: number, shift: boolean,
  W: number, H: number, measure?: Measure): Patch {
  if (orig.type === "line" || orig.type === "arrow") {
    if (handle === "p0") return { x1: clamp(orig.x1 + dx, 0, W), y1: clamp(orig.y1 + dy, 0, H) } as Patch;
    return { x2: clamp(orig.x2 + dx, 0, W), y2: clamp(orig.y2 + dy, 0, H) } as Patch;
  }
  const { x, y, w, h } = bbox(orig);
  let x1 = x, y1 = y, x2 = x + w, y2 = y + h;
  if (handle.includes("w")) x1 = Math.min(x1 + dx, x2 - 4);
  if (handle.includes("e")) x2 = Math.max(x2 + dx, x1 + 4);
  if (handle.includes("n")) y1 = Math.min(y1 + dy, y2 - 4);
  if (handle.includes("s")) y2 = Math.max(y2 + dy, y1 + 4);
  let nw = x2 - x1, nh = y2 - y1;
  const lock = orig.type === "signature" || (shift && handle.length === 2);
  if (lock && w > 0 && h > 0) {
    const r = handle.length === 2 ? Math.max(nw / w, nh / h) : nw / w;
    nw = w * r; nh = h * r;
    if (handle.includes("w")) x1 = x2 - nw;
    if (handle.includes("n")) y1 = y2 - nh;
  }
  return setBox(orig, { x: x1, y: y1, w: nw, h: nh }, measure);
}

// ------------------------------------------------------------- recognition ---

export type Recognised =
  | { type: "line"; x1: number; y1: number; x2: number; y2: number }
  | { type: "rect" | "oval"; x: number; y: number; w: number; h: number };

/** markup.py recognise(): a nearly straight stroke becomes a line, a closed one a rectangle or an oval. */
export function recognise(stroke: StrokePoints): Recognised | null {
  const pts = triples(stroke);
  if (pts.length < 6) return null;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), y0 = Math.min(...ys), x1 = Math.max(...xs), y1 = Math.max(...ys);
  const w = x1 - x0, h = y1 - y0;
  const diag = Math.hypot(w, h);
  if (diag < 10) return null;
  const a = pts[0]!, b = pts[pts.length - 1]!;
  const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
  if (L > 0.85 * diag) {
    const dev = Math.max(...pts.map((p) => segDist(p[0], p[1], a[0], a[1], b[0], b[1])));
    return dev < 0.05 * L ? { type: "line", x1: a[0], y1: a[1], x2: b[0], y2: b[1] } : null;
  }
  if (L > 0.2 * diag || Math.min(w, h) < 0.15 * Math.max(w, h)) return null;
  let area = 0;
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i]!, q = pts[(i + 1) % pts.length]!;
    area += p[0] * q[1] - q[0] * p[1];
  }
  const ratio = Math.abs(area) / 2 / Math.max(w * h, 1e-6);
  const edge = pts.reduce((s, p) => s + Math.min(p[0] - x0, x1 - p[0], p[1] - y0, y1 - p[1]), 0) / pts.length;
  if (ratio > 0.88 || edge < 0.04 * Math.min(w, h)) return { type: "rect", x: x0, y: y0, w, h };
  if (ratio > 0.65) return { type: "oval", x: x0, y: y0, w, h };
  return null;
}

// ------------------------------------------------------------------- text ---

export const LINE_HEIGHT = 1.2;
export const TEXT_PAD = 2;

/** Word-wrap `text` to `maxW` (breaking long words), like Pango WORD_CHAR. */
export function wrapLines(text: string, maxW: number, font: FontSpec, measure: Measure): string[] {
  const out: string[] = [];
  for (const para of text.split("\n")) {
    let line = "";
    for (const tok of para.split(/(\s+)/)) {
      if (!tok) continue;
      if (measure((line + tok).trimEnd(), font) <= maxW) { line += tok; continue; }
      if (/^\s+$/.test(tok)) { out.push(line.trimEnd()); line = ""; continue; }
      if (line.trim()) { out.push(line.trimEnd()); line = ""; }
      if (measure(tok, font) <= maxW) { line = tok; continue; }
      // the word alone is too wide: break it by characters
      for (const ch of tok) {
        if (line && measure(line + ch, font) > maxW) { out.push(line); line = ""; }
        line += ch;
      }
    }
    out.push(line.trimEnd());
  }
  return out;
}

export function textLines(a: { text: string; w: number; style: MarkupStyle }, measure: Measure): string[] {
  const f = fontOf(a);
  return wrapLines(a.text, Math.max(a.w - 2 * TEXT_PAD, 4), f, measure);
}

/** Height a text box needs for its text (markup.py text_height). */
export function textHeight(a: { text: string; w: number; style: MarkupStyle }, measure: Measure): number {
  const f = fontOf(a);
  const n = textLines(a, measure).length;
  return Math.max(n * f.size * LINE_HEIGHT, f.size * LINE_HEIGHT) + 2 * TEXT_PAD;
}

/** Natural (unwrapped) size of a string, padding included. */
export function textExtent(text: string, font: FontSpec, measure: Measure): { w: number; h: number } {
  const lines = text.split("\n");
  return {
    w: Math.max(...lines.map((l) => measure(l, font))) + 2 * TEXT_PAD + 1,
    h: lines.length * font.size * LINE_HEIGHT + 2 * TEXT_PAD,
  };
}

// ------------------------------------------------------------------ paths ---

const KAPPA = 0.5522847498;

class PathBuilder {
  cmds: Cmd[] = [];
  private cx = 0;
  private cy = 0;
  private has = false;
  move(x: number, y: number) { this.cmds.push({ o: "M", x, y }); this.cx = x; this.cy = y; this.has = true; return this; }
  line(x: number, y: number) {
    if (!this.has) return this.move(x, y);
    this.cmds.push({ o: "L", x, y }); this.cx = x; this.cy = y; return this;
  }
  curve(x1: number, y1: number, x2: number, y2: number, x: number, y: number) {
    this.cmds.push({ o: "C", x1, y1, x2, y2, x, y }); this.cx = x; this.cy = y; return this;
  }
  /** Quadratic Bézier from the current point, as a cubic. */
  quad(qx: number, qy: number, x: number, y: number) {
    const { cx, cy } = this;
    return this.curve(cx + 2 / 3 * (qx - cx), cy + 2 / 3 * (qy - cy), x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y), x, y);
  }
  /** cairo_arc semantics: a line to the arc start, then the arc (clockwise in y-down space). */
  arc(cx: number, cy: number, r: number, a0: number, a1: number) {
    this.line(cx + r * Math.cos(a0), cy + r * Math.sin(a0));
    const n = Math.max(1, Math.ceil(Math.abs(a1 - a0) / (Math.PI / 2) - 1e-9));
    const step = (a1 - a0) / n;
    const k = 4 / 3 * Math.tan(step / 4);
    for (let i = 0; i < n; i++) {
      const s = a0 + i * step, e = s + step;
      const [cs, ss, ce, se] = [Math.cos(s), Math.sin(s), Math.cos(e), Math.sin(e)];
      this.curve(cx + r * (cs - k * ss), cy + r * (ss + k * cs), cx + r * (ce + k * se), cy + r * (se - k * ce),
        cx + r * ce, cy + r * se);
    }
    return this;
  }
  close() { this.cmds.push({ o: "Z" }); this.has = false; return this; }
}

export function rectPath(x: number, y: number, w: number, h: number): Cmd[] {
  return new PathBuilder().move(x, y).line(x + w, y).line(x + w, y + h).line(x, y + h).close().cmds;
}

export function ellipsePath(cx: number, cy: number, rx: number, ry: number): Cmd[] {
  const ox = rx * KAPPA, oy = ry * KAPPA;
  return new PathBuilder().move(cx + rx, cy)
    .curve(cx + rx, cy + oy, cx + ox, cy + ry, cx, cy + ry)
    .curve(cx - ox, cy + ry, cx - rx, cy + oy, cx - rx, cy)
    .curve(cx - rx, cy - oy, cx - ox, cy - ry, cx, cy - ry)
    .curve(cx + ox, cy - ry, cx + rx, cy - oy, cx + rx, cy)
    .close().cmds;
}

/** Star outline points (markup.py _shape_path). */
export function starPoints(x: number, y: number, w: number, h: number): [number, number][] {
  const cx = x + w / 2, cy = y + h / 2;
  const pts: [number, number][] = [];
  for (let i = 0; i < 10; i++) {
    const r = i % 2 === 0 ? 0.5 : 0.2;
    const a = -Math.PI / 2 + i * Math.PI / 5;
    pts.push([cx + Math.cos(a) * w * r, cy + Math.sin(a) * h * r * 1.05 + h * 0.03]);
  }
  return pts;
}

export function shapePath(type: "rect" | "rrect" | "oval" | "bubble" | "star", b: Box): Cmd[] {
  const { x, y, w, h } = b;
  if (type === "rect") return rectPath(x, y, w, h);
  if (type === "oval") return w > 0 && h > 0 ? ellipsePath(x + w / 2, y + h / 2, w / 2, h / 2) : [];
  if (type === "star") {
    const p = new PathBuilder();
    starPoints(x, y, w, h).forEach(([px, py], i) => (i ? p.line(px, py) : p.move(px, py)));
    return p.close().cmds;
  }
  const bh = type === "bubble" ? h * 0.78 : h;
  const r = Math.max(0, Math.min(w, bh) * 0.18);
  const p = new PathBuilder();
  p.arc(x + w - r, y + r, r, -Math.PI / 2, 0);
  p.arc(x + w - r, y + bh - r, r, 0, Math.PI / 2);
  if (type === "bubble") {
    p.line(x + w * 0.38, y + bh);
    p.line(x + w * 0.16, y + h);
    p.line(x + w * 0.24, y + bh);
  }
  p.arc(x + r, y + bh - r, r, Math.PI / 2, Math.PI);
  p.arc(x + r, y + r, r, Math.PI, 1.5 * Math.PI);
  return p.close().cmds;
}

/** Flatten a path to polygon vertices (for /Vertices of PDF polygons). */
export function flattenPath(cmds: Cmd[], steps = 6): [number, number][] {
  const out: [number, number][] = [];
  let cx = 0, cy = 0;
  for (const c of cmds) {
    if (c.o === "M" || c.o === "L") { out.push([c.x, c.y]); cx = c.x; cy = c.y; }
    else if (c.o === "C") {
      for (let i = 1; i <= steps; i++) {
        const t = i / steps, u = 1 - t;
        out.push([
          u * u * u * cx + 3 * u * u * t * c.x1 + 3 * u * t * t * c.x2 + t * t * t * c.x,
          u * u * u * cy + 3 * u * u * t * c.y1 + 3 * u * t * t * c.y2 + t * t * t * c.y,
        ]);
      }
      cx = c.x; cy = c.y;
    }
  }
  return out;
}

/** Quadratic-midpoint smoothing through points (markup.py _smooth_path). */
export function smoothPath(pts: [number, number, number][]): Cmd[] {
  const p = new PathBuilder();
  if (!pts.length) return [];
  const [x0, y0] = pts[0]!;
  p.move(x0, y0);
  if (pts.length === 1) return p.line(x0 + 0.01, y0).cmds;
  if (pts.length === 2) return p.line(pts[1]![0], pts[1]![1]).cmds;
  for (let i = 1; i < pts.length - 1; i++) {
    const [cx, cy] = pts[i]!, [nx, ny] = pts[i + 1]!;
    p.quad(cx, cy, (cx + nx) / 2, (cy + ny) / 2);
  }
  const last = pts[pts.length - 1]!;
  return p.line(last[0], last[1]).cmds;
}

/** Closed outline (perfect-freehand polygon) as a smooth path. */
function outlinePath(outline: number[][]): Cmd[] {
  const n = outline.length;
  if (n < 2) return [];
  const p = new PathBuilder();
  const at = (i: number) => outline[i % n]!;
  const mid = (i: number) => [(at(i)[0]! + at(i + 1)[0]!) / 2, (at(i)[1]! + at(i + 1)[1]!) / 2] as const;
  const m0 = mid(0);
  p.move(m0[0], m0[1]);
  for (let i = 1; i <= n; i++) {
    const c = at(i), m = mid(i);
    p.quad(c[0]!, c[1]!, m[0], m[1]);
  }
  return p.close().cmds;
}

export const hasPressure = (s: StrokePoints) => {
  for (let i = 2; i < s.length; i += 3) if (Math.abs(s[i]! - 0.5) > 0.02) return true;
  return false;
};

/** Variable-width ink outline for a stroke (filled). */
export function inkOutline(pts: [number, number, number][], size: number): Cmd[] {
  const outline = getStroke(pts, {
    size, thinning: 0.6, smoothing: 0.5, streamline: 0.35, simulatePressure: false, last: true,
  });
  return outlinePath(outline);
}

/**
 * Pseudo-pressure from pen speed (signature.py _widths): fast = thin, slow =
 * thick. `v` is speed in units/s, `vmax` the speed of the thinnest line.
 * Returns the smoothed pressure, given the previous one.
 */
export function speedPressure(prev: number | null, v: number, vmax: number): number {
  const factor = 1.3 - 0.75 * Math.min(v / vmax, 1); // width factor 0.55..1.3
  const target = Math.min(1, Math.max(0, (factor - 0.4) / 1.2)); // perfect-freehand thinning 0.6
  return prev === null ? target : prev + 0.3 * (target - prev);
}

// ---------------------------------------------------------------- drawing ---

function noteIcon(x: number, y: number, s: number, color: string): Prim[] {
  const f = s * 0.3;
  const dark = "#735900";
  const body = new PathBuilder().move(x, y).line(x + s, y).line(x + s, y + s - f).line(x + s - f, y + s).line(x, y + s).close();
  const fold = new PathBuilder().move(x + s - f, y + s).line(x + s - f, y + s - f).line(x + s, y + s - f);
  const lines = new PathBuilder();
  for (let i = 0; i < 3; i++) {
    const yy = y + s * (0.28 + 0.2 * i);
    lines.move(x + s * 0.2, yy).line(x + s * (i < 2 ? 0.8 : 0.55), yy);
  }
  return [
    { t: "path", cmds: body.cmds, fill: color, stroke: dark, width: s * 0.05 },
    { t: "path", cmds: fold.cmds, fill: null, stroke: dark, width: s * 0.05 },
    { t: "path", cmds: lines.cmds, fill: null, stroke: dark, width: s * 0.06 },
  ];
}

function quadPts(q: number[], i: number) {
  // TL, TR, BR, BL
  return {
    tl: [q[i]!, q[i + 1]!], tr: [q[i + 2]!, q[i + 3]!], br: [q[i + 4]!, q[i + 5]!], bl: [q[i + 6]!, q[i + 7]!],
  } as const;
}

/** Text lines placed in a text box (x = line start, y = baseline). */
export function placeText(a: { x: number; y: number; w: number; text: string; style: MarkupStyle }, measure: Measure) {
  const f = fontOf(a);
  const maxW = Math.max(a.w - 2 * TEXT_PAD, 4);
  return textLines(a, measure).map((text, i) => {
    const lw = measure(text, f);
    const dx = f.align === "center" ? (maxW - lw) / 2 : f.align === "right" ? maxW - lw : 0;
    return { text, x: a.x + TEXT_PAD + dx, y: a.y + TEXT_PAD + i * f.size * LINE_HEIGHT + f.size * 0.92 };
  });
}

/** Signature strokes (normalized 0..1 in the box) mapped into the box. */
export function signatureStrokes(a: { x: number; y: number; w: number; h: number; strokes: StrokePoints[] }) {
  return a.strokes.map((s) => triples(s).map(([u, v, p]) => [a.x + u * a.w, a.y + v * a.h, p] as [number, number, number]));
}

/** The drawing of one annotation, in surface units. */
export function drawAnnotation(a: Ann, measure: Measure): Prim[] {
  const st = a.style;
  switch (a.type) {
    case "rect": case "rrect": case "oval": case "bubble": case "star": {
      const cmds = shapePath(a.type, a);
      return [{ t: "path", cmds, fill: st.fill, stroke: st.width > 0 ? st.stroke : null, width: st.width, cap: "round" }];
    }
    case "line":
      return [{ t: "path", cmds: [{ o: "M", x: a.x1, y: a.y1 }, { o: "L", x: a.x2, y: a.y2 }], fill: null, stroke: st.stroke, width: st.width, cap: "round" }];
    case "arrow": {
      const { x1, y1, x2, y2 } = a;
      const L = Math.hypot(x2 - x1, y2 - y1);
      const head = Math.max(6, st.width * 4);
      if (L < 1e-6) return [];
      const ux = (x2 - x1) / L, uy = (y2 - y1) / L;
      const bx = x2 - ux * head * 0.9, by = y2 - uy * head * 0.9;
      const px = -uy * head * 0.5, py = ux * head * 0.5;
      return [
        { t: "path", cmds: [{ o: "M", x: x1, y: y1 }, { o: "L", x: bx, y: by }], fill: null, stroke: st.stroke, width: st.width, cap: "round" },
        { t: "path", cmds: new PathBuilder().move(x2, y2).line(x2 - ux * head + px, y2 - uy * head + py)
          .line(x2 - ux * head - px, y2 - uy * head - py).close().cmds, fill: st.stroke, stroke: null, width: 0 },
      ];
    }
    case "ink": {
      const out: Prim[] = [];
      for (const s of a.strokes) {
        const pts = triples(s);
        if (!pts.length) continue;
        if (hasPressure(s)) out.push({ t: "path", cmds: inkOutline(pts, st.width), fill: st.stroke, stroke: null, width: 0 });
        else if (pts.length === 1) out.push({ t: "path", cmds: ellipsePath(pts[0]![0], pts[0]![1], st.width / 2, st.width / 2), fill: st.stroke, stroke: null, width: 0 });
        else out.push({ t: "path", cmds: smoothPath(pts), fill: null, stroke: st.stroke, width: st.width, cap: "round" });
      }
      return out;
    }
    case "signature":
      return signatureStrokes(a).filter((p) => p.length)
        .map((pts) => ({ t: "path", cmds: inkOutline(pts, st.width), fill: st.stroke, stroke: null, width: 0 }));
    case "text": {
      const out: Prim[] = [];
      if (st.fill) out.push({ t: "path", cmds: rectPath(a.x, a.y, a.w, a.h), fill: st.fill, stroke: null, width: 0 });
      out.push({ t: "text", lines: placeText(a, measure), font: fontOf(a), color: st.stroke });
      return out;
    }
    case "note":
      return noteIcon(a.x, a.y, NOTE_SIZE, st.stroke || NOTE_COLOR);
    case "highlight": case "underline": case "strike": {
      const out: Prim[] = [];
      const q = a.quads;
      for (let i = 0; i + 7 < q.length; i += 8) {
        const { tl, tr, br, bl } = quadPts(q, i);
        if (a.type === "highlight") {
          out.push({ t: "path", cmds: new PathBuilder().move(tl[0], tl[1]).line(tr[0], tr[1]).line(br[0], br[1]).line(bl[0], bl[1]).close().cmds,
            fill: st.stroke, stroke: null, width: 0, alpha: 0.55, blend: "multiply" });
          continue;
        }
        const qh = Math.hypot(bl[0] - tl[0], bl[1] - tl[1]);
        const lw = Math.max(qh * 0.07, 0.6);
        // fraction from the top edge towards the bottom edge
        const f = a.type === "underline" ? Math.max(0, 1 - (lw * 1.2) / Math.max(qh, 1e-6)) : 0.55;
        const lx = tl[0] + (bl[0] - tl[0]) * f, ly = tl[1] + (bl[1] - tl[1]) * f;
        const rx = tr[0] + (br[0] - tr[0]) * f, ry = tr[1] + (br[1] - tr[1]) * f;
        out.push({ t: "path", cmds: [{ o: "M", x: lx, y: ly }, { o: "L", x: rx, y: ry }], fill: null, stroke: st.stroke, width: lw, cap: "butt" });
      }
      return out;
    }
  }
}

/** SVG path data for commands. */
export function svgD(cmds: Cmd[]): string {
  const r = (v: number) => Math.round(v * 100) / 100;
  return cmds.map((c) => c.o === "Z" ? "Z" : c.o === "C"
    ? `C${r(c.x1)} ${r(c.y1)} ${r(c.x2)} ${r(c.y2)} ${r(c.x)} ${r(c.y)}`
    : `${c.o}${r(c.x)} ${r(c.y)}`).join("");
}

// ------------------------------------------------------------- text runs ---

export interface Char { text: string; q: number[]; cx: number; cy: number; h: number; run: number }

/** Flatten TextRuns into characters in reading order. */
export function charsOf(runs: { text: string; quads: number[] }[]): Char[] {
  const out: Char[] = [];
  runs.forEach((r, ri) => {
    const n = Math.floor(r.quads.length / 8);
    const cp = Array.from(r.text);
    const chars = cp.length === n ? cp : r.text.length === n ? r.text.split("") : cp;
    for (let i = 0; i < n; i++) {
      const q = r.quads.slice(i * 8, i * 8 + 8);
      const b = quadsBox(q);
      out.push({ text: chars[i] ?? "", q, cx: b.x + b.w / 2, cy: b.y + b.h / 2, h: Math.max(b.h, 1e-3), run: ri });
    }
  });
  return out;
}

function nearestChar(chars: Char[], x: number, y: number): number {
  let best = -1, bd = Infinity;
  chars.forEach((c, i) => {
    const b = quadsBox(c.q);
    const dx = Math.max(b.x - x, 0, x - (b.x + b.w));
    const dy = Math.max(b.y - y, 0, y - (b.y + b.h));
    // vertical distance counts more: prefer the line under the pointer
    const d = dx + dy * 4;
    if (d < bd) { bd = d; best = i; }
  });
  return best;
}

/**
 * Text selection from p0 to p1 over characters (reading order), merged into one
 * quad per line: the desktop's mk_text_quads.
 */
export function selectText(chars: Char[], x0: number, y0: number, x1: number, y1: number): { text: string; quads: number[] } {
  if (!chars.length) return { text: "", quads: [] };
  let a = nearestChar(chars, x0, y0), b = nearestChar(chars, x1, y1);
  if (a > b) [a, b] = [b, a];
  const quads: number[] = [];
  let text = "";
  let start: Char | null = null, prev: Char | null = null;
  const flush = () => {
    if (!start || !prev) return;
    quads.push(start.q[0]!, start.q[1]!, prev.q[2]!, prev.q[3]!, prev.q[4]!, prev.q[5]!, start.q[6]!, start.q[7]!);
  };
  for (let i = a; i <= b; i++) {
    const c = chars[i]!;
    const sameLine = prev && prev.run === c.run
      ? true
      : prev ? Math.abs(prev.cy - c.cy) < Math.min(prev.h, c.h) * 0.5 && c.cx >= prev.cx : false;
    if (prev && !sameLine) {
      flush();
      start = null;
      if (!/\s$/.test(text)) text += prev.run === c.run ? "" : "\n";
    } else if (prev && prev.run !== c.run && !/\s$/.test(text) && !/^\s/.test(c.text)) {
      text += " ";
    }
    if (/^\s*$/.test(c.text) && !start) { prev = c; text += c.text; continue; }
    start ??= c;
    prev = c;
    text += c.text;
  }
  flush();
  return { text: text.trim(), quads };
}
