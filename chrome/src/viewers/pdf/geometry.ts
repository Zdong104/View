// Pure helpers for the PDF viewer: coordinate frames, paper names, file names.
// No DOM, no pdf.js: covered by test/unit/a-geometry.test.ts.
//
// Frames (all in PDF points):
//   surface  the page's unrotated CropBox, origin top-left, y down. This is the
//            markup layer's unit (markup/api.ts) and where OCR lines and search
//            hits are stored, so they survive zoom and rotation unchanged.
//   display  the surface turned clockwise by the total rotation (the page's
//            own /Rotate plus the user's rotation), origin top-left. This is
//            what the canvas shows, scaled by the zoom.
//   user     PDF default user space (origin bottom-left, y up), used on save.

export type Rotation = 0 | 90 | 180 | 270;

export function normRotation(deg: number): Rotation {
  return (((Math.round(deg / 90) * 90) % 360) + 360) % 360 as Rotation;
}

/** Display size of a W × H surface turned by `rot`. */
export function displaySize(rot: number, w: number, h: number): [number, number] {
  return normRotation(rot) % 180 ? [h, w] : [w, h];
}

/** Surface point -> display point (both in points). */
export function surfaceToDisplay(rot: number, w: number, h: number, u: number, v: number): [number, number] {
  switch (normRotation(rot)) {
    case 90: return [h - v, u];
    case 180: return [w - u, h - v];
    case 270: return [v, w - u];
    default: return [u, v];
  }
}

/** Display point -> surface point (inverse of surfaceToDisplay). */
export function displayToSurface(rot: number, w: number, h: number, x: number, y: number): [number, number] {
  switch (normRotation(rot)) {
    case 90: return [y, h - x];
    case 180: return [w - x, h - y];
    case 270: return [w - y, x];
    default: return [x, y];
  }
}

/** Axis-aligned bounding box [x, y, w, h] of a flat point list x1,y1,x2,y2,... */
export function bbox(pts: ArrayLike<number>): [number, number, number, number] {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let i = 0; i + 1 < pts.length; i += 2) {
    const x = pts[i]!, y = pts[i + 1]!;
    if (x < x0) x0 = x;
    if (x > x1) x1 = x;
    if (y < y0) y0 = y;
    if (y > y1) y1 = y;
  }
  return [x0, y0, x1 - x0, y1 - y0];
}

/** Display-frame bbox of a surface quad list (TL,TR,BR,BL per quad). */
export function quadsToDisplayRect(rot: number, w: number, h: number, quad: ArrayLike<number>): [number, number, number, number] {
  const pts: number[] = [];
  for (let i = 0; i + 1 < quad.length; i += 2) pts.push(...surfaceToDisplay(rot, w, h, quad[i]!, quad[i + 1]!));
  return bbox(pts);
}

const PAPER_SIZES: [string, number, number][] = [ // name, width mm, height mm
  ["A3", 297, 420], ["A4", 210, 297], ["A5", 148, 210], ["A6", 105, 148],
  ["B4", 250, 353], ["B5", 176, 250], ["Letter", 215.9, 279.4],
  ["Legal", 215.9, 355.6], ["Tabloid", 279.4, 431.8], ["Executive", 184.15, 266.7],
];

/** preview/pdf_ops.paper_name: "A4", "Letter (landscape)" or null. */
export function paperName(wPt: number, hPt: number): string | null {
  const wm = wPt * 25.4 / 72, hm = hPt * 25.4 / 72;
  const [a, b] = wm < hm ? [wm, hm] : [hm, wm];
  for (const [name, pw, ph] of PAPER_SIZES) {
    if (Math.abs(a - pw) < 2 && Math.abs(b - ph) < 2) return name + (wm > hm ? " (landscape)" : "");
  }
  return null;
}

/** "Page size" row like the desktop Get Info. */
export function pageSizeLabel(wPt: number, hPt: number): string {
  const paper = paperName(wPt, hPt);
  return `${wPt.toFixed(0)} × ${hPt.toFixed(0)} pt (${(wPt * 25.4 / 72).toFixed(0)} × ${(hPt * 25.4 / 72).toFixed(0)} mm, `
    + `${(wPt / 72).toFixed(2)} × ${(hPt / 72).toFixed(2)} in)${paper ? ` — ${paper}` : ""}`;
}

/** preview/pdf_view._ranges: [1,2,3,5] -> "1-3_5" (short form when long). */
export function pageRanges(nums: number[]): string {
  const s = [...new Set(nums)].sort((a, b) => a - b);
  const out: string[] = [];
  let start: number | null = null, prev = 0;
  for (const n of s) {
    if (start === null) { start = prev = n; continue; }
    if (n === prev + 1) { prev = n; continue; }
    out.push(start === prev ? `${start}` : `${start}-${prev}`);
    start = prev = n;
  }
  if (start !== null) out.push(start === prev ? `${start}` : `${start}-${prev}`);
  const joined = out.join("_");
  return joined.length < 40 ? joined : `${s[0]}-${s[s.length - 1]}`;
}

export function stemOf(name: string): string {
  const base = name.replace(/[?#].*$/, "");
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base || "document";
}

/** preview/pdf_view._default_name: "<stem><suffix>.pdf". */
export function defaultName(name: string, suffix: string): string {
  return `${stemOf(name)}${suffix}.pdf`;
}

/** "#page=3&zoom=..." -> 3 (1-based), or null. */
export function pageFromHash(hash: string): number | null {
  const m = /(?:^|[#&])page=(\d+)/.exec(hash);
  return m ? Number(m[1]) : null;
}

/** Largest scale (≤ maxScale) at which a W × H box fits into the view, keeping `margin` on each side. */
export function fitScale(viewW: number, viewH: number, w: number, h: number, margin: number, mode: "width" | "page"): number {
  const sw = (viewW - 2 * margin) / w;
  if (mode === "width") return sw;
  return Math.min(sw, (viewH - 2 * margin) / h);
}
