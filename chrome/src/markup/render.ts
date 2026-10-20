// Painting the geom.ts drawing description into SVG (the live overlay) and onto
// a 2D canvas (MarkupController.render: image flattening, thumbnails).
import type { Annotation } from "./api.ts";
import { drawAnnotation, type FontSpec, type Measure, type Prim, svgD } from "./geom.ts";

export const SVG_NS = "http://www.w3.org/2000/svg";

const GENERIC = new Set(["serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"]);

export function cssFamily(family: string): string {
  if (GENERIC.has(family) || /[,"']/.test(family)) return family;
  return `"${family}", sans-serif`;
}

export function cssFont(f: FontSpec): string {
  return `${f.italic ? "italic " : ""}${f.bold ? "bold " : ""}${f.size}px ${cssFamily(f.family)}`;
}

let mctx: OffscreenCanvasRenderingContext2D | CanvasRenderingContext2D | null = null;
let lastFont = "";
/** Text width with the browser's own font metrics (what the SVG overlay shows). */
export const measure: Measure = (text, f) => {
  mctx ??= typeof OffscreenCanvas !== "undefined"
    ? new OffscreenCanvas(1, 1).getContext("2d")
    : document.createElement("canvas").getContext("2d");
  if (!mctx) return text.length * f.size * 0.55;
  const font = cssFont(f);
  if (font !== lastFont) { mctx.font = font; lastFont = font; }
  return mctx.measureText(text).width;
};

export function svgEl<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}

/** Build SVG for prims. Highlight prims (multiply blend) go to `under` when given. */
export function primsToSvg(prims: Prim[], opacity: number, g: SVGGElement, under?: SVGGElement): void {
  if (opacity < 1) { g.setAttribute("opacity", String(opacity)); under?.setAttribute("opacity", String(opacity)); }
  for (const p of prims) {
    if (p.t === "path") {
      const path = svgEl("path", { d: svgD(p.cmds) });
      path.setAttribute("fill", p.fill ?? "none");
      if (p.alpha !== undefined && p.fill) path.setAttribute("fill-opacity", String(p.alpha));
      if (p.stroke && p.width > 0) {
        path.setAttribute("stroke", p.stroke);
        path.setAttribute("stroke-width", String(p.width));
        path.setAttribute("stroke-linecap", p.cap ?? "round");
        path.setAttribute("stroke-linejoin", "round");
      }
      (p.blend === "multiply" && under ? under : g).append(path);
    } else {
      const t = svgEl("text", {
        "font-family": cssFamily(p.font.family), "font-size": p.font.size,
        "font-weight": p.font.bold ? "bold" : "normal", "font-style": p.font.italic ? "italic" : "normal",
        fill: p.color,
      });
      t.setAttribute("xml:space", "preserve");
      t.style.whiteSpace = "pre";
      for (const l of p.lines) {
        const ts = svgEl("tspan", { x: l.x, y: l.y });
        ts.textContent = l.text;
        t.append(ts);
      }
      g.append(t);
    }
  }
}

function path2d(p: Extract<Prim, { t: "path" }>): Path2D {
  const path = new Path2D();
  for (const c of p.cmds) {
    if (c.o === "M") path.moveTo(c.x, c.y);
    else if (c.o === "L") path.lineTo(c.x, c.y);
    else if (c.o === "C") path.bezierCurveTo(c.x1, c.y1, c.x2, c.y2, c.x, c.y);
    else path.closePath();
  }
  return path;
}

export function primsToCanvas(ctx: CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D, prims: Prim[], opacity: number): void {
  for (const p of prims) {
    ctx.save();
    ctx.globalAlpha = opacity;
    if (p.t === "path") {
      const path = path2d(p);
      if (p.blend === "multiply") ctx.globalCompositeOperation = "multiply";
      if (p.fill) {
        ctx.globalAlpha = opacity * (p.alpha ?? 1);
        ctx.fillStyle = p.fill;
        ctx.fill(path);
        ctx.globalAlpha = opacity;
      }
      if (p.stroke && p.width > 0) {
        ctx.strokeStyle = p.stroke;
        ctx.lineWidth = p.width;
        ctx.lineCap = p.cap ?? "round";
        ctx.lineJoin = "round";
        ctx.stroke(path);
      }
    } else {
      ctx.font = cssFont(p.font);
      ctx.fillStyle = p.color;
      ctx.textBaseline = "alphabetic";
      for (const l of p.lines) ctx.fillText(l.text, l.x, l.y);
    }
    ctx.restore();
  }
}

export function renderAnnotations(ctx: CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D,
  anns: Annotation[], unitsToPx: number): void {
  ctx.save();
  ctx.scale(unitsToPx, unitsToPx);
  for (const a of anns) primsToCanvas(ctx, drawAnnotation(a, measure), a.style.opacity ?? 1);
  ctx.restore();
}
