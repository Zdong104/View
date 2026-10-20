// Smoke test: View takes over PDFs (and other enabled kinds) opened from the web
// and from file://, renders them, and leaves everything else alone.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { fixtureUrl, launch, open, startServer, waitForRules } from "./harness.mjs";

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

const isViewer = (url, kind) => url.startsWith(`${ctx.viewer}?${kind ? `kind=${kind}&` : ""}url=`);

async function expectPdfRendered(frameOrPage, pages = 3) {
  await frameOrPage.waitForSelector(`[data-viewer="pdf"][data-pages="${pages}"][data-rendered="true"]`, { timeout: 15000 });
  const canvases = await frameOrPage.$$eval(".pdfViewer canvas", (cs) => cs.filter((c) => c.width > 0).length);
  assert.ok(canvases >= 1, "at least one page canvas painted");
}

test("http PDF with application/pdf opens in View", async () => {
  const page = await ctx.browser.newPage();
  const url = await open(page, `${server.origin}/doc.pdf`);
  assert.ok(isViewer(url, "pdf"), url);
  assert.ok(url.endsWith(`url=${server.origin}/doc.pdf`), url);
  await expectPdfRendered(page);
  await page.waitForFunction(() => document.querySelector(".subtitle")?.textContent === "Page 1 of 3");
  assert.equal(await page.title(), "doc.pdf");
  await page.close();
});

test("misleading URL but correct Content-Type opens in View", async () => {
  const page = await ctx.browser.newPage();
  const url = await open(page, `${server.origin}/download?id=42`);
  assert.ok(isViewer(url, "pdf"), url);
  assert.ok(url.endsWith("url=" + server.origin + "/download?id=42"), url);
  await expectPdfRendered(page);
  await page.close();
});

test("octet-stream + .pdf URL, and octet-stream + disposition filename, open in View", async () => {
  const page = await ctx.browser.newPage();
  assert.ok(isViewer(await open(page, `${server.origin}/octet/paper.pdf`), "pdf"));
  await expectPdfRendered(page);
  assert.ok(isViewer(await open(page, `${server.origin}/files/42`), "pdf"));
  await expectPdfRendered(page);
  await page.close();
});

test("PDF in an iframe is replaced by View inside the frame", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/frame.html`);
  assert.equal(page.url(), `${server.origin}/frame.html`);
  const deadline = Date.now() + 10000;
  let frame;
  while (!(frame = page.frames().find((f) => isViewer(f.url(), "pdf"))) && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 100));
  }
  assert.ok(frame, `frames: ${page.frames().map((f) => f.url()).join(", ")}`);
  await expectPdfRendered(frame);
  await page.close();
});

test("local PDF via file:// opens in View", async () => {
  const page = await ctx.browser.newPage();
  const target = fixtureUrl("sample.pdf");
  const url = await open(page, target);
  assert.ok(isViewer(url, "pdf"), url);
  // With "Allow access to file URLs" (default for --load-extension'ed extensions) it renders;
  // without, the viewer must explain how to grant access.
  const state = await page.waitForFunction(
    () => document.querySelector("[data-rendered=true]") ? "rendered"
      : document.querySelector('[data-status="file-access"]') ? "file-access" : null,
    { timeout: 15000 }).then((h) => h.jsonValue());
  const access = await page.evaluate(() => chrome.extension.isAllowedFileSchemeAccess());
  assert.equal(state, access ? "rendered" : "file-access");
  if (access) await expectPdfRendered(page);
  await page.close();
});

test("other kinds route to their real viewers, which parse the re-downloaded bytes", async () => {
  const page = await ctx.browser.newPage();
  // sample.stl: an ASCII tetrahedron (4 triangles, closed).
  assert.ok(isViewer(await open(page, `${server.origin}/part.stl`), "model"));
  await page.waitForSelector('[data-viewer="model"][data-triangles="4"][data-closed="true"]', { timeout: 15000 });
  // sample.csv: header + 3 rows, 2 columns (one row is Chinese text).
  assert.ok(isViewer(await open(page, `${server.origin}/data.csv`), "table"));
  await page.waitForSelector('[data-viewer="table"][data-rows="3"][data-cols="2"]', { timeout: 15000 });
  await page.close();
});

test("HTML pages and POSTed PDFs are left alone", async () => {
  const page = await ctx.browser.newPage();
  assert.equal(await open(page, `${server.origin}/page.html`), `${server.origin}/page.html`);
  await open(page, `${server.origin}/form.html`);
  await Promise.all([
    page.waitForNavigation({ waitUntil: "domcontentloaded" }).catch(() => {}),
    page.click("#go"),
  ]);
  assert.ok(!page.url().startsWith("chrome-extension://"), page.url());
  await page.close();
});

test("turning PDFs off in Preferences stops interception; all kinds on registers cleanly", async () => {
  const setPrefs = (p) => optionsPage.evaluate(async (prefs) => {
    await chrome.storage.sync.set({ prefs });
    return chrome.runtime.sendMessage({ type: "sync-rules" });
  }, p);
  const kinds = (on) => ({ pdf: on, image: on, model: on, table: on, text: on, media: on });

  const n = await setPrefs({ intercept: kinds(true), respectAttachment: true, theme: "system" });
  assert.equal(typeof n, "number", JSON.stringify(n));
  const registered = await optionsPage.evaluate(async () => (await chrome.declarativeNetRequest.getDynamicRules()).length);
  assert.equal(registered, n, "Chrome accepted every rule (regex limits)");

  await setPrefs({ intercept: { ...kinds(false), model: true }, respectAttachment: true, theme: "system" });
  const page = await ctx.browser.newPage();
  const url = await open(page, `${server.origin}/doc.pdf`);
  assert.ok(!url.startsWith("chrome-extension://"), url);
  await page.close();

  await optionsPage.evaluate(() => chrome.storage.sync.remove("prefs").then(() => chrome.runtime.sendMessage({ type: "sync-rules" })));
});

test("toolbar page: empty window offers Open", async () => {
  const page = await ctx.browser.newPage();
  await page.goto(ctx.viewer);
  await page.waitForSelector('[data-status="empty"] .btn.suggested');
  assert.equal(await page.title(), "View");
  await page.close();
});
