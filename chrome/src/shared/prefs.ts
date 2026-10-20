// User preferences (chrome.storage.sync). The service worker rebuilds the
// interception rules whenever these change.
import { KINDS, type Kind } from "./kinds.ts";

export interface Prefs {
  /** "Make View the default for ..." — per kind, applies to web and file:// URLs. */
  intercept: Record<Kind, boolean>;
  /**
   * Respect `Content-Disposition: attachment` (let Chrome download the file)
   * for non-PDF kinds. PDFs in the main frame are always shown, like pdf.js does,
   * because servers commonly mislabel them.
   */
  respectAttachment: boolean;
  /** Theme override; "system" follows prefers-color-scheme. */
  theme: "system" | "light" | "dark";
}

export const DEFAULT_PREFS: Prefs = {
  intercept: { pdf: true, image: false, model: true, table: true, text: false, media: false },
  respectAttachment: true,
  theme: "system",
};

/** Fill missing/invalid fields from defaults (storage may hold an older shape). */
export function normalizePrefs(raw: unknown): Prefs {
  const r = (raw && typeof raw === "object" ? raw : {}) as Partial<Prefs>;
  const intercept = { ...DEFAULT_PREFS.intercept };
  for (const k of KINDS) {
    const v = r.intercept?.[k];
    if (typeof v === "boolean") intercept[k] = v;
  }
  return {
    intercept,
    respectAttachment: typeof r.respectAttachment === "boolean"
      ? r.respectAttachment : DEFAULT_PREFS.respectAttachment,
    theme: r.theme === "light" || r.theme === "dark" || r.theme === "system"
      ? r.theme : DEFAULT_PREFS.theme,
  };
}

const KEY = "prefs";

export async function loadPrefs(): Promise<Prefs> {
  const got = await chrome.storage.sync.get(KEY);
  return normalizePrefs(got[KEY]);
}

export async function savePrefs(p: Prefs): Promise<void> {
  await chrome.storage.sync.set({ [KEY]: p });
}

export function onPrefsChanged(cb: (p: Prefs) => void): void {
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "sync" && changes[KEY]) cb(normalizePrefs(changes[KEY].newValue));
  });
}

export function setKindIntercept(p: Prefs, kind: Kind, on: boolean): Prefs {
  return { ...p, intercept: { ...p.intercept, [kind]: on } };
}
