// Stream 0: the content script (src/content) takes over PDFs that DNR never
// sees — <embed>/<object> and blob: documents — and leaves other plugins alone.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { launch, open, samplePdf, startServer, waitForFrame, waitForRules } from "./harness.mjs";

const html = (body) => [{ "content-type": "text/html" }, `<!doctype html><title>t</title>${body}`];
const blobScript = (use) => `<script>
fetch("/doc.pdf").then((r) => r.blob()).then((b) => {
  const url = URL.createObjectURL(new Blob([b], { type: "application/pdf" }));
  ${use}
});
</script>`;

let ctx, server, optionsPage;

before(async () => {
  server = await startServer({
    "/embed.html": html('<embed id="e" type="application/pdf" src="/doc.pdf" width="600" height="400">'),
    "/embed-noext.html": html('<embed id="e" src="doc.pdf#page=2" width="600" height="400">'),
    "/object.html": html('<object id="o" data="/doc.pdf" type="application/pdf" width="600" height="400">fallback</object>'),
    "/embed-blob.html": html(blobScript(`const e = document.createElement("embed");
      e.type = "application/pdf"; e.src = url; e.width = 600; e.height = 400; document.body.append(e);`)),
    "/iframe-blob.html": html(blobScript(`const f = document.createElement("iframe");
      f.src = url; f.width = 600; f.height = 400; document.body.append(f);`)),
    "/blob-top.html": html(blobScript("location.href = url;")),
    "/iframe-data.html": html(`<iframe width="600" height="400" src="data:application/pdf;base64,${samplePdf().toString("base64")}"></iframe>`),
    "/embed-swf.html": html('<embed id="e" type="application/x-shockwave-flash" src="/movie.swf" width="10" height="10">'),
  });
  ctx = await launch();
  optionsPage = await waitForRules(ctx.browser, ctx.base);
});

after(async () => {
  await ctx?.browser.close();
  await server?.close();
});

const viewerFrame = (page) => waitForFrame(page, (u) => u.startsWith(`${ctx.viewer}?kind=pdf&`), 15000);

async function expectRendered(frame) {
  await frame.waitForSelector('[data-viewer="pdf"][data-pages="3"][data-rendered="true"]', { timeout: 15000 });
}

test("<embed type=application/pdf> shows View", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/embed.html`);
  const frame = await viewerFrame(page);
  assert.ok(frame.url().endsWith(`url=${server.origin}/doc.pdf`), frame.url());
  await expectRendered(frame);
  assert.equal(await page.$eval("#e", (e) => e.type), "text/html", "embed kept, retargeted");
  await page.close();
});

test("<embed> without type but .pdf src (relative, with #page) shows View", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/embed-noext.html`);
  const frame = await viewerFrame(page);
  assert.ok(frame.url().endsWith(`url=${server.origin}/doc.pdf#page=2`), frame.url());
  await expectRendered(frame);
  await page.close();
});

test("<object data=… type=application/pdf> shows View", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/object.html`);
  const frame = await viewerFrame(page);
  await expectRendered(frame);
  await page.close();
});

test("blob: PDF in an <embed> is handed over by postMessage", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/embed-blob.html`);
  const frame = await viewerFrame(page);
  assert.match(frame.url(), /[?&]transfer=[0-9a-f-]{36}&name=document\.pdf$/);
  await expectRendered(frame);
  await page.close();
});

test("blob: PDF document in an iframe is covered by View", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/iframe-blob.html`);
  await expectRendered(await viewerFrame(page));
  await page.close();
});

test("data: PDF document in an iframe is covered by View", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/iframe-data.html`);
  await expectRendered(await viewerFrame(page));
  await page.close();
});

test("top-level blob: PDF document is covered by View", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/blob-top.html`);
  await page.waitForFunction(() => location.protocol === "blob:", { timeout: 10000 });
  await expectRendered(await viewerFrame(page));
  await page.close();
});

test("non-PDF plugins and PDFs with View's PDF setting off are left alone", async () => {
  const page = await ctx.browser.newPage();
  await open(page, `${server.origin}/embed-swf.html`);
  await new Promise((r) => setTimeout(r, 500));
  assert.equal(await page.$eval("#e", (e) => e.type), "application/x-shockwave-flash");

  await optionsPage.evaluate(() => chrome.storage.sync.set({ prefs: { intercept: { pdf: false } } }));
  await open(page, `${server.origin}/embed.html`);
  await new Promise((r) => setTimeout(r, 800));
  assert.equal(await page.$eval("#e", (e) => e.type), "application/pdf");
  assert.ok(!page.frames().some((f) => f.url().startsWith(ctx.viewer)));
  await optionsPage.evaluate(() => chrome.storage.sync.remove("prefs"));
  await page.close();
});
