// Markup + signature UI styles. Injected once at runtime (the
// build only bundles viewer CSS for src/viewers/<kind>/style.css), using the
// theme tokens from ui/theme.css so light and dark both work.
// Tool icons live in ui/icons.ts (shared icon set).

const CSS = `
.markup-toolbar { display: flex; align-items: center; gap: 2px; flex-wrap: wrap; padding: 3px 6px; }
.markup-toolbar .btn { min-width: 32px; height: 32px; padding: 0 7px; }
.markup-toolbar .btn.mk-chev { min-width: 20px; padding: 0 2px; }
.markup-toolbar .btn.mk-chev .icon { width: 12px; height: 12px; }
.mk-swatch-btn .mk-swatch { width: 16px; height: 16px; border-radius: 50%; box-shadow: inset 0 0 0 1px var(--border); }
.mk-swatch-btn .mk-swatch.mk-ring { background: transparent !important; box-shadow: inset 0 0 0 3px currentColor; }
.mk-swatch.mk-none { background: linear-gradient(135deg, transparent 45%, var(--destructive) 45%, var(--destructive) 55%, transparent 55%), var(--view-bg); }
.mk-pop { position: fixed; z-index: 55; min-width: 0; display: flex; flex-direction: column; gap: 6px; max-height: calc(100vh - 16px); overflow: auto; }
.mk-grid { display: grid; grid-template-columns: repeat(6, 26px); gap: 6px; padding: 4px; }
.mk-grid.mk-shapes { grid-template-columns: repeat(4, 34px); }
.mk-chip { width: 26px; height: 26px; border-radius: 50%; border: 0; padding: 0; box-shadow: inset 0 0 0 1px var(--border); }
.mk-chip[aria-pressed="true"] { outline: 2px solid var(--accent); outline-offset: 2px; }
.mk-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.mk-row { display: flex; align-items: center; gap: 8px; padding: 2px 4px; }
.mk-row label { color: var(--dim-fg); font-size: 12px; min-width: 52px; }
.mk-row select, .mk-row input[type="range"] { flex: 1; }
.mk-pop select { font: inherit; color: inherit; background: var(--view-bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 3px 6px; }
.mk-width-item svg { width: 60px; height: 12px; }
.mk-sig-list { display: flex; flex-direction: column; gap: 4px; min-width: 260px; }
.mk-sig-row { display: flex; align-items: center; gap: 4px; }
.mk-sig-thumb { flex: 1; height: 64px; border: 1px solid var(--border); border-radius: var(--radius); background: #fff; padding: 4px 8px; display: flex; align-items: center; justify-content: center; }
.mk-sig-thumb:hover { box-shadow: 0 0 0 2px var(--accent); }
.mk-sig-thumb svg { max-width: 100%; max-height: 100%; }
.mk-empty { color: var(--dim-fg); text-align: center; padding: 8px; }
.mk-wide { width: 100%; justify-content: center; background: var(--hover); }

.mk-layer { position: absolute; left: 0; top: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; touch-action: none; }
.mk-layer.mk-drawing { pointer-events: all; cursor: crosshair; }
.mk-layer.mk-drawing.mk-text-tool { cursor: text; }
.mk-layer.mk-locked { cursor: none; }
.mk-under { mix-blend-mode: multiply; }
.mk-hit { cursor: move; }
.mk-layer.mk-drawing .mk-hit, .mk-layer.mk-drawing .mk-chrome { pointer-events: none !important; }
.mk-note .mk-hit { cursor: pointer; }
.mk-sel-outline { fill: none; stroke: var(--accent, #3584e4); }
.mk-handle { fill: #fff; stroke: var(--accent, #3584e4); }
.mk-delete { fill: rgba(56, 56, 56, 0.92); cursor: pointer; }
.mk-delete-x { stroke: #fff; fill: none; pointer-events: none; }
.mk-pad-rect { fill: var(--accent, #3584e4); fill-opacity: 0.08; stroke: var(--accent, #3584e4); }
.mk-pad-finger { fill: none; stroke: var(--accent, #3584e4); }

.mk-text-editor { position: fixed; z-index: 54; margin: 0; border: 1px dashed var(--accent); outline: none; resize: none;
  background: transparent; overflow: hidden; white-space: pre-wrap; overflow-wrap: anywhere; box-sizing: border-box; }
.mk-note-pop { width: 260px; }
.mk-note-pop h3 { margin: 2px 4px; font-size: 13px; }
.mk-note-pop textarea { font: inherit; color: inherit; background: var(--view-bg); border: 1px solid var(--border);
  border-radius: var(--radius); min-height: 90px; max-height: 240px; padding: 6px; resize: vertical; }
.mk-hint { color: var(--dim-fg); font-size: 12px; padding: 0 4px; }

.sig-dialog { max-width: 780px; }
.sig-dialog .dialog-header { padding: 8px 8px 0 8px; }
.sig-dialog .dialog-title { padding-left: 0; }
.sig-dialog .dialog-header .btn { padding: 0 14px; background: var(--hover); }
.sig-dialog .dialog-header .btn.suggested { background: var(--accent); }
.sig-status { color: var(--dim-fg); text-align: center; min-height: 20px; margin: 0 0 10px; }
.sig-canvas { display: block; width: 100%; aspect-ratio: 13 / 8; touch-action: none; border-radius: 14px; cursor: crosshair; }
.sig-canvas.sig-live { cursor: none; }
.sig-tools { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
.sig-tools .spacer { flex: 1; }
.sig-tools .linked .btn { background: var(--hover); }
.sig-tools .linked .btn[aria-pressed="true"] { background: var(--active); }
.sig-ink { width: 16px; height: 16px; border-radius: 50%; }
`;

export function injectStyle(): void {
  if (document.getElementById("markup-style")) return;
  const st = document.createElement("style");
  st.id = "markup-style";
  st.textContent = CSS;
  document.head.append(st);
}

/** A small popover anchored under `anchor`; closes on outside click / Escape. */
export function popover(anchor: HTMLElement, content: HTMLElement[], onClose?: () => void): { el: HTMLElement; close: () => void } {
  document.querySelectorAll(".mk-pop").forEach((p) => (p as HTMLElement & { _close?: () => void })._close?.());
  const pop = document.createElement("div") as HTMLDivElement & { _close?: () => void };
  pop.className = "popover mk-pop";
  pop.setAttribute("role", "dialog");
  pop.append(...content);
  document.body.append(pop);
  const r = anchor.getBoundingClientRect();
  const pw = pop.offsetWidth;
  pop.style.top = `${Math.min(r.bottom + 6, innerHeight - pop.offsetHeight - 8)}px`;
  pop.style.left = `${Math.max(8, Math.min(r.left, innerWidth - pw - 8))}px`;
  const outside = (e: Event) => {
    const t = e.target as Node;
    if (!pop.contains(t) && !anchor.contains(t)) close();
  };
  const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); close(); anchor.focus(); } };
  const close = () => {
    if (!pop.isConnected) return;
    pop.remove();
    removeEventListener("pointerdown", outside, true);
    removeEventListener("keydown", esc, true);
    onClose?.();
  };
  pop._close = close;
  setTimeout(() => {
    addEventListener("pointerdown", outside, true);
    addEventListener("keydown", esc, true);
  });
  (pop.querySelector<HTMLElement>("[aria-pressed='true'],button,select,input,textarea"))?.focus();
  return { el: pop, close };
}
