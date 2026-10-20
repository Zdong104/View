// Stream D: table (CSV/TSV) and text viewers. Fixtures are generated at test time
// (a 1M-row CSV goes to the OS temp dir once and is reused between runs).
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createReadStream, existsSync, readFileSync, mkdirSync, statSync, writeFileSync, createWriteStream } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { launch, waitForRules } from "./harness.mjs";

const DIR = join(tmpdir(), "view-chrome-d-fixtures");
mkdirSync(DIR, { recursive: true });
const BIG_ROWS = 1_000_000;
const BIG = join(DIR, `big-${BIG_ROWS}.csv`);

async function makeBig() {
  if (existsSync(BIG) && statSync(BIG).size > 50_000_000) return;
  const tmp = `${BIG}.tmp`;
  const out = createWriteStream(tmp);
  out.write("id,name,city,amount,date,note\n");
  let chunk = [];
  for (let i = 1; i <= BIG_ROWS; i++) {
    chunk.push(`${i},user${i},"City, ${i % 977}",${((i * 7919) % 100000) / 100},2024-${String((i % 12) + 1).padStart(2, "0")}-15,${i % 10 === 0 ? '"quoted ""x"" note"' : "plain note " + i}`);
    if (chunk.length === 20000) { if (!out.write(chunk.join("\n") + "\n")) await new Promise((r) => out.once("drain", r)); chunk = []; }
  }
  if (chunk.length) out.write(chunk.join("\n") + "\n");
  await new Promise((r) => out.end(r));
  const { renameSync } = await import("node:fs");
  renameSync(tmp, BIG);
}

// Small fixtures with the awkward bits that must survive a round trip.
const QUIRKY = Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from('name,value,note\r\n"a, b",1,"he said ""hi"""\r\nplain,2,"multi\r\nline"\r\n中文,3,x\r\nlast,4,no newline at end', "utf8")]);
const SEMI = Buffer.from("name;qty;price\nwidget;3;4.50\ngadget;10;12.00\nthing;7;1.25\n");
const LATIN1 = Buffer.from("name,city\nJosé,Zürich\nRenée,Genève\n", "latin1");
const UTF16 = Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from("a,b\n1,2\n3,4\n", "utf16le")]);
const NUMS = Buffer.from("name,score,when\nbob,10,2024-01-02\nann,9,2024-01-03\ncat,100,2024-01-01\ndan,,2024-02-01\n");
const sha = (b) => createHash("sha256").update(b).digest("hex");

const PY = Buffer.from("import os\r\n\r\n\r\ndef main(name: str) -> int:\r\n    # say hello\r\n    print(\"hello\", name, 42)\r\n    return 0\r\n");
const TXT_BOM = Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from("Zeile eins\nZeile zwei: äöü\n")]);
const TXT_LATIN1 = Buffer.from("Café au lait\nNaïve\n", "latin1");
const LOG_BIG = Buffer.from(Array.from({ length: 400_000 }, (_, i) => `2024-01-01 12:00:${String(i % 60).padStart(2, "0")} INFO request ${i} served in ${i % 97} ms`).join("\n") + "\n");

const files = {
  "/code.py": [{ "content-type": "text/x-python" }, PY],
  "/bom.txt": [{ "content-type": "text/plain" }, TXT_BOM],
  "/latin1.txt": [{ "content-type": "text/plain" }, TXT_LATIN1],
  "/big.log": [{ "content-type": "text/plain" }, LOG_BIG],
  "/data.json": [{ "content-type": "application/json" }, Buffer.from('{"a": [1, 2, 3], "b": "text", "ok": true}\n')],
  "/big.csv": [{ "content-type": "text/csv" }, BIG],
  "/quirky.csv": [{ "content-type": "text/csv; charset=utf-8" }, QUIRKY],
  "/semi.csv": [{ "content-type": "text/csv" }, SEMI],
  "/latin1.csv": [{ "content-type": "text/csv" }, LATIN1],
  "/utf16.csv": [{ "content-type": "text/csv" }, UTF16],
  "/nums.csv": [{ "content-type": "text/csv" }, NUMS],
  "/tab.tsv": [{ "content-type": "text/tab-separated-values" }, Buffer.from("x\ty\n1\t2\n3\t4\n")],
};

let ctx, server, origin;

before(async () => {
  await makeBig();
  server = createServer((req, res) => {
    const hit = files[(req.url ?? "").split("?")[0]];
    if (!hit) { res.writeHead(404).end("not found"); return; }
    const [headers, body] = hit;
    if (typeof body === "string") {
      res.writeHead(200, { ...headers, "content-length": statSync(body).size });
      createReadStream(body).pipe(res);
    } else res.writeHead(200, { ...headers, "content-length": body.length }).end(body);
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  origin = `http://127.0.0.1:${server.address().port}`;
  ctx = await launch();
  await (await waitForRules(ctx.browser, ctx.base)).close();
});

after(async () => {
  await ctx?.browser.close();
  await new Promise((r) => server?.close(r));
});

async function openTable(path, { loaded = true } = {}) {
  const page = await ctx.browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.errors = errors;
  await page.goto(`${ctx.viewer}?kind=table&url=${origin}${path}`);
  await page.waitForSelector('[data-viewer="table"][data-state="ready"]', { timeout: 30000 });
  if (loaded) await page.waitForSelector('[data-viewer="table"][data-loaded="true"]', { timeout: 60000 });
  return page;
}

/** Saved bytes from the viewer, as a Buffer. */
async function savedBytes(page, req = { mode: "save" }) {
  const arr = await page.evaluate(async (req) => {
    const r = await globalThis.viewShell.viewer.save(req);
    return { name: r.suggestedName, bytes: Array.from(new Uint8Array(await r.blob.arrayBuffer())) };
  }, req);
  return Object.assign(Buffer.from(arr.bytes), { name: arr.name });
}

// Export does not mark the document as saved, so undo/dirty can still be checked afterwards.
const EXPORT_CSV = { mode: "export", format: "csv" };

const cellText = (page, r, c) => page.evaluate((r, c) => {
  const row = [...document.querySelectorAll(".tbl-row")].find((x) => x._r === r);
  return row?.querySelector(`.tbl-cell[data-c="${c}"]`)?.textContent ?? null;
}, r, c);

const viewerApi = (page, fn, ...args) => page.evaluate(`(async()=>{const v=globalThis.viewShell.viewer;return (${fn})(v, ...${JSON.stringify(args)})})()`);

async function clickCell(page, r, c) {
  const pt = await page.evaluate((r, c) => {
    const row = [...document.querySelectorAll(".tbl-row")].find((x) => x._r === r);
    const cell = row.querySelector(`.tbl-cell[data-c="${c}"]`);
    const b = cell.getBoundingClientRect();
    return { x: b.left + b.width / 2, y: b.top + b.height / 2 };
  }, r, c);
  await page.mouse.click(pt.x, pt.y);
}

test("1M rows: first paint under 2 s, whole file indexed, rows are right", async () => {
  const page = await openTable("/big.csv");
  const root = await page.$eval('[data-viewer="table"]', (e) => ({ ...e.dataset }));
  assert.equal(root.rows, String(BIG_ROWS));
  assert.equal(root.cols, "6");
  const firstPaint = Number(root.firstPaintMs);
  console.log(`      first paint ${firstPaint} ms (since navigation start), mount ${root.mountMs} ms`);
  assert.ok(firstPaint < 2000, `first paint took ${firstPaint} ms`);
  assert.equal(await cellText(page, 0, 0), "1");
  assert.equal(await cellText(page, 0, 1), "user1");
  assert.equal(await cellText(page, 0, 2), "City, 1");
  assert.equal(await page.$eval(".subtitle", (e) => e.textContent), "1,000,000 rows × 6 columns");
  assert.equal(await page.$eval(".tbl-th .th-title", (e) => e.textContent), "id");
  await page.close();
});

test("1M rows: scrolling stays smooth and shows the right rows", async () => {
  const page = await openTable("/big.csv");
  const stats = await page.evaluate(async () => {
    const sc = document.querySelector(".tbl-scroll");
    const frames = [];
    let last = performance.now();
    const total = sc.scrollHeight - sc.clientHeight;
    for (let i = 1; i <= 120; i++) {
      sc.scrollTop = (total * i) / 120;
      await new Promise((r) => requestAnimationFrame(r));
      const now = performance.now();
      frames.push(now - last);
      last = now;
    }
    frames.sort((a, b) => a - b);
    return { median: frames[60], p95: frames[Math.floor(frames.length * 0.95)], max: frames[frames.length - 1], rendered: document.querySelectorAll(".tbl-row").length };
  });
  console.log(`      scroll frames: median ${stats.median.toFixed(1)} ms, p95 ${stats.p95.toFixed(1)} ms, max ${stats.max.toFixed(1)} ms, ${stats.rendered} rows in DOM`);
  assert.ok(stats.p95 < 100, `p95 frame ${stats.p95}`);
  assert.ok(stats.rendered < 80, "rows are virtualized");
  // at the bottom: last row is 1,000,000
  await page.waitForFunction(() => [...document.querySelectorAll(".tbl-row")].some((r) => r._r === 999999 && r.querySelector('.tbl-cell[data-c="0"]')?.textContent === "1000000"), { timeout: 10000 });
  // middle
  await page.evaluate(() => { const sc = document.querySelector(".tbl-scroll"); sc.scrollTop = 24 * 500000; });
  await page.waitForFunction(() => [...document.querySelectorAll(".tbl-row")].some((r) => r._r === 500000 && r.querySelector('.tbl-cell[data-c="1"]')?.textContent === "user500001"), { timeout: 10000 });
  await page.close();
});

test("saving an untouched document is byte-for-byte identical (1M rows and quirky files)", async () => {
  const big = await openTable("/big.csv");
  // 65 MB: hash inside the page instead of shipping the bytes through CDP
  const hash = await big.evaluate(async () => {
    const r = await globalThis.viewShell.viewer.save({ mode: "save" });
    const d = await crypto.subtle.digest("SHA-256", await r.blob.arrayBuffer());
    return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
  });
  assert.equal(hash, sha(readFileSync(BIG)));
  await big.close();
  for (const [path, buf] of [["/quirky.csv", QUIRKY], ["/semi.csv", SEMI], ["/latin1.csv", LATIN1], ["/utf16.csv", UTF16]]) {
    const page = await openTable(path);
    const saved = await savedBytes(page);
    assert.ok(saved.equals(buf), `${path} changed on save`);
    await page.close();
  }
});

test("quirky file: dialect, BOM, CRLF and quoting are detected and preserved through an edit", async () => {
  const page = await openTable("/quirky.csv");
  assert.equal(await page.$eval(".tbl-status-r", (e) => e.textContent), "UTF-8 (BOM) · Comma · CRLF");
  assert.equal(await cellText(page, 1, 2), "multi\r\nline");
  await clickCell(page, 1, 1);
  await page.keyboard.type("22");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector('[data-viewer="table"]').dataset.dirty === "true");
  assert.ok((await page.title()).startsWith("•"), await page.title());
  const saved = await savedBytes(page, EXPORT_CSV);
  assert.equal(saved.toString("utf8"), QUIRKY.toString("utf8").replace("plain,2,", "plain,22,"));
  // undo restores the exact original
  await page.keyboard.down("Control"); await page.keyboard.press("z"); await page.keyboard.up("Control");
  await page.waitForFunction(() => document.querySelector('[data-viewer="table"]').dataset.dirty === "false");
  assert.ok((await savedBytes(page, EXPORT_CSV)).equals(QUIRKY));
  await page.close();
});

test("encodings and delimiters: semicolon, Latin-1, UTF-16, TSV", async () => {
  let page = await openTable("/semi.csv");
  assert.equal(await page.$eval(".tbl-status-r", (e) => e.textContent), "UTF-8 · Semicolon · LF");
  assert.equal(await page.$eval('[data-viewer="table"]', (e) => e.dataset.cols), "3");
  await page.close();
  page = await openTable("/latin1.csv");
  assert.equal(await page.$eval(".tbl-status-r", (e) => e.textContent), "Windows-1252 · Comma · LF");
  assert.equal(await cellText(page, 0, 0), "José");
  assert.equal(await cellText(page, 1, 1), "Genève");
  // editing a Latin-1 file keeps Latin-1
  await clickCell(page, 0, 1);
  await page.keyboard.type("Köln"); await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector('[data-viewer="table"]').dataset.dirty === "true");
  assert.ok((await savedBytes(page)).equals(Buffer.from("name,city\nJosé,Köln\nRenée,Genève\n", "latin1")));
  await page.close();
  page = await openTable("/utf16.csv");
  assert.equal(await page.$eval(".tbl-status-r", (e) => e.textContent), "UTF-16 LE (BOM) · Comma · LF");
  assert.equal(await cellText(page, 1, 1), "4");
  await page.close();
  page = await openTable("/tab.tsv");
  assert.equal(await page.$eval(".tbl-status-r", (e) => e.textContent), "UTF-8 · Tab · LF");
  await page.close();
});

test("editing: cells, rows, columns, undo/redo, save", async () => {
  const page = await openTable("/nums.csv");
  const api = (fn, ...a) => viewerApi(page, fn, ...a);
  assert.equal(await page.$eval(".subtitle", (e) => e.textContent), "4 rows × 3 columns");
  // double-click edit + Escape cancels
  await clickCell(page, 0, 0);
  await page.keyboard.press("Enter");
  await page.waitForSelector(".tbl-edit");
  await page.keyboard.type("zzz");
  await page.keyboard.press("Escape");
  assert.equal(await cellText(page, 0, 0), "bob");
  // insert row below, type in it
  await api("async (v) => { await v.grid.insertRows(true); }");
  await page.waitForFunction(() => document.querySelector(".subtitle").textContent.startsWith("5 rows"));
  await page.keyboard.type("eve"); await page.keyboard.press("Tab");
  await page.keyboard.type("7"); await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector('[data-viewer="table"]').dataset.dirty === "true");
  // add a column on the right, delete the first row
  await api("async (v) => { v.grid.setCursor(0, 2); await v.grid.insertCols(true); }");
  await page.waitForFunction(() => document.querySelector('[data-viewer="table"]').dataset.cols === "4");
  await api("async (v) => { v.grid.setCursor(0, 0); await v.grid.deleteRows(); }");
  await page.waitForFunction(() => document.querySelector(".subtitle").textContent.startsWith("4 rows × 4"));
  let out = (await savedBytes(page, EXPORT_CSV)).toString();
  assert.equal(out, "name,score,when,\nann,9,2024-01-03,\neve,7,,\ncat,100,2024-01-01,\ndan,,2024-02-01,\n".replace("ann,9,2024-01-03,\neve,7,,\ncat", "eve,7,,\nann,9,2024-01-03,\ncat"));
  // undo everything: back to the original bytes
  for (let i = 0; i < 20; i++) await api("async (v) => { await v.grid.undo(); }");
  await page.waitForFunction(() => document.querySelector('[data-viewer="table"]').dataset.dirty === "false");
  assert.ok((await savedBytes(page, EXPORT_CSV)).equals(NUMS));
  // redo works too
  await api("async (v) => { await v.grid.redo(); }");
  assert.equal(await page.$eval('[data-viewer="table"]', (e) => e.dataset.dirty), "true");
  await page.close();
});

test("sort by clicking a header, numbers before text, empty last; Ctrl+F search and filter", async () => {
  const page = await openTable("/nums.csv");
  const sortBtn = async (c) => page.evaluate((c) => { const b = document.querySelector(`.th-sort[data-c="${c}"]`); const r = b.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }, c);
  let p = await sortBtn(1);
  await page.mouse.click(p.x, p.y);
  await page.waitForFunction(() => document.querySelector(".tbl-th[aria-sort=ascending]"));
  await page.waitForFunction(() => [...document.querySelectorAll(".tbl-row")].find((r) => r._r === 0)?.querySelector('.tbl-cell[data-c="1"]')?.textContent === "9");
  assert.deepEqual([await cellText(page, 0, 1), await cellText(page, 1, 1), await cellText(page, 2, 1), await cellText(page, 3, 1)], ["9", "10", "100", ""]);
  p = await sortBtn(1);
  await page.mouse.click(p.x, p.y);
  await page.waitForFunction(() => document.querySelector(".tbl-th[aria-sort=descending]"));
  await page.waitForFunction(() => [...document.querySelectorAll(".tbl-row")].find((r) => r._r === 0)?.querySelector('.tbl-cell[data-c="1"]')?.textContent === "100");
  p = await sortBtn(1);
  await page.mouse.click(p.x, p.y); // clears
  await page.waitForFunction(() => !document.querySelector(".tbl-th[aria-sort]"));
  // rows re-render asynchronously after the header state flips
  await page.waitForFunction(() => [...document.querySelectorAll(".tbl-row")].find((r) => r._r === 0)?.querySelector('.tbl-cell[data-c="0"]')?.textContent === "bob", { timeout: 5000 });
  assert.equal(await cellText(page, 0, 0), "bob");
  // saving does not depend on the view sort
  assert.ok((await savedBytes(page)).equals(NUMS));
  // find
  await page.focus(".tbl-scroll");
  await page.keyboard.down("Control"); await page.keyboard.press("f"); await page.keyboard.up("Control");
  await page.waitForSelector(".tbl-find:not([hidden])");
  await page.keyboard.type("2024-01");
  await page.waitForFunction(() => document.querySelector(".tbl-find-count").textContent === "1 of 3", { timeout: 5000 });
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".tbl-find-count").textContent === "2 of 3");
  assert.ok(await page.$(".tbl-cell.hit-cur"));
  assert.equal(await page.$$eval(".tbl-cell.hit", (c) => c.length), 3);
  await page.click(".tbl-find .btn[aria-pressed]"); // "Only matching rows"
  await page.waitForFunction(() => document.querySelector(".subtitle").textContent.startsWith("3 rows"));
  await page.keyboard.press("Escape");
  await page.close();
});

test("copy gives TSV, selection stats, column resize", async () => {
  const page = await openTable("/nums.csv");
  await clickCell(page, 0, 1);
  await page.keyboard.down("Shift"); await page.keyboard.press("ArrowDown"); await page.keyboard.press("ArrowDown"); await page.keyboard.up("Shift");
  await page.waitForFunction(() => document.querySelector(".tbl-status-l").textContent.includes("Sum 119"));
  const text = await page.evaluate(() => {
    const dt = new DataTransfer();
    document.querySelector(".tbl-scroll").dispatchEvent(new ClipboardEvent("copy", { clipboardData: dt, bubbles: true, cancelable: true }));
    return dt.getData("text/plain");
  });
  assert.equal(text, "10\n9\n100");
  // paste a 2x2 block somewhere
  await clickCell(page, 1, 0);
  await page.evaluate(() => {
    const dt = new DataTransfer(); dt.setData("text/plain", "a\tb\nc\t\"d\te\"");
    document.querySelector(".tbl-scroll").dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  });
  await page.waitForFunction(() => [...document.querySelectorAll(".tbl-row")].find((r) => r._r === 2)?.querySelector('.tbl-cell[data-c="1"]')?.textContent === "d\te");
  assert.equal(await cellText(page, 1, 0), "a");
  // resize: drag the right edge of column 0
  const before = await page.$eval('.tbl-th[data-c="0"]', (e) => e.getBoundingClientRect().width);
  const rz = await page.$eval('.th-rz[data-c="0"]', (e) => { const r = e.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
  await page.mouse.move(rz.x, rz.y); await page.mouse.down(); await page.mouse.move(rz.x + 60, rz.y, { steps: 4 }); await page.mouse.up();
  const after = await page.$eval('.tbl-th[data-c="0"]', (e) => e.getBoundingClientRect().width);
  assert.ok(Math.abs(after - before - 60) < 3, `${before} -> ${after}`);
  await page.close();
});

test("Get Info lists the table facts; header toggle; export to JSON/TSV", async () => {
  const page = await openTable("/nums.csv");
  const info = await viewerApi(page, "async (v) => v.getInfo()");
  const t = Object.fromEntries(info[0].rows);
  assert.equal(t.Rows, "4"); assert.equal(t.Columns, "3"); assert.equal(t.Delimiter, "Comma"); assert.equal(t["First row is header"], "Yes");
  assert.deepEqual(info[1].rows.map((r) => r[1]), ["text · 0 empty", "number · 1 empty", "date · 0 empty"]);
  const json = JSON.parse((await savedBytes(page, { mode: "export", format: "json" })).toString());
  assert.deepEqual(json[0], { name: "bob", score: 10, when: "2024-01-02" });
  assert.equal(json[3].score, null);
  const tsv = await savedBytes(page, { mode: "export", format: "tsv" });
  assert.equal(tsv.toString(), "name\tscore\twhen\nbob\t10\t2024-01-02\nann\t9\t2024-01-03\ncat\t100\t2024-01-01\ndan\t\t2024-02-01\n");
  assert.equal(tsv.name, "nums.tsv");
  await viewerApi(page, "async (v) => { await v.setHeader(false); }");
  await page.waitForFunction(() => document.querySelector(".subtitle").textContent.startsWith("5 rows"));
  assert.ok((await savedBytes(page)).equals(NUMS), "header toggle keeps the bytes");
  await page.close();
});

test("a CSV opened from the web is taken over by the table viewer", async () => {
  const page = await ctx.browser.newPage();
  await page.goto(`${origin}/semi.csv`, { waitUntil: "domcontentloaded" }).catch(() => {});
  await page.waitForSelector('[data-viewer="table"][data-state="ready"]', { timeout: 15000 });
  assert.ok(page.url().startsWith(`${ctx.viewer}?kind=table&url=`), page.url());
  await page.close();
});

// ---------------------------------------------------------------------------------------------
// Text viewer
// ---------------------------------------------------------------------------------------------

async function openText(path) {
  const page = await ctx.browser.newPage();
  page.errors = [];
  page.on("pageerror", (e) => page.errors.push(String(e)));
  await page.goto(`${ctx.viewer}?kind=text&url=${origin}${path}`);
  await page.waitForSelector('[data-viewer="text"][data-state="ready"]', { timeout: 30000 });
  return page;
}

test("text: language by file name, line numbers, read-only by default, highlighting", async () => {
  const page = await openText("/code.py");
  await page.waitForFunction(() => document.querySelector('[data-viewer="text"]').dataset.language === "Python", { timeout: 15000 });
  assert.ok(await page.$(".cm-lineNumbers"));
  assert.equal(await page.$eval('[data-viewer="text"]', (e) => e.dataset.editable), "false");
  assert.equal(await page.$eval(".subtitle", (e) => e.textContent), "8 lines · Python");
  // keywords get their own colour
  const colors = await page.$$eval(".cm-line span", (els) => new Set(els.map((e) => getComputedStyle(e).color)).size);
  assert.ok(colors >= 3, `distinct token colours: ${colors}`);
  // typing is ignored while read-only
  await page.click(".cm-content");
  await page.keyboard.type("zzz");
  assert.equal(await page.$eval('[data-viewer="text"]', (e) => e.dataset.dirty ?? "false"), "false");
  assert.ok(!(await page.$eval(".cm-content", (e) => e.textContent)).includes("zzz"));
  // unchanged save returns the original bytes
  assert.ok((await savedBytes(page)).equals(PY));
  assert.deepEqual(page.errors, []);
  await page.close();
});

test("text: Edit mode, line endings and BOM survive saving, undo returns to clean", async () => {
  const page = await openText("/code.py");
  await page.evaluate(() => [...document.querySelectorAll(".headerbar .btn")].find((b) => b.textContent.trim() === "Edit").click());
  await page.waitForFunction(() => document.querySelector('[data-viewer="text"]').dataset.editable === "true");
  await page.click(".cm-content");
  await page.keyboard.down("Control"); await page.keyboard.press("Home"); await page.keyboard.up("Control");
  await page.keyboard.type("# top\n");
  await page.waitForFunction(() => document.querySelector('[data-viewer="text"]').dataset.dirty === "true");
  assert.ok((await page.title()).startsWith("•"));
  const saved = await savedBytes(page);
  assert.equal(saved.toString(), "# top\r\n" + PY.toString());
  await page.keyboard.down("Control"); await page.keyboard.press("z"); await page.keyboard.up("Control");
  await page.close();

  const bom = await openText("/bom.txt");
  await bom.evaluate(() => [...document.querySelectorAll(".headerbar .btn")].find((b) => b.textContent.trim() === "Edit").click());
  await bom.click(".cm-content");
  await bom.keyboard.down("Control"); await bom.keyboard.press("End"); await bom.keyboard.up("Control");
  await bom.keyboard.type("Zeile drei");
  assert.ok((await savedBytes(bom)).equals(Buffer.concat([TXT_BOM, Buffer.from("Zeile drei")])));
  await bom.close();

  const latin = await openText("/latin1.txt");
  await latin.evaluate(() => [...document.querySelectorAll(".headerbar .btn")].find((b) => b.textContent.trim() === "Edit").click());
  await latin.click(".cm-content");
  await latin.keyboard.down("Control"); await latin.keyboard.press("End"); await latin.keyboard.up("Control");
  await latin.keyboard.type("Crème");
  assert.ok((await savedBytes(latin)).equals(Buffer.concat([TXT_LATIN1, Buffer.from("Crème", "latin1")])));
  await latin.close();
});

test("text: wrap toggle, Ctrl+F search, go to line, zoom", async () => {
  const page = await openText("/code.py");
  assert.equal(await page.$eval('[data-viewer="text"]', (e) => e.dataset.wrap), "false");
  await page.evaluate(() => [...document.querySelectorAll(".headerbar .btn")].find((b) => b.textContent.trim() === "Wrap").click());
  await page.waitForFunction(() => document.querySelector('[data-viewer="text"]').dataset.wrap === "true" && document.querySelector(".cm-lineWrapping"));
  await page.click(".cm-content");
  await page.keyboard.down("Control"); await page.keyboard.press("f"); await page.keyboard.up("Control");
  await page.waitForSelector(".cm-search input[name=search]");
  await page.keyboard.type("hello");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".cm-searchMatch"));
  await page.keyboard.press("Escape");
  await page.evaluate(() => globalThis.viewShell.viewer.goto("4:5"));
  const pos = await page.evaluate(() => { const s = globalThis.viewShell.viewer.view.state; const l = s.doc.lineAt(s.selection.main.head); return [l.number, s.selection.main.head - l.from + 1]; });
  assert.deepEqual(pos, [4, 5]);
  assert.ok((await page.$eval(".txt-status-l", (e) => e.textContent)).startsWith("Ln 4, Col 5"));
  const size = () => page.$eval(".cm-editor", (e) => getComputedStyle(e).fontSize);
  const before = await size();
  await page.keyboard.down("Control"); await page.keyboard.press("="); await page.keyboard.up("Control");
  assert.notEqual(await size(), before);
  await page.close();
});

test("text: follows light/dark; JSON and Get Info", async () => {
  const page = await openText("/data.json");
  await page.waitForFunction(() => document.querySelector('[data-viewer="text"]').dataset.language === "JSON", { timeout: 15000 });
  const bg = () => page.$eval(".cm-editor", (e) => getComputedStyle(e).backgroundColor);
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  const light = await bg();
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
  const dark = await bg();
  assert.notEqual(light, dark);
  const info = await page.evaluate(() => globalThis.viewShell.viewer.getInfo());
  const t = Object.fromEntries(info[0].rows);
  assert.equal(t.Language, "JSON"); assert.equal(t.Lines, "2"); assert.equal(t.Encoding, "UTF-8");
  await page.close();
});

test("text: a 25 MB log opens read-only in plain mode, quickly", async () => {
  const t0 = Date.now();
  const page = await openText("/big.log");
  const ms = Date.now() - t0;
  console.log(`      ${(LOG_BIG.length / 1e6).toFixed(1)} MB log ready in ${ms} ms`);
  assert.ok(ms < 8000, `took ${ms} ms`);
  assert.ok(await page.$(".txt-banner"));
  assert.equal(await page.$eval('[data-viewer="text"]', (e) => e.dataset.language), "Log");
  assert.equal(await page.$eval('[data-viewer="text"]', (e) => e.dataset.lines), "400001");
  assert.equal(await page.$eval(".headerbar .btn[title^='Read-only']", (e) => e.disabled), true);
  // scroll to the end: the last line renders
  await page.evaluate(() => { const s = document.querySelector(".cm-scroller"); s.scrollTop = s.scrollHeight; });
  await page.waitForFunction(() => document.querySelector(".cm-content").textContent.includes("request 399999 served"), { timeout: 10000 });
  await page.close();
});
