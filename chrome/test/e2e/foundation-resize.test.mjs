// Resizing the window must not raise "ResizeObserver loop completed with undelivered
// notifications" (Chrome lists it under the extension's Errors). It only shows with
// real scrollbars: fit-width toggling the scrollbar resized the PDF scroller again
// inside the observer callback.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { PDFDocument, StandardFonts } from "@cantoo/pdf-lib";
import { fixtureUrl, launch, waitForRules } from "./harness.mjs";

let ctx, dir;
before(async () => {
  dir = mkdtempSync(join(tmpdir(), "view-resize-"));
  const doc = await PDFDocument.create();
  const font = await doc.embedFont(StandardFonts.TimesRoman);
  for (let i = 1; i <= 12; i++) {
    const p = doc.addPage([612, 792]);
    p.drawText(`Paper page ${i}`, { x: 72, y: 720, size: 18, font });
  }
  writeFileSync(join(dir, "paper.pdf"), await doc.save());
  ctx = await launch({ realScrollbars: true });
  (await waitForRules(ctx.browser, ctx.base)).close();
});
after(async () => { await ctx?.browser.close(); rmSync(dir, { recursive: true, force: true }); });

const cases = () => [
  ["pdf", pathToFileURL(join(dir, "paper.pdf")).href],
  ["image", fixtureUrl("sample.png")],
  ["model", fixtureUrl("sample.stl")],
  ["table", fixtureUrl("sample.csv")],
  ["text", fixtureUrl("sample.txt")],
];

test("resizing any viewer raises no ResizeObserver loop error", async () => {
  for (const [kind, url] of cases()) {
    const page = await ctx.browser.newPage();
    await page.evaluateOnNewDocument(() => addEventListener("error", (e) => { (globalThis.__errs ??= []).push(e.message); }));
    await page.setViewport({ width: 1000, height: 800 });
    await page.goto(`${ctx.viewer}?kind=${kind}&url=${url}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".viewer-root, [data-view-kind], main", { timeout: 10000 }).catch(() => {});
    await new Promise((r) => setTimeout(r, 1500));
    for (let w = 1000; w >= 600; w -= 13) {
      await page.setViewport({ width: w, height: 800 - (w % 50) });
      await new Promise((r) => setTimeout(r, 25));
    }
    await new Promise((r) => setTimeout(r, 300));
    const errs = (await page.evaluate(() => globalThis.__errs ?? [])).filter((m) => /ResizeObserver/.test(m));
    assert.deepEqual(errs, [], `${kind}: ${errs.length} ResizeObserver errors`);
    await page.close();
  }
});
