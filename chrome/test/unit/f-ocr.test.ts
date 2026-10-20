// Stream F: OCR post-processing (no model needed except the dictionary check).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { buildCharset, charBounds, ctcDecode } from "../../src/ocr/ctc.ts";
import { components, dbPostprocess, dilate2x2 } from "../../src/ocr/det-post.ts";
import { applyH, minAreaRect, orderCorners, perspectiveTransform, roundHalfEven, unclip, type Pt } from "../../src/ocr/geometry.ts";
import { cropQuad, resize, rot90, type Img } from "../../src/ocr/imgproc.ts";
import { linesToText, readingOrder } from "../../src/ocr/layout.ts";
import { onnxMetadata } from "../../src/ocr/onnx-meta.ts";
import type { OcrLine } from "../../src/ocr/api.ts";

const close = (a: number, b: number, eps = 1e-6) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b}`);

test("minAreaRect finds a rotated rectangle", () => {
  const a = Math.PI / 6, c = Math.cos(a), s = Math.sin(a);
  const pts: Pt[] = [];
  for (let x = 0; x <= 40; x += 2) for (let y = 0; y <= 10; y += 2) pts.push([100 + x * c - y * s, 50 + x * s + y * c]);
  const r = minAreaRect(pts);
  close(r.short, 10, 1e-6);
  const sides = r.corners.map((p, i) => Math.hypot(p[0] - r.corners[(i + 1) % 4]![0], p[1] - r.corners[(i + 1) % 4]![1]));
  close(Math.max(...sides), 40, 1e-6);
  assert.equal(minAreaRect([[1, 1], [5, 1], [9, 1]]).short, 0);
});

test("unclip grows a rectangle by area * ratio / perimeter on every side", () => {
  const box: Pt[] = [[10, 10], [110, 10], [110, 30], [10, 30]];
  const d = (100 * 20 * 1.6) / 240;
  const r = minAreaRect(unclip(box, 1.6));
  close(r.short, 20 + 2 * d, 1e-3);
});

test("roundHalfEven matches numpy.round", () => {
  assert.deepEqual([0.5, 1.5, 2.5, -0.5, 2.4, 2.6].map(roundHalfEven), [0, 2, 2, -0, 2, 3]);
});

test("dilate2x2 grows towards +x/+y like cv2.dilate", () => {
  const m = new Uint8Array(9);
  m[4] = 1;
  assert.deepEqual(Array.from(dilate2x2(m, 3, 3)), [0, 0, 0, 0, 1, 1, 0, 1, 1]);
});

test("8-connected components keep diagonal neighbours together", () => {
  const w = 4, m = new Uint8Array([1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0]);
  const cs = components(m, w, 4);
  assert.equal(cs.length, 2);
});

test("dbPostprocess: two text blobs -> two boxes, sorted, scaled to the destination", () => {
  const W = 200, H = 100;
  const pred = new Float32Array(W * H);
  const fill = (x0: number, y0: number, x1: number, y1: number) => {
    for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) pred[y * W + x] = 0.9;
  };
  fill(110, 20, 180, 35); // right, top row
  fill(10, 22, 90, 36); // left, same row (y within 10 px)
  fill(10, 60, 120, 80); // second row
  const { boxes, scores } = dbPostprocess(pred, W, H, 400, 200);
  assert.equal(boxes.length, 3);
  assert.ok(scores.every((s) => s > 0.7 && s <= 0.9), String(scores)); // the dilated border has p = 0
  assert.ok(boxes[0]![0][0] < 40 && boxes[1]![0][0] > 150 && boxes[2]![0][1] > 80, JSON.stringify(boxes));
  // Unclipped: wider than the blob (80 px * 2 = 160 px at destination scale).
  assert.ok(boxes[0]![1][0] - boxes[0]![0][0] > 160);
});

test("perspective transform maps the corners", () => {
  const src: Pt[] = [[0, 0], [10, 0], [10, 5], [0, 5]];
  const dst: Pt[] = [[3, 4], [20, 6], [19, 15], [2, 12]];
  const h = perspectiveTransform(src, dst);
  src.forEach((p, i) => {
    const q = applyH(h, p[0], p[1]);
    close(q[0], dst[i]![0], 1e-6);
    close(q[1], dst[i]![1], 1e-6);
  });
});

test("crop, resize and rot90 keep pixels where expected", () => {
  const img: Img = { w: 4, h: 2, data: new Uint8Array(24) };
  for (let i = 0; i < 8; i++) img.data.fill(i * 10, i * 3, i * 3 + 3);
  const r = rot90(img);
  assert.equal(r.w, 2);
  assert.equal(r.h, 4);
  assert.equal(r.data[0], 30); // top-left of rot90 = top-right of the source
  const c = cropQuad(img, [[0, 0], [4, 0], [4, 2], [0, 2]]);
  assert.equal(c.img.w, 4);
  assert.equal(c.img.data[3 * 5], 50);
  const s = resize(img, 2, 1);
  assert.equal(s.w * s.h * 3, s.data.length);
});

test("CTC decode drops repeats and blanks; char bounds are ordered", () => {
  const charset = buildCharset("a\nb");
  // classes: 0 blank, 1 a, 2 b, 3 " "
  const steps = [1, 1, 0, 1, 2, 2, 3, 0];
  const C = 4, T = steps.length;
  const p = new Float32Array(T * C);
  steps.forEach((k, t) => (p[t * C + k] = 0.9));
  const [d] = ctcDecode(p, 1, T, C, charset);
  assert.equal(d!.text, "aab ");
  assert.deepEqual(d!.spans, [[0, 2], [3, 4], [4, 6], [6, 7]]);
  const b = charBounds(d!.spans, 8, 64);
  for (let i = 0; i < b.length; i++) {
    assert.ok(b[i]![0] < b[i]![1]);
    if (i) close(b[i]![0], b[i - 1]![1]);
  }
});

test("recognizer dictionary comes from the ONNX metadata (18708 + blank + space = output classes)", () => {
  const model = new Uint8Array(readFileSync(join(process.cwd(), "src/ocr/models/PP-OCRv6_rec_small.onnx")));
  const ch = onnxMetadata(model)["character"];
  assert.ok(ch);
  assert.equal(buildCharset(ch).length, 18710);
});

const line = (text: string, x: number, y: number, w: number, h: number): OcrLine =>
  ({ text, score: 1, box: [x, y, x + w, y, x + w, y + h, x, y + h] });

test("reading order: rows, columns, paragraphs and CJK joining (preview/ocr.py)", () => {
  const lines = [line("world", 300, 12, 100, 20), line("hello", 10, 10, 100, 20), line("next", 10, 40, 80, 20),
    line("para", 10, 120, 80, 20), line("你好", 10, 200, 40, 20), line("世界", 60, 200, 40, 20)];
  assert.deepEqual(readingOrder(lines).map(([i]) => i), [1, 0, 2, 3, 4, 5]);
  assert.equal(linesToText(lines), "hello world\nnext\n\npara\n\n你好世界");
});

test("orderCorners starts at the top-left and runs clockwise", () => {
  assert.deepEqual(orderCorners([[10, 10], [0, 10], [0, 0], [10, 0]]), [[0, 0], [10, 0], [10, 10], [0, 10]]);
});
