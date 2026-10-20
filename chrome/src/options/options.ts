// Options page: "Make View the default for ..." per kind, attachment handling, theme.
import { KIND_LABELS, KINDS } from "../shared/kinds.ts";
import { sendToSw } from "../shared/messages.ts";
import { loadPrefs, onPrefsChanged, savePrefs, setKindIntercept, type Prefs } from "../shared/prefs.ts";
import { el } from "../ui/kit.ts";

function toggle(id: string, checked: boolean, onChange: (v: boolean) => void): HTMLInputElement {
  const i = el("input.switch", { type: "checkbox", id, role: "switch" });
  i.checked = checked;
  i.addEventListener("change", () => onChange(i.checked));
  return i;
}

function applyTheme(p: Prefs): void {
  if (p.theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = p.theme;
}

async function render(): Promise<void> {
  let prefs = await loadPrefs();
  applyTheme(prefs);
  onPrefsChanged(applyTheme);
  const fileAccess = await chrome.extension.isAllowedFileSchemeAccess();
  const commit = async (p: Prefs) => {
    prefs = p;
    applyTheme(p);
    await savePrefs(p);
    await sendToSw({ type: "sync-rules" });
  };

  const kinds = el("section.group", {}, el("h2", { text: "Open with View" }),
    ...KINDS.map((k) => el("label.row", { for: `kind-${k}` },
      el("span", {}, KIND_LABELS[k],
        el("small", { text: k === "pdf" ? "Replaces Chrome's PDF viewer for links, frames and local files"
          : k === "media" || k === "text" ? "Chrome shows these itself; turn on to use View instead" : "" })),
      toggle(`kind-${k}`, prefs.intercept[k], (v) => void commit(setKindIntercept(prefs, k, v))))));

  const behaviour = el("section.group", {}, el("h2", { text: "Behaviour" }),
    el("label.row", { for: "attach" },
      el("span", {}, "Respect “download” links",
        el("small", { text: "When a server marks a non-PDF file as an attachment, let Chrome download it" })),
      toggle("attach", prefs.respectAttachment, (v) => void commit({ ...prefs, respectAttachment: v }))),
    el("label.row", { for: "theme" }, el("span", { text: "Appearance" }), (() => {
      const s = el("select", { id: "theme" },
        el("option", { value: "system", text: "Follow system" }),
        el("option", { value: "light", text: "Light" }),
        el("option", { value: "dark", text: "Dark" }));
      s.value = prefs.theme;
      s.addEventListener("change", () => void commit({ ...prefs, theme: s.value as Prefs["theme"] }));
      return s;
    })()));

  const local = el("section.group", {}, el("h2", { text: "Local files" }),
    el("div.row", {}, el("span", {},
      fileAccess ? "View can open local files you open in Chrome (file://)."
        : "To open local files you open in Chrome (file://), turn on “Allow access to file URLs”.",
      el("small", { class: fileAccess ? "" : "warn",
        text: fileAccess ? "“Allow access to file URLs” is on." : "Without it, use Open… or drag files onto a View tab." })),
      fileAccess ? "" : el("button.btn.suggested", { type: "button",
        onclick: () => void sendToSw({ type: "open-extension-settings" }), text: "Extension Settings" })));

  document.body.replaceChildren(
    el("h1", { text: "View Preferences" }),
    el("p.lead", { text: "Choose which files open in View instead of Chrome's built-in handling." }),
    kinds, behaviour, local);
  document.documentElement.dataset.state = "ready";
}

void render();
