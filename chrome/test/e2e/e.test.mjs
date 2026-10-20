// Stream E: 3D model viewer (STL/OBJ/PLY, orbit, Get Info, a 2M-triangle model)
// and the video/audio player (control bar, shortcuts, Get Info).
// Run: VIEW_DIST=dist-e node --test test/e2e/e.test.mjs
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { after, before, describe, test } from "node:test";
import UPNG from "upng-js";
import { launch, open, startServer, waitForRules } from "./harness.mjs";
import { asciiStl, binaryStl, boxObj, boxPly, boxTris, heightFieldStl, wav } from "../fixtures/e/gen.mjs";

let ctx, server, optionsPage, tmp;
const OCTET = { "content-type": "application/octet-stream" };

/** A route handler with HTTP Range support (Chrome needs 206 to seek in media). */
function ranged(type, buf) {
  return (req, res) => {
    const m = /bytes=(\d*)-(\d*)/.exec(req.headers.range ?? "");
    if (!m) return void res.writeHead(200, { "content-type": type, "content-length": buf.length, "accept-ranges": "bytes" }).end(buf);
    const start = m[1] ? Number(m[1]) : 0;
    const end = Math.min(m[2] ? Number(m[2]) : buf.length - 1, buf.length - 1);
    if (start >= buf.length) return void res.writeHead(416, { "content-range": `bytes */${buf.length}` }).end();
    res.writeHead(206, { "content-type": type, "content-range": `bytes ${start}-${end}/${buf.length}`, "content-length": end - start + 1, "accept-ranges": "bytes" })
      .end(buf.subarray(start, end + 1));
  };
}

/** Record a short WebM (VP8/VP9 + Opus) in the browser: there is no ffmpeg here. */
async function recordWebm() {
  const page = await ctx.browser.newPage();
  await page.goto(ctx.viewer); // any extension page: a secure context with MediaRecorder
  const b64 = await page.evaluate(async () => {
    const canvas = Object.assign(document.createElement("canvas"), { width: 320, height: 240 });
    const g = canvas.getContext("2d");
    const stream = canvas.captureStream(30);
    const ac = new AudioContext();
    const osc = ac.createOscillator();
    const dest = ac.createMediaStreamDestination();
    osc.connect(dest);
    osc.start();
    dest.stream.getAudioTracks().forEach((t) => stream.addTrack(t));
    const rec = new MediaRecorder(stream, { mimeType: MediaRecorder.isTypeSupported("video/webm;codecs=vp8,opus") ? "video/webm;codecs=vp8,opus" : "video/webm" });
    const chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    const done = new Promise((r) => { rec.onstop = r; });
    rec.start(100);
    const t0 = performance.now();
    await new Promise((resolve) => {
      const draw = () => {
        const t = (performance.now() - t0) / 1000;
        g.fillStyle = `hsl(${(t * 200) % 360} 70% 50%)`;
        g.fillRect(0, 0, 320, 240);
        g.fillStyle = "#fff";
        g.fillRect(20 + t * 100, 100, 60, 40);
        if (t < 2) requestAnimationFrame(draw); else resolve();
      };
      draw();
    });
    rec.stop();
    await done;
    osc.stop();
    const buf = new Uint8Array(await new Blob(chunks).arrayBuffer());
    let s = "";
    for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
    return btoa(s);
  });
  await page.close();
  return Buffer.from(b64, "base64");
}

before(async () => {
  tmp = mkdtempSync(join(tmpdir(), "view-e-"));
  const box = boxTris(10, 20, 30, [1000, -500, 0.5]);
  ctx = await launch({ args: ["--autoplay-policy=no-user-gesture-required"] });
  const webm = await recordWebm().catch((e) => { console.log("recordWebm failed:", e.message); return null; });
  const tone = wav(30);
  server = await startServer({
    "/box.stl": [OCTET, binaryStl(box, "View test box")],
    "/box-ascii.stl": [OCTET, asciiStl(box, "ascii box")],
    "/box.obj": [{ "content-type": "text/plain" }, boxObj(10, 20, 30)],
    "/box.ply": [OCTET, boxPly(10, 20, 30, "ascii")],
    "/box-bin.ply": [OCTET, boxPly(10, 20, 30, "binary")],
    "/bad.stl": [OCTET, Buffer.from("this is not a mesh at all")],
    "/tone.wav": ranged("audio/wav", tone),
    "/clip.webm": webm ? ranged("video/webm", webm) : [{}, ""],
    "/broken.mp4": ranged("video/mp4", Buffer.from("not really an mp4 file, just text".repeat(20))),
  });
  optionsPage = await waitForRules(ctx.browser, ctx.base);
  writeFileSync(join(tmp, "tone.wav"), tone);
  writeFileSync(join(tmp, "box.stl"), binaryStl(box));
  if (webm) writeFileSync(join(tmp, "clip.webm"), webm);
  ctx.webm = !!webm;
});

after(async () => {
  await ctx?.browser.close();
  await server?.close();
  if (tmp) rmSync(tmp, { recursive: true, force: true });
});

const viewerUrl = (kind, url) => `${ctx.viewer}?kind=${kind}&url=${url}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function openModel(path, { timeout = 20000, viewport } = {}) {
  const page = await ctx.browser.newPage();
  if (viewport) await page.setViewport(viewport);
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(viewerUrl("model", `${server.origin}${path}`));
  await page.waitForSelector('[data-viewer="model"][data-rendered="true"][data-analyzed="true"]', { timeout });
  page.errors = errors;
  return page;
}
const data = (page, sel = "[data-viewer]") => page.$eval(sel, (e) => ({ ...e.dataset }));
// `fn` is a JS expression over `d` (the dataset) and `a` (arg). Evaluated as a string: the page's CSP forbids `new Function`.
const waitData = (page, fn, arg, sel = "[data-viewer]") => page.waitForFunction(
  `(() => { const d = { ...document.querySelector(${JSON.stringify(sel)}).dataset }; const a = ${JSON.stringify(arg ?? null)}; return (${fn}); })()`,
  { timeout: 10000, polling: 100 });

async function infoRows(page) {
  await page.keyboard.down("Control");
  await page.keyboard.press("KeyI");
  await page.keyboard.up("Control");
  await page.waitForSelector(".inspector dl");
  await page.waitForFunction(() => ![...document.querySelectorAll(".inspector dd")].some((d) => d.textContent === "Computing…"));
  return page.$$eval(".inspector h2, .inspector dl", (nodes) => {
    const out = {};
    let section = "";
    for (const n of nodes) {
      if (n.tagName === "H2") section = n.textContent;
      else {
        const dts = [...n.querySelectorAll("dt")], dds = [...n.querySelectorAll("dd")];
        dts.forEach((dt, i) => { out[`${section}/${dt.textContent}`] = dds[i].textContent; });
      }
    }
    return out;
  });
}

describe("3D models", () => {
  test("binary STL renders, with dimensions, volume, area, triangles in Get Info", async () => {
    const page = await openModel("/box.stl", { viewport: { width: 1000, height: 700 } });
    const d = await data(page);
    assert.equal(d.triangles, "12");
    assert.equal(d.format, "Binary STL");
    assert.equal(d.closed, "true");
    assert.equal(d.vertices, "8");
    assert.ok(Math.abs(Number(d.volume) - 6000) < 1e-3, d.volume);
    assert.ok(Math.abs(Number(d.area) - 2200) < 1e-3, d.area);
    assert.equal(d.smooth, "true");
    assert.equal(await page.$eval(".subtitle", (e) => e.textContent), "12 triangles · 10.0 × 20.0 × 30.0 mm");
    const rows = await infoRows(page);
    assert.equal(rows["Model/Format"], "Binary STL");
    assert.equal(rows["Model/STL header"], "View test box");
    assert.equal(rows["Model/Triangles"], "12");
    assert.equal(rows["Model/Vertices"], "8");
    assert.equal(rows["Model/Dimensions"], "10.0 × 20.0 × 30.0 mm");
    assert.equal(rows["Model/Surface area"], "2,200 mm²");
    assert.equal(rows["Model/Volume"], "6,000 mm³  (6.00 cm³)");
    assert.equal(rows["Model/Watertight"], "Yes (closed mesh)");
    assert.equal(rows["Model/Bounding box min"], "1,000.00, -500.00, 0.50");
    assert.equal(rows["Model/Centre of mass"], "1,005.00, -490.00, 15.50");
    assert.deepEqual(page.errors, []);
    await page.close();
  });

  test("the canvas actually draws the model (not just a background)", async () => {
    const page = await openModel("/box.stl", { viewport: { width: 800, height: 600 } });
    await sleep(300);
    const png = UPNG.decode(await page.screenshot({ encoding: "binary", type: "png" }));
    const rgba = new Uint8Array(UPNG.toRGBA8(png)[0]);
    const px = (x, y) => [...rgba.subarray((y * png.width + x) * 4, (y * png.width + x) * 4 + 3)];
    const corner = px(40, 80), centre = px(png.width >> 1, png.height >> 1);
    assert.notDeepEqual(corner, centre, `corner ${corner} vs centre ${centre}`);
    // the model is bluish-grey, the background nearly neutral
    assert.ok(centre[2] > centre[0], `centre ${centre}`);
    await page.close();
  });

  test("ASCII STL, OBJ (Y-up), PLY ascii and binary all parse to the same box", async () => {
    for (const [path, fmt, up] of [["/box-ascii.stl", "ASCII STL", "z"], ["/box.obj", "OBJ", "y"], ["/box.ply", "PLY", "y"], ["/box-bin.ply", "PLY", "y"]]) {
      const page = await openModel(path);
      const d = await data(page);
      assert.equal(d.format, fmt, path);
      assert.equal(d.up, up, path);
      assert.equal(d.triangles, "12", path);
      assert.ok(Math.abs(Number(d.volume) - 6000) < 1e-3, `${path} ${d.volume}`);
      assert.equal(d.closed, "true", path);
      await page.close();
    }
  });

  test("orbit, pan and zoom with mouse and touchpad gestures; reset and fit", async () => {
    const page = await openModel("/box.stl", { viewport: { width: 1000, height: 700 } });
    const canvas = await page.$("canvas.model-canvas");
    const box = await canvas.boundingBox();
    const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
    const d0 = await data(page);
    assert.equal(Number(d0.yaw), 30);
    assert.equal(Number(d0.pitch), 22);

    // left drag orbits
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx + 100, cy + 50, { steps: 5 });
    await page.mouse.up();
    await waitData(page, "Math.abs(Number(d.yaw) - 30) > 5");
    const d1 = await data(page);
    assert.ok(Number(d1.yaw) !== 30 && Number(d1.pitch) !== 22, JSON.stringify(d1));

    // shift-drag pans
    await page.keyboard.down("Shift");
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx + 60, cy + 30, { steps: 5 });
    await page.mouse.up();
    await page.keyboard.up("Shift");
    await waitData(page, "d.target !== a", d1.target);
    assert.notEqual((await data(page)).target, "0.000,0.000,0.000");

    // pinch (ctrl + wheel) zooms; two-finger scroll orbits
    const dist0 = Number((await data(page)).dist);
    await page.evaluate((x, y) => document.querySelector("canvas.model-canvas").dispatchEvent(
      new WheelEvent("wheel", { deltaY: -40, ctrlKey: true, clientX: x, clientY: y, bubbles: true, cancelable: true })), cx, cy);
    await waitData(page, "Number(d.dist) < a", dist0);
    const yaw0 = Number((await data(page)).yaw);
    await page.evaluate((x, y) => document.querySelector("canvas.model-canvas").dispatchEvent(
      new WheelEvent("wheel", { deltaX: 40, deltaY: 7, clientX: x, clientY: y, bubbles: true, cancelable: true })), cx, cy);
    await waitData(page, "Number(d.yaw) !== a", yaw0);
    await sleep(200); // a mouse wheel notch after idle (not part of a touchpad stream)

    // a mouse wheel (line mode) zooms
    const dist1 = Number((await data(page)).dist);
    await page.evaluate((x, y) => document.querySelector("canvas.model-canvas").dispatchEvent(
      new WheelEvent("wheel", { deltaY: 3, deltaMode: 1, clientX: x, clientY: y, bubbles: true, cancelable: true })), cx, cy);
    await waitData(page, "Number(d.dist) > a", dist1);

    // R resets (animated), W toggles the wireframe
    await page.keyboard.press("KeyR");
    await waitData(page, "d.yaw === '30.00' && d.pitch === '22.00' && d.target === '0.000,0.000,0.000'");
    assert.ok(Math.abs(Number((await data(page)).dist) - Number(d0.dist)) < 1e-3 * Number(d0.dist));
    await page.keyboard.press("KeyW");
    await waitData(page, "d.wireframe === 'true'");
    await page.keyboard.press("KeyW");
    await waitData(page, "d.wireframe === 'false'");

    // double click resets as well
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx + 80, cy, { steps: 4 });
    await page.mouse.up();
    await page.mouse.click(cx, cy, { count: 2 });
    await waitData(page, "d.yaw === '30.00'");
    assert.deepEqual(page.errors, []);
    await page.close();
  });

  test("View menu: standard views, Y axis up, toolbar toggles", async () => {
    const page = await openModel("/box.stl", { viewport: { width: 1000, height: 700 } });
    await page.click('button[data-action="views"]');
    await page.waitForSelector(".popover");
    const items = await page.$$eval(".popover .menu-item", (b) => b.map((x) => x.textContent));
    assert.deepEqual(items.slice(0, 7), ["Front", "Back", "Left", "Right", "Top", "Bottom", "Isometric"]);
    assert.ok(items.some((t) => t.includes("Z Axis Up")) && items.includes("Show Statistics"));
    await page.evaluate(() => [...document.querySelectorAll(".popover .menu-item")].find((b) => b.textContent === "Top").click());
    await waitData(page, "d.pitch === '90.00'");
    await page.click('button[data-action="grid"]');
    await waitData(page, "d.grid === 'false'");
    await page.click('button[data-action="wireframe"]');
    await waitData(page, "d.wireframe === 'true'");
    await page.click('button[data-action="flat"]');
    await waitData(page, "d.flat === 'true'");
    await page.click('button[data-action="views"]');
    await page.evaluate(() => [...document.querySelectorAll(".popover .menu-item")].find((b) => b.textContent.includes("Y Axis Up")).click());
    await waitData(page, "d.up === 'y'");
    await page.click('button[data-action="views"]');
    await page.evaluate(() => [...document.querySelectorAll(".popover .menu-item")].find((b) => b.textContent === "Show Statistics").click());
    await page.waitForFunction(() => !document.querySelector(".model-stats").hidden);
    const stats = await page.$eval(".model-stats", (e) => e.textContent);
    assert.match(stats, /12 triangles · 8 vertices/);
    assert.match(stats, /Volume 6,000 mm³/);
    await page.close();
  });

  test("a file that is not a mesh shows an error page", async () => {
    const page = await ctx.browser.newPage();
    await page.goto(viewerUrl("model", `${server.origin}/bad.stl`));
    await page.waitForSelector('[data-viewer="model"][data-state="error"]', { timeout: 15000 });
    assert.match(await page.$eval(".model-status", (e) => e.textContent), /Can't Open This Model[\s\S]*STL/);
    await page.close();
  });

  test("STL opens in View from file:// and via the interception rule", async () => {
    const page = await ctx.browser.newPage();
    const url = await open(page, `${server.origin}/box.stl`);
    assert.ok(url.startsWith(`${ctx.viewer}?kind=model&url=`), url);
    await page.waitForSelector('[data-viewer="model"][data-analyzed="true"]', { timeout: 20000 });
    const access = await page.evaluate(() => chrome.extension.isAllowedFileSchemeAccess());
    if (access) {
      await open(page, pathToFileURL(join(tmp, "box.stl")).href);
      await page.waitForSelector('[data-viewer="model"][data-analyzed="true"]', { timeout: 20000 });
      assert.equal((await data(page)).triangles, "12");
    }
    await page.close();
  });

  test("a 2-million-triangle model renders", { timeout: 240000 }, async () => {
    const big = heightFieldStl(1000); // 2 * 1000 * 1000 triangles, 100 MB
    const tmpBig = join(tmp, "big.stl");
    writeFileSync(tmpBig, big);
    const bigServer = await startServer({ "/big.stl": [OCTET, big] });
    try {
      const page = await ctx.browser.newPage();
      await page.setViewport({ width: 900, height: 600 });
      const errors = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await page.goto(viewerUrl("model", `${bigServer.origin}/big.stl`));
      await page.waitForSelector('[data-viewer="model"][data-rendered="true"]', { timeout: 120000 });
      const t = await data(page);
      assert.equal(t.triangles, "2000000");
      await page.waitForSelector('[data-viewer="model"][data-analyzed="true"]', { timeout: 120000 });
      const d = await data(page);
      assert.equal(d.vertices, String(1001 * 1001));
      assert.equal(d.closed, "false"); // an open height field
      // the camera still works on it
      const canvas = await (await page.$("canvas.model-canvas")).boundingBox();
      await page.mouse.move(canvas.x + 400, canvas.y + 300);
      await page.mouse.down();
      await page.mouse.move(canvas.x + 460, canvas.y + 300, { steps: 3 });
      await page.mouse.up();
      await waitData(page, "d.yaw !== '30.00'");
      assert.deepEqual(errors, []);
      await page.close();
    } finally {
      await bigServer.close();
    }
  });
});

describe("video and audio", () => {
  async function openMedia(path, wait = '[data-viewer="media"][data-state="paused"], [data-viewer="media"][data-state="ready"]') {
    const page = await ctx.browser.newPage();
    await page.setViewport({ width: 1000, height: 700 });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(viewerUrl("media", path.startsWith("file:") ? path : `${server.origin}${path}`));
    await page.waitForSelector(wait, { timeout: 15000 });
    await page.waitForFunction(() => document.querySelector("[data-viewer=media]").dataset.duration, { timeout: 15000 });
    page.errors = errors;
    return page;
  }
  const media = (page) => data(page, '[data-viewer="media"]');
  const waitMedia = (page, fn, arg) => waitData(page, fn, arg, '[data-viewer="media"]');

  test("audio: plays, seeks, volume, mute, speed, loop with the keyboard and the control bar", async () => {
    const page = await openMedia("/tone.wav");
    let d = await media(page);
    assert.equal(d.audio, "true");
    assert.equal(Number(d.duration), 30);
    assert.equal(await page.$eval(".subtitle", (e) => e.textContent), "0:30");
    assert.equal(await page.$eval(".mv-time:not(.cur)", (e) => e.textContent), "0:30");

    await page.keyboard.press("Space");
    await waitMedia(page, "d.paused === 'false' && Number(d.current) > 0.2");
    await page.keyboard.press("Space");
    await waitMedia(page, "d.paused === 'true'");

    const t0 = Number((await media(page)).current);
    await page.keyboard.press("ArrowRight");
    await waitMedia(page, "Math.abs(Number(d.current) - (a + 5)) < 0.3", t0);
    await page.keyboard.press("ArrowLeft");
    await waitMedia(page, "Math.abs(Number(d.current) - a) < 0.3", t0);
    await page.keyboard.press("KeyL");
    await waitMedia(page, "Math.abs(Number(d.current) - (a + 10)) < 0.3", t0);
    await page.keyboard.press("Digit5");
    await waitMedia(page, "Math.abs(Number(d.current) - 15) < 0.3");
    await page.keyboard.press("Home");
    await waitMedia(page, "Number(d.current) < 0.3");

    await page.keyboard.press("ArrowDown");
    await waitMedia(page, "d.volume === '0.95'");
    await page.keyboard.press("ArrowUp");
    await waitMedia(page, "d.volume === '1.00'");
    await page.keyboard.press("KeyM");
    await waitMedia(page, "d.muted === 'true'");
    await page.keyboard.press("KeyM");
    await waitMedia(page, "d.muted === 'false'");

    // control bar: play button, speed menu, loop toggle
    await page.click(".mv-bar .btn[title^='Play']");
    await waitMedia(page, "d.paused === 'false'");
    await page.click(".mv-bar .btn[title='Playback Speed']");
    await page.waitForSelector(".popover .menu-item");
    await page.evaluate(() => [...document.querySelectorAll(".popover .menu-item")].find((b) => b.textContent.includes("2×")).click());
    await waitMedia(page, "d.rate === '2'");
    await page.click(".mv-bar .btn[title='Loop']");
    await waitMedia(page, "d.loop === 'true'");
    // clicking the seek bar jumps there
    const bar = await (await page.$(".mv-slider.seek")).boundingBox();
    await page.mouse.click(bar.x + bar.width * 0.5, bar.y + bar.height / 2);
    await waitMedia(page, "Math.abs(Number(d.current) - 15) < 1.5");
    await page.keyboard.press("Space");
    assert.deepEqual(page.errors, []);
    await page.close();
  });

  test("audio Get Info: duration, container, codec, channels, sample rate", async () => {
    const page = await openMedia("/tone.wav");
    const rows = await infoRows(page);
    assert.equal(rows["Media/Duration"], "0:30");
    assert.equal(rows["Media/Container"], "WAV");
    assert.equal(rows["Media/Audio"], "PCM, 16-bit · mono · 8 kHz · 128 kb/s");
    assert.equal(rows["File/File name"], "tone.wav");
    await page.close();
  });

  test("video: WebM plays, shows resolution, auto-hides the control bar, Get Info has codecs", async (t) => {
    if (!ctx.webm) return t.skip("MediaRecorder produced no WebM");
    const page = await openMedia("/clip.webm");
    const d = await media(page);
    assert.equal(d.audio, "false");
    assert.equal(d.width, "320");
    assert.equal(d.height, "240");
    assert.match(await page.$eval(".subtitle", (e) => e.textContent), /^320 × 240 · 0:0\d$/);
    // the file reports no duration until the viewer has seen the end: it must have been fixed
    assert.ok(Number.isFinite(Number(d.duration)) && Number(d.duration) > 0.5, d.duration);

    await page.keyboard.press("Space");
    await waitMedia(page, "d.paused === 'false'");
    await page.mouse.move(300, 300);
    await page.mouse.move(320, 310);
    await sleep(2600);
    if ((await media(page)).paused === "false") {
      assert.ok(await page.$eval(".media-view", (e) => e.classList.contains("hide-controls")), "bar hides while playing");
      await page.mouse.move(500, 400);
      await page.waitForFunction(() => !document.querySelector(".media-view").classList.contains("hide-controls"));
    }

    // click on the picture toggles playback; the PiP and full screen buttons exist
    assert.ok(await page.$(".mv-bar .btn[title='Full Screen (F)']"));
    assert.ok(await page.$(".mv-bar .btn[title='Picture in Picture']"));
    await page.evaluate(() => document.querySelector("video").pause());
    await waitMedia(page, "d.paused === 'true'");
    await page.mouse.click(500, 300);
    await waitMedia(page, "d.paused === 'false' || d.state === 'ended'");
    await page.evaluate(() => document.querySelector("video").pause());

    const rows = await infoRows(page);
    assert.equal(rows["Media/Container"], "WebM");
    assert.match(rows["Media/Video"], /^320 × 240 · .*VP[89]/);
    assert.match(rows["Media/Audio"], /^Opus/);
    await page.close();
  });

  test("full screen with F (and back)", async (t) => {
    if (!ctx.webm) return t.skip("no WebM");
    const page = await openMedia("/clip.webm");
    await page.keyboard.press("KeyF");
    const fs = await page.waitForFunction(() => document.querySelector("[data-viewer=media]").dataset.fullscreen === "true", { timeout: 5000 }).then(() => true, () => false);
    if (fs) {
      await page.keyboard.press("KeyF");
      await waitMedia(page, "d.fullscreen === 'false'");
    } else t.diagnostic("headless Chrome refused full screen; not asserted");
    await page.close();
  });

  test("local files via file:// play, and a broken file shows an error page", async () => {
    const access = await optionsPage.evaluate(() => chrome.extension.isAllowedFileSchemeAccess());
    if (access) {
      const page = await openMedia(pathToFileURL(join(tmp, "tone.wav")).href);
      const rows = await infoRows(page);
      assert.equal(rows["Media/Container"], "WAV");
      await page.keyboard.press("Space");
      await waitMedia(page, "d.paused === 'false'");
      await page.close();
    }
    const page = await ctx.browser.newPage();
    await page.goto(viewerUrl("media", `${server.origin}/broken.mp4`));
    await page.waitForSelector('[data-viewer="media"][data-state="error"]', { timeout: 15000 });
    assert.match(await page.$eval(".mv-error", (e) => e.textContent), /Can’t play this file/);
    await page.close();
  });

  test("with media interception on, a .wav URL opens in View", async () => {
    const kinds = { pdf: true, image: false, model: true, table: true, text: false, media: true };
    await optionsPage.evaluate(async (intercept) => {
      await chrome.storage.sync.set({ prefs: { intercept, respectAttachment: true, theme: "system" } });
      await chrome.runtime.sendMessage({ type: "sync-rules" });
    }, kinds);
    try {
      const page = await ctx.browser.newPage();
      const url = await open(page, `${server.origin}/tone.wav`);
      assert.ok(url.startsWith(`${ctx.viewer}?kind=media&url=`), url);
      await page.waitForSelector('[data-viewer="media"][data-duration]', { timeout: 15000 });
      await page.close();
    } finally {
      await optionsPage.evaluate(() => chrome.storage.sync.remove("prefs").then(() => chrome.runtime.sendMessage({ type: "sync-rules" })));
    }
  });
});
