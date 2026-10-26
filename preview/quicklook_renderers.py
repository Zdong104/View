"""Per-file-type renderers for Quick Look (see quicklook.py).

A renderer builds one widget for one file and must release everything it
started (threads, media, GL viewers, subprocesses) in destroy().  Heavy
modules are imported lazily so the resident service starts fast.
"""
import importlib
import os
import shutil
import subprocess
import sys
import threading

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango

from .i18n import _, ngettext

ATTRS = ("standard::*,time::modified,time::created,time::access,"
         "thumbnail::path,thumbnail::is-valid,unix::mode")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".jpe", ".webp", ".gif", ".bmp", ".tif", ".tiff",
              ".heic", ".heif", ".avif", ".jxl", ".svg", ".svgz", ".ico", ".tga", ".qoi",
              ".xpm", ".pnm", ".ppm", ".pgm", ".pbm"}
MODEL_EXTS = {".stl", ".obj", ".ply"}
OFFICE_EXTS = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".doc", ".xls", ".ppt",
               ".rtf", ".odg", ".docm", ".xlsm", ".pptm", ".pps", ".ppsx"}
FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".pfb"}
MEDIA_EXTS = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".ogv", ".mpg", ".mpeg",
              ".wmv", ".flv", ".3gp", ".ts", ".mp3", ".ogg", ".oga", ".opus", ".flac",
              ".wav", ".m4a", ".aac", ".wma", ".aiff", ".aif"}
TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".py", ".pyi", ".js", ".mjs", ".ts", ".tsx",
             ".jsx", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
             ".csv", ".tsv", ".log", ".sh", ".bash", ".zsh", ".fish", ".c", ".h", ".cc",
             ".cpp", ".hpp", ".cxx", ".cu", ".cuh", ".rs", ".go", ".java", ".kt", ".swift",
             ".rb", ".pl", ".php", ".lua", ".r", ".m", ".sql", ".xml", ".html", ".htm",
             ".css", ".scss", ".less", ".tex", ".bib", ".cmake", ".mk", ".diff", ".patch",
             ".service", ".desktop", ".env", ".gitignore", ".dockerfile", ".vue", ".svelte",
             ".zig", ".nim", ".hs", ".ml", ".scala", ".dart", ".jl", ".ipynb", ".srt", ".vtt"}
TEXT_NAMES = {"makefile", "dockerfile", "readme", "license", "copying", "changelog",
              "cmakelists.txt", "authors", "todo", "news", "install", "gemfile", "rakefile"}
ARCHIVE_CTS = {"application/zip", "application/x-tar", "application/x-compressed-tar",
               "application/x-bzip-compressed-tar", "application/x-xz-compressed-tar",
               "application/x-zstd-compressed-tar", "application/x-7z-compressed",
               "application/vnd.rar", "application/x-rar", "application/java-archive",
               "application/x-bzip2-compressed-tar", "application/x-lzma-compressed-tar"}
TEXT_CAP = 2 * 1024 * 1024
HIGHLIGHT_CAP = 600 * 1024


# ================================================================ helpers ===

def human_size(n):
    return GLib.format_size(int(n))


def fmt_dt(dt):
    if dt is None:
        return "—"
    try:
        return dt.to_local().format("%b %-d, %Y at %-I:%M %p")
    except Exception:  # noqa: BLE001
        return "—"


def _ext(name):
    name = name.lower()
    for double in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lzma"):
        if name.endswith(double):
            return double
    return os.path.splitext(name)[1]


_app_label_cache = {}


def default_app_for(ct):
    if ct in _app_label_cache:
        return _app_label_cache[ct]
    app = None
    try:
        app = Gio.AppInfo.get_default_for_type(ct, False) if ct else None
    except Exception:  # noqa: BLE001
        app = None
    name = app.get_display_name() if app is not None else None
    _app_label_cache[ct] = name
    return name


def _label(text="", css=(), xalign=0.0, wrap=False, selectable=False, ellipsize=False,
           lines=-1):
    lb = Gtk.Label(label=text, xalign=xalign, css_classes=list(css), selectable=selectable)
    if wrap:
        lb.set_wrap(True)
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    if lines > 0:
        lb.set_lines(lines)
    if selectable:
        lb.set_can_focus(False)
    return lb


def _texture_from_path(path):
    try:
        return Gdk.Texture.new_from_filename(path)
    except Exception:  # noqa: BLE001
        return None


def _surface_scale(widget):
    try:
        native = widget.get_native()
        s = native.get_surface() if native is not None else None
        return float(s.get_scale()) if s is not None else float(widget.get_scale_factor())
    except Exception:  # noqa: BLE001
        return 1.0


def _is_dark():
    return Adw.StyleManager.get_default().get_dark()


# ===================================================================== base ===

class Renderer:
    kind = "file"
    wants_vertical_keys = False

    def __init__(self, win, gfile, info, local=None):
        self.win = win
        self.gfile = gfile              # what Nautilus asked for (title, opening)
        self.local = local or gfile     # resolved target (trash:// -> file://)
        self.info = info
        self.alive = True
        self.extra = ""
        self._timeouts = []
        self._cancel = threading.Event()

    @property
    def path(self):
        return self.local.get_path()

    # ---- header ----
    def get_title(self):
        if self.info is not None and self.info.get_display_name():
            return self.info.get_display_name()
        return self.gfile.get_basename() or self.gfile.get_uri()

    def kind_description(self):
        ct = self.info.get_content_type() if self.info else None
        return (Gio.content_type_get_description(ct) if ct else None) or "File"

    def size_text(self):
        if self.info is None or not self.info.has_attribute("standard::size"):
            return ""
        return human_size(self.info.get_size())

    def get_subtitle(self):
        parts = [self.kind_description(), self.size_text(), self.extra]
        return "  ·  ".join(p for p in parts if p)

    def set_extra(self, text):
        self.extra = text or ""
        if self.alive and self.win.renderer is self:
            self.win.refresh_header()

    def preview_can_open(self):
        return False

    def preview_file(self):
        return self.local

    def default_open_uri(self):
        return self.gfile.get_uri()

    def default_app_label(self):
        ct = self.info.get_content_type() if self.info else None
        name = default_app_for(ct)
        return f"Open With {name}" if name else "Open"

    # ---- lifecycle ----
    def build(self):
        raise NotImplementedError

    def attached(self):
        """Called after the widget is placed in the window."""

    def destroy(self):
        self.alive = False
        self._cancel.set()
        ctx = GLib.MainContext.default()
        for src in self._timeouts:
            try:
                if ctx.find_source_by_id(src) is not None:
                    GLib.source_remove(src)
            except Exception:  # noqa: BLE001
                pass
        self._timeouts = []

    def run_bg(self, work, done=None, name="ql-worker"):
        """Run work() in a thread; done(result_or_exc, ok) on the main thread
        if this renderer is still showing."""
        def thread():
            try:
                res, ok = work(), True
            except Exception as e:  # noqa: BLE001
                res, ok = e, False
                if not self._cancel.is_set():
                    print(f"[quicklook] {name} failed for {self.gfile.get_uri()}: "
                          f"{type(e).__name__}: {e}", file=sys.stderr)
            if done is not None:
                GLib.idle_add(finish, res, ok)

        def finish(res, ok):
            if self.alive:
                try:
                    done(res, ok)
                except Exception:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
            return False
        threading.Thread(target=thread, name=name, daemon=True).start()

    def timeout(self, ms, fn):
        def tick():
            if not self.alive:
                return False
            keep = fn()
            if not keep:
                try:
                    self._timeouts.remove(src)
                except ValueError:
                    pass
            return keep
        src = GLib.timeout_add(ms, tick)
        self._timeouts.append(src)
        return src

    # ---- shared info card ----
    def icon_widget(self, size=128, allow_thumb=True):
        thumb = None
        if allow_thumb and self.info is not None:
            tp = self.info.get_attribute_byte_string("thumbnail::path") \
                if self.info.has_attribute("thumbnail::path") else None
            if tp and os.path.exists(tp):
                tex = _texture_from_path(tp)
                if tex is not None:
                    thumb = Gtk.Picture(paintable=tex, can_shrink=True,
                                        content_fit=Gtk.ContentFit.CONTAIN)
                    thumb.set_size_request(int(size * 1.5), int(size * 1.5))
        if thumb is not None:
            return thumb
        gicon = self.info.get_icon() if self.info is not None else None
        img = Gtk.Image(pixel_size=size)
        if gicon is not None:
            img.set_from_gicon(gicon)
        else:
            img.set_from_icon_name("text-x-generic")
        return img

    def info_rows(self):
        rows = [("Kind", self.kind_description())]
        if self.info is not None and self.info.get_file_type() != Gio.FileType.DIRECTORY:
            size = self.info.get_size()
            rows.append(("Size", f"{human_size(size)} ({size:,} bytes)" if size >= 1000
                         else f"{size} bytes"))
        parent = self.gfile.get_parent()
        if parent is not None:
            rows.append(("Where", parent.get_path() or parent.get_parse_name()))
        if self.info is not None:
            if self.info.get_is_symlink():
                rows.append(("Link to", self.info.get_symlink_target() or "—"))
            rows.append(("Created", fmt_dt(self.info.get_creation_date_time())))
            rows.append(("Modified", fmt_dt(self.info.get_modification_date_time())))
        return rows

    def build_card(self, icon=None, rows=None, below=None, subtitle=None):
        """Icon + name + key/value grid, centred; `below` goes under it."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                        margin_top=36, margin_bottom=36, margin_start=36, margin_end=36,
                        valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=28, halign=Gtk.Align.CENTER)
        icon = icon if icon is not None else self.icon_widget()
        icon.set_valign(Gtk.Align.CENTER)
        top.append(icon)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      valign=Gtk.Align.CENTER)
        name = _label(self.get_title(), css=("ql-card-title",), wrap=True, selectable=True)
        name.set_max_width_chars(36)
        name.set_width_chars(18)
        col.append(name)
        if subtitle:
            col.append(_label(subtitle, css=("ql-dim",), wrap=True))
        grid = Gtk.Grid(column_spacing=14, row_spacing=4, margin_top=8)
        self._card_values = {}
        for i, (k, v) in enumerate(rows if rows is not None else self.info_rows()):
            grid.attach(_label(k, css=("ql-key",), xalign=1.0), 0, i, 1, 1)
            val = _label(v, wrap=True, selectable=True)
            val.set_max_width_chars(48)
            val.set_width_chars(22)
            grid.attach(val, 1, i, 1, 1)
            self._card_values[k] = val
        col.append(grid)
        top.append(col)
        outer.append(top)
        if below is not None:
            outer.append(below)
        sw = Gtk.ScrolledWindow(child=outer, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vexpand=True, hexpand=True)
        return sw


class GenericRenderer(Renderer):
    kind = "generic"

    def build(self):
        return self.build_card()


class ErrorRenderer(Renderer):
    kind = "error"

    def __init__(self, win, gfile, error, info=None):
        super().__init__(win, gfile, info)
        self.error = error

    def kind_description(self):
        return "Unavailable"

    def build(self):
        msg = self.error.message if isinstance(self.error, GLib.Error) else str(self.error)
        page = Adw.StatusPage(icon_name="dialog-question-symbolic",
                              title=self.gfile.get_basename() or "File",
                              description=GLib.markup_escape_text(msg or "Cannot preview"))
        return page


# ================================================= embedding View viewers ===

class ViewerHost(Gtk.Box):
    """Stands in for the app window an embedded Viewer expects.  It is a real
    widget, so insert_action_group / add_controller / get_mapped / 'map' and
    Adw.Dialog.present(parent) work, plus the three shell methods."""

    def __init__(self, renderer):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._r = renderer

    def show_toast(self, message, button_label=None, on_button=None):
        self._r.win.show_toast(message, button_label, on_button)

    def refresh_title(self):
        if self._r.alive:
            self._r.on_viewer_title()

    def open_files(self, files):
        self._r.win.open_in_preview(list(files))

    def get_application(self):
        return self._r.win.app


def embed_viewer(renderer, module, clsname):
    """Create View's viewer in compact mode, or None to use our fallback."""
    if os.environ.get("PREVIEW_QL_NO_EMBED"):
        return None
    import importlib.util
    if importlib.util.find_spec(f"preview.{module}") is None:
        return None
    mod = importlib.import_module(f"preview.{module}")
    cls = getattr(mod, clsname)
    if not hasattr(cls, "set_compact") and not os.environ.get("PREVIEW_QL_FORCE_EMBED"):
        return None
    host = ViewerHost(renderer)
    viewer = cls(host)
    if hasattr(viewer, "set_compact"):
        viewer.set_compact(True)
    viewer.build_header(Adw.HeaderBar())   # throwaway: never shown
    host.append(viewer)
    return host, viewer


def release_viewer(viewer):
    if viewer is None:
        return
    for meth in ("shutdown", "release", "cleanup"):
        fn = getattr(viewer, meth, None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
            break
    for attr in ("worker", "tworker", "thumb_worker"):   # PdfViewer render threads
        worker = getattr(viewer, attr, None)
        if worker is not None and hasattr(worker, "stop"):
            try:
                worker.stop()
            except Exception:  # noqa: BLE001
                pass
    parent = viewer.get_parent()
    if parent is not None:
        try:
            parent.remove(viewer)
        except Exception:  # noqa: BLE001
            pass
    import gc
    GLib.timeout_add(400, lambda: gc.collect() and False)


class EmbeddingRenderer(Renderer):
    """Tries View's own viewer first, falls back to build_fallback()."""
    module = clsname = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.viewer = None
        self.host = None

    def build(self):
        try:
            res = embed_viewer(self, self.module, self.clsname)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            res = None
        if res is not None:
            self.host, self.viewer = res
            self.win.app.stats["last_embedded"] = True
            self.start_metadata()
            return self.host
        self.win.app.stats["last_embedded"] = False
        self.start_metadata()
        return self.build_fallback()

    def attached(self):
        if self.viewer is not None:
            try:
                self.viewer.load([self.local])
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                self._swap_to_fallback(e)

    def _swap_to_fallback(self, _err):
        release_viewer(self.viewer)
        self.viewer = None
        try:
            w = self.build_fallback()
        except Exception as e:  # noqa: BLE001
            w = ErrorRenderer(self.win, self.gfile, e).build()
        self.win.bin.set_child(w)

    def on_viewer_title(self):
        pass

    def start_metadata(self):
        pass

    def build_fallback(self):
        raise NotImplementedError

    def preview_can_open(self):
        return self.local.get_path() is not None

    def destroy(self):
        super().destroy()
        release_viewer(self.viewer)
        self.viewer = None


# ======================================================================= PDF ===

_PAPER = [("A4", 210, 297), ("Letter", 215.9, 279.4), ("Legal", 215.9, 355.6),
          ("A3", 297, 420), ("A5", 148, 210), ("Tabloid", 279.4, 431.8), ("B5", 176, 250)]


def paper_name(w_pt, h_pt):
    w_mm, h_mm = w_pt / 72 * 25.4, h_pt / 72 * 25.4
    a, b = sorted((w_mm, h_mm))
    for name, pw, ph in _PAPER:
        if abs(a - pw) < 3 and abs(b - ph) < 3:
            return name + (" landscape" if w_mm > h_mm else "")
    return f"{w_mm:.0f} × {h_mm:.0f} mm"


def _poppler():
    import gi
    gi.require_version("Poppler", "0.18")
    from gi.repository import Poppler
    return Poppler


class PdfRenderer(EmbeddingRenderer):
    kind = "pdf"
    module, clsname = "pdf_view", "PdfViewer"
    wants_vertical_keys = True

    def __init__(self, *a, pdf_file=None, **kw):
        super().__init__(*a, **kw)
        if pdf_file is not None:          # e.g. an office document converted to PDF
            self.local = pdf_file
        self._pages = []          # [(w_pt, h_pt)]
        self._pics = []
        self._fb_worker = None

    def start_metadata(self):
        path = self.local.get_path()

        def work():
            P = _poppler()
            doc = P.Document.new_from_file(GLib.filename_to_uri(path, None), None)
            n = doc.get_n_pages()
            w, h = doc.get_page(0).get_size() if n else (0, 0)
            return n, w, h, (doc.get_title() or "").strip()

        def done(res, ok):
            if not ok:
                if isinstance(res, GLib.Error) and "ncrypt" in (res.message or ""):
                    self.set_extra("Password protected")
                return
            n, w, h, title = res
            parts = [f"{n} page{'s' if n != 1 else ''}"]
            if w and h:
                parts.append(paper_name(w, h))
            if title and title.lower() not in (self.get_title() or "").lower():
                parts.append(f"“{title[:60]}”")
            self.set_extra("  ·  ".join(parts))
        if path:
            self.run_bg(work, done, "ql-pdf-meta")

    # ---- fallback: page images in a scrolled column, rendered lazily ----
    def build_fallback(self):
        path = self.local.get_path()
        if not path:
            return GenericRenderer(self.win, self.gfile, self.info, self.local).build()
        self.col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                           margin_top=14, margin_bottom=14, margin_start=18, margin_end=18)
        self.sw = Gtk.ScrolledWindow(child=self.col, hexpand=True, vexpand=True,
                                     hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     css_classes=["ql-pdfpages"])
        self.spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                   width_request=48, height_request=48)
        self.col.append(self.spinner)
        self._fb_worker = _PdfPageWorker(path, self._on_page_texture, self._on_doc_info)
        self.sw.get_vadjustment().connect("value-changed", lambda *_: self._request_visible())
        self.sw.connect("notify::width", lambda *_: None)
        self._last_w = 0
        self.timeout(250, self._poll_width)
        return self.sw

    def _on_doc_info(self, sizes, err):
        if not self.alive:
            return False
        self.col.remove(self.spinner)
        if err is not None:
            self.col.append(Adw.StatusPage(icon_name="x-office-document-symbolic",
                                           title=_("Cannot render this PDF"),
                                           description=GLib.markup_escape_text(str(err))))
            return False
        self._pages = sizes
        for (w, h) in sizes:
            pic = Gtk.Picture(paintable=Gdk.Paintable.new_empty(max(1, int(w)), max(1, int(h))),
                              can_shrink=True, content_fit=Gtk.ContentFit.FILL,
                              css_classes=["ql-pdfpage"], halign=Gtk.Align.CENTER)
            pic._ql_have = 0
            self.col.append(pic)
            self._pics.append(pic)
        self._last_w = 0
        self._poll_width()
        GLib.idle_add(lambda: self._request_visible() and False)
        return False

    def _page_width(self):
        return int(min(1100, max(200, (self._last_w or 860) - 36)))

    def _poll_width(self):
        """Pictures don't do height-for-width; size every page explicitly."""
        w = self.sw.get_width()
        if w and w != self._last_w or (not self._last_w and self._pics):
            self._last_w = w or 0
            pw = self._page_width()
            for p, (sw_, sh_) in zip(self._pics, self._pages):
                p.set_size_request(pw, max(1, round(pw * sh_ / max(1.0, sw_))))
                p._ql_have = 0
            self._request_visible()
        return True

    def _target_px(self):
        return int(min(2400, self._page_width() * _surface_scale(self.sw)))

    def _request_visible(self):
        if not self._pics or self._fb_worker is None:
            return
        adj = self.sw.get_vadjustment()
        top, bottom = adj.get_value(), adj.get_value() + adj.get_page_size()
        want, visible = [], []
        for i, pic in enumerate(self._pics):
            ok, rect = pic.compute_bounds(self.col)
            if not ok:
                continue
            y0, y1 = rect.get_y(), rect.get_y() + rect.get_height()
            if y1 >= top - adj.get_page_size() and y0 <= bottom + adj.get_page_size():
                visible.append(i)
        if not visible:
            visible = [0]
        px = self._target_px()
        lo, hi = max(0, visible[0] - 1), min(len(self._pics) - 1, visible[-1] + 1)
        for i in range(lo, hi + 1):
            if self._pics[i]._ql_have != px:
                want.append(i)
        # drop far-away textures to bound memory
        for i, pic in enumerate(self._pics):
            if (i < lo - 4 or i > hi + 4) and pic._ql_have:
                w, h = self._pages[i]
                pic.set_paintable(Gdk.Paintable.new_empty(max(1, int(w)), max(1, int(h))))
                pic._ql_have = 0
        if want:
            self._fb_worker.submit([(i, px) for i in want])

    def _on_page_texture(self, index, px, tex):
        if self.alive and index < len(self._pics):
            self._pics[index].set_paintable(tex)
            self._pics[index]._ql_have = px
        return False

    def destroy(self):
        super().destroy()
        if self._fb_worker is not None:
            self._fb_worker.stop()
            self._fb_worker = None
        self._pics = []


class _PdfPageWorker:
    def __init__(self, path, deliver, on_info):
        self.path, self.deliver, self.on_info = path, deliver, on_info
        self.cv = threading.Condition()
        self.jobs = []
        self.quit = False
        threading.Thread(target=self.run, name="ql-pdf", daemon=True).start()

    def submit(self, jobs):
        with self.cv:
            self.jobs = list(jobs)
            self.cv.notify()

    def stop(self):
        with self.cv:
            self.quit = True
            self.jobs = []
            self.cv.notify()

    def run(self):
        import cairo
        try:
            P = _poppler()
            doc = P.Document.new_from_file(GLib.filename_to_uri(self.path, None), None)
            sizes = [doc.get_page(i).get_size() for i in range(doc.get_n_pages())]
            GLib.idle_add(self.on_info, sizes, None)
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(self.on_info, [], getattr(e, "message", None) or str(e))
            return
        while True:
            with self.cv:
                while not self.jobs and not self.quit:
                    self.cv.wait()
                if self.quit:
                    return
                index, px = self.jobs.pop(0)
            try:
                page = doc.get_page(index)
                w, h = page.get_size()
                scale = px / max(1.0, w)
                pw, ph = max(1, int(w * scale)), max(1, int(h * scale))
                surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
                cr = cairo.Context(surf)
                cr.set_source_rgb(1, 1, 1)
                cr.paint()
                cr.scale(scale, scale)
                page.render(cr)
                surf.flush()
                data = GLib.Bytes.new(bytes(surf.get_data()))
                tex = Gdk.MemoryTexture.new(pw, ph, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                            data, surf.get_stride())
                GLib.idle_add(self.deliver, index, px, tex)
            except Exception as e:  # noqa: BLE001
                print("[quicklook] pdf page render failed:", e, file=sys.stderr)


# ==================================================================== images ===

class ImageRenderer(EmbeddingRenderer):
    kind = "image"
    module, clsname = "image_view", "ImageViewer"

    def __init__(self, *a, fmt=None, dims=(0, 0), **kw):
        super().__init__(*a, **kw)
        self.fmt, self.dims = fmt, dims
        self._anim_iter = None

    def start_metadata(self):
        w, h = self.dims
        if w and h:
            self.set_extra(f"{w} × {h}")

    HUGE_PX = 40_000_000          # above this: low-res decode in a thread, no embed
    MAX_DECODE_PX = 700_000_000   # above this: thumbnail / info only

    def build(self):
        w, h = self.dims
        if w * h > self.HUGE_PX:
            self.win.app.stats["last_embedded"] = False
            self.start_metadata()
            return self._build_large()
        if self._is_animated():
            self.win.app.stats["last_embedded"] = False
            self.start_metadata()
            return self._build_animation()
        return super().build()

    def _build_large(self):
        """Huge images: show the cached thumbnail (or a spinner) at once, then a
        ~4K downscale decoded off the main thread (one decode at a time)."""
        self.pic = self._picture()
        self.pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        tp = self.info.get_attribute_byte_string("thumbnail::path") \
            if self.info is not None and self.info.has_attribute("thumbnail::path") else None
        if tp and os.path.exists(tp):
            self.pic.set_paintable(_texture_from_path(tp))
        spin = Adw.Spinner(width_request=40, height_request=40,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        ov = Gtk.Overlay(child=self.pic)
        ov.add_overlay(spin)
        w, h = self.dims
        if w * h > self.MAX_DECODE_PX:
            spin.set_visible(False)
            if self.pic.get_paintable() is None:
                return self.build_card(rows=self.info_rows() + [("Note", "Too large to preview")])
            return ov
        path, cancel = self.path, self._cancel

        def work():
            with _DECODE_LOCK:
                if cancel.is_set():
                    return None
                from PIL import Image, ImageOps
                Image.MAX_IMAGE_PIXELS = None
                with Image.open(path) as im:
                    im.draft("RGB", (4096, 4096))        # JPEG: decode at reduced scale
                    im.thumbnail((4096, 4096), Image.Resampling.BILINEAR, reducing_gap=2.0)
                    im = ImageOps.exif_transpose(im)
                    im = im.convert("RGBA")
                    data = GLib.Bytes.new(im.tobytes())
                    return Gdk.MemoryTexture.new(im.width, im.height,
                                                 Gdk.MemoryFormat.R8G8B8A8, data, im.width * 4)

        def done(res, ok):
            spin.set_visible(False)
            if ok and res is not None:
                self.pic.set_paintable(res)
            elif not ok:
                print("[quicklook] large image decode failed:", res, file=sys.stderr)
                if self.pic.get_paintable() is None:
                    self.win.bin.set_child(self.build_card())
        self.run_bg(work, done, "ql-bigimage")
        return ov

    def _is_animated(self):
        if _ext(self.local.get_basename() or "") not in (".gif", ".webp", ".png", ".apng"):
            return False
        try:
            from PIL import Image
            with Image.open(self.path) as im:
                return bool(getattr(im, "is_animated", False))
        except Exception:  # noqa: BLE001
            return False

    def _picture(self):
        return Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.SCALE_DOWN,
                           hexpand=True, vexpand=True, margin_top=8, margin_bottom=8,
                           margin_start=8, margin_end=8)

    def build_fallback(self):
        self.pic = self._picture()
        path = self.path
        w, h = self.dims
        is_svg = _ext(path).startswith(".svg")

        def work():
            limit = 3200
            if is_svg:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 1600, 1600, True)
            elif w > limit or h > limit:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, limit, limit, True)
            else:
                pb = GdkPixbuf.Pixbuf.new_from_file(path)
            pb = pb.apply_embedded_orientation() or pb
            return Gdk.Texture.new_for_pixbuf(pb)

        def done(res, ok):
            if ok:
                self.pic.set_paintable(res)
            else:
                self.win.bin.set_child(GenericRenderer(self.win, self.gfile, self.info,
                                                       self.local).build())
        self.run_bg(work, done, "ql-image")
        return self.pic

    def _build_animation(self):
        self.pic = self._picture()
        path = self.path

        def work():
            return GdkPixbuf.PixbufAnimation.new_from_file(path)

        def done(anim, ok):
            if not ok:
                self.pic.set_paintable(_texture_from_path(path))
                return
            it = anim.get_iter(None)
            self._anim_iter = it

            def step():
                if not self.alive:
                    return False
                pb = it.get_pixbuf()
                if pb is not None:
                    self.pic.set_paintable(Gdk.Texture.new_for_pixbuf(pb.copy()))
                delay = it.get_delay_time()
                if delay < 0:
                    return False
                it.advance(None)
                self.timeout(max(20, delay), lambda: step() and False)
                return False
            step()
        self.run_bg(work, done, "ql-anim")
        return self.pic


# ==================================================================== models ===

class ModelRenderer(EmbeddingRenderer):
    kind = "model"
    module, clsname = "stl_view", "ModelViewer"

    def on_viewer_title(self):
        try:
            sub = self.viewer.get_subtitle() if self.viewer is not None else ""
        except Exception:  # noqa: BLE001
            sub = ""
        if sub:
            self.set_extra(sub)

    def build_fallback(self):
        """Static software render (numpy + Pillow painter's algorithm)."""
        self.pic = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN,
                               hexpand=True, vexpand=True, margin_top=12, margin_bottom=12,
                               margin_start=12, margin_end=12)
        spin = Adw.Spinner(width_request=48, height_request=48,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        stack = Gtk.Overlay(child=self.pic)
        stack.add_overlay(spin)
        path, dark = self.path, _is_dark()
        cancel = self._cancel

        def work():
            from .mesh_io import load_mesh
            mesh = load_mesh(path)
            if cancel.is_set():
                return None
            return mesh, _render_mesh(mesh, dark)

        def done(res, ok):
            spin.set_visible(False)
            if not ok or res is None:
                self.win.bin.set_child(GenericRenderer(self.win, self.gfile, self.info,
                                                       self.local).build())
                return
            mesh, tex = res
            self.pic.set_paintable(tex)
            ext = mesh.vertices.max(0) - mesh.vertices.min(0)
            self.set_extra(f"{mesh.n_triangles:,} triangles  ·  "
                           f"{ext[0]:.4g} × {ext[1]:.4g} × {ext[2]:.4g}")
        self.run_bg(work, done, "ql-mesh")
        return stack


def _render_mesh(mesh, dark, size=1400, max_tris=90000):
    import numpy as np
    from PIL import Image, ImageDraw
    v = np.asarray(mesh.vertices, dtype=np.float64)
    f = np.asarray(mesh.faces)
    if len(f) > max_tris:
        f = f[np.random.default_rng(0).choice(len(f), max_tris, replace=False)]
    if getattr(mesh, "up", "z") == "z":       # to y-up
        v = v[:, [0, 2, 1]] * np.array([1, 1, -1])
    v = v - (v.max(0) + v.min(0)) / 2
    yaw, pitch = np.radians(-35), np.radians(22)
    ry = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0], [-np.sin(yaw), 0, np.cos(yaw)]])
    rx = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)], [0, np.sin(pitch), np.cos(pitch)]])
    p = v @ (rx @ ry).T
    tri = p[f]                                           # (T,3,3)
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    light = np.array([0.35, 0.55, 0.75])
    light /= np.linalg.norm(light)
    shade = 0.25 + 0.75 * np.abs(n @ light)
    span = max(p[:, 0].ptp(), p[:, 1].ptp(), 1e-9)
    s = size * 0.86 / span
    xy = np.empty((len(f), 3, 2))
    xy[..., 0] = tri[..., 0] * s + size / 2
    xy[..., 1] = -tri[..., 1] * s + size / 2
    order = np.argsort(tri[..., 2].mean(1))              # far (negative z) first
    base = np.array([150, 170, 205]) if dark else np.array([120, 140, 180])
    cols = (base[None, :] * shade[:, None]).clip(0, 255).astype(np.uint8)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i in order:
        c = tuple(int(x) for x in cols[i])
        draw.polygon([tuple(pt) for pt in xy[i]], fill=c, outline=c)
    data = GLib.Bytes.new(img.tobytes())
    return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.R8G8B8A8, data, size * 4)


# ===================================================================== media ===

class MediaRenderer(Renderer):
    """Video / audio via View's MediaPlayer (VA-API + DMABuf zero-copy),
    compact: just play/pause and a seek bar."""
    kind = "media"

    def __init__(self, *a, audio=False, **kw):
        super().__init__(*a, **kw)
        self.audio = audio
        self.player = None

    def build(self):
        from .media_player import MediaPlayer
        audio_box = None
        if self.audio:
            audio_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                                valign=Gtk.Align.CENTER, halign=Gtk.Align.FILL,
                                margin_start=48, margin_end=48, margin_bottom=56)
            audio_box.append(self.icon_widget(160))
            audio_box.append(_label(self.get_title(), css=("ql-card-title",), xalign=0.5,
                                    wrap=True))
        p = MediaPlayer(compact=True, audio_widget=audio_box, click_to_pause=False)
        p.add_css_class("ql-media-dark")
        p.connect("info-changed", self._on_info)
        p.connect("error", self._on_error)
        p.connect("osd", lambda pl, text: pl.show_osd(text))
        self.player = p
        p.load_uri(self.local.get_uri(), audio_hint=self.audio)
        return p

    def _on_info(self, p):
        if not self.alive or p.duration <= 0:
            return
        d = p.duration // 1_000_000_000
        parts = [f"{d // 3600}:{d // 60 % 60:02d}:{d % 60:02d}" if d >= 3600
                 else f"{d // 60}:{d % 60:02d}"]
        w, h = p.video_size
        if not p.audio_only and w > 0:
            parts.append(f"{w} × {h}")
        self.set_extra("  ·  ".join(parts))

    def _on_error(self, _p, message):
        if self.alive:
            self.win.bin.set_child(GenericRenderer(self.win, self.gfile, self.info,
                                                   self.local).build())
            self.win.show_toast(_("Cannot play: {message}").format(message=message))
            self._release()

    def _release(self):
        p, self.player = self.player, None
        if p is not None:
            try:
                p.release()
            except Exception:  # noqa: BLE001
                pass

    def destroy(self):
        super().destroy()
        self._release()


# ================================================================== archives ===

def _make_table(headers, rows, right=(), expand_col=0, expand_all=False):
    model = Gtk.StringList.new([str(i) for i in range(len(rows))])
    cv = Gtk.ColumnView(model=Gtk.NoSelection(model=model), show_column_separators=True,
                        css_classes=["data-table"], hexpand=True, vexpand=True)

    def setup(_f, item, ci):
        lb = Gtk.Label(xalign=1.0 if ci in right else 0.0,
                       ellipsize=Pango.EllipsizeMode.MIDDLE if ci == expand_col
                       else Pango.EllipsizeMode.END, max_width_chars=60)
        if ci in right:
            lb.add_css_class("numeric")
        item.set_child(lb)

    def bind(_f, item, ci):
        row = rows[int(item.get_item().get_string())]
        item.get_child().set_text(row[ci] if ci < len(row) else "")
    for ci, h in enumerate(headers):
        f = Gtk.SignalListItemFactory()
        f.connect("setup", setup, ci)
        f.connect("bind", bind, ci)
        col = Gtk.ColumnViewColumn(title=h, factory=f, resizable=True,
                                   expand=expand_all or ci == expand_col)
        cv.append_column(col)
    return Gtk.ScrolledWindow(child=cv, hexpand=True, vexpand=True)


def list_archive(path, cancel, limit=20000):
    """[(name, size or None, is_dir)], total_uncompressed, truncated"""
    import tarfile
    import zipfile
    out, total = [], 0
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for zi in z.infolist():
                if cancel.is_set() or len(out) >= limit:
                    return out, total, True
                out.append((zi.filename, None if zi.is_dir() else zi.file_size, zi.is_dir()))
                total += zi.file_size
        return out, total, False
    try:
        is_tar = tarfile.is_tarfile(path)
    except Exception:  # noqa: BLE001
        is_tar = False
    if is_tar:
        with tarfile.open(path) as t:
            for m in t:
                if cancel.is_set() or len(out) >= limit:
                    return out, total, True
                out.append((m.name + ("/" if m.isdir() else ""),
                            None if m.isdir() else m.size, m.isdir()))
                if not m.isdir():
                    total += m.size
        return out, total, False
    exe = shutil.which("7z") or shutil.which("7zz")
    if exe:
        proc = subprocess.Popen([exe, "l", "-slt", "-ba", "-p", path],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, text=True, errors="replace")
        cur = {}
        try:
            for line in proc.stdout:
                if cancel.is_set() or len(out) >= limit:
                    proc.kill()
                    return out, total, True
                line = line.rstrip("\n")
                if not line:
                    if "Path" in cur:
                        d = "D" in cur.get("Attributes", "") or cur.get("Folder") == "+"
                        sz = int(cur["Size"]) if cur.get("Size", "").isdigit() else None
                        out.append((cur["Path"] + ("/" if d else ""), None if d else sz, d))
                        total += sz or 0
                    cur = {}
                elif " = " in line:
                    k, v = line.split(" = ", 1)
                    cur[k] = v
            if "Path" in cur:
                out.append((cur["Path"], int(cur.get("Size") or 0), False))
        finally:
            proc.wait()
        return out, total, False
    raise ValueError("Unsupported archive format")


class ArchiveRenderer(Renderer):
    kind = "archive"
    wants_vertical_keys = True

    def build(self):
        self.stack = Gtk.Stack()
        self.stack.add_named(Adw.Spinner(width_request=48, height_request=48,
                                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER),
                             "wait")
        path, cancel = self.path, self._cancel

        def done(res, ok):
            if not ok:
                self.stack.add_named(self.build_card(), "card")
                self.stack.set_visible_child_name("card")
                return
            entries, total, truncated = res
            files = sum(1 for e in entries if not e[2])
            rows = [(n, "—" if s is None else human_size(s)) for n, s, _d in entries]
            self.stack.add_named(_make_table(["Name", "Size"], rows, right=(1,)), "list")
            self.stack.set_visible_child_name("list")
            self.set_extra(f"{files:,}{'+' if truncated else ''} file{'s' if files != 1 else ''}"
                           f"  ·  {human_size(total)} uncompressed")
        self.run_bg(lambda: list_archive(path, cancel), done, "ql-archive")
        return self.stack


# ===================================================================== text ===

def _gtksource():
    try:
        import gi
        gi.require_version("GtkSource", "5")
        from gi.repository import GtkSource
        return GtkSource
    except (ValueError, ImportError):
        return None


def read_text(path, cap=TEXT_CAP):
    with open(path, "rb") as f:
        data = f.read(cap + 1)
    truncated = len(data) > cap
    data = data[:cap]
    for enc in ("utf-8", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8",):
        try:
            return data.decode(enc), truncated
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8", errors="strict"), truncated
    except UnicodeDecodeError as e:
        if truncated and e.start > len(data) - 4:      # cut inside a multibyte char
            return data[:e.start].decode("utf-8", errors="replace"), truncated
        return data.decode("latin-1"), truncated


def looks_like_text(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    if not head:
        return True
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start > len(head) - 4


class TextRenderer(Renderer):
    kind = "text"
    wants_vertical_keys = True

    def build(self):
        path = self.path
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.NONE, top_margin=10, bottom_margin=10,
                                 left_margin=14, right_margin=14, css_classes=["ql-code"])
        self.buffer = self.view.get_buffer()
        GS = _gtksource()
        if GS is not None:
            try:
                self.buffer = GS.Buffer()
                self.view = GS.View(buffer=self.buffer, editable=False, cursor_visible=False,
                                    monospace=True, show_line_numbers=True,
                                    top_margin=10, bottom_margin=10, left_margin=8)
            except Exception:  # noqa: BLE001
                GS = None
        self.sw = Gtk.ScrolledWindow(child=self.view, hexpand=True, vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.banner = Adw.Banner(title=_("Showing the first 2 MB"))
        box.append(self.banner)
        box.append(self.sw)
        name = self.local.get_basename() or ""
        dark = _is_dark()

        def work():
            text, truncated = read_text(path)
            runs = None
            if GS is None and len(text) <= HIGHLIGHT_CAP:
                runs = _pygments_runs(text, name, self.info.get_content_type()
                                      if self.info else None, dark)
            return text, truncated, runs

        def done(res, ok):
            if not ok:
                self.win.bin.set_child(self.build_card())
                return
            text, truncated, runs = res
            self.banner.set_revealed(truncated)
            if GS is not None:
                self._setup_gtksource(GS, name, text)
            self.buffer.set_text(text)
            self.view.get_buffer().place_cursor(self.view.get_buffer().get_start_iter())
            lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
            self.set_extra(f"{lines:,} line{'s' if lines != 1 else ''}")
            if runs:
                self._apply_runs(*runs)
        self.run_bg(work, done, "ql-text")
        return box

    def _setup_gtksource(self, GS, name, text):
        lm = GS.LanguageManager.get_default()
        lang = lm.guess_language(name, None)
        if lang is not None:
            self.buffer.set_language(lang)
        sm = GS.StyleSchemeManager.get_default()
        scheme = sm.get_scheme("Adwaita-dark" if _is_dark() else "Adwaita")
        if scheme is not None:
            self.buffer.set_style_scheme(scheme)

    def _apply_runs(self, styles, runs):
        buf = self.buffer
        table = buf.get_tag_table()
        tags = {}
        for key, st in styles.items():
            tag = Gtk.TextTag(name=f"ql-{key}")
            if st.get("color"):
                tag.set_property("foreground", "#" + st["color"])
            if st.get("bold"):
                tag.set_property("weight", Pango.Weight.BOLD)
            if st.get("italic"):
                tag.set_property("style", Pango.Style.ITALIC)
            table.add(tag)
            tags[key] = tag
        it = iter(runs)

        def chunk():
            if not self.alive:
                return False
            s = buf.get_start_iter()
            e = buf.get_start_iter()
            n = 0
            for key, a, b in it:
                s.set_offset(a)
                e.set_offset(b)
                buf.apply_tag(tags[key], s, e)
                n += 1
                if n >= 4000:
                    return True
            return False
        if chunk():
            src = GLib.idle_add(chunk, priority=GLib.PRIORITY_LOW)
            self._timeouts.append(src)


def _pygments_runs(text, name, content_type, dark):
    try:
        from .syntax import pygments_runs
    except ImportError:
        return None
    return pygments_runs(text, name, content_type, dark)


class CsvRenderer(EmbeddingRenderer):
    """Read-only table: View's TableViewer if present, else a ColumnView of
    the first CSV_ROWS rows (delimiter sniffed); total rows counted in the
    background."""
    kind = "csv"
    module, clsname = "table_view", "TableViewer"
    wants_vertical_keys = True
    CSV_ROWS = 5000

    def build_fallback(self):
        path = self.path
        delim = "\t" if _ext(path) == ".tsv" else None
        self.stack = Gtk.Stack()
        self.stack.add_named(Adw.Spinner(width_request=48, height_request=48,
                                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER),
                             "wait")
        limit = self.CSV_ROWS
        cancel = self._cancel

        def work():
            import csv
            import io
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                head = f.read(4 * 1024 * 1024)
            whole = len(head) >= size
            text = head.decode("utf-8", errors="replace")
            if text.startswith("\ufeff"):
                text = text[1:]
            dialect = None
            if delim is None:
                try:
                    dialect = csv.Sniffer().sniff(text[:32768], delimiters=",;\t|")
                except csv.Error:
                    dialect = None
            kw = {"delimiter": delim} if delim else {}
            reader = csv.reader(io.StringIO(text), dialect or "excel", **kw)
            rows, more = [], not whole
            for r in reader:
                if len(rows) > limit:
                    more = True
                    break
                rows.append(r)
            if not whole and not more:
                rows = rows[:-1]          # last line was probably cut in half
            return rows, more, size

        def done(res, ok):
            if not ok or not res[0]:
                tr = TextRenderer(self.win, self.gfile, self.info, self.local)
                tr.set_extra = self.set_extra
                self._text = tr
                self.stack.add_named(tr.build(), "text")
                self.stack.set_visible_child_name("text")
                return
            rows, more, size = res
            ncol = min(64, max(len(r) for r in rows[:2000]))
            head, body = rows[0], rows[1:]
            headers = [(head[i] if i < len(head) else "") or f"Column {i + 1}"
                       for i in range(ncol)]
            self.stack.add_named(_make_table(headers, body, expand_col=-1,
                                             expand_all=ncol <= 6), "table")
            self.stack.set_visible_child_name("table")
            if not more:
                self.set_extra(f"{len(body):,} rows  ·  {ncol} columns")
                return
            self.set_extra(f"first {len(body):,} rows  ·  {ncol} columns  ·  counting…")

            def count():
                n = 0
                with open(path, "rb") as f:
                    while not cancel.is_set():
                        chunk = f.read(16 * 1024 * 1024)
                        if not chunk:
                            break
                        n += chunk.count(b"\n")
                return n

            def counted(n, ok2):
                if ok2:
                    self.set_extra(f"showing {len(body):,} of ~{max(0, n - 1):,} rows  ·  "
                                   f"{ncol} columns")
            self.run_bg(count, counted, "ql-csv-count")
        self.run_bg(work, done, "ql-csv")
        return self.stack

    def preview_can_open(self):
        return self.viewer is not None     # only if View has a table viewer

    def destroy(self):
        super().destroy()
        t = getattr(self, "_text", None)
        if t is not None:
            t.destroy()


# ==================================================================== fonts ===

class FontRenderer(Renderer):
    kind = "font"

    def build(self):
        self.pic = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.SCALE_DOWN,
                               hexpand=True, vexpand=True, valign=Gtk.Align.START,
                               margin_top=20, margin_start=24, margin_end=24, margin_bottom=20)
        sw = Gtk.ScrolledWindow(child=self.pic, hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        path, dark = self.path, _is_dark()
        scale = 2

        def work():
            from PIL import Image, ImageDraw, ImageFont
            fam, style = ImageFont.truetype(path, 20).getname()
            lines = [("title", 34, f"{fam} {style}".strip()),
                     ("", 22, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
                     ("", 22, "abcdefghijklmnopqrstuvwxyz"),
                     ("", 22, "0123456789 .,:;!?@#$%&*()[]{}—"),
                     ("", 14, ""),
                     ("", 16, "The quick brown fox jumps over the lazy dog."),
                     ("", 24, "The quick brown fox jumps over the lazy dog."),
                     ("", 36, "The quick brown fox jumps over the lazy dog."),
                     ("", 52, "The quick brown fox jumps over the lazy dog."),
                     ("", 72, "The quick brown fox jumps.")]
            W = 900 * scale
            fg = (235, 235, 235, 255) if dark else (30, 30, 30, 255)
            rendered, H = [], 0
            for _k, size, text in lines:
                font = ImageFont.truetype(path, size * scale)
                asc, desc = font.getmetrics()
                rendered.append((font, text, asc + desc))
                H += int((asc + desc) * 1.15)
            img = Image.new("RGBA", (W, H + 20), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            y = 0
            for font, text, lh in rendered:
                if text:
                    d.text((0, y), text, font=font, fill=fg)
                y += int(lh * 1.15)
            bbox = img.getbbox() or (0, 0, W, H)
            img = img.crop((0, 0, min(W, bbox[2] + 4), min(img.height, bbox[3] + 4)))
            tex = Gdk.MemoryTexture.new(img.width, img.height, Gdk.MemoryFormat.R8G8B8A8,
                                        GLib.Bytes.new(img.tobytes()), img.width * 4)
            return tex, fam, style, img.width // scale, img.height // scale

        def done(res, ok):
            if not ok:
                self.win.bin.set_child(self.build_card())
                return
            tex, fam, style, w, h = res
            self.pic.set_paintable(tex)
            self.pic.set_size_request(-1, -1)
            self.set_extra(f"{fam} {style}".strip())
        self.run_bg(work, done, "ql-font")
        return sw


# =================================================================== office ===

def office_thumbnail(path):
    """Embedded preview image of ODF / OOXML documents, or None."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for cand in ("Thumbnails/thumbnail.png", "docProps/thumbnail.jpeg",
                         "docProps/thumbnail.png", "docProps/thumbnail.wmf"):
                if cand in names and not cand.endswith(".wmf"):
                    data = z.read(cand)
                    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
    except Exception:  # noqa: BLE001
        return None
    return None


def soffice_exe():
    for name in ("soffice", "libreoffice"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


_DECODE_LOCK = threading.Lock()
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "view", "quicklook")


class OfficeRenderer(Renderer):
    kind = "office"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.inner = None
        self.proc = None

    def build(self):
        self.bin = Adw.Bin(hexpand=True, vexpand=True)
        path = self.path
        exe = soffice_exe()
        thumb_tex = None
        try:
            thumb_tex = office_thumbnail(path) if path else None
        except Exception:  # noqa: BLE001
            thumb_tex = None
        icon = None
        if thumb_tex is not None:
            icon = Gtk.Picture(paintable=thumb_tex, can_shrink=True,
                               content_fit=Gtk.ContentFit.CONTAIN)
            icon.set_size_request(220, 220)
        if exe is None or path is None:
            self.bin.set_child(self.build_card(icon=icon))
            return self.bin
        spin_box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        spin_box.append(Adw.Spinner())
        spin_box.append(_label("Converting with LibreOffice…", css=("ql-dim",)))
        self.bin.set_child(self.build_card(icon=icon, below=spin_box))
        self._convert(exe, path, icon)
        return self.bin

    def _cache_pdf(self, path):
        import hashlib
        st = os.stat(path)
        key = hashlib.sha1(f"{self.local.get_uri()}\0{st.st_mtime_ns}\0{st.st_size}"
                           .encode()).hexdigest()
        return os.path.join(CACHE_DIR, key + ".pdf"), key

    def _convert(self, exe, path, icon):
        out_pdf, key = self._cache_pdf(path)
        if os.path.exists(out_pdf):
            GLib.idle_add(lambda: self._show_pdf(out_pdf) and False)
            return
        cancel = self._cancel

        def work():
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmpdir = os.path.join(CACHE_DIR, "tmp-" + key)
            os.makedirs(tmpdir, exist_ok=True)
            profile = "file://" + os.path.join(CACHE_DIR, "lo-profile")
            self.proc = subprocess.Popen(
                [exe, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                 "--convert-to", "pdf", "--outdir", tmpdir, path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                self.proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                raise RuntimeError("LibreOffice timed out")
            if cancel.is_set():
                shutil.rmtree(tmpdir, ignore_errors=True)
                return None
            pdfs = [n for n in os.listdir(tmpdir) if n.lower().endswith(".pdf")]
            if not pdfs:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise RuntimeError("conversion produced no PDF")
            os.replace(os.path.join(tmpdir, pdfs[0]), out_pdf)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return out_pdf

        def done(res, ok):
            if ok and res:
                self._show_pdf(res)
            else:
                self.bin.set_child(self.build_card(icon=icon))
        self.run_bg(work, done, "ql-office")

    def _show_pdf(self, pdf_path):
        if not self.alive:
            return
        self.inner = PdfRenderer(self.win, self.gfile, self.info, self.local,
                                 pdf_file=Gio.File.new_for_path(pdf_path))
        self.inner.set_extra = self.set_extra          # page count -> our subtitle
        try:
            w = self.inner.build()
            self.bin.set_child(w)
            self.inner.attached()
            self.wants_vertical_keys = True
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()

    def destroy(self):
        super().destroy()
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 9)
            except Exception:  # noqa: BLE001
                pass
        if self.inner is not None:
            self.inner.destroy()
            self.inner = None


# =================================================================== folder ===

class FolderRenderer(Renderer):
    kind = "folder"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.count = [0, 0, 0, 0]        # bytes, files, folders, unreadable
        self.counting = True

    def kind_description(self):
        return "Folder"

    def size_text(self):
        b, f, d, _ = self.count
        txt = human_size(b)
        return txt + ("…" if self.counting else "")

    def default_open_uri(self):
        return self.gfile.get_uri()

    def build(self):
        rows = self.info_rows()
        rows.insert(1, ("Size", "Calculating…"))
        rows.insert(2, ("Contains", "—"))
        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                max_children_per_line=6, min_children_per_line=3,
                                column_spacing=8, row_spacing=8, halign=Gtk.Align.CENTER)
        below = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.grid_label = _label("", css=("heading",))
        below.append(self.grid_label)
        below.append(self.grid)
        icon = self.icon_widget(160, allow_thumb=False)
        w = self.build_card(icon=icon, rows=rows, below=below)
        self._start_count()
        self._start_children()
        return w

    # ---- recursive size, live counter ----
    def _start_count(self):
        root = self.path
        if root is None:
            self.counting = False
            self._card_values["Size"].set_text("—")
            return
        cancel, count = self._cancel, self.count

        def walk():
            try:
                dev = os.lstat(root).st_dev
            except OSError:
                return
            stack = [root]
            while stack:
                if cancel.is_set():
                    return
                d = stack.pop()
                try:
                    it = os.scandir(d)
                except OSError:
                    count[3] += 1
                    continue
                with it:
                    for e in it:
                        if cancel.is_set():
                            return
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            count[3] += 1
                            continue
                        if e.is_dir(follow_symlinks=False):
                            count[2] += 1
                            if st.st_dev == dev:     # stay on this filesystem
                                stack.append(e.path)
                        else:
                            count[1] += 1
                            count[0] += st.st_size
        done_flag = [False]

        def thread():
            walk()
            done_flag[0] = True

        threading.Thread(target=thread, name="ql-du", daemon=True).start()

        def tick():
            finished = done_flag[0]
            self.counting = not finished
            self._update_count_labels()
            return not finished
        self.timeout(120, tick)

    def _update_count_labels(self):
        b, f, d, bad = self.count
        size = f"{human_size(b)} ({b:,} bytes)" if b >= 1000 else f"{b} bytes"
        if self.counting:
            size += " …"
        self._card_values["Size"].set_text(size)
        cont = f"{f:,} file{'s' if f != 1 else ''}, {d:,} folder{'s' if d != 1 else ''}"
        if bad and not self.counting:
            cont += f" ({bad:,} unreadable)"
        self._card_values["Contains"].set_text(cont)
        if self.win.renderer is self:
            self.win.refresh_header()

    def get_subtitle(self):
        b, f, d, _ = self.count
        s = f"Folder  ·  {human_size(b)} — {f:,} files, {d:,} folders"
        return s + ("  ·  counting…" if self.counting else "")

    # ---- first children ----
    def _start_children(self):
        gfile = self.local
        attrs = ("standard::display-name,standard::icon,standard::is-hidden,"
                 "standard::is-backup,standard::type,thumbnail::path,thumbnail::is-valid")

        def work():
            infos = []
            en = gfile.enumerate_children(attrs, Gio.FileQueryInfoFlags.NONE, None)
            total = 0
            while True:
                if self._cancel.is_set():
                    return None
                i = en.next_file(None)
                if i is None:
                    break
                if i.get_is_hidden() or i.get_is_backup():
                    continue
                total += 1
                if len(infos) < 20000:
                    infos.append(i)
            en.close(None)
            infos.sort(key=lambda i: (i.get_file_type() != Gio.FileType.DIRECTORY,
                                      GLib.utf8_collate_key_for_filename(
                                          i.get_display_name(), -1)))
            first = infos[:12]
            thumbs = {}
            for i in first:
                tp = i.get_attribute_byte_string("thumbnail::path") \
                    if i.has_attribute("thumbnail::path") else None
                if tp and os.path.exists(tp):
                    tex = _texture_from_path(tp)
                    if tex is not None:
                        thumbs[i.get_display_name()] = tex
            return first, thumbs, total

        def done(res, ok):
            if not ok or res is None:
                return
            first, thumbs, total = res
            text = ngettext("{n:,} item", "{n:,} items", total).format(n=total)
            if total > 12:
                text = _("{items} — first {n}").format(items=text, n=12)
            self.grid_label.set_text(text)
            for i in first:
                tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                               css_classes=["ql-tile"], width_request=96)
                tex = thumbs.get(i.get_display_name())
                if tex is not None:
                    pic = Gtk.Picture(paintable=tex, can_shrink=True,
                                      content_fit=Gtk.ContentFit.CONTAIN)
                    pic.set_size_request(64, 64)
                    tile.append(pic)
                else:
                    img = Gtk.Image(pixel_size=64)
                    if i.get_icon() is not None:
                        img.set_from_gicon(i.get_icon())
                    tile.append(img)
                lb = _label(i.get_display_name(), xalign=0.5, wrap=True, lines=2)
                lb.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                lb.set_max_width_chars(12)
                lb.set_justify(Gtk.Justification.CENTER)
                tile.append(lb)
                tile.set_tooltip_text(i.get_display_name())
                self.grid.append(tile)
        self.run_bg(work, done, "ql-ls")


# ================================================================= dispatch ===

def _resolve(gfile, info):
    """trash:// and recent:// items point at a real file: preview that."""
    if gfile.get_path() is not None:
        return gfile, info
    target = info.get_attribute_string("standard::target-uri") \
        if info.has_attribute("standard::target-uri") else None
    if target:
        t = Gio.File.new_for_uri(target)
        try:
            return t, t.query_info(ATTRS, Gio.FileQueryInfoFlags.NONE, None)
        except GLib.Error:
            pass
    return gfile, info


def _image_header(path):
    """(format, width, height) from the header only.  Pillow first: GdkPixbuf's
    get_file_info() decodes the whole image for some loaders (780 ms for a
    20000x20000 PNG)."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            w, h = im.size
            try:      # PNG's getexif() decodes the whole image: JPEG/TIFF/WebP only
                if im.format in ("JPEG", "MPO", "TIFF", "WEBP") and \
                        im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                    w, h = h, w
            except Exception:  # noqa: BLE001
                pass
            return im.format or "image", w, h
    except Exception:  # noqa: BLE001
        pass
    try:
        fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(path)
        return (fmt.get_name() if fmt is not None else None), w, h
    except Exception:  # noqa: BLE001
        return None, 0, 0


def warm_up():
    """Import the heavy modules in a background thread so the first Space
    press on a PDF / image / model does not pay for them."""
    def work():
        for mod in ("numpy", "PIL.Image", "preview.mesh_io", "preview.image_view",
                    "preview.pdf_view", "preview.stl_view", "pygments.lexers.python",
                    "pygments.styles"):
            try:
                importlib.import_module(mod)
            except Exception as e:  # noqa: BLE001
                print(f"[quicklook] warm-up import {mod} failed: {e}", file=sys.stderr)
    threading.Thread(target=work, name="ql-warmup", daemon=True).start()


def create_renderer(win, gfile):
    try:
        info = gfile.query_info(ATTRS, Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error as e:
        return ErrorRenderer(win, gfile, e)
    local, linfo = _resolve(gfile, info)
    args = (win, gfile, linfo, local)
    ftype = linfo.get_file_type()
    ct = linfo.get_content_type() or ""
    name = (local.get_basename() or "").lower()
    ext = _ext(name)
    path = local.get_path()

    if ftype == Gio.FileType.DIRECTORY or ct == "inode/directory":
        return FolderRenderer(*args)
    if path is None or ftype not in (Gio.FileType.REGULAR, Gio.FileType.SYMBOLIC_LINK):
        return GenericRenderer(*args)
    if linfo.get_size() == 0:
        return TextRenderer(*args) if (ext in TEXT_EXTS or ct.startswith("text/")) \
            else GenericRenderer(*args)
    if ext == ".pdf" or ct == "application/pdf":
        return PdfRenderer(*args)
    if ext in MODEL_EXTS:
        return ModelRenderer(*args)
    if ext in IMAGE_EXTS or (ct.startswith("image/") and ct != "image/vnd.djvu"):
        fmt, w, h = _image_header(path)
        if fmt is not None:
            return ImageRenderer(*args, fmt=fmt, dims=(w, h))
        return GenericRenderer(*args)
    if ct.startswith("video/") or ct.startswith("audio/") or ext in MEDIA_EXTS \
            or ct == "application/ogg":
        audio = ct.startswith("audio/") or (not ct.startswith("video/") and ext in
                                            {".mp3", ".oga", ".opus", ".flac", ".wav", ".m4a",
                                             ".aac", ".wma", ".aiff", ".aif"})
        return MediaRenderer(*args, audio=audio)
    if ext in OFFICE_EXTS:
        return OfficeRenderer(*args)
    if ct in ARCHIVE_CTS or ext in {".zip", ".tar", ".tgz", ".tbz2", ".txz", ".tar.gz",
                                    ".tar.bz2", ".tar.xz", ".tar.zst", ".7z", ".rar", ".jar",
                                    ".whl", ".apk", ".epub", ".tar.lzma"}:
        return ArchiveRenderer(*args)
    if ext in FONT_EXTS or ct.startswith("font/") or ct in (
            "application/x-font-ttf", "application/x-font-otf", "application/vnd.ms-opentype"):
        return FontRenderer(*args)
    if ext in (".csv", ".tsv") or ct in ("text/csv", "text/tab-separated-values"):
        return CsvRenderer(*args)
    if ext in TEXT_EXTS or name in TEXT_NAMES or Gio.content_type_is_a(ct, "text/plain") \
            or looks_like_text(path):
        return TextRenderer(*args)
    return GenericRenderer(*args)
