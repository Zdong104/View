import assert from "node:assert/strict";
import { test } from "node:test";
import {
  defaultName, displaySize, displayToSurface, pageFromHash, pageRanges, paperName, surfaceToDisplay,
} from "../../src/viewers/pdf/geometry.ts";
import { PageModel, pageRef, surfaceOf } from "../../src/viewers/pdf/model.ts";

const model = (n: number) => new PageModel(Array.from({ length: n }, (_, i) => pageRef(0, i)));
const idx = (m: PageModel) => m.pages.map((p) => p.index);

test("move pages like preview/pdf_view.move_rows", () => {
  const m = model(5);
  const [a, , c] = m.pages;
  assert.equal(m.move([a!.id], 3), 2);
  assert.deepEqual(idx(m), [1, 2, 0, 3, 4]);
  assert.equal(m.move([a!.id, c!.id], 0), 0);
  assert.deepEqual(idx(m), [2, 0, 1, 3, 4], "moved pages keep their document order");
  assert.equal(m.move([m.pages[4]!.id], 5), -1, "moving the last page to the end is a no-op");
  assert.ok(m.dirty);
});

test("delete keeps at least one page, rotate wraps, undo/redo restore", () => {
  const m = model(3);
  assert.equal(m.remove(m.pages.map((p) => p.id)), false);
  assert.ok(m.remove([m.pages[1]!.id]));
  assert.deepEqual(idx(m), [0, 2]);
  m.rotate([m.pages[0]!.id], -1);
  assert.equal(m.pages[0]!.rotation, 270);
  m.rotate([m.pages[0]!.id], 2);
  assert.equal(m.pages[0]!.rotation, 90);
  assert.equal(m.undo(), "pages");
  assert.equal(m.pages[0]!.rotation, 270);
  m.undo(); m.undo();
  assert.deepEqual(idx(m), [0, 1, 2]);
  assert.ok(!m.dirty);
  m.redo();
  assert.deepEqual(idx(m), [0, 2]);
  let ext = 0;
  m.pushExternal(() => ext--, () => ext++);
  assert.equal(m.undo(), "external");
  assert.equal(ext, -1);
  assert.equal(m.redo(), "external");
  assert.equal(ext, 0);
  assert.ok(!m.canRedo);
});

test("insert, identity and clean state", () => {
  const m = model(2);
  assert.ok(m.identity);
  m.insert(1, [pageRef(1, 0), pageRef(1, 1)]);
  assert.deepEqual(m.pages.map((p) => `${p.src}:${p.index}`), ["0:0", "1:0", "1:1", "0:1"]);
  assert.ok(!m.identity);
  m.markClean();
  assert.ok(!m.dirty);
  assert.equal(surfaceOf(m.pages[0]!), "p0");
  assert.equal(surfaceOf(m.pages[1]!), "s1p0");
});

test("surface <-> display frames are inverse for every rotation", () => {
  for (const r of [0, 90, 180, 270, -90, 450]) {
    const [x, y] = surfaceToDisplay(r, 600, 800, 100, 50);
    assert.deepEqual(displayToSurface(r, 600, 800, x, y), [100, 50], `rotation ${r}`);
  }
  assert.deepEqual(surfaceToDisplay(90, 600, 800, 0, 0), [800, 0], "top-left goes top-right when turned clockwise");
  assert.deepEqual(displaySize(270, 600, 800), [800, 600]);
});

test("names, ranges, paper sizes, hash", () => {
  assert.equal(pageRanges([1, 2, 3, 5, 7, 8]), "1-3_5_7-8");
  assert.equal(defaultName("Report.final.pdf", "-edited"), "Report.final-edited.pdf");
  assert.equal(paperName(595.28, 841.89), "A4");
  assert.equal(paperName(792, 612), "Letter (landscape)");
  assert.equal(paperName(500, 500), null);
  assert.equal(pageFromHash("#page=12&zoom=100"), 12);
  assert.equal(pageFromHash("#nameddest=x"), null);
});
