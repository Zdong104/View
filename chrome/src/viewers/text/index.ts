// Text viewer: CodeMirror 6, read-only until you press Edit. Port of preview/text_view.py:
// language by file name, encoding / BOM / line endings round-trip on save, wrap toggle,
// Find, Go to Line, zoom, and a plain light-weight mode for very large files.
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { LanguageDescription, bracketMatching, defaultHighlightStyle, foldGutter, foldKeymap, indentOnInput, syntaxHighlighting, HighlightStyle } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { highlightSelectionMatches, openSearchPanel, search, searchKeymap, findNext, findPrevious } from "@codemirror/search";
import { Compartment, EditorState, type Extension, type Text } from "@codemirror/state";
import { EditorView, drawSelection, highlightActiveLine, highlightActiveLineGutter, keymap, lineNumbers } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";
import { showDialog } from "../../ui/dialog.ts";
import { button, el, loadStyle, humanSize } from "../../ui/kit.ts";
import { extOf } from "../../shared/kinds.ts";
import type { DocumentSource, InfoSection, SavedInfo, SaveRequest, SaveResult, Viewer, ViewerContext, ViewerModule } from "../contract.ts";

const LARGE_BYTES = 20 * 1024 * 1024; // above this: read-only, no highlighting, plain mode
const MAX_LOAD_BYTES = 128 * 1024 * 1024; // never read more than this
const HIGHLIGHT_CAP = 1_500_000; // characters; longer text is not highlighted
const PROSE_EXTS = new Set(["txt", "md", "markdown", "rst", "org", "tex", "srt", "vtt"]);
const DEFAULT_PX = 13, MIN_PX = 8, MAX_PX = 40;
const EOL_NAMES: Record<string, string> = { LF: "LF (Unix)", CRLF: "CRLF (Windows)", CR: "CR (classic Mac)" };
const EOL_CHARS: Record<string, string> = { LF: "\n", CRLF: "\r\n", CR: "\r" };

/** Extensions that language-data doesn't know but which are a known format. */
const ALIASES: Record<string, string> = {
  jsonc: "json", jsonl: "json", ndjson: "json", mjs: "js", cjs: "js", pyw: "py", pyi: "py", xhtml: "html",
  htm: "html", markdown: "md", sass: "scss", ex: "exs", cuh: "cu", hh: "cpp", hxx: "cpp", hpp: "cpp",
  cxx: "cpp", cc: "cpp", cmd: "bat", gradle: "groovy", kts: "kt", ini: "ini", conf: "ini", cfg: "ini",
  service: "ini", desktop: "ini", editorconfig: "ini", properties: "properties", env: "properties",
  gitconfig: "ini", sty: "tex", cls: "tex", bib: "tex", patch: "diff",
};

interface Decoded {
  text: string;
  /** Label for TextDecoder / the encoder. */
  encoding: string;
  encLabel: string;
  bom: Uint8Array;
  eol: "LF" | "CRLF" | "CR";
  mixed: boolean;
  nul: boolean;
  truncated: boolean;
}

function decodeBytes(bytes: Uint8Array, truncated: boolean): Decoded {
  let encoding = "utf-8", encLabel = "UTF-8", bom = new Uint8Array(0), body = bytes;
  if (bytes[0] === 0xEF && bytes[1] === 0xBB && bytes[2] === 0xBF) {
    bom = bytes.slice(0, 3); body = bytes.subarray(3); encLabel = "UTF-8 with BOM";
  } else if (bytes[0] === 0xFF && bytes[1] === 0xFE) {
    encoding = "utf-16le"; encLabel = "UTF-16 LE"; bom = bytes.slice(0, 2); body = bytes.subarray(2);
  } else if (bytes[0] === 0xFE && bytes[1] === 0xFF) {
    encoding = "utf-16be"; encLabel = "UTF-16 BE"; bom = bytes.slice(0, 2); body = bytes.subarray(2);
  } else {
    try {
      new TextDecoder("utf-8", { fatal: true }).decode(truncated ? bytes.subarray(0, Math.max(0, bytes.length - 4)) : bytes);
    } catch {
      encoding = "windows-1252"; encLabel = "Windows-1252 (Latin-1)";
    }
  }
  let text = new TextDecoder(encoding, { ignoreBOM: true }).decode(body);
  const crlf = (text.match(/\r\n/g) ?? []).length;
  const lf = (text.match(/\n/g) ?? []).length - crlf;
  const cr = (text.match(/\r/g) ?? []).length - crlf;
  const kinds: [number, "LF" | "CRLF" | "CR"][] = [[lf, "LF"], [crlf, "CRLF"], [cr, "CR"]];
  const eol = kinds.reduce((a, b) => (b[0] > a[0] ? b : a))[1];
  const mixed = kinds.filter((k) => k[0] > 0).length > 1;
  const nul = text.includes("\u0000");
  if (nul) text = text.replaceAll("\u0000", "�");
  return { text, encoding, encLabel, bom, eol, mixed, nul, truncated };
}

/** Encode back to the file's encoding; null when a character doesn't exist in it. */
function encodeText(text: string, encoding: string): Uint8Array | null {
  if (encoding === "utf-8") return new TextEncoder().encode(text);
  if (encoding === "utf-16le" || encoding === "utf-16be") {
    const le = encoding === "utf-16le";
    const out = new Uint8Array(text.length * 2);
    for (let i = 0; i < text.length; i++) {
      const c = text.charCodeAt(i);
      out[i * 2 + (le ? 0 : 1)] = c & 255;
      out[i * 2 + (le ? 1 : 0)] = c >> 8;
    }
    return out;
  }
  const dec = new TextDecoder(encoding);
  const map = new Map<string, number>();
  for (let b = 0; b < 256; b++) {
    const ch = dec.decode(new Uint8Array([b]));
    if (ch !== "�" && !map.has(ch)) map.set(ch, b);
  }
  const out = new Uint8Array(text.length);
  let n = 0;
  for (const ch of text) {
    const cp = ch.codePointAt(0)!;
    const b = cp < 128 ? cp : map.get(ch);
    if (b === undefined) return null;
    out[n++] = b;
  }
  return out.subarray(0, n);
}

const isDark = (): boolean => {
  const forced = document.documentElement.dataset.theme;
  return forced ? forced === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
};

const highlight = HighlightStyle.define([
  { tag: [t.keyword, t.operatorKeyword, t.modifier, t.controlKeyword], color: "var(--tok-keyword)" },
  { tag: [t.string, t.special(t.string), t.regexp], color: "var(--tok-string)" },
  { tag: [t.number, t.bool, t.null, t.atom], color: "var(--tok-number)" },
  { tag: [t.comment, t.lineComment, t.blockComment], color: "var(--tok-comment)", fontStyle: "italic" },
  { tag: [t.function(t.variableName), t.function(t.propertyName), t.labelName], color: "var(--tok-function)" },
  { tag: [t.typeName, t.className, t.namespace, t.definition(t.typeName)], color: "var(--tok-type)" },
  { tag: [t.propertyName, t.attributeName], color: "var(--tok-prop)" },
  { tag: [t.tagName, t.heading, t.strong], color: "var(--tok-tag)", fontWeight: "bold" },
  { tag: [t.meta, t.processingInstruction, t.documentMeta], color: "var(--tok-comment)" },
  { tag: [t.link, t.url], color: "var(--tok-function)", textDecoration: "underline" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: t.invalid, color: "var(--destructive)" },
]);

function languageFor(name: string): LanguageDescription | null {
  const ext = extOf(name);
  const alias = ALIASES[ext];
  return LanguageDescription.matchFilename(languages, alias ? `x.${alias}` : name)
    ?? LanguageDescription.matchFilename(languages, name.toLowerCase());
}

class TextViewer implements Viewer {
  private ctx!: ViewerContext;
  private source!: DocumentSource;
  private blob!: Blob;
  private root!: HTMLElement;
  private view!: EditorView;
  private dec!: Decoded;
  private savedDoc!: Text;
  private eol: "LF" | "CRLF" | "CR" = "LF";
  private encoding = "utf-8";
  private encLabel = "UTF-8";
  /** What save() produced, applied as the saved state by onSaved() once it is written. */
  private pendingSave: { result: SaveResult; doc: Text; encoding: string; encLabel: string; bom: Uint8Array } | null = null;
  private langName = "Plain Text";
  private readonlyReason = "";
  private plain = false;
  private editing = false;
  private wrapOn = false;
  private px = DEFAULT_PX;
  private dirty = false;
  private statusEl = el("div.txt-status-l");
  private infoEl = el("div.txt-status-r");
  private editBtn!: HTMLButtonElement;
  private wrapBtn!: HTMLButtonElement;
  private readonly cEditable = new Compartment();
  private readonly cWrap = new Compartment();
  private readonly cLang = new Compartment();
  private readonly cTheme = new Compartment();
  private themeObs?: MutationObserver;

  async mount(container: HTMLElement, source: DocumentSource, ctx: ViewerContext): Promise<void> {
    this.ctx = ctx;
    this.source = source;
    await loadStyle("text");
    this.root = el("div.txt-root", { "data-viewer": "text", "data-state": "loading" });
    container.append(this.root);
    this.blob = await source.blob();
    this.root.dataset.bytes = String(this.blob.size);

    const truncated = this.blob.size > MAX_LOAD_BYTES;
    const bytes = new Uint8Array(await this.blob.slice(0, MAX_LOAD_BYTES).arrayBuffer());
    let body = bytes;
    if (truncated) { const cut = bytes.lastIndexOf(10); if (cut > 0) body = bytes.subarray(0, cut + 1); }
    this.dec = decodeBytes(body, truncated);
    this.eol = this.dec.eol; this.encoding = this.dec.encoding; this.encLabel = this.dec.encLabel;

    const large = this.blob.size > LARGE_BYTES;
    this.plain = large || truncated;
    const reasons: string[] = [];
    if (large) reasons.push("large file");
    if (truncated) reasons.push(`showing only the first ${MAX_LOAD_BYTES / 1048576} MB`);
    if (this.dec.nul) reasons.push("contains NUL bytes");
    this.readonlyReason = reasons.join(", ");
    const ext = extOf(source.name);
    this.wrapOn = !this.plain && PROSE_EXTS.has(ext);
    try { this.px = Math.min(MAX_PX, Math.max(MIN_PX, Number(localStorage.getItem("view.text.px")) || DEFAULT_PX)); } catch { /* storage unavailable */ }
    this.root.style.setProperty("--txt-size", `${this.px}px`);

    const banner = this.readonlyReason
      ? el("div.txt-banner", { role: "status", text: large
        ? `This file is large (${humanSize(this.blob.size)}). It is open read-only and syntax highlighting is off.`
        : truncated ? `This file is very large; showing only the first ${MAX_LOAD_BYTES / 1048576} MB, read-only.`
          : "This file contains NUL bytes, so it is open read-only." })
      : null;

    const state = EditorState.create({ doc: this.dec.text, extensions: this.extensions() });
    this.view = new EditorView({ state, parent: el("div.txt-editor") });
    this.savedDoc = state.doc;
    const host = el("div.txt-editor-host", {}, this.view.dom);
    this.root.append(...(banner ? [banner] : []), host, el("div.txt-status", {}, this.statusEl, this.infoEl));
    this.buildToolbar();
    this.buildShortcuts();
    this.themeObs = new MutationObserver(() => this.applyTheme());
    this.themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const mq = matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", () => this.applyTheme(), { signal: ctx.signal });

    this.langName = {
      log: "Log", md: "Markdown", markdown: "Markdown", txt: "Plain Text",
    }[ext] ?? "Plain Text";
    this.root.dataset.language = this.langName;
    this.refreshStatus();
    this.root.dataset.state = "ready";
    this.root.dataset.lines = String(this.view.state.doc.lines);
    this.root.dataset.editable = "false";
    this.root.dataset.wrap = String(this.wrapOn);
    this.view.focus();
    ctx.refreshTitle();
    if (!this.plain && this.dec.text.length <= HIGHLIGHT_CAP) void this.loadLanguage();
  }

  private extensions(): Extension[] {
    const base: Extension[] = [
      lineNumbers(),
      drawSelection(),
      EditorState.tabSize.of(4),
      this.cEditable.of(EditorState.readOnly.of(true)),
      this.cWrap.of(this.wrapOn ? EditorView.lineWrapping : []),
      this.cLang.of([]),
      this.cTheme.of(this.theme()),
      keymap.of([...defaultKeymap, ...searchKeymap.filter((k) => k.key !== "Mod-g" && k.key !== "Shift-Mod-g" && k.key !== "Mod-Alt-g"), ...historyKeymap, ...foldKeymap, indentWithTab]),
      EditorView.updateListener.of((u) => {
        if (u.docChanged) this.onDocChanged();
        if (u.docChanged || u.selectionSet) this.refreshStatus();
      }),
      EditorView.contentAttributes.of({ "aria-label": this.source.name }),
    ];
    if (this.plain) {
      // Very large files: only what is needed to scroll and select quickly.
      return [...base, search({ top: true })];
    }
    return [...base, highlightActiveLineGutter(), highlightActiveLine(), history(), indentOnInput(), bracketMatching(), foldGutter(),
      highlightSelectionMatches(), search({ top: true }), syntaxHighlighting(highlight), syntaxHighlighting(defaultHighlightStyle, { fallback: true })];
  }

  private theme(): Extension {
    return EditorView.theme({
      "&": { color: "var(--window-fg)", backgroundColor: "var(--view-bg)", height: "100%", fontSize: "var(--txt-size)" },
      ".cm-scroller": { fontFamily: "var(--mono)", lineHeight: "1.5" },
      ".cm-content": { caretColor: "var(--accent)" },
      ".cm-gutters": { backgroundColor: "var(--window-bg)", color: "var(--dim-fg)", border: "none", borderRight: "1px solid var(--border)" },
      ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--window-fg) 5%, transparent)" },
      ".cm-activeLineGutter": { backgroundColor: "var(--hover)", color: "var(--window-fg)" },
      "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": { backgroundColor: "var(--selected)" },
      ".cm-cursor": { borderLeftColor: "var(--accent)" },
      ".cm-searchMatch": { backgroundColor: "color-mix(in srgb, var(--warning) 35%, transparent)", outline: "none" },
      ".cm-searchMatch-selected": { backgroundColor: "color-mix(in srgb, var(--warning) 70%, transparent)" },
      ".cm-panels": { backgroundColor: "var(--window-bg)", color: "var(--window-fg)", borderColor: "var(--border)" },
      ".cm-panel.cm-search input, .cm-panel.cm-search button": { font: "inherit" },
      ".cm-panel.cm-search input": { color: "var(--window-fg)", backgroundColor: "var(--view-bg)", border: "1px solid var(--border)", borderRadius: "6px", padding: "3px 6px" },
      ".cm-panel.cm-search button": { color: "var(--window-fg)", backgroundImage: "none", backgroundColor: "var(--hover)", border: "1px solid var(--border)", borderRadius: "6px" },
      ".cm-foldPlaceholder": { backgroundColor: "var(--hover)", border: "none", color: "var(--dim-fg)" },
    }, { dark: isDark() });
  }

  private applyTheme(): void {
    this.view.dispatch({ effects: this.cTheme.reconfigure(this.theme()) });
  }

  private async loadLanguage(): Promise<void> {
    const desc = languageFor(this.source.name);
    if (!desc) return;
    try {
      const support = await desc.load();
      if (this.ctx.signal.aborted) return;
      this.view.dispatch({ effects: this.cLang.reconfigure(support) });
      this.langName = desc.name;
      this.root.dataset.language = desc.name;
      this.refreshStatus();
      this.ctx.refreshTitle();
    } catch { /* highlighting is optional */ }
  }

  // ---- toolbar / shortcuts -------------------------------------------------------------

  private buildToolbar(): void {
    const tools = this.ctx.header.tools;
    this.editBtn = button({ icon: "markup", label: "Edit", title: "Edit this file", toggle: true, onClick: () => this.setEditing(!this.editing) });
    if (this.readonlyReason) { this.editBtn.disabled = true; this.editBtn.title = `Read-only: ${this.readonlyReason}`; }
    this.wrapBtn = button({ label: "Wrap", title: "Wrap long lines", toggle: true, pressed: this.wrapOn, onClick: () => this.setWrap(!this.wrapOn) });
    tools.append(
      this.editBtn, this.wrapBtn,
      button({ icon: "search", title: "Find (Ctrl+F)", onClick: () => this.openFind() }),
      button({ label: "Go to Line", title: "Go to Line (Ctrl+G)", onClick: () => void this.gotoLine() }),
    );
  }

  private buildShortcuts(): void {
    const sc = this.ctx.shortcuts;
    // CodeMirror handles these itself when the editor has focus (it marks the event handled).
    const unlessHandled = (fn: () => void) => (ev: KeyboardEvent) => { if (ev.defaultPrevented) return false; fn(); };
    sc.add({ keys: "Mod+F", description: "Find", run: unlessHandled(() => this.openFind()) });
    sc.add({ keys: "F3", description: "Find next", run: unlessHandled(() => findNext(this.view)) });
    sc.add({ keys: "Shift+F3", description: "Find previous", run: unlessHandled(() => findPrevious(this.view)) });
    sc.add({ keys: "Mod+Shift+G", description: "Find previous", run: unlessHandled(() => findPrevious(this.view)) });
    sc.add({ keys: "Mod+G", description: "Go to line", run: () => void this.gotoLine() });
    sc.add({ keys: "Mod+=", description: "Larger text", run: () => this.zoom(1) });
    sc.add({ keys: "Mod++", description: "Larger text", run: () => this.zoom(1) });
    sc.add({ keys: "Mod+-", description: "Smaller text", run: () => this.zoom(-1) });
    sc.add({ keys: "Mod+0", description: "Actual size", run: () => this.zoom(0) });
  }

  private openFind(): void {
    openSearchPanel(this.view);
  }

  private zoom(dir: number): void {
    this.px = dir === 0 ? DEFAULT_PX : Math.min(MAX_PX, Math.max(MIN_PX, this.px + dir));
    this.root.style.setProperty("--txt-size", `${this.px}px`);
    try { localStorage.setItem("view.text.px", String(this.px)); } catch { /* ignore */ }
    this.view.requestMeasure();
  }

  private setWrap(on: boolean): void {
    this.wrapOn = on;
    this.view.dispatch({ effects: this.cWrap.reconfigure(on ? EditorView.lineWrapping : []) });
    this.wrapBtn.setAttribute("aria-pressed", String(on));
    this.root.dataset.wrap = String(on);
  }

  private setEditing(on: boolean): void {
    if (on && this.readonlyReason) { this.ctx.toast(`This file is open read-only (${this.readonlyReason})`); return; }
    this.editing = on;
    this.view.dispatch({ effects: this.cEditable.reconfigure(EditorState.readOnly.of(!on)) });
    this.editBtn.setAttribute("aria-pressed", String(on));
    this.root.dataset.editable = String(on);
    this.view.contentDOM.setAttribute("aria-readonly", String(!on));
    this.view.focus();
  }

  async gotoLine(): Promise<void> {
    const n = this.view.state.doc.lines;
    const input = el("input", { type: "text", inputmode: "numeric", placeholder: "Line or line:column", "aria-label": "Line", class: "txt-goto" });
    const body = el("div", {}, el("p", { text: `Enter a line number from 1 to ${n.toLocaleString()} (or line:column).` }), input);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); (input.closest("dialog")?.querySelector(".suggested") as HTMLElement | null)?.click(); }
    });
    const r = await showDialog({ title: "Go to Line", body, actions: [{ label: "Cancel", value: "no" }, { label: "Go", value: "go", suggested: true }] });
    if (r !== "go") { this.view.focus(); return; }
    this.goto(input.value);
  }

  goto(spec: string): void {
    const m = /^\s*(\d+)\s*(?:[:,]\s*(\d+))?\s*$/.exec(spec);
    if (!m) { this.ctx.toast("Enter a line number"); return; }
    const doc = this.view.state.doc;
    const line = doc.line(Math.max(1, Math.min(Number(m[1]), doc.lines)));
    const col = m[2] ? Math.min(Number(m[2]) - 1, line.length) : 0;
    this.view.dispatch({ selection: { anchor: line.from + col }, effects: EditorView.scrollIntoView(line.from + col, { y: "center" }) });
    this.view.focus();
  }

  // ---- state ---------------------------------------------------------------------------------

  private onDocChanged(): void {
    const dirty = !this.view.state.doc.eq(this.savedDoc);
    if (dirty !== this.dirty) { this.dirty = dirty; this.ctx.setDirty(dirty); }
    this.ctx.refreshTitle();
    this.root.dataset.dirty = String(dirty);
    this.root.dataset.lines = String(this.view.state.doc.lines);
  }

  private refreshStatus(): void {
    const st = this.view.state;
    const sel = st.selection.main;
    const line = st.doc.lineAt(sel.head);
    const picked = sel.empty ? "" : `  ·  ${(sel.to - sel.from).toLocaleString()} selected`;
    this.statusEl.textContent = `Ln ${line.number.toLocaleString()}, Col ${(sel.head - line.from + 1).toLocaleString()}${picked}`;
    this.infoEl.textContent = `${this.langName} · ${this.encLabel} · ${this.eol}`;
  }

  getTitle(): string { return this.source.name; }
  getSubtitle(): string {
    const n = this.view?.state.doc.lines ?? 0;
    return `${n.toLocaleString()} line${n === 1 ? "" : "s"} · ${this.langName}`;
  }
  isDirty(): boolean { return this.dirty; }

  async save(req: SaveRequest): Promise<SaveResult | null> {
    void req;
    if (this.readonlyReason) { this.ctx.toast(`This file is open read-only (${this.readonlyReason})`); return null; }
    const name = this.source.name;
    const doc = this.view.state.doc;
    const types: FilePickerAcceptType[] = [{ description: "Text file", accept: { "text/plain": [`.${extOf(name) || "txt"}`] } }];
    // Untouched: hand back the original bytes (mixed line endings and odd bytes survive).
    if (!this.dirty) return { blob: this.blob, suggestedName: name, types, replacesOriginal: true };
    let text = doc.toString();
    if (this.eol !== "LF") text = text.replaceAll("\n", EOL_CHARS[this.eol]!);
    let enc = this.encoding;
    let bytes = encodeText(text, enc);
    let bom = this.dec.bom;
    let encLabel = this.encLabel;
    if (!bytes) {
      enc = "utf-8"; bytes = encodeText(text, enc)!; bom = new Uint8Array(0);
      encLabel = "UTF-8";
      this.ctx.toast("Some characters don’t exist in the original encoding; saved as UTF-8", { timeout: 6000 });
    }
    const blob = new Blob([bom as BlobPart, bytes as BlobPart], { type: "text/plain" });
    const result: SaveResult = { blob, suggestedName: name, types, replacesOriginal: true };
    // Becomes the saved state only in onSaved(), after the shell really wrote it
    // (the user may cancel the save dialog, or keep typing while it is open).
    this.pendingSave = { result, doc, encoding: enc, encLabel, bom };
    return result;
  }

  onSaved(info: SavedInfo): void {
    const p = this.pendingSave;
    if (!p || info.result !== p.result) return;
    this.pendingSave = null;
    this.savedDoc = p.doc;
    this.blob = p.result.blob;
    this.encoding = p.encoding;
    this.encLabel = p.encLabel;
    this.dec.bom = p.bom;
    this.onDocChanged();
    this.refreshStatus();
  }

  async getInfo(): Promise<InfoSection[]> {
    const doc = this.view.state.doc;
    const text = doc.length <= 20_000_000 ? doc.toString() : "";
    const words = text ? (text.match(/\S+/g) ?? []).length : null;
    const rows: InfoSection["rows"] = [
      ["Language", this.langName],
      ["Lines", doc.lines.toLocaleString()],
      ["Words", words === null ? "—" : words.toLocaleString()],
      ["Characters", doc.length.toLocaleString()],
      ["Encoding", this.encLabel],
      ["Line endings", EOL_NAMES[this.eol] + (this.dec.mixed ? " (mixed; saved as one style)" : "")],
    ];
    if (this.readonlyReason) rows.push(["Mode", `Read-only — ${this.readonlyReason}`]);
    if (this.dirty) rows.push(["Status", "Edited (unsaved)"]);
    return [{ title: "Text", rows }];
  }

  destroy(): void {
    this.themeObs?.disconnect();
    this.view?.destroy();
    this.root?.remove();
  }
}

const mod: ViewerModule = { kind: "text", create: () => new TextViewer() };
export default mod;
