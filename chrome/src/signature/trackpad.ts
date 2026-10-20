// "Trackpad" pen: the closest browser substitute for the desktop's absolute
// touchpad capture (preview/touchpad.py). Browsers expose no absolute touchpad
// contacts, so the pointer is locked (Pointer Lock) to the drawing element and
// relative movementX/Y are integrated into a virtual pen position, with OS
// acceleration turned off (`unadjustedMovement`) where the platform allows.
//
// Pen down:  "swipe to sign" (the default, like the desktop): every movement
//            draws, and a pause longer than PAUSE_MS ends the stroke. Lifting
//            the finger is invisible to the page; the movement events simply
//            stop, so the pause is the pen-up signal. Taps don't make dots.
//            Holding Shift moves the pen without ink (to start a new word: a
//            relative pen can't jump to where the finger lands).
//            Space switches to click-drag mode (ink only while the click is held).
// Ends on:   Escape (the browser releases the lock), Enter or any other
//            non-modifier key, losing the lock / focus, or 45 s idle.
//            Backspace undoes the last stroke.

export const PAUSE_MS = 150;
const IDLE_MS = 45_000;
const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "AltGraph", "CapsLock", "OS"]);

export interface PenRect { x0: number; y0: number; x1: number; y1: number }

export interface RelativePenOptions {
  /** Element that gets the pointer lock. */
  target: Element;
  /** Allowed area, in the caller's coordinates (px scaled by `gain`). */
  bounds(): PenRect;
  /** Caller units per CSS px of movement. */
  gain: number;
  start?: { x: number; y: number };
  /** Pen position changed (also while up, to show the cursor). `speed` is px/s of raw movement. */
  onMove(x: number, y: number, down: boolean, speed: number): void;
  onDown(x: number, y: number): void;
  onUp(): void;
  onUndo?(): void;
  /** The session ended (lock released); `reason` = "key" | "lost" | "idle" | "stop". */
  onEnd(reason: string, key?: string): void;
  onModeChange?(hoverInk: boolean): void;
}

export class RelativePen {
  x = 0;
  y = 0;
  down = false;
  hoverInk = true;
  active = false;
  private buttons = false;
  private pause = 0;
  private idle = 0;
  private lastT = 0;
  private ac = new AbortController();

  private o: RelativePenOptions;

  constructor(o: RelativePenOptions) {
    this.o = o;
    const b = o.bounds();
    this.x = o.start?.x ?? (b.x0 + b.x1) / 2;
    this.y = o.start?.y ?? (b.y0 + b.y1) / 2;
  }

  /** Request the lock; resolves false if the browser refused it. */
  async start(): Promise<boolean> {
    const t = this.o.target;
    try {
      await t.requestPointerLock({ unadjustedMovement: true });
    } catch {
      try { await t.requestPointerLock(); } catch { return false; }
    }
    if (document.pointerLockElement !== t) return false;
    this.active = true;
    const sig = { signal: this.ac.signal, capture: true };
    document.addEventListener("pointerlockchange", () => {
      if (document.pointerLockElement !== t) this.end("lost");
    }, sig);
    document.addEventListener("pointermove", (e) => this.move(e), sig);
    document.addEventListener("pointerdown", (e) => {
      e.preventDefault(); e.stopPropagation();
      this.buttons = true;
      if (!this.hoverInk) this.penDown();
    }, sig);
    document.addEventListener("pointerup", (e) => {
      e.preventDefault(); e.stopPropagation();
      this.buttons = false;
      if (!this.hoverInk) this.penUp();
    }, sig);
    addEventListener("keydown", (e) => this.key(e), sig);
    addEventListener("blur", () => this.end("lost"), sig);
    this.bumpIdle();
    this.o.onMove(this.x, this.y, false, 0);
    return true;
  }

  stop(): void { this.end("stop"); }

  private end(reason: string, key?: string): void {
    if (!this.active) return;
    this.active = false;
    this.penUp();
    clearTimeout(this.pause);
    clearTimeout(this.idle);
    this.ac.abort();
    if (document.pointerLockElement === this.o.target) document.exitPointerLock();
    this.o.onEnd(reason, key);
  }

  private bumpIdle() {
    clearTimeout(this.idle);
    this.idle = window.setTimeout(() => this.end("idle"), IDLE_MS);
  }

  private penDown() {
    if (this.down) return;
    this.down = true;
    this.o.onDown(this.x, this.y);
  }

  private penUp() {
    if (!this.down) return;
    this.down = false;
    this.o.onUp();
  }

  private move(e: PointerEvent) {
    const evs = e.getCoalescedEvents?.().length ? e.getCoalescedEvents() : [e];
    for (const ev of evs) {
      const dx = ev.movementX, dy = ev.movementY;
      if (!dx && !dy) continue;
      const now = ev.timeStamp || performance.now();
      const dt = Math.max((now - this.lastT) / 1000, 0.004);
      this.lastT = now;
      const speed = Math.hypot(dx, dy) / dt;
      const b = this.o.bounds();
      this.x = Math.min(Math.max(this.x + dx * this.o.gain, b.x0), b.x1);
      this.y = Math.min(Math.max(this.y + dy * this.o.gain, b.y0), b.y1);
      if (ev.shiftKey) this.penUp();           // reposition without ink
      else if (this.hoverInk && !this.down) this.penDown();
      this.o.onMove(this.x, this.y, this.down, speed);
    }
    this.bumpIdle();
    if (this.hoverInk && !this.buttons) {
      clearTimeout(this.pause);
      this.pause = window.setTimeout(() => this.penUp(), PAUSE_MS);
    }
  }

  private key(e: KeyboardEvent) {
    if (MODIFIERS.has(e.key)) return;
    e.preventDefault();
    e.stopPropagation();
    this.bumpIdle();
    if (e.key === " ") {
      this.hoverInk = !this.hoverInk;
      if (!this.hoverInk && !this.buttons) this.penUp();
      this.o.onModeChange?.(this.hoverInk);
      return;
    }
    if (e.key === "Backspace" || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z")) {
      this.penUp();
      this.o.onUndo?.();
      return;
    }
    this.end("key", e.key);
  }
}

export const pointerLockSupported = () => typeof Element !== "undefined" && "requestPointerLock" in Element.prototype;
