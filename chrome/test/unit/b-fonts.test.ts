// Stream B: fonts for exported text boxes. WinAnsi text keeps the standard
// fonts; CJK / Cyrillic text embeds a subset of a font from an injected
// FontProvider (machine fonts located with fc-match); without one we fall back
// to "?" and report a warning. Checked through pdf-lib and pdf.js.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { PDFDict, PDFDocument, PDFName, PDFStream } from "@cantoo/pdf-lib";
import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs";
import type { Annotation } from "../../src/markup/api.ts";
import { exportPdfAnnotations } from "../../src/markup/pdf-export.ts";
import { type FontProvider, isWinAnsi, rankFonts, ttcFace, ttcNames } from "../../src/markup/pdf-fonts.ts";
import { buildSubsetTtf } from "../../src/markup/ttf-build.ts";
import fontkit from "@pdf-lib/fontkit";

const STD_FONTS = new URL("../../node_modules/pdfjs-dist/standard_fonts/", `file://${process.cwd()}/test/unit/`).pathname;

/** A system font file for a fontconfig pattern, or null. */
function systemFont(pattern: string): { file: string; ps: string } | null {
  try {
    const [file, ps] = execFileSync("fc-match", ["-f", "%{file}|%{postscriptname}", pattern], { encoding: "utf8" }).split("|");
    return file && ps && readFileSync(file).length ? { file, ps } : null;
  } catch { return null; }
}
const CJK = systemFont(":lang=zh-cn");
const CJK_OK = CJK && /cjk|han|hei|song|wqy|droid|noto/i.test(CJK.file) ? CJK : null;
const LATIN = systemFont("DejaVu Sans");

const providerFor = (f: { file: string; ps: string } | null, calls: string[] = []): FontProvider => async (req) => {
  calls.push(req.text);
  return f ? [{ name: f.ps, postscriptName: f.ps, load: async () => readFileSync(f.file) }] : [];
};

const textBox = (text: string): Annotation => ({
  id: "t1", surface: "p0", created: 0, modified: 0, type: "text", x: 40, y: 40, w: 260, h: 60, text,
  style: { stroke: "#000000", fill: null, width: 0, opacity: 1, font: { family: "sans-serif", size: 16 } },
});

async function exportText(text: string, provider: FontProvider | null, flatten = false) {
  const doc = await PDFDocument.create();
  doc.addPage([400, 300]);
  const warnings: string[] = [];
  await exportPdfAnnotations(doc, [textBox(text)], () => 0, { flatten, fonts: provider, onWarning: (w) => warnings.push(w) });
  return { bytes: await doc.save(), warnings };
}

/** Fonts used by the FreeText appearance: [resource name, BaseFont, has FontFile*]. */
async function apFonts(bytes: Uint8Array) {
  const doc = await PDFDocument.load(bytes);
  const annot = doc.context.lookup(doc.getPage(0).node.Annots()!.get(0)) as PDFDict;
  const ap = annot.lookup(PDFName.of("AP"), PDFDict).lookup(PDFName.of("N"), PDFStream);
  const fonts = ap.dict.lookup(PDFName.of("Resources"), PDFDict).lookup(PDFName.of("Font"), PDFDict);
  return fonts.keys().map((k) => {
    const f = fonts.lookup(k, PDFDict);
    const desc = f.lookup(PDFName.of("DescendantFonts"));
    const d0 = desc ? doc.context.lookup((desc as unknown as { get(i: number): unknown }).get(0) as never) as PDFDict : f;
    const fd = d0.lookup(PDFName.of("FontDescriptor")) as PDFDict | undefined;
    const ff = fd && ["FontFile", "FontFile2", "FontFile3"].map((n) => fd.lookup(PDFName.of(n))).find(Boolean) as PDFStream | undefined;
    const fileKey = fd && ["FontFile", "FontFile2", "FontFile3"].find((n) => fd.lookup(PDFName.of(n)));
    return { name: k.asString(), base: f.lookup(PDFName.of("BaseFont"))!.toString(), embedded: ff?.getContentsSize() ?? 0, fileKey };
  });
}

async function pdfjsText(bytes: Uint8Array) {
  const task = pdfjs.getDocument({ data: bytes.slice(), useSystemFonts: false, standardFontDataUrl: STD_FONTS });
  const pdf = await task.promise;
  const page = await pdf.getPage(1);
  const annots = (await page.getAnnotations()) as { subtype: string; textContent?: string[]; contentsObj?: { str: string } }[];
  const content = await page.getTextContent();
  const pageText = content.items.map((i) => ("str" in i ? i.str : "")).join("");
  await task.destroy();
  return { annots, pageText };
}

test("WinAnsi detection and font ranking", () => {
  assert.ok(isWinAnsi("Café – “quotes” €5\nnext"));
  assert.ok(!isWinAnsi("你好"));
  assert.ok(!isWinAnsi("Привет"));
  const fake = (family: string, style = "Regular") => ({ family, style, fullName: `${family} ${style}`, postscriptName: family.replace(/ /g, ""), blob: async () => new Blob() });
  const all = [fake("Arial"), fake("Noto Sans CJK SC", "Bold"), fake("Noto Sans CJK SC"), fake("Microsoft YaHei"), fake("Georgia")];
  const r = rankFonts(all, { family: "sans-serif", bold: false, italic: false, text: "你好" });
  assert.deepEqual(r.map((f) => f.fullName), ["Noto Sans CJK SC Regular", "Microsoft YaHei Regular", "Arial Regular"]);
  assert.equal(rankFonts(all, { family: "sans-serif", bold: true, italic: false, text: "你好" })[0]!.fullName, "Noto Sans CJK SC Bold");
  assert.equal(rankFonts(all, { family: "Georgia", bold: false, italic: false, text: "Привет" })[0]!.family, "Georgia");
});

test("WinAnsi text uses a standard font: nothing embedded, provider not asked", async () => {
  const calls: string[] = [];
  const { bytes, warnings } = await exportText("Hello, café!", providerFor(LATIN, calls));
  assert.deepEqual(calls, []);
  assert.deepEqual(warnings, []);
  const fonts = await apFonts(bytes);
  assert.deepEqual(fonts.map((f) => [f.name, f.base, f.embedded]), [["/Helv", "/Helvetica", 0]]);
  const { annots } = await pdfjsText(bytes);
  assert.equal(annots[0]!.textContent?.join(""), "Hello, café!");
});

test("CJK text embeds a subset of a covering font (TTC face) and round-trips through pdf.js", { skip: !CJK_OK && "no CJK font on this machine" }, async () => {
  const text = "你好，世界 Hello 中文测试";
  const { bytes, warnings } = await exportText(text, providerFor(CJK_OK));
  assert.deepEqual(warnings, []);
  const [f] = await apFonts(bytes);
  assert.equal(f!.name, "/VF0");
  assert.equal(f!.fileKey, "FontFile2", "glyphs copied into a TrueType font");
  assert.ok(f!.embedded > 0, "font program embedded");
  assert.ok(bytes.length < 40_000, `only the used glyphs, not the whole ${readFileSync(CJK_OK!.file).length} byte font: ${bytes.length}`);
  if (/NotoSansCJK/.test(CJK_OK!.file)) assert.match(f!.base, /NotoSansCJKsc/, "the requested face of the collection");
  const { annots } = await pdfjsText(bytes);
  assert.equal(annots[0]!.subtype, "FreeText");
  assert.equal(annots[0]!.contentsObj?.str, text);
  // pdf.js reads the appearance stream's text back through the embedded font's ToUnicode map
  assert.equal(annots[0]!.textContent?.join("").replace(/\s+/g, " "), text);

  const flat = await exportText(text, providerFor(CJK_OK), true);
  assert.deepEqual(flat.warnings, []);
  const { pageText } = await pdfjsText(flat.bytes);
  assert.equal(pageText.replace(/\s+/g, ""), text.replace(/\s+/g, ""));
});

test("Cyrillic text embeds a single-face TrueType font", { skip: !LATIN && "no DejaVu Sans on this machine" }, async () => {
  const text = "Привет, мир";
  const { bytes, warnings } = await exportText(text, providerFor(LATIN));
  assert.deepEqual(warnings, []);
  const [f] = await apFonts(bytes);
  assert.ok(f!.embedded > 0);
  const { annots } = await pdfjsText(bytes);
  assert.equal(annots[0]!.textContent?.join(""), text);
});

test("no covering font: '?' in the appearance, full text in /Contents, and a warning", async () => {
  const text = "签名 Sign";
  for (const provider of [providerFor(null), null]) {
    const { bytes, warnings } = await exportText(text, provider);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0]!, /“\?”/);
    const fonts = await apFonts(bytes);
    assert.equal(fonts[0]!.base, "/Helvetica");
    const { annots } = await pdfjsText(bytes);
    assert.equal(annots[0]!.contentsObj?.str, text);
    assert.equal(annots[0]!.textContent?.join(""), "?? Sign");
  }
});

test("ttcFace extracts a standalone font from a collection", { skip: !CJK_OK?.file.endsWith(".ttc") && "no TTC font" }, () => {
  const ttc = new Uint8Array(readFileSync(CJK_OK!.file));
  const face = ttcFace(ttc, 0);
  const tag = String.fromCharCode(...face.subarray(0, 4));
  assert.ok(tag === "OTTO" || tag === "\0\x01\0\0", JSON.stringify(tag));
  assert.ok(face.length < ttc.length);
});

/** A fontkit-like source font with three glyphs drawn from lines, quadratics and cubics. */
function fakeSource() {
  const box = [{ command: "moveTo", args: [100, 0] }, { command: "lineTo", args: [900, 0] }, { command: "lineTo", args: [900, 800] },
    { command: "lineTo", args: [100, 800] }, { command: "closePath", args: [] }];
  const ring = [{ command: "moveTo", args: [500, 0] }, { command: "bezierCurveTo", args: [800, 0, 900, 300, 900, 400] },
    { command: "bezierCurveTo", args: [900, 700, 700, 800, 500, 800] }, { command: "quadraticCurveTo", args: [100, 800, 100, 400] },
    { command: "lineTo", args: [100, 300] }, { command: "closePath", args: [] }];
  const glyphs: Record<number, { id: number; advanceWidth: number; path: { commands: unknown[] } }> = {
    0x4e2d: { id: 5, advanceWidth: 1000, path: { commands: box } }, // 中
    0x6587: { id: 9, advanceWidth: 1000, path: { commands: ring } }, // 文
    0x41: { id: 2, advanceWidth: 600, path: { commands: box } },
    0x20: { id: 1, advanceWidth: 300, path: { commands: [] } },
  };
  return {
    unitsPerEm: 1000, ascent: 880, descent: -120, lineGap: 0, capHeight: 700, xHeight: 500, italicAngle: 0,
    underlinePosition: -100, underlineThickness: 50, postscriptName: "ViewTestCJK", familyName: "View Test CJK",
    hasGlyphForCodePoint: (cp: number) => cp in glyphs,
    glyphForCodePoint: (cp: number) => glyphs[cp]!,
  };
}

test("generated TrueType subset parses and maps code points to the copied outlines", () => {
  const ttf = buildSubsetTtf(fakeSource() as never, "中文A中");
  const f = fontkit.create(ttf);
  assert.equal(f.numGlyphs, 4); // .notdef + 中 文 A
  assert.equal(f.postscriptName?.endsWith("+ViewTestCJK"), true);
  for (const ch of "中文A") assert.ok(f.hasGlyphForCodePoint(ch.codePointAt(0)!), ch);
  const g = f.glyphForCodePoint(0x6587);
  assert.equal(g.advanceWidth, 1000);
  const bb = g.bbox;
  assert.ok(Math.abs(bb.minX - 100) <= 1 && Math.abs(bb.maxX - 900) <= 2 && Math.abs(bb.maxY - 800) <= 2, JSON.stringify(bb));
  assert.equal(f.glyphForCodePoint(0x41).advanceWidth, 600);
});

test("a generated font through the provider round-trips through pdf.js (machine independent)", async () => {
  const ttf = buildSubsetTtf(fakeSource() as never, "中文 A");
  const provider: FontProvider = async () => [{ name: "test", load: async () => ttf }];
  const { bytes, warnings } = await exportText("中文 A", provider);
  assert.deepEqual(warnings, []);
  const [f] = await apFonts(bytes);
  assert.equal(f!.fileKey, "FontFile2");
  const { annots } = await pdfjsText(bytes);
  assert.equal(annots[0]!.textContent?.join(""), "中文 A");
  assert.equal(annots[0]!.contentsObj?.str, "中文 A");
});

test("collections: faces are found by PostScript name", { skip: !CJK_OK?.file.endsWith(".ttc") && "no TTC font" }, () => {
  const names = ttcNames(new Uint8Array(readFileSync(CJK_OK!.file)));
  assert.ok(names.includes(CJK_OK!.ps), JSON.stringify(names));
});
