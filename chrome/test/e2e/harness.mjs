// E2E harness: Chrome for Testing (downloaded by Puppeteer into .cache/puppeteer)
// with the unpacked extension from dist/, plus a tiny local HTTP server.
// Branded Google Chrome 137+ ignores --load-extension, so never point this at
// /usr/bin/google-chrome. Set HEADFUL=1 to watch.
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import puppeteer from "puppeteer";

const here = dirname(fileURLToPath(import.meta.url));
export const ROOT = join(here, "..", "..");
export const DIST = join(ROOT, process.env.VIEW_DIST || "dist");
export const FIXTURES = join(ROOT, "test", "fixtures");
export const fixtureUrl = (name) => pathToFileURL(join(FIXTURES, name)).href;
const fixture = (name) => readFileSync(join(FIXTURES, name));

/** Routes: path -> [headers, body]. */
function routes() {
  const pdf = fixture("sample.pdf");
  return {
    "/doc.pdf": [{ "content-type": "application/pdf" }, pdf],
    // Misleading URL (no .pdf), correct Content-Type
    "/download?id=42": [{ "content-type": "application/pdf; qs=0.001" }, pdf],
    // Generic type + .pdf in the URL
    "/octet/paper.pdf": [{ "content-type": "application/octet-stream" }, pdf],
    // Generic type, file name only in Content-Disposition
    "/files/42": [{ "content-type": "application/octet-stream", "content-disposition": 'inline; filename="report.pdf"' }, pdf],
    "/post.pdf": [{ "content-type": "application/pdf" }, pdf],
    "/part.stl": [{ "content-type": "application/octet-stream" }, fixture("sample.stl")],
    "/data.csv": [{ "content-type": "text/csv; charset=utf-8" }, fixture("sample.csv")],
    "/frame.html": [{ "content-type": "text/html" }, '<!doctype html><title>frame</title><iframe id="f" src="/doc.pdf" style="width:600px;height:400px"></iframe>'],
    "/form.html": [{ "content-type": "text/html" }, '<!doctype html><title>form</title><form method="post" action="/post.pdf"><button id="go">go</button></form>'],
    "/page.html": [{ "content-type": "text/html" }, "<!doctype html><title>plain</title><p>hello</p>"],
  };
}

/**
 * Start the fixture server. `extra` adds routes: path -> [headers, body] or a
 * handler (req, res, body) => void. Every request is logged in `requests`
 * ({method, url, headers}) so tests can assert on Referer/Cookie headers.
 */
export async function startServer(extra = {}) {
  const table = { ...routes(), ...extra };
  const requests = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      requests.push({ method: req.method, url: req.url, headers: req.headers });
      const hit = table[req.url ?? ""] ?? table[(req.url ?? "").split("?", 1)[0]];
      if (!hit) { res.writeHead(404).end("not found"); return; }
      if (typeof hit === "function") { hit(req, res, body); return; }
      const [headers, content] = hit;
      res.writeHead(200, { ...headers, "content-length": Buffer.byteLength(content) }).end(content);
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const { port } = server.address();
  return { origin: `http://127.0.0.1:${port}`, port, requests, close: () => new Promise((r) => server.close(r)) };
}

/** The sample PDF bytes, for routes in individual test files. */
export const samplePdf = () => fixture("sample.pdf");

/** Launch Chrome for Testing with the extension. `opts.args`/`opts.userDataDir` are passed through; see `realScrollbars` below. */
export async function launch(opts = {}) {
  const browser = await puppeteer.launch({
    headless: process.env.HEADFUL ? false : true,
    pipe: true,
    enableExtensions: [DIST],
    userDataDir: opts.userDataDir,
    // Puppeteer's headless mode hides scrollbars; `realScrollbars` keeps them, so
    // layout that depends on a scrollbar appearing behaves like in real Chrome.
    ignoreDefaultArgs: opts.realScrollbars ? ["--hide-scrollbars"] : undefined,
    // SwiftShader: reliable software WebGL in headless Chrome (3D viewer).
    args: ["--no-first-run", "--no-default-browser-check", "--enable-unsafe-swiftshader", ...(opts.args ?? [])],
  });
  const sw = await browser.waitForTarget(
    (t) => t.type() === "service_worker" && t.url().startsWith("chrome-extension://") && t.url().endsWith("/sw.js"),
    { timeout: 15000 },
  );
  const extId = new URL(sw.url()).host;
  const base = `chrome-extension://${extId}/`;
  return { browser, extId, base, viewer: `${base}viewer.html` };
}

/** Wait until the extension's dynamic rules exist (first run registers them asynchronously). */
export async function waitForRules(browser, base, min = 1) {
  const page = await browser.newPage();
  await page.goto(`${base}options.html`);
  await page.waitForFunction(async (n) => (await chrome.declarativeNetRequest.getDynamicRules()).length >= n, { timeout: 10000 }, min);
  return page;
}

/** Wait for a frame of `page` whose URL satisfies `pred` (polling; frames attach asynchronously). */
export async function waitForFrame(page, pred, timeout = 10000) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const f = page.frames().find((fr) => pred(fr.url()));
    if (f) return f;
    if (Date.now() > deadline) throw new Error(`no matching frame; frames: ${page.frames().map((fr) => fr.url()).join(", ")}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}

/** Navigate and return the final top-level URL (follows DNR redirects to the viewer). */
export async function open(page, url) {
  await page.goto(url, { waitUntil: "domcontentloaded" }).catch((e) => {
    // Navigations that turn into downloads abort; the caller checks page.url().
    if (!/ERR_ABORTED/.test(String(e))) throw e;
  });
  return page.url();
}
