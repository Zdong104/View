// "New Signature" dialog (signature.py SignatureCaptureWindow): sign in the
// paper box with a mouse, pen (pressure, coalesced events) or finger, or in
// trackpad mode (Pointer Lock, relative: see trackpad.ts). Strokes are kept in
// paper millimetres with a pressure per point (real pen pressure, else derived
// from speed: fast = thin, slow = thick, like ink) and normalized on save.
import type { StrokePoints } from "../markup/api.ts";
import { inkOutline, speedPressure, triples } from "../markup/geom.ts";
import { primsToCanvas } from "../markup/render.ts";
import { el, icon as mkIcon } from "../ui/kit.ts";
import { injectStyle } from "../markup/style.ts";
import { normalizeStrokes, type StoredSignature } from "./index.ts";
import { pointerLockSupported, RelativePen } from "./trackpad.ts";

const INKS: [string, string][] = [["Black", "#141414"], ["Blue", "#1c3f94"], ["Red", "#a51d2d"]];
const PENS: [string, number][] = [["Fine", 0.6], ["Medium", 0.9], ["Bold", 1.3]];
const PAD_W = 130, PAD_H = 80; // paper size in mm (the desktop's fallback pad size)
const MARGIN = 1.5;
const MIN_STEP = 0.12;
const VMAX = 260; // mm/s for the thinnest line

type Result = Omit<StoredSignature, "id" | "created">;

export function captureSignature(mode: "pointer" | "trackpad"): Promise<Result | null> {
  injectStyle();
  return new Promise((resolve) => {
    let strokes: StrokePoints[] = [];
    let color = INKS[0]![1];
    let pen = PENS[1]![1];
    let live = false;
    let finger: { x: number; y: number } | null = null;
    let trackpad = mode === "trackpad" && pointerLockSupported();
    let pad: RelativePen | null = null;
    let result: Result | null = null;

    const dlg = el("dialog.dialog.sig-dialog", { "aria-label": "New Signature" });
    const cancelBtn = el("button.btn", { type: "button", text: "Cancel", "data-action": "cancel" });
    const saveBtn = el("button.btn.suggested", { type: "button", text: "Save", "data-action": "save", disabled: true });
    const status = el("p.sig-status", { role: "status" });
    const canvas = el("canvas.sig-canvas", { "aria-label": "Signature pad", tabindex: 0 });
    const ctx = canvas.getContext("2d")!;

    const toggleGroup = <T,>(items: [string, T][], cur: T, label: (n: string, v: T) => HTMLElement | string, set: (v: T) => void) => {
      const box = el("div.linked", { role: "radiogroup" });
      const btns = items.map(([name, v]) => {
        const lab = label(name, v);
        const b = el("button.btn", { type: "button", title: name, "aria-label": name, "aria-pressed": String(v === cur) }, lab);
        b.addEventListener("click", () => { btns.forEach((x) => x.setAttribute("aria-pressed", String(x === b))); set(v); });
        return b;
      });
      box.append(...btns);
      return box;
    };
    const inks = toggleGroup(INKS, color, (_n, c) => {
      const dot = el("span.sig-ink");
      dot.style.background = c;
      return dot;
    }, (c) => { color = c; draw(); });
    const pens = toggleGroup(PENS, pen, (n) => n, (w) => { pen = w; draw(); });
    const source = toggleGroup<"pointer" | "trackpad">([["Mouse or Pen", "pointer"], ["Trackpad", "trackpad"]],
      trackpad ? "trackpad" : "pointer", (n) => n, (m) => setMode(m === "trackpad"));
    source.hidden = !pointerLockSupported();
    source.dataset.action = "source";
    const undoBtn = el("button.btn", { type: "button", title: "Undo Stroke (Backspace)", "aria-label": "Undo Stroke", "data-action": "undo" }, mkIcon("undo"));
    const clearBtn = el("button.btn", { type: "button", title: "Clear (Delete)", "aria-label": "Clear", "data-action": "clear" }, mkIcon("clear"));
    const startBtn = el("button.btn.suggested.pill", { type: "button", text: "Start", "data-action": "start" });

    dlg.append(
      el("div.dialog-header", {}, cancelBtn, el("h2.dialog-title", { text: "New Signature" }), saveBtn),
      el("div.dialog-body", {}, status, canvas,
        el("div.sig-tools", {}, inks, pens, el("div.spacer"), source, undoBtn, clearBtn, startBtn)),
    );

    // ---------------------------------------------------------- drawing ---
    const geom = () => {
      const r = canvas.getBoundingClientRect();
      return { r, s: r.width / PAD_W };
    };
    const toMm = (cx: number, cy: number) => {
      const { r, s } = geom();
      return {
        x: Math.min(Math.max((cx - r.left) / s, MARGIN), PAD_W - MARGIN),
        y: Math.min(Math.max((cy - r.top) / s, MARGIN), PAD_H - MARGIN),
      };
    };
    function draw() {
      const { r, s } = geom();
      const dpr = devicePixelRatio || 1;
      const W = Math.max(1, Math.round(r.width * dpr)), H = Math.max(1, Math.round(r.height * dpr));
      if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, r.width, r.height);
      const pw = r.width, ph = r.height;
      ctx.beginPath();
      ctx.roundRect(0.5, 0.5, pw - 1, ph - 1, 14);
      ctx.fillStyle = "#fff";
      ctx.fill();
      const accent = getComputedStyle(dlg).getPropertyValue("--accent").trim() || "#3584e4";
      ctx.strokeStyle = live ? accent : "rgba(0,0,0,0.18)";
      ctx.lineWidth = live ? 3 : 1;
      ctx.stroke();
      // baseline with an "x", like a paper form
      const base = ph * 0.72, bx = pw * 0.08;
      ctx.strokeStyle = "rgba(0,0,0,0.25)";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(bx, base); ctx.lineTo(pw * 0.92, base); ctx.stroke();
      ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.moveTo(bx, base - 18); ctx.lineTo(bx + 12, base - 6); ctx.moveTo(bx + 12, base - 18); ctx.lineTo(bx, base - 6); ctx.stroke();
      if (!strokes.length && !live) {
        ctx.fillStyle = "rgba(0,0,0,0.45)";
        ctx.font = "15px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(trackpad ? "Click Start (or press Space), then swipe on the trackpad to sign"
          : "Sign here with your mouse, pen or finger", pw / 2, ph * 0.42);
        ctx.textAlign = "start";
      }
      ctx.save();
      ctx.scale(s, s);
      primsToCanvas(ctx, strokes.filter((st) => st.length).map((st) => ({
        t: "path" as const, cmds: inkOutline(triples(st), pen), fill: color, stroke: null, width: 0,
      })), 1);
      if (live && finger) {
        ctx.fillStyle = accent;
        ctx.globalAlpha = 0.45;
        ctx.beginPath(); ctx.arc(finger.x, finger.y, 2.2, 0, Math.PI * 2); ctx.fill();
      }
      ctx.restore();
    }

    function sync() {
      const has = strokes.length > 0;
      saveBtn.disabled = !has || live;
      undoBtn.disabled = !has;
      clearBtn.disabled = !has;
      startBtn.hidden = !trackpad;
      startBtn.textContent = live ? "Done" : has ? "Sign Again" : "Start";
      canvas.classList.toggle("sig-live", live);
      draw();
    }

    function setStatus(t: string) { status.textContent = t; }

    function setMode(tp: boolean) {
      pad?.stop();
      trackpad = tp;
      setStatus(tp
        ? "Trackpad mode: click Start, then swipe with one finger to sign, no clicking. Lift your finger to end a stroke; hold Shift to move without ink. Any key ends."
        : "Sign in the box with your mouse, a pen or a finger. Backspace undoes.");
      sync();
    }

    // ------------------------------------------------- mouse / pen / touch ---
    let cur: { id: number; stroke: StrokePoints; lastT: number; last: { x: number; y: number }; p: number | null } | null = null;
    const pressureOf = (e: PointerEvent, prev: number | null, v: number) =>
      e.pointerType === "pen" && e.pressure > 0 ? e.pressure : speedPressure(prev, v, VMAX);
    canvas.addEventListener("pointerdown", (e) => {
      if (trackpad || live || (e.pointerType === "mouse" && e.button !== 0)) return;
      e.preventDefault();
      canvas.setPointerCapture(e.pointerId);
      const p = toMm(e.clientX, e.clientY);
      const pr = pressureOf(e, null, 0);
      cur = { id: e.pointerId, stroke: [p.x, p.y, pr], lastT: e.timeStamp, last: p, p: pr };
      strokes.push(cur.stroke);
      sync();
    });
    canvas.addEventListener("pointermove", (e) => {
      if (!cur || e.pointerId !== cur.id) return;
      const evs = e.getCoalescedEvents?.().length ? e.getCoalescedEvents() : [e];
      for (const ev of evs) {
        const p = toMm(ev.clientX, ev.clientY);
        const d = Math.hypot(p.x - cur.last.x, p.y - cur.last.y);
        if (d < MIN_STEP) continue;
        const v = d / Math.max((ev.timeStamp - cur.lastT) / 1000, 0.004);
        cur.p = pressureOf(ev, cur.p, v);
        cur.stroke.push(p.x, p.y, cur.p);
        cur.last = p;
        cur.lastT = ev.timeStamp;
      }
      draw();
    });
    const endStroke = (e: PointerEvent) => {
      if (!cur || e.pointerId !== cur.id) return;
      cur = null;
      sync();
    };
    canvas.addEventListener("pointerup", endStroke);
    canvas.addEventListener("pointercancel", endStroke);

    // ----------------------------------------------------------- trackpad ---
    let history: StrokePoints[] = [];
    async function start() {
      if (!trackpad || live) return;
      history = strokes.map((s) => s.slice());
      let stroke: StrokePoints | null = null;
      let pr: number | null = null;
      const { s } = geom();
      pad = new RelativePen({
        target: canvas,
        gain: 1 / s,
        bounds: () => ({ x0: MARGIN, y0: MARGIN, x1: PAD_W - MARGIN, y1: PAD_H - MARGIN }),
        start: { x: PAD_W * 0.15, y: PAD_H * 0.6 },
        onDown: (x, y) => { pr = speedPressure(null, 0, VMAX); stroke = [x, y, pr]; strokes.push(stroke); sync(); },
        onMove: (x, y, down, speed) => {
          finger = { x, y };
          if (down && stroke) {
            const n = stroke.length;
            if (Math.hypot(x - stroke[n - 3]!, y - stroke[n - 2]!) >= MIN_STEP) {
              pr = speedPressure(pr, speed / geom().s, VMAX);
              stroke.push(x, y, pr);
            }
          }
          draw();
        },
        onUp: () => { stroke = null; sync(); },
        onUndo: () => { strokes.pop(); sync(); },
        onModeChange: (hover) => setStatus(hover
          ? "Swipe to sign: just move your finger to draw. Space: click-drag instead."
          : "Click-drag mode: hold the click and move to draw. Space: swipe to sign."),
        onEnd: (reason, key) => {
          live = false;
          finger = null;
          pad = null;
          if (reason === "key" && key === "Escape") strokes = history;
          setStatus(reason === "idle" ? "Stopped after 45 s without movement. Press Space to continue."
            : strokes.length ? "Looks good? Click Save. Or Sign Again to add more strokes." : "Click Start (or press Space), then swipe on the trackpad to sign.");
          sync();
        },
      });
      live = true;
      setStatus("Starting…");
      sync();
      if (!(await pad.start())) {
        live = false;
        pad = null;
        setStatus("The browser did not allow locking the pointer. Click Start again, or use Mouse or Pen.");
        sync();
        return;
      }
      setStatus("Swipe to sign, no clicking · Shift: move without ink · Backspace undoes · Enter or Esc ends");
    }

    // -------------------------------------------------------- buttons/keys ---
    const close = (r: Result | null) => { result = r; pad?.stop(); dlg.close(); };
    const save = () => {
      if (!strokes.length || live) return;
      const n = normalizeStrokes(strokes, pen);
      close({ name: `Signature ${new Date().toLocaleString()}`, aspect: n.aspect, strokes: n.strokes, color, pen: n.pen });
    };
    cancelBtn.addEventListener("click", () => close(null));
    saveBtn.addEventListener("click", save);
    undoBtn.addEventListener("click", () => { strokes.pop(); sync(); });
    clearBtn.addEventListener("click", () => { strokes = []; sync(); });
    startBtn.addEventListener("click", () => { if (live) pad?.stop(); else void start(); });
    dlg.addEventListener("keydown", (e) => {
      if (live) return; // the trackpad session handles keys
      const t = e.target as HTMLElement;
      const onButton = t.tagName === "BUTTON" && t !== canvas;
      if (e.key === "Backspace" || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z")) { e.preventDefault(); strokes.pop(); sync(); }
      else if (e.key === "Delete") { e.preventDefault(); strokes = []; sync(); }
      else if (e.key === " " && trackpad && !onButton) { e.preventDefault(); void start(); }
      else if (e.key === "Enter" && !onButton && strokes.length) { e.preventDefault(); save(); }
    });
    dlg.addEventListener("cancel", (e) => { if (live) e.preventDefault(); });
    dlg.addEventListener("close", () => {
      ro.disconnect();
      dlg.remove();
      resolve(result);
    }, { once: true });
    const ro = new ResizeObserver(() => draw());
    ro.observe(canvas);

    document.body.append(dlg);
    dlg.showModal();
    setMode(trackpad);
    canvas.focus();
  });
}
