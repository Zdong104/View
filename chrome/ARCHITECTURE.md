# View for Chrome: architecture

View for Chrome is the Manifest V3 extension version of View. It becomes Chrome's default viewer for the file kinds the user turns on, and it is meant to reach feature parity with the GTK app in `preview/`. Distribution is the Chrome Web Store only (no hosted PWA). This document is the contract for engineers building the viewers in parallel. If something here is wrong, fix the document in the same PR as the code.

```
cd chrome
npm install          # also downloads Chrome for Testing into chrome/.cache/puppeteer
npm run build        # -> chrome/dist/ (load unpacked from chrome://extensions)
npm test             # typecheck + license audit + build + unit + e2e (headless Chrome for Testing)
npm run watch        # rebuild on change
npm run package      # minified build, zipped as view-chrome-<version>.zip for the Web Store
HEADFUL=1 npm run test:e2e   # watch the e2e run
```

---

## 1. Interception: how View becomes the default viewer

### 1.1 Mechanism: declarativeNetRequest redirects on response headers

The design follows the MV3 pdf.js Chromium extension (`mozilla/pdf.js/extensions/chromium/pdfHandler.js`). The relevant code is in `src/background/rules.ts`, a pure function covered by unit tests, and `src/background/sw.ts`.

- **Requirement: Chrome 128 or newer** (`minimum_chrome_version: "128"`). From that version, DNR rules can match on `condition.responseHeaders`. Chrome evaluates them when the response headers of a navigation arrive and **redirects the navigation** to `viewer.html?kind=<kind>&url=<original URL>`. MV3 has no blocking `webRequest`, so DNR is the only option. Chrome 123–127 accept the condition but ignore it (pdf.js works around that). We don't need to, because we require 128 and the stable channel is at 155.
- The rules are **dynamic** rules, rebuilt from the user's preferences on `runtime.onInstalled`, once per browser session (a `storage.session` flag, because `onInstalled` doesn't fire in every case, e.g. incognito), and on every change to `storage.sync` prefs. They can't be static rulesets: `regexSubstitution` needs the full `chrome-extension://<id>/` URL, and the ID differs between unpacked and store builds.
- **URL format**: `url=` is always the last parameter and is **not percent-encoded**. DNR can only paste the match verbatim (`\0`), so `parseViewerUrl()` takes everything after `url=` literally. The original fragment (`#page=3`) ends up in `location.hash`, and the shell hands it to the viewer as `ctx.hash`.
- `viewer.html` is listed in `web_accessible_resources` for `<all_urls>`. This is needed so web pages and iframes can be redirected to it.

Rule priorities (higher wins; an `allow` rule stops lower-priority rules):

| Priority | Rule | Notes |
|---|---|---|
| 100 | Per-tab "Open in Chrome" bypass | A session rule scoped to `tabIds` and the exact URL (`src/background/bypass.ts`). Added by the viewer menu, removed when that navigation commits or fails (`webNavigation.onCommitted`/`onErrorOccurred`; a download ends in an error) or the tab closes. Pending bypasses are kept in `storage.session`, so a service-worker restart doesn't leak the rule. A viewer embedded in a page opens the original in a new tab. |
| 90 | `sub_frame` + `Content-Disposition: attachment*` → allow | Never take over a download inside a frame. |
| 80 | PDF `main_frame`, URL contains `=download` + attachment → allow | Keeps Google Drive-style downloads working (as pdf.js does). |
| 70 | PDF redirects (`main_frame` + `sub_frame`, never POST): (a) `Content-Type` is `application/pdf` (or `x-pdf`), with or without parameters; (b) generic type (`octet-stream` and similar) and `.pdf` in the URL; (c) generic type and `Content-Disposition` names a `*.pdf` file | (c) uses the pdf.js double negation, because `responseHeaders` is OR-ed. A PDF in the main frame is shown even when it is sent as an attachment, because servers mislabel PDFs all the time. |
| 60 | `main_frame` attachment → allow | Only when the "Respect download links" pref is on (the default). Applies to non-PDF kinds. |
| 50 | Other kinds by `Content-Type` (`src/shared/kinds.ts` `MIMES`) | `main_frame` only. SVG is deliberately excluded, because SVGs are often interactive pages. |
| 40 | Other kinds by URL path extension when the type is generic (`octet-stream`, or `text/plain` for non-text kinds) | Extensions are chunked to at most 40 characters per regex. **DNR rejects any regex whose compiled, case-insensitive program is over 2 KB.** Twelve long names such as `gitattributes` already overflow it, and the e2e test caught exactly that. |
| 30 | `file://` URLs by extension | Has an effect only when "Allow access to file URLs" is on. |

**Default kinds**: PDF, 3D models and tables are on. Images, text/code and video/audio are off, because Chrome's own handling of those is adequate. The user can change all of this on the options page (`options.html`), and the toolbar menu links to it.

### 1.2 Edge cases

| Case | Behaviour |
|---|---|
| `file://` with file access (on by default for `--load-extension` builds; the user must turn it on for store installs) | The DNR rule at priority 30 redirects. The viewer reads the file with `XMLHttpRequest`, because `fetch()` doesn't support `file:`. |
| `file://` without file access | DNR doesn't see the navigation, but `webNavigation.onBeforeNavigate` (filter `schemes: ["file"]`) does. `tabs.update` replaces the tab with the viewer, which shows "Allow access to file URLs" with a button that opens `chrome://extensions/?id=…`, plus an **Open…** fallback that uses the File System Access picker. |
| `Content-Disposition: attachment` | Frames: never taken over. Main frame: PDFs are taken over (pdf.js behaviour); other kinds follow the "Respect download links" pref. |
| POST-loaded documents (form submissions) | Not taken over (`excludedRequestMethods: ["post"]`). The viewer can't replay a POST and MV3 can't read response bodies, so Chrome's native viewer keeps them. Covered by the e2e test. |
| `<iframe src=x.pdf>` | `sub_frame` rules redirect the frame to the viewer. Covered by the e2e test. |
| `<embed>` / `<object>` PDFs (`type=application/pdf`, or no type and a `.pdf` URL) | **Done** (`src/content/detect.ts` + `content.css`, all frames, `document_start`). This is pdf.js's technique: a CSS-animation hook (`animationstart`) detects the elements. An `<embed>` is kept for page scripts but switched to `type=text/html` + the viewer URL; an `<object>` gets a full-size viewer `<iframe>` child and a non-PDF type. Chrome's own `<embed name=plugin>` inside a PDF document is skipped. Nothing happens when the PDF pref is off. |
| `blob:` PDFs (in a tab, an iframe or an embed) | **Done.** The extension origin can't fetch a page's `blob:` URL, but the content script (in the page's origin, `match_origin_as_fallback: true`) can. A `blob:` document whose `contentType` is `application/pdf` is covered by a viewer iframe in a closed shadow root on `<body>`. A `blob:` embed or object gets a viewer frame. In both cases the bytes go by a **postMessage handshake** (`src/content/transfer.ts`): `viewer.html?kind=pdf&transfer=<nonce>&name=…` posts `view:transfer-ready` to its parent, and the content script answers with the `ArrayBuffer` (transferred) to the extension origin only. A hostile page could answer itself, but that only shows bytes it already controls. |
| `data:` PDFs | **Done for iframes.** A `data:` document has an opaque origin, and Chrome refuses to load web-accessible extension pages into it (verified). So the *parent* page's content script retargets `<iframe src="data:application/pdf…">` to a transfer viewer frame and fetches the `data:` URL itself. A top-level `data:` PDF is blocked by Chrome unless the user types it in; if so, it stays in Chrome's viewer. |
| Context menu | "Open Link in View" and "Open in View" (on images, video and audio) open `viewer.html?url=…` in a new tab. |
| Toolbar button | Opens an empty viewer tab. It offers **Open…** (`showOpenFilePicker`, multiple files: the first opens in this tab, the rest in new tabs via `?handle=<id>`, an IndexedDB stash), drag and drop (`DataTransferItem.getAsFileSystemHandle()`, which gives writable handles), and a **Recent** list of stored handles. |

### 1.3 Fetching the bytes

- **The viewer re-fetches the URL itself**, so the document is requested twice; the first response is cancelled at the header stage. The request runs from the extension page with `credentials: "include"`. Host permission `<all_urls>` bypasses CORS.
  - **Cookies, verified (Chrome for Testing 155, `foundation-network.test.mjs`)**: the re-download gets **all** of the site's cookies, including `SameSite=Lax` and `SameSite=Strict`, and still does with "Block third-party cookies" on. The test proves the setting is active: a cross-site iframe gets no cookie under it. Chrome treats requests from an extension page with host permission to that host as first-party. Not covered: partitioned (CHIPS) cookies set inside a third-party iframe, which belong to a different partition key, and enterprise policies that block cookies for extensions.
  - **Referer, done** (`src/background/referer.ts`, a port of pdf.js `preserve-referer.js`):
    - `webRequest.onSendHeaders` (`extraHeaders`) remembers the Referer of main- and sub-frame requests in memory, by requestId.
    - `webRequest.onBeforeRedirect` sees DNR's redirect to `viewer.html` and stores `{url, referer}` per tab and frame in `storage.session`. The context menu stores the page URL as the Referer.
    - Before mounting a viewer for an http(s) URL, the shell sends `prepare-fetch`. The service worker then adds a session DNR `modifyHeaders` rule that sets `Referer` for exactly that URL, with `initiatorDomains: [extension id]`, `tabIds: [tab]` and resource types xmlhttprequest, media, image and other, so pdf.js range requests and `<video src>` get it too. The rules are removed when the tab closes.
    - The e2e test serves a PDF that returns 403 without the right Referer.
    - Permission `webRequest` is observation only and adds no new install warning.
- **Large files**: the shell never downloads a document just to sniff it when the rule already gave the kind. `DocumentSource.streamable` tells the PDF viewer to pass the URL to pdf.js, which then uses HTTP Range requests (`disableAutoFetch`, 256 KB chunks), so the first page appears fast on 600 MB files.
  - Local files from a handle are `File` objects: use `Blob.slice()`, or pdf.js `PDFDataRangeTransport` over it.
  - `file://` URLs: Chrome's file loader is expected to honour `Range` (verify this first). If it does, stream A should build a `PDFDataRangeTransport` over ranged XHR instead of reading the whole file.
- **Kind detection** for documents opened without a rule hint (picker, drop, context menu), in `src/shared/kinds.ts`, which mirrors `preview/viewer_base.py`: magic bytes first, then `Content-Type` (a generic `text/plain` loses to a more specific extension), then the file name, then the hint, then text sniffing (no NUL byte and valid UTF-8 in the first 8 KB).

---

## 2. Libraries

**Rule**: everything shipped must be MIT, Apache-2.0, BSD or ISC (or similar). `npm test` enforces this: `scripts/licenses.mjs` audits the production dependency tree, 83 packages and all permissive today. View is PolyForm-Noncommercial, so **no GPL, AGPL or LGPL**. That rules out mupdf.js, ghostscript, libheif/heic2any, ffmpeg.wasm and poppler.

All the dependencies below are **already in `package.json`**, so streams don't fight over `package-lock.json`. To add another one, send a one-line PR to the foundation owner and run `npm run licenses`.

| Need | Choice | License | Verified / notes |
|---|---|---|---|
| PDF render, text layer, search, forms, XFA | `pdfjs-dist` 6.4 (`build/pdf.mjs` + `web/pdf_viewer.mjs`) | Apache-2.0 | Wired up and tested. The worker, wasm decoders (openjpeg, jbig2, qcms), CMaps, standard fonts and ICC profiles are copied to `dist/vendor/pdfjs/` (`src/viewers/pdf/copy.json`). `isEvalSupported: false` because the CSP forbids eval. Form values can be saved with `PDFDocumentProxy.saveDocument()`. |
| PDF editing (page ops, merge, annotations, signatures, encryption) | `@cantoo/pdf-lib` 2.11 (maintained fork of pdf-lib) | MIT | **Encryption confirmed** in the package README and code (`core/security/PDFSecurity`): `doc.encrypt({userPassword, ownerPassword, permissions})` uses AES-256 R6 by default, and AES-128 or RC4 are opt-in. It can also load encrypted PDFs with `{password}`. Upstream `pdf-lib` 1.17 has been unmaintained since 2021 and can't encrypt. |
| 3D | `three` 0.186 (+ `examples/jsm` STLLoader, OBJLoader, PLYLoader, OrbitControls) | MIT | |
| CSV/TSV | `papaparse` 5.7 | MIT | Run `Papa.parse` inside **our own** module Worker. Its built-in `worker: true` mode builds a worker from its own script URL, which breaks in a bundle. |
| Text/code viewer and editor | `codemirror` 6 + `@codemirror/language-data` | MIT | Chosen over highlight.js and shiki. The desktop text view is editable, and CodeMirror 6 virtualizes the viewport (handles multi-MB files), loads languages lazily, and has search built in. highlight.js would need a hand-written virtualized renderer. |
| EXIF/GPS/XMP/ICC | `exifr` 7 | MIT | To keep EXIF on save (a canvas drops it): for JPEG, splice the original APP1 segment back in; for PNG and WebP, write an `eXIf` chunk / `EXIF` chunk (about 50 lines; no extra dependency). |
| Animation decode | **WebCodecs `ImageDecoder`** (built in) | n/a | Decodes GIF, WebP, APNG and AVIF frames with timing. No library needed. |
| Animation encode | `gifenc` (GIF), `upng-js` (APNG) | MIT | Animated WebP: encode each frame with `OffscreenCanvas.convertToBlob({type:"image/webp"})` and mux `ANIM`/`ANMF` RIFF chunks ourselves (about 150 lines, no dependency). |
| Ink smoothing | `perfect-freehand` | MIT | Pressure-aware stroke outlines for sketch, draw and signatures. |
| Zip (export many pages or images when no directory picker is available) | `fflate` | MIT | |
| OCR (Live Text) | `onnxruntime-web` 1.30 + PaddleOCR PP-OCR mobile det/cls/rec ONNX models (the same family RapidOCR uses on the desktop) | MIT; models Apache-2.0 | **Chosen over tesseract.js** (Apache-2.0, LSTM). PP-OCR is much better on Chinese and on photos or screenshots, and it matches the desktop's results exactly. tesseract.js would need `chi_sim` traineddata (about 20 MB) and is clearly worse on mixed CJK. |
| TIFF | `utif2` (UTIF.js) | MIT | **Wired.** `src/viewers/image/tiff.ts`, lazily imported in the image worker. Decodes the first full-resolution page and applies the Orientation tag. Uncompressed, Deflate, LZW, PackBits and JPEG-in-TIFF are supported. |
| Fonts in saved PDF annotations | `@pdf-lib/fontkit` (parsing installed fonts only) + own TrueType builder | MIT | **Fonts are embedded only through `src/markup/pdf-fonts.ts` and `src/markup/ttf-build.ts`**: it builds a minimal glyf font with just the needed glyphs, and pdf-lib embeds it whole. Never call `embedFont(…, { subset: true })`: @cantoo/pdf-lib expects `@cantoo/fontkit`, and its subset path breaks with `@pdf-lib/fontkit`. |
| CodeMirror packages | `@codemirror/{state,view,language,commands,search}`, `@lezer/highlight` | MIT | Declared explicitly (the text viewer imports them directly) at the versions the lockfile had resolved. |
| JPEG XL / HEIC | — | — | **Limitation.** Chrome decodes neither, and the HEIC decoders (libheif) are LGPL. JXL via `@jsquash/jxl` (Apache-2.0, libjxl BSD) is possible in Phase 3. HEIC: show "can't open" and suggest converting first. |
| Video/audio | `<video>` / `<audio>` | n/a | Chrome codecs only: H.264, HEVC (where the hardware supports it), VP8/VP9, AV1, AAC, MP3, Opus, Vorbis, FLAC and WAV, in MP4/WebM/Ogg/MKV. **AVI, WMV and some 3GP files won't play** (ffmpeg.wasm is LGPL/GPL). |

### 2.1 MV3 constraints and how we meet them

- **No remote code.** Every script, worker and wasm file ships in the package. `extension_pages` CSP is `script-src 'self' 'wasm-unsafe-eval'; object-src 'self'`, and `'wasm-unsafe-eval'` is required for pdf.js, onnxruntime and any jsquash codec. Workers are module workers loaded via `chrome.runtime.getURL()`.
- **Data** (models, fonts) may legally be fetched at runtime. Decision: **ship the OCR models in the package.**
  - Size, measured: 14 MB for `ort-wasm-simd-threaded.wasm` (the only ORT variant we copy) plus 31 MB of PP-OCR models and dictionary (`dist/ocr/models`), so the extension grows by about 46 MB. That is far below the Web Store's 2 GB limit. The models are not committed to git: `scripts/models.mjs` fetches them during the build from RapidOCR's pinned URLs and checks their SHA-256 (see `src/ocr/MODELS.md`).
  - Reasons: it works offline, sends nothing to a third party, and the review is simpler.
  - Fallback if download size becomes a complaint: download the models on first use from a pinned GitHub release, check the SHA-256, and store them in IndexedDB. Only the models can be fetched this way; the wasm is code and must stay in the package.
- **OCR threading**: extension pages are not cross-origin isolated by default, so there is no `SharedArrayBuffer`. ORT then falls back to `numThreads = 1` inside our dedicated Worker. That's fine for Live Text, at about 1–2 s per page.
  - Turning on `cross_origin_embedder_policy: require-corp` for the whole extension would risk breaking cross-origin fetches of documents.
  - If multi-threaded OCR is ever needed, give OCR its own isolated page (an offscreen document) instead.
- The current release zip is **2.8 MB**: shell plus pdf.js. The expected v1 size is about 35 MB with OCR.

---

## 3. Tooling, layout, contracts, UI

### 3.1 Tooling

- **Language: TypeScript, bundled with esbuild.** The types are what let six streams build against `contract.ts` and `markup/api.ts` without talking to each other. esbuild needs no config framework: `scripts/build.mjs` is about 120 lines and handles MV3 multi-entry builds plus code splitting.
- **Why not Vite**: an MV3 build needs plugins (crxjs) and a dev server that doesn't fit extensions well.
- **Type-checking**: `tsc` 7 (the native compiler, fast), with `--noEmit` and `strict`, `noUncheckedIndexedAccess`, `erasableSyntaxOnly` (no enums or parameter properties) and `verbatimModuleSyntax`. Imports use explicit `.ts` extensions.
- **No UI framework.** The viewers are canvas-heavy and virtualized, so plain DOM plus the small kit in `src/ui/kit.ts` keeps them fast and dependency-free.
- **Build conventions**, so that no stream has to edit `scripts/build.mjs`:
  - `src/viewers/<kind>/style.css` → `dist/viewers/<kind>.css`. Load it with `loadStyle(kind)` from `ui/kit.ts`; the shell CSS doesn't include it.
  - `src/**/copy.json` → `[{from, to}]` copies vendor wasm, workers, models and fonts into `dist/`.
  - `src/**/<name>.worker.ts` → `dist/<same path>/<name>.worker.js`, a self-contained ESM bundle (no code splitting). Start it with `new Worker(chrome.runtime.getURL("viewers/pdf/foo.worker.js"), { type: "module" })`. Existing workers were left as they are: the OCR and image workers start themselves from their own lazily imported chunk (`workerUrl = import.meta.url`), and the table and model workers are plain JS copied by `copy.json`.
  - `src/<area>/dev/<name>.{html,ts}` → `dist/dev/<area>-<name>.*`, dev builds only. These are standalone pages for e2e-testing a module; see `src/markup/dev/playground.*`.
  - Every `import()` of a viewer or service becomes its own chunk, so opening a CSV never loads pdf.js.
  - The service worker is one ESM file with no dynamic import (MV3 workers can't use it).

### 3.2 Layout (owner in brackets; see §7)

```
chrome/
  package.json  tsconfig.json  .puppeteerrc.cjs  ARCHITECTURE.md
  scripts/  build.mjs  unit.mjs  licenses.mjs  package.mjs  icons.sh          [0]
  static/   manifest.json  icons/icon{16,32,48,128}.png (from data/*.svg)    [0]
  src/
    background/  sw.ts (rules sync, file:// fallback, menus, action), rules.ts   [0]
    shared/      kinds.ts (ext/MIME/magic routing), prefs.ts, viewer-url.ts, messages.ts [0]
    shell/       viewer.html, main.ts, shell.ts, source.ts, save.ts, shortcuts.ts, handles.ts [0]
    options/     options.html/.ts/.css                                        [0]
    ui/          kit.ts, icons.ts, theme.css                                  [0]
    inspector/   index.ts (Get Info: File section + renderer)                 [0]
    viewers/     contract.ts, registry.ts, stub.ts                            [0]
      pdf/       index.ts (reference implementation), style.css, copy.json    [A]
      image/                                                                  [C]
      table/  text/                                                           [D]
      model/  media/                                                          [E]
    markup/      api.ts (contract), index.ts (model stub), dev/               [B]
    signature/   api.ts, index.ts                                             [B]
    convert/     (images → PDF, batch convert)                                [C]
    ocr/         api.ts, index.ts, (worker.ts, copy.json, models)             [F]
    content/     detect.ts + content.css (embed/object/blob:/data: PDFs), transfer.ts [0]
    types/       fsa.d.ts (File System Access typings)                        [0]
  test/
    unit/*.test.ts (node:test, bundled by esbuild)   e2e/*.test.mjs (Puppeteer)
    fixtures/make-fixtures.mjs + generated sample.{pdf,png,csv,stl,txt}
```

### 3.3 Viewer contract (`src/viewers/contract.ts`, a port of `viewer_base.py`)

```ts
interface ViewerModule { kind: Kind; create(): Viewer }        // default export of viewers/<kind>/index.ts
interface Viewer {
  mount(container: HTMLElement, source: DocumentSource, ctx: ViewerContext): Promise<void>;
  getTitle?(): string; getSubtitle?(): string;                 // "Page 2 of 7", "1920 × 1080"
  isDirty?(): boolean;
  save?(req: SaveRequest): Promise<SaveResult | null>;          // produce bytes; the shell writes them
  onSaved?(info: SavedInfo): void | Promise<void>;              // called only after a real write: clear dirty HERE
  exportFormats?(): ExportFormat[];
  print?(): Promise<void>;
  getInfo?(): Promise<InfoSection[]>;                           // the shell prepends the File section
  acceptsDrop?(s: DocumentSource[]): boolean; addDropped?(s: DocumentSource[]): Promise<void>;
  destroy(): void;
}
interface DocumentSource { name; url?; handle?; mime; size; lastModified; streamable; blob(); head(n?) }
interface ViewerContext {
  header: { start: HTMLElement; tools: HTMLElement };  sidebar: { el; setVisible(v); visible };
  shortcuts: { add({keys:"Mod+=", description, run}) };  services: { markup(opts), signatures(), ocr() };
  signal: AbortSignal; hash: string; kind: Kind;
  toast(msg, {actionLabel, onAction, timeout}); refreshTitle(); setDirty(d); openSources(srcs);
  requestSave(req: SaveRequest): Promise<void>;   // your own Save/Export buttons → same path as Mod+S
}
```

`ctx.requestSave(req)` behaves exactly like the shell's Save (Mod+S), Save As (Mod+Shift+S) and Export… commands:
- The shell calls your `save(req)` and routes the result: overwrite the opened handle (`mode: "save"` with `replacesOriginal`), else the save dialog, else a download.
- `{mode: "export"}` without `format` first shows the export picker built from `exportFormats()`, then calls `save()` with the chosen format; other fields such as `selectionOnly` are kept. With `format`, the picker is skipped.
- It resolves when the save is done, cancelled or failed. Failures are shown as a toast and never thrown.
- Covered by `test/e2e/foundation-save.test.mjs`.

**`Viewer.onSaved(info)`: dirty state follows real writes.** `save()` only *produces* bytes. The user may still cancel the save dialog, the write may fail, or an in-place write may lose permission and fall back to the dialog. So:
- **Don't clear dirty state in `save()`.** Remember what you serialized (the document snapshot and the `SaveResult` object), and make it the saved state in `onSaved`.
- The shell calls `onSaved` only after a successful write, awaits it, and then refreshes the title.
- `SavedInfo` fields:
  - `mode`, `format?`, `replacesOriginal`;
  - `result`: the exact object your `save()` returned, so compare by identity;
  - `destination`: `"overwritten"`, `"saved-as"` or `"downloaded"`;
  - `name`, and `handle?` (absent for downloads).
- Exports (`replacesOriginal: false`) don't change what "saved" means.
- Edits made while the dialog was open must keep the document dirty. The table viewer marks the undo position captured at `serialize` time; the text viewer compares against the saved `Text` snapshot.
- The table, text, image and PDF viewers implement it. `test/e2e/foundation-integration.test.mjs` checks, for table, text and image, that cancelling leaves the document dirty and saving makes it clean.

Rules for viewers:

- Only touch `container`, `ctx.header.*` and `ctx.sidebar.el`. The shell owns Open, Save, Get Info (Ctrl+I), the main menu, the sidebar toggle (F9) and full screen. The sidebar button appears when the viewer has put something into `sidebar.el`.
- Clean up through `ctx.signal`.
- Do parsing, rendering and OCR in Workers.
- Set `data-viewer="<kind>"` on your root element, and set `data-state`/progress attributes that the e2e tests can wait on. See the PDF viewer's `data-pages` and `data-rendered`.
- **Reference implementation**: `src/viewers/pdf/index.ts`. It shows a lazy chunk, `loadStyle`, a vendor worker via `copy.json`, header tools, shortcuts, `getInfo`, streaming from `source.url`, and a fallback to `source.blob()`. Copy its shape.

### 3.4 Shell and UI kit

- `shell.ts` provides:
  - A header bar: sidebar toggle, the viewer's `start` slot, title and subtitle (with • when dirty), the viewer's `tools` slot, then Info and the main menu (Open, Save, Save As, Export, Print, Open in Chrome, Shortcuts, Preferences).
  - Sidebar and inspector panels.
  - The empty window (Open…, drop zone, Recent).
  - Status pages: file access, HTTP error, unsupported kind.
  - Toasts, a drop overlay, `beforeunload` when dirty, and routing through `REGISTRY`.
- **Shortcuts**: Mod+O, Mod+S, Mod+Shift+S, Mod+Shift+E (Export), Mod+P, Mod+I, F9 and `?` / Mod+? (help). Viewer shortcuts shadow the shell's and go away on destroy.
  - **Keyboard Shortcuts sheet** (the desktop's `Adw.ShortcutsDialog`): built live from the registry. The "General" group comes first (shell shortcuts), then the open viewer's shortcuts under its heading (PDF, Images, 3D Models, Tables, Text and Source Code, Video and Audio). Chords with the same description are merged. So **write good `description`s**; they are the help text.
- **Dialogs** (`src/ui/dialog.ts`): `showDialog({title, body, actions})` is a modal on a native `<dialog>`, with Escape and backdrop-click to close. `pickExportFormat(formats)` is the Export picker. The shell's Export… uses it with `viewer.exportFormats()` and then calls `viewer.save({mode: "export", format})`. Viewers may call either function for their own UI.
- **Recent** (empty window): opened time ("5 minutes ago"), × to forget one entry, Clear for all. A file that was moved or deleted drops out with a toast.
- **Chrome reserves Ctrl+W, Ctrl+T, Ctrl+N, Ctrl+Tab and Ctrl+Q**; pages never receive them. Use the Fullscreen API (F11 is the browser's own key).
- **Theme** (`src/ui/theme.css`): libadwaita tokens (`--accent #3584e4`, `--window-bg`, `--header-bg`, `--sidebar-bg`, `--canvas-bg`, `--card-bg`, `--border`, `--dim-fg`, `--hover`, `--selected`, radius 6 / 12 px, Adwaita Sans → Cantarell → system-ui).
  - Light/dark follows `prefers-color-scheme`, and the options page can force either via `data-theme`.
  - Widgets: `button()` for flat header buttons (with `toggle` and `suggested` variants), `linked()`, `menuButton()` for popovers (they scroll when taller than the window), and `icon()` / `setIcon()` for 16 px symbolic icons in `ui/icons.ts`. That file is the **single icon set**: shell, markup and signature tools, 3D, media, image (`tools`, `rotateLeft`, `add`, `remove`), and PDF (`chevronUp`, `chevronDown`). **Add icons there, not inside viewers.**
  - Never hard-code colours.

---

### 3.5 Shared module APIs (beyond the contract files)

**Markup (stream B, `src/markup/`)**
- `importPdfAnnotations(doc, {pages?, raw?, surfaceOf?})` in `pdf-import.ts` returns `{annotations, sourceIds}`. It turns the existing markup annotations of a pdf.js document (ours or another app's) into editable `Annotation[]`. pdf.js doesn't expose everything, so it also reads the file with @cantoo/pdf-lib: pass `raw` as a loaded document, let it load from `doc.getData()` (the default), or pass `raw: null` for a lossy pdf.js-only import.
- The importing viewer must:
  1. hide the imported annotations from pdf.js rendering;
  2. on save, call `removePdfAnnotations(doc, sourceIds)` (`pdf-export.ts`) before `writePdfAnnotations(...)`, so annotations aren't duplicated.
- **Keyboard rule**: the markup controller listens for `keydown` on the window, but it handles only keys the viewer **didn't** handle. It ignores events that are already `defaultPrevented`, events in text fields, and events while a `<dialog>` is open. So a viewer that wants a key (Delete for pages, the arrows for paging) must `preventDefault()` it (`ctx.shortcuts` does this), and keys a viewer leaves alone reach the markup selection (Delete, the arrows, Escape).
- Fonts: see §2 (`pdf-fonts.ts`/`ttf-build.ts`; never `subset: true`).

**OCR (stream F, `src/ocr/`)**
- `createTextLayer(result, {handleCopy?, onChange?})` (`text-layer.ts`, re-exported from `ocr/index.ts`) returns a `TextLayer`: an SVG whose viewBox is the OCR image's pixel space. Put `layer.el` inside the relatively positioned element that shows the image or page, and it scales with the zoom.
  - Selection: drag to select (character-accurate), double-click for a word, triple-click for a line, Ctrl+C to copy.
  - Pointer events that start outside text pass through, so pan and markup keep working.
  - API: `hit(x, y)`, `selectedText()`, `text()`, `selectAll()`, `setShowBoxes()`, `destroy()`.
- `linesToText(lines)` gives plain text in reading order.
- `createOcr()` handles share one worker, started on the first call. Dispose the handle in `destroy()`.

**Services**: `ctx.services.markup()` → `createMarkup` (`markup/index.ts`) and `ctx.services.ocr()` → `createOcr` (`ocr/index.ts`). Both are dynamic imports, so the OCR models and onnxruntime load only on first use.

## 4. Saving (`src/shell/save.ts`)

Viewers only produce a `SaveResult {blob, suggestedName, types, replacesOriginal}`. The shell decides where it goes:

1. **The document was opened from a handle** (picker, drop or Recent) and the result `replacesOriginal`. Request `readwrite` permission, then `createWritable()`, then write. This is atomic: Chrome writes a swap file and renames it on `close()`. This is true **in-place save**, like the desktop app.
2. **Web URL, `file://` URL, Save As (Mod+Shift+S) or Export.** Use `showSaveFilePicker({suggestedName, types, startIn: handle})`. The new handle is adopted, so the next Ctrl+S overwrites it. A `file://` original can't be overwritten: the extension has no handle for it, and `startIn` can't take a path.
3. **No File System Access** (should not happen in Chrome). Fall back to `<a download>` with a `blob:` URL. We deliberately don't request the `downloads` permission.

Multi-file outputs (Export pages to PNG, batch convert) use `showDirectoryPicker()` and write each file, or fall back to a `fflate` zip.

"Save selected pages" (Ctrl+S with pages selected) is the PDF viewer returning `replacesOriginal: false` from `save({mode:"save", selectionOnly:true})`.

---

## 5. Testing (verified on this machine: Linux, Node 22.22, Chrome for Testing 155)

- **Branded Google Chrome 137+ ignores `--load-extension`, so `/usr/bin/google-chrome` is never used.** Puppeteer 25 downloads **Chrome for Testing** into `chrome/.cache/puppeteer` during `npm install`; `.puppeteerrc.cjs` skips the headless shell and Firefox. Tests launch it with `enableExtensions: [dist]`, `pipe: true` and `headless: true` (the new headless mode supports extensions), and find the extension ID from its service-worker target.
- **Fact verified with this setup**: extensions loaded by command line get "Allow access to file URLs" **on** (`isAllowedFileSchemeAccess() === true`). So the no-access fallback (§1.2) is **not covered by e2e**. Test it by hand: install from a zip, or turn the toggle off on `chrome://extensions`.
- `npm run test:unit` uses `node:test`. `scripts/unit.mjs` bundles the `.ts` tests with esbuild first, because this Node build has no TypeScript type stripping. Current suites: `kinds`, `viewer-url`, `rules`, 16 tests.
- Stream 0 e2e files:
  - `foundation-content.test.mjs` (8 tests): embed, embed without a type, object, a `blob:` embed, a `blob:` iframe, a `data:` iframe, a top-level `blob:` PDF; and non-PDF plugins plus the PDF pref off are left alone.
  - `foundation-network.test.mjs` (5 tests): Referer kept, plus a control case; cookies, including Strict; cookies with third-party cookies blocked; the bypass rule removed after commit, and reload goes back to View.
  - `foundation-ui.test.mjs` (3 tests): the shortcuts sheet; Recent open, remove and stale handling (seeded with OPFS handles); the theme pref on the options page and the viewer.
- `harness.mjs` helpers:
  - `startServer(extra)` takes extra routes as `[headers, body]` or a `(req, res, body)` handler, and logs every request in `server.requests` (for header assertions).
  - `launch({args, userDataDir})`, `waitForFrame(page, pred)`, `samplePdf()`.
  - Build and run with `VIEW_DIST=dist-<you>`, so parallel builds don't clobber each other.
- `npm run test:e2e` (`test/e2e/interception.test.mjs` with `harness.mjs`, 9 tests). A local HTTP server serves the fixtures. The tests assert:
  - `/doc.pdf` with `application/pdf` is redirected to `viewer.html?kind=pdf&url=…`, renders 3 pages, and shows the subtitle "Page 1 of 3".
  - A misleading URL `/download?id=42` with the correct Content-Type is taken over.
  - `octet-stream` with a `.pdf` URL, and `octet-stream` with a disposition filename, are taken over.
  - A PDF in an iframe is taken over inside the frame.
  - `file://…/sample.pdf` renders.
  - STL and CSV route to their (stub) viewers with the correct byte count.
  - HTML pages and POSTed PDFs are left alone.
  - Turning PDFs off in prefs stops interception.
  - With **all** kinds on, Chrome accepts every rule; this guards the regex limit.
  - The empty toolbar page offers Open.
- Fixtures come from `node test/fixtures/make-fixtures.mjs` (pdf-lib plus a hand-written PNG encoder) and are deterministic. **Never use private files.**
- Each stream adds `test/e2e/<stream>.test.mjs` and `test/unit/<module>.test.ts`, plus fixtures under `test/fixtures/<stream>/`. Reuse `harness.mjs`: `launch`, `startServer`, `open` and `fixtureUrl`.

---

## 6. Parity matrix

Phases: 1 = core viewing, 2 = editing, 3 = advanced. In the Owner column, 0 = foundation and A–F are the streams in §7.

| Desktop feature | Browser approach | Phase | Owner | Known limitations |
|---|---|---|---|---|
| Default viewer / open from file manager | DNR redirects (§1), action button, Open…, drop, Recent, context menu | done | 0 | Store installs need the user to turn on "Allow access to file URLs" for `file://`. The OS file manager can't "Open with View": `file_handlers` is ChromeOS-only for extensions. |
| PDF render, fast on huge files | pdf.js; Range streaming for http; `PDFDataRangeTransport` over `Blob.slice` or ranged XHR for local files | 1 (render done) | A | Memory use on a 600 MB scanned PDF must be measured; cap the canvas cache. |
| Thumbnails | Own virtualized list (IntersectionObserver) rendering small canvases; pdf.js's thumbnail viewer isn't in `pdf_viewer.mjs` | 1 | A | |
| Search, text selection | `PDFFindController` and `TextLayer` (already wired) | 1 | A | |
| Forms | `AnnotationLayer` with `ENABLE_FORMS` and XFA; save via `saveDocument()`, then pdf-lib | 1–2 | A | No JavaScript form actions (needs the pdf.js scripting sandbox; Phase 3 if wanted). |
| Print | Render pages at 150–300 dpi into a print-only container plus `@page`, then `window.print()` (like pdf.js `PDFPrintService`) | 1 | A | Chrome's print dialog, so no direct printer selection. |
| Get Info | Shell File section plus the viewer's `getInfo()` | done (basic) | 0 / each | No created or last-opened times (not exposed to the web). |
| Page delete, reorder, rotate, insert, merge, drag pages out | Page model of `PageRef {src, index, rotation}` as in `pdf_ops.py`; pdf-lib `copyPages` on save; drag-out via the `DownloadURL` dataTransfer | 2 | A | `PDFViewer` is single-document: merged views need stream A's own page canvas (recommended). |
| Save selected pages | `save({selectionOnly:true})`, then Save As | 2 | A | |
| Export pages to images | pdf.js to `OffscreenCanvas` to PNG/JPEG, then `showDirectoryPicker` or a zip | 2 | A | |
| Password-encrypt | `@cantoo/pdf-lib` `encrypt()` (AES-256) | 2 | A | |
| Reduce file size | Re-encode image XObjects (decode, downscale, JPEG via OffscreenCanvas) plus object streams | 3 | A | Weaker than ghostscript (which is AGPL and excluded). Fonts aren't touched. |
| Markup: sketch, draw, shapes, arrows, text boxes, notes | `MarkupController` (§7, B) with an SVG overlay, written as real PDF annotations or flattened onto images | 2 | B (+A, C) | CJK text in text boxes needs an embedded font: use `queryLocalFonts()` with permission, or a bundled OFL subset. |
| Highlight, underline, strikethrough | Snap to `TextRun`s from the PDF text layer (A) or OCR (F) | 2 | B | |
| Signatures (vector) | Capture dialog: Pointer Events (mouse, pen with pressure and coalesced events, touchscreen); store in IndexedDB | 2 | B | |
| **1:1 touchpad signing** | **Not possible: no web API exposes absolute touchpad contacts.** Substitute: a Pointer-Lock "trackpad mode" that integrates `movementX/Y` (with `unadjustedMovement: true` where supported), with "swipe to sign" as the default: every movement draws, no click needed, and a 150 ms pause in movement events (the finger lifted) ends the stroke; Shift moves without ink, Space switches to click-drag | 2 | B | It is relative, not absolute: the shape is right but the user can't "place" a stroke by touch position. `unadjustedMovement` may be unsupported on Linux, so it falls back to accelerated movement. Optional Phase 3 exact path: a native-messaging host reusing the desktop's `touchpad.py`/`touchpad_win.py` for users who also install View desktop. |
| Image zoom, pan, pinch | Canvas or `<img>` with CSS transforms; `wheel` with `ctrlKey` is a touchpad pinch in Chrome; Pointer Events for touch | 1 | C | |
| Rotate, flip, crop, adjust colour, resize, instant alpha | Edit-op list replayed in a Worker on `OffscreenCanvas` (like `image_ops.py`); instant alpha as a tolerance flood fill | 2 | C | |
| EXIF/GPS info | `exifr` with an OpenStreetMap link | 1 | C | |
| Keep animation when edited (GIF, WebP, APNG) | Decode with `ImageDecoder`; apply ops per frame; encode with gifenc, UPNG.js or our own WebP muxer | 2–3 | C | Lossy WebP re-encode quality is set by Chrome's encoder. |
| Formats: PNG, JPEG, WebP, GIF, BMP, ICO, AVIF, SVG | Native decoding | 1 | C | **HEIC: no** (LGPL decoders only). JXL: `@jsquash/jxl` in Phase 3. TIFF: done (`utif2`). TGA and QOI: small own decoders in Phase 3. |
| Batch convert; combine images into a PDF | `src/convert/` with OffscreenCanvas encoders and pdf-lib `embedJpg`/`embedPng` | 2 | C | |
| Slideshow (image viewer) | Fullscreen API plus a timer | 3 | C | |
| Live Text OCR | `ocr/` service with onnxruntime-web and PP-OCR (det, cls, rec, CTC decode) in a Worker; selectable overlay | 3 | F (+A, C) | Single-threaded wasm, about 1–2 s per page. |
| Searchable PDF export | Invisible text (render mode 3) with a GlyphLessFont (Identity-H, as Tesseract does), so no CJK font is needed | 3 | F + A | |
| 3D STL/OBJ/PLY, orbit, dimensions, volume, area, 10M triangles | three.js; parse in a Worker into transferable typed arrays; flat shading from screen-space derivatives (no normal buffer, which halves memory); stats computed in the Worker | 1 | E | 10M non-indexed triangles is about 360 MB of positions. Test on the target hardware and fall back to decimation. |
| CSV/TSV viewer and editor (1M rows) | PapaParse in a Worker; row offsets plus a virtualized grid; edits as an overlay map; save by streaming `unparse` into a Blob | 1–2 | D | |
| Text and code viewer with syntax highlighting (editable) | CodeMirror 6, languages lazy-loaded | 1 | D | Files over about 50 MB open read-only and plain. |
| Video and audio | Native `<video>`/`<audio>`; a `blob:` URL for local files (streams without loading into memory) | 1 | E | Codecs as in §2. Seeking in an http source needs Range support from the server. |

**Desktop-only, not ported**: GNOME Files Quick Look (Space, the Sushi replacement); Set as Wallpaper; the VA-API / hardware-decode toggle and Direct Composition (`VIEW_NO_DCOMP`); evdev and Windows raw touchpad capture (see the native-messaging option above); the `view-app --new-signature` CLI; `.desktop`/MIME registration and the Nautilus "Combine Images into PDF" action; the snap `raw-input` plug and the `input` group setup; the OCR installer (`ocr_install.py`); multiple windows (each document is a tab instead); `make selftest` (replaced by `npm test`).

---

## 7. Work breakdown (parallel streams)

Each stream owns its paths exclusively.

- **Shared contracts** (`viewers/contract.ts`, `markup/api.ts`, `signature/api.ts`, `ocr/api.ts`) and everything marked [0] in §3.2 belong to the **foundation owner (stream 0)**. Change them through a small PR that the affected streams agree to.
- `package.json` and `package-lock.json` belong to stream 0. All the planned dependencies are already installed.
- Each stream adds its tests under `test/unit/<stream>-*.test.ts`, `test/e2e/<stream>.test.mjs` and `test/fixtures/<stream>/`.

### Stream 0: foundation (shell, interception, contracts); kept by the repo owner or orchestrator

**Owns**: `src/background`, `src/shared`, `src/shell`, `src/options`, `src/ui`, `src/inspector`, `src/content`, `src/types`, `src/viewers/{contract,registry,stub}.ts`, `scripts/`, `static/`, `store/`, `test/e2e/{interception,foundation-*}.test.mjs`, `test/e2e/harness.mjs`.

**Done**:
- The embed/object content script and the blob:/data: fallback.
- Referer preservation, with the cookie behaviour verified.
- Bypass cleanup on commit.
- The Keyboard Shortcuts sheet, the Export picker, Recent polish, and the theme on the options page.
- The store listing draft in `store/` (description, permission justifications, privacy policy).

**Remaining**:
- Store screenshots and promo tile.
- A manual pass of the no-file-access flow from a packed install.

### Stream A: PDF viewer and page editing

**Owns**: `src/viewers/pdf/**`.

**Consumes**:
- `ctx.services.markup({docKind:"pdf"})`. Create a `MarkupHost` per rendered page: surface `"p<origIndex>"`, units in PDF points with the origin top-left, and `textRuns()` built from the pdf.js text content.
- `WritePdfAnnotations` from `src/markup/pdf-export.ts` (B) in `save()`.
- `ocr()` (F) for scanned pages and searchable export.

**Provides**:
- Page `TextRun`s for the markup layer.
- `acceptsDrop` for inserting PDFs and images.
- The page model and `remapSurfaces` calls on reorder or delete.

**Done when**:
- Thumbnails sidebar, search bar (Ctrl+F), forms with save, print, and the page ops (delete, reorder, rotate, insert, merge, save selected pages) all work.
- Export to images, encrypt and reduce size work.
- A 1,000-page fixture shows page 1 in under 1 s from a local http server.
- e2e tests cover each operation: pdf-lib checks the saved output for page count, rotation, encryption and form values.

### Stream B: markup and signatures (shared by PDF and image)

**Owns**: `src/markup/**` (excluding `api.ts`, which is the contract; changes need agreement from A and C), `src/signature/**`.

**Implements**: the full `MarkupController`, covering:
- An SVG overlay per surface; pointer tools for select, sketch (shape recognition as in `markup.py`), draw, line, arrow, rect, rrect, oval, bubble, star, text box, note, highlight, underline and strikethrough; move/resize handles; undo; and the shared toolbar (colour, width, font, signature menu).
- `render()` (canvas flattening).
- `pdf-export.ts` (`WritePdfAnnotations` with appearance streams).
- `pdf-import.ts`: pdf.js annotations to `Annotation[]`, so existing markup can be edited.
- The signature capture dialog (pointer mode and Pointer-Lock trackpad mode) and an IndexedDB `SignatureStore`.

**Done when**: e2e tests on `dist/dev/markup-playground.html` draw each tool with synthetic pointer events, check undo and redo, and check `toJSON()`; a unit test round-trips `pdf-export` through pdf-lib and pdf.js (each annotation type is present, with `/AP`); and a signature captured with the mouse is placed and exported as vector paths.

### Stream C: image viewer, edits and batch tools

**Owns**: `src/viewers/image/**`, `src/convert/**`.

**Consumes**:
- `services.markup({docKind:"image"})`, with one surface `"img"` in source pixels, and `render()` to flatten on save.
- `ocr()` for Live Text.

**Provides**: `acceptsDrop` for adding images to the image list.

**Done when**:
- Zoom, pan and pinch work; so do rotate, flip, crop, adjust colour, resize and instant alpha.
- EXIF and GPS appear in Get Info.
- Saving works in place (for handles) and keeps EXIF.
- Editing an animated GIF, WebP or APNG keeps the frame count and delays (e2e decodes the output with `ImageDecoder`).
- Multi-image sidebar with batch convert, and combine into PDF.

### Stream D: table and text viewers

**Owns**: `src/viewers/table/**`, `src/viewers/text/**`.

**Done when**:
- CSV/TSV with delimiter and encoding detection opens 1M rows (a generated fixture) with first paint in under 2 s and smooth scrolling; editing cells, adding and deleting rows and columns, sorting, Ctrl+F, and saving round-trip byte-for-byte when nothing changed.
- The text viewer uses CodeMirror with the language picked by extension or name (`kinds.ts`), read-only by default with Edit mode, save, and a large-file plain fallback.

### Stream E: 3D models and media

**Owns**: `src/viewers/model/**`, `src/viewers/media/**`.

**Done when**:
- STL (ASCII and binary), OBJ and PLY parse in a Worker; orbit, pan and zoom work with touchpad gestures; reset and fit work; Get Info shows dimensions, volume, area and triangle count (cross-checked against `preview/mesh_io.py` values on the fixture tetrahedron).
- A 2M-triangle generated fixture renders.
- Video and audio play with a View-styled control bar, keyboard shortcuts (Space, arrows, F), and Get Info showing duration, resolution and codec where available.

### Stream F: OCR (Live Text)

**Owns**: `src/ocr/**` (worker, pre- and post-processing, `copy.json` for the ORT wasm and models).

**Implements**: `OcrService.recognize()`. The pipeline is DB detection, then box unclip and contour extraction (implemented in JS: no OpenCV, which is too large), then direction classification, then CRNN/SVTR recognition with CTC decode, with character quads for selection.

**Done when**: on generated English and Chinese text images, the recognized text matches the expected strings; the first call takes under 3 s and the next ones under 1.5 s per 1080p image on this machine; and the service is consumed by A and C through `ctx.services.ocr()`.

### Cross-stream dependencies

| | Depends on | Notes |
|---|---|---|
| A | B (markup controller, pdf-export), F (ocr) | A builds against the interfaces and the stubs, which already work as models. |
| C | B, F | Same as A. |
| B | — | Tests standalone on the dev playground. |
| D, E | — | Fully independent. |
| F | — | Fully independent. |

**Order of value**: A first (the PDF default viewer is the headline feature), with B close behind (signing).

---

## 8. Decisions for the repo owner, and known limitations

**Decisions**:
Settled so far (orchestrator, 2026-10):
- **Default kinds**: PDF, 3D models and tables are on; images, text and media are off.
- **OCR ships in the package.** It adds **about 46 MB** to the extension: the ORT wasm is 14 MB, and the PP-OCR models plus the dictionary are about 31 MB. No download on first use.
- **No native-messaging helper.** Touchpad signing in the browser stays relative (the Pointer-Lock trackpad mode).
- **No manifest `key` yet.**

Still open:
- **Store listing.**
   - The permissions `<all_urls>`, `webRequest`, `webNavigation` and the all-sites content script trigger the warning "Read and change all your data on all websites", which needs a justification at review (draft in `store/permissions.md`). A default viewer can't work without them.
   - PolyForm-Noncommercial is compatible with a free store listing; confirm that you are fine with publishing source-available code there.
- **Extension ID stability.** Add a `key` before store publication if a stable ID is ever needed.

**Limitations found**:
- Branded Chrome 137+ ignores `--load-extension`, so tests use Chrome for Testing.
- **DNR regexes are capped at a compiled size of 2 KB.** Long extension lists must be chunked; the e2e test caught this.
- POST-loaded PDFs stay in Chrome's viewer, and so do top-level `data:` PDFs, which a user can only reach by typing them, because Chrome won't load extension pages into opaque origins.
- The viewer re-requests the document, so one-time URLs (signed URLs that expire on first use) can fail. The "Open in Chrome" button is the escape hatch.
- There is no absolute touchpad input on the web.
- No HEIC; no AVI or WMV.
- Reserved Chrome shortcuts (Ctrl+W, Ctrl+T, Ctrl+N).
- An original opened via `file://` can't be overwritten in place; it needs Save As once, and the shell then remembers the handle for that tab.
