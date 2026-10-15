"""Host-agnostic markup layer: shapes, ink, text boxes, notes, text markup and
signatures, like macOS Preview's Markup toolbar.

Model
    Items live in a "page" coordinate system chosen by the host: user units,
    origin top-left, y down (the PDF viewer uses display-frame points; an image
    viewer would use image pixels). Items are plain dataclasses and round-trip
    through JSON (to_dict / from_dict).

Rendering
    render_item(cr, item) draws in the cairo context's current user space, so
    the same code paints on screen, onto a PDFSurface (vector) or an
    ImageSurface.

Interaction
    MarkupController implements the tools on top of a host object that maps
    widget coordinates to (page_key, x, y). The host forwards its pointer
    events and calls controller.draw(cr, key, scale) when painting a page.
    Host protocol (methods with an mk_ prefix):
        mk_page_at(wx, wy) -> (key, x, y) | None     page under a widget point
        mk_to_page(key, wx, wy) -> (x, y)
        mk_to_widget(key, x, y) -> (wx, wy)
        mk_scale(key) -> float                       widget px per page unit
        mk_items(key) -> list[Item]                  mutable list for that page
        mk_page_size(key) -> (w, h)
        mk_keys() -> iterable of keys (topmost last) to hit-test
        mk_begin_edit()                              snapshot for undo
        mk_changed()                                 items changed: redraw + dirty
        mk_widget() -> Gtk.Widget                    parent for popovers, cursor
        mk_visible_rect(key) -> (x, y, w, h)         visible part of the page
      optional:
        mk_text_quads(key, x0, y0, x1, y1) -> (text, [(x, y, w, h), ...])
        mk_tool_changed(tool)
"""
from __future__ import annotations

import copy
import itertools
import math
import time
import sys
from dataclasses import asdict, dataclass, field

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango, PangoCairo

_ids = itertools.count(1)

BOX_KINDS = {"rect", "rrect", "oval", "bubble", "star", "text", "sig", "note"}
LINE_KINDS = {"line", "arrow"}
INK_KINDS = {"sketch", "draw"}
TEXTMARK_KINDS = {"highlight", "underline", "strike"}
ANNOT_KINDS = {"note"} | TEXTMARK_KINDS          # saved as real PDF annotations
SHAPES = ["line", "arrow", "rect", "rrect", "oval", "bubble", "star"]
NOTE_SIZE = 20.0
NOTE_COLOR = (1.0, 0.85, 0.25, 1.0)
HIGHLIGHT_COLOR = (1.0, 0.86, 0.1, 1.0)

TOOL_NAMES = {
    "select": "Select", "sketch": "Sketch", "draw": "Draw", "line": "Line",
    "arrow": "Arrow", "rect": "Rectangle", "rrect": "Rounded Rectangle",
    "oval": "Oval", "bubble": "Speech Bubble", "star": "Star", "text": "Text",
    "note": "Note", "highlight": "Highlight", "underline": "Underline",
    "strike": "Strikethrough",
}


@dataclass
class Item:
    kind: str
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    # line/arrow: [[x1, y1], [x2, y2]]; sketch/draw: strokes [[[x, y, t], ...], ...];
    # highlight/underline/strike: quads [[x, y, w, h], ...]
    points: list = field(default_factory=list)
    stroke: tuple = (0.84, 0.16, 0.16, 1.0)
    fill: tuple | None = None
    width: float = 2.0
    text: str = ""
    font: str = "Sans"
    size: float = 14.0
    sig: dict | None = None
    id: int = field(default_factory=lambda: next(_ids))

    def copy(self) -> "Item":
        c = copy.copy(self)
        c.points = copy.deepcopy(self.points)
        return c

    def fingerprint(self):
        return (self.kind, round(self.x, 2), round(self.y, 2), round(self.w, 2),
                round(self.h, 2), repr(self.points)[:2000], self.stroke, self.fill,
                self.width, self.text, self.font, self.size, id(self.sig))

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("id", None)
        if self.sig is not None:
            d["sig"] = {k: v for k, v in self.sig.items() if k != "path"}
        return d

    @classmethod
    def from_dict(cls, d) -> "Item":
        d = dict(d)
        for k in ("stroke", "fill"):
            if isinstance(d.get(k), list):
                d[k] = tuple(d[k])
        d.pop("id", None)
        return cls(**d)


# ============================================================ geometry ===

def bbox(it: Item):
    """(x, y, w, h) of an item."""
    if it.kind in LINE_KINDS and len(it.points) >= 2:
        (x1, y1), (x2, y2) = it.points[0][:2], it.points[1][:2]
        return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)
    if it.kind in INK_KINDS:
        pts = [p for s in it.points for p in s]
        if not pts:
            return it.x, it.y, 0, 0
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
    if it.kind in TEXTMARK_KINDS:
        if not it.points:
            return it.x, it.y, 0, 0
        x0 = min(q[0] for q in it.points)
        y0 = min(q[1] for q in it.points)
        x1 = max(q[0] + q[2] for q in it.points)
        y1 = max(q[1] + q[3] for q in it.points)
        return x0, y0, x1 - x0, y1 - y0
    return it.x, it.y, it.w, it.h


def translate(it: Item, dx, dy):
    if it.kind in LINE_KINDS:
        it.points = [[p[0] + dx, p[1] + dy] for p in it.points]
    elif it.kind in INK_KINDS:
        it.points = [[[p[0] + dx, p[1] + dy] + list(p[2:]) for p in s] for s in it.points]
    elif it.kind in TEXTMARK_KINDS:
        it.points = [[q[0] + dx, q[1] + dy, q[2], q[3]] for q in it.points]
    it.x += dx
    it.y += dy


def set_bbox(it: Item, nx, ny, nw, nh):
    """Scale an item's geometry into a new bounding box."""
    ox, oy, ow, oh = bbox(it)
    sx = nw / ow if ow > 1e-6 else 1.0
    sy = nh / oh if oh > 1e-6 else 1.0
    if it.kind in LINE_KINDS:
        it.points = [[nx + (p[0] - ox) * sx, ny + (p[1] - oy) * sy] for p in it.points]
    elif it.kind in INK_KINDS:
        it.points = [[[nx + (p[0] - ox) * sx, ny + (p[1] - oy) * sy] + list(p[2:]) for p in s]
                     for s in it.points]
    elif it.kind in TEXTMARK_KINDS:
        return
    it.x, it.y, it.w, it.h = nx, ny, nw, nh
    if it.kind == "text":
        it.h = text_height(it)
    elif it.kind == "sig":
        it.h = it.w * sig_aspect(it.sig)


def rotate_point(x, y, w, h, turns):
    for _ in range(turns % 4):
        x, y, w, h = h - y, x, h, w
    return x, y, w, h


def rotate_item(it: Item, w, h, turns):
    """Follow the page content when a w×h page turns clockwise by quarter turns.
    Text, signatures and notes stay upright; their centre moves."""
    turns %= 4
    if not turns:
        return
    nw, nh = (h, w) if turns % 2 else (w, h)

    def rp(px, py):
        return rotate_point(px, py, w, h, turns)[:2]

    if it.kind in LINE_KINDS:
        it.points = [list(rp(p[0], p[1])) for p in it.points]
    elif it.kind in INK_KINDS:
        it.points = [[list(rp(p[0], p[1])) + list(p[2:]) for p in s] for s in it.points]
    elif it.kind in TEXTMARK_KINDS:
        quads = []
        for x, y, qw, qh in it.points:
            a, b = rp(x, y), rp(x + qw, y + qh)
            quads.append([min(a[0], b[0]), min(a[1], b[1]), abs(a[0] - b[0]), abs(a[1] - b[1])])
        it.points = quads
    elif it.kind in ("text", "sig", "note"):
        cx, cy = rp(it.x + it.w / 2, it.y + it.h / 2)
        it.x = min(max(cx - it.w / 2, 0), max(nw - it.w, 0))
        it.y = min(max(cy - it.h / 2, 0), max(nh - it.h, 0))
    else:
        a, b = rp(it.x, it.y), rp(it.x + it.w, it.y + it.h)
        it.x, it.y = min(a[0], b[0]), min(a[1], b[1])
        it.w, it.h = abs(a[0] - b[0]), abs(a[1] - b[1])


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def hit(it: Item, x, y, tol):
    """Does page point (x, y) touch the item (tol in page units)?"""
    if it.kind in LINE_KINDS and len(it.points) >= 2:
        (ax, ay), (bx, by) = it.points[0][:2], it.points[1][:2]
        return _seg_dist(x, y, ax, ay, bx, by) <= tol + it.width / 2
    if it.kind in INK_KINDS:
        for s in it.points:
            if len(s) == 1 and math.hypot(x - s[0][0], y - s[0][1]) <= tol + it.width:
                return True
            for a, b in zip(s, s[1:]):
                if _seg_dist(x, y, a[0], a[1], b[0], b[1]) <= tol + it.width:
                    return True
        return False
    if it.kind in TEXTMARK_KINDS:
        return any(q[0] - tol <= x <= q[0] + q[2] + tol and q[1] - tol <= y <= q[1] + q[3] + tol
                   for q in it.points)
    bx, by, bw, bh = bbox(it)
    return bx - tol <= x <= bx + bw + tol and by - tol <= y <= by + bh + tol


def handles(it: Item):
    """Resize handles: list of (name, x, y) in page units."""
    if it.kind in LINE_KINDS and len(it.points) >= 2:
        return [("p0", *it.points[0][:2]), ("p1", *it.points[1][:2])]
    if it.kind in ANNOT_KINDS:
        return []
    x, y, w, h = bbox(it)
    if it.kind == "sig":
        return [("nw", x, y), ("ne", x + w, y), ("sw", x, y + h), ("se", x + w, y + h)]
    if it.kind == "text":
        return [("w", x, y + h / 2), ("e", x + w, y + h / 2)]
    return [("nw", x, y), ("n", x + w / 2, y), ("ne", x + w, y), ("e", x + w, y + h / 2),
            ("se", x + w, y + h), ("s", x + w / 2, y + h), ("sw", x, y + h), ("w", x, y + h / 2)]


HANDLE_CURSORS = {"nw": "nwse-resize", "se": "nwse-resize", "ne": "nesw-resize",
                  "sw": "nesw-resize", "n": "ns-resize", "s": "ns-resize",
                  "e": "ew-resize", "w": "ew-resize", "p0": "crosshair", "p1": "crosshair"}


# ============================================================ rendering ===

def _set_rgba(cr, c):
    cr.set_source_rgba(c[0], c[1], c[2], c[3] if len(c) > 3 else 1.0)


def sig_aspect(sig):
    if not sig:
        return 0.3
    try:
        from .signature import signature_aspect
        return signature_aspect(sig)
    except Exception:
        return 0.3


def _layout(cr, it: Item):
    layout = PangoCairo.create_layout(cr)
    fo = cairo.FontOptions()
    fo.set_hint_metrics(cairo.HINT_METRICS_OFF)
    fo.set_hint_style(cairo.HINT_STYLE_NONE)
    PangoCairo.context_set_font_options(layout.get_context(), fo)
    layout.context_changed()
    fd = Pango.FontDescription.from_string(it.font or "Sans")
    fd.set_absolute_size(max(it.size, 1) * Pango.SCALE)
    layout.set_font_description(fd)
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_width(int(max(it.w - 4, 4) * Pango.SCALE))
    layout.set_text(it.text or "", -1)
    return layout


def text_extent(text, font="Sans", size=14.0):
    """Natural (unwrapped) size of a text string."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    it = Item("text", text=text, font=font, size=size, w=100000)
    layout = _layout(cairo.Context(surf), it)
    _ink, log = layout.get_extents()
    return log.width / Pango.SCALE + 4, log.height / Pango.SCALE + 4


def text_height(it: Item):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    layout = _layout(cairo.Context(surf), it)
    _ink, log = layout.get_extents()
    return max(log.height / Pango.SCALE, it.size * 1.2) + 4


def _smooth_path(cr, pts):
    """Quadratic-midpoint smoothing through points."""
    if len(pts) == 1:
        cr.move_to(pts[0][0], pts[0][1])
        cr.line_to(pts[0][0] + 0.01, pts[0][1])
        return
    cr.move_to(pts[0][0], pts[0][1])
    if len(pts) == 2:
        cr.line_to(pts[1][0], pts[1][1])
        return
    for i in range(1, len(pts) - 1):
        cx, cy = pts[i][0], pts[i][1]
        mx, my = (pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2
        x0, y0 = cr.get_current_point()
        cr.curve_to(x0 + 2 / 3 * (cx - x0), y0 + 2 / 3 * (cy - y0),
                    mx + 2 / 3 * (cx - mx), my + 2 / 3 * (cy - my), mx, my)
    cr.line_to(pts[-1][0], pts[-1][1])


def _shape_path(cr, it: Item):
    x, y, w, h = it.x, it.y, it.w, it.h
    k = it.kind
    if k == "rect":
        cr.rectangle(x, y, w, h)
    elif k in ("rrect", "bubble"):
        bh = h * 0.78 if k == "bubble" else h
        r = min(w, bh) * 0.18
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + bh - r, r, 0, math.pi / 2)
        if k == "bubble":
            cr.line_to(x + w * 0.38, y + bh)
            cr.line_to(x + w * 0.16, y + h)
            cr.line_to(x + w * 0.24, y + bh)
        cr.arc(x + r, y + bh - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()
    elif k == "oval":
        if w > 0 and h > 0:
            cr.save()
            cr.translate(x + w / 2, y + h / 2)
            cr.scale(w / 2, h / 2)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
    elif k == "star":
        cx, cy = x + w / 2, y + h / 2
        for i in range(10):
            r = 0.5 if i % 2 == 0 else 0.2
            a = -math.pi / 2 + i * math.pi / 5
            px, py = cx + math.cos(a) * w * r, cy + math.sin(a) * h * r * 1.05 + h * 0.03
            (cr.move_to if i == 0 else cr.line_to)(px, py)
        cr.close_path()


def _draw_note_icon(cr, x, y, s, color=NOTE_COLOR):
    f = s * 0.3
    cr.save()
    cr.move_to(x, y)
    cr.line_to(x + s, y)
    cr.line_to(x + s, y + s - f)
    cr.line_to(x + s - f, y + s)
    cr.line_to(x, y + s)
    cr.close_path()
    _set_rgba(cr, color)
    cr.fill_preserve()
    cr.set_source_rgba(0.45, 0.35, 0.0, 1)
    cr.set_line_width(s * 0.05)
    cr.stroke()
    cr.move_to(x + s - f, y + s)
    cr.line_to(x + s - f, y + s - f)
    cr.line_to(x + s, y + s - f)
    cr.stroke()
    for i in range(3):
        yy = y + s * (0.28 + 0.2 * i)
        cr.move_to(x + s * 0.2, yy)
        cr.line_to(x + s * (0.8 if i < 2 else 0.55), yy)
    cr.set_line_width(s * 0.06)
    cr.stroke()
    cr.restore()


def render_item(cr, it: Item):
    """Draw one item in the context's current user space (= page units)."""
    k = it.kind
    cr.save()
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    if k in ("rect", "rrect", "oval", "bubble", "star"):
        _shape_path(cr, it)
        if it.fill is not None:
            _set_rgba(cr, it.fill)
            cr.fill_preserve()
        if it.width > 0:
            _set_rgba(cr, it.stroke)
            cr.set_line_width(it.width)
            cr.stroke()
        cr.new_path()
    elif k in LINE_KINDS and len(it.points) >= 2:
        (x1, y1), (x2, y2) = it.points[0][:2], it.points[1][:2]
        _set_rgba(cr, it.stroke)
        cr.set_line_width(it.width)
        if k == "arrow":
            L = math.hypot(x2 - x1, y2 - y1)
            head = max(6.0, it.width * 4)
            if L > 1e-6:
                ux, uy = (x2 - x1) / L, (y2 - y1) / L
                bx, by = x2 - ux * head * 0.9, y2 - uy * head * 0.9
                cr.move_to(x1, y1)
                cr.line_to(bx, by)
                cr.stroke()
                px, py = -uy * head * 0.5, ux * head * 0.5
                cr.move_to(x2, y2)
                cr.line_to(x2 - ux * head + px, y2 - uy * head + py)
                cr.line_to(x2 - ux * head - px, y2 - uy * head - py)
                cr.close_path()
                cr.fill()
        else:
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()
    elif k in INK_KINDS:
        if k == "draw" and any(len(p) > 2 for s in it.points for p in s):
            try:
                from .signature import render_strokes
                hexc = "#%02x%02x%02x" % tuple(int(c * 255) for c in it.stroke[:3])
                render_strokes(cr, [[tuple(p[:3]) if len(p) > 2 else (p[0], p[1], 0)
                                     for p in s] for s in it.points], hexc, it.width)
            except Exception:
                k = "sketch"
        if k == "sketch" or not any(len(p) > 2 for s in it.points for p in s):
            _set_rgba(cr, it.stroke)
            cr.set_line_width(it.width)
            for s in it.points:
                if s:
                    _smooth_path(cr, s)
            cr.stroke()
    elif k == "text":
        if it.fill is not None:
            cr.rectangle(it.x, it.y, it.w, it.h)
            _set_rgba(cr, it.fill)
            cr.fill()
        layout = _layout(cr, it)
        _set_rgba(cr, it.stroke)
        cr.move_to(it.x + 2, it.y + 2)
        PangoCairo.show_layout(cr, layout)
    elif k == "note":
        _draw_note_icon(cr, it.x, it.y, it.w, it.stroke if it.stroke else NOTE_COLOR)
    elif k in TEXTMARK_KINDS:
        c = it.stroke
        for qx, qy, qw, qh in it.points:
            if k == "highlight":
                cr.set_operator(cairo.OPERATOR_MULTIPLY)
                cr.set_source_rgba(c[0], c[1], c[2], 0.55)
                cr.rectangle(qx, qy, qw, qh)
                cr.fill()
                cr.set_operator(cairo.OPERATOR_OVER)
            else:
                lw = max(qh * 0.07, 0.6)
                yy = qy + qh - lw * 1.2 if k == "underline" else qy + qh * 0.55
                _set_rgba(cr, c)
                cr.set_line_width(lw)
                cr.set_line_cap(cairo.LINE_CAP_BUTT)
                cr.move_to(qx, yy)
                cr.line_to(qx + qw, yy)
                cr.stroke()
    elif k == "sig" and it.sig:
        from .signature import render_signature
        render_signature(cr, it.sig, it.x, it.y, it.w)
    cr.restore()


def render_items(cr, items, kinds=None):
    for it in items:
        if kinds is None or it.kind in kinds:
            render_item(cr, it)


# ============================================================== shapes ===

def recognise(points):
    """Preview-style Sketch: turn a nearly-straight stroke into a line and a
    closed stroke into a rectangle or an oval. Returns an Item or None."""
    if len(points) < 6:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    w, h = x1 - x0, y1 - y0
    diag = math.hypot(w, h)
    if diag < 10:
        return None
    a, b = points[0], points[-1]
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    if L > 0.85 * diag:
        dev = max(_seg_dist(p[0], p[1], a[0], a[1], b[0], b[1]) for p in points)
        if dev < 0.05 * L:
            return Item("line", points=[[a[0], a[1]], [b[0], b[1]]])
        return None
    if L > 0.2 * diag or min(w, h) < 0.15 * max(w, h):
        return None
    # closed: compare the stroke's area with its bounding box
    area = 0.0
    for p, q in zip(points, points[1:] + points[:1]):
        area += p[0] * q[1] - q[0] * p[1]
    ratio = abs(area) / 2 / max(w * h, 1e-6)
    # how far the points stay from the box edges (rectangles hug them)
    edge = sum(min(p[0] - x0, x1 - p[0], p[1] - y0, y1 - p[1]) for p in points) / len(points)
    if ratio > 0.88 or edge < 0.04 * min(w, h):
        return Item("rect", x0, y0, w, h)
    if 0.65 < ratio <= 0.88:
        return Item("oval", x0, y0, w, h)
    return None


# ============================================================ clipboard ===

_CLIPBOARD: list = []


# =========================================================== controller ===

class MarkupController(GObject.Object):
    __gsignals__ = {
        "tool-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    STICKY_TOOLS = {"sketch", "draw", "highlight", "underline", "strike"}

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.tool = "select"
        self.stroke = (0.84, 0.16, 0.16, 1.0)
        self.fill = None
        self.width = 2.0
        self.font = "Sans"
        self.size = 14.0
        self.highlight_color = HIGHLIGHT_COLOR
        self.selected = None            # (key, item id)
        self.enabled = True
        self._op = None
        self._preview = None            # (key, Item) being created
        self.pad = None                 # TouchpadDrawing session
        self._popover = None

    # ---------------------------------------------------------- state ----
    def set_tool(self, tool):
        if tool == self.tool:
            return
        self.tool = tool
        if tool != "select":
            self.select(None)
        self.emit("tool-changed", tool)
        cb = getattr(self.host, "mk_tool_changed", None)
        if cb:
            cb(tool)

    def select(self, sel):
        if sel != self.selected:
            self.selected = sel
            self.emit("selection-changed")
            self.host.mk_widget().queue_draw()

    def selected_item(self):
        if not self.selected:
            return None, None
        key, iid = self.selected
        for it in self.host.mk_items(key):
            if it.id == iid:
                return key, it
        return None, None

    def set_style(self, **kw):
        """Change the current style; also applies to the selected item."""
        for k, v in kw.items():
            setattr(self, k, v)
        key, it = self.selected_item()
        if it is None:
            return
        self.host.mk_begin_edit()
        if "stroke" in kw:
            if it.kind in TEXTMARK_KINDS or it.kind not in ("sig",):
                it.stroke = kw["stroke"]
        if "fill" in kw and it.kind in ("rect", "rrect", "oval", "bubble", "star", "text"):
            it.fill = kw["fill"]
        if "width" in kw and it.kind not in ("text", "note", "sig") | TEXTMARK_KINDS:
            it.width = kw["width"]
        if ("font" in kw or "size" in kw) and it.kind == "text":
            it.font, it.size = self.font, self.size
            it.h = text_height(it)
        self.host.mk_changed()

    # ------------------------------------------------------- hit test ----
    def _tol(self, key):
        return 5.0 / max(self.host.mk_scale(key), 1e-6)

    def item_at(self, wx, wy):
        """Topmost item under a widget point -> (key, item) or (None, None)."""
        for key in reversed(list(self.host.mk_keys())):
            x, y = self.host.mk_to_page(key, wx, wy)
            tol = self._tol(key)
            for it in reversed(self.host.mk_items(key)):
                if hit(it, x, y, tol):
                    return key, it
        return None, None

    def handle_at(self, wx, wy):
        key, it = self.selected_item()
        if it is None:
            return None
        r = 7.0
        for name, hx, hy in handles(it):
            px, py = self.host.mk_to_widget(key, hx, hy)
            if math.hypot(px - wx, py - wy) <= r + 2:
                return name
        # delete button
        bx, by, bw, bh = bbox(it)
        px, py = self.host.mk_to_widget(key, bx, by)
        if math.hypot(px - 12 - wx, py - 12 - wy) <= 9:
            return "delete"
        return None

    def cursor_at(self, wx, wy):
        if not self.enabled:
            return None
        if self.tool == "select":
            h = self.handle_at(wx, wy)
            if h == "delete":
                return "pointer"
            if h:
                return HANDLE_CURSORS.get(h, "move")
            key, it = self.item_at(wx, wy)
            if it is not None:
                return "pointer" if it.kind == "note" else "move"
            return None
        if self.tool in TEXTMARK_KINDS:
            return "text"
        return "crosshair"

    # ---------------------------------------------------------- events ----
    def on_press(self, wx, wy, n_press=1, state=0) -> bool:
        """Begin an interaction. Returns True if the controller owns this
        press/drag; False lets the host do its own thing (scroll, select text)."""
        if not self.enabled or self.pad is not None:
            return False
        host = self.host
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if self.tool == "select":
            h = self.handle_at(wx, wy)
            key, it = self.selected_item()
            if h == "delete":
                self.delete_selected()
                self._op = {"mode": "none"}
                return True
            if h:
                self._op = {"mode": "resize", "key": key, "item": it, "handle": h,
                            "orig": it.copy(), "start": (wx, wy), "moved": False}
                return True
            key, it = self.item_at(wx, wy)
            if it is None:
                self.select(None)
                return False
            self.select((key, it.id))
            if n_press >= 2 and it.kind in ("text", "note"):
                self.edit_item(key, it)
                self._op = {"mode": "none"}
                return True
            self._op = {"mode": "move", "key": key, "item": it, "orig": it.copy(),
                        "start": (wx, wy), "moved": False}
            return True

        hitp = host.mk_page_at(wx, wy)
        if hitp is None:
            return False
        key, x, y = hitp
        if self.tool in INK_KINDS:
            t = time.monotonic()
            it = Item(self.tool, stroke=self.stroke, width=self.width,
                      points=[[[x, y, t]]])
            self._preview = (key, it)
            self._op = {"mode": "ink", "key": key, "item": it}
            return True
        if self.tool in TEXTMARK_KINDS:
            it = Item(self.tool, stroke=self.highlight_color if self.tool == "highlight"
                      else self.stroke, points=[])
            self._preview = (key, it)
            self._op = {"mode": "textmark", "key": key, "item": it, "p0": (x, y)}
            return True
        if self.tool in ("text", "note"):
            self._op = {"mode": "place", "key": key, "p0": (x, y), "start": (wx, wy)}
            return True
        # shapes
        if self.tool in LINE_KINDS:
            it = Item(self.tool, stroke=self.stroke, width=self.width,
                      points=[[x, y], [x, y]])
        else:
            it = Item(self.tool, x, y, 0, 0, stroke=self.stroke, fill=self.fill,
                      width=self.width)
        self._preview = (key, it)
        self._op = {"mode": "shape", "key": key, "item": it, "p0": (x, y),
                    "start": (wx, wy), "shift": shift}
        return True

    def on_drag(self, wx, wy, state=0):
        op = self._op
        if not op:
            return
        host = self.host
        mode = op["mode"]
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if mode in ("move", "resize"):
            key, it = op["key"], op["item"]
            sx, sy = op["start"]
            if not op["moved"]:
                if math.hypot(wx - sx, wy - sy) < 2:
                    return
                host.mk_begin_edit()
                op["moved"] = True
            s = host.mk_scale(key)
            dx, dy = (wx - sx) / s, (wy - sy) / s
            orig = op["orig"]
            pw, ph = host.mk_page_size(key)
            if mode == "move":
                if it.kind in TEXTMARK_KINDS:
                    return
                ox, oy, ow, oh = bbox(orig)
                dx = min(max(dx, -ox), pw - ox - ow)
                dy = min(max(dy, -oy), ph - oy - oh)
                restore = orig.copy()
                restore.id = it.id
                it.__dict__.update(restore.__dict__)
                translate(it, dx, dy)
            else:
                self._resize(it, orig, op["handle"], dx, dy, shift, pw, ph)
            host.mk_widget().queue_draw()
            return
        key = op["key"]
        x, y = host.mk_to_page(key, wx, wy)
        pw, ph = host.mk_page_size(key)
        x, y = min(max(x, 0), pw), min(max(y, 0), ph)
        if mode == "ink":
            s = op["item"].points[-1]
            lx, ly = s[-1][0], s[-1][1]
            if math.hypot(x - lx, y - ly) * host.mk_scale(key) >= 1.5:
                s.append([x, y, time.monotonic()])
        elif mode == "textmark":
            fn = getattr(host, "mk_text_quads", None)
            if fn:
                x0, y0 = op["p0"]
                text, quads = fn(key, x0, y0, x, y)
                op["item"].points = [list(q) for q in quads]
                op["item"].text = text or ""
        elif mode == "shape":
            it = op["item"]
            x0, y0 = op["p0"]
            if it.kind in LINE_KINDS:
                if shift:     # snap to 45°
                    a = round(math.atan2(y - y0, x - x0) / (math.pi / 4)) * math.pi / 4
                    L = math.hypot(x - x0, y - y0)
                    x, y = x0 + math.cos(a) * L, y0 + math.sin(a) * L
                it.points = [[x0, y0], [x, y]]
            else:
                w, h = x - x0, y - y0
                if shift:
                    m = max(abs(w), abs(h))
                    w, h = math.copysign(m, w), math.copysign(m, h)
                it.x, it.y = min(x0, x0 + w), min(y0, y0 + h)
                it.w, it.h = abs(w), abs(h)
        host.mk_widget().queue_draw()

    def _resize(self, it, orig, handle, dx, dy, shift, pw, ph):
        if it.kind in LINE_KINDS:
            i = 0 if handle == "p0" else 1
            pts = [list(p) for p in orig.points]
            pts[i] = [min(max(pts[i][0] + dx, 0), pw), min(max(pts[i][1] + dy, 0), ph)]
            it.points = pts
            return
        x, y, w, h = bbox(orig)
        x1, y1, x2, y2 = x, y, x + w, y + h
        if "w" in handle:
            x1 = min(x1 + dx, x2 - 4)
        if "e" in handle:
            x2 = max(x2 + dx, x1 + 4)
        if "n" in handle:
            y1 = min(y1 + dy, y2 - 4)
        if "s" in handle:
            y2 = max(y2 + dy, y1 + 4)
        nw, nh = x2 - x1, y2 - y1
        lock = it.kind == "sig" or (shift and len(handle) == 2)
        if lock and w > 0 and h > 0:
            r = max(nw / w, nh / h) if handle in ("nw", "ne", "sw", "se") else nw / w
            nw, nh = w * r, h * r
            if "w" in handle:
                x1 = x2 - nw
            if "n" in handle:
                y1 = y2 - nh
        restore = orig.copy()
        restore.id = it.id
        it.__dict__.update(restore.__dict__)
        set_bbox(it, x1, y1, nw, nh)

    def on_release(self, wx, wy):
        op, self._op = self._op, None
        self._preview = None
        if not op:
            return
        host = self.host
        mode = op["mode"]
        if mode in ("move", "resize"):
            if op["moved"]:
                host.mk_changed()
            return
        if mode == "none":
            return
        key = op["key"]
        if mode == "ink":
            it = op["item"]
            if self.tool == "sketch" and len(it.points) == 1:
                shape = recognise(it.points[0])
                if shape is not None:
                    shape.stroke, shape.width = it.stroke, it.width
                    it = shape
            self._add(key, it, keep_tool=True)
            return
        if mode == "textmark":
            it = op["item"]
            if it.points:
                self._add(key, it, keep_tool=True)
            host.mk_widget().queue_draw()
            return
        if mode == "place":
            x, y = op["p0"]
            pw, ph = host.mk_page_size(key)
            if self.tool == "note":
                it = Item("note", min(x, pw - NOTE_SIZE), min(y, ph - NOTE_SIZE),
                          NOTE_SIZE, NOTE_SIZE, stroke=NOTE_COLOR, text="")
            else:
                w = min(220.0, pw - x)
                it = Item("text", x, y, max(w, 40), 0, stroke=self.stroke,
                          fill=self.fill, text="Text", font=self.font, size=self.size)
                it.h = text_height(it)
            self._add(key, it)
            self.edit_item(key, it, new=True)
            return
        if mode == "shape":
            it = op["item"]
            sx, sy = op["start"]
            if math.hypot(wx - sx, wy - sy) < 4:      # click: default-size shape
                x0, y0 = op["p0"]
                pw, ph = host.mk_page_size(key)
                if it.kind in LINE_KINDS:
                    it.points = [[x0, y0], [min(x0 + 100, pw), y0]]
                else:
                    w, h = (80, 80) if it.kind == "star" else (100, 70)
                    it.x, it.y = min(x0, pw - w), min(y0, ph - h)
                    it.w, it.h = w, h
            self._add(key, it)

    def _add(self, key, it, keep_tool=False):
        self.host.mk_begin_edit()
        self.host.mk_items(key).append(it)
        if not keep_tool:
            self.set_tool("select")
            self.select((key, it.id))
        self.host.mk_changed()

    def add_item(self, key, it, select=True):
        """Insert an item (e.g. a signature chosen from a menu)."""
        self.host.mk_begin_edit()
        self.host.mk_items(key).append(it)
        if select:
            self.set_tool("select")
            self.select((key, it.id))
        self.host.mk_changed()

    # ------------------------------------------------------- commands ----
    def delete_selected(self) -> bool:
        key, it = self.selected_item()
        if it is None:
            return False
        self.host.mk_begin_edit()
        items = self.host.mk_items(key)
        items[:] = [i for i in items if i.id != it.id]
        self.selected = None
        self.emit("selection-changed")
        self.host.mk_changed()
        return True

    def copy_selected(self) -> bool:
        key, it = self.selected_item()
        if it is None:
            return False
        _CLIPBOARD[:] = [it.copy()]
        return True

    def cut_selected(self) -> bool:
        return self.copy_selected() and self.delete_selected()

    def paste(self, key) -> bool:
        if not _CLIPBOARD or key is None:
            return False
        it = _CLIPBOARD[0].copy()
        it.id = next(_ids)
        if it.kind not in TEXTMARK_KINDS:
            pw, ph = self.host.mk_page_size(key)
            x, y, w, h = bbox(it)
            dx = 12 if x + w + 12 <= pw else -x
            dy = 12 if y + h + 12 <= ph else -y
            translate(it, dx, dy)
            _CLIPBOARD[0] = it.copy()
        self.add_item(key, it)
        return True

    def nudge(self, dx, dy) -> bool:
        key, it = self.selected_item()
        if it is None or it.kind in TEXTMARK_KINDS:
            return False
        pw, ph = self.host.mk_page_size(key)
        x, y, w, h = bbox(it)
        dx = min(max(dx, -x), pw - x - w)
        dy = min(max(dy, -y), ph - y - h)
        self.host.mk_begin_edit()
        translate(it, dx, dy)
        self.host.mk_changed()
        return True

    def escape(self) -> bool:
        if self.pad is not None:
            self.pad.stop()
            return True
        if self.selected:
            self.select(None)
            return True
        if self.tool != "select":
            self.set_tool("select")
            return True
        return False

    # ----------------------------------------------------- text edit ----
    def edit_item(self, key, it, new=False):
        """Popover editor for a text box or a note."""
        widget = self.host.mk_widget()
        if self._popover is not None:
            self._popover.popdown()
        tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                          top_margin=6, bottom_margin=6, left_margin=6, right_margin=6)
        tv.get_buffer().set_text(it.text)
        sw = Gtk.ScrolledWindow(child=tv, min_content_width=240, min_content_height=90,
                                max_content_height=240, propagate_natural_height=True)
        hint = Gtk.Label(label="Ctrl+Enter to finish", css_classes=["dim-label", "caption"],
                         xalign=0)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        if it.kind == "note":
            box.append(Gtk.Label(label="Note", xalign=0, css_classes=["heading"]))
        box.append(sw)
        box.append(hint)
        pop = Gtk.Popover(child=box, position=Gtk.PositionType.BOTTOM)
        parent_set = getattr(self.host, "mk_set_popover", None)
        if parent_set:
            parent_set(pop)
        else:
            pop.set_parent(widget)
        x, y, w, h = bbox(it)
        wx, wy = self.host.mk_to_widget(key, x, y)
        s = self.host.mk_scale(key)
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(wx), int(wy)
        rect.width, rect.height = max(1, int(w * s)), max(1, int(h * s))
        pop.set_pointing_to(rect)
        self._popover = pop
        state = {"edited": False}
        buf = tv.get_buffer()

        def changed(b):
            if not state["edited"] and not new:
                self.host.mk_begin_edit()
            state["edited"] = True
            it.text = b.get_text(b.get_start_iter(), b.get_end_iter(), False)
            if it.kind == "text":
                it.h = text_height(it)
            widget.queue_draw()

        def on_key(ctrl, keyval, code, st):
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and st & Gdk.ModifierType.CONTROL_MASK:
                pop.popdown()
                return True
            if keyval == Gdk.KEY_Escape:
                pop.popdown()
                return True
            return False

        def closed(p):
            if self._popover is p:
                self._popover = None
            unset = getattr(self.host, "mk_set_popover", None)
            if unset:
                GLib.idle_add(lambda: unset(None, p) and False)
            else:
                GLib.idle_add(lambda: p.unparent() and False)
            if it.kind == "text" and not it.text.strip():
                items = self.host.mk_items(key)
                items[:] = [i for i in items if i.id != it.id]
                self.select(None)
            self.host.mk_changed()
            widget.grab_focus()

        buf.connect("changed", changed)
        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", on_key)
        tv.add_controller(kc)
        pop.connect("closed", closed)
        pop.popup()
        tv.grab_focus()
        if new or it.kind == "text":
            buf.select_range(buf.get_start_iter(), buf.get_end_iter())

    # ------------------------------------------------------- painting ----
    def draw(self, cr, key, scale, accent=(0.21, 0.52, 0.89)):
        """Paint the page's items, the in-progress item and the selection
        chrome. cr is in page units; scale = widget px per unit."""
        render_items(cr, self.host.mk_items(key))
        if self._preview and self._preview[0] == key:
            render_item(cr, self._preview[1])
        if self.pad is not None and self.pad.key == key and self.pad.rect:
            x, y, w, h = self.pad.rect
            cr.save()
            cr.set_source_rgba(accent[0], accent[1], accent[2], 0.08)
            cr.rectangle(x, y, w, h)
            cr.fill_preserve()
            cr.set_source_rgba(accent[0], accent[1], accent[2], 0.9)
            cr.set_line_width(1.5 / scale)
            cr.set_dash([6 / scale, 4 / scale])
            cr.stroke()
            if self.pad.finger:
                fx, fy = self.pad.finger
                cr.set_dash([])
                cr.arc(fx, fy, 5 / scale, 0, 2 * math.pi)
                cr.stroke()
            cr.restore()
        if self.selected and self.selected[0] == key:
            _k, it = self.selected_item()
            if it is not None:
                self._draw_chrome(cr, it, scale, accent)

    def _draw_chrome(self, cr, it, scale, accent):
        px = 1.0 / scale
        x, y, w, h = bbox(it)
        cr.save()
        cr.set_source_rgba(accent[0], accent[1], accent[2], 1)
        cr.set_line_width(1.5 * px)
        if it.kind not in LINE_KINDS:
            m = 3 * px
            cr.rectangle(x - m, y - m, w + 2 * m, h + 2 * m)
            cr.stroke()
        for _name, hx, hy in handles(it):
            cr.arc(hx, hy, 5 * px, 0, 2 * math.pi)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgba(accent[0], accent[1], accent[2], 1)
            cr.stroke()
        # delete badge, outside the top-left corner
        bx, by = x - 12 * px, y - 12 * px
        cr.arc(bx, by, 8 * px, 0, 2 * math.pi)
        cr.set_source_rgba(0.22, 0.22, 0.22, 0.92)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(1.6 * px)
        d = 3.2 * px
        cr.move_to(bx - d, by - d)
        cr.line_to(bx + d, by + d)
        cr.move_to(bx + d, by - d)
        cr.line_to(bx - d, by + d)
        cr.stroke()
        cr.restore()

    # ------------------------------------------------------- touchpad ----
    def start_touchpad(self, key):
        """Draw with the touchpad mapped onto the visible part of page `key`."""
        if self.pad is not None:
            self.pad.stop()
            return None
        try:
            self.pad = TouchpadDrawing(self, key)
        except Exception as e:
            self.pad = None
            return str(e)
        return None


class TouchpadDrawing:
    """One finger on the (grabbed) touchpad draws ink onto a page region.
    Any key, focus loss, or 45 s without touch ends the session."""

    MARGIN_MM = 1.5

    def __init__(self, ctrl: MarkupController, key):
        from .touchpad import Touchpad
        self.ctrl, self.key = ctrl, key
        self.host = ctrl.host
        self.pad = Touchpad(on_frame=self._on_frame, on_lost=self.stop)
        self.item = None
        self.active_id = None
        self.finger = None
        self.last_touch = time.monotonic()
        vx, vy, vw, vh = self.host.mk_visible_rect(key)
        pad_w, pad_h = self.pad.width_mm, self.pad.height_mm
        mx, my = vw * 0.04, vh * 0.04
        vx, vy, vw, vh = vx + mx, vy + my, vw - 2 * mx, vh - 2 * my
        s = min(vw / pad_w, vh / pad_h)
        self.s = s
        self.rect = (vx + (vw - pad_w * s) / 2, vy + (vh - pad_h * s) / 2, pad_w * s, pad_h * s)
        widget = self.host.mk_widget()
        self.root = widget.get_root()
        self.keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        self.keys.connect("key-pressed", self._on_key)
        self._active_h = None
        if isinstance(self.root, Gtk.Window):
            self.root.add_controller(self.keys)
            self._active_h = self.root.connect("notify::is-active", self._on_active)
        self._idle = GLib.timeout_add_seconds(1, self._check_idle)
        self.pad.grab()
        widget.queue_draw()

    def _on_frame(self, contacts, t):
        self.last_touch = time.monotonic()
        x0, y0, w, h = self.rect
        if len(contacts) == 1:
            cid, mx, my = contacts[0]
            m = self.MARGIN_MM
            mx = min(max(mx, m), self.pad.width_mm - m)
            my = min(max(my, m), self.pad.height_mm - m)
            px, py = x0 + mx * self.s, y0 + my * self.s
            self.finger = (px, py)
            if self.item is None:
                self.host.mk_begin_edit()
                self.item = Item("draw", stroke=self.ctrl.stroke,
                                 width=max(self.ctrl.width * 0.75, 0.6), points=[])
                self.host.mk_items(self.key).append(self.item)
            if cid != self.active_id:
                self.active_id = cid
                self.item.points.append([[px, py, t]])
            else:
                s = self.item.points[-1]
                lx, ly = s[-1][0], s[-1][1]
                if math.hypot(px - lx, py - ly) >= 0.12 * self.s:
                    s.append([px, py, t])
        else:
            if (len(contacts) > 1 and self.active_id is not None and self.item
                    and self.item.points):
                s = self.item.points[-1]
                if len(s) < 4 and s[-1][2] - s[0][2] < 0.12:
                    self.item.points.pop()
            self.active_id = None
            self.finger = None
        self.host.mk_widget().queue_draw()

    def _on_key(self, ctrl, keyval, code, state):
        if keyval in (Gdk.KEY_BackSpace,) and self.item and self.item.points:
            self.item.points.pop()
            self.host.mk_widget().queue_draw()
            return True
        if keyval in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
                      Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R):
            return False
        self.stop()
        return True

    def _on_active(self, win, _p):
        if not win.is_active():
            self.stop()

    def _check_idle(self):
        if time.monotonic() - self.last_touch > 45:
            self.stop()
            return False
        return True

    def stop(self):
        if self.ctrl.pad is not self:
            return
        self.ctrl.pad = None
        try:
            self.pad.release()
            self.pad.close()
        except Exception:
            pass
        if self._idle:
            GLib.source_remove(self._idle)
            self._idle = 0
        if isinstance(self.root, Gtk.Window):
            self.root.remove_controller(self.keys)
            if self._active_h:
                self.root.disconnect(self._active_h)
        items = self.host.mk_items(self.key)
        if self.item is not None and not self.item.points:
            items[:] = [i for i in items if i.id != self.item.id]
        self.host.mk_changed()
        cb = getattr(self.host, "mk_touchpad_stopped", None)
        if cb:
            cb()


# ============================================================== toolbar ===

class ToolIcon(Gtk.Widget):
    """16 px icon drawn with the widget's foreground colour (for tools that
    have no stock symbolic icon)."""
    __gtype_name__ = "PreviewMarkupToolIcon"

    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.set_size_request(16, 16)

    def do_snapshot(self, s):
        from gi.repository import Graphene
        r = Graphene.Rect()
        r.init(0, 0, 16, 16)
        cr = s.append_cairo(r)
        c = self.get_color()
        cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
        cr.set_line_width(1.5)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        k = self.kind
        if k == "select":
            cr.move_to(4, 1.5)
            cr.line_to(4, 13)
            cr.line_to(6.8, 10.4)
            cr.line_to(8.8, 14.6)
            cr.line_to(10.6, 13.8)
            cr.line_to(8.7, 9.7)
            cr.line_to(12.5, 9.5)
            cr.close_path()
            cr.fill()
            return
        if k == "line":
            cr.move_to(2.5, 13.5)
            cr.line_to(13.5, 2.5)
        elif k == "arrow":
            cr.move_to(2.5, 13.5)
            cr.line_to(12, 4)
            cr.stroke()
            cr.move_to(14, 2)
            cr.line_to(7.5, 3.5)
            cr.line_to(12.5, 8.5)
            cr.close_path()
            cr.fill()
            return
        elif k in ("rect", "rrect", "oval", "bubble", "star"):
            _shape_path(cr, Item(k, 2, 2.5 if k != "star" else 1.5, 12, 11 if k != "star" else 13))
        elif k == "sketch":
            cr.move_to(1.5, 11)
            cr.curve_to(4, 2, 6, 2, 7.5, 8)
            cr.curve_to(9, 14, 12, 13, 14.5, 4)
        elif k == "draw":
            cr.move_to(2, 14)
            cr.line_to(4, 10)
            cr.line_to(12, 2)
            cr.line_to(14, 4)
            cr.line_to(6, 12)
            cr.close_path()
        elif k == "note":
            cr.rectangle(2.5, 2.5, 11, 11)
            cr.move_to(5, 6)
            cr.line_to(11, 6)
            cr.move_to(5, 9)
            cr.line_to(9, 9)
        elif k == "highlight":
            cr.rectangle(1, 4, 14, 8)
            cr.save()
            cr.set_source_rgba(c.red, c.green, c.blue, 0.35)
            cr.fill()
            cr.restore()
            cr.move_to(4, 10)
            cr.line_to(6.5, 5)
            cr.line_to(9, 10)
            cr.move_to(5, 8.5)
            cr.line_to(8, 8.5)
        elif k == "touchpad":
            cr.rectangle(1.5, 3, 13, 10)
            cr.move_to(1.5, 10.5)
            cr.line_to(14.5, 10.5)
        cr.stroke()


def _tool_button(kind, tooltip, group=None, icon=None):
    b = Gtk.ToggleButton(tooltip_text=tooltip, css_classes=["flat"])
    b.set_child(Gtk.Image(icon_name=icon) if icon else ToolIcon(kind))
    if group is not None:
        b.set_group(group)
    return b


def _rgba_tuple(c: Gdk.RGBA):
    return (c.red, c.green, c.blue, c.alpha)


def _gdk_rgba(t):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = t[0], t[1], t[2], t[3] if len(t) > 3 else 1.0
    return c


class MarkupToolbar(Gtk.Box):
    """The Markup toolbar row. `extra` widgets (e.g. a SignatureButton) are
    appended after the tools."""

    def __init__(self, ctrl: MarkupController, extra=(), text_tools=True):
        super().__init__(spacing=2, css_classes=["toolbar"], margin_start=6, margin_end=6)
        self.ctrl = ctrl
        self.buttons = {}
        sel = _tool_button("select", "Select (Esc)")
        self.buttons["select"] = sel
        self.append(sel)
        if text_tools:
            for k, tip, icon in (("highlight", "Highlight Text", None),
                                 ("underline", "Underline Text", "format-text-underline-symbolic"),
                                 ("strike", "Strike Through Text",
                                  "format-text-strikethrough-symbolic")):
                b = _tool_button(k, tip, sel, icon)
                self.buttons[k] = b
                self.append(b)
            self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4,
                                      margin_end=4))
        for k, tip in (("sketch", "Sketch (shapes are recognised)"), ("draw", "Draw (ink)")):
            b = _tool_button(k, tip, sel)
            self.buttons[k] = b
            self.append(b)
        # shapes: one toggle showing the last shape + a chooser
        self.shape = "rect"
        self.shape_btn = _tool_button("rect", "Shapes", sel)
        self.buttons["shape"] = self.shape_btn
        chooser = Gtk.MenuButton(tooltip_text="Choose Shape", css_classes=["flat"],
                                 icon_name="pan-down-symbolic")
        grid = Gtk.FlowBox(max_children_per_line=4, selection_mode=Gtk.SelectionMode.NONE)
        pop = Gtk.Popover(child=grid)
        for k in SHAPES:
            sb = Gtk.Button(tooltip_text=TOOL_NAMES[k], css_classes=["flat"])
            sb.set_child(ToolIcon(k))
            sb.connect("clicked", self._pick_shape, k, pop)
            grid.append(sb)
        chooser.set_popover(pop)
        sbox = Gtk.Box(css_classes=["linked"])
        sbox.append(self.shape_btn)
        sbox.append(chooser)
        self.append(sbox)
        for k, tip, icon in (("text", "Text Box", "insert-text-symbolic"), ("note", "Note", None)):
            b = _tool_button(k, tip, sel, icon)
            self.buttons[k] = b
            self.append(b)
        for w in extra:
            self.append(w)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4,
                                  margin_end=4))
        # style controls
        self.stroke_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=True),
                                                tooltip_text="Border / Ink Colour",
                                                rgba=_gdk_rgba(ctrl.stroke))
        self.stroke_btn.connect("notify::rgba", lambda b, _p: ctrl.set_style(
            stroke=_rgba_tuple(b.get_rgba())))
        self.append(self.stroke_btn)
        self.fill_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=True),
                                              tooltip_text="Fill Colour",
                                              rgba=_gdk_rgba((1, 1, 1, 0)))
        self.fill_btn.connect("notify::rgba", self._fill_changed)
        self.append(self.fill_btn)
        widths = Gtk.StringList.new(["0.5 pt", "1 pt", "2 pt", "3 pt", "5 pt", "8 pt"])
        self._widths = [0.5, 1, 2, 3, 5, 8]
        self.width_dd = Gtk.DropDown(model=widths, selected=2, tooltip_text="Line Width")
        self.width_dd.connect("notify::selected", lambda d, _p: ctrl.set_style(
            width=self._widths[d.get_selected()]))
        self.append(self.width_dd)
        self.font_btn = Gtk.FontDialogButton(dialog=Gtk.FontDialog(), tooltip_text="Font",
                                             level=Gtk.FontLevel.FONT,
                                             font_desc=Pango.FontDescription.from_string(
                                                 f"{ctrl.font} {ctrl.size:g}"))
        self.font_btn.connect("notify::font-desc", self._font_changed)
        self.append(self.font_btn)
        self.pad_btn = Gtk.ToggleButton(tooltip_text="Draw with Touchpad — the whole pad maps "
                                        "onto the visible page; any key ends",
                                        css_classes=["flat"], visible=False)
        self.pad_btn.set_child(Gtk.Image(icon_name="input-touchpad-symbolic"))
        self.append(self.pad_btn)

        self._syncing = False
        for k, b in self.buttons.items():
            b.connect("toggled", self._toggled, k)
        ctrl.connect("tool-changed", lambda c, t: self.sync())
        ctrl.connect("selection-changed", lambda c: self._sync_style())
        self.sync()

    def _pick_shape(self, btn, kind, pop):
        pop.popdown()
        self.shape = kind
        self.shape_btn.set_child(ToolIcon(kind))
        self.shape_btn.set_tooltip_text(TOOL_NAMES[kind])
        self.ctrl.set_tool(kind)

    def _toggled(self, btn, k):
        if self._syncing or not btn.get_active():
            return
        self.ctrl.set_tool(self.shape if k == "shape" else k)

    def sync(self):
        self._syncing = True
        t = self.ctrl.tool
        k = "shape" if t in SHAPES else t
        if k in self.buttons:
            self.buttons[k].set_active(True)
        self.pad_btn.set_visible(t in INK_KINDS and sys.platform.startswith("linux"))
        self._syncing = False

    def _sync_style(self):
        pass

    def _fill_changed(self, b, _p):
        c = b.get_rgba()
        self.ctrl.set_style(fill=None if c.alpha < 0.01 else _rgba_tuple(c))

    def _font_changed(self, b, _p):
        fd = b.get_font_desc()
        if fd is None:
            return
        size = fd.get_size() / Pango.SCALE or self.ctrl.size
        fam = fd.get_family() or "Sans"
        self.ctrl.set_style(font=fam, size=size)
