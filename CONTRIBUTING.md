# Contributing to View

Thanks for your interest! View is a small community project and every kind of help is welcome: bug reports, "it works on my distro" reports, docs, translations of ideas into issues, and code.

The most valuable contribution right now is **testing on other systems**. Please file a [Platform report](https://github.com/Zdong104/View/issues/new?template=platform_report.yml).

Please follow our [Code of Conduct](CODE_OF_CONDUCT.md). For questions and ideas, use [Discussions](https://github.com/Zdong104/View/discussions); for bugs and requests, use the issue forms.

## Run from source

Requirements: Python 3.12+, GTK 4, libadwaita 1.x, PyGObject, Poppler (GIR), GStreamer with `gtk4paintablesink`, plus the Python packages listed in the Makefile / packaging files.

```bash
git clone https://github.com/Zdong104/View.git
cd View
make run                       # runs the app from the working tree
```

If a `make` target is missing or fails on your system, that is itself a useful bug report.

## Project layout

All code lives in `preview/`:

| Module | Purpose |
| --- | --- |
| `app.py` | Application shell: windows, file routing, drag and drop, menus |
| `viewer_base.py` | Contract between the shell and the document viewers |
| `pdf_view.py` | PDF viewer: thumbnails, page editing, forms, search, export |
| `pdf_canvas.py` | Scrolling page canvas, zoom and layout modes |
| `pdf_render.py` | Background page rendering (worker threads) |
| `pdf_ops.py` | PDF document model and save/export pipeline |
| `markup.py` | Markup layer: shapes, ink, text, notes, signatures |
| `signature.py`, `touchpad.py` | Signature storage/rendering and absolute touchpad capture (evdev) |
| `image_view.py`, `image_ops.py` | Image viewer and its edit pipeline |
| `convert.py` | Images to PDF (library, CLI and dialog) |
| `ocr.py` | Live Text via RapidOCR |
| `ocr_helper.py` | Out-of-process OCR engine (`view-ocr.exe` on Windows) |
| `stl_view.py`, `mesh_io.py` | 3D viewer (GLArea) and mesh loading/statistics |
| `table_view.py`, `table_data.py` | CSV / TSV viewer and editor, and its document model |
| `text_view.py`, `syntax.py` | Text and code viewer, Pygments helpers |
| `markdown_view.py` | Rendered Markdown (Source / Split / Preview) for the text viewer |
| `dxf_view.py` | DXF (2D CAD) viewer: ezdxf scene cache, banded rasterizer, layers, export |
| `video_view.py`, `media_player.py` | Video / audio viewer and player widget |
| `inspector.py` | Get Info window |
| `i18n.py` | UI translation (gettext); catalogs live in `po/` |
| `quicklook.py`, `quicklook_renderers.py` | Quick Look service for GNOME Files |

## Translations

UI strings are wrapped with `_()` (`from .i18n import _, N_, ngettext`); the language is picked in Preferences (or follows the system locale) and applies after a restart.

- Write `_("Could not save “{name}”: {err}").format(name=n, err=e)` with named placeholders, never `f"…"` or string concatenation inside `_()`.
- Use `ngettext("{n} page", "{n} pages", n).format(n=n)` for plurals.
- Module-level constants (menu tables, labels) use `N_("…")` and are translated with `_()` where they are shown.
- Don't use `_` as a throwaway variable (`lambda *_`, `for _ in …`) in a function that also calls `_()`; use `_a`, `_x`, …
- `make i18n` refreshes `po/view.pot`, merges it into every `po/*.po` and compiles `preview/locale/<lang>/LC_MESSAGES/view.mo`. Commit the `.mo` files: the packages ship them as they are. It needs GNU gettext (`xgettext`, `msgmerge`, `msgfmt`).
- To add a language: `msginit -i po/view.pot -l <lang> -o po/<lang>.po --no-translator`, translate it, add it to `LANGUAGES` in `preview/i18n.py`, and run `make i18n`.

The translation framework and the Simplified Chinese catalog were contributed by [@azkuang](https://github.com/azkuang); improvements to `po/zh_CN.po` or new languages are welcome.

## Chrome extension

The Chrome version lives in `chrome/` (TypeScript, esbuild, Manifest V3). It needs Node 22+ and takes its version from `preview/__init__.py`.

```bash
cd chrome
npm install        # also downloads Chrome for Testing for the tests
npm run build      # unpacked extension in chrome/dist/ (fetches the OCR models on first build)
npm test           # typecheck, licence check, unit and end-to-end tests
npm run package    # view-chrome-<version>.zip for the Chrome Web Store
```

Load `chrome/dist/` with **Load unpacked** on `chrome://extensions`. [chrome/ARCHITECTURE.md](chrome/ARCHITECTURE.md) explains the layout, the viewer contract and how each desktop feature maps to the browser. Only permissively licensed dependencies (MIT, Apache-2.0, BSD, ISC) are allowed; `npm test` checks this.

## Coding style

- Match the surrounding code: naming, formatting, comment density.
- Use GTK4 / libadwaita idioms (`Adw.*` widgets, `Gio.Action`s, no deprecated GTK3 APIs).
- **Never block the main loop.** Do file parsing, rendering, OCR and anything else slow in a worker thread and hand results back with `GLib.idle_add`.
- Keep GUI-free logic (`*_ops`, `*_io`, `*_data`) free of GTK imports so it can be tested.
- Prefer small, focused changes over large refactors.

## Testing

```bash
make selftest
```

For anything visual, render the real window to a PNG and look at it. The trick: create the window, wait about 1.5 s, then

```python
paintable = Gtk.WidgetPaintable.new(window)
snapshot = Gtk.Snapshot()
paintable.snapshot(snapshot, paintable.get_intrinsic_width(), paintable.get_intrinsic_height())
texture = window.get_native().get_renderer().render_texture(snapshot.to_node(), None)
texture.save_to_png("shot.png")
```

Attach screenshots to your pull request when the UI changes. Use sample files you made yourself, never private documents.

## Pull request checklist

- [ ] Based on current `main`, one topic per PR
- [ ] `make selftest` passes
- [ ] New UI strings go through `_()` and `make i18n` has been run
- [ ] Tried it manually (and with a large file if you touched rendering or loading)
- [ ] The UI stays responsive (no blocking calls on the main thread)
- [ ] Docs / README updated if behaviour changed
- [ ] No private files, credentials or large binaries added

## License of contributions

View is released under the [PolyForm Noncommercial License 1.0.0](LICENSE). By submitting a contribution you agree that it is licensed under the same terms (inbound = outbound), and that you have the right to submit it. The maintainer may also relicense the project, including offering commercial licenses, which is how commercial use is granted permission.
