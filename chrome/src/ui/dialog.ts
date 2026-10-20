// Modal dialogs (libadwaita AdwDialog look) on top of the native <dialog>
// element: focus handling, Escape and backdrop clicks come for free.
// Shared by the shell (shortcuts sheet, export picker) and usable by viewers.
import { button, el } from "./kit.ts";

export interface DialogAction {
  label: string;
  /** Value the dialog resolves with when this action is chosen. */
  value: string;
  suggested?: boolean;
  destructive?: boolean;
}

export interface DialogOptions {
  title: string;
  body: Node;
  /** Bottom-row buttons. Without actions the dialog shows a close button only. */
  actions?: DialogAction[];
  /** Max width in px (default 440). */
  width?: number;
}

/** Show a modal dialog; resolves with the chosen action's value, or null if dismissed. */
export function showDialog(o: DialogOptions): Promise<string | null> {
  return new Promise((resolve) => {
    const dlg = el("dialog.dialog", { "aria-label": o.title });
    dlg.style.maxWidth = `${o.width ?? 440}px`;
    const close = (v: string | null) => { dlg.close(v ?? ""); };
    const header = el("div.dialog-header", {},
      el("h2.dialog-title", { text: o.title }),
      button({ icon: "close", title: "Close", onClick: () => close(null) }));
    const actions = o.actions?.length
      ? el("div.dialog-actions", {}, ...o.actions.map((a) => {
          const b = button({ label: a.label, title: a.label, suggested: a.suggested, onClick: () => close(a.value) });
          if (a.destructive) b.classList.add("destructive");
          b.dataset.value = a.value;
          return b;
        }))
      : null;
    dlg.append(header, el("div.dialog-body", {}, o.body), ...(actions ? [actions] : []));
    dlg.addEventListener("click", (e) => { if (e.target === dlg) close(null); }); // backdrop
    dlg.addEventListener("close", () => {
      dlg.remove();
      resolve(dlg.returnValue || null);
    }, { once: true });
    document.body.append(dlg);
    dlg.showModal();
    (actions?.querySelector<HTMLElement>(".suggested") ?? dlg.querySelector<HTMLElement>("input,button"))?.focus();
  });
}

export interface ExportChoice { id: string; label: string }

/**
 * The export format picker ("Export…", Ctrl+Shift+E). The shell uses it for its
 * menu; viewers may call it too, then pass the result to their own save() via
 * the shell (Viewer.exportFormats / SaveRequest.format).
 */
export async function pickExportFormat(formats: ExportChoice[], title = "Export"): Promise<string | null> {
  if (formats.length === 0) return null;
  const list = el("div.choice-list", { role: "radiogroup", "aria-label": "Format" });
  formats.forEach((f, i) => {
    const input = el("input", { type: "radio", name: "export-format", value: f.id, id: `fmt-${f.id}` });
    input.checked = i === 0;
    list.append(el("label.choice", { for: `fmt-${f.id}` }, input, el("span", { text: f.label })));
  });
  const res = await showDialog({
    title,
    body: list,
    actions: [{ label: "Cancel", value: "cancel" }, { label: "Export…", value: "ok", suggested: true }],
  });
  if (res !== "ok") return null;
  return list.querySelector<HTMLInputElement>("input:checked")?.value ?? null;
}
