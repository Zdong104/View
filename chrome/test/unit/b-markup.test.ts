// Stream B: markup geometry, and pdf-export round-tripped through pdf-lib and pdf.js.
import assert from "node:assert/strict";
import { test } from "node:test";
import { decodePDFRawStream, PDFArray, PDFDict, PDFDocument, PDFName, PDFRawStream, PDFRef, PDFStream } from "@cantoo/pdf-lib";
import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs";
import type { Annotation } from "../../src/markup/api.ts";
import { charsOf, recognise, selectText, textHeight, wrapLines } from "../../src/markup/geom.ts";
import { removePdfAnnotations, writePdfAnnotations } from "../../src/markup/pdf-export.ts";
import { importPdfAnnotations, type PdfJsDocLike } from "../../src/markup/pdf-import.ts";

const st = (o: Partial<Annotation["style"]> = {}) => ({ stroke: "#d62828", fill: null, width: 2, opacity: 1, ...o });
const base = (id: string) => ({ id, surface: "p0", created: 1_700_000_000_000, modified: 1_700_000_000_000 });
const sigStrokes = [[0.05, 0.6, 0.5, 0.2, 0.2, 0.7, 0.4, 0.8, 0.4, 0.6, 0.3, 0.6, 0.95, 0.5, 0.5]];

function allTypes(): Annotation[] {
  return [
    { ...base("rect"), type: "rect", x: 40, y: 40, w: 100, h: 60, style: st({ fill: "#3584e4", opacity: 0.5 }) },
    { ...base("rrect"), type: "rrect", x: 160, y: 40, w: 100, h: 60, style: st() },
    { ...base("oval"), type: "oval", x: 280, y: 40, w: 100, h: 60, style: st({ fill: "#33d17a" }) },
    { ...base("bubble"), type: "bubble", x: 400, y: 40, w: 100, h: 60, style: st() },
    { ...base("star"), type: "star", x: 40, y: 130, w: 80, h: 80, style: st() },
    { ...base("line"), type: "line", x1: 150, y1: 140, x2: 260, y2: 200, style: st({ width: 3 }) },
    { ...base("arrow"), type: "arrow", x1: 280, y1: 200, x2: 380, y2: 140, style: st({ width: 3 }), comment: "look here" },
    { ...base("ink"), type: "ink", strokes: [[400, 140, 0.5, 420, 160, 0.5, 450, 150, 0.5, 480, 200, 0.5]], style: st() },
    { ...base("text"), type: "text", x: 40, y: 240, w: 200, h: 40, text: "Hello PDF world, wrapped text box", style: st({ stroke: "#000000", fill: "#fff3a0", width: 0, font: { family: "serif", size: 14, bold: true, align: "center" } }) },
    { ...base("note"), type: "note", x: 300, y: 250, text: "A note", style: st({ stroke: "#ffd940", width: 0 }) },
    { ...base("hl"), type: "highlight", quads: [40, 320, 200, 320, 200, 336, 40, 336], text: "highlighted", style: st({ stroke: "#ffdb1a", width: 0 }) },
    { ...base("ul"), type: "underline", quads: [40, 350, 200, 350, 200, 366, 40, 366], style: st({ width: 0 }) },
    { ...base("so"), type: "strike", quads: [40, 380, 200, 380, 200, 396, 40, 396], style: st({ width: 0 }) },
    { ...base("sig"), type: "signature", x: 300, y: 320, w: 160, h: 50, strokes: sigStrokes, style: st({ stroke: "#1c3f94", width: 2 }) },
  ];
}

const SUBTYPES: Record<string, string> = {
  rect: "Square", rrect: "Square", oval: "Circle", bubble: "Polygon", star: "Polygon", line: "Line", arrow: "Line",
  ink: "Ink", text: "FreeText", note: "Text", hl: "Highlight", ul: "Underline", so: "StrikeOut",
};

async function exportDoc(flatten: boolean): Promise<Uint8Array> {
  const doc = await PDFDocument.create();
  doc.addPage([595, 842]);
  await writePdfAnnotations(doc, allTypes(), (s) => (s === "p0" ? 0 : null), { flatten });
  return doc.save();
}

function annots(doc: PDFDocument): PDFDict[] {
  const arr = doc.getPage(0).node.Annots();
  return arr ? arr.asArray().map((r) => doc.context.lookup(r) as PDFDict) : [];
}

/** Decoded content of the page's flattened markup XObject. */
function markupXObject(doc: PDFDocument): string {
  const xo = doc.getPage(0).node.Resources()!.lookup(PDFName.of("XObject"), PDFDict);
  const key = xo.keys().find((k) => k.asString().startsWith("/ViewMarkup"));
  assert.ok(key, "page draws a ViewMarkup form XObject");
  const s = xo.lookup(key, PDFStream) as PDFRawStream;
  return new TextDecoder().decode(decodePDFRawStream(s).decode());
}

function pdfjsDoc(bytes: Uint8Array) {
  return pdfjs.getDocument({ data: bytes.slice(), useSystemFonts: false,
    standardFontDataUrl: new URL("../../node_modules/pdfjs-dist/standard_fonts/", `file://${process.cwd()}/test/unit/`).pathname }).promise;
}

test("recognise: closed strokes become rect / oval, straight ones lines", () => {
  const rect: number[] = [];
  for (let i = 0; i <= 40; i++) rect.push(i * 5, 0, 0.5);
  for (let i = 0; i <= 20; i++) rect.push(200, i * 5, 0.5);
  for (let i = 40; i >= 0; i--) rect.push(i * 5, 100, 0.5);
  for (let i = 20; i >= 1; i--) rect.push(0, i * 5, 0.5);
  assert.equal(recognise(rect)?.type, "rect");
  const oval: number[] = [];
  for (let i = 0; i <= 64; i++) oval.push(100 + 90 * Math.cos(i / 64 * 2 * Math.PI), 60 + 50 * Math.sin(i / 64 * 2 * Math.PI), 0.5);
  assert.equal(recognise(oval)?.type, "oval");
  const line: number[] = [];
  for (let i = 0; i <= 20; i++) line.push(i * 10, i * 5 + (i % 2) * 0.5, 0.5);
  assert.equal(recognise(line)?.type, "line");
  const zig: number[] = [];
  for (let i = 0; i <= 20; i++) zig.push(i * 10, (i % 2) * 40, 0.5);
  assert.equal(recognise(zig), null);
});

test("text wrapping and auto height", () => {
  const m = (t: string) => t.length * 7;
  const f = { family: "sans-serif", size: 14 };
  assert.deepEqual(wrapLines("aaa bbb ccc", 40, f, m), ["aaa", "bbb", "ccc"]);
  assert.deepEqual(wrapLines("aaa bbb ccc", 50, f, m), ["aaa bbb", "ccc"]);
  assert.deepEqual(wrapLines("aaaaaaaaaa", 35, f, m), ["aaaaa", "aaaaa"]);
  assert.deepEqual(wrapLines("a b\nc", 200, f, m), ["a b", "c"]);
  const h = textHeight({ text: "aaa bbb ccc", w: 44, style: st({ font: { family: "sans-serif", size: 10 } }) }, m);
  assert.ok(Math.abs(h - (3 * 12 + 4)) < 1e-6, String(h));
});

test("text selection snaps to characters and merges lines", () => {
  const run = (text: string, x: number, y: number) => ({
    text, quads: [...text].flatMap((_, i) => [x + i * 10, y, x + i * 10 + 10, y, x + i * 10 + 10, y + 12, x + i * 10, y + 12]),
  });
  const chars = charsOf([run("Hello world", 10, 10), run("second line", 10, 30)]);
  const one = selectText(chars, 12, 15, 58, 15);
  assert.equal(one.text, "Hello");
  assert.deepEqual(one.quads, [10, 10, 60, 10, 60, 22, 10, 22]);
  const two = selectText(chars, 75, 15, 45, 35);
  assert.equal(two.text, "world\nseco");
  assert.equal(two.quads.length, 16);
});

test("pdf-export writes every annotation kind with an appearance stream", async () => {
  const bytes = await exportDoc(false);
  const doc = await PDFDocument.load(bytes);
  const list = annots(doc);
  const got = list.map((d) => d.lookup(PDFName.of("Subtype"), PDFName).asString().slice(1)).sort();
  assert.deepEqual(got, Object.values(SUBTYPES).sort());
  for (const d of list) {
    const ap = d.lookup(PDFName.of("AP"), PDFDict);
    assert.ok(ap.lookup(PDFName.of("N")) instanceof PDFStream, `${d.get(PDFName.of("Subtype"))} has /AP /N`);
    assert.ok(d.get(PDFName.of("Rect")) instanceof PDFArray);
  }
  // the signature is never an annotation: vector paths in the page content
  const content = markupXObject(doc);
  assert.match(content, / c\n/);
  assert.match(content, /\nf\n/);
  assert.doesNotMatch(content, /\/Im|BI\b/);
  const xo = doc.getPage(0).node.Resources()!.lookup(PDFName.of("XObject"), PDFDict);
  for (const k of xo.keys()) assert.notEqual((xo.lookup(k, PDFStream) as PDFRawStream).dict.get(PDFName.of("Subtype"))?.toString(), "/Image");
});

test("pdf.js sees every annotation with an appearance, and pdf-import round-trips them", async () => {
  const bytes = await exportDoc(false);
  const pdf = await pdfjsDoc(bytes);
  const page = await pdf.getPage(1);
  const data = (await page.getAnnotations()) as { subtype: string; hasAppearance: boolean }[];
  assert.deepEqual(data.map((d) => d.subtype).sort(), Object.values(SUBTYPES).sort());
  for (const d of data) assert.ok(d.hasAppearance, `${d.subtype} hasAppearance`);

  const raw = await PDFDocument.load(bytes);
  for (const [withRaw, opts] of [[false, { raw: null }], [true, { raw }], [true, {}]] as const) {
    const { annotations, sourceIds } = await importPdfAnnotations(pdf as unknown as PdfJsDocLike, opts);
    assert.equal(sourceIds.length, 13);
    const byType = new Map(annotations.map((a) => [a.type, a]));
    const want = allTypes().filter((a) => a.type !== "signature");
    for (const w of want) {
      const a = byType.get(w.type);
      assert.ok(a, `imported ${w.type} (raw=${withRaw})`);
      if (withRaw) assert.equal(a.id, w.id);
      const close = (x: number, y: number, what: string) => assert.ok(Math.abs(x - y) < 0.6, `${w.type}.${what}: ${x} vs ${y}`);
      if ("w" in w && "w" in a) { close(a.x, w.x, "x"); close(a.y, w.y, "y"); close(a.w, w.w, "w"); close(a.h, w.h, "h"); }
      if ((w.type === "line" || w.type === "arrow") && withRaw) { // pdf.js alone normalizes /L (direction lost)
        const b = a as typeof w;
        close(b.x1, w.x1, "x1"); close(b.y1, w.y1, "y1"); close(b.x2, w.x2, "x2"); close(b.y2, w.y2, "y2");
        close(b.style.width, w.style.width, "width");
      }
      if (w.type === "note") { const b = a as typeof w; close(b.x, w.x, "x"); close(b.y, w.y, "y"); assert.equal(b.text, w.text); }
      if (w.type === "ink") assert.deepEqual((a as typeof w).strokes[0]!.map((v) => Math.round(v)), w.strokes[0]!.map((v) => Math.round(v)));
      if ("quads" in w) assert.deepEqual((a as typeof w).quads.map((v) => Math.round(v)), w.quads);
      if (w.type === "text") {
        const b = a as typeof w;
        assert.equal(b.text, w.text);
        assert.equal(b.style.font?.size, 14);
        assert.equal(b.style.font?.family, "serif");
        assert.equal(b.style.font?.bold, true);
        assert.equal(b.style.fill, "#fff3a0");
        if (withRaw) assert.equal(b.style.font?.align, "center");
      }
      assert.equal(a.style.stroke.toLowerCase(), w.style.stroke.toLowerCase(), `${w.type} colour`);
      if (w.comment) assert.equal(a.comment, w.comment);
    }
    if (withRaw) {
      assert.equal(byType.get("rect")!.style.fill, "#3584e4");
      assert.equal(byType.get("rect")!.style.opacity, 0.5);
      assert.equal(byType.get("oval")!.style.fill, "#33d17a");
      assert.equal((byType.get("highlight") as { text?: string }).text, "highlighted");
    }
  }
  await pdf.loadingTask.destroy();
});

test("flatten draws shapes, ink and text into the page; notes and text markup stay annotations", async () => {
  const doc = await PDFDocument.load(await exportDoc(true));
  const got = annots(doc).map((d) => d.lookup(PDFName.of("Subtype"), PDFName).asString().slice(1)).sort();
  assert.deepEqual(got, ["Highlight", "StrikeOut", "Text", "Underline"]);
  const content = markupXObject(doc);
  assert.match(content, /BT\n\/TiBo 14 Tf/);
  assert.match(content, / re\n|\n[\d. ]+ l\n/);
});

test("removePdfAnnotations drops imported annotations by pdf.js id", async () => {
  const bytes = await exportDoc(false);
  const pdf = await pdfjsDoc(bytes);
  const { sourceIds } = await importPdfAnnotations(pdf as unknown as PdfJsDocLike);
  await pdf.loadingTask.destroy();
  const doc = await PDFDocument.load(bytes);
  removePdfAnnotations(doc, sourceIds.slice(0, 5));
  assert.equal(annots(doc).length, 8);
  assert.ok(doc.getPage(0).node.Annots()!.asArray().every((r) => r instanceof PDFRef));
});
