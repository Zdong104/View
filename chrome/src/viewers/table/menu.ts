// A small popup menu (context menus, toolbar dropdowns) in the shell's popover style.
import { el } from "../../ui/kit.ts";

export type MenuItem =
  | "-"
  | {
    label: string;
    run?: () => void;
    checked?: boolean;
    disabled?: boolean;
    accel?: string;
    /** A non-interactive section title. */
    heading?: boolean;
  };

/** Show `items` under an element or at a point; resolves nothing, closes on any outside input. */
export function popupMenu(at: HTMLElement | { x: number; y: number }, items: MenuItem[]): void {
  const prevFocus = document.activeElement as HTMLElement | null;
  const menu = el("div.popover.pm", { role: "menu" });
  const buttons: HTMLButtonElement[] = [];
  for (const it of items) {
    if (it === "-") { menu.append(el("div.menu-sep")); continue; }
    if (it.heading) { menu.append(el("div.menu-heading", { text: it.label })); continue; }
    const b = el("button.menu-item", {
      type: "button",
      role: it.checked === undefined ? "menuitem" : "menuitemcheckbox",
      "aria-checked": it.checked === undefined ? undefined : String(it.checked),
      disabled: it.disabled,
      onclick: () => { close(); it.run?.(); },
    },
    el("span", { text: (it.checked === undefined ? "" : it.checked ? "✓ " : "  ") + it.label }),
    it.accel ? el("span.accel", { text: it.accel }) : null);
    if (!it.disabled) buttons.push(b);
    menu.append(b);
  }
  document.body.append(menu);
  const rect = at instanceof HTMLElement ? at.getBoundingClientRect() : null;
  const pt = at instanceof HTMLElement ? { x: 0, y: 0 } : at;
  let x = rect ? rect.left : pt.x, y = rect ? rect.bottom + 4 : pt.y;
  const w = menu.offsetWidth, h = menu.offsetHeight;
  x = Math.max(4, Math.min(x, innerWidth - w - 4));
  y = Math.max(4, Math.min(y, innerHeight - h - 4));
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  buttons[0]?.focus();

  const close = () => {
    menu.remove();
    removeEventListener("pointerdown", outside, true);
    removeEventListener("keydown", key, true);
    removeEventListener("blur", close);
    prevFocus?.focus?.({ preventScroll: true });
  };
  const outside = (ev: Event) => { if (!menu.contains(ev.target as Node)) close(); };
  const key = (ev: KeyboardEvent) => {
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); close(); return; }
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    ev.preventDefault();
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const n = buttons.length;
    buttons[(i + (ev.key === "ArrowDown" ? 1 : -1) + n) % n]?.focus();
  };
  addEventListener("pointerdown", outside, true);
  addEventListener("keydown", key, true);
  addEventListener("blur", close);
}
