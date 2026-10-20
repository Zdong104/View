// Stream B e2e: the markup layer on dist/dev/markup-playground.html. Every tool
// is drawn with real (CDP) pointer input, then selection, move/resize, undo/redo,
// toJSON(), render(), and a signature captured with the mouse, placed and
// exported to PDF as vector paths.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { decodePDFRawStream, PDFDict, PDFDocument, PDFName, PDFStream } from "@cantoo/pdf-lib";
import { execFileSync } from "node:child_process";
import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs";
import { launch } from "./harness.mjs";

let ctx;
before(async () => { ctx = await launch(); });
after(async () => { await ctx?.browser.close(); });

const OX = 40, OY = 80; // playground surface origin (client px)

async function playground(query = "kind=pdf") {
  const page = await ctx.browser.newPage();
  await page.setViewport({ width: 1000, height: 700 });
  await page.goto(`${ctx.base}dev/markup-playground.html?${query}`);
  await page.waitForSelector("html[data-state=ready]");
  return page;
}

const json = (page) => page.evaluate(() => globalThis.markup.toJSON());
const last = async (page) => (await json(page)).at(-1);

async function tool(page, name) {
  if (["line", "arrow", "rect", "rrect", "oval", "bubble", "star"].includes(name)) {
    await page.click('[data-action="shapes"]');
    await page.click(`.mk-pop [data-shape="${name}"]`);
  } else {
    await page.click(`.markup-toolbar [data-tool="${name}"]`);
  }
  assert.equal(await page.evaluate(() => globalThis.markup.tool), name);
}

/** Drag through surface points (surface units = px at scale 1). */
async function drag(page, pts, steps = 6) {
  const [x0, y0] = pts[0];
  await page.mouse.move(OX + x0, OY + y0);
  await page.mouse.down();
  for (const [x, y] of pts.slice(1)) await page.mouse.move(OX + x, OY + y, { steps });
  await page.mouse.up();
}

const near = (a, b, tol = 1.5) => Math.abs(a - b) <= tol;

test("every tool creates its annotation; toJSON reflects it", async () => {
  const page = await playground();
  const shapes = { rect: [[20, 150], [120, 220]], rrect: [[140, 150], [240, 220]], oval: [[260, 150], [360, 220]],
    bubble: [[380, 150], [480, 220]], star: [[500, 150], [580, 230]] };
  for (const [name, [[x0, y0], [x1, y1]]] of Object.entries(shapes)) {
    await tool(page, name);
    await drag(page, [[x0, y0], [x1, y1]]);
    const a = await last(page);
    assert.equal(a.type, name);
    assert.ok(near(a.x, x0) && near(a.y, y0) && near(a.w, x1 - x0) && near(a.h, y1 - y0), JSON.stringify(a));
    assert.equal(a.style.stroke, "#d62828");
    // shape tools are not sticky: back to Select with the new shape selected
    assert.equal(await page.evaluate(() => globalThis.markup.tool), "select");
    assert.deepEqual(await page.evaluate(() => globalThis.markup.selection), [a.id]);
  }
  for (const name of ["line", "arrow"]) {
    await tool(page, name);
    await drag(page, [[30, 260], [200, 300]]);
    const a = await last(page);
    assert.equal(a.type, name);
    assert.ok(near(a.x1, 30) && near(a.y1, 260) && near(a.x2, 200) && near(a.y2, 300), JSON.stringify(a));
  }
  // a click without dragging makes a default-size shape
  await tool(page, "rect");
  await page.mouse.click(OX + 400, OY + 300);
  const def = await last(page);
  assert.equal(def.type, "rect");
  assert.equal(def.w, 100);
  assert.equal(def.h, 70);

  // sketch: a rough closed rectangle is recognised, a squiggle stays ink
  await tool(page, "sketch");
  await drag(page, [[230, 250], [330, 252], [331, 330], [229, 331], [231, 256]], 10);
  assert.equal((await last(page)).type, "rect");
  assert.equal(await page.evaluate(() => globalThis.markup.tool), "sketch", "sketch is sticky");
  await drag(page, [[350, 250], [370, 290], [390, 255], [410, 300], [430, 260]], 8);
  const ink = await last(page);
  assert.equal(ink.type, "ink");
  assert.ok(ink.strokes[0].length >= 30);
  assert.ok(ink.strokes[0].filter((_, i) => i % 3 === 2).every((p) => p === 0.5), "sketch ink has no pressure");

  // draw: raw ink with speed-derived pressure (variable width)
  await tool(page, "draw");
  await drag(page, [[450, 250], [470, 300], [520, 260], [560, 320]], 12);
  const drawn = await last(page);
  assert.equal(drawn.type, "ink");
  const pr = drawn.strokes[0].filter((_, i) => i % 3 === 2);
  assert.ok(new Set(pr.map((p) => p.toFixed(3))).size > 2, "pressure varies");

  // text box: click, type, Ctrl+Enter
  await tool(page, "text");
  await page.mouse.click(OX + 20, OY + 340);
  await page.waitForSelector(".mk-text-editor");
  await page.keyboard.type("Hello markup");
  await page.keyboard.down("Control"); await page.keyboard.press("Enter"); await page.keyboard.up("Control");
  const text = await last(page);
  assert.equal(text.type, "text");
  assert.equal(text.text, "Hello markup");
  assert.ok(near(text.x, 20) && near(text.y, 340));
  assert.ok(text.h > 14);
  assert.equal(await page.$(".mk-text-editor"), null);

  // note: click, type in the popover, Ctrl+Enter
  await tool(page, "note");
  await page.mouse.click(OX + 560, OY + 30);
  await page.waitForSelector(".mk-note-pop textarea");
  await page.keyboard.type("Remember this");
  await page.keyboard.down("Control"); await page.keyboard.press("Enter"); await page.keyboard.up("Control");
  const note = await last(page);
  assert.equal(note.type, "note");
  assert.equal(note.text, "Remember this");

  // text markup snaps to the text layer's characters
  const cw = await page.evaluate(() => globalThis.charWidth);
  const at = (line, i) => [20 + (i + 0.5) * cw, line === 0 ? 68 : 108];
  for (const [name, line, i0, i1, want] of [["highlight", 0, 4, 8, "quick"], ["underline", 0, 16, 18, "fox"], ["strike", 1, 0, 5, "Second"]]) {
    await tool(page, name);
    await drag(page, [at(line, i0), at(line, i1)]);
    const a = await last(page);
    assert.equal(a.type, name);
    assert.equal(a.text, want);
    assert.equal(a.quads.length, 8);
    assert.ok(near(a.quads[0], 20 + i0 * cw, 0.01) && near(a.quads[2], 20 + (i1 + 1) * cw, 0.01), JSON.stringify(a.quads));
    assert.equal(await page.evaluate(() => globalThis.markup.tool), name, "text tools are sticky");
  }
  assert.equal((await last(page)).style.stroke, "#d62828");
  assert.equal((await json(page)).find((a) => a.type === "highlight").style.stroke, "#ffdb1a");

  const types = (await json(page)).map((a) => a.type);
  for (const t of ["rect", "rrect", "oval", "bubble", "star", "line", "arrow", "ink", "text", "note", "highlight", "underline", "strike"]) {
    assert.ok(types.includes(t), `has ${t}`);
  }
  // every annotation is in the SVG overlay, and render() paints them
  const n = await page.$$eval(".mk-items > g[data-id]", (gs) => gs.length);
  assert.equal(n, types.length);
  assert.ok(await page.evaluate(() => globalThis.inkPixels()) > 5000);
  await page.close();
});

test("select, move, resize, restyle, delete; undo and redo", async () => {
  const page = await playground("kind=image");
  assert.equal(await page.$eval('[data-tool="highlight"]', (b) => b.hidden), true, "no text tools on images");
  await tool(page, "rect");
  await drag(page, [[100, 100], [200, 160]]);
  const id = (await last(page)).id;
  // click elsewhere deselects, click on the shape selects
  await page.mouse.click(OX + 400, OY + 350);
  assert.deepEqual(await page.evaluate(() => globalThis.markup.selection), []);
  await page.mouse.click(OX + 150, OY + 130);
  assert.deepEqual(await page.evaluate(() => globalThis.markup.selection), [id]);
  // move
  await drag(page, [[150, 130], [200, 180]]);
  let a = await last(page);
  assert.ok(near(a.x, 150) && near(a.y, 150), JSON.stringify(a));
  // resize with the south-east handle
  await drag(page, [[250, 210], [300, 250]]);
  a = await last(page);
  assert.ok(near(a.w, 150) && near(a.h, 100), JSON.stringify(a));
  // shift+resize from a corner keeps the aspect ratio
  await page.keyboard.down("Shift");
  await drag(page, [[300, 250], [330, 255]]);
  await page.keyboard.up("Shift");
  a = await last(page);
  assert.ok(near(a.w / a.h, 1.5, 0.02), JSON.stringify(a));
  // colour from the toolbar applies to the selection (and becomes the default)
  await page.click('[data-action="stroke"]');
  await page.click('.mk-pop [data-color="#3584e4"]');
  assert.equal((await last(page)).style.stroke, "#3584e4");
  await page.click('[data-action="width"]');
  await page.click('.mk-pop [data-width="5"]');
  assert.equal((await last(page)).style.width, 5);
  // arrow keys nudge
  const before = await last(page);
  await page.keyboard.press("ArrowRight");
  assert.ok(near((await last(page)).x, before.x + 1, 0.01));
  // Delete removes; Ctrl+Z restores; Ctrl+Shift+Z deletes again
  await page.keyboard.press("Delete");
  assert.equal((await json(page)).length, 0);
  await page.keyboard.down("Control"); await page.keyboard.press("z"); await page.keyboard.up("Control");
  assert.equal((await json(page)).length, 1);
  await page.keyboard.down("Control"); await page.keyboard.down("Shift"); await page.keyboard.press("Z");
  await page.keyboard.up("Shift"); await page.keyboard.up("Control");
  assert.equal((await json(page)).length, 0);
  // full history through the API
  const r = await page.evaluate(() => {
    const m = globalThis.markup;
    const n = [];
    while (m.canUndo) { m.undo(); n.push(m.toJSON().length); }
    const undone = m.toJSON().length;
    while (m.canRedo) m.redo();
    return { undone, redone: m.toJSON().length, steps: n.length, dirty: m.dirty };
  });
  assert.equal(r.undone, 0);
  assert.equal(r.redone, 0);
  assert.ok(r.steps >= 8, JSON.stringify(r));
  assert.equal(r.dirty, true);
  // copy / paste offsets by 12
  await page.evaluate(() => globalThis.markup.undo());
  await page.evaluate(() => globalThis.markup.select([globalThis.markup.toJSON()[0].id]));
  await page.keyboard.down("Control"); await page.keyboard.press("c"); await page.keyboard.press("v"); await page.keyboard.up("Control");
  const all = await json(page);
  assert.equal(all.length, 2);
  assert.ok(near(all[1].x, all[0].x + 12, 0.01));
  await page.evaluate(() => globalThis.markup.markSaved());
  assert.equal(await page.evaluate(() => globalThis.markup.dirty), false);
  await page.close();
});

test("rotated and zoomed surfaces map pointer input to surface units", async () => {
  const page = await playground("kind=image&scale=1.5&rot=90");
  await tool(page, "line");
  // rot 90: surface (x, y) is shown at client (OX + (H - y) * s, OY + x * s), H = 400
  const c = (x, y) => [(400 - y) * 1.5, x * 1.5];
  await drag(page, [c(100, 50), c(300, 150)]);
  const a = await last(page);
  assert.ok(near(a.x1, 100, 1) && near(a.y1, 50, 1) && near(a.x2, 300, 1) && near(a.y2, 150, 1), JSON.stringify(a));
  await page.close();
});

test("signature captured with the mouse is placed and exported as vector paths", async () => {
  const page = await playground("kind=pdf");
  await page.click('[data-action="signature"]');
  await page.waitForSelector(".mk-pop .mk-empty");
  await page.click('.mk-pop [data-action="new-signature"]');
  await page.waitForSelector("dialog.sig-dialog[open] canvas.sig-canvas");
  const box = await (await page.$("canvas.sig-canvas")).boundingBox();
  const P = (fx, fy) => [box.x + fx * box.width, box.y + fy * box.height];
  const stroke = async (pts) => {
    await page.mouse.move(...P(...pts[0]));
    await page.mouse.down();
    for (const p of pts.slice(1)) await page.mouse.move(...P(...p), { steps: 8 });
    await page.mouse.up();
  };
  assert.equal(await page.$eval('[data-action="save"]', (b) => b.disabled), true);
  await stroke([[0.15, 0.65], [0.22, 0.3], [0.3, 0.7], [0.4, 0.35], [0.5, 0.68], [0.62, 0.4]]);
  await stroke([[0.65, 0.6], [0.8, 0.45], [0.9, 0.62]]);
  await page.click('dialog.sig-dialog [data-action="save"]');
  await page.waitForFunction(() => globalThis.markup.toJSON().at(-1)?.type === "signature"); // placed after the IndexedDB write
  const sig = await last(page);
  assert.equal(sig.type, "signature");
  assert.equal(sig.strokes.length, 2);
  assert.ok(sig.strokes.flat().every((v) => v >= 0 && v <= 1), "normalized strokes");
  assert.ok(sig.w > 50 && sig.h > 5 && sig.style.width > 0);
  assert.equal(sig.surface, "p0");
  const stored = await page.evaluate(async () => (await globalThis.markup.signatures.list()).length);
  assert.equal(stored, 1);

  // exported PDF: no annotation, the signature is vector paths in the page content
  const b64 = await page.evaluate(() => globalThis.exportPdf(false));
  const doc = await PDFDocument.load(Buffer.from(b64, "base64"));
  assert.equal(doc.getPage(0).node.Annots()?.size() ?? 0, 0);
  const xo = doc.getPage(0).node.Resources().lookup(PDFName.of("XObject"), PDFDict);
  const keys = xo.keys();
  assert.equal(keys.length, 1);
  const s = xo.lookup(keys[0], PDFStream);
  assert.equal(s.dict.get(PDFName.of("Subtype")).toString(), "/Form");
  const content = new TextDecoder().decode(decodePDFRawStream(s).decode());
  assert.ok((content.match(/ c\n/g) ?? []).length > 20, "Bézier path segments");
  assert.match(content, /\nf\n/);
  assert.match(content, /0\.11 0\.247 0\.58 rg|0 0 0 rg|0\.078 0\.078 0\.078 rg/);
  assert.doesNotMatch(content, /\bDo\b|\bBI\b/);

  // stored in IndexedDB: survives a reload and is offered in the menu
  await page.reload();
  await page.waitForSelector("html[data-state=ready]");
  await page.click('[data-action="signature"]');
  await page.waitForSelector(".mk-pop [data-signature]");
  await page.click(".mk-pop [data-signature]");
  const again = await last(page);
  assert.equal(again.type, "signature");
  assert.deepEqual(again.strokes, sig.strokes);
  await page.close();
});

test("trackpad mode (Pointer Lock): swiping draws without a click, on the page and in the signature pad", async () => {
  const page = await playground("kind=pdf");
  await tool(page, "draw");
  await page.click('[data-action="trackpad"]');
  await page.waitForFunction(() => document.pointerLockElement?.matches(".mk-layer"));
  // swipe to sign: plain movement draws, no button held
  for (let i = 1; i <= 20; i++) await page.mouse.move(500 + i * 5, 300 + (i % 5) * 4);
  await page.keyboard.press("Enter"); // any key ends the session
  await page.waitForFunction(() => !document.pointerLockElement);
  const ink = await last(page);
  assert.equal(ink.type, "ink");
  assert.ok(ink.strokes[0].length / 3 >= 15, String(ink.strokes[0].length));

  // Space switches to click-drag: movement alone draws nothing, a held click does
  const before = (await page.evaluate(() => globalThis.markup.toJSON().length));
  await page.click('[data-action="trackpad"]');
  await page.waitForFunction(() => document.pointerLockElement?.matches(".mk-layer"));
  await page.keyboard.press(" ");
  for (let i = 1; i <= 10; i++) await page.mouse.move(400 + i * 5, 200);
  await page.mouse.down();
  for (let i = 1; i <= 10; i++) await page.mouse.move(450 + i * 5, 220 + i);
  await page.mouse.up();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.pointerLockElement);
  const drag = await last(page);
  assert.equal(await page.evaluate(() => globalThis.markup.toJSON().length), before + 1);
  assert.equal(drag.strokes.length, 1, "only the click-drag drew");

  // signature dialog: Space starts, swiping draws, a pause lifts the pen, Shift moves without ink
  await page.click('[data-action="signature"]');
  await page.waitForSelector('.mk-pop [data-action="new-signature"]');
  await page.click('.mk-pop [data-action="new-signature"]');
  await page.waitForSelector("dialog.sig-dialog[open]");
  await page.click('dialog [aria-label="Trackpad"]');
  await page.focus("canvas.sig-canvas");
  await page.keyboard.press(" ");
  await page.waitForFunction(() => document.pointerLockElement?.matches("canvas.sig-canvas"));
  for (let i = 1; i <= 15; i++) await page.mouse.move(300 + i * 6, 300 - (i % 4) * 5);
  await new Promise((r) => setTimeout(r, 300)); // finger lifted: no events
  await page.keyboard.down("Shift");
  for (let i = 1; i <= 10; i++) await page.mouse.move(390 + i * 10, 300);
  await page.keyboard.up("Shift");
  for (let i = 1; i <= 15; i++) await page.mouse.move(490 + i * 6, 320 + (i % 3) * 5);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.pointerLockElement);
  await page.click('dialog.sig-dialog [data-action="save"]');
  await page.waitForFunction(() => globalThis.markup.toJSON().at(-1)?.type === "signature"); // placed after the IndexedDB write
  const sig = await last(page);
  assert.equal(sig.type, "signature");
  assert.equal(sig.strokes.length, 2, "the pause split the strokes and the Shift move drew nothing");
  const a = sig.strokes[0], b = sig.strokes[1];
  assert.ok(b[0] - a[a.length - 3] > 0.1, `second stroke starts to the right of the first: ${a[a.length - 3]} -> ${b[0]}`);
  await page.close();
});

const hasCjkFont = (() => {
  try { return /cjk|han|hei|song|wqy|noto/i.test(execFileSync("fc-match", ["-f", "%{file}", ":lang=zh-cn"], { encoding: "utf8" })); } catch { return false; }
})();

test("CJK text boxes: without font access a warning, with Local Font Access an embedded font", async () => {
  const add = (page) => page.evaluate(() => globalThis.markup.add({ type: "text", surface: "p0", x: 20, y: 20, w: 300, h: 40,
    text: "中文签名 OK", style: { stroke: "#000000", fill: null, width: 0, opacity: 1, font: { family: "sans-serif", size: 16 } } }));
  let page = await playground("kind=pdf");
  await add(page);
  await page.evaluate(() => globalThis.exportPdf(false));
  assert.equal((await page.evaluate(() => globalThis.lastWarnings)).length, 1, "no permission in headless: fallback + warning");
  await page.close();
  if (!hasCjkFont) return;

  const cdp = await ctx.browser.target().createCDPSession();
  await cdp.send("Browser.grantPermissions", { origin: ctx.base.replace(/\/$/, ""), permissions: ["localFonts"] });
  page = await playground("kind=pdf");
  await add(page);
  const b64 = await page.evaluate(() => globalThis.exportPdf(false));
  assert.deepEqual(await page.evaluate(() => globalThis.lastWarnings), []);
  const task = pdfjs.getDocument({ data: new Uint8Array(Buffer.from(b64, "base64")) });
  const annots = await (await (await task.promise).getPage(1)).getAnnotations();
  assert.equal(annots[0].subtype, "FreeText");
  assert.equal(annots[0].textContent.join(""), "中文签名 OK");
  await task.destroy();
  await cdp.send("Browser.resetPermissions");
  await page.close();
});
