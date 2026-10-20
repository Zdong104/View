// The shared Markup toolbar (markup.py MarkupToolbar + signature.py
// SignatureButton): tools, shape chooser, colours, line width, font, the
// signature menu and trackpad drawing. The viewer places `el`.
import type { Tool } from "./api.ts";
import type { Markup } from "./controller.ts";
import { defaultFont, SHAPES } from "./geom.ts";
import { popover } from "./style.ts";
import type { IconName } from "../ui/icons.ts";
import { el, icon as mkIcon } from "../ui/kit.ts";
import { pointerLockSupported } from "../signature/trackpad.ts";
import { signatureSvg, signaturePng } from "../signature/index.ts";

const TOOL_NAMES: Record<string, string> = {
  select: "Select", sketch: "Sketch (shapes are recognised)", draw: "Draw (ink)", line: "Line", arrow: "Arrow",
  rect: "Rectangle", rrect: "Rounded Rectangle", oval: "Oval", bubble: "Speech Bubble", star: "Star",
  text: "Text Box", note: "Note", highlight: "Highlight Text", underline: "Underline Text", strike: "Strike Through Text",
};
const COLORS = ["#000000", "#5e5c64", "#9a9996", "#ffffff", "#d62828", "#ff7800", "#f6d32d", "#33d17a",
  "#3584e4", "#1c3f94", "#9141ac", "#e66100"];
const WIDTHS = [0.5, 1, 2, 3, 5, 8];
const FAMILIES: [string, string][] = [["sans-serif", "Sans"], ["serif", "Serif"], ["monospace", "Monospace"],
  ["Helvetica", "Helvetica"], ["Times New Roman", "Times New Roman"], ["Courier New", "Courier New"], ["Georgia", "Georgia"]];
const SIZES = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48, 64, 72];

function iconButton(icon: IconName, title: string, onClick?: (e: MouseEvent) => void): HTMLButtonElement {
  const b = el("button.btn", { type: "button", title, "aria-label": title });
  b.append(mkIcon(icon));
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

export class Toolbar {
  readonly el: HTMLDivElement;
  private toolButtons = new Map<string, HTMLButtonElement>();
  private textTools: HTMLElement[] = [];
  private shape: typeof SHAPES[number] = "rect";
  private shapeBtn: HTMLButtonElement;
  private strokeSwatch: HTMLSpanElement;
  private fillSwatch: HTMLSpanElement;
  private padBtn: HTMLButtonElement;
  private offs: (() => void)[] = [];

  private ctrl: Markup;

  constructor(ctrl: Markup) {
    this.ctrl = ctrl;
    const bar = el("div.markup-toolbar", { role: "toolbar", "aria-label": "Markup" });
    this.el = bar;
    bar.append(this.tool("select", "select"));
    for (const k of ["highlight", "underline", "strike"] as const) {
      const b = this.tool(k, k);
      this.textTools.push(b);
      bar.append(b);
    }
    const sep1 = el("div.hsep", { role: "separator" });
    this.textTools.push(sep1);
    bar.append(sep1, this.tool("sketch", "sketch"), this.tool("draw", "draw"));

    // shapes: one toggle showing the last shape + a chooser
    this.shapeBtn = this.tool("shape", "rect");
    const chooser = iconButton("chevron", "Choose Shape", () => this.chooseShape(chooser));
    chooser.classList.add("mk-chev");
    chooser.setAttribute("aria-haspopup", "dialog");
    chooser.dataset.action = "shapes";
    bar.append(el("div.linked", {}, this.shapeBtn, chooser), this.tool("text", "text"), this.tool("note", "note"));

    const sigBtn = iconButton("signature", "Sign", () => void this.signatureMenu(sigBtn));
    sigBtn.setAttribute("aria-haspopup", "dialog");
    sigBtn.dataset.action = "signature";
    bar.append(sigBtn, el("div.hsep", { role: "separator" }));

    // style controls
    this.strokeSwatch = el("span.mk-swatch");
    const strokeBtn = el("button.btn.mk-swatch-btn", { type: "button", title: "Border / Ink Colour", "aria-label": "Border / Ink Colour", "data-action": "stroke" }, this.strokeSwatch);
    strokeBtn.addEventListener("click", () => this.colorMenu(strokeBtn, "stroke"));
    this.fillSwatch = el("span.mk-swatch");
    const fillBtn = el("button.btn.mk-swatch-btn", { type: "button", title: "Fill Colour", "aria-label": "Fill Colour", "data-action": "fill" }, this.fillSwatch);
    fillBtn.addEventListener("click", () => this.colorMenu(fillBtn, "fill"));
    const widthBtn = iconButton("width", "Line Width", () => this.widthMenu(widthBtn));
    widthBtn.dataset.action = "width";
    const fontBtn = iconButton("font", "Font", () => this.fontMenu(fontBtn));
    fontBtn.dataset.action = "font";
    this.padBtn = iconButton("trackpad", "Draw with Trackpad: swipe with one finger to draw (no clicking), hold Shift to move without ink, any key ends", () => void ctrl.startTrackpad());
    this.padBtn.dataset.action = "trackpad";
    bar.append(strokeBtn, fillBtn, widthBtn, fontBtn, this.padBtn);

    this.offs.push(ctrl.listen("tool", () => this.sync()), ctrl.listen("selection", () => this.sync()),
      ctrl.listen("style", () => this.sync()), ctrl.listen("surfaces", () => this.sync()));
    this.sync();
  }

  destroy(): void { this.offs.forEach((f) => f()); }

  private tool(key: string, icon: IconName): HTMLButtonElement {
    const b = iconButton(icon, key === "select" ? "Select (Esc)" : key === "shape" ? TOOL_NAMES[this.shape]! : TOOL_NAMES[key]!, () => {
      this.ctrl.setTool((key === "shape" ? this.shape : key) as Tool);
    });
    b.dataset.tool = key;
    b.setAttribute("aria-pressed", "false");
    this.toolButtons.set(key, b);
    return b;
  }

  private sync(): void {
    const ctrl = this.ctrl;
    const t = ctrl.tool;
    const key = (SHAPES as readonly string[]).includes(t) ? "shape" : t;
    if (key === "shape") this.setShape(t as typeof SHAPES[number]);
    for (const [k, b] of this.toolButtons) b.setAttribute("aria-pressed", String(k === key));
    const showText = ctrl.docKind === "pdf" || ctrl.hasTextRuns();
    this.textTools.forEach((e) => (e.hidden = !showText));
    const sel = ctrl.selected();
    const first = sel[0];
    const hl = t === "highlight" || (sel.length > 0 && sel.every((a) => a.type === "highlight"));
    this.strokeSwatch.style.background = hl ? ctrl.highlightColor : first?.style.stroke ?? ctrl.style.stroke;
    const fill = first ? first.style.fill : ctrl.style.fill;
    this.fillSwatch.classList.toggle("mk-none", !fill);
    this.fillSwatch.style.background = fill ?? "";
    this.padBtn.hidden = !((t === "sketch" || t === "draw") && pointerLockSupported());
    let active = false;
    for (const v of ctrl.views) active ||= v.trackpadActive;
    this.padBtn.setAttribute("aria-pressed", String(active));
  }

  private setShape(k: typeof SHAPES[number]) {
    this.shape = k;
    const b = this.shapeBtn;
    b.replaceChildren(mkIcon(k));
    b.title = TOOL_NAMES[k]!;
    b.setAttribute("aria-label", b.title);
    b.dataset.shape = k;
  }

  private chooseShape(anchor: HTMLElement) {
    const grid = el("div.mk-grid.mk-shapes", { role: "group", "aria-label": "Shapes" });
    const pop = popover(anchor, [grid]);
    for (const k of SHAPES) {
      const b = iconButton(k, TOOL_NAMES[k]!, () => { pop.close(); this.setShape(k); this.ctrl.setTool(k); });
      b.dataset.shape = k;
      b.setAttribute("aria-pressed", String(this.shape === k));
      grid.append(b);
    }
    grid.querySelector<HTMLElement>("[aria-pressed='true']")?.focus();
  }

  private colorMenu(anchor: HTMLElement, which: "stroke" | "fill") {
    const ctrl = this.ctrl;
    const first = ctrl.selected()[0];
    const cur = which === "stroke" ? (first?.style.stroke ?? ctrl.style.stroke) : (first ? first.style.fill : ctrl.style.fill);
    const grid = el("div.mk-grid", { role: "group", "aria-label": which === "stroke" ? "Colour" : "Fill" });
    const pick = (c: string | null) => {
      ctrl.applyStyle(which === "stroke" ? { stroke: c ?? "#000000" } : { fill: c });
      pop.close();
    };
    if (which === "fill") {
      const none = el("button.mk-chip.mk-swatch.mk-none", { type: "button", title: "No Fill", "aria-label": "No Fill", "aria-pressed": String(!cur), "data-color": "none" });
      none.addEventListener("click", () => pick(null));
      grid.append(none);
    }
    for (const c of COLORS) {
      const b = el("button.mk-chip", { type: "button", title: c, "aria-label": c, "aria-pressed": String(cur?.toLowerCase() === c), "data-color": c });
      b.style.background = c;
      b.addEventListener("click", () => pick(c));
      grid.append(b);
    }
    const custom = el("input", { type: "color", "aria-label": "Custom colour", value: /^#[0-9a-f]{6}$/i.test(cur ?? "") ? cur! : "#000000" });
    custom.addEventListener("change", () => pick(custom.value));
    const rows: HTMLElement[] = [grid, el("div.mk-row", {}, el("label", { text: "Custom" }), custom)];
    if (which === "stroke") {
      const op = el("input", { type: "range", min: 10, max: 100, step: 5, "aria-label": "Opacity",
        value: String(Math.round((first?.style.opacity ?? ctrl.style.opacity) * 100)) });
      op.addEventListener("change", () => ctrl.applyStyle({ opacity: Number(op.value) / 100 }));
      rows.push(el("div.mk-row", {}, el("label", { text: "Opacity" }), op));
    }
    const pop = popover(anchor, rows);
  }

  private widthMenu(anchor: HTMLElement) {
    const ctrl = this.ctrl;
    const cur = ctrl.selected()[0]?.style.width ?? ctrl.style.width;
    const items = WIDTHS.map((w) => {
      const line = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      line.setAttribute("viewBox", "0 0 60 12");
      line.innerHTML = `<path d="M2 6h56" stroke="currentColor" stroke-linecap="round" stroke-width="${Math.min(w, 10)}"/>`;
      const b = el("button.menu-item.mk-width-item", { type: "button", "aria-pressed": String(cur === w), "data-width": String(w) },
        line, el("span", { text: `${w} pt` }));
      b.addEventListener("click", () => { ctrl.applyStyle({ width: w }); pop.close(); });
      return b;
    });
    const pop = popover(anchor, items);
  }

  private fontMenu(anchor: HTMLElement) {
    const ctrl = this.ctrl;
    const first = ctrl.selected().find((a) => a.type === "text");
    const f = { ...defaultFont(), ...(first?.style.font ?? ctrl.style.font ?? {}) };
    const fam = el("select", { "aria-label": "Font family" });
    for (const [v, label] of FAMILIES) fam.append(el("option", { value: v, text: label }));
    if (!FAMILIES.some(([v]) => v === f.family)) fam.append(el("option", { value: f.family, text: f.family }));
    fam.value = f.family;
    const size = el("select", { "aria-label": "Font size" });
    for (const s of SIZES) size.append(el("option", { value: String(s), text: `${s} pt` }));
    if (!SIZES.includes(f.size)) size.append(el("option", { value: String(f.size), text: `${f.size} pt` }));
    size.value = String(f.size);
    const apply = () => ctrl.applyStyle({ font: { ...f } });
    fam.addEventListener("change", () => { f.family = fam.value; apply(); });
    size.addEventListener("change", () => { f.size = Number(size.value); apply(); });
    const toggle = (icon: IconName, title: string, get: () => boolean, set: (v: boolean) => void) => {
      const b = iconButton(icon, title);
      b.setAttribute("aria-pressed", String(get()));
      b.addEventListener("click", () => { set(!get()); b.setAttribute("aria-pressed", String(get())); apply(); });
      return b;
    };
    const aligns = (["left", "center", "right"] as const).map((al) => {
      const b = iconButton(al === "left" ? "alignLeft" : al === "center" ? "alignCenter" : "alignRight", `Align ${al}`);
      b.setAttribute("aria-pressed", String(f.align === al));
      b.addEventListener("click", () => {
        f.align = al;
        aligns.forEach((x, i) => x.setAttribute("aria-pressed", String(["left", "center", "right"][i] === al)));
        apply();
      });
      return b;
    });
    popover(anchor, [
      el("div.mk-row", {}, el("label", { text: "Font" }), fam),
      el("div.mk-row", {}, el("label", { text: "Size" }), size),
      el("div.mk-row", {},
        el("div.linked", {}, toggle("bold", "Bold", () => !!f.bold, (v) => (f.bold = v)), toggle("italic", "Italic", () => !!f.italic, (v) => (f.italic = v))),
        el("div.linked", {}, ...aligns)),
    ]);
  }

  private async signatureMenu(anchor: HTMLElement) {
    const ctrl = this.ctrl;
    const sigs = await ctrl.signatures.list();
    const list = el("div.mk-sig-list");
    const pop = popover(anchor, [list]);
    if (!sigs.length) list.append(el("div.mk-empty", { text: "No signatures yet" }));
    for (const sig of sigs.slice(0, 8)) {
      const pick = el("button.mk-sig-thumb", { type: "button", title: "Place this signature", "aria-label": `Place ${sig.name}`, "data-signature": sig.id });
      pick.append(signatureSvg(sig));
      pick.addEventListener("click", () => { pop.close(); ctrl.placeSignature(sig); });
      const more = iconButton("more", "More");
      more.addEventListener("click", () => {
        const copy = el("button.menu-item", { type: "button", text: "Copy as Image" });
        const del = el("button.menu-item", { type: "button", text: "Delete" });
        const sub = popover(more, [copy, del]);
        copy.addEventListener("click", async () => {
          sub.close();
          try {
            await navigator.clipboard.write([new ClipboardItem({ "image/png": signaturePng(sig) })]);
          } catch { /* clipboard permission denied */ }
        });
        del.addEventListener("click", async () => { sub.close(); await ctrl.signatures.remove(sig.id); });
      });
      list.append(el("div.mk-sig-row", {}, pick, more));
    }
    const create = el("button.btn.mk-wide", { type: "button", text: "Create Signature…", "data-action": "new-signature" });
    create.addEventListener("click", () => { pop.close(); void ctrl.newSignature(); });
    list.append(create);
    create.focus();
  }
}
