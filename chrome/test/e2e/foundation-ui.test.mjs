// Stream 0: shell UI — Keyboard Shortcuts sheet, Recent list, theme preference.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { launch, open, startServer, waitForRules } from "./harness.mjs";

let ctx, server, optionsPage;

before(async () => {
  server = await startServer();
  ctx = await launch();
  optionsPage = await waitForRules(ctx.browser, ctx.base);
});

after(async () => {
  await ctx?.browser.close();
  await server?.close();
});

test("? opens the Keyboard Shortcuts sheet with shell and viewer groups; Escape closes it", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/doc.pdf`);
  await page.waitForSelector('[data-viewer="pdf"][data-rendered="true"]', { timeout: 15000 });
  await page.keyboard.type("?");
  await page.waitForSelector("dialog.dialog[open]");
  const groups = await page.$$eval(".shortcut-group h3", (hs) => hs.map((h) => h.textContent));
  assert.equal(groups[0], "General");
  assert.ok(groups.includes("PDF"), groups.join(","));
  const general = await page.$$eval(".shortcut-group:first-child .shortcut-row", (rows) =>
    rows.map((r) => r.textContent));
  assert.ok(general.some((t) => t.startsWith("Open") && t.includes("Ctrl+O")), general.join(" | "));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.dialog"));
  await page.close();
});

test("Recent: entries open, can be removed one by one, and stale ones drop out", async () => {
  const page = await ctx.browser.newPage();
  await page.goto(ctx.viewer);
  // Seed the Recent list with OPFS handles (real FileSystemFileHandles, no picker needed).
  await page.evaluate(async () => {
    const dir = await navigator.storage.getDirectory();
    const mk = async (name, text) => {
      const h = await dir.getFileHandle(name, { create: true });
      const w = await h.createWritable(); await w.write(text); await w.close();
      return h;
    };
    const a = await mk("notes.txt", "hello");
    const b = await mk("gone.csv", "a,b\n");
    const c = await mk("keep.stl", "solid x\nendsolid x\n");
    const db = await new Promise((r) => { const q = indexedDB.open("view", 1); q.onupgradeneeded = () => q.result.createObjectStore("handles"); q.onsuccess = () => r(q.result); });
    await new Promise((r) => {
      const t = db.transaction("handles", "readwrite");
      t.objectStore("handles").put([
        { name: "notes.txt", handle: a, opened: Date.now() - 60_000 },
        { name: "gone.csv", handle: b, opened: Date.now() - 7_200_000 },
        { name: "keep.stl", handle: c, opened: Date.now() - 86_400_000 * 3 },
      ], "recent");
      t.oncomplete = r;
    });
    await dir.removeEntry("gone.csv"); // file deleted behind View's back
  });
  await page.reload();
  await page.waitForSelector(".recent .recent-item");
  const names = () => page.$$eval(".recent-item .menu-item span:first-child", (s) => s.map((x) => x.textContent));
  assert.deepEqual(await names(), ["notes.txt", "gone.csv", "keep.stl"]);

  // Remove one entry with its × button.
  await page.click('.recent-item button[title="Remove “keep.stl” from Recent"]');
  await page.waitForFunction(() => document.querySelectorAll(".recent-item").length === 2);

  // A deleted file drops out with a toast.
  await page.click('.recent-item .menu-item[title="gone.csv"]');
  await page.waitForFunction(() => [...document.querySelectorAll(".toast")].some((t) => t.textContent.includes("moved or deleted")));
  assert.deepEqual(await names(), ["notes.txt"]);

  // Opening an entry routes it to its viewer.
  await page.click('.recent-item .menu-item[title="notes.txt"]');
  await page.waitForSelector('[data-viewer="text"]', { timeout: 10000 });
  await page.close();
});

test("the theme preference applies to the options page and the viewer", async () => {
  const page = await ctx.browser.newPage();
  await page.goto(ctx.viewer);
  await page.waitForSelector('[data-status="empty"]');
  await optionsPage.reload();
  await optionsPage.waitForSelector("#theme");
  await optionsPage.select("#theme", "dark");
  await optionsPage.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  assert.equal(bg, "rgb(34, 34, 38)");
  await optionsPage.select("#theme", "system");
  await page.waitForFunction(() => !("theme" in document.documentElement.dataset));
  await page.close();
});
