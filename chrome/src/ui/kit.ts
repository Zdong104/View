// Tiny DOM helpers + the shared widget kit (header buttons, toggles, popovers).
// No framework: viewers are imperative and performance-sensitive (canvases,
// virtualized lists), so plain DOM keeps them simple and fast.
import { ICONS, type IconName } from "./icons.ts";

type Attrs = Record<string, string | number | boolean | EventListener | undefined>;

/** el("div.cls1.cls2", {title: "x", onclick: fn}, child, "text") */
export function el<K extends keyof HTMLElementTagNameMap>(
  spec: K | `${K}.${string}`,
  attrs: Attrs = {},
  ...children: (Node | string | null | undefined | false)[]
): HTMLElementTagNameMap[K] {
  const [tag, ...classes] = spec.split(".") as [K, ...string[]];
  const node = document.createElement(tag);
  if (classes.length) node.className = classes.join(" ");
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "text") node.textContent = String(v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

export function icon(name: IconName): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  svg.innerHTML = ICONS[name];
  return svg;
}

/** Replace `host`'s children with icon `name` (e.g. toggling play/pause). */
export function setIcon(host: Element, name: IconName): void {
  host.replaceChildren(icon(name));
}

export interface ButtonOpts {
  icon?: IconName;
  label?: string;
  /** Tooltip; include the shortcut, e.g. "Zoom In (Ctrl++)". */
  title: string;
  onClick?: (ev: MouseEvent) => void;
  toggle?: boolean;
  pressed?: boolean;
  suggested?: boolean;
  id?: string;
}

/** A flat header-bar button (libadwaita style). */
export function button(o: ButtonOpts): HTMLButtonElement {
  const b = el("button.btn", {
    type: "button",
    title: o.title,
    "aria-label": o.title,
    id: o.id,
  });
  if (o.icon) b.append(icon(o.icon));
  if (o.label) b.append(el("span.btn-label", { text: o.label }));
  if (o.suggested) b.classList.add("suggested");
  if (o.toggle) {
    b.setAttribute("aria-pressed", String(!!o.pressed));
    b.addEventListener("click", () => b.setAttribute("aria-pressed",
      String(b.getAttribute("aria-pressed") !== "true")));
  }
  if (o.onClick) b.addEventListener("click", o.onClick);
  return b;
}

/** Grouped (linked) buttons, like Adw "linked" boxes. */
export function linked(...buttons: HTMLElement[]): HTMLDivElement {
  return el("div.linked", {}, ...buttons);
}

export function separator(): HTMLDivElement {
  return el("div.hsep", { role: "separator" });
}

/**
 * Put an already-attached popover under `anchor`, kept inside the window: it
 * lines up with the anchor's left edge for buttons in the left half of the
 * window and with its right edge otherwise, and opens upwards when there is
 * no room below.
 */
export function placePopover(pop: HTMLElement, anchor: DOMRect, gap = 6): void {
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const leftHalf = anchor.left + anchor.width / 2 < innerWidth / 2;
  const x = leftHalf ? anchor.left : anchor.right - w;
  const below = anchor.bottom + gap;
  const y = below + h <= innerHeight - 8 ? below : Math.max(8, anchor.top - gap - h);
  pop.style.left = `${Math.max(8, Math.min(x, innerWidth - w - 8))}px`;
  pop.style.top = `${Math.max(8, Math.min(y, innerHeight - h - 8))}px`;
  pop.style.right = "auto";
}

/** A simple popover menu anchored under a button. Items: [label, handler, shortcut?]. */
export function menuButton(
  opts: { icon: IconName; title: string },
  items: (readonly [string, () => void, string?] | "-")[],
): HTMLButtonElement {
  const b = button({ icon: opts.icon, title: opts.title });
  b.setAttribute("aria-haspopup", "menu");
  b.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const menu = el("div.popover", { role: "menu" });
    for (const it of items) {
      if (it === "-") { menu.append(el("div.menu-sep")); continue; }
      const [label, fn, accel] = it;
      menu.append(el("button.menu-item", {
        type: "button", role: "menuitem",
        onclick: () => { close(); fn(); },
      }, el("span", { text: label }), accel ? el("span.accel", { text: accel }) : null));
    }
    document.body.append(menu);
    placePopover(menu, b.getBoundingClientRect());
    const close = () => { menu.remove(); removeEventListener("click", close); removeEventListener("keydown", esc); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    setTimeout(() => { addEventListener("click", close); addEventListener("keydown", esc); });
  });
  return b;
}

export function humanSize(n: number): string {
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let v = n;
  for (const u of units) {
    if (v < 1000 || u === "TB") return u === "bytes" ? `${n} bytes` : `${v.toFixed(1)} ${u}`;
    v /= 1000;
  }
  return `${n} bytes`;
}

/**
 * Load a viewer's stylesheet (src/viewers/<kind>/style.css, built to
 * viewers/<kind>.css). Viewer CSS is not bundled into the shell so each kind
 * only pays for its own styles.
 */
export function loadStyle(kind: string): Promise<void> {
  const href = chrome.runtime.getURL(`viewers/${kind}.css`);
  if (document.querySelector(`link[href="${href}"]`)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const link = el("link", { rel: "stylesheet", href });
    link.onload = () => resolve();
    link.onerror = () => reject(new Error(`Failed to load ${href}`));
    document.head.append(link);
  });
}
