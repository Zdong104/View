// Stream F: Live Text OCR on generated English and Chinese text images
// (drawn on a canvas in dist/dev/ocr-bench.html), timings on a 1080p image,
// and the selectable text layer.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { launch } from "./harness.mjs";

let ctx;

before(async () => {
  ctx = await launch();
});

after(async () => {
  await ctx?.browser.close();
});

async function benchPage() {
  const page = await ctx.browser.newPage();
  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  await page.goto(`${ctx.base}dev/ocr-bench.html`);
  await page.waitForSelector("html[data-state=ready]");
  return page;
}

const run = (page, spec, show = false) => page.evaluate((s, sh) => globalThis.ocrBench.run(s, sh), spec, show);
const SANS = "sans-serif", CJK = "'Noto Sans CJK SC', 'Noto Sans CJK', sans-serif";

const EN = ["Invoice number 2024-0042 due on March 15", "Total amount: $1,234.56 including tax",
  "Please contact support@example.com for help", "Meeting notes from the quarterly review",
  "Revenue grew 18 percent year over year", "Shipping address: 42 Main Street, Springfield"];
const ZH = ["欢迎使用文字识别功能", "请在三月十五日之前付款", "会议记录：季度业绩回顾", "收入同比增长百分之十八", "北京市海淀区中关村大街"];

/** A 1920x1080 "screenshot": two columns, 16 lines, a third of them Chinese. */
function spec1080() {
  const lines = [];
  let y = 90;
  for (let i = 0; i < 16; i++) {
    const zh = i % 3 === 2;
    lines.push([zh ? ZH[i % ZH.length] : EN[i % EN.length], i % 2 ? 980 : 80, y, zh ? 34 : 30, zh ? CJK : SANS]);
    if (i % 2) y += 120;
  }
  return { width: 1920, height: 1080, lines, expected: lines.map((l) => l[0]) };
}

test("1080p image: first call (incl. model load) and next calls are fast enough", async () => {
  const page = await benchPage();
  const spec = spec1080();
  const times = [];
  for (let i = 0; i < 3; i++) {
    const r = await run(page, spec);
    times.push(Math.round(r.ms));
    assert.deepEqual(r.result.lines.map((l) => l.text).sort(), spec.expected.slice().sort());
    if (i === 2) console.log(`1080p timings ms: first ${times[0]}, next ${times.slice(1).join(", ")}; last: ${JSON.stringify(r.timings)}`);
  }
  assert.ok(await page.evaluate(() => globalThis.ocrBench.ocr.ready));
  // Targets are 3 s / 1.5 s; the bounds leave room for a loaded machine (parallel e2e runs).
  assert.ok(times[0] < 6000, `first call ${times[0]} ms`);
  assert.ok(Math.min(times[1], times[2]) < 3000, `next calls ${times.slice(1)} ms`);
  await page.close();
});

test("English text is recognized exactly", async () => {
  const page = await benchPage();
  const lines = EN.map((t, i) => [t, 40, 60 + i * 46, 28 + (i % 3) * 4, i % 2 ? "serif" : SANS]);
  const r = await run(page, { width: 1000, height: 520, lines });
  assert.deepEqual(r.result.lines.map((l) => l.text), EN);
  assert.equal(r.text, EN.join("\n"));
  for (const l of r.result.lines) {
    assert.ok(l.score > 0.9, `${l.text}: ${l.score}`);
    assert.equal(l.box.length, 8);
  }
  await page.close();
});

test("Chinese and mixed text is recognized exactly", async () => {
  const page = await benchPage();
  const texts = [...ZH, "支持中英文混排 Live Text 2026"];
  const lines = texts.map((t, i) => [t, 40, 70 + i * 70, 36, CJK]);
  const r = await run(page, { width: 900, height: 480, lines });
  assert.deepEqual(r.result.lines.map((l) => l.text), texts);
  await page.close();
});

// Long lowercase sans-serif lines upside down can defeat the 0/180 classifier
// (u/n, p/d look alike); the desktop's RapidOCR gives the same output for them.
test("upside-down and slanted lines (direction classifier, rotated crops)", async () => {
  const page = await benchPage();
  const r = await run(page, {
    width: 900, height: 520,
    lines: [["Rotated label 2026", 600, 80, 40, "serif", 180], ["THE QUICK BROWN FOX", 700, 200, 40, SANS, 180],
      ["Slanted line of text", 80, 320, 34, SANS, -8], ["倒置的中文文字", 500, 460, 36, CJK, 180]],
  });
  assert.deepEqual(r.result.lines.map((l) => l.text).sort(),
    ["Rotated label 2026", "THE QUICK BROWN FOX", "Slanted line of text", "倒置的中文文字"].sort());
  await page.close();
});

test("character quads: one per code point, in reading order", async () => {
  const page = await benchPage();
  const text = "Hello, world 你好";
  const r = await run(page, { width: 700, height: 120, lines: [[text, 30, 75, 40, CJK]] });
  const [l] = r.result.lines;
  assert.equal(l.text, text);
  assert.equal(l.charQuads.length, Array.from(text).length * 8);
  const lefts = [];
  for (let i = 0; i < l.charQuads.length; i += 8) lefts.push(l.charQuads[i]);
  for (let i = 1; i < lefts.length; i++) assert.ok(lefts[i] >= lefts[i - 1], `char ${i} left ${lefts[i]} < ${lefts[i - 1]}`);
  // "H" starts near the drawn x and "好" ends near the line end.
  assert.ok(Math.abs(lefts[0] - 30) < 12, `first char at ${lefts[0]}`);
  const lastRight = l.charQuads[l.charQuads.length - 6];
  assert.ok(Math.abs(lastRight - l.box[2]) < 15, `last char ends ${lastRight}, box ${l.box[2]}`);
  await page.close();
});

test("text layer: drag-select, select all, copy", async () => {
  const page = await benchPage();
  // Shown at half size: the layer maps CSS pixels back to image pixels.
  await run(page, { width: 1000, height: 300, lines: [["Select this sentence please", 40, 100, 40, SANS], ["Second line here", 40, 160, 40, SANS]] }, true);
  await page.waitForSelector("svg.ocr-layer[data-ocr-lines='2']");
  const rect = await page.$eval("svg.ocr-layer", (s) => { const r = s.getBoundingClientRect(); return { x: r.x, y: r.y }; });
  // Drag across the first word ("Select", ~ x 40..160 image px, y ~ 70..110) at scale 0.5.
  await page.mouse.move(rect.x + 18, rect.y + 44);
  await page.mouse.down();
  await page.mouse.move(rect.x + 60, rect.y + 44, { steps: 4 });
  await page.mouse.move(rect.x + 84, rect.y + 44, { steps: 4 });
  await page.mouse.up();
  const partial = await page.evaluate(() => globalThis.ocrBench.layer().selectedText());
  assert.ok(/^Sel/.test(partial) && partial.length < 12, `partial selection "${partial}"`);
  const copied = await page.evaluate(() => {
    const layer = globalThis.ocrBench.layer();
    layer.selectAll();
    const ev = new ClipboardEvent("copy", { clipboardData: new DataTransfer(), cancelable: true });
    document.dispatchEvent(ev);
    return { text: ev.clipboardData.getData("text/plain"), prevented: ev.defaultPrevented, sel: layer.el.dataset.selection };
  });
  assert.deepEqual(copied, { text: "Select this sentence please\nSecond line here", prevented: true, sel: "true" });
  // Double-click selects a word.
  await page.mouse.click(rect.x + 100, rect.y + 44, { clickCount: 1 });
  await page.mouse.click(rect.x + 100, rect.y + 44, { clickCount: 2 });
  assert.equal(await page.evaluate(() => globalThis.ocrBench.layer().selectedText()), "this");
  await page.close();
});

test("an aborted recognition rejects with AbortError; the service keeps working", async () => {
  const page = await benchPage();
  const res = await page.evaluate(async () => {
    const { ocr } = globalThis.ocrBench;
    const c = document.createElement("canvas");
    c.width = 400; c.height = 100;
    const g = c.getContext("2d");
    g.fillStyle = "#fff"; g.fillRect(0, 0, 400, 100);
    g.fillStyle = "#000"; g.font = "32px sans-serif"; g.fillText("Abort me", 20, 60);
    const ac = new AbortController();
    const p = ocr.recognize(await createImageBitmap(c), { signal: ac.signal });
    ac.abort();
    const err = await p.then(() => "resolved", (e) => e.name);
    const again = await ocr.recognize(await createImageBitmap(c));
    return { err, text: again.lines.map((l) => l.text) };
  });
  assert.deepEqual(res, { err: "AbortError", text: ["Abort me"] });
  await page.close();
});

test("a transparent PNG blob is decoded in the worker and composited on white", async () => {
  const page = await benchPage();
  const texts = await page.evaluate(async () => {
    const c = new OffscreenCanvas(500, 120);
    const g = c.getContext("2d");
    g.fillStyle = "#000"; g.font = "36px sans-serif"; g.fillText("Transparent PNG 42", 20, 70);
    const blob = await c.convertToBlob({ type: "image/png" });
    const r = await globalThis.ocrBench.ocr.recognize(blob);
    return r.lines.map((l) => l.text);
  });
  assert.deepEqual(texts, ["Transparent PNG 42"]);
  await page.close();
});
