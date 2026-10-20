// PDF viewer dialogs (password, Export with Password, Export as Images) on top
// of the shared ui/dialog.ts, plus a pointer-positioned context menu that uses
// the kit's popover styles.
import { showDialog } from "../../ui/dialog.ts";
import { el } from "../../ui/kit.ts";
import type { EncryptOptions } from "./build.ts";

const field = (label: string, input: HTMLElement) =>
  el("label.pdf-field", {}, el("span", { text: label }), input);

export async function askPassword(name: string, wrong: boolean): Promise<string | null> {
  const input = el("input.pdf-input", { type: "password", autocomplete: "off", "aria-label": "Password", "data-role": "pdf-password" });
  const body = el("div.pdf-form", {},
    el("p", { text: wrong ? "Incorrect password. Try again." : `“${name}” is protected with a password.` }),
    input);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); body.closest("dialog")?.close("ok"); }
  });
  const p = showDialog({ title: "Password Required", body, actions: [
    { label: "Cancel", value: "cancel" }, { label: "Unlock", value: "ok", suggested: true }] });
  input.focus();
  const res = await p;
  return res === "ok" ? input.value : null;
}

export async function askEncrypt(): Promise<EncryptOptions | null> {
  const user = el("input.pdf-input", { type: "password", autocomplete: "new-password", placeholder: "Password to open", "data-role": "user-password" });
  const owner = el("input.pdf-input", { type: "password", autocomplete: "new-password", placeholder: "Owner password (optional)", "data-role": "owner-password" });
  const check = (label: string, key: string) => {
    const c = el("input", { type: "checkbox", "data-perm": key });
    c.checked = true;
    return [el("label.pdf-check", {}, c, el("span", { text: label })), c] as const;
  };
  const perms = [
    check("Allow printing", "print"),
    check("Allow copying text and images", "copy"),
    check("Allow changes and page assembly", "modify"),
    check("Allow comments and form filling", "annotate"),
  ] as const;
  const body = el("div.pdf-form", {},
    el("p.dim", { text: "The new PDF is encrypted with AES-256. Permissions are enforced by the owner password." }),
    user, owner, ...perms.map((p) => p[0] as HTMLElement));
  for (;;) {
    const p = showDialog({ title: "Export with Password", body, actions: [
      { label: "Cancel", value: "cancel" }, { label: "Save As…", value: "ok", suggested: true }] });
    user.focus();
    const res = await p;
    if (res !== "ok") return null;
    if (!user.value && !owner.value) continue; // needs a password; show again
    return {
      userPassword: user.value,
      ownerPassword: owner.value || undefined,
      allowPrint: perms[0][1].checked,
      allowCopy: perms[1][1].checked,
      allowModify: perms[2][1].checked,
      allowAnnotate: perms[3][1].checked,
    };
  }
}

export async function askExportImages(count: number): Promise<{ format: "png" | "jpeg"; dpi: number } | null> {
  const fmt = el("select.pdf-input", { "data-role": "format" },
    el("option", { value: "png", text: "PNG" }), el("option", { value: "jpeg", text: "JPEG" }));
  const dpi = el("input.pdf-input", { type: "number", min: 36, max: 1200, step: 25, value: 150, "data-role": "dpi" });
  const body = el("div.pdf-form", {},
    el("p", { text: `Each of the ${count} page${count > 1 ? "s" : ""} becomes one image file in the folder you choose.` }),
    field("Format", fmt), field("Resolution (dpi)", dpi));
  const res = await showDialog({ title: "Export as Images", body, actions: [
    { label: "Cancel", value: "cancel" }, { label: "Choose Folder…", value: "ok", suggested: true }] });
  if (res !== "ok") return null;
  return { format: fmt.value === "jpeg" ? "jpeg" : "png", dpi: Math.max(36, Math.min(1200, Number(dpi.value) || 150)) };
}

export type MenuItem = readonly [label: string, run: () => void, accel?: string, disabled?: boolean] | "-";

/** A popover menu at a point (context menus) or under an element. */
export function popupMenu(at: { x: number; y: number } | HTMLElement, items: MenuItem[]): void {
  const menu = el("div.popover.pdf-popup", { role: "menu" });
  const close = () => {
    menu.remove();
    removeEventListener("pointerdown", outside, true);
    removeEventListener("keydown", esc, true);
    removeEventListener("blur", close);
  };
  for (const it of items) {
    if (it === "-") { menu.append(el("div.menu-sep")); continue; }
    const [label, run, accel, disabled] = it;
    const b = el("button.menu-item", { type: "button", role: "menuitem", disabled: !!disabled },
      el("span", { text: label }), accel ? el("span.accel", { text: accel }) : null);
    b.addEventListener("click", () => { close(); run(); });
    menu.append(b);
  }
  document.body.append(menu);
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let x: number, y: number;
  if (at instanceof HTMLElement) {
    const r = at.getBoundingClientRect();
    x = Math.min(r.right - mw, innerWidth - mw - 8);
    y = r.bottom + 6;
  } else { x = at.x; y = at.y; }
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - mw - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, innerHeight - mh - 8))}px`;
  menu.style.right = "auto";
  const outside = (e: PointerEvent) => { if (!menu.contains(e.target as Node)) close(); };
  const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  setTimeout(() => {
    addEventListener("pointerdown", outside, true);
    addEventListener("keydown", esc, true);
    addEventListener("blur", close);
  });
  (menu.querySelector("button:not([disabled])") as HTMLElement | null)?.focus();
  menu.addEventListener("keydown", (e) => {
    const btns = [...menu.querySelectorAll<HTMLButtonElement>("button:not([disabled])")];
    const i = btns.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown") { btns[(i + 1) % btns.length]?.focus(); e.preventDefault(); }
    if (e.key === "ArrowUp") { btns[(i - 1 + btns.length) % btns.length]?.focus(); e.preventDefault(); }
  });
}
