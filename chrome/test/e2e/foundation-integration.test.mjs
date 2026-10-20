// Stream 0 integration: Viewer.onSaved (a cancelled save dialog leaves the
// document dirty; a real write makes it clean) for the table, text and image
// viewers, and TIFF decoding in the image viewer.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { FIXTURES, launch, startServer, waitForRules } from "./harness.mjs";

let ctx, server;

before(async () => {
  const tif = (name) => [{ "content-type": "image/tiff" }, readFileSync(join(FIXTURES, name))];
  server = await startServer({ "/sample.tif": tif("sample.tif"), "/sample-rot.tif": tif("sample-rot.tif") });
  ctx = await launch();
  await (await waitForRules(ctx.browser, ctx.base)).close();
});

after(async () => {
  await ctx?.browser.close();
  await server?.close();
});

/** Create `name` in the extension's OPFS with `bytes` and open it via viewer.html?handle=… */
async function openHandle(name, bytes, kind) {
  const page = await ctx.browser.newPage();
  await page.setViewport({ width: 1000, height: 700 });
  await page.goto(ctx.viewer);
  const id = await page.evaluate(async (name, bytes) => {
    const dir = await navigator.storage.getDirectory();
    const h = await dir.getFileHandle(name, { create: true });
    const w = await h.createWritable(); await w.write(new Uint8Array(bytes)); await w.close();
    const id = crypto.randomUUID();
    const db = await new Promise((r) => { const q = indexedDB.open("view", 1); q.onupgradeneeded = () => q.result.createObjectStore("handles"); q.onsuccess = () => r(q.result); });
    await new Promise((r) => { const t = db.transaction("handles", "readwrite"); t.objectStore("handles").put(h, `pending:${id}`); t.oncomplete = r; });
    return id;
  }, name, [...bytes]);
  await page.goto(`${ctx.viewer}?handle=${id}`);
  await page.waitForSelector(`[data-viewer="${kind}"]`, { timeout: 15000 });
  await page.waitForFunction(() => document.documentElement.dataset.state === "ready", { timeout: 15000 });
  return page;
}

const readOpfs = (page, name) => page.evaluate(async (n) => {
  try { return await (await (await (await navigator.storage.getDirectory()).getFileHandle(n)).getFile()).text(); } catch { return null; }
}, name);

const isDirty = (page) => page.evaluate(() => globalThis.viewShell.viewer.isDirty());
const titleDirty = async (page) => (await page.title()).startsWith("•");

/** Save As through the shell with the save dialog cancelled, then with it answered. */
async function cancelThenSave(page, pickedName) {
  // Cancel: the dialog rejects with AbortError, nothing is written.
  await page.evaluate(() => {
    globalThis.showSaveFilePicker = async () => { throw new DOMException("The user aborted a request.", "AbortError"); };
  });
  await page.evaluate(() => globalThis.viewShell.ctx.requestSave({ mode: "save-as" }));
  assert.equal(await isDirty(page), true, "cancelled save keeps the document dirty");
  assert.ok(await titleDirty(page));
  assert.equal(await readOpfs(page, pickedName), null);
  // Save: the dialog hands out an OPFS file, the shell writes it, onSaved cleans up.
  await page.evaluate((n) => {
    globalThis.showSaveFilePicker = async () => (await navigator.storage.getDirectory()).getFileHandle(n, { create: true });
  }, pickedName);
  await page.evaluate(() => globalThis.viewShell.ctx.requestSave({ mode: "save-as" }));
  await page.waitForFunction(() => !globalThis.viewShell.viewer.isDirty(), { timeout: 10000 });
  assert.ok(!(await titleDirty(page)));
}

test("text: cancel keeps it dirty, save makes it clean", async () => {
  const page = await openHandle("notes.txt", Buffer.from("hello\n"), "text");
  await page.evaluate(() => [...document.querySelectorAll(".headerbar .btn")].find((b) => b.textContent.trim() === "Edit")?.click());
  await page.click(".cm-content");
  await page.keyboard.type("more ");
  await page.waitForFunction(() => document.querySelector('[data-viewer="text"]').dataset.dirty === "true");
  await cancelThenSave(page, "notes-copy.txt");
  assert.match(await readOpfs(page, "notes-copy.txt"), /^(more hello\n|hello\nmore )$/);
  assert.equal(await page.$eval('[data-viewer="text"]', (e) => e.dataset.dirty), "false");
  await page.close();
});

test("table: cancel keeps it dirty, save makes it clean", async () => {
  const page = await openHandle("data.csv", Buffer.from("name,value\nalpha,1\nbeta,2\n"), "table");
  await page.waitForSelector('[data-viewer="table"][data-rows="2"]');
  await page.evaluate(async () => { const v = globalThis.viewShell.viewer; v.grid.setCursor(0, 1); await v.grid.insertCols(true); });
  await page.waitForFunction(() => document.querySelector('[data-viewer="table"]').dataset.dirty === "true");
  await cancelThenSave(page, "data-copy.csv");
  assert.equal(await readOpfs(page, "data-copy.csv"), "name,value,\nalpha,1,\nbeta,2,\n");
  assert.equal(await page.$eval('[data-viewer="table"]', (e) => e.dataset.dirty), "false");
  // Edits made after the snapshot that was saved keep the document dirty.
  await page.evaluate(async () => { const v = globalThis.viewShell.viewer; v.grid.setCursor(0, 0); await v.grid.insertCols(true); });
  await page.waitForFunction(() => document.querySelector('[data-viewer="table"]').dataset.dirty === "true");
  await page.close();
});

test("image: cancel keeps it dirty, save makes it clean", async () => {
  const png = readFileSync(join(FIXTURES, "sample.png"));
  const page = await openHandle("pic.png", png, "image");
  await page.waitForSelector('[data-viewer="image"][data-state="ready"][data-size="64x48"]');
  await page.click('.headerbar button[title^="Rotate Right"]');
  await page.waitForSelector('[data-viewer="image"][data-state="ready"][data-size="48x64"]');
  assert.equal(await isDirty(page), true);
  await cancelThenSave(page, "pic-copy.png");
  const size = await page.evaluate(async () => {
    const f = await (await (await navigator.storage.getDirectory()).getFileHandle("pic-copy.png")).getFile();
    const b = await createImageBitmap(f);
    return `${b.width}x${b.height}`;
  });
  assert.equal(size, "48x64");
  await page.close();
});

test("TIFF files open in the image viewer (Deflate; EXIF orientation applied)", async () => {
  const page = await ctx.browser.newPage();
  await page.goto(`${ctx.viewer}?kind=image&url=${server.origin}/sample.tif`);
  await page.waitForSelector('[data-viewer="image"][data-state="ready"]', { timeout: 15000 });
  assert.equal(await page.$eval('[data-viewer="image"]', (e) => e.dataset.size), "40x30");
  await page.goto(`${ctx.viewer}?kind=image&url=${server.origin}/sample-rot.tif`);
  await page.waitForSelector('[data-viewer="image"][data-state="ready"]', { timeout: 15000 });
  assert.equal(await page.$eval('[data-viewer="image"]', (e) => e.dataset.size), "30x40");
  await page.close();
});
