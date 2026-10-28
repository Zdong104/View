"""DXF viewer: 2D CAD drawings (AutoCAD Drawing Exchange Format).

ezdxf (imported lazily, in a worker thread) parses the file and its drawing
add-on resolves layers, colors, linetypes, text, dimensions, hatches and
blocks into plain lines, Bezier paths and filled outlines.  A recording
backend keeps those once, as pycairo paths grouped by style and chopped into
chunks with bounding boxes (Scene).  Nothing is parsed again after that:

* the canvas asks a worker to rasterize the visible region (plus a margin)
  from the cached paths at the current zoom; chunks outside it are skipped;
* while panning or pinching, the last raster is moved and scaled until the
  next one is ready, so the window stays responsive on large drawings.

Layer visibility and the dark-mode color swap (ACI 7 is black on a light
background and white on a dark one, like AutoCAD) are applied at paint time,
so toggling them only repaints.
"""
from __future__ import annotations

import math
import os
import threading
import weakref
from collections import Counter

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

from .i18n import _, ngettext  # noqa: E402
from .viewer_base import Viewer, device_scale, file_info_section, surface_origin  # noqa: E402

NORM = float(1 << 20)       # the drawing's extents span this many path units
CHUNK_OPS = 2000            # primitives per cached path chunk (culling granularity)
MAX_SCALE = 7.0             # device px per path unit: cairo's 24.8 fixed point ends at ±8M
RASTER_MARGIN = 0.2         # rasterize this much beyond each side of the view
RASTER_MAX_PX = 24_000_000
TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
KIND_ORDER = {"fill": 0, "stroke": 1, "point": 2, "text": 3}
LIGHT_BG = (1.0, 1.0, 1.0)
DARK_BG = (0.12, 0.12, 0.13)
DARK_FG = (0.92, 0.92, 0.92)
# $INSUNITS
UNITS = {1: "in", 2: "ft", 3: "mi", 4: "mm", 5: "cm", 6: "m", 7: "km", 8: "µin", 9: "mil",
         10: "yd", 11: "Å", 12: "nm", 13: "µm", 14: "dm", 15: "dam", 16: "hm", 17: "Gm",
         18: "AU", 19: "ly", 20: "pc", 21: "US ft", 22: "US in", 23: "US yd", 24: "US mi"}


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Scene: the drawing, flattened once into styled cairo path chunks
# ---------------------------------------------------------------------------
class Group:
    """Everything of one style: kind (fill / stroke / point / text), layer,
    the top-level entity's layer (an INSERT's), color and lineweight."""
    __slots__ = ("kind", "layer", "top", "rgba", "bg", "lw", "chunks", "lines", "paths",
                 "polys", "fpaths", "points")

    def __init__(self, kind, layer, top, color, lw):
        self.kind, self.layer, self.top, self.lw = kind, layer, top, lw
        self.bg = color == "bg"
        self.rgba = (1.0, 1.0, 1.0, 1.0) if self.bg else _parse_color(color)
        self.chunks = []        # [(cairo.Path, (x0, y0, x1, y1) in path units)]
        self.lines = []         # recording: flat x0, y0, x1, y1, …
        self.paths = []         # [(command codes, vertices)]
        self.polys = []         # [vertices] closed polygons
        self.fpaths = []        # [(command codes, vertices)] filled, closed sub-paths
        self.points = []        # flat x, y, …


class Scene:
    def __init__(self):
        self.groups: list[Group] = []
        self.layers = []        # [(key, name, (r, g, b), visible at open, primitives)]
        self.extents = None     # (x0, y0, x1, y1) in drawing units
        self.bbox = (0.0, 0.0, 0.0, 0.0)     # the same in path units (y down)
        self.k = 1.0            # path units per drawing unit
        self.center = (0.0, 0.0)
        self.units = 0
        self.info = []          # Inspector rows
        self.n_entities = 0

    def is_empty(self):
        return not self.groups

    def to_path_units(self, x, y):
        return (x - self.center[0]) * self.k, -(y - self.center[1]) * self.k


def _parse_color(c):
    c = (c or "#000000").lstrip("#")
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        a = int(c[6:8], 16) if len(c) >= 8 else 255
    except ValueError:
        return (0.0, 0.0, 0.0, 1.0)
    return (r / 255, g / 255, b / 255, a / 255)


_recorder_cls = None


def _recorder_class():
    """ezdxf drawing backend that records primitives into Groups (defined
    on first use: ezdxf is imported lazily)."""
    global _recorder_cls
    if _recorder_cls is not None:
        return _recorder_cls
    import numpy as np
    from ezdxf.addons.drawing.backend import Backend

    class Recorder(Backend):
        def __init__(self, doc, cancel):
            super().__init__()
            self.doc = doc
            self.cancel = cancel
            self.groups: dict[tuple, Group] = {}
            self.tops: dict[str, str] = {}
            self.count = 0

        def _top(self, handle):
            top = self.tops.get(handle)
            if top is None:
                e = self.doc.entitydb.get(handle) if handle else None
                top = (e.dxf.get("layer", "0") if e is not None else "0").lower()
                self.tops[handle] = top
            return top

        def _group(self, kind, p):
            self.count += 1
            if not self.count & 1023 and self.cancel.is_set():
                raise Cancelled()
            cur = self.current_entity
            dxftype = cur.dxftype() if cur is not None else ""
            if kind == "fill" and dxftype in TEXT_TYPES:
                kind = "text"
            color = "bg" if dxftype == "WIPEOUT" else p.color
            key = (kind, p.layer.lower(), self._top(p.handle), color, p.lineweight)
            g = self.groups.get(key)
            if g is None:
                g = self.groups[key] = Group(*key)
            return g

        def set_background(self, color):
            pass

        def draw_point(self, pos, properties):
            self._group("point", properties).points += (pos.x, pos.y)

        def draw_line(self, start, end, properties):
            self._group("stroke", properties).lines += (start.x, start.y, end.x, end.y)

        def draw_solid_lines(self, lines, properties):
            out = self._group("stroke", properties).lines
            for s, e in lines:
                out += (s.x, s.y, e.x, e.y)

        def draw_path(self, path, properties):
            if len(path):
                self._group("stroke", properties).paths.append(
                    (path.command_codes(), path.np_vertices()))

        def draw_filled_paths(self, paths, properties):
            g = self._group("fill", properties)
            paths = list(paths)
            if g.kind == "fill":
                # exteriors counter-clockwise, holes clockwise: the group is
                # filled with the nonzero rule, so separate hatches unite
                try:
                    from ezdxf.addons.drawing.backend import oriented_paths
                    ext, holes = oriented_paths(paths)
                    paths = ext + holes
                except Exception:  # noqa: BLE001
                    pass
            for p in paths:
                if len(p):
                    g.fpaths.append((p.command_codes(), p.np_vertices()))

        def draw_filled_polygon(self, points, properties):
            v = points.np_vertices()
            if len(v) < 3:
                return
            x, y = v[:, 0], v[:, 1]
            if float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) < 0:
                v = v[::-1]                    # counter-clockwise (nonzero fill)
            self._group("fill", properties).polys.append(v)

        def draw_image(self, image_data, properties):
            # raster images are not shown; their outline marks the place
            v = image_data.pixel_boundary_path.np_vertices()
            if len(v) < 2:
                return
            m = image_data.transform
            pts = [m.transform((float(x), float(y), 0.0)) for x, y in v]
            out = self._group("stroke", properties).lines
            for a, b in zip(pts, pts[1:] + pts[:1]):
                out += (a.x, a.y, b.x, b.y)

        def clear(self):
            pass

        def finalize(self):
            pass

    _recorder_cls = Recorder
    return Recorder


def _emit_path(cr, codes, pts, close):
    """Append an ezdxf path (codes + normalized vertex list) to cr."""
    x, y = pts[0]
    cr.move_to(x, y)
    i = 1
    for c in codes:
        if c == 1:                                  # LINE_TO
            x, y = pts[i]
            cr.line_to(x, y)
            i += 1
        elif c == 3:                                # CURVE4_TO: ctrl1, ctrl2, end
            (a, b), (c2, d), (x, y) = pts[i], pts[i + 1], pts[i + 2]
            cr.curve_to(a, b, c2, d, x, y)
            i += 3
        elif c == 2:                                # CURVE3_TO: ctrl, end
            (qx, qy), (ex, ey) = pts[i], pts[i + 1]
            cr.curve_to(x + (qx - x) * 2 / 3, y + (qy - y) * 2 / 3,
                        ex + (qx - ex) * 2 / 3, ey + (qy - ey) * 2 / 3, ex, ey)
            x, y = ex, ey
            i += 2
        elif c == 4:                                # MOVE_TO
            if close:
                cr.close_path()
            x, y = pts[i]
            cr.move_to(x, y)
            i += 1
    if close:
        cr.close_path()


def _build_chunks(scene, groups, cancel):
    """Normalize every group's recording into path units and turn it into
    cairo path chunks (the per-vertex Python work happens once, here)."""
    import numpy as np
    lo, hi = np.array([np.inf, np.inf]), np.array([-np.inf, -np.inf])
    for g in groups:
        arrs = [v for _c, v in g.paths] + g.polys + [v for _c, v in g.fpaths]
        if g.lines:
            arrs.append(np.asarray(g.lines, dtype=float).reshape(-1, 2))
        if g.points:
            arrs.append(np.asarray(g.points, dtype=float).reshape(-1, 2))
        for a in arrs:
            a = a[np.isfinite(a).all(axis=1)]
            if len(a):
                lo = np.minimum(lo, a.min(axis=0))
                hi = np.maximum(hi, a.max(axis=0))
    if not np.isfinite(lo).all():
        return
    span = float(max(hi[0] - lo[0], hi[1] - lo[1]))
    if span <= 0:
        span = 1.0
    k = NORM / span
    cx, cy = float(lo[0] + hi[0]) / 2, float(lo[1] + hi[1]) / 2
    scene.extents = (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))
    scene.k, scene.center = k, (cx, cy)
    x0, y0 = scene.to_path_units(lo[0], hi[1])
    x1, y1 = scene.to_path_units(hi[0], lo[1])
    scene.bbox = (x0, y0, x1, y1)
    off = np.array([cx, cy])
    mul = np.array([k, -k])

    def norm(v):
        return (np.asarray(v, dtype=float) - off) * mul

    def spread(v):
        v = (v | (v << 8)) & 0x00FF00FF
        v = (v | (v << 4)) & 0x0F0F0F0F
        v = (v | (v << 2)) & 0x33333333
        return (v | (v << 1)) & 0x55555555

    def z_order(pts):
        """Indices sorting points (path units) along a Morton curve, so each
        chunk covers a compact area and culling works when zoomed in."""
        if len(pts) < 2:
            return np.arange(len(pts))
        q = np.clip(((pts + NORM / 2) / NORM * 1024).astype(np.int64), 0, 1023)
        return np.argsort(spread(q[:, 0]) | (spread(q[:, 1]) << 1), kind="stable")

    surf = cairo.ImageSurface(cairo.FORMAT_A8, 1, 1)
    for g in groups:
        if cancel.is_set():
            raise Cancelled()
        cr = cairo.Context(surf)
        n = 0

        def flush():
            nonlocal n
            if n:
                g.chunks.append((cr.copy_path(), cr.path_extents()))
                cr.new_path()
                n = 0
        if g.lines:
            ln = norm(np.asarray(g.lines).reshape(-1, 2)).reshape(-1, 4)
            ln = ln[z_order((ln[:, :2] + ln[:, 2:]) / 2)]
            for xa, ya, xb, yb in ln.tolist():
                cr.move_to(xa, ya)
                cr.line_to(xb, yb)
                n += 1
                if n >= CHUNK_OPS:
                    flush()
        for items, close in ((g.paths, False), (g.fpaths, True), (g.polys, None)):
            if not items:
                continue
            firsts = norm([(it[1] if close is not None else it)[0] for it in items])
            for i in z_order(firsts).tolist():
                if close is None:                       # polygon: vertices only
                    pts = norm(items[i]).tolist()
                    cr.move_to(*pts[0])
                    for p in pts[1:]:
                        cr.line_to(*p)
                    cr.close_path()
                else:
                    codes, v = items[i]
                    pts = norm(v).tolist()
                    _emit_path(cr, codes, pts, close)
                n += 1 + len(pts) // 8
                if n >= CHUNK_OPS:
                    flush()
        if g.points:
            pts = norm(np.asarray(g.points).reshape(-1, 2))
            for x, y in pts[z_order(pts)].tolist():
                cr.move_to(x, y)
                cr.line_to(x, y)
                n += 1
                if n >= CHUNK_OPS:
                    flush()
        flush()
        g.lines = g.paths = g.polys = g.fpaths = g.points = None     # free the recording
    scene.groups = sorted((g for g in groups if g.chunks), key=lambda g: KIND_ORDER[g.kind])


def build_scene(path: str, cancel: threading.Event | None = None) -> Scene:
    """Parse a DXF file and flatten its model space (worker thread)."""
    cancel = cancel or threading.Event()
    import ezdxf
    from ezdxf import recover
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
    try:
        doc = ezdxf.readfile(path)
    except ezdxf.DXFStructureError:
        doc, _auditor = recover.readfile(path)       # slower, but repairs broken files
    if cancel.is_set():
        raise Cancelled()
    msp = doc.modelspace()
    scene = Scene()
    ctx = RenderContext(doc)
    shown = {}

    def all_layers_on(layers):
        # record hidden layers too: the Layers list can show them later
        for lp in layers:
            shown[lp.layer.lower()] = lp.is_visible
            lp.is_visible = True
    ctx.set_layer_properties_override(all_layers_on)
    ctx.set_current_layout(msp)
    rec = _recorder_class()(doc, cancel)
    # a white background resolves ACI 7 to black; dark mode swaps it at paint time
    config = Configuration(background_policy=BackgroundPolicy.WHITE)
    Frontend(ctx, rec, config=config).draw_layout(
        msp, finalize=True, layout_properties=ctx.current_layout_properties)
    groups = list(rec.groups.values())
    counts = Counter()
    for g in groups:
        counts[g.layer] += (len(g.lines) // 4 + len(g.paths) + len(g.polys) + len(g.fpaths)
                            + len(g.points) // 2)
    _build_chunks(scene, groups, cancel)

    for key, lp in ctx.layers.items():
        rgba = _parse_color(lp.color)
        scene.layers.append((lp.layer.lower(), lp.layer, rgba[:3], shown.get(lp.layer.lower(), True),
                             counts.get(lp.layer.lower(), 0)))
    known = {ly[0] for ly in scene.layers}
    for key in sorted(set(counts) - known):          # layers missing from the table
        scene.layers.append((key, key, (0.5, 0.5, 0.5), True, counts[key]))
    scene.layers.sort(key=lambda ly: ly[1].casefold())

    types = Counter(e.dxftype() for e in msp)
    scene.n_entities = sum(types.values())
    scene.units = int(doc.header.get("$INSUNITS", 0) or 0)
    release = getattr(doc, "acad_release", "") or ""
    rows = [(_("Format"), f"DXF {doc.dxfversion}" + (f" (AutoCAD {release})" if release else "")),
            (_("Units"), UNITS.get(scene.units, _("Unitless")))]
    if scene.extents:
        x0, y0, x1, y1 = scene.extents
        rows.append((_("Size"), f"{_num(x1 - x0)} × {_num(y1 - y0)} {UNITS.get(scene.units, '')}"
                     .strip()))
        rows.append((_("Extents"), f"({_num(x0)}, {_num(y0)}) – ({_num(x1)}, {_num(y1)})"))
    n_on = sum(1 for ly in scene.layers if ly[3])
    rows.append((_("Layers"), _("{n:,} ({on:,} shown)").format(n=len(scene.layers), on=n_on)))
    blocks = sum(1 for b in doc.blocks if not b.name.startswith("*"))
    rows.append((_("Blocks"), f"{blocks:,}"))
    rows.append((_("Entities"), f"{scene.n_entities:,}"))
    if types:
        rows.append((_("Most common"), ", ".join(f"{t} {n:,}" for t, n in types.most_common(6))))
    scene.info = rows
    return scene


def _num(v):
    v = float(v)
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e6 or a < 1e-3:
        return f"{v:.4g}"
    return f"{v:,.{max(0, 4 - int(math.floor(math.log10(a))))}f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Painting (any thread; also used for export)
# ---------------------------------------------------------------------------
def adapt_color(rgb, dark):
    """Black and white swap with the background (ACI 7); otherwise colors
    that would all but vanish into it (blue on dark, yellow on white) are
    lifted or darkened a little, keeping their hue."""
    r, g, b = rgb
    if dark and r == g == b == 0.0:
        return DARK_FG
    if not dark and r == g == b == 1.0:
        return (0.0, 0.0, 0.0)
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if dark and lum < 0.2:
        return tuple(c + (1.0 - c) * 0.35 for c in rgb)
    if not dark and lum > 0.8:
        return tuple(c * 0.75 for c in rgb)
    return rgb


def group_color(g, dark):
    if g.bg:
        return (*(DARK_BG if dark else LIGHT_BG), 1.0)
    return (*adapt_color(g.rgba[:3], dark), g.rgba[3])


def paint(scene, cr, scale, ox, oy, w, h, dark=False, hidden=frozenset(), lineweights=True,
          hairline=1.0):
    """Draw the scene: path point (ox, oy) at cr's origin, `scale` units of
    cr per path unit, culling everything outside a w × h area.  Line widths
    are in cr units (hairline for thin lines)."""
    base = cr.get_matrix()
    view = cairo.Matrix(scale, 0, 0, scale, -ox * scale, -oy * scale).multiply(base)
    pad = 8 * hairline / scale
    vx0, vy0 = ox - pad, oy - pad
    vx1, vy1 = ox + w / scale + pad, oy + h / scale + pad
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for g in scene.groups:
        if g.layer in hidden or g.top in hidden:
            continue
        cr.new_path()
        cr.set_matrix(view)
        drawn = False
        for path, (x0, y0, x1, y1) in g.chunks:
            if x1 < vx0 or x0 > vx1 or y1 < vy0 or y0 > vy1:
                continue
            cr.append_path(path)
            drawn = True
        cr.set_matrix(base)
        if not drawn:
            continue
        cr.set_source_rgba(*group_color(g, dark))
        if g.kind == "fill":
            cr.set_fill_rule(cairo.FILL_RULE_WINDING)
            cr.fill()
        elif g.kind == "text":
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.fill()
        elif g.kind == "point":
            cr.set_line_width(3 * hairline)
            cr.stroke()
        else:
            lw = hairline
            if lineweights and g.lw > 0.3:
                lw *= min(4.0, g.lw / 0.3)
            cr.set_line_width(lw)
            cr.stroke()


def rasterize(scene, scale, ox, oy, pw, ph, dark, hidden, lineweights, hairline):
    """paint() into a pw × ph texture.  Big rasters are split into horizontal
    bands painted in parallel (pycairo drops the GIL while cairo strokes)."""
    n = 1
    if pw * ph > 600_000 and sum(len(g.chunks) for g in scene.groups) > 40:
        n = max(1, min(6, (os.cpu_count() or 2) - 1, ph // 64))
    edges = [ph * i // n for i in range(n + 1)]
    out = [None] * n

    def band(i):
        h = edges[i + 1] - edges[i]
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, h)
        cr = cairo.Context(surf)
        cr.set_tolerance(0.25)
        paint(scene, cr, scale, ox, oy + edges[i] / scale, pw, h, dark, hidden, lineweights,
              hairline)
        surf.flush()
        out[i] = (bytes(surf.get_data()), surf.get_stride())
    threads = [threading.Thread(target=band, args=(i,), name="dxf-band", daemon=True)
               for i in range(1, n)]
    for t in threads:
        t.start()
    band(0)
    for t in threads:
        t.join()
    if any(o is None for o in out):
        raise RuntimeError("raster band failed")
    return Gdk.MemoryTexture.new(pw, ph, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                 GLib.Bytes.new(b"".join(o[0] for o in out)), out[0][1])


def export(scene, path, fmt, hidden=frozenset(), lineweights=True):
    """Whole drawing as PNG, PDF or SVG (light colors, white background)."""
    x0, y0, x1, y1 = scene.bbox
    bw, bh = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    long_side = 4096 if fmt == "png" else 1190          # px / pt (A3-ish)
    s = long_side / max(bw, bh)
    m = 0.03 * long_side
    w, h = bw * s + 2 * m, bh * s + 2 * m
    if fmt == "png":
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, math.ceil(w), math.ceil(h))
        hair = 1.2
    elif fmt == "svg":
        surf = cairo.SVGSurface(path, w, h)
        hair = 0.35
    else:
        surf = cairo.PDFSurface(path, w, h)
        hair = 0.35
    cr = cairo.Context(surf)
    cr.set_source_rgb(*LIGHT_BG)
    cr.paint()
    cr.translate(m, m)
    paint(scene, cr, s, x0, y0, bw * s, bh * s, False, hidden, lineweights, hair)
    if fmt == "png":
        surf.write_to_png(path)
    surf.finish()


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------
class DxfCanvas(Gtk.Widget):
    """Shows a Scene with pan / zoom.  Zoom is in logical px per path unit;
    (cx, cy) is the path point at the centre of the view."""
    __gtype_name__ = "PreviewDxfCanvas"
    __gsignals__ = {"view-changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self):
        super().__init__(focusable=True, hexpand=True, vexpand=True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.scene = None
        self.fit = True
        self.zoom = 1.0
        self.cx = self.cy = 0.0
        self.hidden = frozenset()
        self.dark = False
        self.lineweights = True
        self.raster = None          # (texture, scale, rx0, ry0, style)
        self._busy = False
        self._want = False
        self._gen = 0
        self._ptr = None
        self._drag0 = None

        drag = Gtk.GestureDrag(button=0)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", lambda *_: self.set_cursor_from_name(None))
        self.add_controller(drag)
        click = Gtk.GestureClick()
        click.connect("pressed", self._pressed)
        self.add_controller(click)
        pinch = Gtk.GestureZoom()
        pinch.connect("begin", self._pinch_begin)
        pinch.connect("scale-changed", self._pinch_changed)
        self.add_controller(pinch)
        self._pinch = pinch
        self._pinch_z0 = 1.0
        self._pinch_anchor = None
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: setattr(self, "_ptr", (x, y)))
        self.add_controller(motion)

    # ---- content / style -------------------------------------------------
    def set_scene(self, scene):
        self.scene = scene
        self.raster = None
        self.fit = True
        self._changed()

    def set_style(self, hidden=None, dark=None, lineweights=None):
        if hidden is not None:
            self.hidden = frozenset(hidden)
        if dark is not None:
            self.dark = bool(dark)
        if lineweights is not None:
            self.lineweights = bool(lineweights)
        self.queue_draw()

    def release(self):
        self._gen += 1
        self.scene = None
        self.raster = None

    # ---- geometry ----------------------------------------------------------
    def fit_zoom(self):
        s = self.scene
        vw, vh = self.get_width(), self.get_height()
        if s is None or s.is_empty() or vw <= 0 or vh <= 0:
            return 1.0
        x0, y0, x1, y1 = s.bbox
        bw, bh = max(x1 - x0, NORM * 1e-6), max(y1 - y0, NORM * 1e-6)
        return min(vw / bw, vh / bh) * 0.94

    def max_zoom(self):
        return MAX_SCALE / device_scale(self)

    def eff_zoom(self):
        return self.fit_zoom() if self.fit else self.zoom

    def center(self):
        if self.fit and self.scene is not None:
            x0, y0, x1, y1 = self.scene.bbox
            return (x0 + x1) / 2, (y0 + y1) / 2
        return self.cx, self.cy

    def zoom_percent(self):
        return 100.0 * self.eff_zoom() / self.fit_zoom()

    def _clamp(self):
        if self.scene is None:
            return
        x0, y0, x1, y1 = self.scene.bbox
        self.cx = min(max(self.cx, x0), x1)
        self.cy = min(max(self.cy, y0), y1)

    def zoom_to(self, z, ax=None, ay=None):
        if self.scene is None or self.scene.is_empty():
            return
        fz = self.fit_zoom()
        z_old = self.eff_zoom()
        z = max(fz / 8, min(max(self.max_zoom(), fz), z))
        vw, vh = self.get_width(), self.get_height()
        if ax is None:
            ax, ay = vw / 2, vh / 2
        cx, cy = self.center()
        if abs(z - fz) / fz < 0.005:
            self.fit = True
        else:
            px, py = cx + (ax - vw / 2) / z_old, cy + (ay - vh / 2) / z_old
            self.cx, self.cy = px - (ax - vw / 2) / z, py - (ay - vh / 2) / z
            self.zoom = z
            self.fit = False
            self._clamp()
        self._changed()

    def zoom_by(self, factor, ax=None, ay=None):
        self.zoom_to(self.eff_zoom() * factor, ax, ay)

    def zoom_fit(self):
        self.fit = True
        self._changed()

    def pan(self, dx, dy):
        if self.scene is None:
            return
        z = self.eff_zoom()
        cx, cy = self.center()
        self.zoom = z
        self.fit = False
        self.cx, self.cy = cx - dx / z, cy - dy / z
        self._clamp()
        self._changed()

    def _changed(self):
        self.queue_draw()
        self.emit("view-changed")

    # ---- input -------------------------------------------------------------
    def _drag_begin(self, g, _x, _y):
        self.grab_focus()
        self._drag0 = None
        if self.scene is not None:
            self._drag0 = (self.eff_zoom(),) + self.center()
            self.set_cursor_from_name("grabbing")

    def _drag_update(self, _g, dx, dy):
        if self._drag0 is None:
            return
        z, cx, cy = self._drag0
        self.zoom, self.fit = z, False
        self.cx, self.cy = cx - dx / z, cy - dy / z
        self._clamp()
        self._changed()

    def _pressed(self, _g, n_press, x, y):
        if n_press == 2:
            if self.fit:
                self.zoom_by(2.0, x, y)
            else:
                self.zoom_fit()

    def _pinch_begin(self, _g, _seq):
        self._pinch_z0 = self.eff_zoom()
        ok, x, y = self._pinch.get_bounding_box_center()
        self._pinch_anchor = (x, y) if ok else None

    def _pinch_changed(self, _g, scale):
        a = self._pinch_anchor or (None, None)
        self.zoom_to(self._pinch_z0 * scale, a[0], a[1])

    def _scroll(self, ctl, dx, dy):
        if self.scene is None:
            return False
        state = ctl.get_current_event_state()
        wheel = ctl.get_unit() == Gdk.ScrollUnit.WHEEL
        a = self._ptr or (None, None)
        if state & Gdk.ModifierType.CONTROL_MASK or (wheel and not state & Gdk.ModifierType.SHIFT_MASK):
            # a mouse wheel zooms at the pointer (as in CAD programs); touchpad
            # scrolling pans, Ctrl+scroll zooms like the image viewer
            self.zoom_by(math.exp(-dy * (0.15 if wheel else 0.01)), a[0], a[1])
            return True
        if wheel:                                       # Shift+wheel: pan sideways
            dx, dy = dy, 0.0
        m = 50 if wheel else 1
        self.pan(-dx * m, -dy * m)
        return True

    # ---- drawing -------------------------------------------------------------
    def _style(self):
        return (self.dark, self.hidden, self.lineweights)

    def do_size_allocate(self, width, height, baseline):
        self.emit("view-changed")

    def do_snapshot(self, snapshot):
        vw, vh = self.get_width(), self.get_height()
        bg = Gdk.RGBA()
        bg.red, bg.green, bg.blue = DARK_BG if self.dark else LIGHT_BG
        bg.alpha = 1.0
        r = Graphene.Rect()
        snapshot.append_color(bg, r.init(0, 0, vw, vh))
        s = self.scene
        if s is None or s.is_empty() or vw <= 0 or vh <= 0:
            return
        ds = device_scale(self)
        z = self.eff_zoom()
        cx, cy = self.center()
        want = z * ds
        rs = self.raster
        if rs is not None:
            tex, scale, rx0, ry0, style = rs
            x = (rx0 - cx) * z + vw / 2
            y = (ry0 - cy) * z + vh / 2
            w, h = tex.get_width() / scale * z, tex.get_height() / scale * z
            exact = abs(scale - want) <= want * 1e-6
            if exact:                       # 1:1 on device pixels: keep it sharp
                sx, sy = surface_origin(self)
                x, y = round((sx + x) * ds) / ds - sx, round((sy + y) * ds) / ds - sy
                w, h = tex.get_width() / ds, tex.get_height() / ds
            snapshot.append_texture(tex, r.init(x, y, w, h))
            covers = x <= 0.5 and y <= 0.5 and x + w >= vw - 0.5 and y + h >= vh - 0.5
            if exact and covers and style == self._style():
                return
        self._request_raster()

    def _request_raster(self):
        if self._busy:
            self._want = True
            return
        vw, vh = self.get_width(), self.get_height()
        if self.scene is None or vw <= 0 or vh <= 0:
            return
        ds = device_scale(self)
        z = self.eff_zoom()
        cx, cy = self.center()
        scale = z * ds
        m = RASTER_MARGIN
        pw, ph = math.ceil(vw * (1 + 2 * m) * ds), math.ceil(vh * (1 + 2 * m) * ds)
        if pw * ph > RASTER_MAX_PX:
            m = 0.0
            pw, ph = math.ceil(vw * ds), math.ceil(vh * ds)
        rx0 = cx - vw * (0.5 + m) / z
        ry0 = cy - vh * (0.5 + m) / z
        style = self._style()
        scene, gen = self.scene, self._gen
        self._busy, self._want = True, False

        def work():
            try:
                tex = rasterize(scene, scale, rx0, ry0, pw, ph, *style, max(1.0, ds * 0.9))
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                tex = None
            GLib.idle_add(done, tex)

        def done(tex):
            self._busy = False
            if gen != self._gen:
                return False
            if tex is not None:
                self.raster = (tex, scale, rx0, ry0, style)
            self.queue_draw()
            return False
        threading.Thread(target=work, name="dxf-raster", daemon=True).start()


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------
class DxfViewer(Viewer):
    def __init__(self, window):
        super().__init__(window)
        self.gfile = None
        self.scene = None
        self.compact = False
        self._alive = True
        self._cancel = threading.Event()
        self.layer_checks = {}

        self.canvas = DxfCanvas()
        self.canvas.connect("view-changed", lambda *_: self._on_view_changed())
        self.spinner = Adw.Spinner(width_request=48, height_request=48,
                                   halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.empty = Gtk.Label(label=_("This drawing has nothing to show"),
                               css_classes=["dim-label", "title-3"], visible=False,
                               halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.osd = Gtk.Label(css_classes=["preview-osd"], halign=Gtk.Align.CENTER,
                             valign=Gtk.Align.END, margin_bottom=24, visible=False,
                             can_target=False)
        self._osd_timer = 0
        self._last_pct = None
        ov = Gtk.Overlay(child=self.canvas, hexpand=True, vexpand=True)
        for w in (self.spinner, self.empty, self.osd):
            ov.add_overlay(w)
        self.append(ov)

        self._build_actions()
        sm = Adw.StyleManager.get_default()
        self.canvas.set_style(dark=sm.get_dark())
        ref = weakref.ref(self)

        def on_dark(m, *_):
            v = ref()
            if v is not None and v._alive:
                v.canvas.set_style(dark=m.get_dark())
        self._dark_id = sm.connect("notify::dark", on_dark)
        self.connect("destroy", lambda *_: self.release())

    # ---- actions ------------------------------------------------------------
    def _build_actions(self):
        g = Gio.SimpleActionGroup()
        self.actions = g
        for name, cb in (("zoom-in", lambda: self.canvas.zoom_by(1.25)),
                         ("zoom-out", lambda: self.canvas.zoom_by(0.8)),
                         ("zoom-fit", lambda: self.canvas.zoom_fit()),
                         ("export", lambda: self.export_dialog())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            g.add_action(a)
        lw = Gio.SimpleAction.new_stateful("lineweights", None, GLib.Variant("b", True))

        def on_lw(action, value):
            action.set_state(value)
            self.canvas.set_style(lineweights=value.get_boolean())
        lw.connect("change-state", on_lw)
        g.add_action(lw)
        # the header bar is outside this widget: put the actions on the window
        host = self.window if isinstance(self.window, Gtk.Widget) else self
        host.insert_action_group("dxf", g)

        sc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.GLOBAL)
        for keys, action in ((("plus", "equal", "KP_Add", "<Control>plus", "<Control>equal"),
                              "dxf.zoom-in"),
                             (("minus", "KP_Subtract", "<Control>minus"), "dxf.zoom-out"),
                             (("0", "KP_0", "<Control>0"), "dxf.zoom-fit"),
                             (("<Control><Shift>e",), "dxf.export")):
            for k in keys:
                sc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(k),
                                             action=Gtk.NamedAction.new(action)))
        self.add_controller(sc)
        self._enable(False)

    def _enable(self, on):
        for name in ("zoom-in", "zoom-out", "zoom-fit", "export"):
            self.actions.lookup_action(name).set_enabled(on)

    def build_header(self, header):
        if self.compact:
            return
        zoom = Gtk.Box(css_classes=["linked"])
        zoom.append(Gtk.Button(icon_name="zoom-out-symbolic", action_name="dxf.zoom-out",
                               tooltip_text=_("Zoom Out (−)")))
        zoom.append(Gtk.Button(icon_name="zoom-fit-best-symbolic", action_name="dxf.zoom-fit",
                               tooltip_text=_("Fit to Window (0)")))
        zoom.append(Gtk.Button(icon_name="zoom-in-symbolic", action_name="dxf.zoom-in",
                               tooltip_text=_("Zoom In (+)")))
        header.pack_start(zoom)
        self.layers_btn = Gtk.MenuButton(icon_name="view-list-symbolic", tooltip_text=_("Layers"),
                                         sensitive=False)
        header.pack_start(self.layers_btn)
        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append(_("Show Lineweights"), "dxf.lineweights")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append(_("Export As…"), "dxf.export")
        menu.append_section(None, sec)
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu,
                                       tooltip_text=_("Drawing Options")))

    def set_compact(self, compact=True):
        """Quick Look: no header chrome."""
        self.compact = bool(compact)

    # ---- Viewer API -----------------------------------------------------------
    def get_current_file(self):
        return self.gfile

    def get_title(self):
        return (self.gfile.get_basename() if self.gfile is not None else None) or _("Drawing")

    def get_subtitle(self):
        s = self.scene
        if s is None:
            return _("Loading…") if self.gfile is not None else ""
        if s.extents is None:
            return _("Empty drawing")
        x0, y0, x1, y1 = s.extents
        unit = UNITS.get(s.units, "")
        size = f"{_num(x1 - x0)} × {_num(y1 - y0)}" + (f" {unit}" if unit else "")
        n = len(s.layers)
        return size + " · " + ngettext("{n} layer", "{n} layers", n).format(n=n)

    def get_info(self):
        sections = [file_info_section(self.gfile)] if self.gfile is not None else []
        if self.scene is not None:
            sections.append((_("Drawing"), list(self.scene.info)))
        return sections

    def release(self):
        if not self._alive:
            return
        self._alive = False
        self._cancel.set()
        self.canvas.release()
        if self._osd_timer:
            GLib.source_remove(self._osd_timer)
            self._osd_timer = 0
        try:
            Adw.StyleManager.get_default().disconnect(self._dark_id)
        except Exception:  # noqa: BLE001
            pass

    # ---- loading ----------------------------------------------------------------
    def load(self, files):
        gf = files[0]
        self.gfile = gf
        path = gf.get_path()
        if path is None:
            raise OSError(_("Only local files are supported"))
        cancel = self._cancel

        def work():
            try:
                scene, err = build_scene(path, cancel), None
            except Cancelled:
                return
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                scene, err = None, e
            GLib.idle_add(self._loaded, scene, err)
        threading.Thread(target=work, name="dxf-load", daemon=True).start()
        self.refresh_title()

    def _loaded(self, scene, err):
        if not self._alive:
            return False
        self.spinner.set_visible(False)
        if err is not None:
            show = getattr(self.window, "_show_error", None)       # not in Quick Look
            if show is not None:
                show(err)
            else:
                self.empty.set_label(_("Could not open this drawing"))
                self.empty.set_visible(True)
            return False
        self.scene = scene
        hidden = {key for key, _n, _c, vis, _cnt in scene.layers if not vis}
        self.canvas.set_style(hidden=hidden)
        self.canvas.set_scene(scene)
        self.empty.set_visible(scene.is_empty())
        self._enable(not scene.is_empty())
        if hasattr(self, "layers_btn"):
            self.layers_btn.set_popover(self._layers_popover(scene))
            self.layers_btn.set_sensitive(bool(scene.layers))
        self.refresh_title()
        if not self.compact:
            self.canvas.grab_focus()
        return False

    # ---- layers ---------------------------------------------------------------------
    def _layers_popover(self, scene):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top = Gtk.Box(spacing=6)
        top.append(Gtk.Label(label=_("Layers"), css_classes=["heading"], hexpand=True, xalign=0))
        show = Gtk.Button(label=_("Show All"), css_classes=["flat"])
        hide = Gtk.Button(label=_("Hide All"), css_classes=["flat"])
        top.append(show)
        top.append(hide)
        box.append(top)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.layer_checks = {}
        for key, name, rgb, vis, count in scene.layers:
            row = Gtk.Box(spacing=8, margin_start=6, margin_end=8, margin_top=2, margin_bottom=2)
            check = Gtk.CheckButton(active=vis)
            sw = Gtk.DrawingArea(content_width=12, content_height=12, valign=Gtk.Align.CENTER)
            sw.set_draw_func(self._draw_swatch, rgb)
            label = Gtk.Label(label=name, xalign=0, hexpand=True,
                              ellipsize=Pango.EllipsizeMode.END, max_width_chars=28)
            if not count:
                label.add_css_class("dim-label")
            label.set_tooltip_text(ngettext("{n:,} object", "{n:,} objects", count).format(n=count))
            row.append(check)
            row.append(sw)
            row.append(label)
            check.connect("toggled", lambda *_: self._layers_changed())
            self.layer_checks[key] = check
            lb.append(row)
        scroller = Gtk.ScrolledWindow(child=lb, propagate_natural_height=True,
                                      max_content_height=440, min_content_width=260,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        box.append(scroller)

        def set_all(on):
            self._bulk = True
            for c in self.layer_checks.values():
                c.set_active(on)
            self._bulk = False
            self._layers_changed()
        show.connect("clicked", lambda *_: set_all(True))
        hide.connect("clicked", lambda *_: set_all(False))
        self._bulk = False
        return Gtk.Popover(child=box)

    @staticmethod
    def _draw_swatch(_area, cr, w, h, rgb):
        cr.set_source_rgb(*adapt_color(rgb, Adw.StyleManager.get_default().get_dark()))
        cr.rectangle(0.5, 0.5, w - 1, h - 1)
        cr.fill_preserve()
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.8)
        cr.set_line_width(1)
        cr.stroke()

    def _layers_changed(self):
        if getattr(self, "_bulk", False):
            return
        self.canvas.set_style(hidden={k for k, c in self.layer_checks.items()
                                      if not c.get_active()})

    # ---- zoom OSD -----------------------------------------------------------------------
    def _on_view_changed(self):
        if self.scene is None or self.scene.is_empty():
            return
        pct = round(self.canvas.zoom_percent())
        if self._last_pct is not None and pct != self._last_pct:
            self.osd.set_text(f"{pct}%")
            self.osd.set_visible(True)
            if self._osd_timer:
                GLib.source_remove(self._osd_timer)

            def hide():
                self._osd_timer = 0
                self.osd.set_visible(False)
                return False
            self._osd_timer = GLib.timeout_add(1100, hide)
        self._last_pct = pct

    # ---- export ----------------------------------------------------------------------------
    def export_dialog(self):
        if self.scene is None or self.scene.is_empty():
            return
        dlg = Gtk.FileDialog(title=_("Export Drawing"))
        stem = os.path.splitext(self.gfile.get_basename() or "drawing")[0]
        dlg.set_initial_name(stem + ".pdf")
        if self.gfile.get_parent() is not None:
            dlg.set_initial_folder(self.gfile.get_parent())
        filters = Gio.ListStore.new(Gtk.FileFilter)
        for label, suffix in ((_("PDF Document"), "pdf"), (_("SVG Image"), "svg"),
                              (_("PNG Image"), "png")):
            f = Gtk.FileFilter(name=label)
            f.add_suffix(suffix)
            filters.append(f)
        dlg.set_filters(filters)

        def done(d, res):
            try:
                gf = d.save_finish(res)
            except GLib.Error:
                return
            path = gf.get_path()
            if path is None:
                self.toast(_("Only local files are supported"))
                return
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            if ext not in ("pdf", "svg", "png"):
                ext = "pdf"
                path += ".pdf"
            self._export(path, ext)
        dlg.save(self.get_root(), None, done)

    def _export(self, path, fmt):
        scene, hidden, lw = self.scene, self.canvas.hidden, self.canvas.lineweights

        def work():
            try:
                export(scene, path, fmt, hidden, lw)
                err = None
            except Exception as e:  # noqa: BLE001
                err = e
            GLib.idle_add(finished, err)

        def finished(err):
            if self._alive:
                if err is None:
                    self.toast(_("Exported “{name}”").format(name=os.path.basename(path)))
                else:
                    self.toast(_("Could not export: {error}").format(error=err))
            return False
        threading.Thread(target=work, name="dxf-export", daemon=True).start()
