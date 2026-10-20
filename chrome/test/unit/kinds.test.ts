import assert from "node:assert/strict";
import { test } from "node:test";
import { detectKind, kindFromMagic, kindFromMime, kindFromName, looksLikeText } from "../../src/shared/kinds.ts";

const bytes = (s: string) => new Uint8Array([...s].map((c) => c.charCodeAt(0)));

test("names route like preview/viewer_base.kind_of", () => {
  assert.equal(kindFromName("Report.PDF"), "pdf");
  assert.equal(kindFromName("https://x.org/a/b/photo.JPG?w=200#x"), "image");
  assert.equal(kindFromName("part.stl"), "model");
  assert.equal(kindFromName("data.tsv"), "table");
  assert.equal(kindFromName("clip.webm"), "media");
  assert.equal(kindFromName("main.rs"), "text");
  assert.equal(kindFromName("Makefile"), "text");
  assert.equal(kindFromName(".env.local"), "text");
  assert.equal(kindFromName("file%20name.csv"), "table");
  assert.equal(kindFromName("archive.zip"), null);
});

test("mime types", () => {
  assert.equal(kindFromMime("application/pdf; charset=binary"), "pdf");
  assert.equal(kindFromMime("image/svg+xml"), null);
  assert.equal(kindFromMime("image/x-whatever"), "image");
  assert.equal(kindFromMime("text/csv"), "table");
  assert.equal(kindFromMime("application/octet-stream"), null);
});

test("magic bytes", () => {
  assert.equal(kindFromMagic(bytes("%PDF-1.7\n")), "pdf");
  assert.equal(kindFromMagic(bytes("garbage\n%PDF-1.4")), "pdf");
  assert.equal(kindFromMagic(bytes("\x89PNG\r\n\x1a\n")), "image");
  assert.equal(kindFromMagic(bytes("RIFF\0\0\0\0WEBPVP8 ")), "image");
  assert.equal(kindFromMagic(bytes("ply\nformat ascii 1.0")), "model");
  assert.equal(kindFromMagic(bytes("\0\0\0\x18ftypisom")), "media");
});

test("detectKind priorities", () => {
  // misleading name, real PDF bytes
  assert.equal(detectKind({ name: "download", head: bytes("%PDF-1.4") }), "pdf");
  // text/plain served CSV: name wins over the generic text type
  assert.equal(detectKind({ name: "a.csv", mime: "text/plain", head: bytes("a,b\n1,2\n") }), "table");
  // unknown extension but UTF-8 text
  assert.equal(detectKind({ name: "NOTES", head: bytes("hello\n") }), "text");
  assert.equal(detectKind({ name: "blob.bin", head: new Uint8Array([0, 1, 2]) }), null);
  assert.equal(detectKind({ name: "x", hint: "model" }), "model");
});

test("text sniffing", () => {
  assert.ok(looksLikeText(new Uint8Array()));
  assert.ok(looksLikeText(new TextEncoder().encode("héllo 你好")));
  assert.ok(!looksLikeText(new Uint8Array([0x68, 0, 0x69])));
  const cut = new TextEncoder().encode("ab你").subarray(0, 4); // multibyte char cut at the end
  assert.ok(looksLikeText(cut));
});
