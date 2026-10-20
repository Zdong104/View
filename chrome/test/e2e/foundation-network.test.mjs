// Stream 0: how the viewer re-downloads intercepted documents (Referer,
// cookies, third-party-cookie blocking) and the one-shot "Open in Chrome" bypass.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { launch, open, samplePdf, startServer, waitForRules } from "./harness.mjs";

const pdf = samplePdf();
const sendPdf = (res) => res.writeHead(200, { "content-type": "application/pdf", "content-length": pdf.length }).end(pdf);

let server, ctx, optionsPage;
const tmpDirs = [];

before(async () => {
  server = await startServer({
    "/link.html": [{ "content-type": "text/html" }, '<!doctype html><a id="a" href="/guarded.pdf">pdf</a>'],
    // Hot-link protection: only serves the PDF to requests that come from link.html.
    "/guarded.pdf": (req, res) => {
      if (req.headers.referer === `${server.origin}/link.html`) sendPdf(res);
      else res.writeHead(403, { "content-type": "text/plain" }).end("forbidden");
    },
    "/login": (req, res) => res.writeHead(200, {
      "content-type": "text/html",
      "set-cookie": ["plain=1; Path=/", "lax=1; SameSite=Lax; Path=/", "strict=1; SameSite=Strict; Path=/"],
    }).end("logged in"),
    "/private.pdf": (req, res) => {
      if (/\bstrict=1\b/.test(req.headers.cookie ?? "")) sendPdf(res);
      else res.writeHead(401, { "content-type": "text/plain" }).end("login required");
    },
    "/echo": (req, res) => res.writeHead(200, { "content-type": "text/plain" }).end("echo"),
  });
  ctx = await launch();
  optionsPage = await waitForRules(ctx.browser, ctx.base);
});

after(async () => {
  await ctx?.browser.close();
  await server?.close();
  for (const d of tmpDirs) rmSync(d, { recursive: true, force: true });
});

const rendered = (page) => page.waitForSelector('[data-viewer="pdf"][data-rendered="true"]', { timeout: 15000 });
const viewerRequests = (path) => server.requests.filter((r) => r.url === path && r.headers["sec-fetch-dest"] !== "document");

test("the viewer's re-download carries the Referer of the intercepted navigation", async () => {
  server.requests.length = 0;
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/link.html`);
  await Promise.all([page.waitForNavigation(), page.click("#a")]);
  assert.ok(page.url().startsWith(`${ctx.viewer}?kind=pdf&url=`), page.url());
  await rendered(page);
  const reqs = viewerRequests("/guarded.pdf");
  assert.ok(reqs.length >= 1);
  for (const r of reqs) assert.equal(r.headers.referer, `${server.origin}/link.html`);
  // The rule is scoped to the tab and goes away with it.
  const tabRules = () => optionsPage.evaluate(async () =>
    (await chrome.declarativeNetRequest.getSessionRules()).filter((r) => r.id >= 2_000_000).length);
  assert.ok((await tabRules()) >= 1);
  await page.close();
  await optionsPage.waitForFunction(async () =>
    (await chrome.declarativeNetRequest.getSessionRules()).every((r) => r.id < 2_000_000), { timeout: 5000 });
});

test("without an intercepted navigation no Referer is invented (control)", async () => {
  server.requests.length = 0;
  const page = await ctx.browser.newPage();
  await page.goto(`${ctx.viewer}?kind=pdf&url=${server.origin}/guarded.pdf`);
  await page.waitForSelector('[data-status="error"]', { timeout: 15000 });
  assert.ok(viewerRequests("/guarded.pdf").every((r) => r.headers.referer !== `${server.origin}/link.html`));
  await page.close();
});

test("cookies (including SameSite=Strict) reach the re-download", async () => {
  server.requests.length = 0;
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/login`);
  await open(page, `${server.origin}/private.pdf`);
  await rendered(page);
  for (const r of viewerRequests("/private.pdf")) assert.match(r.headers.cookie, /plain=1; lax=1; strict=1/);
  await page.close();
});

test("…also with third-party cookies blocked in Chrome settings", async () => {
  const dir = mkdtempSync(join(tmpdir(), "view-3pc-"));
  tmpDirs.push(dir);
  mkdirSync(join(dir, "Default"), { recursive: true });
  writeFileSync(join(dir, "Default", "Preferences"),
    JSON.stringify({ profile: { cookie_controls_mode: 1, block_third_party_cookies: true } }));
  const blocked = await launch({ userDataDir: dir });
  try {
    await waitForRules(blocked.browser, blocked.base);
    const page = await blocked.browser.newPage();
    // Prove the setting is active: a cross-site iframe gets no cookie.
    const other = `http://localhost:${server.port}`;
    await page.goto(`${other}/login`);
    await page.goto(`${server.origin}/echo`);
    server.requests.length = 0;
    await page.evaluate((u) => new Promise((r) => {
      const f = document.createElement("iframe"); f.onload = r; f.src = u; document.body.append(f);
    }), `${other}/echo`);
    const thirdParty = server.requests.find((r) => r.url === "/echo" && r.headers.host.startsWith("localhost"));
    assert.equal(thirdParty?.headers.cookie, undefined, "third-party cookie blocking is active");
    // The extension's re-download is first-party for cookie purposes.
    server.requests.length = 0;
    await open(page, `${other}/private.pdf`);
    await rendered(page);
    for (const r of viewerRequests("/private.pdf")) assert.match(r.headers.cookie, /strict=1/);
  } finally {
    await blocked.browser.close();
  }
});

test("Open in Chrome bypasses View once; the rule is gone after the navigation commits", async () => {
  const page = await ctx.browser.newPage();
  const target = `${server.origin}/doc.pdf`;
  assert.ok((await open(page, target)).startsWith(ctx.viewer));
  await rendered(page);
  await Promise.all([
    page.waitForNavigation({ waitUntil: "domcontentloaded" }),
    page.evaluate((url) => chrome.runtime.sendMessage({ type: "open-original", url }), target),
  ]);
  assert.equal(page.url(), target, "Chrome's own handling, not View");
  await optionsPage.waitForFunction(async () =>
    (await chrome.declarativeNetRequest.getSessionRules()).every((r) => r.id < 1_000_000 || r.id >= 2_000_000),
  { timeout: 5000 });
  // A reload goes back to View.
  await page.reload({ waitUntil: "domcontentloaded" }).catch(() => {});
  await page.waitForFunction((v) => location.href.startsWith(v), { timeout: 10000 }, ctx.viewer);
  await page.close();
});
