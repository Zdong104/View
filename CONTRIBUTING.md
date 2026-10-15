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
| `video_view.py`, `media_player.py` | Video / audio viewer and player widget |
| `inspector.py` | Get Info window |
| `quicklook.py`, `quicklook_renderers.py` | Quick Look service for GNOME Files |

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
- [ ] Tried it manually (and with a large file if you touched rendering or loading)
- [ ] The UI stays responsive (no blocking calls on the main thread)
- [ ] Docs / README updated if behaviour changed
- [ ] No private files, credentials or large binaries added

## License of contributions

View is released under the [PolyForm Noncommercial License 1.0.0](LICENSE). By submitting a contribution you agree that it is licensed under the same terms (inbound = outbound), and that you have the right to submit it. The maintainer may also relicense the project, including offering commercial licenses, which is how commercial use is granted permission.
