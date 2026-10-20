// Stream A: the PDF viewer. Every page operation is checked by saving and
// inspecting the output with pdf-lib (page count, order via distinct page
// widths, rotation, form values, encryption), plus search, thumbnails, print,
// export to images, reduce size, Live Text, and the 1,000-page check over a
// local http server with Range support.
// Fixtures are generated here (deterministic, no files on disk).
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { deflateSync } from "node:zlib";
import { PDFDocument, PDFHexString, PDFName, PDFRawStream, StandardFonts, rgb } from "@cantoo/pdf-lib";
import { launch, startServer, waitForRules } from "./harness.mjs";

let ctx, server;
const fx = {};

// ---- fixtures -------------------------------------------------------------------------

/** n pages; page i (1-based) is (400 + 10 i) × 600 pt, so order survives a save. */
async function pagesPdf(n, label = "doc") {
  const doc = await PDFDocument.create();
  doc.setTitle(`Stream A ${label}`);
  doc.setAuthor("View tests");
  const font = await doc.embedFont(StandardFonts.Helvetica);
  for (let i = 1; i <= n; i++) {
    const p = doc.addPage([400 + 10 * (i % 100), 600]);
    p.drawText(`${label} page ${i} of ${n}`, { x: 40, y: 540, size: 20, font });
    p.drawText(`The quick brown fox marker${i} jumps.`, { x: 40, y: 500, size: 12, font });
    p.drawRectangle({ x: 40, y: 200, width: 120, height: 80, color: rgb(0.2, 0.5, 0.9) });
  }
  return Buffer.from(await doc.save());
}

/** pagesPdf(5) plus an outline: Chapter 1 (p1) > Section 1.1 (p2), Chapter 2 (p4, /XYZ). */
async function tocPdf() {
  const doc = await PDFDocument.load(await pagesPdf(5));
  const ctx = doc.context;
  const pages = doc.getPages();
  const outlines = ctx.nextRef(), c1 = ctx.nextRef(), s11 = ctx.nextRef(), c2 = ctx.nextRef();
  const dest = (i, top) => ctx.obj([pages[i].ref, "XYZ", null, top ?? null, null]);
  ctx.assign(s11, ctx.obj({ Title: PDFHexString.fromText("Section 1.1"), Parent: c1, Dest: dest(1) }));
  ctx.assign(c1, ctx.obj({ Title: PDFHexString.fromText("Chapter 1"), Parent: outlines, Next: c2, First: s11, Last: s11, Count: 1, Dest: dest(0) }));
  ctx.assign(c2, ctx.obj({ Title: PDFHexString.fromText("Chapter 2"), Parent: outlines, Prev: c1, Dest: dest(3, 300) }));
  ctx.assign(outlines, ctx.obj({ Type: "Outlines", First: c1, Last: c2, Count: 3 }));
  doc.catalog.set(PDFName.of("Outlines"), outlines);
  return Buffer.from(await doc.save());
}

async function formPdf() {
  const doc = await PDFDocument.create();
  const page = doc.addPage([500, 400]);
  const form = doc.getForm();
  form.createTextField("name").addToPage(page, { x: 60, y: 300, width: 220, height: 26 });
  form.createCheckBox("agree").addToPage(page, { x: 60, y: 250, width: 18, height: 18 });
  const dd = form.createDropdown("color");
  dd.addOptions(["Red", "Green", "Blue"]);
  dd.addToPage(page, { x: 60, y: 190, width: 140, height: 24 });
  return Buffer.from(await doc.save());
}

function crc32(buf) {
  let c, crc = 0xffffffff;
  for (const b of buf) {
    c = (crc ^ b) & 0xff;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    crc = (crc >>> 8) ^ c;
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/** RGB PNG, gradient plus deterministic noise (compresses badly as PNG, well as JPEG). */
function png(w, h, noise = 0) {
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(type, "ascii"), data]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td));
    return Buffer.concat([len, td, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 2;
  const raw = Buffer.alloc((w * 3 + 1) * h);
  let seed = 7;
  for (let y = 0; y < h; y++) {
    raw[y * (w * 3 + 1)] = 0;
    for (let x = 0; x < w; x++) {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      const n = noise ? (seed % (2 * noise)) - noise : 0;
      const o = y * (w * 3 + 1) + 1 + x * 3;
      raw[o] = Math.max(0, Math.min(255, (x * 255) / w + n));
      raw[o + 1] = Math.max(0, Math.min(255, (y * 255) / h + n));
      raw[o + 2] = Math.max(0, Math.min(255, 160 + n));
    }
  }
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}

async function imagePdf() {
  const doc = await PDFDocument.create();
  const img = await doc.embedPng(png(1500, 1500, 40));
  const p = doc.addPage([595, 842]);
  p.drawImage(img, { x: 20, y: 100, width: 555, height: 555 });
  const p2 = doc.addPage([595, 842]);
  p2.drawImage(img, { x: 20, y: 100, width: 300, height: 300 });
  return Buffer.from(await doc.save());
}

async function encryptedPdf() {
  const doc = await PDFDocument.load(await pagesPdf(2, "secret"));
  doc.encrypt({ userPassword: "open-sesame", ownerPassword: "owner" });
  return Buffer.from(await doc.save());
}

/** Serve with HTTP Range support (pdf.js streams big files in 256 KB chunks). */
const ranged = (body) => (req, res) => {
  const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range ?? "");
  const head = { "content-type": "application/pdf", "accept-ranges": "bytes" };
  if (!m) { res.writeHead(200, { ...head, "content-length": body.length }).end(body); return; }
  const a = Number(m[1]), b = m[2] ? Math.min(Number(m[2]), body.length - 1) : body.length - 1;
  res.writeHead(206, { ...head, "content-range": `bytes ${a}-${b}/${body.length}`, "content-length": b - a + 1 }).end(body.subarray(a, b + 1));
};

before(async () => {
  fx.five = await pagesPdf(5);
  fx.three = await pagesPdf(3, "other");
  fx.form = await formPdf();
  fx.image = await imagePdf();
  fx.secret = await encryptedPdf();
  fx.big = await pagesPdf(1000, "big");
  fx.png = png(300, 200);
  fx.toc = await tocPdf();
  const pdf = (b) => [{ "content-type": "application/pdf" }, b];
  server = await startServer({
    "/five.pdf": pdf(fx.five), "/form.pdf": pdf(fx.form), "/image.pdf": pdf(fx.image),
    "/secret.pdf": pdf(fx.secret), "/toc.pdf": pdf(fx.toc),
    "/annotated.pdf": (req, res) => res.writeHead(200, { "content-type": "application/pdf" }).end(fx.annotated), "/big.pdf": ranged(fx.big), "/warm.pdf": ranged(fx.three),
  });
  ctx = await launch();
  await (await waitForRules(ctx.browser, ctx.base)).close();
});

after(async () => {
  await ctx?.browser.close();
  await server?.close();
});

// ---- helpers ----------------------------------------------------------------------------

async function openPdf(path, pages) {
  const page = await ctx.browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  await page.goto(`${server.origin}${path}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(`[data-viewer="pdf"][data-pages="${pages}"][data-rendered="true"]`, { timeout: 20000 });
  await page.waitForFunction(() => document.documentElement.dataset.state === "ready");
  await page.evaluate(() => {
    // Saves land in the extension's OPFS instead of an OS dialog.
    globalThis.__saves = [];
    globalThis.showSaveFilePicker = async (o) => {
      const dir = await navigator.storage.getDirectory();
      const name = `a-save-${crypto.randomUUID()}.pdf`;
      globalThis.__saves.push({ name, suggested: o?.suggestedName ?? "" });
      return dir.getFileHandle(name, { create: true });
    };
  });
  return page;
}

/** Wait for save number `n` (1-based) to be written, return {bytes, suggested}. */
async function savedFile(page, n) {
  await page.waitForFunction(async (n) => {
    const s = globalThis.__saves[n - 1];
    if (!s || document.querySelector('[data-viewer="pdf"][data-busy]')) return false;
    try {
      const f = await (await (await navigator.storage.getDirectory()).getFileHandle(s.name)).getFile();
      return f.size > 0;
    } catch { return false; }
  }, { timeout: 30000, polling: 100 }, n);
  await new Promise((r) => setTimeout(r, 150)); // let close() finish
  const { b64, suggested } = await page.evaluate(async (n) => {
    const s = globalThis.__saves[n - 1];
    const f = await (await (await navigator.storage.getDirectory()).getFileHandle(s.name)).getFile();
    const buf = new Uint8Array(await f.arrayBuffer());
    let bin = "";
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
    return { b64: btoa(bin), suggested: s.suggested };
  }, n);
  return { bytes: Buffer.from(b64, "base64"), suggested };
}

/** Original page numbers (1-based) of a saved pagesPdf, from the page widths. */
const order = (doc) => doc.getPages().map((p) => Math.round((Math.round(p.getSize().width) - 400) / 10));

const press = async (page, ...keys) => {
  for (const k of keys.slice(0, -1)) await page.keyboard.down(k);
  await page.keyboard.press(keys[keys.length - 1]);
  for (const k of keys.slice(0, -1).reverse()) await page.keyboard.up(k);
};

async function retype(page, sel, text) {
  await page.$eval(sel, (e) => { e.focus(); e.select(); });
  await page.keyboard.type(text);
}

/** Start an HTML5 drag on `sel` (synthetic DragEvent; Puppeteer can't drive native DnD headless). Returns the DataTransfer handle. */
async function htmlDrag(page, sel) {
  return page.evaluateHandle((sel) => {
    const dt = new DataTransfer();
    document.querySelector(sel).dispatchEvent(new DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer: dt }));
    return dt;
  }, sel);
}

const thumb = (pos) => `.pdf-thumb[data-pos="${pos}"]`;

async function clickThumb(page, pos, mod) {
  await page.waitForSelector(thumb(pos));
  if (mod) await page.keyboard.down(mod);
  await page.click(thumb(pos));
  if (mod) await page.keyboard.up(mod);
}

async function menu(page, label) {
  await page.click('[data-role="pdf-menu"]');
  await page.waitForSelector(".pdf-popup");
  const items = await page.$$(".pdf-popup .menu-item");
  for (const it of items) {
    if ((await it.evaluate((e) => e.firstElementChild.textContent)) === label) { await it.click(); return; }
  }
  throw new Error(`no menu item ${label}`);
}

/** Let the viewer's showOpenFilePicker return these files (staged in OPFS). */
async function stageFiles(page, files) {
  await page.evaluate(async (files) => {
    const dir = await navigator.storage.getDirectory();
    const handles = [];
    for (const f of files) {
      const h = await dir.getFileHandle(f.name, { create: true });
      const w = await h.createWritable();
      await w.write(Uint8Array.from(atob(f.b64), (c) => c.charCodeAt(0)));
      await w.close();
      handles.push(h);
    }
    globalThis.showOpenFilePicker = async () => handles;
  }, files.map((f) => ({ name: f.name, b64: f.bytes.toString("base64") })));
}

const pagesAttr = (page) => page.$eval('[data-viewer="pdf"]', (e) => Number(e.dataset.pages));

// ---- tests ---------------------------------------------------------------------------------

test("renders lazily with thumbnails, page entry, zoom modes and Get Info", async () => {
  const page = await openPdf("/five.pdf", 5);
  await page.waitForFunction(() => document.querySelector(".subtitle")?.textContent === "Page 1 of 5");
  assert.ok(await page.$eval(".sidebar", (e) => !e.hidden), "sidebar shown for multi-page documents");
  await page.waitForSelector(".pdf-thumb canvas");
  // text layer for selection
  await page.waitForSelector(".pdf-slot .textLayer span", { timeout: 10000 });
  // fit page then actual size (Ctrl+9, Ctrl+1)
  await page.click(".pdf-scroll");
  await press(page, "Control", "9");
  const fitPage = await page.$eval('[data-viewer="pdf"]', (e) => Number(e.dataset.zoom));
  await press(page, "Control", "1");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.zoom === "1");
  assert.ok(fitPage > 0 && fitPage !== 1);
  // go to page 4 from the page entry
  await retype(page, ".pdf-page-entry", "4");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".subtitle")?.textContent === "Page 4 of 5");
  const info = await page.evaluate(() => globalThis.viewShell.viewer.getInfo());
  const rows = Object.fromEntries(info[0].rows);
  assert.equal(rows.Pages, "5");
  assert.equal(rows.Title, "Stream A doc");
  assert.equal(rows.Author, "View tests");
  assert.match(rows["Page size"], /^440 × 600 pt/);
  assert.equal(rows.Security, "None");
  await page.close();
});

test("Ctrl+F finds text, highlights it and steps through matches", async () => {
  const page = await openPdf("/five.pdf", 5);
  await press(page, "Control", "f");
  await page.waitForSelector(".pdf-find-input");
  await page.keyboard.type("marker3");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.matches === "1");
  await page.waitForFunction(() => document.querySelector(".subtitle")?.textContent === "Page 3 of 5");
  await page.waitForSelector(".pdf-hl-box.current");
  await retype(page, ".pdf-find-input", "quick brown");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.matches === "5");
  await page.waitForFunction(() => /^\d of 5$/.test(document.querySelector(".pdf-find-label").textContent));
  const first = await page.$eval(".pdf-find-label", (e) => e.textContent);
  await page.keyboard.press("Enter");
  await page.waitForFunction((f) => document.querySelector(".pdf-find-label").textContent !== f, {}, first);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.querySelector(".pdf-findbar").hidden && !document.querySelector(".pdf-hl-box"));
  await page.close();
});

test("Delete removes the selected thumbnails; undo restores; save has the right pages", async () => {
  const page = await openPdf("/five.pdf", 5);
  await clickThumb(page, 1);
  await clickThumb(page, 3, "Control");
  await page.waitForFunction(() => document.querySelectorAll(".pdf-thumb.selected").length === 2);
  await page.keyboard.press("Delete");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.pages === "3");
  assert.ok(await page.$eval(".headerbar .title", (e) => e.classList.contains("dirty-dot")), "dirty after delete");
  await press(page, "Control", "z");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.pages === "5");
  await press(page, "Control", "Shift", "Z");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.pages === "3");
  await page.click(".pdf-scroll");
  await press(page, "Control", "s");
  const { bytes, suggested } = await savedFile(page, 1);
  const doc = await PDFDocument.load(bytes);
  assert.deepEqual(order(doc), [1, 3, 5]);
  assert.equal(await page.$eval('[data-viewer="pdf"]', (e) => e.dataset.savedVia), "worker", "built in the save worker");
  assert.equal(suggested, "five-edited.pdf");
  assert.equal(doc.getTitle(), "Stream A doc", "metadata survives");
  // the deleted pages' content is gone from the file
  assert.ok(!bytes.includes(Buffer.from("marker2")) && !bytes.includes(Buffer.from("doc page 2")), "no orphaned page content");
  await page.waitForFunction(() => !document.querySelector(".headerbar .title").classList.contains("dirty-dot"));
  await page.close();
});

test("drag thumbnails to reorder; rotate; Save As writes order and rotation", async () => {
  const page = await openPdf("/five.pdf", 5);
  await page.waitForSelector(thumb(2));
  const dt = await htmlDrag(page, thumb(0));
  await page.evaluate((dt, sel) => {
    const r = document.querySelector(sel).getBoundingClientRect();
    const list = document.querySelector(".pdf-thumbs-list");
    const at = { bubbles: true, cancelable: true, clientX: r.x + r.width / 2, clientY: r.y + r.height * 0.8, dataTransfer: dt };
    list.dispatchEvent(new DragEvent("dragover", at));
    list.dispatchEvent(new DragEvent("drop", at));
  }, dt, thumb(2));
  await page.waitForFunction(() => document.querySelector('.pdf-thumb[data-pos="0"] .pdf-thumb-label')?.textContent === "1"
    && globalThis.viewShell.viewer.model.pages[2].index === 0);
  // rotate page at position 1 (originally page 3) right twice and position 0 left once
  await clickThumb(page, 1);
  await press(page, "Control", "]");
  await press(page, "Control", "]");
  await clickThumb(page, 0);
  await press(page, "Control", "[");
  await press(page, "Control", "Shift", "S");
  const { bytes } = await savedFile(page, 1);
  const doc = await PDFDocument.load(bytes);
  assert.deepEqual(order(doc), [2, 3, 1, 4, 5]);
  assert.deepEqual(doc.getPages().map((p) => p.getRotation().angle), [270, 180, 0, 0, 0]);
  await page.close();
});

test("Ctrl+S with pages selected saves only those as a new PDF", async () => {
  const page = await openPdf("/five.pdf", 5);
  await clickThumb(page, 1);
  await clickThumb(page, 3, "Shift");
  await page.waitForFunction(() => document.querySelectorAll(".pdf-thumb.selected").length === 3);
  assert.match(await page.$eval(".pdf-thumbs-footer", (e) => e.textContent), /3 pages selected/);
  await press(page, "Control", "s");
  const { bytes, suggested } = await savedFile(page, 1);
  assert.deepEqual(order(await PDFDocument.load(bytes)), [2, 3, 4]);
  assert.equal(suggested, "five-pages-2-4.pdf");
  // the document itself is unchanged
  assert.equal(await pagesAttr(page), 5);
  await page.close();
});

test("insert blank page, PDF pages and an image; merge PDFs; save", async () => {
  const page = await openPdf("/five.pdf", 5);
  await clickThumb(page, 0);
  await menu(page, "Insert Blank Page");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.pages === "6");
  await stageFiles(page, [{ name: "other.pdf", bytes: fx.three }, { name: "pic.png", bytes: fx.png }]);
  await menu(page, "Insert Pages from File…");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.pages === "10");
  await stageFiles(page, [{ name: "m1.pdf", bytes: fx.three }, { name: "m2.pdf", bytes: await pagesPdf(2, "m2") }]);
  await menu(page, "Merge PDFs into This Document…");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.pages === "15");
  await press(page, "Control", "s");
  const { bytes } = await savedFile(page, 1);
  const doc = await PDFDocument.load(bytes);
  assert.equal(doc.getPageCount(), 15);
  const sizes = doc.getPages().map((p) => p.getSize()).map(({ width, height }) => `${Math.round(width)}x${Math.round(height)}`);
  // page 1 (410), blank (410 like page 1), other 1-3 (410,420,430), image, then 2..5, merged 3 + 2
  assert.equal(sizes[0], "410x600");
  assert.equal(sizes[1], "410x600");
  assert.deepEqual(sizes.slice(2, 5), ["410x600", "420x600", "430x600"]);
  assert.equal(sizes[5], "225x150", "300×200 px image at 96 dpi");
  assert.deepEqual(sizes.slice(6, 10), ["420x600", "430x600", "440x600", "450x600"]);
  assert.deepEqual(sizes.slice(10), ["410x600", "420x600", "430x600", "410x600", "420x600"]);
  await page.close();
});

test("AcroForm values are filled in and saved into the PDF", async () => {
  const page = await openPdf("/form.pdf", 1);
  await page.waitForSelector(".annotationLayer input[type=text]", { timeout: 10000 });
  assert.ok(!(await page.evaluate(() => globalThis.viewShell.viewer.isDirty())), "rendering forms does not dirty");
  await page.type(".annotationLayer input[type=text]", "Ada Lovelace");
  await page.click(".annotationLayer input[type=checkbox]");
  await page.select(".annotationLayer select", "Green");
  await page.waitForFunction(() => document.querySelector(".headerbar .title").classList.contains("dirty-dot"));
  await page.click(".pdf-scroll", { offset: { x: 5, y: 5 } });
  await press(page, "Control", "s");
  const { bytes } = await savedFile(page, 1);
  const form = (await PDFDocument.load(bytes)).getForm();
  assert.equal(form.getTextField("name").getText(), "Ada Lovelace");
  assert.equal(form.getCheckBox("agree").isChecked(), true);
  assert.deepEqual(form.getDropdown("color").getSelected(), ["Green"]);
  await page.close();
});

test("Export with Password writes an AES-256 encrypted PDF; encrypted PDFs ask for the password", async () => {
  const page = await openPdf("/five.pdf", 5);
  await menu(page, "Export with Password…");
  await page.waitForSelector('dialog [data-role="user-password"]');
  await page.type('dialog [data-role="user-password"]', "s3cret");
  await page.click('dialog input[data-perm="print"]');
  await page.click('dialog .dialog-actions [data-value="ok"]');
  const { bytes, suggested } = await savedFile(page, 1);
  assert.equal(suggested, "five-protected.pdf");
  const text = bytes.toString("latin1");
  assert.match(text, /\/Encrypt/);
  assert.match(text, /\/V 5/);
  assert.match(text, /\/R 6/);
  await assert.rejects(PDFDocument.load(bytes), /encrypt/i);
  const opened = await PDFDocument.load(bytes, { password: "s3cret" });
  assert.equal(opened.getPageCount(), 5);
  await page.close();

  // opening a protected PDF asks for its password (wrong first, then right)
  const p2 = await ctx.browser.newPage();
  await p2.goto(`${server.origin}/secret.pdf`, { waitUntil: "domcontentloaded" });
  await p2.waitForSelector('dialog [data-role="pdf-password"]', { timeout: 15000 });
  await p2.type('dialog [data-role="pdf-password"]', "nope");
  await p2.keyboard.press("Enter");
  await p2.waitForFunction(() => /Incorrect/.test(document.querySelector("dialog")?.textContent ?? ""), { timeout: 10000 });
  await p2.type('dialog [data-role="pdf-password"]', "open-sesame");
  await p2.keyboard.press("Enter");
  await p2.waitForSelector('[data-viewer="pdf"][data-pages="2"][data-rendered="true"]', { timeout: 15000 });
  const info = Object.fromEntries((await p2.evaluate(() => globalThis.viewShell.viewer.getInfo()))[0].rows);
  assert.match(info.Security, /^Encrypted \(password to open\)/);
  await p2.close();
});

test("Reduce File Size re-encodes large images", async () => {
  const page = await openPdf("/image.pdf", 2);
  await menu(page, "Reduce File Size…");
  const { bytes, suggested } = await savedFile(page, 1);
  assert.equal(suggested, "image-reduced.pdf");
  assert.ok(bytes.length < fx.image.length * 0.6, `${bytes.length} < ${fx.image.length}`);
  const doc = await PDFDocument.load(bytes);
  assert.equal(doc.getPageCount(), 2);
  const filters = doc.context.enumerateIndirectObjects()
    .filter(([, o]) => o instanceof PDFRawStream && o.dict.lookup(PDFName.of("Subtype")) === PDFName.of("Image"))
    .map(([, o]) => o.dict.lookup(PDFName.of("Filter"))?.toString());
  assert.deepEqual(filters, ["/DCTDecode"], "one shared image, now JPEG");
  await page.close();
});

test("Export as Images writes one PNG per page", async () => {
  const page = await openPdf("/five.pdf", 5);
  await page.evaluate(async () => {
    const root = await navigator.storage.getDirectory();
    const dir = await root.getDirectoryHandle(`a-export-${Date.now()}`, { create: true });
    globalThis.__exportDir = dir;
    globalThis.showDirectoryPicker = async () => dir;
  });
  await menu(page, "Export as Images…");
  await page.waitForSelector('dialog [data-role="dpi"]');
  await retype(page, 'dialog [data-role="dpi"]', "72");
  await page.click('dialog .dialog-actions [data-value="ok"]');
  await page.waitForFunction(async () => {
    const names = [];
    for await (const [n] of globalThis.__exportDir.entries()) names.push(n);
    return names.length === 5 && !document.querySelector("[data-busy]");
  }, { timeout: 20000, polling: 200 });
  const files = await page.evaluate(async () => {
    const out = [];
    for await (const [n, h] of globalThis.__exportDir.entries()) {
      const f = await h.getFile();
      const bmp = await createImageBitmap(f);
      const head = [...new Uint8Array(await f.slice(0, 4).arrayBuffer())];
      out.push({ n, w: bmp.width, h: bmp.height, head });
    }
    return out.sort((a, b) => a.n.localeCompare(b.n));
  });
  assert.deepEqual(files.map((f) => f.n), ["five-001.png", "five-002.png", "five-003.png", "five-004.png", "five-005.png"]);
  assert.deepEqual(files[0].head, [0x89, 0x50, 0x4e, 0x47]);
  assert.deepEqual([files[0].w, files[0].h], [410, 600], "72 dpi = 1 px per point");
  await page.close();
});

test("print renders every page into the print container", async () => {
  const page = await openPdf("/five.pdf", 5);
  await page.evaluate(() => {
    globalThis.print = () => { globalThis.__printed = document.querySelectorAll(".pdf-print img").length; };
  });
  await press(page, "Control", "p");
  await page.waitForFunction(() => globalThis.__printed === 5, { timeout: 20000 });
  await page.waitForFunction(() => !document.querySelector(".pdf-print"));
  await page.close();
});

test("markup surfaces are attached per rendered page", async () => {
  const page = await openPdf("/five.pdf", 5);
  await press(page, "Control", "Shift", "A");
  await page.waitForFunction(() => globalThis.viewShell.viewer.surfaces?.size > 0);
  const hosts = await page.evaluate(() => [...document.querySelectorAll(".pdf-slot")].map((s) => s.querySelector(".pdf-mk") !== null));
  assert.ok(hosts.every(Boolean));
  assert.ok(!(await page.$eval(".pdf-markupbar", (e) => e.hidden)), "markup toolbar shown");
  // markup is written into the saved PDF: shapes flattened, notes as real /Text annotations
  await page.evaluate(() => {
    const mk = globalThis.viewShell.viewer.mk;
    const style = { stroke: "#e01b24", fill: null, width: 2, opacity: 1 };
    mk.add({ type: "rect", surface: "p1", style, x: 50, y: 60, w: 120, h: 80 });
    mk.add({ type: "note", surface: "p1", style, x: 200, y: 100, text: "Check this" });
  });
  await page.waitForFunction(() => document.querySelector(".headerbar .title").classList.contains("dirty-dot"));
  await page.click(".pdf-scroll");
  await press(page, "Control", "s");
  const { bytes } = await savedFile(page, 1);
  const doc = await PDFDocument.load(bytes);
  const p2 = doc.getPage(1);
  const annots = p2.node.Annots()?.asArray().map((r) => doc.context.lookup(r).lookup(PDFName.of("Subtype"))?.toString()) ?? [];
  assert.deepEqual(annots, ["/Text"]);
  assert.ok(p2.node.Resources().lookup(PDFName.of("XObject"))?.keys().some((k) => /ViewMarkup/.test(k.toString())), "flattened shapes");
  assert.equal(doc.getPage(0).node.Annots()?.size() ?? 0, 0, "page 1 untouched");
  // undo goes through the shared history (markup first, then pages)
  await press(page, "Control", "z");
  await page.waitForFunction(() => globalThis.viewShell.viewer.mk.list().length === 1);
  await page.close();
});

test("Live Text: a scanned page is recognized, searchable and selectable", { timeout: 120000 }, async () => {
  const page = await openPdf("/five.pdf", 5);
  // A page that is only an image of text.
  await page.evaluate(async () => {
    const c = document.createElement("canvas");
    c.width = 1200; c.height = 400;
    const g = c.getContext("2d");
    g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
    g.fillStyle = "#000"; g.font = "bold 72px sans-serif";
    g.fillText("HELLO SCANNED WORLD", 60, 220);
    const blob = await new Promise((r) => c.toBlob(r, "image/png"));
    const v = globalThis.viewShell.viewer;
    await v.insertFiles([{ name: "scan.png", mime: "image/png", blob }], 0);
  });
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.pages === "6");
  const ok = await page.evaluate(async () => {
    const v = globalThis.viewShell.viewer;
    await v.recognize([v.model.pages[0]]);
    return v.ocrLines.get(`${v.model.pages[0].src}:0`)?.map((l) => l.text).join(" ") ?? "";
  });
  assert.match(ok.toUpperCase(), /HELLO/, `OCR text: ${ok}`);
  await page.waitForSelector(".pdf-slot .pdf-ocr svg", { timeout: 10000 });
  await press(page, "Control", "f");
  await page.keyboard.type("scanned");
  await page.waitForFunction(() => Number(document.querySelector('[data-viewer="pdf"]').dataset.matches) >= 1, { timeout: 10000 });
  await page.close();
});

test("dragging selected thumbnails out of the window carries a PDF of those pages", async () => {
  const page = await openPdf("/five.pdf", 5);
  await clickThumb(page, 1);
  await clickThumb(page, 2, "Shift");
  await page.waitForSelector('[data-viewer="pdf"][data-drag-out="ready"]', { timeout: 15000 });
  const dt = await htmlDrag(page, thumb(1));
  const url = await page.evaluate((dt) => dt.getData("DownloadURL"), dt);
  const m = /^application\/pdf:([^:]+):(blob:.+)$/.exec(url);
  assert.ok(m, url);
  assert.equal(m[1], "five-pages-2-3.pdf");
  const b64 = await page.evaluate(async (u) => {
    const buf = new Uint8Array(await (await fetch(u)).arrayBuffer());
    let bin = ""; for (const x of buf) bin += String.fromCharCode(x);
    return btoa(bin);
  }, m[2]);
  assert.deepEqual(order(await PDFDocument.load(Buffer.from(b64, "base64"))), [2, 3]);
  await page.evaluate((dt) => document.querySelector(".pdf-thumbs-list").dispatchEvent(new DragEvent("dragend", { bubbles: true, dataTransfer: dt })), dt);
  assert.equal(await pagesAttr(page), 5, "dragging out does not change the document");
  await page.close();
});

test("sidebar: Table of Contents jumps to sections; Highlights and Notes lists markup", async () => {
  const page = await openPdf("/toc.pdf", 5);
  await page.click('.pdf-side-tabs [data-panel="toc"]');
  await page.waitForSelector(".pdf-toc-row");
  const rows = await page.$$eval(".pdf-toc-row", (r) => r.map((e) => [e.querySelector(".pdf-toc-title").textContent, e.getAttribute("aria-level"), e.querySelector(".pdf-toc-page")?.textContent]));
  assert.deepEqual(rows, [["Chapter 1", "1", "1"], ["Section 1.1", "2", "2"], ["Chapter 2", "1", "4"]]);
  const rowsEls = await page.$$(".pdf-toc-row");
  await rowsEls[2].click();
  await page.waitForFunction(() => document.querySelector(".subtitle")?.textContent === "Page 4 of 5");
  await page.waitForFunction(() => document.querySelectorAll(".pdf-toc-row")[2].classList.contains("current"));
  // markup appears in Highlights and Notes
  await press(page, "Control", "Shift", "A");
  await page.waitForFunction(() => globalThis.viewShell.viewer.mk);
  await page.evaluate(() => globalThis.viewShell.viewer.mk.add({ type: "note", surface: "p2", style: { stroke: "#f6d32d", fill: null, width: 1, opacity: 1 }, x: 40, y: 40, text: "Look here" }));
  await page.click('.pdf-side-tabs [data-panel="annots"]');
  await page.waitForFunction(() => [...document.querySelectorAll(".pdf-annot-row")].some((r) => /Note · page 3/.test(r.textContent) && /Look here/.test(r.textContent)));
  await page.click(".pdf-annot-row[data-annot-id]");
  await page.waitForFunction(() => document.querySelector(".subtitle")?.textContent === "Page 3 of 5");
  await page.close();
});

test("existing markup is imported, editable, and saved without duplicates", async () => {
  // 1. make a PDF whose annotations were written by the markup layer (B's writePdfAnnotations)
  const maker = await openPdf("/five.pdf", 5);
  await press(maker, "Control", "Shift", "A");
  await maker.waitForFunction(() => globalThis.viewShell.viewer.mk);
  await maker.evaluate(() => {
    const mk = globalThis.viewShell.viewer.mk;
    mk.add({ type: "note", surface: "p0", style: { stroke: "#f6d32d", fill: null, width: 1, opacity: 1 }, x: 300, y: 80, text: "Original note" });
    mk.add({ type: "highlight", surface: "p0", style: { stroke: "#f6d32d", fill: null, width: 1, opacity: 0.4 }, quads: [40, 90, 240, 90, 240, 104, 40, 104], text: "quick" });
  });
  await maker.click(".pdf-scroll");
  await press(maker, "Control", "s");
  fx.annotated = (await savedFile(maker, 1)).bytes;
  await maker.close();
  const kinds = (doc) => doc.getPage(0).node.Annots()?.asArray().map((r) => doc.context.lookup(r).lookup(PDFName.of("Subtype"))?.toString()).sort() ?? [];
  assert.deepEqual(kinds(await PDFDocument.load(fx.annotated)), ["/Highlight", "/Text"]);

  // 2. open it: pdf.js draws them until the markup layer takes over
  const page = await openPdf("/annotated.pdf", 5);
  await page.waitForSelector(".annotationLayer .textAnnotation", { timeout: 10000 });
  await press(page, "Control", "Shift", "A");
  await page.waitForFunction(() => globalThis.viewShell.viewer.mk?.list().length === 2, { timeout: 10000 });
  // pdf.js's own HTML (note icon, pop-up) is hidden for imported annotations
  await page.waitForFunction(() => [...document.querySelectorAll(".annotationLayer .textAnnotation, .annotationLayer .popupAnnotation")]
    .every((e) => getComputedStyle(e).display === "none"));
  const note = await page.evaluate(() => {
    const mk = globalThis.viewShell.viewer.mk;
    const n = mk.list().find((a) => a.type === "note");
    mk.update(n.id, { text: "Edited note" });
    return n.id;
  });
  assert.ok(note);
  await page.click(".pdf-scroll");
  await press(page, "Control", "s");
  const { bytes } = await savedFile(page, 1);
  const doc = await PDFDocument.load(bytes);
  assert.deepEqual(kinds(doc), ["/Highlight", "/Text"], "each annotation once");
  const contents = doc.getPage(0).node.Annots().asArray().map((r) => doc.context.lookup(r).lookup(PDFName.of("Contents"))?.decodeText?.());
  assert.ok(contents.includes("Edited note") && !contents.includes("Original note"), JSON.stringify(contents));
  await page.close();
});

test("Searchable PDF keeps CJK text (GlyphLessFont, no installed font needed)", async () => {
  const page = await openPdf("/five.pdf", 5);
  // OCR results for page 2 (stand-in for the OCR service; its own test is stream F's)
  await page.evaluate(() => {
    const v = globalThis.viewShell.viewer;
    const ref = v.model.pages[1];
    v.ocrLines.set(`${ref.src}:${ref.index}`, [
      { text: "你好世界 Hello", box: [40, 300, 240, 300, 240, 324, 40, 324] },
      { text: "Ünïcödé 𠀀", box: [40, 340, 200, 340, 200, 360, 40, 360] },
    ]);
  });
  await menu(page, "Export as Searchable PDF (OCR)…");
  const { bytes, suggested } = await savedFile(page, 1);
  assert.equal(suggested, "five-searchable.pdf");
  const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
  const task = pdfjs.getDocument({ data: new Uint8Array(bytes), verbosity: 0 });
  const doc = await task.promise;
  const text = (await (await doc.getPage(2)).getTextContent()).items.map((i) => i.str).join(" ");
  assert.match(text, /你好世界 Hello/);
  assert.match(text, /Ünïcödé 𠀀/);
  await task.destroy();
  await page.close();
});

test("1,000-page PDF over http shows page 1 in under 1 s and stays virtualized", async () => {
  // warm up the extension page and pdf.js chunks once
  const warm = await openPdf("/warm.pdf", 3);
  await warm.close();
  const page = await ctx.browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  const t0 = Date.now();
  await page.goto(`${server.origin}/big.pdf`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-viewer="pdf"][data-pages="1000"][data-rendered="true"]', { timeout: 20000 });
  const dt = Date.now() - t0;
  const inPage = await page.evaluate(() => performance.now());
  console.log(`# 1000-page PDF: page 1 rendered ${dt} ms after navigation (viewer page alive ${Math.round(inPage)} ms)`);
  assert.ok(dt < 1000, `first page took ${dt} ms`);
  const slots = await page.$$eval(".pdf-slot", (s) => s.length);
  assert.ok(slots < 20, `only nearby pages are materialized (${slots})`);
  const rows = await page.$$eval(".pdf-thumb", (s) => s.length);
  assert.ok(rows > 0 && rows < 60, `thumbnails are virtualized (${rows})`);
  // jump to page 900
  await retype(page, ".pdf-page-entry", "900");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector('[data-viewer="pdf"]').dataset.current === "900");
  await page.waitForFunction(() => {
    const s = document.querySelector('.pdf-slot[data-pos="899"] canvas');
    return s && s.width > 0;
  }, { timeout: 10000 });
  assert.ok(await page.$$eval(".pdf-slot", (s) => s.length) < 20);
  await page.close();
});
