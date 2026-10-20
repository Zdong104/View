import assert from "node:assert/strict";
import { test } from "node:test";
import { findInPage, normalizeQuery, pageTextFromRuns, runsFromOcr, runsFromTextContent } from "../../src/viewers/pdf/search.ts";

const info = { w: 600, h: 800, x0: 0, y0: 0, rotate: 0 };

test("text items become per-char quads in surface units (top-left origin)", () => {
  const items = [{ str: "Hello", dir: "ltr", transform: [10, 0, 0, 10, 100, 700], width: 50, height: 10, fontName: "f", hasEOL: true }];
  const { runs } = runsFromTextContent(items, { f: { ascent: 0.8, descent: -0.2, vertical: false, fontFamily: "sans" } }, info);
  assert.equal(runs.length, 1);
  const q = runs[0]!.quads;
  assert.equal(q.length, 5 * 8);
  // first char: x 100..110, baseline y 700 -> surface v = 100; top = 100 - 8, bottom = 100 + 2
  assert.deepEqual(q.slice(0, 8).map((n) => Math.round(n * 10) / 10), [100, 92, 110, 92, 110, 102, 100, 102]);
});

test("search is case-insensitive, collapses whitespace and returns boxes", () => {
  const items = [
    { str: "The Quick", dir: "ltr", transform: [10, 0, 0, 10, 0, 700], width: 90, height: 10, fontName: "f", hasEOL: false },
    { str: "  brown fox", dir: "ltr", transform: [10, 0, 0, 10, 95, 700], width: 110, height: 10, fontName: "f", hasEOL: true },
    { str: "quick again", dir: "ltr", transform: [10, 0, 0, 10, 0, 680], width: 110, height: 10, fontName: "f", hasEOL: true },
  ];
  const { runs, eol } = runsFromTextContent(items, {}, info);
  const t = pageTextFromRuns(runs, true, eol);
  assert.equal(t.norm, "the quick brown fox quick again ");
  const hits = findInPage(t, normalizeQuery("QUICK   brown"));
  assert.equal(hits.length, 1);
  assert.equal(findInPage(t, "quick").length, 2);
  assert.equal(findInPage(t, "zebra").length, 0);
  const [x, , w] = hits[0]!;
  assert.ok(x! >= 39 && x! <= 41 && w! > 100, `box ${hits[0]}`);
});

test("OCR lines get approximate character quads", () => {
  const runs = runsFromOcr([{ text: "ab", box: [0, 0, 20, 0, 20, 10, 0, 10] }]);
  assert.deepEqual(runs[0]!.quads, [0, 0, 10, 0, 10, 10, 0, 10, 10, 0, 20, 0, 20, 10, 10, 10]);
  const t = pageTextFromRuns(runs, false);
  assert.equal(findInPage(t, "b").length, 1);
});

test("GlyphLessFont ToUnicode maps 2-byte CIDs to UTF-16 (astral chars as surrogate pairs)", async () => {
  const { toUnicodeCMap } = await import("../../src/viewers/pdf/build.ts");
  const cmap = toUnicodeCMap(new Map([["你", 1], ["𠀀", 2]]));
  assert.match(cmap, /<0001> <4F60>/);
  assert.match(cmap, /<0002> <D840DC00>/);
  assert.match(cmap, /2 beginbfchar/);
});
