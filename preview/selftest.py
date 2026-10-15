"""``view-app --selftest``: prove that an installation works.

Headless checks (no display needed): library versions, every module imports,
render a PDF page with Poppler, decode a PNG with Pillow and GdkPixbuf, parse
an STL, parse a CSV, highlight code, combine images into a PDF, GStreamer
elements, PyOpenGL import, OCR availability.

UI checks (only when a display is available, skipped with --no-ui): open a
PDF, an image, a CSV, a text file and an STL in real windows and make sure
each viewer comes up.  ``--screenshots DIR`` also saves a PNG of each window.

Exit status 0 when every required check passes, 1 otherwise.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

from . import runtime

MIN_GTK = (4, 18)       # what libadwaita 1.7 requires; CSS custom properties need 4.16
MIN_ADW = (1, 7)        # Adw.ToggleGroup; Adw.ShortcutsDialog (1.8) is optional


class Result:
    def __init__(self):
        self.failed = []
        self.warned = []

    def ok(self, name, detail=""):
        print(f"  ok    {name}" + (f"  ({detail})" if detail else ""), flush=True)

    def fail(self, name, err):
        self.failed.append(name)
        print(f"  FAIL  {name}: {err}", flush=True)

    def warn(self, name, detail):
        self.warned.append(name)
        print(f"  warn  {name}: {detail}", flush=True)

    def check(self, name, fn, required=True):
        try:
            detail = fn()
        except Exception as e:  # noqa: BLE001
            if os.environ.get("PREVIEW_SELFTEST_TRACE"):
                traceback.print_exc()
            msg = f"{type(e).__name__}: {e}".strip()
            (self.fail if required else self.warn)(name, msg.splitlines()[0] if msg else "?")
            return False
        self.ok(name, detail or "")
        return True


# ------------------------------------------------------------ fixtures ---

def _make_fixtures(d):
    import cairo
    from PIL import Image, ImageDraw
    paths = {}
    # PDF: one A6 page with a filled rectangle and text
    p = os.path.join(d, "sample.pdf")
    surf = cairo.PDFSurface(p, 298, 420)
    cr = cairo.Context(surf)
    cr.set_source_rgb(0.1, 0.3, 0.8)
    cr.rectangle(30, 30, 200, 120)
    cr.fill()
    cr.set_source_rgb(0, 0, 0)
    cr.select_font_face("Sans")
    cr.set_font_size(28)
    cr.move_to(30, 220)
    cr.show_text("View selftest")
    surf.finish()
    paths["pdf"] = p
    # PNG
    p = os.path.join(d, "sample.png")
    im = Image.new("RGB", (160, 100), (240, 240, 240))
    dr = ImageDraw.Draw(im)
    dr.ellipse((20, 10, 140, 90), fill=(220, 60, 40))
    im.save(p)
    paths["png"] = p
    p = os.path.join(d, "sample2.jpg")
    im.rotate(90, expand=True).save(p, quality=85)
    paths["jpg"] = p
    # STL (ASCII tetrahedron)
    p = os.path.join(d, "sample.stl")
    v = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
    tris = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    with open(p, "w") as f:
        f.write("solid t\n")
        for a, b, c in tris:
            f.write(" facet normal 0 0 0\n  outer loop\n")
            for i in (a, b, c):
                f.write("   vertex %g %g %g\n" % v[i])
            f.write("  endloop\n endfacet\n")
        f.write("endsolid t\n")
    paths["stl"] = p
    # CSV
    p = os.path.join(d, "sample.csv")
    with open(p, "w", newline="") as f:
        f.write("name,qty,price\napple,3,1.25\npear,5,0.80\n\"plum, red\",7,2.10\n")
    paths["csv"] = p
    # text / code
    p = os.path.join(d, "sample.py")
    with open(p, "w") as f:
        f.write("def hello(name):\n    return f'Hello, {name}!'\n")
    paths["text"] = p
    return paths


# ------------------------------------------------------- headless checks ---

def _versions():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    gtk = (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version())
    adw = (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version())
    if gtk[:2] < MIN_GTK:
        raise RuntimeError(f"GTK {'.'.join(map(str, gtk))} < {'.'.join(map(str, MIN_GTK))}")
    if adw[:2] < MIN_ADW:
        raise RuntimeError(f"libadwaita {'.'.join(map(str, adw))} < "
                           f"{'.'.join(map(str, MIN_ADW))}")
    return (f"Python {sys.version.split()[0]}, GTK {'.'.join(map(str, gtk))}, "
            f"libadwaita {'.'.join(map(str, adw))}, PyGObject {gi.__version__}")


MODULES = ["app", "viewer_base", "image_view", "image_ops", "pdf_view", "pdf_ops",
           "pdf_render", "pdf_canvas", "markup", "signature", "stl_view", "mesh_io",
           "table_view", "table_data", "text_view", "syntax", "video_view", "media_player",
           "inspector", "convert", "ocr", "ocr_install", "preferences", "touchpad"]
LINUX_MODULES = ["integration", "quicklook", "quicklook_renderers"]


def _import_all():
    import importlib
    mods = MODULES + (LINUX_MODULES if runtime.IS_LINUX else [])
    for m in mods:
        importlib.import_module(f"preview.{m}")
    return f"{len(mods)} modules"


def _pdf(paths):
    import cairo
    import gi
    gi.require_version("Poppler", "0.18")
    from gi.repository import Poppler, Gio
    doc = Poppler.Document.new_from_gfile(Gio.File.new_for_path(paths["pdf"]), None, None)
    page = doc.get_page(0)
    w, h = page.get_size()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(w), int(h))
    cr = cairo.Context(surf)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    page.render(cr)
    surf.flush()
    data = surf.get_data()
    stride = surf.get_stride()
    off = 80 * stride + 100 * 4                      # inside the blue rectangle (BGRA)
    b, g, r = data[off], data[off + 1], data[off + 2]
    if not (b > 150 and r < 80):
        raise RuntimeError(f"unexpected pixel {r},{g},{b}")
    text = page.get_text() or ""
    if "selftest" not in text:
        raise RuntimeError(f"text layer missing ({text!r})")
    from pypdf import PdfReader
    n = len(PdfReader(paths["pdf"]).pages)
    return f"Poppler {Poppler.get_version()}, {int(w)}×{int(h)} pt, pypdf pages={n}"


def _png(paths):
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    from .image_ops import load_base
    im, _exif, _icc = load_base(paths["png"])
    if im.size != (160, 100):
        raise RuntimeError(f"size {im.size}")
    pb = GdkPixbuf.Pixbuf.new_from_file(paths["png"])
    if (pb.get_width(), pb.get_height()) != (160, 100):
        raise RuntimeError("GdkPixbuf size mismatch")
    fmts = sorted({f.get_name() for f in GdkPixbuf.Pixbuf.get_formats()})
    return f"Pillow + GdkPixbuf ({len(fmts)} loaders)"


def _stl(paths):
    from . import mesh_io
    m = mesh_io.load_mesh(paths["stl"])
    if m.n_triangles != 4:
        raise RuntimeError(f"{m.n_triangles} triangles")
    return f"{m.fmt}, {m.n_triangles} triangles, {m.n_vertices} vertices"


def _csv(paths):
    from .table_data import TableData
    t = TableData()
    t.open_path(paths["csv"])
    ends = [int(x) for chunk in t.scan() for x in chunk]
    if t.delim != "," or len(ends) != 4:
        raise RuntimeError(f"delim={t.delim!r} records={len(ends)}")
    return f"{len(ends)} records, header={t.has_header}"


def _code(paths):
    from . import syntax
    with open(paths["text"]) as f:
        text = f.read()
    r = syntax.pygments_runs(text, "sample.py", "text/x-python", False)
    if not r or not r[1]:
        raise RuntimeError("no highlighting runs")
    import pygments
    return f"Pygments {pygments.__version__}, {len(r[1])} runs"


def _combine(paths, d):
    from . import convert
    out = os.path.join(d, "combined.pdf")
    convert.images_to_pdf([paths["png"], paths["jpg"]], out, "fit", 0.0, True)
    from pypdf import PdfReader
    n = len(PdfReader(out).pages)
    if n != 2:
        raise RuntimeError(f"{n} pages")
    return "2 images → 2-page PDF"


def _gst():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    missing = [e for e in ("playbin3", "gtk4paintablesink", "videoconvert", "audioconvert")
               if Gst.ElementFactory.find(e) is None]
    if missing:
        raise RuntimeError("missing GStreamer elements: " + ", ".join(missing))
    hw = [f.get_name() for f in Gst.Registry.get().get_feature_list(Gst.ElementFactory)
          if "Hardware" in (f.get_metadata("klass") or "") and
          "Decoder" in (f.get_metadata("klass") or "")]
    return f"GStreamer {Gst.version_string().split()[-1]}, hw decoders: {len(hw)}"


def _gl():
    from OpenGL import GL  # noqa: F401
    import OpenGL
    return f"PyOpenGL {OpenGL.__version__} ({os.environ.get('PYOPENGL_PLATFORM', 'default')})"


def _ocr():
    from . import ocr
    helper = ocr.helper_path()
    if helper:
        ocr._get_engine()                   # starts the helper and loads the models
        return f"helper process {helper}"
    if ocr.is_available():
        import onnxruntime  # noqa: F401  (find_spec alone misses broken installs)
        import rapidocr  # noqa: F401
    else:
        from . import ocr_install
        ok, why = ocr_install.can_install()
        raise RuntimeError("not installed; " + ("offered as an optional download" if ok else why))
    return "rapidocr + onnxruntime importable"


def _ocr_run(paths, d):
    from PIL import Image, ImageDraw, ImageFont
    from . import ocr
    im = Image.new("RGB", (520, 90), "white")
    dr = ImageDraw.Draw(im)
    try:
        font = ImageFont.load_default(size=48)
    except TypeError:
        font = ImageFont.load_default()
    dr.text((14, 14), "Hello Preview", fill="black", font=font)
    lines = ocr.recognize(im)
    text = ocr.lines_to_text(lines)
    if "Preview" not in text:
        raise RuntimeError(f"recognized {text!r}")
    return repr(text)


# ------------------------------------------------------------- UI checks ---

def _ui(paths, res, shots=None):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gio, GLib, Gtk
    if not Gtk.init_check():
        res.warn("ui", "no display; skipped (run under a desktop session or xvfb-run)")
        return
    from .app import MainWindow, PreviewApp

    app = PreviewApp()
    app.set_application_id(runtime.APP_ID + ".SelfTest")
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.add_recent = lambda *_a: None          # keep the user's Recent list clean
    cases = [("pdf", [paths["pdf"]]), ("image", [paths["png"], paths["jpg"]]),
             ("table", [paths["csv"]]), ("text", [paths["text"]]), ("model", [paths["stl"]])]

    def pump(seconds):
        end = time.monotonic() + seconds
        ctx = GLib.MainContext.default()
        while time.monotonic() < end:
            ctx.iteration(False)
            time.sleep(0.01)

    def run_cases(_app):
        app.hold()
        try:
            for kind, files in cases:
                name = f"ui: {kind} viewer"
                win = MainWindow(app)
                win.set_default_size(900, 640)
                win.present()
                try:
                    win.load_files(kind, [Gio.File.new_for_path(p) for p in files])
                    pump(2.5 if kind in ("pdf", "model") else 1.5)
                    if win.viewer is None:
                        raise RuntimeError("viewer failed to load (error page shown)")
                    title = win.viewer.get_title()
                    if shots:
                        _try_snapshot(res, win, os.path.join(shots, f"selftest-{kind}.png"))
                    res.ok(name, title or "")
                except Exception as e:  # noqa: BLE001
                    if os.environ.get("PREVIEW_SELFTEST_TRACE"):
                        traceback.print_exc()
                    res.fail(name, f"{type(e).__name__}: {e}")
                win._confirmed_close = True
                win.destroy()
                pump(0.2)
            # signature window with the mouse fallback
            try:
                from .signature import SignatureCaptureWindow
                host = MainWindow(app)
                sw = SignatureCaptureWindow(host, None, draw_mode=True)
                sw.present()
                pump(0.5)
                if shots:
                    _try_snapshot(res, sw, os.path.join(shots, "selftest-signature.png"))
                sw.close()
                host.destroy()
                res.ok("ui: signature window (mouse/pen mode)")
            except Exception as e:  # noqa: BLE001
                res.fail("ui: signature window", f"{type(e).__name__}: {e}")
            try:
                from .preferences import PreferencesDialog
                host = MainWindow(app)
                host.present()
                dlg = PreferencesDialog(app)
                dlg.present(host)
                pump(0.8)
                if shots:
                    _try_snapshot(res, dlg, os.path.join(shots, "selftest-preferences.png"))
                dlg.force_close()
                host.destroy()
                res.ok("ui: preferences dialog")
            except Exception as e:  # noqa: BLE001
                res.fail("ui: preferences dialog", f"{type(e).__name__}: {e}")
            for action in ("shortcuts", "about"):
                try:
                    host = MainWindow(app)
                    host.present()
                    pump(0.3)
                    app.activate_action(action, None)
                    pump(0.8)
                    dlg = host.get_visible_dialog()
                    if dlg is None:
                        raise RuntimeError("no dialog shown")
                    if shots:
                        _try_snapshot(res, dlg, os.path.join(shots, f"selftest-{action}.png"))
                    dlg.force_close()
                    host.destroy()
                    res.ok(f"ui: {action} dialog", type(dlg).__name__)
                except Exception as e:  # noqa: BLE001
                    res.fail(f"ui: {action} dialog", f"{type(e).__name__}: {e}")
        finally:
            app.release()
            app.quit()

    app.connect("activate", run_cases)
    app.run([sys.argv[0]])


def _try_snapshot(res, widget, path):
    try:
        _snapshot(widget, path)
    except Exception as e:  # noqa: BLE001
        res.warn("screenshot " + os.path.basename(path), str(e))


def _snapshot(widget, path):
    import gi
    gi.require_version("Graphene", "1.0")
    from gi.repository import GLib, Graphene, Gtk
    paintable = Gtk.WidgetPaintable.new(widget)
    node = None
    for _ in range(60):                 # the first frame may not have been drawn yet
        w, h = widget.get_width(), widget.get_height()
        if w > 0 and h > 0:
            snap = Gtk.Snapshot()
            paintable.snapshot(snap, w, h)
            node = snap.to_node()
            if node is not None:
                break
        end = time.monotonic() + 0.05
        while time.monotonic() < end:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.005)
    if node is None:
        raise RuntimeError("nothing rendered")
    rect = Graphene.Rect().init(0, 0, w, h)
    tex = widget.get_native().get_renderer().render_texture(node, rect)
    tex.save_to_png(path)


# ------------------------------------------------------------------ main ---

def main(argv=()):
    argv = list(argv)
    no_ui = "--no-ui" in argv
    run_ocr = "--ocr" in argv
    shots = None
    if "--screenshots" in argv:
        i = argv.index("--screenshots")
        shots = os.path.abspath(argv[i + 1]) if i + 1 < len(argv) else os.getcwd()
        os.makedirs(shots, exist_ok=True)
    print(f"View {runtime.VERSION} selftest on {sys.platform}"
          f"{' (snap)' if runtime.IN_SNAP else ''}{' (frozen)' if runtime.FROZEN else ''}",
          flush=True)
    res = Result()
    # (Windows cannot delete files Poppler still has open: ignore cleanup errors)
    with tempfile.TemporaryDirectory(prefix="preview-selftest-", ignore_cleanup_errors=True) as d:
        if not res.check("versions", _versions):
            print("FAILED: required libraries are missing or too old", flush=True)
            return 1
        try:
            paths = _make_fixtures(d)
        except Exception as e:  # noqa: BLE001
            res.fail("fixtures", e)
            return 1
        res.check("imports", _import_all)
        res.check("pdf render (Poppler + pypdf)", lambda: _pdf(paths))
        res.check("image decode (PNG/JPEG)", lambda: _png(paths))
        res.check("3D model parse (STL)", lambda: _stl(paths))
        res.check("CSV parse", lambda: _csv(paths))
        res.check("syntax highlighting", lambda: _code(paths))
        res.check("combine images into PDF (img2pdf)", lambda: _combine(paths, d))
        res.check("video (GStreamer)", _gst)
        res.check("OpenGL (PyOpenGL)", _gl)
        ocr_ok = res.check("text recognition (OCR)", _ocr, required=runtime.OCR_BUNDLED)
        if ocr_ok and run_ocr:
            res.check("OCR recognize", lambda: _ocr_run(paths, d))
        if not no_ui:
            _ui(paths, res, shots)
    if res.failed:
        print(f"FAILED: {len(res.failed)} check(s): {', '.join(res.failed)}", flush=True)
        return 1
    print("PASSED" + (f" with {len(res.warned)} warning(s)" if res.warned else ""), flush=True)
    return 0
