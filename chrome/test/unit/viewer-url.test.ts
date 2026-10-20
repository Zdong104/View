import assert from "node:assert/strict";
import { test } from "node:test";
import { dnrSubstitution, parseViewerUrl, viewerUrl } from "../../src/shared/viewer-url.ts";

const BASE = "chrome-extension://abc/viewer.html";

test("raw URL after url= survives untouched, including its own query", () => {
  const target = "https://ex.com/get?id=5&format=pdf&x=%20y";
  const p = parseViewerUrl(viewerUrl(BASE, target, "pdf"));
  assert.equal(p.url, target);
  assert.equal(p.kind, "pdf");
  assert.equal(p.hash, "");
});

test("fragment goes to hash", () => {
  const p = parseViewerUrl(viewerUrl(BASE, "file:///home/u/a%20b.pdf#page=3"));
  assert.equal(p.url, "file:///home/u/a%20b.pdf");
  assert.equal(p.hash, "#page=3");
  assert.equal(p.kind, undefined);
});

test("what DNR produces parses back", () => {
  const sub = dnrSubstitution(BASE, "image");
  const href = sub.replace("\\0", "http://h/i.png?a=1&url=2");
  const p = parseViewerUrl(href);
  assert.equal(p.kind, "image");
  assert.equal(p.url, "http://h/i.png?a=1&url=2");
});

test("handle and empty", () => {
  assert.deepEqual(parseViewerUrl(`${BASE}?handle=xyz`), { hash: "", handle: "xyz" });
  assert.deepEqual(parseViewerUrl(BASE), { hash: "" });
});

test("transfer URLs carry nonce and encoded name", async () => {
  const { transferViewerUrl } = await import("../../src/shared/viewer-url.ts");
  const p = parseViewerUrl(transferViewerUrl(BASE, "abc-123", "my file & co.pdf", "pdf"));
  assert.deepEqual(p, { hash: "", kind: "pdf", transfer: "abc-123", name: "my file & co.pdf" });
});

test("a fully percent-encoded url= is decoded once", () => {
  const p = parseViewerUrl(`${BASE}?kind=image&url=file%3A%2F%2F%2Fhome%2Fu%2Fa%2520b.png`);
  assert.equal(p.kind, "image");
  assert.equal(p.url, "file:///home/u/a%20b.png");
});
