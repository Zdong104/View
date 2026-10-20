// Stream C: image viewer, edits and batch tools. Fixtures are generated here (gifenc
// and UPNG in Node, canvas encoders in the browser, a hand-built EXIF block) and
// served by a local server; edited outputs are checked by decoding them again
// (createImageBitmap / WebCodecs ImageDecoder in the page, exifr and pdf-lib in Node).
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { after, before, test } from "node:test";
import gifenc from "gifenc";
import UPNG from "upng-js";
import exifr from "exifr";
import { PDFDocument } from "@cantoo/pdf-lib";
import { launch, waitForRules } from "./harness.mjs";

let ctx, server, origin;
const files = new Map(); // path -> [content-type, Buffer]

// ---- fixtures ----------------------------------------------------------------------

const W = 40, H = 30;
const DELAYS = [100, 200, 300];
/** Frame i: solid colour i (red, green, blue) with a white top-left 8×8 block. */
function frameRgba(i) {
  const px = new Uint8Array(W * H * 4);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const o = (y * W + x) * 4;
    const white = x < 8 && y < 8;
    px[o] = white || i === 0 ? 255 : 0; px[o + 1] = white || i === 1 ? 255 : 0; px[o + 2] = white || i === 2 ? 255 : 0; px[o + 3] = 255;
  }
  return px;
}

function makeGif() {
  const enc = gifenc.GIFEncoder();
  DELAYS.forEach((d, i) => {
    const px = frameRgba(i);
    const pal = gifenc.quantize(px, 16);
    enc.writeFrame(gifenc.applyPalette(px, pal), W, H, { palette: pal, delay: d, repeat: 0 });
  });
  enc.finish();
  return Buffer.from(enc.bytes());
}

const makeApng = () => Buffer.from(UPNG.encode(DELAYS.map((_, i) => frameRgba(i).buffer), W, H, 0, DELAYS));

/** Little-endian TIFF/EXIF block: IFD0 (Make, Model, Orientation 6) + Exif IFD + GPS IFD. */
function makeExif() {
  const ifds = [
    [[0x010f, 2, "ViewTest\0"], [0x0110, 2, "Model X\0"], [0x0112, 3, [6]], [0x8769, 4, "EXIF"], [0x8825, 4, "GPS"]],
    [[0x829a, 5, [[1, 125]]], [0x829d, 5, [[28, 10]]], [0x8827, 3, [200]], [0x9003, 2, "2024:05:01 12:00:00\0"],
      [0x920a, 5, [[50, 1]]], [0xa002, 4, [60]], [0xa003, 4, [40]]],
    [[1, 2, "N\0"], [2, 5, [[48, 1], [51, 1], [2960, 100]]], [3, 2, "E\0"], [4, 5, [[2, 1], [17, 1], [4020, 100]]], [6, 5, [[35, 1]]]],
  ];
  const size = (t, v) => (t === 2 ? v.length : t === 3 ? 2 * v.length : t === 4 ? 4 : 8 * v.length);
  // layout: header 8, then each IFD followed by its overflow data
  const offs = [];
  let o = 8;
  for (const ifd of ifds) {
    offs.push(o);
    o += 2 + ifd.length * 12 + 4;
    for (const [, t, v] of ifd) if (size(t, v) > 4) o += size(t, v);
  }
  const b = Buffer.alloc(o);
  b.write("II", 0); b.writeUInt16LE(42, 2); b.writeUInt32LE(8, 4);
  ifds.forEach((ifd, k) => {
    let p = offs[k];
    let data = p + 2 + ifd.length * 12 + 4;
    b.writeUInt16LE(ifd.length, p); p += 2;
    for (const [tag, t, v0] of ifd) {
      const v = v0 === "EXIF" ? [offs[1]] : v0 === "GPS" ? [offs[2]] : v0;
      const n = t === 2 ? v.length : v.length;
      b.writeUInt16LE(tag, p); b.writeUInt16LE(t, p + 2); b.writeUInt32LE(n, p + 4);
      const sz = size(t, v);
      let at = p + 8;
      if (sz > 4) { b.writeUInt32LE(data, p + 8); at = data; data += sz; }
      if (t === 2) b.write(v, at, "latin1");
      else if (t === 3) v.forEach((x, i) => b.writeUInt16LE(x, at + i * 2));
      else if (t === 4) b.writeUInt32LE(v[0], at);
      else v.forEach(([nu, de], i) => { b.writeUInt32LE(nu, at + i * 8); b.writeUInt32LE(de, at + i * 8 + 4); });
      p += 12;
    }
    b.writeUInt32LE(0, p);
  });
  return b;
}

function jpegWithExif(jpeg, tiff) {
  const payload = Buffer.concat([Buffer.from("Exif\0\0", "latin1"), tiff]);
  const seg = Buffer.alloc(4);
  seg[0] = 0xff; seg[1] = 0xe1; seg.writeUInt16BE(payload.length + 2, 2);
  return Buffer.concat([jpeg.subarray(0, 2), seg, payload, jpeg.subarray(2)]);
}

/** Browser-made fixtures: still PNG, JPEG (pixels stored 60×40), animated lossless WebP, text image, SVG. */
async function browserFixtures(page) {
  const r = await page.evaluate(async (W, H, DELAYS) => {
    const b64 = async (blob) => { const u = new Uint8Array(await blob.arrayBuffer()); let s = ""; for (const x of u) s += String.fromCharCode(x); return btoa(s); };
    // 64×48 white with a red 16×12 block at the top-left quadrant corner (0..16, 0..12)
    const c = new OffscreenCanvas(64, 48); const g = c.getContext("2d");
    g.fillStyle = "#fff"; g.fillRect(0, 0, 64, 48); g.fillStyle = "#f00"; g.fillRect(0, 0, 16, 12);
    g.fillStyle = "#00f"; g.fillRect(24, 18, 16, 12);
    const png = await b64(await c.convertToBlob({ type: "image/png" }));
    const j = new OffscreenCanvas(60, 40); const jg = j.getContext("2d");
    jg.fillStyle = "#0a0"; jg.fillRect(0, 0, 60, 40); jg.fillStyle = "#fff"; jg.fillRect(0, 0, 10, 10);
    const jpg = await b64(await j.convertToBlob({ type: "image/jpeg", quality: 0.95 }));
    // Animated WebP muxed by hand (independent of the viewer's muxer).
    const chunk = (type, data) => { const pad = data.length & 1; const o = new Uint8Array(8 + data.length + pad); for (let i = 0; i < 4; i++) o[i] = type.charCodeAt(i); new DataView(o.buffer).setUint32(4, data.length, true); o.set(data, 8); return o; };
    const cat = (a) => { const o = new Uint8Array(a.reduce((n, x) => n + x.length, 0)); let p = 0; for (const x of a) { o.set(x, p); p += x.length; } return o; };
    const u24 = (a, o, v) => { a[o] = v & 255; a[o + 1] = (v >> 8) & 255; a[o + 2] = (v >> 16) & 255; };
    const anmf = [];
    for (let i = 0; i < 3; i++) {
      const f = new OffscreenCanvas(W, H); const fg = f.getContext("2d");
      fg.fillStyle = ["#f00", "#0f0", "#00f"][i]; fg.fillRect(0, 0, W, H); fg.fillStyle = "#fff"; fg.fillRect(0, 0, 8, 8);
      const u = new Uint8Array(await (await f.convertToBlob({ type: "image/webp", quality: 1 })).arrayBuffer());
      let o = 12, img = null;
      while (o < u.length) { const n = new DataView(u.buffer).getUint32(o + 4, true); const t = String.fromCharCode(...u.slice(o, o + 4)); if (t === "VP8L" || t === "VP8 ") img = u.slice(o, o + 8 + n + (n & 1)); o += 8 + n + (n & 1); }
      const head = new Uint8Array(16); u24(head, 6, W - 1); u24(head, 9, H - 1); u24(head, 12, DELAYS[i]); head[15] = 2;
      anmf.push(chunk("ANMF", cat([head, img])));
    }
    const vp8x = new Uint8Array(10); vp8x[0] = 0x02; u24(vp8x, 4, W - 1); u24(vp8x, 7, H - 1);
    const body = cat([chunk("VP8X", vp8x), chunk("ANIM", new Uint8Array(6)), ...anmf]);
    const riff = new Uint8Array(12); riff.set([82, 73, 70, 70]); new DataView(riff.buffer).setUint32(4, body.length + 4, true); riff.set([87, 69, 66, 80], 8);
    const webp = await b64(new Blob([riff, body]));
    // Text for Live Text
    const t = new OffscreenCanvas(640, 160); const tg = t.getContext("2d");
    tg.fillStyle = "#fff"; tg.fillRect(0, 0, 640, 160); tg.fillStyle = "#000"; tg.font = "bold 64px sans-serif"; tg.fillText("HELLO 123", 40, 105);
    const text = await b64(await t.convertToBlob({ type: "image/png" }));
    return { png, jpg, webp, text };
  }, W, H, DELAYS);
  const buf = (s) => Buffer.from(s, "base64");
  files.set("/still.png", ["image/png", buf(r.png)]);
  files.set("/photo.jpg", ["image/jpeg", jpegWithExif(buf(r.jpg), makeExif())]);
  files.set("/anim.webp", ["image/webp", buf(r.webp)]);
  files.set("/text.png", ["image/png", buf(r.text)]);
}

before(async () => {
  files.set("/anim.gif", ["image/gif", makeGif()]);
  files.set("/anim.png", ["image/png", makeApng()]);
  files.set("/logo.svg", ["image/svg+xml", Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="#e01b24"/></svg>')]);
  server = createServer((req, res) => {
    const hit = files.get(req.url ?? "");
    if (!hit) { res.writeHead(404).end(); return; }
    res.writeHead(200, { "content-type": hit[0], "content-length": hit[1].length }).end(hit[1]);
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  origin = `http://127.0.0.1:${server.address().port}`;
  ctx = await launch();
  const p = await waitForRules(ctx.browser, ctx.base);
  await browserFixtures(p);
  await p.close();
});

after(async () => {
  await ctx?.browser.close();
  await new Promise((r) => server?.close(r));
});

// ---- helpers --------------------------------------------------------------------------

async function openImage(path) {
  const page = await ctx.browser.newPage();
  await page.setViewport({ width: 1000, height: 700 });
  await page.goto(`${ctx.viewer}?kind=image&url=${origin}${path}`);
  await page.waitForSelector('[data-viewer="image"][data-state="ready"]', { timeout: 15000 });
  return page;
}

const state = (page) => page.$eval('[data-viewer="image"]', (e) => ({ ...e.dataset }));
async function waitReady(page, pred = "true") {
  await page.waitForFunction(`(() => { const d = document.querySelector('[data-viewer="image"]').dataset; return d.state === "ready" && (${pred}); })()`, { timeout: 15000 });
}
const subtitle = (page) => page.$eval(".subtitle", (e) => e.textContent);

/** Encode the current image as `fmt` through the viewer and decode it in the page. */
async function decodeOutput(page, fmt = "png") {
  return page.evaluate(async (fmt) => {
    const v = globalThis.viewShell.viewer;
    const r = await v.encodeItem(v.current, fmt, null, 0);
    const bmp = await createImageBitmap(r.blob);
    const c = new OffscreenCanvas(bmp.width, bmp.height); const g = c.getContext("2d"); g.drawImage(bmp, 0, 0);
    const px = (x, y) => [...g.getImageData(x, y, 1, 1).data];
    return { w: bmp.width, h: bmp.height, kind: r.kind, type: r.blob.type, tl: px(1, 1), tr: px(bmp.width - 2, 1), bl: px(1, bmp.height - 2), br: px(bmp.width - 2, bmp.height - 2), center: px(bmp.width >> 1, bmp.height >> 1) };
  }, fmt);
}

/** Decode an animated output with WebCodecs ImageDecoder: frame count, delays, loop. */
async function decodeAnim(page, fmt) {
  return page.evaluate(async (fmt) => {
    const v = globalThis.viewShell.viewer;
    const r = await v.encodeItem(v.current, fmt, null, 0);
    const bytes = new Uint8Array(await r.blob.arrayBuffer());
    const type = { gif: "image/gif", webp: "image/webp", apng: "image/png" }[fmt];
    const dec = new ImageDecoder({ data: bytes, type });
    await dec.tracks.ready; await dec.completed;
    const t = dec.tracks.selectedTrack;
    const delays = [], sizes = [], colors = [];
    for (let i = 0; i < t.frameCount; i++) {
      const { image } = await dec.decode({ frameIndex: i });
      delays.push(Math.round(image.duration / 1000));
      sizes.push(`${image.displayWidth}x${image.displayHeight}`);
      const c = new OffscreenCanvas(image.displayWidth, image.displayHeight); const g = c.getContext("2d"); g.drawImage(image, 0, 0);
      colors.push([...g.getImageData(c.width >> 1, c.height >> 1, 1, 1).data]);
      image.close();
    }
    const head = String.fromCharCode(...bytes.slice(0, 64));
    return { frames: t.frameCount, reps: t.repetitionCount === Infinity ? "inf" : t.repetitionCount, delays, sizes, colors, kind: r.kind, vp8l: /VP8L/.test(String.fromCharCode(...bytes)), head };
  }, fmt);
}

/** Screen point of an image pixel in the viewport. */
const screenOf = (page, ix, iy) => page.evaluate((ix, iy) => {
  const v = globalThis.viewShell.viewer.view;
  const r = v.viewport.getBoundingClientRect();
  const [x, y] = v.toView(ix, iy);
  return [r.left + x, r.top + y];
}, ix, iy);

async function menuItem(page, label) {
  await page.click(".img-tools");
  const items = await page.$$(".popover .menu-item");
  for (const it of items) {
    if ((await it.evaluate((e) => e.firstChild.textContent)) === label) { await it.click(); return; }
  }
  throw new Error(`no menu item ${label}`);
}

const near = (a, b, tol = 40) => a.slice(0, 3).every((v, i) => Math.abs(v - b[i]) <= tol);
const RED = [255, 0, 0], WHITE = [255, 255, 255], BLUE = [0, 0, 255];

// ---- tests -------------------------------------------------------------------------------

test("zoom: fit, actual, in, out, ctrl+wheel and touch pinch; pan when zoomed", async () => {
  const page = await openImage("/still.png");
  assert.equal(await subtitle(page), "64 × 48");
  const zoom = async () => Number(await page.$eval(".img-viewport", (e) => e.dataset.zoom));
  assert.equal(await zoom(), 1, "small images fit at 100 %");
  await page.focus(".img-viewport");
  await page.keyboard.press("+");
  assert.equal(await zoom(), 1.25);
  await page.keyboard.press("-");
  assert.equal(await zoom(), 1);
  // Ctrl+wheel (touchpad pinch) zooms around the pointer
  const [cx, cy] = await screenOf(page, 32, 24);
  await page.mouse.move(cx, cy);
  await page.keyboard.down("Control");
  await page.mouse.wheel({ deltaY: -40 });
  await page.keyboard.up("Control");
  await page.waitForFunction(() => Number(document.querySelector(".img-viewport").dataset.zoom) > 1.3);
  // touch pinch: two pointers moving apart
  await page.keyboard.press("0");
  const z0 = await zoom();
  await page.evaluate(([cx, cy]) => {
    const vp = document.querySelector(".img-viewport");
    const ev = (type, id, x) => vp.dispatchEvent(new PointerEvent(type, { pointerId: id, pointerType: "touch", clientX: x, clientY: cy, bubbles: true, isPrimary: id === 11 }));
    ev("pointerdown", 11, cx - 20); ev("pointerdown", 12, cx + 20);
    ev("pointermove", 11, cx - 60); ev("pointermove", 12, cx + 60);
    ev("pointerup", 11, cx - 60); ev("pointerup", 12, cx + 60);
  }, [cx, cy]);
  const z1 = await zoom();
  assert.ok(z1 > z0 * 2.5, `pinch zoomed ${z0} -> ${z1}`);
  // pan by dragging
  await page.keyboard.press("1");
  for (let i = 0; i < 15; i++) await page.keyboard.press("+");
  const left0 = await page.$eval(".img-canvas", (e) => parseFloat(e.style.left));
  await page.mouse.move(500, 350); await page.mouse.down(); await page.mouse.move(420, 300, { steps: 4 }); await page.mouse.up();
  const left1 = await page.$eval(".img-canvas", (e) => parseFloat(e.style.left));
  assert.ok(left1 < left0 - 50, `panned ${left0} -> ${left1}`);
  await page.keyboard.press("0");
  assert.equal(await zoom(), 1);
  await page.close();
});

test("rotate and flip (header buttons, Ctrl+R, Tools menu), with undo and redo", async () => {
  const page = await openImage("/still.png");
  await page.focus(".img-viewport");
  await page.keyboard.down("Control"); await page.keyboard.press("r"); await page.keyboard.up("Control");
  await waitReady(page, 'd.size === "48x64"');
  let o = await decodeOutput(page);
  assert.deepEqual([o.w, o.h], [48, 64]);
  assert.ok(near(o.tr, RED) && near(o.tl, WHITE), `red corner moved to top-right ${o.tr} ${o.tl}`);
  await menuItem(page, "Flip Horizontal");
  await waitReady(page, 'd.ops === "2"');
  o = await decodeOutput(page);
  assert.ok(near(o.tl, RED) && near(o.tr, WHITE), "flip mirrored it back to the left");
  await page.click('button[title="Rotate Left (Ctrl+L)"]');
  await waitReady(page, 'd.ops === "3" && d.size === "64x48"');
  assert.equal(await subtitle(page), "64 × 48");
  assert.ok(await page.evaluate(() => globalThis.viewShell.viewer.isDirty()));
  await page.keyboard.down("Control"); await page.keyboard.press("z"); await page.keyboard.up("Control");
  await waitReady(page, 'd.ops === "2" && d.size === "48x64"');
  await page.keyboard.down("Control"); await page.keyboard.down("Shift"); await page.keyboard.press("z");
  await page.keyboard.up("Shift"); await page.keyboard.up("Control");
  await waitReady(page, 'd.ops === "3"');
  assert.ok((await page.title()).startsWith("•"), "title shows the dirty dot");
  await page.close();
});

test("crop to a rectangular and an elliptical selection", async () => {
  const page = await openImage("/still.png");
  await menuItem(page, "Rectangular Selection");
  const [x0, y0] = await screenOf(page, 20, 14), [x1, y1] = await screenOf(page, 44, 34);
  await page.mouse.move(x0, y0); await page.mouse.down(); await page.mouse.move(x1, y1, { steps: 5 }); await page.mouse.up();
  await page.keyboard.down("Control"); await page.keyboard.press("k"); await page.keyboard.up("Control");
  await waitReady(page, 'd.size === "24x20"');
  const o = await decodeOutput(page);
  assert.deepEqual([o.w, o.h], [24, 20]);
  assert.ok(near(o.center, BLUE), `crop kept the blue block ${o.center}`);
  await menuItem(page, "Elliptical Selection");
  const [a, b] = await screenOf(page, 0, 0), [c, d] = await screenOf(page, 24, 20);
  await page.mouse.move(a + 1, b + 1); await page.mouse.down(); await page.mouse.move(c - 0.5, d - 0.5, { steps: 5 }); await page.mouse.up();
  await page.click(".img-hint button[title^='Crop']");
  await waitReady(page, 'd.ops === "2"');
  const e = await decodeOutput(page);
  assert.equal(e.tl[3], 0, "outside the ellipse is transparent");
  assert.equal(e.center[3], 255);
  await page.close();
});

test("instant alpha makes the clicked colour region transparent", async () => {
  const page = await openImage("/still.png");
  await menuItem(page, "Instant Alpha");
  const [x, y] = await screenOf(page, 60, 44);
  await page.mouse.click(x, y);
  await waitReady(page, 'd.ops === "1"');
  const o = await decodeOutput(page);
  assert.equal(o.br[3], 0, "white background is transparent");
  assert.equal(o.tl[3], 255, "the red block stays opaque");
  assert.ok(near(o.tl, RED));
  // JPEG can't hold alpha: Ctrl+S on a .png keeps PNG; Get Info lists the edit
  const info = await page.evaluate(() => globalThis.viewShell.viewer.getInfo());
  assert.ok(info[0].rows.some(([k, v]) => k === "Edits" && v === "alpha"));
  await page.close();
});

test("adjust colour (live preview, Apply) and adjust size", async () => {
  const page = await openImage("/still.png");
  await page.focus(".img-viewport");
  await page.keyboard.down("Control"); await page.keyboard.down("Shift"); await page.keyboard.press("c");
  await page.keyboard.up("Shift"); await page.keyboard.up("Control");
  await page.waitForSelector(".img-color:not([hidden])");
  await page.waitForFunction(() => Number(document.querySelector('[data-viewer="image"]').dataset.preview) >= 1);
  await page.$eval('.img-color input[data-key="saturation"]', (i) => { i.value = "-1"; i.dispatchEvent(new Event("input")); });
  await page.waitForFunction(() => Number(document.querySelector('[data-viewer="image"]').dataset.preview) >= 2);
  await page.click(".img-color-actions .btn.suggested");
  await waitReady(page, 'd.ops === "1"');
  let o = await decodeOutput(page);
  const [r, g, b] = o.tl;
  assert.ok(Math.abs(r - g) < 4 && Math.abs(g - b) < 4, `desaturated to gray: ${o.tl}`);
  // Adjust Size dialog: width 32 keeps the aspect ratio
  await menuItem(page, "Adjust Size…");
  await page.waitForSelector("dialog.dialog[open]");
  await page.$$eval("dialog .img-input", (inputs) => {
    const w = inputs[1]; w.value = "32"; w.dispatchEvent(new Event("input"));
  });
  await page.click('dialog .btn[data-value="ok"]');
  await waitReady(page, 'd.size === "32x24"');
  o = await decodeOutput(page);
  assert.deepEqual([o.w, o.h], [32, 24]);
  await page.close();
});

test("EXIF orientation, camera and GPS in Get Info; EXIF kept on save", async () => {
  const page = await openImage("/photo.jpg");
  assert.equal(await subtitle(page), "40 × 60", "EXIF orientation 6 is applied");
  await page.keyboard.down("Control"); await page.keyboard.press("i"); await page.keyboard.up("Control");
  await page.waitForFunction(() => /GPS/.test(document.querySelector(".inspector")?.textContent ?? ""));
  const text = await page.$eval(".inspector", (e) => e.textContent);
  for (const s of ["ViewTest", "Model X", "f/2.8", "1/125 s", "200", "48.858222, 2.294500", "35 m", "EXIF orientation"]) {
    assert.ok(text.includes(s), `inspector shows ${s}`);
  }
  const href = await page.$eval('.inspector a[href*="openstreetmap"]', (a) => a.href);
  assert.match(href, /mlat=48\.858222&mlon=2\.294500/);
  // edit and save: same format, EXIF kept, orientation reset to 1 (pixels are upright now)
  await page.focus(".img-viewport");
  await page.keyboard.down("Control"); await page.keyboard.press("r"); await page.keyboard.up("Control");
  await waitReady(page, 'd.size === "60x40"');
  const saved = await page.evaluate(async () => {
    const r = await globalThis.viewShell.viewer.save({ mode: "save-as" });
    const u = new Uint8Array(await r.blob.arrayBuffer()); let s = ""; for (const x of u) s += String.fromCharCode(x);
    return { name: r.suggestedName, b64: btoa(s), type: r.blob.type };
  });
  assert.equal(saved.name, "photo.jpg");
  assert.equal(saved.type, "image/jpeg");
  const bytes = Buffer.from(saved.b64, "base64");
  const tags = await exifr.parse(bytes, { tiff: true, exif: true, gps: true, translateValues: false });
  assert.equal(tags.Make, "ViewTest");
  assert.equal(tags.Orientation, 1);
  assert.equal(tags.ISO, 200);
  assert.ok(Math.abs(tags.latitude - 48.858222) < 1e-5 && Math.abs(tags.longitude - 2.2945) < 1e-5);
  assert.equal(tags.ExifImageWidth, 60);
  assert.equal(tags.ExifImageHeight, 40);
  await page.close();
});

test("saving in place overwrites the opened file handle (OPFS) and clears dirty", async () => {
  const page = await ctx.browser.newPage();
  await page.goto(ctx.viewer);
  const id = await page.evaluate(async (url) => {
    const bytes = await (await fetch(url)).arrayBuffer();
    const dir = await navigator.storage.getDirectory();
    const h = await dir.getFileHandle("inplace.png", { create: true });
    const w = await h.createWritable(); await w.write(bytes); await w.close();
    const id = crypto.randomUUID();
    const db = await new Promise((r) => { const q = indexedDB.open("view", 1); q.onupgradeneeded = () => q.result.createObjectStore("handles"); q.onsuccess = () => r(q.result); });
    await new Promise((r) => { const t = db.transaction("handles", "readwrite"); t.objectStore("handles").put(h, `pending:${id}`); t.oncomplete = r; });
    return id;
  }, `${origin}/still.png`);
  await page.goto(`${ctx.viewer}?handle=${id}`);
  await page.waitForSelector('[data-viewer="image"][data-state="ready"]', { timeout: 15000 });
  await page.focus(".img-viewport");
  await page.keyboard.down("Control"); await page.keyboard.press("r"); await page.keyboard.up("Control");
  await waitReady(page, 'd.size === "48x64"');
  assert.ok((await page.title()).startsWith("•"));
  await page.keyboard.down("Control"); await page.keyboard.press("s"); await page.keyboard.up("Control");
  await page.waitForFunction(() => !document.title.startsWith("•"), { timeout: 10000 });
  const out = await page.evaluate(async () => {
    const f = await (await (await navigator.storage.getDirectory()).getFileHandle("inplace.png")).getFile();
    const b = await createImageBitmap(f);
    return [b.width, b.height, f.type];
  });
  assert.deepEqual(out.slice(0, 2), [48, 64], "the file on disk is the rotated image");
  await waitReady(page, 'd.ops === "0"');
  assert.equal(await page.evaluate(() => globalThis.viewShell.viewer.isDirty()), false);
  await page.close();
});

for (const [path, fmt] of [["/anim.gif", "gif"], ["/anim.png", "apng"], ["/anim.webp", "webp"]]) {
  test(`edited animated ${fmt.toUpperCase()} keeps frame count, delays and loop`, async () => {
    const page = await openImage(path);
    await waitReady(page, 'd.frames === "3"');
    const before = await decodeAnim(page, fmt);
    assert.equal(before.frames, 3);
    await page.focus(".img-viewport");
    await page.keyboard.down("Control"); await page.keyboard.press("r"); await page.keyboard.up("Control");
    await waitReady(page, `d.size === "${H}x${W}"`);
    // crop off the top 10 rows of the rotated frames too
    await page.evaluate(() => { const v = globalThis.viewShell.viewer.view; v.setTool("rect"); v.setSel([0, 10, v.iw, v.ih]); });
    await page.keyboard.down("Control"); await page.keyboard.press("k"); await page.keyboard.up("Control");
    await waitReady(page, `d.size === "${H}x${W - 10}"`);
    const out = await decodeAnim(page, fmt);
    assert.equal(out.kind, "anim");
    assert.equal(out.frames, 3, "frame count kept");
    assert.deepEqual(out.delays, DELAYS, "per-frame delays kept");
    assert.equal(out.reps, before.reps, "loop count kept");
    assert.deepEqual(out.sizes, Array(3).fill(`${H}x${W - 10}`));
    assert.ok(near(out.colors[0], RED) && near(out.colors[1], [0, 255, 0]) && near(out.colors[2], BLUE), JSON.stringify(out.colors));
    if (fmt === "webp") assert.ok(out.vp8l, "lossless source stays lossless (VP8L)");
    // Ctrl+S keeps the original container
    const name = await page.evaluate(async () => (await globalThis.viewShell.viewer.save({ mode: "save-as" })).suggestedName);
    assert.equal(name, path.slice(1));
    await page.close();
  });
}

test("animation exported to other animated formats and flattened for stills", async () => {
  const page = await openImage("/anim.gif");
  await waitReady(page, 'd.frames === "3"');
  for (const fmt of ["webp", "apng"]) {
    const o = await decodeAnim(page, fmt);
    assert.equal(o.frames, 3, fmt);
    assert.deepEqual(o.delays, DELAYS, fmt);
  }
  const still = await decodeOutput(page, "jpeg");
  assert.equal(still.kind, "flat");
  assert.equal(still.type, "image/jpeg");
  await page.close();
});

test("markup is flattened into the saved image", async () => {
  const page = await openImage("/still.png");
  await page.focus(".img-viewport");
  await page.keyboard.down("Control"); await page.keyboard.down("Shift"); await page.keyboard.press("a");
  await page.keyboard.up("Shift"); await page.keyboard.up("Control");
  await page.waitForFunction(() => document.querySelector(".img-markup-bar:not([hidden])") && globalThis.viewShell.viewer.current.mk);
  await page.evaluate(() => {
    globalThis.viewShell.viewer.current.mk.add({ type: "rect", surface: "img", x: 40, y: 30, w: 20, h: 14,
      style: { stroke: "#00ff00", fill: "#00ff00", width: 2, opacity: 1 } });
  });
  assert.ok(await page.evaluate(() => globalThis.viewShell.viewer.isDirty()));
  let o = await decodeOutput(page);
  const px = await page.evaluate(async () => {
    const v = globalThis.viewShell.viewer;
    const r = await v.encodeItem(v.current, "png", null, 0);
    const b = await createImageBitmap(r.blob); const c = new OffscreenCanvas(b.width, b.height); const g = c.getContext("2d"); g.drawImage(b, 0, 0);
    return [...g.getImageData(50, 37, 1, 1).data];
  });
  assert.ok(near(px, [0, 255, 0]), `markup rect drawn into the pixels: ${px}`);
  // a geometry edit commits the markup into the op list (rotating it with the image)
  await page.keyboard.down("Control"); await page.keyboard.press("r"); await page.keyboard.up("Control");
  await waitReady(page, 'd.ops === "2"');
  o = await decodeOutput(page);
  assert.deepEqual([o.w, o.h], [48, 64]);
  const edits = await page.evaluate(() => globalThis.viewShell.viewer.getInfo().then((s) => s[0].rows.find((r) => r[0] === "Edits")?.[1]));
  assert.equal(edits, "markup, rotate");
  await page.close();
});

test("multi-image sidebar: add, select, reorder, batch convert to a folder, combine into a PDF", async () => {
  const page = await openImage("/still.png");
  await page.evaluate(async (origin) => {
    const src = async (p) => {
      const blob = await (await fetch(origin + p)).blob();
      return { name: p.slice(1), mime: blob.type, size: blob.size, lastModified: 0, streamable: false, blob: async () => blob, head: async (n = 8192) => new Uint8Array(await blob.slice(0, n).arrayBuffer()) };
    };
    const v = globalThis.viewShell.viewer;
    const list = [await src("/photo.jpg"), await src("/anim.gif")];
    if (!v.acceptsDrop(list)) throw new Error("drop refused");
    await v.addDropped(list);
  }, origin);
  await page.waitForFunction(() => document.querySelectorAll(".img-row").length === 3 && !document.querySelector(".sidebar").hidden);
  assert.equal((await state(page)).items, "3");
  // next image with the arrow key, subtitle counts
  await page.focus(".img-viewport");
  await page.keyboard.press("ArrowRight");
  await waitReady(page, 'd.size === "40x60"');
  assert.equal(await subtitle(page), "40 × 60  ·  2 of 3");
  // Ctrl+A selects all; rotate applies to every selected image
  await page.keyboard.down("Control"); await page.keyboard.press("a"); await page.keyboard.up("Control");
  assert.equal((await state(page)).selected, "3");
  await page.keyboard.down("Control"); await page.keyboard.press("r"); await page.keyboard.up("Control");
  await page.waitForFunction(() => globalThis.viewShell.viewer.items.every((i) => i.ops.length === 1));
  // reorder by dragging the last row to the top (after selecting a single image)
  await page.click(".img-row:nth-child(1)");
  await page.evaluate(() => {
    const rows = document.querySelectorAll(".img-row");
    const dt = new DataTransfer();
    rows[2].dispatchEvent(new DragEvent("dragstart", { dataTransfer: dt, bubbles: true }));
    rows[0].dispatchEvent(new DragEvent("dragover", { dataTransfer: dt, bubbles: true, cancelable: true, clientY: rows[0].getBoundingClientRect().top + 2 }));
    rows[0].dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
  });
  const order = await page.$$eval(".img-row-name", (n) => n.map((e) => e.textContent.replace(" •", "")));
  assert.deepEqual(order, ["anim.gif", "still.png", "photo.jpg"], JSON.stringify(order));
  // Export Selected Images… through the UI; the folder picker hands out an OPFS folder
  await page.evaluate(async () => {
    const root = await navigator.storage.getDirectory();
    try { await root.removeEntry("batch", { recursive: true }); } catch {}
    const dir = await root.getDirectoryHandle("batch", { create: true });
    await dir.getFileHandle("still.jpg", { create: true }); // taken: the export must not overwrite it
    globalThis.showDirectoryPicker = async () => dir;
  });
  await page.click('.img-listbar button[title="Export Selected Images…"]');
  await page.waitForSelector("dialog.dialog[open]");
  await page.select("dialog select", "jpeg");
  await page.$$eval("dialog .img-input", (inputs) => { inputs[2].value = "32"; });
  await page.click('dialog .btn[data-value="ok"]');
  await page.waitForFunction(async () => {
    const dir = await (await navigator.storage.getDirectory()).getDirectoryHandle("batch");
    const names = []; for await (const k of dir.keys()) names.push(k);
    return names.length === 4;
  }, { timeout: 15000 });
  const outs = await page.evaluate(async () => {
    const dir = await (await navigator.storage.getDirectory()).getDirectoryHandle("batch");
    const out = {};
    for await (const [k, h] of dir.entries()) {
      const f = await h.getFile();
      if (!f.size) { out[k] = "empty"; continue; }
      const b = await createImageBitmap(f); out[k] = `${f.type} ${b.width}x${b.height}`;
    }
    return out;
  });
  assert.equal(outs["still.jpg"], "empty");
  assert.equal(outs["still 2.jpg"], "image/jpeg 24x32", "rotated, longest side 32, unique name");
  assert.ok(/image\/jpeg (32x21|32x22)/.test(outs["photo.jpg"]), outs["photo.jpg"]);
  assert.equal(outs["anim.jpg"], "image/jpeg 24x32", "first frame, rotated, longest side 32");
  // Combine into a PDF (A4, margin): one page per image in list order
  const pdfB64 = await page.evaluate(async () => {
    const b = await globalThis.viewShell.viewer.exportPdf({ pageSize: "a4", marginMm: 10 });
    const u = new Uint8Array(await b.arrayBuffer()); let s = ""; for (const x of u) s += String.fromCharCode(x); return btoa(s);
  });
  const pdf = await PDFDocument.load(Buffer.from(pdfB64, "base64"));
  assert.equal(pdf.getPageCount(), 3);
  const sizes = pdf.getPages().map((p) => p.getSize()).map(({ width, height }) => [Math.round(width), Math.round(height)]);
  assert.deepEqual(sizes, [[595, 842], [595, 842], [842, 595]], "portrait pages; landscape for the wide rotated photo");
  await page.close();
});

test("Export as PDF and Export As go through the shell's save path", async () => {
  const page = await openImage("/still.png");
  await page.evaluate(() => {
    globalThis.picked = [];
    globalThis.showSaveFilePicker = async (o) => {
      globalThis.picked.push(o.suggestedName);
      return (await navigator.storage.getDirectory()).getFileHandle(`picked-${o.suggestedName}`, { create: true });
    };
  });
  await page.click('button[title="Export as PDF…"]');
  await page.waitForSelector("dialog.dialog[open]");
  await page.select("dialog select", "letter");
  await page.click('dialog .btn[data-value="ok"]');
  await page.waitForFunction(() => globalThis.picked.includes("still.pdf"), { timeout: 15000 });
  const pdfB64 = await page.waitForFunction(async () => {
    const f = await (await (await navigator.storage.getDirectory()).getFileHandle("picked-still.pdf")).getFile();
    if (!f.size) return null;
    const u = new Uint8Array(await f.arrayBuffer()); let s = ""; for (const x of u) s += String.fromCharCode(x); return btoa(s);
  }).then((h) => h.jsonValue());
  const pdf = await PDFDocument.load(Buffer.from(pdfB64, "base64"));
  assert.equal(pdf.getPageCount(), 1);
  assert.deepEqual(Object.values(pdf.getPage(0).getSize()).map(Math.round), [792, 612], "US Letter, landscape for a wide image");
  // Export… (Ctrl+Shift+E): the shell's format picker, then the viewer's options dialog
  await page.focus(".img-viewport");
  await page.keyboard.down("Control"); await page.keyboard.down("Shift"); await page.keyboard.press("e");
  await page.keyboard.up("Shift"); await page.keyboard.up("Control");
  await page.waitForSelector("dialog.dialog[open] .choice-list");
  await page.click('dialog .btn[data-value="ok"]');
  await page.waitForSelector("dialog.dialog[open] select");
  await page.select("dialog select", "webp");
  await page.click('dialog .btn[data-value="ok"]');
  await page.waitForFunction(() => globalThis.picked.includes("still.webp"), { timeout: 15000 });
  await page.close();
});

test("SVG opens rasterized; Live Text recognizes text and is selectable", async () => {
  const svg = await openImage("/logo.svg");
  assert.equal(await subtitle(svg), "2048 × 1024");
  await svg.close();
  const page = await openImage("/text.png");
  await page.waitForFunction(() => Number(document.querySelector(".img-text")?.dataset.lines) >= 1, { timeout: 60000 });
  const text = await page.evaluate(() => globalThis.viewShell.viewer.live.text());
  assert.match(text.replace(/\s+/g, " "), /HELLO\s*123/i);
  assert.ok(await page.$(".img-text svg.ocr-layer .ocr-hit"));
  await page.close();
});
