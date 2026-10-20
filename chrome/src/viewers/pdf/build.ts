// Save pipeline (port of preview/pdf_ops.build_writer/_finish_write) on
// @cantoo/pdf-lib. Builds a new PDF from the page model:
//   - the base source is loaded and restructured in place, so its catalog
//     (outline, AcroForm, metadata, names, viewer prefs) survives;
//   - pages from other sources are copied in (copyPages);
//   - the page tree is rebuilt flat in the model's order, user rotation is
//     added to /Rotate;
//   - AcroForm fields are pruned to the kept pages and fields of copied pages
//     are added (form values come in through the bytes: the viewer passes
//     pdf.js saveDocument() output for sources whose forms were edited);
//   - unreachable objects are dropped, so deleted pages are really gone from
//     the file (pdf-lib would otherwise write them back);
//   - optional: markup (markup/pdf-export.ts), invisible OCR text, image
//     re-encoding (reduce.ts), AES-256 encryption.
import {
  PDFArray, PDFDict, PDFDocument, PDFName, PDFNumber, PDFPageLeaf, PDFPageTree, PDFRef, PDFStream, PDFBool,
  PDFHexString, PDFString, TextRenderingMode, beginText, endText, popGraphicsState, pushGraphicsState,
  setFontAndSize, setTextMatrix, setTextRenderingMode, showText, PDFOperator,
  type PDFObject, type PDFPage,
} from "@cantoo/pdf-lib";
import type { SurfaceLine } from "./search.ts";

export interface BuildSource { bytes: Uint8Array; password: string | null }
export interface BuildPage { src: number; index: number; rotation: number }

export interface EncryptOptions {
  userPassword: string;
  ownerPassword?: string;
  allowPrint: boolean;
  allowCopy: boolean;
  allowModify: boolean;
  allowAnnotate: boolean;
}

export interface BuildOptions {
  /** Source whose catalog is kept. */
  base: number;
  /** Re-encrypt with the base's password (normal Save of a protected file). */
  keepPassword?: boolean;
  basePassword?: string | null;
  encrypt?: EncryptOptions;
  /** Write markup; `pageIndexOf(i)` is identity here, the caller maps surfaces. */
  annotate?: (doc: PDFDocument) => Promise<void>;
  /** Output page index -> OCR lines (surface units) written as invisible text. */
  textLayers?: Map<number, SurfaceLine[]>;
  /** Called with each loaded source document before pages are taken from it. */
  prepare?: (src: number, doc: PDFDocument) => void;
  /** Hook to shrink images before writing (reduce.ts). */
  transform?: (doc: PDFDocument) => Promise<void>;
  progress?: (msg: string) => void;
}

export async function loadPdfLib(s: BuildSource): Promise<PDFDocument> {
  const opts = { updateMetadata: false, throwOnInvalidObject: false };
  try {
    return await PDFDocument.load(s.bytes, { ...opts, password: s.password ?? undefined });
  } catch (e) {
    // Owner-password-only files open in pdf.js without a prompt: try the empty user password.
    if (s.password === null && /encrypt/i.test(String((e as Error)?.message ?? e))) {
      return PDFDocument.load(s.bytes, { ...opts, password: "" });
    }
    throw e;
  }
}

const N = (n: string) => PDFName.of(n);
const tagOf = (r: PDFRef) => r.tag;

export async function buildPdf(
  getSource: (src: number) => Promise<BuildSource>,
  pages: BuildPage[],
  opts: BuildOptions,
): Promise<Uint8Array> {
  if (!pages.length) throw new Error("A PDF needs at least one page");
  const docs = new Map<number, PDFDocument>();
  const load = async (src: number) => {
    let d = docs.get(src);
    if (!d) { d = await loadPdfLib(await getSource(src)); opts.prepare?.(src, d); docs.set(src, d); }
    return d;
  };
  const doc = await load(opts.base);
  const ctx = doc.context;
  const basePages = doc.getPages();
  for (const p of basePages) normalizeInherited(p.node);

  // Pick or copy every output page.
  const out: (PDFPage | null)[] = new Array(pages.length).fill(null);
  const usedBase = new Set<number>();
  const copied = new Set<PDFPage>();
  const wanted = new Map<number, number[]>(); // src -> output positions
  pages.forEach((p, i) => {
    if (p.src === opts.base && !usedBase.has(p.index) && basePages[p.index]) {
      usedBase.add(p.index);
      out[i] = basePages[p.index]!;
    } else {
      const l = wanted.get(p.src) ?? [];
      l.push(i);
      wanted.set(p.src, l);
    }
  });
  for (const [src, positions] of wanted) {
    opts.progress?.("Copying pages…");
    const from = await load(src);
    const got = await doc.copyPages(from, positions.map((i) => pages[i]!.index));
    got.forEach((pg, k) => { out[positions[k]!] = pg; copied.add(pg); });
  }
  const final = out as PDFPage[];

  // Flat page tree in model order; rotation; annotation /P back-links.
  const pagesDict = PDFPageTree.withContext(ctx);
  pagesDict.set(N("Kids"), ctx.obj(final.map((p) => p.ref)));
  pagesDict.set(N("Count"), PDFNumber.of(final.length));
  const pagesRef = ctx.register(pagesDict);
  final.forEach((p, i) => {
    p.node.set(N("Parent"), pagesRef);
    const rot = pages[i]!.rotation % 360;
    if (rot) {
      const cur = p.node.lookup(N("Rotate"));
      const deg = cur instanceof PDFNumber ? cur.asNumber() : 0;
      p.node.set(N("Rotate"), PDFNumber.of(((deg + rot) % 360 + 360) % 360));
    }
    const annots = p.node.lookup(N("Annots"));
    if (annots instanceof PDFArray) {
      for (const a of annots.asArray()) {
        const d = a instanceof PDFRef ? ctx.lookup(a) : a;
        if (d instanceof PDFDict && d.has(N("P"))) d.set(N("P"), p.ref);
      }
    }
  });
  doc.catalog.set(N("Pages"), pagesRef);
  const internal = doc as unknown as { pageCache?: { invalidate(): void }; pageMap?: Map<unknown, PDFPage> };
  for (const p of final) internal.pageMap?.set(p.node, p);
  internal.pageCache?.invalidate();

  fixForms(doc, final, copied);
  doc.setModificationDate(new Date());

  if (opts.annotate) { opts.progress?.("Writing markup…"); await opts.annotate(doc); }
  if (opts.textLayers?.size) await writeTextLayers(doc, opts.textLayers);

  // Pages that were left out must not be reachable (their content would stay in the file).
  const dead = new Set(basePages.filter((_, i) => !usedBase.has(i)).map((p) => tagOf(p.ref)));
  collectGarbage(doc, dead);

  if (opts.transform) await opts.transform(doc);

  if (opts.encrypt) {
    const e = opts.encrypt;
    doc.encrypt({
      userPassword: e.userPassword,
      ownerPassword: e.ownerPassword || e.userPassword,
      permissions: {
        printing: e.allowPrint ? "highResolution" : false,
        copying: e.allowCopy,
        contentAccessibility: e.allowCopy,
        modifying: e.allowModify,
        documentAssembly: e.allowModify,
        annotating: e.allowAnnotate,
        fillingForms: e.allowAnnotate,
      },
    });
  } else if (opts.keepPassword && opts.basePassword) {
    doc.encrypt({ userPassword: opts.basePassword, ownerPassword: opts.basePassword });
  }
  opts.progress?.("Writing PDF…");
  return doc.save({ useObjectStreams: true, updateFieldAppearances: false, addDefaultPage: false });
}

function normalizeInherited(leaf: PDFPageLeaf): void {
  for (const k of PDFPageLeaf.InheritableEntries) {
    const key = N(k);
    if (!leaf.get(key)) {
      const v = leaf.getInheritableAttribute(key);
      if (v) leaf.set(key, v);
    }
  }
}

/** Keep only form fields with widgets on the output pages; add fields of copied pages. */
function fixForms(doc: PDFDocument, final: PDFPage[], copied: Set<PDFPage>): void {
  const ctx = doc.context;
  const live = new Set<string>();
  const copiedWidgets: PDFRef[] = [];
  for (const p of final) {
    const annots = p.node.lookup(N("Annots"));
    if (!(annots instanceof PDFArray)) continue;
    for (const a of annots.asArray()) {
      if (!(a instanceof PDFRef)) continue;
      const d = ctx.lookup(a);
      if (!(d instanceof PDFDict) || d.lookup(N("Subtype")) !== N("Widget")) continue;
      live.add(a.tag);
      if (copied.has(p)) copiedWidgets.push(a);
    }
  }
  let acro = doc.catalog.lookup(N("AcroForm"));
  if (!(acro instanceof PDFDict)) {
    if (!copiedWidgets.length) return;
    acro = ctx.obj({ Fields: [], NeedAppearances: true });
    doc.catalog.set(N("AcroForm"), ctx.register(acro as PDFDict));
  }
  const form = acro as PDFDict;
  let fields = form.lookup(N("Fields"));
  if (!(fields instanceof PDFArray)) { fields = ctx.obj([]); form.set(N("Fields"), fields as PDFArray); }
  const arr = fields as PDFArray;
  // Fields of copied pages (top-level ancestors of their widgets).
  const have = new Set(arr.asArray().filter((r): r is PDFRef => r instanceof PDFRef).map((r) => r.tag));
  for (const w of copiedWidgets) {
    let ref = w;
    for (let depth = 0; depth < 32; depth++) {
      const d = ctx.lookup(ref);
      const parent = d instanceof PDFDict ? d.get(N("Parent")) : undefined;
      if (!(parent instanceof PDFRef)) break;
      ref = parent;
    }
    if (!have.has(ref.tag)) { arr.push(ref); have.add(ref.tag); }
  }
  if (copiedWidgets.length) form.set(N("NeedAppearances"), PDFBool.True);
  // Prune: a field stays when it (or a descendant) is a live widget.
  const keep = (ref: PDFObject, depth: number): boolean => {
    if (depth > 32) return false;
    const d = ref instanceof PDFRef ? ctx.lookup(ref) : ref;
    if (!(d instanceof PDFDict)) return false;
    const kids = d.lookup(N("Kids"));
    if (kids instanceof PDFArray) {
      const kept = kids.asArray().filter((k) => keep(k, depth + 1));
      d.set(N("Kids"), ctx.obj(kept));
      if (kept.length) return true;
    }
    return ref instanceof PDFRef && live.has(ref.tag);
  };
  form.set(N("Fields"), ctx.obj(arr.asArray().filter((f) => keep(f, 0))));
}

/** Delete every indirect object not reachable from the trailer (dead pages are cut off). */
export function collectGarbage(doc: PDFDocument, dead: Set<string> = new Set()): void {
  const ctx = doc.context;
  const live = new Set<string>();
  const stack: PDFObject[] = [];
  const t = ctx.trailerInfo;
  for (const v of [t.Root, t.Info, t.Encrypt, t.ID]) if (v) stack.push(v as PDFObject);
  while (stack.length) {
    const o = stack.pop()!;
    if (o instanceof PDFRef) {
      if (live.has(o.tag) || dead.has(o.tag)) continue;
      live.add(o.tag);
      const v = ctx.lookup(o);
      if (v) stack.push(v);
    } else if (o instanceof PDFDict) {
      for (const [, v] of o.entries()) stack.push(v);
    } else if (o instanceof PDFArray) {
      for (const v of o.asArray()) stack.push(v);
    } else if (o instanceof PDFStream) {
      stack.push(o.dict);
    }
  }
  for (const [ref] of ctx.enumerateIndirectObjects()) if (!live.has(ref.tag)) ctx.delete(ref);
}

/**
 * Invisible text (render mode 3) over scanned pages, so they become searchable
 * and selectable. Like Tesseract, the text uses a "GlyphLessFont": a Type0 /
 * Identity-H font with no glyph program, every character 0.5 em wide, and a
 * ToUnicode map. No real font is needed, so every script (CJK included) is kept.
 */
async function writeTextLayers(doc: PDFDocument, layers: Map<number, SurfaceLine[]>): Promise<void> {
  const ctx = doc.context;
  const cids = new Map<string, number>();
  const cidOf = (ch: string) => {
    let c = cids.get(ch);
    if (c === undefined) { c = cids.size + 1; cids.set(ch, c); }
    return c;
  };
  const toUnicodeRef = ctx.nextRef();
  const descriptor = ctx.register(ctx.obj({
    Type: "FontDescriptor", FontName: "GlyphLessFont", Flags: 4, FontBBox: [0, 0, 500, 1000],
    ItalicAngle: 0, Ascent: 1000, Descent: 0, CapHeight: 1000, StemV: 80,
  }));
  const cidFont = ctx.register(ctx.obj({
    Type: "Font", Subtype: "CIDFontType2", BaseFont: "GlyphLessFont",
    CIDSystemInfo: { Registry: PDFString.of("Adobe"), Ordering: PDFString.of("Identity"), Supplement: 0 },
    FontDescriptor: descriptor, DW: 500, CIDToGIDMap: "Identity",
  }));
  const fontRef = ctx.register(ctx.obj({
    Type: "Font", Subtype: "Type0", BaseFont: "GlyphLessFont", Encoding: "Identity-H",
    DescendantFonts: [cidFont], ToUnicode: toUnicodeRef,
  }));
  const all = doc.getPages();
  for (const [i, lines] of layers) {
    const page = all[i];
    if (!page) continue;
    const cb = page.getCropBox();
    const key = page.node.newFontDictionary("OcrF", fontRef);
    const toUser = (u: number, v: number): [number, number] => [cb.x + u, cb.y + cb.height - v];
    const ops: PDFOperator[] = [pushGraphicsState(), beginText(), setTextRenderingMode(TextRenderingMode.Invisible)];
    for (const l of lines) {
      const chars = [...l.text];
      if (!l.text.trim() || l.box.length < 8 || cids.size + chars.length > 0xfff0) continue;
      const [x1, y1, x2, y2, , , x4, y4] = l.box as [number, number, number, number, number, number, number, number];
      const width = Math.hypot(x2 - x1, y2 - y1), height = Math.hypot(x4 - x1, y4 - y1);
      if (width < 1 || height < 1) continue;
      const size = height * 0.85;
      const sx = width / (chars.length * 0.5 * size);
      const [ox, oy] = toUser(x4, y4);
      const [ex, ey] = toUser(x4 + (x2 - x1), y4 + (y2 - y1));
      const ang = Math.atan2(ey - oy, ex - ox);
      const cos = Math.cos(ang), sin = Math.sin(ang);
      const hex = chars.map((ch) => cidOf(ch).toString(16).padStart(4, "0")).join("");
      ops.push(setFontAndSize(key, size), setTextMatrix(cos * sx, sin * sx, -sin, cos, ox, oy + height * 0.15),
        showText(PDFHexString.of(hex)));
    }
    ops.push(endText(), popGraphicsState());
    page.pushOperators(...ops);
  }
  ctx.assign(toUnicodeRef, ctx.flateStream(toUnicodeCMap(cids)));
}

/** ToUnicode CMap for 2-byte CIDs. */
export function toUnicodeCMap(cids: Map<string, number>): string {
  const hex4 = (n: number) => n.toString(16).padStart(4, "0").toUpperCase();
  const utf16 = (ch: string) => [...Array(ch.length).keys()].map((i) => hex4(ch.charCodeAt(i))).join("");
  const entries = [...cids].map(([ch, cid]) => `<${hex4(cid)}> <${utf16(ch)}>`);
  const blocks: string[] = [];
  for (let i = 0; i < entries.length; i += 100) {
    const b = entries.slice(i, i + 100);
    blocks.push(`${b.length} beginbfchar\n${b.join("\n")}\nendbfchar`);
  }
  return ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
    "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
    "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
    "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange",
    ...blocks, "endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"].join("\n");
}
