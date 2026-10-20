// The contract between the shell (src/shell) and the document viewers
// (src/viewers/<kind>). Port of preview/viewer_base.py to the browser.
//
// Lifecycle, driven by the shell:
//   const v = (await REGISTRY[kind]()).create();
//   await v.mount(container, source, ctx);   // container is empty and sized
//   ... user works; v calls ctx.setDirty()/ctx.refreshTitle() ...
//   await v.save?.({...}) when the user presses Ctrl+S (shell writes the bytes)
//   v.destroy();                              // also aborts ctx.signal
//
// Rules for viewers:
//  - Never block the main thread with parsing/rendering: use Workers.
//  - Only touch the DOM you are given: `container`, and the header/sidebar slots
//    in ctx. Don't add an Info button (the shell owns Get Info, Ctrl+I) or a
//    Save/Open button (the shell owns those too).
//  - Register shortcuts through ctx.shortcuts so they show up in the help and
//    are removed on destroy.
//  - Everything you add listeners to must be cleaned up via ctx.signal.
import type { Kind } from "../shared/kinds.ts";
import type { MarkupController, MarkupOptions } from "../markup/api.ts";
import type { OcrService } from "../ocr/api.ts";
import type { SignatureStore } from "../signature/api.ts";

/** A document to open. Created by the shell from a URL, a File or a FileSystemFileHandle. */
export interface DocumentSource {
  /** Display file name, e.g. "report.pdf". */
  readonly name: string;
  /** Original URL (http(s), file:, blob:) when the document came from one. */
  readonly url?: string;
  /** Writable handle when opened via the picker or drag & drop (enables in-place save). */
  readonly handle?: FileSystemFileHandle;
  /** Content-Type reported by the server / File.type. */
  readonly mime: string | null;
  /** Size in bytes when known (Content-Length / File.size). Updated after blob(). */
  readonly size: number | null;
  readonly lastModified: number | null;
  /** The full contents (fetched once, then cached). */
  blob(): Promise<Blob>;
  /** First `n` bytes (default 8 KB) for sniffing. */
  head(n?: number): Promise<Uint8Array>;
  /**
   * True if `url` can be handed to a range-capable loader (pdf.js) directly,
   * i.e. it is http(s) and the extension can fetch it with the user's cookies.
   */
  readonly streamable: boolean;
}

export interface HeaderSlots {
  /** Left of the title: navigation (page back/forward, sidebar toggles are the shell's). */
  readonly start: HTMLElement;
  /** Right of the title: the viewer's tools (zoom, rotate, markup toggle, search). */
  readonly tools: HTMLElement;
}

export interface SidebarSlot {
  /** Empty element the viewer fills (thumbnails, image list, outline). */
  readonly el: HTMLElement;
  /** Show/hide; the shell also toggles it with its sidebar button (F9). */
  setVisible(visible: boolean): void;
  readonly visible: boolean;
}

export interface Shortcut {
  /** "Mod+=", "Mod+Shift+S", "Delete", "ArrowRight", "Space". Mod = Ctrl (Cmd on macOS). */
  readonly keys: string;
  readonly description: string;
  /** Return false to let the event continue (not preventDefault'ed). */
  run(ev: KeyboardEvent): boolean | void;
}

export interface ShortcutRegistry {
  /** Returns an unregister function. Also auto-unregistered on destroy. */
  add(shortcut: Shortcut): () => void;
}

export interface ToastOptions {
  actionLabel?: string;
  onAction?: () => void;
  /** ms; default 3000. 0 = until dismissed. */
  timeout?: number;
}

/** Lazily-loaded shared services. Each import is code-split; don't call them at mount unless needed. */
export interface Services {
  markup(opts: MarkupOptions): Promise<MarkupController>;
  signatures(): Promise<SignatureStore>;
  ocr(): Promise<OcrService>;
}

export interface ViewerContext {
  readonly header: HeaderSlots;
  readonly sidebar: SidebarSlot;
  readonly shortcuts: ShortcutRegistry;
  readonly services: Services;
  /** Aborted when the viewer is destroyed: pass it to addEventListener/fetch. */
  readonly signal: AbortSignal;
  /** Location fragment the document was opened with ("#page=3"), or "". */
  readonly hash: string;
  /** The kind the interception rule or sniffing decided. */
  readonly kind: Kind;
  toast(message: string, opts?: ToastOptions): void;
  /** Call after title/subtitle (getTitle/getSubtitle) changed. */
  refreshTitle(): void;
  /** Mark the document modified (shows • in the title, enables Save, warns on close). */
  setDirty(dirty: boolean): void;
  /** Open more documents (each in a new tab), e.g. "Open selected pages". */
  openSources(sources: DocumentSource[]): void;
  /**
   * Ask the shell to save/export, exactly as if the user pressed Mod+S /
   * Mod+Shift+S / chose Export… (use it for your own Save or Export buttons).
   * The shell calls back into Viewer.save(req) and routes the result through the
   * usual path: overwrite the opened file handle, else the save dialog, else a
   * download. For mode "export" without `format`, the shell first shows the
   * export picker (Viewer.exportFormats()) and calls save() with the choice;
   * with `format`, the picker is skipped. Resolves when done, cancelled or
   * failed (failures are shown as a toast, never thrown).
   */
  requestSave(req: SaveRequest): Promise<void>;
}

/** Get Info content, like viewer_base.get_info(): [(section, [(label, value)])]. */
export interface InfoSection {
  title: string;
  /** Values written as "[text](https://url)" are rendered as links. */
  rows: [label: string, value: string][];
}

export interface SaveRequest {
  /** "save" = overwrite/same format; "export" = viewer-specific format chosen by the user. */
  mode: "save" | "save-as" | "export";
  /** For export: a format id the viewer offered via exportFormats(). */
  format?: string;
  /** Viewer-specific: e.g. PDF "only selected pages" (Ctrl+S with a page selection). */
  selectionOnly?: boolean;
}

export interface SaveResult {
  blob: Blob;
  suggestedName: string;
  /** For the save picker's type filter. */
  types?: FilePickerAcceptType[];
  /** True if this replaces the original document (shell overwrites the handle when it has one). */
  replacesOriginal: boolean;
}

/** Passed to Viewer.onSaved after the shell has written a SaveResult. */
export interface SavedInfo {
  /** The request that produced it (Mod+S → "save", Mod+Shift+S → "save-as", Export… → "export"). */
  mode: SaveRequest["mode"];
  /** Export format id, when mode is "export". */
  format?: string;
  /** The exact object your save() returned (compare by identity to match it to your pending state). */
  result: SaveResult;
  /** Same as result.replacesOriginal: true for Save / Save As (the written file is now the document). */
  replacesOriginal: boolean;
  /** "overwritten": the opened file; "saved-as": a file picked in the save dialog; "downloaded": the download fallback. */
  destination: "overwritten" | "saved-as" | "downloaded";
  /** Name of the file written. */
  name: string;
  /** The handle written to (absent for downloads). After a replacesOriginal save it is the document's handle. */
  handle?: FileSystemFileHandle;
}

export interface ExportFormat {
  id: string;
  label: string; // "PNG Images…", "Encrypted PDF…"
}

export interface Viewer {
  mount(container: HTMLElement, source: DocumentSource, ctx: ViewerContext): Promise<void>;
  getTitle?(): string;
  /** "Page 2 of 7", "1920 × 1080", "1,000,000 rows". */
  getSubtitle?(): string;
  isDirty?(): boolean;
  /** Serialize for saving. Omit if the viewer is read-only. */
  save?(req: SaveRequest): Promise<SaveResult | null>;
  /**
   * Called only after the shell has actually written what save() returned — not
   * when the user cancels the save dialog or the write fails. Clear your dirty
   * state HERE, not in save(). The shell awaits it, then refreshes the title.
   */
  onSaved?(info: SavedInfo): void | Promise<void>;
  exportFormats?(): ExportFormat[];
  print?(): Promise<void>;
  getInfo?(): Promise<InfoSection[]>;
  /** Files dropped onto an open document (insert pages, add images). */
  acceptsDrop?(sources: DocumentSource[]): boolean;
  addDropped?(sources: DocumentSource[]): Promise<void>;
  destroy(): void;
}

export interface ViewerModule {
  readonly kind: Kind;
  create(): Viewer;
}
