// The application shell (port of preview/app.py's window): header bar, sidebar
// and inspector slots, empty window with Open/drag & drop/recents, routing to
// viewers by kind, Get Info, save plumbing, toasts and shortcuts.
import { fileInfoSection, renderInfo } from "../inspector/index.ts";
import { detectKind, KIND_LABELS, type Kind } from "../shared/kinds.ts";
import { sendToSw } from "../shared/messages.ts";
import { VIEWER_PATH, type ViewerParams } from "../shared/viewer-url.ts";
import { receiveTransfer } from "../content/transfer.ts";
import { pickExportFormat, showDialog } from "../ui/dialog.ts";
import { button, el, icon, menuButton, separator } from "../ui/kit.ts";
import type {
  DocumentSource, SavedInfo, SaveRequest, Services, SidebarSlot, ToastOptions, Viewer, ViewerContext,
} from "../viewers/contract.ts";
import { REGISTRY } from "../viewers/registry.ts";
import {
  addRecent, clearRecent, ensurePermission, recentFiles, removeRecent, stashHandle, takeHandle, type RecentEntry,
} from "./handles.ts";
import { saveBlob } from "./save.ts";
import { displayChord, Shortcuts } from "./shortcuts.ts";
import { FileAccessError, HttpError, sourceFromFile, sourceFromHandle, sourceFromUrl } from "./source.ts";

const services: Services = {
  markup: async (opts) => (await import("../markup/index.ts")).createMarkup(opts),
  signatures: async () => (await import("../signature/index.ts")).createSignatureStore(),
  ocr: async () => (await import("../ocr/index.ts")).createOcr(),
};

/** Headings of viewer shortcuts in the Keyboard Shortcuts sheet (as in preview/app.py). */
const SHORTCUT_GROUPS: Record<Kind, string> = {
  pdf: "PDF", image: "Images", model: "3D Models", table: "Tables",
  text: "Text and Source Code", media: "Video and Audio",
};

function relativeTime(t: number): string {
  const s = (Date.now() - t) / 1000;
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (s < 60) return rtf.format(0, "second");
  if (s < 3600) return rtf.format(-Math.round(s / 60), "minute");
  if (s < 86400) return rtf.format(-Math.round(s / 3600), "hour");
  if (s < 86400 * 30) return rtf.format(-Math.round(s / 86400), "day");
  return new Date(t).toLocaleDateString();
}

function bigIcon(): SVGSVGElement {
  const i = icon("document");
  i.classList.add("big-icon");
  return i;
}

function pill<T extends HTMLElement>(b: T): T {
  b.classList.add("pill");
  return b;
}

export class Shell {
  readonly shortcuts = new Shortcuts();
  private headerStart = el("div.start");
  private headerTools = el("div.tools");
  private titleEl = el("div.title", { text: "View" });
  private subtitleEl = el("div.subtitle");
  private sidebarEl = el("aside.sidebar", { hidden: true, "aria-label": "Sidebar" });
  private inspectorEl = el("aside.inspector", { hidden: true, "aria-label": "Info" });
  private content = el("main.content");
  private toasts = el("div.toasts", { role: "status", "aria-live": "polite" });
  private sidebarBtn = button({ icon: "sidebar", title: "Show Sidebar (F9)", toggle: true, onClick: () => this.sidebar.setVisible(!this.sidebar.visible) });
  private infoBtn = button({ icon: "info", title: "Get Info (Ctrl+I)", toggle: true, onClick: () => this.toggleInspector() });

  private viewer: Viewer | null = null;
  private source: DocumentSource | null = null;
  private kind: Kind | null = null;
  private life: AbortController | null = null;
  private dirty = false;
  private handle: FileSystemFileHandle | undefined;
  /** The current viewer's context (also reachable from tests via globalThis.viewShell). */
  private ctx: ViewerContext | null = null;

  readonly sidebar: SidebarSlot;

  constructor(root: HTMLElement) {
    const sidebarEl = this.sidebarEl;
    const sidebarBtn = this.sidebarBtn;
    this.sidebar = {
      el: sidebarEl,
      get visible() { return !sidebarEl.hidden; },
      setVisible(v: boolean) {
        sidebarEl.hidden = !v;
        sidebarBtn.setAttribute("aria-pressed", String(v));
      },
    };
    this.sidebarBtn.hidden = true;
    this.infoBtn.disabled = true;

    const menu = menuButton({ icon: "menu", title: "Main Menu" }, [
      ["Open…", () => void this.openPicker(), displayChord("Mod+O")],
      ["Save", () => void this.save({ mode: "save" }), displayChord("Mod+S")],
      ["Save As…", () => void this.save({ mode: "save-as" }), displayChord("Mod+Shift+S")],
      ["Export…", () => void this.exportMenu(), displayChord("Mod+Shift+E")],
      ["Print…", () => void this.print(), displayChord("Mod+P")],
      "-",
      ["Open in Chrome", () => this.openOriginal()],
      "-",
      ["Keyboard Shortcuts", () => this.showShortcuts(), "?"],
      ["Preferences", () => void chrome.runtime.openOptionsPage()],
    ]);

    root.append(el("div.window", {},
      el("header.headerbar", {},
        el("div.start", {}, this.sidebarBtn, this.headerStart),
        el("div.titles", {}, this.titleEl, this.subtitleEl),
        this.headerTools,
        el("div.end", {}, separator(), this.infoBtn, menu),
      ),
      this.sidebarEl, this.content, this.inspectorEl,
    ), this.toasts);

    const s = this.shortcuts;
    s.add({ keys: "Mod+O", description: "Open", run: () => void this.openPicker() });
    s.add({ keys: "Mod+S", description: "Save", run: () => void this.save({ mode: "save" }) });
    s.add({ keys: "Mod+Shift+S", description: "Save As", run: () => void this.save({ mode: "save-as" }) });
    s.add({ keys: "Mod+Shift+E", description: "Export", run: () => void this.exportMenu() });
    s.add({ keys: "Mod+P", description: "Print", run: () => void this.print() });
    s.add({ keys: "Mod+I", description: "Get Info", run: () => this.toggleInspector() });
    s.add({ keys: "F9", description: "Toggle sidebar", run: () => { if (!this.sidebarBtn.hidden) this.sidebar.setVisible(!this.sidebar.visible); } });
    s.add({ keys: "F11", description: "Full screen", run: () => void this.toggleFullscreen() });
    s.add({ keys: "?", description: "Keyboard shortcuts", run: () => void this.showShortcuts() });
    s.add({ keys: "Mod+?", description: "Keyboard shortcuts", run: () => void this.showShortcuts() });

    this.setupDrop();
    addEventListener("beforeunload", (e) => { if (this.dirty) e.preventDefault(); });
  }

  // ---- entry points ----------------------------------------------------------

  async start(p: ViewerParams): Promise<void> {
    if (p.handle) {
      const h = await takeHandle(p.handle);
      if (h && (await ensurePermission(h, "read").catch(() => false))) {
        return this.open(await sourceFromHandle(h), null, p.hash);
      }
    }
    if (p.transfer) {
      // Embedded by the content script for a blob:/data: document: the bytes come by postMessage.
      const name = p.name || "document";
      this.setTitle(name, "Loading…");
      try {
        const t = await receiveTransfer(p.transfer);
        return this.open(sourceFromFile(new File([t.buffer], t.name || name, { type: t.mime })), p.kind ?? null, p.hash);
      } catch (e) {
        return this.showLoadError(e, sourceFromFile(new File([], name)));
      }
    }
    if (p.url) return this.open(sourceFromUrl(p.url), p.kind ?? null, p.hash);
    return this.showEmpty();
  }

  async open(source: DocumentSource, hint: Kind | null, hash = ""): Promise<void> {
    this.close();
    this.source = source;
    this.handle = source.handle;
    this.setTitle(source.name, "Loading…");
    document.documentElement.dataset.state = "loading";
    try {
      // The interception rule already decided the kind from Content-Type/extension:
      // trust it and let the viewer stream (no full download just to sniff).
      const kind = hint ?? detectKind({ name: source.name, mime: source.mime, head: await source.head() });
      if (!kind) return this.showUnsupported(source);
      if (source.handle) void addRecent(source.handle);
      // Re-attach the Referer of the intercepted navigation before anything fetches.
      if (source.url && /^https?:/i.test(source.url)) {
        await sendToSw({ type: "prepare-fetch", url: source.url }).catch(() => null);
      }
      await this.mountViewer(kind, source, hash);
    } catch (e) {
      this.showLoadError(e, source);
    }
  }

  private async mountViewer(kind: Kind, source: DocumentSource, hash: string): Promise<void> {
    const mod = await REGISTRY[kind]();
    const life = new AbortController();
    this.life = life;
    this.kind = kind;
    const viewer = mod.create();
    this.viewer = viewer;
    document.documentElement.dataset.viewKind = kind;
    const container = el("div.viewer-root", { style: "position:absolute;inset:0" });
    this.content.replaceChildren(container);
    const ctx: ViewerContext = {
      header: { start: this.headerStart, tools: this.headerTools },
      sidebar: this.sidebar,
      shortcuts: this.shortcuts.scoped(life.signal, SHORTCUT_GROUPS[kind]),
      services,
      signal: life.signal,
      hash,
      kind,
      toast: (m, o) => this.toast(m, o),
      refreshTitle: () => this.refreshTitle(),
      setDirty: (d) => { this.dirty = d; this.refreshTitle(); },
      openSources: (srcs) => void this.openInNewTabs(srcs),
      requestSave: (req) => (viewer === this.viewer ? this.requestSave(req) : Promise.resolve()),
    };
    this.ctx = ctx;
    await viewer.mount(container, source, ctx);
    if (this.viewer !== viewer) return; // replaced while loading
    this.sidebarBtn.hidden = this.sidebarEl.childElementCount === 0;
    this.infoBtn.disabled = false;
    this.refreshTitle();
    document.documentElement.dataset.state = "ready";
    if (!this.inspectorEl.hidden) void this.updateInspector();
  }

  /** Tear down the current viewer and all of its header/sidebar contributions. */
  close(): void {
    this.life?.abort();
    this.viewer?.destroy();
    this.viewer = null;
    this.ctx = null;
    this.source = null;
    this.kind = null;
    this.dirty = false;
    this.headerStart.replaceChildren();
    this.headerTools.replaceChildren();
    this.sidebarEl.replaceChildren();
    this.sidebar.setVisible(false);
    this.sidebarBtn.hidden = true;
    this.infoBtn.disabled = true;
    this.inspectorEl.hidden = true;
    delete document.documentElement.dataset.viewKind;
  }

  // ---- title / toast / inspector -------------------------------------------------

  private setTitle(title: string, subtitle: string): void {
    this.titleEl.textContent = title;
    this.titleEl.classList.toggle("dirty-dot", this.dirty);
    this.subtitleEl.textContent = subtitle;
    this.subtitleEl.hidden = !subtitle;
    document.title = `${this.dirty ? "• " : ""}${title}`;
  }

  refreshTitle(): void {
    const v = this.viewer;
    this.dirty = v?.isDirty?.() ?? this.dirty;
    this.setTitle(v?.getTitle?.() ?? this.source?.name ?? "View", v?.getSubtitle?.() ?? "");
  }

  toast(message: string, o: ToastOptions = {}): void {
    const t = el("div.toast", {}, el("span", { text: message }));
    const close = () => t.remove();
    if (o.actionLabel) {
      t.append(button({ label: o.actionLabel, title: o.actionLabel, onClick: () => { close(); o.onAction?.(); } }));
    }
    t.append(button({ icon: "close", title: "Dismiss", onClick: close }));
    this.toasts.append(t);
    const ms = o.timeout ?? 3000;
    if (ms > 0) setTimeout(close, ms);
  }

  toggleInspector(): void {
    if (!this.viewer) return;
    this.inspectorEl.hidden = !this.inspectorEl.hidden;
    this.infoBtn.setAttribute("aria-pressed", String(!this.inspectorEl.hidden));
    if (!this.inspectorEl.hidden) void this.updateInspector();
  }

  private async updateInspector(): Promise<void> {
    if (!this.source) return;
    const extra = (await this.viewer?.getInfo?.().catch(() => [])) ?? [];
    renderInfo(this.inspectorEl, [fileInfoSection(this.source), ...extra]);
  }

  // ---- open / drop ---------------------------------------------------------------

  async openPicker(): Promise<void> {
    let handles: FileSystemFileHandle[];
    try {
      handles = await window.showOpenFilePicker({ multiple: true });
    } catch {
      return; // cancelled
    }
    await this.openHandles(handles);
  }

  private async openHandles(handles: FileSystemFileHandle[]): Promise<void> {
    if (!handles.length) return;
    const [first, ...rest] = handles;
    if (!this.viewer && first) {
      await this.open(await sourceFromHandle(first), null);
    } else {
      rest.unshift(first!);
    }
    for (const h of rest) await this.openTab(`handle=${await stashHandle(h)}`);
  }

  private async openInNewTabs(sources: DocumentSource[]): Promise<void> {
    for (const s of sources) {
      if (s.handle) await this.openTab(`handle=${await stashHandle(s.handle)}`);
      else if (s.url) await this.openTab(`url=${s.url}`);
    }
  }

  private async openTab(query: string): Promise<void> {
    const tab = await chrome.tabs.getCurrent();
    await chrome.tabs.create({
      url: `${chrome.runtime.getURL(VIEWER_PATH)}?${query}`,
      index: tab ? tab.index + 1 : undefined,
      openerTabId: tab?.id,
    });
  }

  private setupDrop(): void {
    let overlay: HTMLElement | null = null;
    let depth = 0;
    const hasFiles = (e: DragEvent) => e.dataTransfer?.types.includes("Files");
    addEventListener("dragenter", (e) => {
      if (!hasFiles(e)) return;
      depth++;
      overlay ??= this.content.appendChild(el("div.drop-target"));
    });
    addEventListener("dragleave", () => {
      if (--depth <= 0) { depth = 0; overlay?.remove(); overlay = null; }
    });
    addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
    addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0; overlay?.remove(); overlay = null;
      // getAsFileSystemHandle must be called synchronously inside the drop handler.
      const items = [...(e.dataTransfer?.items ?? [])].filter((i) => i.kind === "file");
      const pending = items.map((i) => i.getAsFileSystemHandle?.() ?? Promise.resolve(null));
      const files = items.map((i) => i.getAsFile());
      void (async () => {
        const handles = await Promise.all(pending);
        const sources = await Promise.all(handles.map(async (h, i) =>
          h?.kind === "file" ? sourceFromHandle(h as FileSystemFileHandle)
            : files[i] ? sourceFromFile(files[i]!) : null));
        const list = sources.filter((s): s is DocumentSource => !!s);
        if (this.viewer?.acceptsDrop?.(list)) return this.viewer.addDropped?.(list);
        const fileHandles = handles.filter((h): h is FileSystemFileHandle => h?.kind === "file");
        if (fileHandles.length === list.length) return this.openHandles(fileHandles);
        if (!this.viewer && list[0]) await this.open(list[0], null);
      })();
    });
  }

  // ---- save / export / print ----------------------------------------------------

  async save(req: SaveRequest): Promise<void> {
    if (!this.viewer?.save) {
      if (this.viewer) this.toast("This document can't be edited, so there is nothing to save");
      return;
    }
    try {
      const r = await this.viewer.save(req);
      if (!r) return;
      const viewer = this.viewer;
      const out = await saveBlob(r, { handle: this.handle, forcePicker: req.mode !== "save" });
      if (out.kind === "cancelled") return; // nothing written: the viewer stays dirty
      if (out.kind !== "downloaded" && r.replacesOriginal) this.handle = out.handle;
      const info: SavedInfo = {
        mode: req.mode,
        ...(req.format ? { format: req.format } : {}),
        result: r,
        replacesOriginal: r.replacesOriginal,
        destination: out.kind,
        name: out.kind === "downloaded" ? out.name : out.handle.name,
        ...(out.kind !== "downloaded" ? { handle: out.handle } : {}),
      };
      if (viewer === this.viewer) {
        try {
          await viewer?.onSaved?.(info);
        } catch (e) {
          console.error("onSaved failed", e);
        }
        if (r.replacesOriginal) this.dirty = false;
        this.refreshTitle();
      }
      this.toast(out.kind === "downloaded" ? `Downloaded “${out.name}”` : "Saved");
    } catch (e) {
      this.toast(`Couldn't save: ${(e as Error).message}`, { timeout: 6000 });
    }
  }

  /** ViewerContext.requestSave: same path as the shell's own Save/Export commands. */
  private async requestSave(req: SaveRequest): Promise<void> {
    if (req.mode === "export" && !req.format) return this.exportMenu(req);
    return this.save(req);
  }

  private async exportMenu(base: Partial<SaveRequest> = {}): Promise<void> {
    if (!this.viewer) return;
    const formats = this.viewer.exportFormats?.() ?? [];
    if (!formats.length) return this.toast("This document has no export formats");
    const format = await pickExportFormat(formats, `Export “${this.source?.name ?? "Document"}”`);
    if (format) await this.save({ ...base, mode: "export", format });
  }

  private async print(): Promise<void> {
    if (this.viewer?.print) await this.viewer.print();
    else window.print();
  }

  private openOriginal(): void {
    const url = this.source?.url;
    if (url && /^(https?|file):/.test(url)) void sendToSw({ type: "open-original", url });
    else this.toast("This document was opened from your computer");
  }

  private async toggleFullscreen(): Promise<void> {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  }

  private shortcutsOpen = false;

  /** Keyboard Shortcuts sheet (desktop: Adw.ShortcutsDialog), built from what is registered now. */
  private async showShortcuts(): Promise<void> {
    if (this.shortcutsOpen) return;
    this.shortcutsOpen = true;
    const body = el("div.shortcuts");
    for (const [group, rows] of this.shortcuts.sheet()) {
      body.append(el("section.shortcut-group", {}, el("h3", { text: group }),
        ...rows.map(([keys, desc]) => el("div.shortcut-row", {}, el("span", { text: desc }),
          el("span", {}, ...keys.flatMap((k, i) => [i ? " " : "", el("kbd", { text: k })]))))));
    }
    body.append(el("p.note", { text: "Chrome keeps Ctrl+W, Ctrl+T and Ctrl+N for itself." }));
    try {
      await showDialog({ title: "Keyboard Shortcuts", body, width: 520 });
    } finally {
      this.shortcutsOpen = false;
    }
  }

  // ---- status pages ------------------------------------------------------------

  private statusPage(state: string, title: string, text: string, ...actions: HTMLElement[]): HTMLElement {
    document.documentElement.dataset.state = state;
    const page = el("div.status-page", { "data-status": state },
      bigIcon(),
      el("h1", { text: title }),
      el("p", { text }),
      el("div.actions", {}, ...actions),
    );
    this.content.replaceChildren(page);
    return page;
  }

  async showEmpty(): Promise<void> {
    this.close();
    this.setTitle("View", "");
    const page = this.statusPage("empty", "Open a File",
      "PDFs, images, 3D models, tables, text and video. Drop files here or choose them.",
      pill(button({ label: "Open…", title: "Open (Ctrl+O)", suggested: true, onClick: () => void this.openPicker() })),
    );
    await this.renderRecent(page);
  }

  /** "Recent" list on the empty window: open, forget one, clear all; stale entries drop out. */
  private async renderRecent(page: HTMLElement): Promise<void> {
    page.querySelector(".recent")?.remove();
    const recent = await recentFiles().catch((): RecentEntry[] => []);
    if (!recent.length) return;
    const list = el("div.recent", { "aria-label": "Recent files" },
      el("div.recent-head", {}, el("h2", { text: "Recent" }),
        el("button.link-btn", { type: "button", text: "Clear", onclick: async () => {
          await clearRecent();
          await this.renderRecent(page);
        } })));
    for (const r of recent) {
      const openIt = async () => {
        try {
          if (!(await ensurePermission(r.handle, "read"))) return;
          await this.open(await sourceFromHandle(r.handle), null);
        } catch (e) {
          if ((e as DOMException).name === "NotFoundError") {
            await removeRecent(r.handle);
            this.toast(`“${r.name}” was moved or deleted`);
            await this.renderRecent(page);
          } else {
            this.toast(`Couldn't open “${r.name}”: ${(e as Error).message}`);
          }
        }
      };
      list.append(el("div.recent-item", {},
        el("button.menu-item", { type: "button", title: r.name, onclick: () => void openIt() },
          el("span", { text: r.name }), el("span.when", { text: relativeTime(r.opened) })),
        button({ icon: "close", title: `Remove “${r.name}” from Recent`, onClick: async () => {
          await removeRecent(r.handle);
          await this.renderRecent(page);
        } })));
    }
    page.append(list);
  }

  private showUnsupported(source: DocumentSource): void {
    this.setTitle(source.name, "");
    this.statusPage("unsupported", "View can't open this file",
      `“${source.name}” is not a kind of file View knows (${Object.values(KIND_LABELS).join(", ")}).`,
      ...(source.url ? [button({ label: "Open in Chrome", title: "Open in Chrome", onClick: () => this.openOriginal() })] : []));
  }

  private showLoadError(e: unknown, source: DocumentSource): void {
    console.error(e);
    this.setTitle(source.name, "");
    if (e instanceof FileAccessError) {
      const page = this.statusPage("file-access", "Allow access to file URLs",
        "To open local files from the address bar, turn on “Allow access to file URLs” for View on the extensions page, then reload. Or choose the file with Open…",
        button({ label: "Open Extension Settings", title: "Open Extension Settings", suggested: true, onClick: () => void sendToSw({ type: "open-extension-settings" }) }),
        button({ label: "Open…", title: "Open (Ctrl+O)", onClick: () => void this.openPicker() }),
      );
      page.append(el("p", {}, el("code", { text: decodeURI(e.url) })));
      return;
    }
    const msg = e instanceof HttpError ? e.message : `Couldn't load “${source.name}”: ${(e as Error)?.message ?? e}`;
    this.statusPage("error", "Couldn't open the document", msg,
      ...(source.url ? [button({ label: "Open in Chrome", title: "Open in Chrome", onClick: () => this.openOriginal() })] : []));
  }
}
