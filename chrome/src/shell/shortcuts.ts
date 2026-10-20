// Keyboard shortcut plumbing. One keydown listener on window; the most recently
// registered handler for a chord wins (viewer shortcuts shadow shell ones).
// Note: Chrome reserves some chords (Ctrl+W, Ctrl+T, Ctrl+N, Ctrl+Tab, Ctrl+Q)
// — pages never receive them, so View cannot use them like the desktop app.
import type { Shortcut, ShortcutRegistry } from "../viewers/contract.ts";

const isMac = /Mac|iPhone|iPad/.test(navigator.platform);

/** "Mod+Shift+S" style chord for a keyboard event. */
export function chordOf(ev: KeyboardEvent): string {
  const parts: string[] = [];
  if (isMac ? ev.metaKey : ev.ctrlKey) parts.push("Mod");
  if (isMac && ev.ctrlKey) parts.push("Ctrl");
  if (ev.altKey) parts.push("Alt");
  let key = ev.key;
  if (key === " ") key = "Space";
  else if (key.length === 1) key = key.toUpperCase();
  // Shift is implied by the character for printable keys ("+", "?"), explicit otherwise.
  if (ev.shiftKey && (key.length > 1 || /[A-Z]/.test(key))) parts.push("Shift");
  parts.push(key);
  return parts.join("+");
}

export function normalizeChord(keys: string): string {
  const parts = keys.split("+").filter(Boolean);
  // "Mod++" splits into ["Mod"] with a trailing "+": restore it.
  if (keys.endsWith("++") || keys === "+") parts.push("+");
  const key = parts.pop()!;
  const mods = ["Mod", "Ctrl", "Alt", "Shift"].filter((m) => parts.includes(m));
  return [...mods, key.length === 1 ? key.toUpperCase() : key].join("+");
}

export function displayChord(keys: string): string {
  return normalizeChord(keys).replace("Mod", isMac ? "⌘" : "Ctrl");
}

function isEditable(t: EventTarget | null): boolean {
  const e = t as HTMLElement | null;
  return !!e && (e.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.tagName));
}

export interface GroupedShortcut extends Shortcut { group: string }

export class Shortcuts implements ShortcutRegistry {
  private map = new Map<string, GroupedShortcut[]>();

  constructor(target: Window = window) {
    target.addEventListener("keydown", (ev) => this.dispatch(ev));
    // Viewers focus their scroller on load, which also matches :focus-visible; the
    // big content focus rings key off data-kbd so they appear only after Tab.
    const root = target.document.documentElement;
    target.addEventListener("keydown", (ev) => { if (ev.key === "Tab") root.dataset.kbd = ""; }, true);
    target.addEventListener("pointerdown", () => { delete root.dataset.kbd; }, true);
  }

  /** `group` is the heading in the Keyboard Shortcuts sheet. */
  add(s: Shortcut, group = "General"): () => void {
    const k = normalizeChord(s.keys);
    const entry: GroupedShortcut = Object.assign(Object.create(s) as Shortcut, { group, keys: s.keys, description: s.description, run: s.run.bind(s) });
    const list = this.map.get(k) ?? [];
    list.push(entry);
    this.map.set(k, list);
    return () => {
      const l = this.map.get(k);
      const i = l?.indexOf(entry) ?? -1;
      if (l && i >= 0) l.splice(i, 1);
    };
  }

  /** A scoped registry whose shortcuts all go away with `signal`. */
  scoped(signal: AbortSignal, group: string): ShortcutRegistry {
    return {
      add: (s) => {
        const off = this.add(s, group);
        signal.addEventListener("abort", off, { once: true });
        return off;
      },
    };
  }

  /** The active shortcut for each chord (shadowed ones are hidden). */
  all(): GroupedShortcut[] {
    return [...this.map.values()].flatMap((l) => l.slice(-1));
  }

  /**
   * For the help sheet: [group, [[chords, description]]], groups in first-registration
   * order, chords with the same description merged ("Ctrl+= / Ctrl++").
   */
  sheet(): [string, [string[], string][]][] {
    const groups = new Map<string, Map<string, string[]>>();
    for (const s of this.all()) {
      const g = groups.get(s.group) ?? new Map<string, string[]>();
      groups.set(s.group, g);
      g.set(s.description, [...(g.get(s.description) ?? []), displayChord(s.keys)]);
    }
    return [...groups].map(([g, m]) => [g, [...m].map(([d, keys]) => [keys, d])]);
  }

  private dispatch(ev: KeyboardEvent): void {
    const chord = chordOf(ev);
    // In text fields only chords with a modifier (Ctrl+S) are shortcuts.
    if (isEditable(ev.target) && !chord.includes("Mod")) return;
    const list = this.map.get(chord);
    const s = list?.[list.length - 1];
    if (!s) return;
    if (s.run(ev) !== false) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  }
}
