// Stream 0: ViewerContext.requestSave goes through the same save path as Mod+S:
// in-place overwrite of the opened handle, the save dialog otherwise, and the
// export picker when an export request has no format.
// Files live in the extension's OPFS (real FileSystemFileHandles, no OS dialogs);
// showSaveFilePicker is stubbed to hand out OPFS handles.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { launch, waitForRules } from "./harness.mjs";

let ctx;

before(async () => {
  ctx = await launch();
  await (await waitForRules(ctx.browser, ctx.base)).close();
});

after(async () => {
  await ctx?.browser.close();
});

/** Open `name` (created in OPFS with `text`) through viewer.html?handle=… and instrument the viewer. */
async function openOpfsFile(name, text) {
  const page = await ctx.browser.newPage();
  await page.goto(ctx.viewer);
  const id = await page.evaluate(async (name, text) => {
    const dir = await navigator.storage.getDirectory();
    const h = await dir.getFileHandle(name, { create: true });
    const w = await h.createWritable(); await w.write(text); await w.close();
    const id = crypto.randomUUID();
    const db = await new Promise((r) => { const q = indexedDB.open("view", 1); q.onupgradeneeded = () => q.result.createObjectStore("handles"); q.onsuccess = () => r(q.result); });
    await new Promise((r) => { const t = db.transaction("handles", "readwrite"); t.objectStore("handles").put(h, `pending:${id}`); t.oncomplete = r; });
    return id;
  }, name, text);
  await page.goto(`${ctx.viewer}?handle=${id}`);
  await page.waitForFunction(() => document.documentElement.dataset.state === "ready", { timeout: 10000 });
  await page.evaluate(() => {
    const shell = globalThis.viewShell;
    const calls = (globalThis.saveCalls = []);
    // Whatever viewer handles .txt, make its save() deterministic for the test.
    shell.viewer.save = async (req) => {
      calls.push(req);
      const replacesOriginal = req.mode === "save";
      return { blob: new Blob([`saved:${req.mode}:${req.format ?? ""}`]), suggestedName: "out.bin", replacesOriginal };
    };
    shell.viewer.exportFormats = () => [{ id: "png", label: "PNG Image" }, { id: "jpg", label: "JPEG Image" }];
    // A dirty flag that only a real write clears (Viewer.onSaved).
    globalThis.fakeDirty = false;
    globalThis.savedInfos = [];
    shell.viewer.isDirty = () => globalThis.fakeDirty;
    shell.viewer.onSaved = (info) => {
      globalThis.savedInfos.push({ mode: info.mode, destination: info.destination, name: info.name, format: info.format });
      if (info.replacesOriginal) globalThis.fakeDirty = false;
    };
    globalThis.showSaveFilePicker = async (opts) => {
      globalThis.pickerOpts = opts;
      const dir = await navigator.storage.getDirectory();
      return dir.getFileHandle("picked.bin", { create: true });
    };
  });
  return page;
}

const readOpfs = (page, name) => page.evaluate(async (n) =>
  (await (await (await navigator.storage.getDirectory()).getFileHandle(n)).getFile()).text(), name);

test("requestSave({mode:'save'}) overwrites the opened file in place and clears dirty", async () => {
  const page = await openOpfsFile("orig.txt", "v1");
  await page.evaluate(() => { globalThis.fakeDirty = true; globalThis.viewShell.ctx.setDirty(true); });
  assert.ok((await page.title()).startsWith("•"));
  await page.evaluate(() => globalThis.viewShell.ctx.requestSave({ mode: "save" }));
  assert.equal(await readOpfs(page, "orig.txt"), "saved:save:");
  assert.equal(await page.evaluate(() => globalThis.pickerOpts), undefined, "no save dialog");
  assert.deepEqual(await page.evaluate(() => globalThis.savedInfos), [{ mode: "save", destination: "overwritten", name: "orig.txt" }]);
  assert.ok(!(await page.title()).startsWith("•"));
  await page.waitForFunction(() => [...document.querySelectorAll(".toast")].some((t) => t.textContent.includes("Saved")));
  await page.close();
});

test("requestSave({mode:'export', format}) skips the picker and uses the save dialog", async () => {
  const page = await openOpfsFile("a.txt", "v1");
  await page.evaluate(() => globalThis.viewShell.ctx.requestSave({ mode: "export", format: "jpg" }));
  assert.deepEqual(await page.evaluate(() => globalThis.saveCalls), [{ mode: "export", format: "jpg" }]);
  assert.equal(await page.evaluate(() => globalThis.pickerOpts?.suggestedName), "out.bin");
  assert.equal(await readOpfs(page, "picked.bin"), "saved:export:jpg");
  assert.equal(await readOpfs(page, "a.txt"), "v1", "original untouched");
  assert.deepEqual(await page.evaluate(() => globalThis.savedInfos), [{ mode: "export", destination: "saved-as", name: "picked.bin", format: "jpg" }]);
  assert.equal(await page.$("dialog.dialog"), null);
  await page.close();
});

test("requestSave({mode:'export'}) without format shows the export picker and honours the choice", async () => {
  const page = await openOpfsFile("b.txt", "v1");
  const done = page.evaluate(() => globalThis.viewShell.ctx.requestSave({ mode: "export", selectionOnly: true }));
  await page.waitForSelector("dialog.dialog[open] input[value=jpg]");
  await page.click("label[for=fmt-jpg]");
  await page.click('dialog.dialog .btn[data-value="ok"]');
  await done;
  assert.deepEqual(await page.evaluate(() => globalThis.saveCalls), [{ mode: "export", selectionOnly: true, format: "jpg" }]);
  assert.equal(await readOpfs(page, "picked.bin"), "saved:export:jpg");

  // Cancelling the picker saves nothing.
  const again = page.evaluate(() => globalThis.viewShell.ctx.requestSave({ mode: "export" }));
  await page.waitForSelector("dialog.dialog[open]");
  await page.keyboard.press("Escape");
  await again;
  assert.equal(await page.evaluate(() => globalThis.saveCalls.length), 1);
  await page.close();
});

test("requestSave({mode:'save-as'}) always asks where, even with a handle", async () => {
  const page = await openOpfsFile("c.txt", "v1");
  await page.evaluate(() => globalThis.viewShell.ctx.requestSave({ mode: "save-as" }));
  assert.equal(await readOpfs(page, "picked.bin"), "saved:save-as:");
  assert.equal(await readOpfs(page, "c.txt"), "v1");
  await page.close();
});
