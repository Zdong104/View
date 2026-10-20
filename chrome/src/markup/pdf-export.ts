// Writing markup into a PDF (WritePdfAnnotations in api.ts; called by the PDF
// viewer's save()). Port of the annotation half of preview/pdf_ops.py, but
// with real annotations for every kind and appearance streams (/AP /N) built
// from the same geom.ts drawing the overlay shows, so every reader renders
// them identically, including readers that ignore annotation geometry.
//
//   rect -> /Square, rrect -> /Square (/IT /ViewRRect), oval -> /Circle,
//   bubble, star -> /Polygon (/IT /ViewBubble | /ViewStar), line -> /Line,
//   arrow -> /Line (/LE ClosedArrow, /IT /LineArrow), ink -> /Ink,
//   text -> /FreeText, note -> /Text, highlight/underline/strike ->
//   /Highlight /Underline /StrikeOut, signature -> page content (vector paths).
// flatten=true draws shapes, ink and text boxes into the page content instead;
// notes and text markup always stay annotations (markup.py ANNOT_KINDS).
//
// Coordinates: surface units are PDF points of the page's unrotated CropBox,
// origin top-left, so user space is X = crop.x + x, Y = crop.y + crop.h - y.
// Text: WinAnsi-only text uses the 14 standard fonts (nothing embedded).
// Other text (CJK…) gets an embedded subset of an installed font found
// through a FontProvider (pdf-fonts.ts: Local Font Access by default); if none
// covers it, those characters are drawn as "?" and a warning is reported.
// /Contents always keeps the full Unicode text.
import {
  type PDFDocument, PDFDict, PDFHexString, PDFName, PDFObject, type PDFFont, type PDFPage, PDFRef, PDFString,
  StandardFonts, drawObject, popGraphicsState, pushGraphicsState,
} from "@cantoo/pdf-lib";
import type { Annotation, WritePdfAnnotations } from "./api.ts";
import {
  bbox, type Cmd, drawAnnotation, flattenPath, fontOf, type FontSpec, NOTE_SIZE, parseColor, type Prim, shapePath, triples,
} from "./geom.ts";
import {
  type FontProvider, type FontRequest, type FontSource, isCollection, isWinAnsi, localFontProvider, ttcFace, ttcNames,
} from "./pdf-fonts.ts";
import { buildSubsetTtf } from "./ttf-build.ts";

/** Annotation kinds drawn into the page when flattening (signatures always are). */
const BOXES = new Set(["rect", "rrect", "oval", "bubble", "star"]);
const FLATTENABLE = new Set(["ink", "rect", "rrect", "oval", "bubble", "star", "line", "arrow", "text"]);

/** AcroForm-style resource names for the standard fonts, so /DA names the font the /AP uses. */
const FONT_KEYS: Record<string, [string, StandardFonts]> = {
  "sans:": ["Helv", StandardFonts.Helvetica], "sans:b": ["HeBo", StandardFonts.HelveticaBold],
  "sans:i": ["HeOb", StandardFonts.HelveticaOblique], "sans:bi": ["HeBO", StandardFonts.HelveticaBoldOblique],
  "serif:": ["TiRo", StandardFonts.TimesRoman], "serif:b": ["TiBo", StandardFonts.TimesRomanBold],
  "serif:i": ["TiIt", StandardFonts.TimesRomanItalic], "serif:bi": ["TiBI", StandardFonts.TimesRomanBoldItalic],
  "mono:": ["Cour", StandardFonts.Courier], "mono:b": ["CoBo", StandardFonts.CourierBold],
  "mono:i": ["CoOb", StandardFonts.CourierOblique], "mono:bi": ["CoBO", StandardFonts.CourierBoldOblique],
};

export function fontKey(f: FontSpec): string {
  const fam = /mono|courier|consol/i.test(f.family) ? "mono" : /serif|times|georgia|garamond|roman/i.test(f.family) && !/sans/i.test(f.family) ? "serif" : "sans";
  return `${fam}:${f.bold ? "b" : ""}${f.italic ? "i" : ""}`;
}

const num = (v: number) => (Math.abs(v) < 1e-9 ? "0" : String(Math.round(v * 1000) / 1000));
const rgb = (c: string) => parseColor(c).slice(0, 3) as [number, number, number];

/** A font chosen for one annotation's text. */
interface UsedFont {
  name: string;
  font: PDFFont;
  /** Replace characters the font can't show with "?". */
  clean(text: string): string;
  width(text: string, size: number): number;
}

type Fontkit = {
  create(buf: Uint8Array, postscriptName?: string): FkFont & { fonts?: FkFont[] };
};
type FkFont = { postscriptName?: string; hasGlyphForCodePoint(cp: number): boolean; characterSet?: number[] };

/** Missing glyphs (code points) of `text` in a font. */
function missing(text: string, has: (cp: number) => boolean): number[] {
  const out: number[] = [];
  for (const ch of text) {
    const cp = ch.codePointAt(0)!;
    if (cp !== 10 && cp !== 13 && cp !== 9 && !has(cp)) out.push(cp);
  }
  return out;
}

class Fonts {
  private std = new Map<string, Promise<UsedFont>>();
  private custom: { req: FontRequest; fk: FkFont; has: (cp: number) => boolean }[] = [];
  private count = 0;
  private fontkit: Fontkit | null = null;
  private failed = new Set<string>();
  readonly warnings: string[] = [];
  private doc: PDFDocument;
  private provider: FontProvider | null;
  constructor(doc: PDFDocument, provider: FontProvider | null) { this.doc = doc; this.provider = provider; }

  standard(f: FontSpec): Promise<UsedFont> {
    const k = fontKey(f);
    let p = this.std.get(k);
    if (!p) {
      p = this.doc.embedFont(FONT_KEYS[k]![1]).then((font) => {
        const cs = new Set(font.getCharacterSet());
        const clean = (t: string) => Array.from(t, (ch) => (cs.has(ch.codePointAt(0)!) ? ch : "?")).join("");
        return { name: FONT_KEYS[k]![0], font, clean, width: (t: string, size: number) => font.widthOfTextAtSize(clean(t), size) };
      });
      this.std.set(k, p);
    }
    return p;
  }

  /** Standard font for WinAnsi text, else an embedded subset of a covering font. */
  async forText(f: FontSpec, text: string): Promise<UsedFont> {
    if (isWinAnsi(text)) return this.standard(f);
    const req: FontRequest = { family: f.family, bold: !!f.bold, italic: !!f.italic, text };
    const same = (r: FontRequest) => r.family === req.family && r.bold === req.bold && r.italic === req.italic;
    const reuse = this.custom.find((c) => same(c.req) && !missing(text, c.has).length)
      ?? this.custom.find((c) => !missing(text, c.has).length);
    if (reuse) return this.embedFor(reuse, text);
    const key = `${req.family}:${req.bold}:${req.italic}:${[...new Set(missing(text, isWinAnsiCp))].sort().join(",")}`;
    if (!this.failed.has(key) && this.provider) {
      try {
        for (const src of await this.provider(req)) {
          const used = await this.tryEmbed(src, text, req);
          if (used) return used;
        }
      } catch { /* provider failed: fall back below */ }
    }
    if (!this.failed.has(key)) {
      this.failed.add(key);
      this.warnings.push(this.provider
        ? "Some text-box characters could not be embedded (no installed font covers them, or font access was not allowed), so they show as “?” in the saved PDF."
        : "Some text-box characters need an embedded font, which is not available here, so they show as “?” in the saved PDF.");
    }
    return this.standard(f);
  }

  /** Parse a candidate and keep it if it covers `text`. */
  private async tryEmbed(src: FontSource, text: string, req: FontRequest): Promise<UsedFont | null> {
    let bytes: Uint8Array = new Uint8Array(await src.load());
    this.fontkit ??= ((await import("@pdf-lib/fontkit")) as unknown as { default: Fontkit }).default;
    let fk: FkFont;
    try {
      if (isCollection(bytes)) {
        const i = Math.max(0, ttcNames(bytes).indexOf(src.postscriptName ?? ""));
        bytes = ttcFace(bytes, i);
      }
      fk = this.fontkit.create(bytes);
    } catch { return null; }
    const has = (cp: number) => fk.hasGlyphForCodePoint(cp);
    // must cover everything a standard font could show neither (rare symbols are let go)
    if (missing(text, has).filter((cp) => !isWinAnsiCp(cp)).length) return null;
    const entry = { req, fk, has };
    this.custom.push(entry);
    return this.embedFor(entry, text);
  }

  /** Embed a font with just the glyphs of `text` (outlines copied into a small TrueType font). */
  private async embedFor(c: { req: FontRequest; fk: FkFont; has: (cp: number) => boolean }, text: string): Promise<UsedFont> {
    const ttf = buildSubsetTtf(c.fk as never, text, { bold: c.req.bold, italic: c.req.italic });
    this.doc.registerFontkit(this.fontkit as never);
    const font = await this.doc.embedFont(ttf, { subset: false });
    const has = c.has;
    const clean = (t: string) => Array.from(t, (ch) => (/[\n\r\t]/.test(ch) || has(ch.codePointAt(0)!) ? ch : "?")).join("");
    return { name: `VF${this.count++}`, font, clean, width: (t, size) => font.widthOfTextAtSize(clean(t), size) };
  }
}

const isWinAnsiCp = (cp: number) => isWinAnsi(String.fromCodePoint(cp));

/** Collects the resources (fonts, graphics states) a content stream uses. */
class Res {
  fonts = new Map<string, PDFRef>();
  gs = new Map<string, { name: string; CA: number; BM?: string }>();
  font(name: string, ref: PDFRef) { this.fonts.set(name, ref); return name; }
  state(alpha: number, blend?: string): string | null {
    if (alpha >= 0.999 && !blend) return null;
    const key = `${num(alpha)}:${blend ?? ""}`;
    let g = this.gs.get(key);
    if (!g) { g = { name: `GS${this.gs.size}`, CA: alpha, BM: blend }; this.gs.set(key, g); }
    return g.name;
  }
  dict(doc: PDFDocument): PDFDict {
    const ctx = doc.context;
    const d = ctx.obj({});
    if (this.fonts.size) {
      const f = ctx.obj({});
      for (const [n, r] of this.fonts) f.set(PDFName.of(n), r);
      d.set(PDFName.of("Font"), f);
    }
    if (this.gs.size) {
      const g = ctx.obj({});
      for (const s of this.gs.values()) {
        g.set(PDFName.of(s.name), ctx.obj({ Type: "ExtGState", CA: s.CA, ca: s.CA, ...(s.BM ? { BM: s.BM } : {}) }));
      }
      d.set(PDFName.of("ExtGState"), g);
    }
    return d;
  }
}

type Map2 = (x: number, y: number) => [number, number];

function pathOps(cmds: Cmd[], m: Map2): string {
  const out: string[] = [];
  for (const c of cmds) {
    if (c.o === "Z") { out.push("h"); continue; }
    if (c.o === "C") {
      const [a, b] = m(c.x1, c.y1), [d, e] = m(c.x2, c.y2), [x, y] = m(c.x, c.y);
      out.push(`${num(a)} ${num(b)} ${num(d)} ${num(e)} ${num(x)} ${num(y)} c`);
      continue;
    }
    const [x, y] = m(c.x, c.y);
    out.push(`${num(x)} ${num(y)} ${c.o === "M" ? "m" : "l"}`);
  }
  return out.join("\n");
}

/** Content-stream operators for prims (in user space via `m`). */
function primOps(prims: Prim[], opacity: number, m: Map2, res: Res, font?: UsedFont): string {
  const out: string[] = [];
  for (const p of prims) {
    out.push("q");
    if (p.t === "path") {
      if (!p.cmds.length) { out.pop(); continue; }
      const gs = res.state(opacity * (p.alpha ?? 1), p.blend === "multiply" ? "Multiply" : undefined);
      if (gs) out.push(`/${gs} gs`);
      const fill = !!p.fill, stroke = !!p.stroke && p.width > 0;
      if (fill) out.push(`${rgb(p.fill!).map(num).join(" ")} rg`);
      if (stroke) out.push(`${rgb(p.stroke!).map(num).join(" ")} RG`, `${num(p.width)} w`, `${p.cap === "butt" ? 0 : 1} J`, "1 j");
      out.push(pathOps(p.cmds, m), fill && stroke ? "B" : fill ? "f" : stroke ? "S" : "n");
    } else {
      const gs = res.state(opacity);
      if (gs) out.push(`/${gs} gs`);
      if (!font) { out.pop(); continue; }
      res.font(font.name, font.font.ref);
      out.push("BT", `/${font.name} ${num(p.font.size)} Tf`, `${rgb(p.color).map(num).join(" ")} rg`);
      for (const l of p.lines) {
        const [x, y] = m(l.x, l.y);
        out.push(`1 0 0 1 ${num(x)} ${num(y)} Tm`, `${font.font.encodeText(font.clean(l.text)).toString()} Tj`);
      }
      out.push("ET");
    }
    out.push("Q");
  }
  return out.join("\n");
}

/** User-space bounding box of prims ([x0, y0, x1, y1]), stroke widths included. */
function primsRect(prims: Prim[], a: Annotation, m: Map2): [number, number, number, number] {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const add = (x: number, y: number, pad: number) => {
    const [X, Y] = m(x, y);
    x0 = Math.min(x0, X - pad); x1 = Math.max(x1, X + pad); y0 = Math.min(y0, Y - pad); y1 = Math.max(y1, Y + pad);
  };
  for (const p of prims) {
    if (p.t !== "path") continue;
    const pad = p.stroke ? p.width / 2 : 0;
    for (const c of p.cmds) {
      if (c.o === "Z") continue;
      if (c.o === "C") { add(c.x1, c.y1, pad); add(c.x2, c.y2, pad); }
      add(c.x, c.y, pad);
    }
  }
  const b = bbox(a);
  const pad = BOXES.has(a.type) ? a.style.width / 2 : 0;
  add(b.x, b.y, pad);
  add(b.x + b.w, b.y + b.h, pad);
  return [x0, y0, x1, y1];
}

const SUBTYPE: Record<string, string> = {
  rect: "Square", rrect: "Square", oval: "Circle", bubble: "Polygon", star: "Polygon", line: "Line", arrow: "Line",
  ink: "Ink", text: "FreeText", note: "Text", highlight: "Highlight", underline: "Underline", strike: "StrikeOut",
};
const INTENT: Record<string, string> = { rrect: "ViewRRect", bubble: "ViewBubble", star: "ViewStar", arrow: "LineArrow" };

function annotationDict(doc: PDFDocument, page: PDFPage, a: Annotation, m: Map2, rect: [number, number, number, number],
  ap: PDFRef, font?: UsedFont): PDFDict {
  const ctx = doc.context;
  const st = a.style;
  const d = ctx.obj({
    Type: "Annot", Subtype: SUBTYPE[a.type]!, Rect: rect.map((v) => Math.round(v * 1000) / 1000),
    F: a.type === "note" ? 4 | 8 | 16 : 4, C: rgb(st.stroke),
  });
  type Lit = string | number | boolean | PDFObject | Lit[] | { [k: string]: Lit };
  const obj = ctx.obj.bind(ctx) as (v: Lit) => PDFObject;
  const set = (k: string, v: Lit) => d.set(PDFName.of(k), v instanceof PDFObject ? v : obj(v));
  set("P", page.ref);
  set("NM", PDFHexString.fromText(a.id));
  set("M", PDFString.fromDate(new Date(a.modified || Date.now())));
  set("CreationDate", PDFString.fromDate(new Date(a.created || Date.now())));
  if (a.author) set("T", PDFHexString.fromText(a.author));
  if ((st.opacity ?? 1) < 1) set("CA", st.opacity);
  if (INTENT[a.type]) set("IT", INTENT[a.type]!);
  const contents = a.type === "text" || a.type === "note" ? a.text : a.comment ?? ("text" in a ? a.text : undefined);
  if (contents) set("Contents", PDFHexString.fromText(contents));
  const userBox = () => { const b = bbox(a); const [X0, Y0] = m(b.x, b.y + b.h); const [X1, Y1] = m(b.x + b.w, b.y); return [X0, Y0, X1, Y1] as const; };
  const rd = () => { const [X0, Y0, X1, Y1] = userBox(); return [X0 - rect[0], rect[3] - Y1, rect[2] - X1, Y0 - rect[1]].map((v) => Math.max(0, Math.round(v * 1000) / 1000)); };
  const bs = () => set("BS", { Type: "Border", W: st.width, S: "S" });

  switch (a.type) {
    case "rect": case "rrect": case "oval":
      bs(); set("RD", rd());
      if (st.fill) set("IC", rgb(st.fill));
      break;
    case "bubble": case "star": {
      bs(); if (st.fill) set("IC", rgb(st.fill));
      set("Vertices", flattenPath(shapePath(a.type, a), 4).flatMap(([x, y]) => m(x, y)).map((v) => Math.round(v * 1000) / 1000));
      break;
    }
    case "line": case "arrow": {
      bs();
      set("L", [...m(a.x1, a.y1), ...m(a.x2, a.y2)]);
      set("LE", ["None", a.type === "arrow" ? "ClosedArrow" : "None"]);
      if (a.type === "arrow") set("IC", rgb(st.stroke));
      break;
    }
    case "ink":
      bs();
      set("InkList", a.strokes.map((s) => triples(s).flatMap(([x, y]) => m(x, y)).map((v) => Math.round(v * 1000) / 1000)));
      break;
    case "text": {
      const f = fontOf(a);
      const name = font?.name ?? "Helv";
      const c = rgb(st.stroke).map(num).join(" ");
      set("DA", PDFString.of(`/${name} ${num(f.size)} Tf ${c} rg`));
      set("DS", PDFString.of(`font: ${f.italic ? "italic " : ""}${f.bold ? "bold " : ""}${num(f.size)}pt ${f.family.replace(/[;:]/g, "")}; text-align:${f.align}; color:${st.stroke}`));
      set("Q", f.align === "center" ? 1 : f.align === "right" ? 2 : 0);
      set("IT", "FreeText");
      set("BS", { Type: "Border", W: 0, S: "S" });
      if (st.fill) set("C", rgb(st.fill)); else d.delete(PDFName.of("C"));
      set("RD", rd());
      if (a.comment) set("ViewComment", PDFHexString.fromText(a.comment));
      break;
    }
    case "note":
      set("Name", "Comment");
      set("Open", false);
      break;
    case "highlight": case "underline": case "strike": {
      const q: number[] = [];
      for (let i = 0; i + 7 < a.quads.length; i += 8) {
        const [tlx, tly, trx, try_, brx, bry, blx, bly] = a.quads.slice(i, i + 8) as [number, number, number, number, number, number, number, number];
        for (const [x, y] of [[tlx, tly], [trx, try_], [blx, bly], [brx, bry]] as const) q.push(...m(x, y));
      }
      set("QuadPoints", q.map((v) => Math.round(v * 1000) / 1000));
      if (a.text) set("ViewText", PDFHexString.fromText(a.text));
      break;
    }
    default:
      break;
  }
  set("AP", ctx.obj({ N: ap }));
  return d;
}

export interface ExportOptions {
  flatten: boolean;
  /**
   * Where to find fonts for text the standard fonts can't show (CJK…). Default:
   * installed fonts via the Local Font Access API; null = never embed.
   */
  fonts?: FontProvider | null;
  /** Called with user-facing messages (e.g. characters that could not be embedded); show as a toast. */
  onWarning?: (message: string) => void;
}

/** writePdfAnnotations returning its warnings. */
export async function exportPdfAnnotations(doc: PDFDocument, annotations: Annotation[],
  pageIndexOf: (surface: string) => number | null, opts: ExportOptions): Promise<{ warnings: string[] }> {
  const fonts = new Fonts(doc, opts.fonts === undefined ? localFontProvider : opts.fonts);
  const fontOfAnn = new Map<string, UsedFont>();
  for (const a of annotations) {
    if (a.type === "text" && pageIndexOf(a.surface) !== null) fontOfAnn.set(a.id, await fonts.forText(fontOf(a), a.text));
  }
  const byPage = new Map<number, Annotation[]>();
  for (const a of annotations) {
    const i = pageIndexOf(a.surface);
    if (i === null || i < 0 || i >= doc.getPageCount()) continue;
    let l = byPage.get(i);
    if (!l) byPage.set(i, (l = []));
    l.push(a);
  }
  const ctx = doc.context;
  for (const [index, anns] of byPage) {
    const page = doc.getPage(index);
    const crop = page.getCropBox();
    const m: Map2 = (x, y) => [crop.x + x, crop.y + crop.height - y];
    const flatRes = new Res();
    const flatOps: string[] = [];
    for (const a of anns) {
      const font = fontOfAnn.get(a.id);
      const measure = (t: string, f: FontSpec) => (font ? font.width(t, f.size) : t.length * f.size * 0.5);
      const prims = drawAnnotation(a, measure);
      const opacity = a.style.opacity ?? 1;
      if (a.type === "signature" || (opts.flatten && FLATTENABLE.has(a.type))) {
        flatOps.push(primOps(prims, opacity, m, flatRes, font));
        continue;
      }
      const rect = a.type === "note"
        ? (() => { const [x0, y1] = m(a.x, a.y); const [x1, y0] = m(a.x + NOTE_SIZE, a.y + NOTE_SIZE); return [x0, y0, x1, y1] as [number, number, number, number]; })()
        : primsRect(prims, a, m);
      const res = new Res();
      const ops = primOps(prims, opacity, m, res, font);
      const ap = ctx.register(ctx.flateStream(ops, {
        Type: "XObject", Subtype: "Form", FormType: 1, BBox: rect, Resources: res.dict(doc),
      }));
      const dict = annotationDict(doc, page, a, m, rect, ap, font);
      page.node.addAnnot(ctx.register(dict));
    }
    if (flatOps.length) {
      const { x, y, width, height } = page.getMediaBox();
      const xo = ctx.register(ctx.flateStream(flatOps.join("\n"), {
        Type: "XObject", Subtype: "Form", FormType: 1, BBox: [x, y, x + width, y + height], Resources: flatRes.dict(doc),
      }));
      const name = page.node.newXObject("ViewMarkup", xo);
      page.pushOperators(pushGraphicsState(), drawObject(name), popGraphicsState());
    }
  }
  for (const w of fonts.warnings) opts.onWarning?.(w);
  return { warnings: fonts.warnings };
}

/** The contract entry point (api.ts WritePdfAnnotations); also accepts ExportOptions. */
export const writePdfAnnotations: WritePdfAnnotations & ((doc: PDFDocument, annotations: Annotation[],
  pageIndexOf: (surface: string) => number | null, opts: ExportOptions) => Promise<void>) = async (doc, annotations, pageIndexOf, opts: ExportOptions) => {
  await exportPdfAnnotations(doc, annotations, pageIndexOf, opts);
};

/**
 * Remove annotations from a loaded document by pdf.js id ("12R" / "12R3"), as
 * returned in ImportResult.sourceIds: call before writePdfAnnotations when the
 * imported annotations are written back, so they are not duplicated.
 */
export function removePdfAnnotations(doc: PDFDocument, sourceIds: Iterable<string>): void {
  const want = new Set([...sourceIds]);
  for (const page of doc.getPages()) {
    const annots = page.node.Annots();
    if (!annots) continue;
    for (let i = annots.size() - 1; i >= 0; i--) {
      const r = annots.get(i);
      if (r instanceof PDFRef && (want.has(`${r.objectNumber}R`) || want.has(`${r.objectNumber}R${r.generationNumber}`))) {
        annots.remove(i);
        const popup = (doc.context.lookup(r) as PDFDict | undefined)?.get(PDFName.of("Popup"));
        if (popup instanceof PDFRef) {
          for (let j = annots.size() - 1; j >= 0; j--) if (annots.get(j) === popup) { annots.remove(j); if (j < i) i--; }
        }
      }
    }
  }
}

export default writePdfAnnotations;
