"""``view-app --selftest``: prove that an installation works.

Headless checks (no display needed): library versions, every module imports,
render a PDF page with Poppler, decode a PNG with Pillow and GdkPixbuf, parse
an STL, parse a CSV, highlight code, parse and draw a DXF drawing, render
Markdown, combine images into a PDF, GStreamer elements, PyOpenGL import, OCR
availability, translation (language detection and text handling always; the
engine when it is installed).

UI checks (only when a display is available, skipped with --no-ui): open a
PDF, an image, a CSV, a text file, a Markdown file, an STL and a DXF in real
windows and make sure each viewer comes up.  ``--screenshots DIR`` also saves a PNG of each window.

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
    # Markdown
    p = os.path.join(d, "sample.md")
    with open(p, "w") as f:
        f.write("# Title\n\nSome **bold** text and a [link](https://example.com).\n\n"
                "- one\n- two\n\n```python\nprint('hi')\n```\n\n"
                "| a | b |\n|---|--:|\n| 1 | 2 |\n")
    paths["md"] = p
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
           "table_view", "table_data", "text_view", "syntax", "markdown_view", "dxf_view",
           "dxf_repair", "video_view", "media_player",
           "inspector", "convert", "ocr", "ocr_install", "preferences", "touchpad",
           "integration", "screenshot", "translate", "translate_install", "translate_ui",
           "translate_helper", "translate_view", "md_translate",
           "code_translate"]
LINUX_MODULES = ["quicklook", "quicklook_renderers"]


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


def _dxf(paths, d):
    """Write a small drawing with ezdxf, then parse, rasterize and export it."""
    import ezdxf
    from . import dxf_view
    p = os.path.join(d, "sample.dxf")
    doc = ezdxf.new("R2010", setup=True)
    doc.layers.add("PARTS", color=1)
    doc.layers.add("OFF", color=3).off()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
    msp.add_circle((50, 30), 20, dxfattribs={"layer": "PARTS"})
    msp.add_line((0, 0), (100, 60), dxfattribs={"layer": "OFF"})
    hatch = msp.add_hatch(color=5)
    hatch.paths.add_polyline_path([(5, 5), (25, 5), (25, 20), (5, 20)], is_closed=True)
    msp.add_text("View", height=6).set_placement((60, 45))
    doc.saveas(p)
    paths["dxf"] = p
    scene = dxf_view.build_scene(p)
    layers = {key: vis for key, _n, _c, vis, _cnt in scene.layers}
    if scene.is_empty() or layers.get("off") is not False or layers.get("parts") is not True:
        raise RuntimeError(f"groups={len(scene.groups)} layers={layers}")
    x0, y0, x1, y1 = scene.extents
    if not (abs(x1 - x0 - 100) < 1 and abs(y1 - y0 - 60) < 1):
        raise RuntimeError(f"extents {scene.extents}")
    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 120)
    cr = cairo.Context(surf)
    bx0, by0, bx1, by1 = scene.bbox
    dxf_view.paint(scene, cr, 200 / (bx1 - bx0), bx0, by0, 200, 120)
    surf.flush()
    data = surf.get_data()
    off = 95 * surf.get_stride() + 30 * 4             # inside the blue hatch (BGRA)
    if not (data[off] > 150 and data[off + 2] < 80 and data[off + 3] > 200):
        raise RuntimeError(f"unexpected pixel {data[off + 2]},{data[off + 1]},{data[off]}")
    svg = os.path.join(d, "sample.svg")
    dxf_view.export(scene, svg, "svg")
    if os.path.getsize(svg) < 500:
        raise RuntimeError("empty SVG export")
    return (f"ezdxf {ezdxf.__version__}, {len(scene.groups)} styles, "
            f"{sum(len(g.chunks) for g in scene.groups)} path chunks")


# What small DXF writers (CAD exports, scripts) produce: no $ACADVER (so R12),
# no handles, and post-R12 entities without their subclass markers.
LOOSE_DXF = """0
SECTION
2
HEADER
9
$INSUNITS
70
4
0
ENDSEC
0
SECTION
2
TABLES
0
TABLE
2
LAYER
70
1
0
LAYER
2
CUT
70
0
62
1
6
CONTINUOUS
0
ENDTAB
0
ENDSEC
0
SECTION
2
ENTITIES
0
LWPOLYLINE
8
CUT
90
4
70
1
10
0.0
20
0.0
10
80.0
20
0.0
10
80.0
20
40.0
10
0.0
20
40.0
0
HATCH
8
CUT
62
5
10
0.0
20
0.0
30
0.0
210
0.0
220
0.0
230
1.0
2
SOLID
70
1
71
0
91
1
92
3
72
0
73
1
93
3
10
10.0
20
10.0
10
30.0
20
10.0
10
20.0
20
30.0
97
0
75
0
76
1
98
0
0
CIRCLE
8
CUT
10
60.0
20
20.0
40
10.0
0
ENDSEC
0
EOF
"""


def _dxf_loose(paths, d):
    """A loosely written DXF (marker-less, handle-less LWPOLYLINE and HATCH)
    must load, with its flags and layers intact."""
    from . import dxf_repair, dxf_view
    p = os.path.join(d, "loose.dxf")
    with open(p, "w") as f:
        f.write(LOOSE_DXF)
    doc = dxf_view.read_document(p)
    msp = doc.modelspace()
    types = [e.dxftype() for e in msp]
    if types != ["LWPOLYLINE", "HATCH", "CIRCLE"]:
        raise RuntimeError(f"entities {types}")
    lw, hatch = msp[0], msp[1]
    if not (lw.closed and len(lw) == 4 and lw.dxf.layer == "CUT"):
        raise RuntimeError(f"LWPOLYLINE closed={lw.closed} n={len(lw)} layer={lw.dxf.layer}")
    if not (hatch.dxf.color == 5 and len(hatch.paths) == 1 and hatch.dxf.solid_fill):
        raise RuntimeError("HATCH lost its color or boundary")
    sample = os.path.join(d, "sample.dxf")              # well-formed, written by _dxf()
    if os.path.exists(sample):
        with open(sample, "rb") as f:
            if dxf_repair.normalize(f.read()) is not None:
                raise RuntimeError("a well-formed file would be rewritten")
    scene = dxf_view.build_scene(p)
    paths["dxf_loose"] = p
    x0, y0, x1, y1 = scene.extents
    if not (abs(x0) < 1e-6 and abs(y0) < 1e-6 and abs(x1 - 80) < 1e-6 and abs(y1 - 40) < 1e-6):
        raise RuntimeError(f"extents {scene.extents}")
    return f"{', '.join(types)} on {doc.dxfversion}"


def _markdown(paths):
    from . import markdown_view
    from .markdown_view import BULLETS
    import markdown_it
    with open(paths["md"]) as f:
        ops = markdown_view.build_ops(f.read(), False, os.path.dirname(paths["md"]))
    kinds = [op[0] for op in ops.ops]
    tags = {t for op in ops.ops if op[0] == "t" for t in op[2]}
    missing = [what for what, ok in (("heading", "h1" in tags), ("bold", "b" in tags),
                                     ("link", ops.links == ["https://example.com"]),
                                     ("table", "table" in kinds), ("code", bool(ops.styles)))
               if not ok]
    if missing:
        raise RuntimeError("missing " + ", ".join(missing))
    # a blank line inside a code block keeps the grey box (no empty paragraph);
    # a task list item shows its check box instead of the bullet
    small = markdown_view.build_ops("```\na\n\n\nb\n```\n\n- [ ] todo\n- [x] done\n- plain\n",
                                    False, None)
    code = "".join(op[1] for op in small.ops if op[0] == "t" and
                   any(t.startswith("blk:code") for t in op[2]))
    if code.count("\n") != 4 or any(not line for line in code.split("\n")[:-1]):
        raise RuntimeError(f"code block with an empty line: {code!r}")
    markers = [op[1] for op in small.ops if op[0] == "t" and "marker" in op[2]]
    if markers != ["☐\t", "☑\t", BULLETS[0] + "\t"] or "☐" in "".join(
            op[1] for op in small.ops if op[0] == "t" and "marker" not in op[2]):
        raise RuntimeError(f"task list markers {markers}")
    return f"markdown-it-py {markdown_it.__version__}, {len(ops.ops)} insert operations"


def _combine(paths, d):
    from . import convert
    out = os.path.join(d, "combined.pdf")
    convert.images_to_pdf([paths["png"], paths["jpg"]], out, "fit", 0.0, True)
    from pypdf import PdfReader
    n = len(PdfReader(out).pages)
    if n != 2:
        raise RuntimeError(f"{n} pages")
    return "2 images → 2-page PDF"


def _screenshots(paths, d):
    from . import screenshot
    cut = os.path.join(d, "half.png")
    with open(paths["png"], "rb") as f, open(cut, "wb") as out:
        out.write(f.read()[:-20])
    if not screenshot._complete(paths["png"]) or screenshot._complete(cut):
        raise RuntimeError("cannot tell a complete PNG from a partly written one")
    if not screenshot.supported():
        return "not available here"
    return (f"watching {screenshot.screenshot_dir()}, "
            f"{'on' if screenshot.chosen() else 'off'}, {screenshot._mode()} autostart "
            f"{'set' if screenshot.autostart_on() else 'not set'}, "
            f"watcher {'running' if screenshot.running() else 'not running'}")


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
    from . import opengl_win
    gl = opengl_win.ensure()            # before PyOpenGL loads opengl32.dll (Windows)
    from OpenGL import GL  # noqa: F401
    import OpenGL
    return (f"PyOpenGL {OpenGL.__version__} "
            f"({os.environ.get('PYOPENGL_PLATFORM', 'default')}), {gl}")


def _certs():
    """The Windows store may not hold GitHub's root yet: the build must bring
    Mozilla's (certifi) for update checks."""
    import certifi
    from .updates import ssl_context
    n = ssl_context().cert_store_stats()["x509_ca"]
    if n < 100:
        raise RuntimeError(f"only {n} root certificates")
    return f"{n} root certificates incl. certifi {certifi.__version__}"


def _touchpad():
    from .touchpad import Touchpad
    pad = Touchpad()
    try:
        return f"{pad.name}, {pad.width_mm:.0f} × {pad.height_mm:.0f} mm"
    finally:
        pad.close()


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


def _translate_text():
    """What works without the engine: detection, sentence handling, the BPE
    tokenizer, and a clean "not installed" error."""
    import io
    from . import translate as tr
    samples = {"en": "The weather is nice today and we are going to the park.",
               "es": "¿Dónde está la estación? El tren sale a las ocho de la mañana.",
               "zh": "今天天气很好，我们去公园散步吧。", "zt": "這個問題很簡單，我們會說明。",
               "fr": "Le chat est sur la table et il ne veut pas descendre.",
               "ja": "今日はいい天気ですね。", "ru": "Сегодня хорошая погода."}
    for code, text in samples.items():
        got = tr.detect(text)
        if got != code:
            raise RuntimeError(f"detected {got!r} for {code} text")
    units = tr._segment("A line that wraps in the\nmiddle of a sentence. Dr. Smith is here.\n\n"
                        "第一句。第二句！")
    sents = [s for k, s in units if k == "s"]
    if sents != ["A line that wraps in the middle of a sentence.", "Dr. Smith is here.",
                 "第一句。", "第二句！"]:
        raise RuntimeError(f"sentences {sents!r}")
    bpe = tr._BPE(io.StringIO("#version: 0.2\nh e\nl l\nhe ll\no</w> x\nl o</w>\n"))
    if bpe.segment(["hello"]) != ["hell@@", "o"]:
        raise RuntimeError(f"BPE {bpe.segment(['hello'])!r}")
    toks = tr._moses_tokenize('He said "it\'s fine", then left.', "en")
    back = tr._moses_detokenize(toks, "en")
    if back != 'He said "it\'s fine", then left.':
        raise RuntimeError(f"Moses round trip {toks!r} -> {back!r}")
    if not tr.is_available():
        try:
            tr.translate("Hello", "en", "es")
        except tr.NotInstalled:
            return "detection ok; engine not installed (handled)"
        raise RuntimeError("translate() without an engine did not raise NotInstalled")
    return f"detection ok; {len(tr.installed())} language pack(s)"


_MD_DOC = """---
title: Keep this front matter
---

# Getting *started* with `view` ##

Install it with `pip install view`, then read [the guide](https://example.com/a_b "Guide")
or <https://example.com>. Some **bold text:** here.\\
A hard break, <kbd>Ctrl</kbd> and ![a small logo](logo.png).

> A quote with a list:
> - first *item*
> - second item

1. [ ] task to do
2. step two
   continued here

| Name | What it does |
|------|:------------:|
| `cli` | runs the tool |
| plain | some *text* |

Setext title
------------

```python
print("never translated")
```

<div align="center">kept as it is</div>

[ref]: https://example.com/ref
"""


def _translate_markdown():
    """Structure-kept Markdown translation, with stand-in translators: one
    that keeps the placeholders, one that loses them (the fallback), one
    that answers in Chinese punctuation."""
    import re
    from . import md_translate as mt
    keep = mt.structure(_MD_DOC)

    def upper(texts):
        return [re.sub(r"[a-z]+", lambda m: m.group(0).upper(), t) for t in texts]

    def lossy(texts):
        return [re.sub(r"[A-Z]\d+", "", t).upper() for t in texts]

    def cjk(texts):
        return [re.sub(r"[A-Za-z]+", "文", t).replace(":", "：").replace(" ", "") for t in texts]
    for name, fn in (("placeholders kept", upper), ("placeholders lost", lossy),
                     ("CJK", cjk)):
        out = mt.translate(_MD_DOC, fn)
        if mt.structure(out) != keep:
            a = [x for x, y in zip(keep, mt.structure(out)) if x != y][:1]
            raise RuntimeError(f"{name}: structure changed at {a}")
        for must in ('print("never translated")', "title: Keep this front matter",
                     "https://example.com/a_b", "`pip install view`", "<kbd>", "</kbd>",
                     "<div align=\"center\">kept as it is</div>", "[ref]: https://example.com/ref",
                     "logo.png", "------------"):
            if must not in out:
                raise RuntimeError(f"{name}: lost {must!r}")
    out = mt.translate(_MD_DOC, upper)
    for must in ("# GETTING *STARTED* WITH `view` ##", "[THE GUIDE](https://example.com/a_b",
                 "> - FIRST *ITEM*", "1. [ ] TASK TO DO", "| `cli` | RUNS THE TOOL |",
                 "![A SMALL LOGO](logo.png)", "SETEXT TITLE", "HERE.\\\nA HARD BREAK"):
        if must not in out:
            raise RuntimeError(f"translated text missing: {must!r}")
    # pieces for the side-by-side views
    from . import translate_view as tv
    units = tv.text_units("one\ntwo\n\n\nthree\n" + "x\n" * 70)
    if [(a, n) for a, n, _t in units] != [(0, 2), (4, 30), (34, 30), (64, 11)]:
        raise RuntimeError(f"text units {[(a, n) for a, n, _t in units]}")
    lines = [["001 A Title", 72, 60, 300, 80], ["002 First line of a paragraph that", 72, 100, 540, 110],
             ["003 wraps here.", 72, 112, 200, 122], ["004 Second paragraph starts", 90, 124, 540, 134],
             ["005 and ends.", 72, 136, 160, 146], ["7", 300, 700, 306, 710]]
    paras = tv.group_lines(lines)
    if paras != [("A Title", True), ("First line of a paragraph that\nwraps here.", False),
                 ("Second paragraph starts\nand ends.", False)]:
        raise RuntimeError(f"PDF paragraphs {paras!r}")
    if tv.worth_translating("aj wj = cj dj , dj = wj ‖wj‖ , (1)") or \
            not tv.worth_translating("We evaluate this inversion.") or \
            not tv.worth_translating("今天天气很好"):
        raise RuntimeError("formula / text tell-apart")
    return "structure kept (3 stand-in translators); paragraphs, line numbers"


def _translate_mixed():
    """Text partly in the target language already, code, tables: what goes to
    the engine and what is kept (a stand-in translator, no engine)."""
    import re
    from . import code_translate as ct, syntax, translate as tr

    def run(text, src, tgt):            # translate_batch's own segmenting, a fake engine
        units = tr._segment(text, src, tgt)
        sent = [u[1] for u in units if u[0] == "s"]
        it = iter(["<" + x.upper() + ">" for x in sent])
        return tr._assemble([("s", next(it)) if u[0] == "s" else u for u in units], tgt), sent
    out, sent = run("Wendong finished, HR first.\nExpectation, 上班的具体jd, O1, H1b, "
                    "是否远程, 股份, 工资.\n说明。dd", "en", "zh")
    if "上班的具体jd, O1, H1b, 是否远程, 股份, 工资." not in out or "说明。dd" not in out \
            or any(re.search(r"[一-鿿]", x) for x in sent):
        raise RuntimeError(f"mixed en/zh into zh: {sent!r} -> {out!r}")
    out, sent = run("今天天气很好 and we are going out", "zh", "en")
    if sent != ["今天天气很好"] or "and we are going out" not in out:
        raise RuntimeError(f"mixed zh/en into en: {sent!r}")
    if tr.detect("Wendong finished, HR first. Expectation, O1, H1b, description.dd") != "en":
        raise RuntimeError("stray 'O1' taken for Portuguese")
    if tr.detect("TagGraph is a memory framework with a pipeline that turns each note into a "
                 "graph. Retrieval is a staged pipeline.") != "en":
        raise RuntimeError("English with many 'a' taken for Hungarian")
    # code: comments and messages only, on the same lines
    src = ('#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\nimport os  # noqa: F401\n\n\n'
           'def f(path, n):\n    """Open the file and read\n    every line of it."""\n'
           '    key = "user_id"\n    # Count the lines,\n    # skipping the empty ones.\n'
           '    print("Found the lines in the file")\n    return os.path.join(path, key)\n')
    plan = ct.CodePlan(src, syntax.lexer_for("f.py"))
    want = ["Open the file and read every line of it.", "Count the lines, skipping the empty ones.",
            "Found the lines in the file"]
    if plan.pieces != want:
        raise RuntimeError(f"code pieces {plan.pieces!r}")
    out = plan.render({p: p.upper() for p in plan.pieces})
    for must in ("# -*- coding: utf-8 -*-", '"user_id"', "# noqa: F401", "# COUNT THE LINES,",
                 "# SKIPPING THE EMPTY ONES.", '"""OPEN THE FILE AND READ', "def f(path, n):"):
        if must not in out:
            raise RuntimeError(f"code: lost {must!r}")
    if out.count("\n") != src.count("\n"):
        raise RuntimeError("code: the line count changed")
    if ct.distribute("数字来源于店家CSV出口，作为团队的短篇摘要印刷。", [55, 33]) != \
            ["数字来源于店家CSV出口，", "作为团队的短篇摘要印刷。"]:
        raise RuntimeError("laying a block back over its lines")
    # tables: text cells only
    cells = {"Wireless keyboard": True, "Please deliver before noon": True, "无线键盘": True,
             "2026-09-01": False, "49.90": False, "SKU-1001": False, "user_id": False,
             "a@b.com": False, "https://x.org": False, "12 kg": False, "USD": False}
    bad = [c for c, want in cells.items() if ct.cell_worth(c) != want]
    if bad:
        raise RuntimeError(f"table cells misjudged: {bad}")
    return "target-language text kept; code comments/strings only; table text cells"


def _translate_engine():
    from . import translate as tr
    helper = tr.helper_path()
    if helper:
        tr._helper(helper)._start()         # starts the helper and waits for "ready"
        return f"helper process {helper}"
    if tr.is_available():
        import ctranslate2  # noqa: F401  (find_spec alone misses broken installs)
        import sentencepiece  # noqa: F401
        return f"ctranslate2 {ctranslate2.__version__} importable"
    from . import translate_install
    ok, why = translate_install.can_install()
    raise RuntimeError("not installed; " + ("offered as an optional download" if ok else why))


def _translate_download(d):
    """Two windows downloading the same language pack: one download, the
    other waits for it; two processes (no shared lock): separate temporary
    files, and the pack that lands first is kept."""
    import io
    import threading
    import zipfile
    from . import translate as tr
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pack/model/model.bin", b"x" * 4096)
        z.writestr("pack/metadata.json", "{}")
    blob = buf.getvalue()
    calls, parts = [], set()
    incoming = os.path.join(d, "packs", ".incoming")

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(blob))}

        def read(self, n=-1):
            time.sleep(0.02)
            parts.update(os.listdir(incoming))
            return super().read(min(n, 512) if n and n > 0 else 512)

    def urlopen(url, timeout=None):
        calls.append(url)
        return Response(blob)

    saved = tr.packs_dir, tr._index, tr._urlopen
    tr.packs_dir = lambda: os.path.join(d, "packs")
    tr._index = lambda: {("xx", "yy"): ["https://example.invalid/xx_yy"]}
    tr._urlopen = urlopen
    errors = []

    def run(fn, *args):
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    try:
        def both(fn, *args):
            threads = [threading.Thread(target=run, args=(fn, *args)) for _i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        both(tr.download, [("xx", "yy")])
        if errors or len(calls) != 1 or tr.installed() != [("xx", "yy")]:
            raise RuntimeError(f"in one process: {len(calls)} downloads, {errors or tr.installed()}")
        tr.remove_all()
        calls.clear()
        parts.clear()
        both(tr._download_pack, ("xx", "yy"), "https://example.invalid/xx_yy",
             tr._incoming_dir(), "", (0, 1), None, None)
        part_files = {p for p in parts if p.endswith(".part")}
        if errors or len(calls) != 2 or len(part_files) != 2 or tr.installed() != [("xx", "yy")]:
            raise RuntimeError(f"two processes: {errors} {sorted(parts)} {tr.installed()}")
        if os.listdir(incoming):
            raise RuntimeError(f"left behind: {os.listdir(incoming)}")
    finally:
        tr.packs_dir, tr._index, tr._urlopen = saved
    return "one download for two windows; separate .part files, complete pack kept"


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
             ("table", [paths["csv"]]), ("text", [paths["text"]]), ("text", [paths["md"]]),
             ("model", [paths["stl"]])]
    if "dxf" in paths:
        cases.append(("cad", [paths["dxf"]]))
    if "dxf_loose" in paths:
        cases.append(("cad", [paths["dxf_loose"]]))

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
                label = ("markdown" if files[0].endswith(".md") else
                         "cad-loose" if files[0].endswith("loose.dxf") else kind)
                name = f"ui: {label} viewer"
                win = MainWindow(app)
                win.set_default_size(900, 640)
                win.present()
                try:
                    win.load_files(kind, [Gio.File.new_for_path(p) for p in files])
                    pump(2.5 if kind in ("pdf", "model", "cad") else 1.5)
                    if win.viewer is None:
                        raise RuntimeError("viewer failed to load (error page shown)")
                    if label == "markdown" and (win.viewer._md is None or win.viewer._md.view
                                                .get_buffer().get_char_count() == 0):
                        raise RuntimeError("Markdown preview is empty")
                    if kind == "cad":
                        # parsing and rasterizing run in worker threads: give a busy
                        # machine some more time before calling it a failure
                        end = time.monotonic() + 10
                        while win.viewer.canvas.raster is None and time.monotonic() < end:
                            pump(0.1)
                        if win.viewer.canvas.raster is None:
                            raise RuntimeError("drawing not rendered")
                    dead = _unreachable_actions(win, pump)
                    if dead:
                        raise RuntimeError("header bar can't reach " + ", ".join(dead))
                    title = win.viewer.get_title()
                    gl_err = getattr(win.viewer, "_gl_failed", None)
                    if gl_err:
                        # the Windows build brings its own OpenGL (Mesa) for PCs without
                        # a driver, so there it must always work
                        if runtime.IS_WINDOWS and runtime.FROZEN:
                            raise RuntimeError(f"no 3D rendering: {gl_err}")
                        res.warn(name + " (OpenGL)", gl_err)
                    if shots:
                        _try_snapshot(res, win, os.path.join(shots, f"selftest-{label}.png"))
                    res.ok(name, title or "")
                except Exception as e:  # noqa: BLE001
                    if os.environ.get("PREVIEW_SELFTEST_TRACE"):
                        traceback.print_exc()
                    res.fail(name, f"{type(e).__name__}: {e}")
                win._confirmed_close = True
                win.destroy()
                pump(0.2)
            try:
                res.ok("ui: Markdown table widths (Chinese)", _ui_cjk_table(pump, shots))
            except Exception as e:  # noqa: BLE001
                if os.environ.get("PREVIEW_SELFTEST_TRACE"):
                    traceback.print_exc()
                res.fail("ui: Markdown table widths (Chinese)", f"{type(e).__name__}: {e}")
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


CJK_TABLE = """# 表格

| 名称 | 描述 | 状态 |
|------|------|:----:|
| 甲 | 列表中的第一项，描述比较长 | 完成 |
| 乙 | 简短 | 待定 |
| PDF | 支持 PDF 文件的查看和搜索 | 完成 |
"""


def _ui_cjk_table(pump, shots=None):
    """A Chinese table in a wide preview: every cell on one line.  (Chinese
    may wrap between any two characters, and the table used to be laid out
    at its minimum width: one character per line.)"""
    from gi.repository import Gtk
    from .markdown_view import MarkdownPreview
    win = Gtk.Window(default_width=900, default_height=400)
    preview = MarkdownPreview()
    win.set_child(preview)
    win.present()
    try:
        preview.set_language("zh-cn")       # (as a translation into Chinese has)
        preview.set_source(CJK_TABLE)
        end = time.monotonic() + 5
        labels = []
        while time.monotonic() < end:
            pump(0.1)
            grids = [w for w in _walk(preview) if isinstance(w, Gtk.Grid)]
            labels = [w for g in grids for w in _walk(g) if isinstance(w, Gtk.Label)]
            if labels and all(lab.get_width() > 0 for lab in labels):
                break
        if len(labels) != 12:
            raise RuntimeError(f"expected a table of 12 cells, found {len(labels)}")
        glyph = labels[0].create_pango_layout("中").get_pixel_size()[0]
        widths = [lab.get_width() for lab in labels[:3]]
        if min(widths) < 2 * glyph or widths[1] < 8 * glyph:
            raise RuntimeError(f"columns {widths} px wide, a character is {glyph} px")
        wrapped = [lab.get_text() for lab in labels if lab.get_layout().get_line_count() > 1]
        if wrapped:
            raise RuntimeError(f"cells wrapped in a 900 px view: {wrapped}")
        # Chinese text marked as Chinese (else Pango may take Japanese glyphs / mixed fonts)
        buf = preview.view.get_buffer()
        tag = buf.get_tag_table().lookup("lang")
        if (tag is None or tag.get_property("language") != "zh-cn" or
                not buf.get_start_iter().has_tag(tag) or
                any("zh-cn" not in lab.get_attributes().to_string() for lab in labels)):
            raise RuntimeError("text or table cells without the zh-cn language")
        if shots:
            try:
                _snapshot(win, os.path.join(shots, "selftest-markdown-cjk-table.png"))
            except Exception:  # noqa: BLE001
                pass
        return f"columns {widths} px, a character {glyph} px"
    finally:
        preview.release()
        win.destroy()


def _menu_actions(model, out):
    for i in range(model.get_n_items()):
        a = model.get_item_attribute_value(i, "action", None)
        if a is not None:
            out.append((a.get_string(), model.get_item_attribute_value(i, "target", None)))
        links = model.iterate_item_links(i)
        while links.next():
            _menu_actions(links.get_value(), out)
    return out


def _unreachable_actions(win, pump):
    """Header-bar buttons and menu items whose action the viewer has enabled
    but which can't find it from the header (it lives outside the viewer)."""
    from gi.repository import Gio, Gtk
    groups = [g for g in vars(win.viewer).values() if isinstance(g, Gio.ActionGroup)]
    wanted = []
    for w in _walk(win.header):
        if isinstance(w, Gtk.MenuButton) and w.get_menu_model() is not None:
            wanted += _menu_actions(w.get_menu_model(), [])
        elif isinstance(w, Gtk.Actionable) and w.get_action_name():
            wanted.append((w.get_action_name(), w.get_action_target_value()))
    dead = set()
    for name, target in wanted:
        short = name.partition(".")[2]
        if not any(g.has_action(short) and g.get_action_enabled(short) for g in groups):
            continue            # not the viewer's, or off for now
        probe = Gtk.Button(action_name=name)
        if target is not None:
            probe.set_action_target_value(target)
        win.header.pack_start(probe)
        pump(0.02)
        if not probe.get_sensitive():
            dead.add(name)
        win.header.remove(probe)
    return sorted(dead)


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


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
        res.check("Markdown render (markdown-it-py)", lambda: _markdown(paths))
        res.check("DXF drawing (ezdxf)", lambda: _dxf(paths, d))
        res.check("loosely written DXF (no subclass markers)", lambda: _dxf_loose(paths, d))
        res.check("combine images into PDF (img2pdf)", lambda: _combine(paths, d))
        res.check("screenshot editing", lambda: _screenshots(paths, d))
        res.check("video (GStreamer)", _gst)
        res.check("OpenGL (PyOpenGL)", _gl)
        res.check("update check certificates", _certs,
                  required=runtime.IS_WINDOWS and runtime.FROZEN)
        res.check("touchpad (for signing)", _touchpad, required=False)
        ocr_ok = res.check("text recognition (OCR)", _ocr, required=runtime.OCR_BUNDLED)
        if ocr_ok and run_ocr:
            res.check("OCR recognize", lambda: _ocr_run(paths, d))
        res.check("translation (detection, sentences)", _translate_text)
        res.check("translation (Markdown structure, side-by-side)", _translate_markdown)
        res.check("translation (mixed languages, code, tables)", _translate_mixed)
        res.check("translation engine", _translate_engine, required=runtime.OCR_BUNDLED)
        res.check("language pack downloads (concurrent)", lambda: _translate_download(d))
        if not no_ui:
            _ui(paths, res, shots)
    if res.failed:
        print(f"FAILED: {len(res.failed)} check(s): {', '.join(res.failed)}", flush=True)
        return 1
    print("PASSED" + (f" with {len(res.warned)} warning(s)" if res.warned else ""), flush=True)
    return 0
