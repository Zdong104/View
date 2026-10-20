// pdf.js annotations -> editable Annotation[] (the inverse of pdf-export.ts),
// so markup already in a PDF (ours or another app's) can be moved, restyled
// and deleted. Uses PDFPageProxy.getAnnotations(). pdf.js does not expose
// everything (line direction: it normalizes /L; interior colour; opacity of
// shapes; /NM ids; text-box alignment; the selected text of highlights), so
// the same file is also parsed with @cantoo/pdf-lib (`raw`): pass a loaded
// document, let it be loaded from doc.getData() when there is markup to
// import (default), or pass raw: null for a pdf.js-only (lossy) import.
//
// The viewer that imports should (1) hide the imported annotations from pdf.js
// rendering, and (2) on save call removePdfAnnotations(doc, sourceIds) from
// pdf-export.ts before writePdfAnnotations so they are not duplicated.
import { PDFDocument, PDFArray, PDFDict, PDFHexString, PDFName, PDFNumber, PDFRef, PDFString } from "@cantoo/pdf-lib";
import type { Annotation, MarkupStyle, StrokePoints } from "./api.ts";
import { defaultFont, toHex } from "./geom.ts";

/** The parts of pdf.js's PDFDocumentProxy we use (works with the legacy build too). */
export interface PdfJsDocLike {
  numPages: number;
  getData?(): Promise<Uint8Array>;
  getPage(n: number): Promise<{ view: number[]; getAnnotations(params?: { intent?: string }): Promise<unknown[]> }>;
}

export interface ImportResult {
  annotations: Annotation[];
  /** pdf.js ids ("12R") of the PDF annotations that were imported. */
  sourceIds: string[];
}

interface Data {
  subtype?: string; id?: string; rect?: number[]; color?: ArrayLike<number> | null; it?: string;
  annotationFlags?: number; borderStyle?: { width?: number; horizontalCornerRadius?: number };
  contentsObj?: { str?: string }; titleObj?: { str?: string }; modificationDate?: string | null; creationDate?: string | null;
  inkLists?: ArrayLike<number>[]; lineCoordinates?: number[]; lineEndings?: string[]; vertices?: ArrayLike<number> | null;
  quadPoints?: ArrayLike<number> | null; opacity?: number; textContent?: string[];
  defaultAppearanceData?: { fontSize?: number; fontName?: string; fontColor?: ArrayLike<number> };
}

const HIDDEN = 2;
const SUPPORTED = new Set(["Square", "Circle", "Polygon", "PolyLine", "Line", "Ink", "FreeText", "Text",
  "Highlight", "Underline", "StrikeOut", "Squiggly"]);

export function parsePdfDate(s: string | null | undefined): number | null {
  const m = /^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([Z+-])?(\d{2})?'?(\d{2})?/.exec(s ?? "");
  if (!m) return null;
  const n = (i: number, d: number) => (m[i] ? Number(m[i]) : d);
  let t = Date.UTC(n(1, 0), n(2, 1) - 1, n(3, 1), n(4, 0), n(5, 0), n(6, 0));
  if (m[7] === "+" || m[7] === "-") t -= (m[7] === "+" ? 1 : -1) * (n(8, 0) * 60 + n(9, 0)) * 60_000;
  return t;
}

const hex255 = (c: ArrayLike<number> | null | undefined, d: string) =>
  c && c.length >= 3 ? toHex(c[0]! / 255, c[1]! / 255, c[2]! / 255) : d;

function rawDict(raw: PDFDocument | undefined, id: string | undefined): PDFDict | undefined {
  if (!raw || !id) return undefined;
  const m = /^(\d+)R(\d*)$/.exec(id);
  if (!m) return undefined;
  const o = raw.context.lookup(PDFRef.of(Number(m[1]), Number(m[2] || 0)));
  return o instanceof PDFDict ? o : undefined;
}

function rawText(d: PDFDict | undefined, k: string): string | undefined {
  const v = d?.lookup(PDFName.of(k));
  return v instanceof PDFString || v instanceof PDFHexString ? v.decodeText() : undefined;
}
function rawNums(d: PDFDict | undefined, k: string): number[] | undefined {
  const v = d?.lookup(PDFName.of(k));
  if (!(v instanceof PDFArray)) return undefined;
  return v.asArray().map((x) => (x instanceof PDFNumber ? x.asNumber() : 0));
}
function rawNum(d: PDFDict | undefined, k: string): number | undefined {
  const v = d?.lookup(PDFName.of(k));
  return v instanceof PDFNumber ? v.asNumber() : undefined;
}

function fontFromName(name: string | undefined) {
  const n = (name ?? "Helv").replace(/^\//, "");
  const family = /^(Ti|Times)/i.test(n) ? "serif" : /^(Co|Courier)/i.test(n) ? "monospace" : "sans-serif";
  const bold = /^(HeBo|HeBO|TiBo|TiBI|CoBo|CoBO)$/.test(n) || /bold/i.test(n);
  const italic = /^(HeOb|HeBO|TiIt|TiBI|CoOb|CoBO)$/.test(n) || /italic|oblique/i.test(n);
  return { family, bold, italic };
}

/** Import the markup annotations of `doc` (all pages, or opts.pages, 0-based). */
export async function importPdfAnnotations(doc: PdfJsDocLike, opts: {
  pages?: number[];
  raw?: PDFDocument | null;
  surfaceOf?: (pageIndex: number) => string;
} = {}): Promise<ImportResult> {
  let raw = opts.raw ?? undefined;
  let wantRaw = opts.raw !== null && !raw && !!doc.getData;
  const out: Annotation[] = [];
  const sourceIds: string[] = [];
  const pages = opts.pages ?? Array.from({ length: doc.numPages }, (_, i) => i);
  const used = new Set<string>();
  for (const pi of pages) {
    const page = await doc.getPage(pi + 1);
    const [vx = 0, , , vy1 = 0] = page.view;
    const mx = (X: number) => X - vx;
    const my = (Y: number) => vy1 - Y;
    const surface = opts.surfaceOf?.(pi) ?? `p${pi}`;
    const items = (await page.getAnnotations({ intent: "display" })) as Data[];
    if (wantRaw && items.some((d) => d.subtype && SUPPORTED.has(d.subtype))) {
      wantRaw = false;
      try {
        raw = await PDFDocument.load(await doc.getData!(), { ignoreEncryption: true, updateMetadata: false, throwOnInvalidObject: false });
      } catch { raw = undefined; }
    }
    for (const d of items) {
      if (!d.subtype || !d.rect || (d.annotationFlags ?? 0) & HIDDEN) continue;
      const rd0 = rawDict(raw, d.id);
      const [rx0, ry0, rx1, ry1] = d.rect as [number, number, number, number];
      const width = d.borderStyle?.width ?? 1;
      const stroke = hex255(d.color, "#000000");
      const ic = rawNums(rd0, "IC");
      const fill = ic && ic.length >= 3 ? toHex(ic[0]!, ic[1]!, ic[2]!) : null;
      const opacity = rawNum(rd0, "CA") ?? d.opacity ?? 1;
      const style: MarkupStyle = { stroke, fill, width, opacity };
      let id = rawText(rd0, "NM") ?? `pdf:${d.id ?? out.length}`;
      if (used.has(id)) id = `${id}:${out.length}`;
      used.add(id);
      const base = {
        id, surface, style,
        created: parsePdfDate(d.creationDate) ?? parsePdfDate(d.modificationDate) ?? 0,
        modified: parsePdfDate(d.modificationDate) ?? 0,
        ...(d.titleObj?.str ? { author: d.titleObj.str } : {}),
      };
      const contents = d.contentsObj?.str || undefined;
      const comment = contents ? { comment: contents } : {};
      // the box inside the rect: /RD when known, else half the border
      const rd = rawNums(rd0, "RD");
      const [l, t, r, b] = rd && rd.length === 4 ? rd as [number, number, number, number] : [width / 2, width / 2, width / 2, width / 2];
      const box = { x: mx(rx0 + l), y: my(ry1 - t), w: Math.max(0, rx1 - rx0 - l - r), h: Math.max(0, ry1 - ry0 - t - b) };
      let a: Annotation | null = null;
      switch (d.subtype) {
        case "Square":
          a = { ...base, ...comment, ...box, type: d.it === "ViewRRect" || (d.borderStyle?.horizontalCornerRadius ?? 0) > 0 ? "rrect" : "rect" };
          break;
        case "Circle":
          a = { ...base, ...comment, ...box, type: "oval" };
          break;
        case "Polygon": case "PolyLine": {
          if (d.subtype === "Polygon" && (d.it === "ViewBubble" || d.it === "ViewStar")) {
            a = { ...base, ...comment, ...box, type: d.it === "ViewBubble" ? "bubble" : "star" };
            break;
          }
          const v = Array.from(d.vertices ?? []);
          if (v.length < 4) break;
          if (d.subtype === "Polygon") v.push(v[0]!, v[1]!);
          const s: StrokePoints = [];
          for (let i = 0; i + 1 < v.length; i += 2) s.push(mx(v[i]!), my(v[i + 1]!), 0.5);
          a = { ...base, ...comment, style: { ...style, fill: null }, type: "ink", strokes: [s] };
          break;
        }
        case "Line": {
          const [x1 = 0, y1 = 0, x2 = 0, y2 = 0] = rawNums(rd0, "L") ?? d.lineCoordinates ?? [];
          const le = d.lineEndings ?? [];
          const arrowEnd = /Arrow/.test(le[1] ?? ""), arrowStart = /Arrow/.test(le[0] ?? "");
          const pts = arrowStart && !arrowEnd ? [x2, y2, x1, y1] : [x1, y1, x2, y2];
          a = { ...base, ...comment, style: { ...style, fill: null }, type: arrowEnd || arrowStart || d.it === "LineArrow" ? "arrow" : "line",
            x1: mx(pts[0]!), y1: my(pts[1]!), x2: mx(pts[2]!), y2: my(pts[3]!) };
          break;
        }
        case "Ink": {
          const strokes = (d.inkLists ?? []).map((list) => {
            const s: StrokePoints = [];
            for (let i = 0; i + 1 < list.length; i += 2) s.push(mx(list[i]!), my(list[i + 1]!), 0.5);
            return s;
          }).filter((s) => s.length);
          if (strokes.length) a = { ...base, ...comment, style: { ...style, fill: null }, type: "ink", strokes };
          break;
        }
        case "FreeText": {
          const da = d.defaultAppearanceData;
          const ff = fontFromName(da?.fontName);
          const q = rawNum(rd0, "Q");
          const align = q === 1 ? "center" : q === 2 ? "right" : "left";
          const textColor = hex255(da?.fontColor, "#000000");
          const bg = d.color && d.color.length >= 3 ? stroke : null;
          const viewComment = rawText(rd0, "ViewComment");
          a = {
            ...base, ...(viewComment ? { comment: viewComment } : {}), ...box, type: "text",
            text: contents ?? (d.textContent ?? []).join("\n"),
            style: { stroke: textColor, fill: bg, width: 0, opacity,
              font: { ...defaultFont(), ...ff, size: da?.fontSize || 12, align } },
          };
          break;
        }
        case "Text":
          a = { ...base, type: "note", x: mx(rx0), y: my(ry1), text: contents ?? "", style: { ...style, fill: null, width: 0 } };
          break;
        case "Highlight": case "Underline": case "StrikeOut": case "Squiggly": {
          const qp = Array.from(d.quadPoints ?? []);
          const quads: number[] = [];
          // pdf.js normalizes each quad to [minX, maxY, maxX, maxY, minX, minY, maxX, minY]
          for (let i = 0; i + 7 < qp.length; i += 8) {
            const [x0, y1, x1, , , y0] = qp.slice(i, i + 8) as [number, number, number, number, number, number];
            quads.push(mx(x0), my(y1), mx(x1), my(y1), mx(x1), my(y0), mx(x0), my(y0));
          }
          if (!quads.length) break;
          const type = d.subtype === "Highlight" ? "highlight" : d.subtype === "StrikeOut" ? "strike" : "underline";
          const viewText = rawText(rd0, "ViewText");
          a = {
            ...base, type, quads, style: { ...style, fill: null, width: 0 },
            ...(viewText !== undefined ? { text: viewText, ...(contents && contents !== viewText ? { comment: contents } : {}) }
              : contents ? { text: contents } : {}),
          };
          break;
        }
        default:
          break;
      }
      if (a) { out.push(a); if (d.id) sourceIds.push(d.id); }
    }
  }
  return { annotations: out, sourceIds };
}
