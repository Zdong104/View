"""Image viewer: zoom/pan canvas, thumbnail sidebar, Preview-style tools.

Edits are a per-image list of ops (see image_ops) replayed on the upright
base image, so display, save, export, print and undo share one pipeline.
"""
import concurrent.futures
import io
import math
import os
import shutil
import tempfile
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk

import cairo
from PIL import Image

from . import convert, runtime
from .image_ops import (ALPHA_FORMATS, ANIM_EXTS, COLOR_DEFAULTS, PIXBUF_SAVE, WRITABLE,
                        adjust_color, anim_dest_ok, anim_probe, iter_frames, save_animated,
                        apply_ops, color_is_neutral, fit_size, image_info_sections, load_base,
                        load_thumb, ops_need_alpha, oriented_size, quick_preview, save_any)
from .viewer_base import (IMAGE_EXTS, Viewer, device_scale, file_info_section, kind_of,
                          surface_origin)

MAX_ZOOM = 32.0
THUMB_PX = 208            # sidebar rows are 104 logical px tall (2x displays)
PREVIEW_PX = 1600         # colour-adjust live preview
DISPLAY_MAX = 4096        # display texture; full-res tiles are used when zoomed in
TILE_MAX = 4096           # longest side of a detail tile
TILE_CACHE = 96 * 2**20   # bytes of recent detail tiles kept for re-use
_loader = concurrent.futures.ThreadPoolExecutor(max_workers=1)    # full decodes


class _LifoPool:
    """Thumbnail workers: newest request first, so the rows the user is looking
    at (most recently bound) are served before ones scrolled past."""

    def __init__(self, workers=3):
        import collections
        self._q = collections.deque()
        self._cv = threading.Condition()
        for _ in range(workers):
            threading.Thread(target=self._run, daemon=True).start()

    def submit(self, fn):
        with self._cv:
            self._q.append(fn)
            self._cv.notify()

    def _run(self):
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                fn = self._q.pop()
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


_pool = _LifoPool(3)
_untitled_counter = [0]

EXPORT_FORMATS = [  # label, extension, has quality
    ("PNG", ".png", False), ("JPEG", ".jpg", True), ("WebP", ".webp", True),
    ("AVIF", ".avif", True), ("TIFF", ".tif", False), ("BMP", ".bmp", False),
    ("GIF", ".gif", False), ("Animated PNG", ".apng", False),
]
BATCH_FORMATS = EXPORT_FORMATS + [("PDF", ".pdf", False)]


# ------------------------------------------------------------- helpers ---

def pil_to_texture(im):
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.mode else "RGB")
    fmt = Gdk.MemoryFormat.R8G8B8A8 if im.mode == "RGBA" else Gdk.MemoryFormat.R8G8B8
    w, h = im.size
    return Gdk.MemoryTexture.new(w, h, fmt, GLib.Bytes.new(im.tobytes()), w * len(im.mode))


def _thumb_of(im):
    return fit_size(im, THUMB_PX)


def _append_op(ops, op):
    """Append op, merging consecutive rotations / flips."""
    if ops and op[0] == "rotate" and ops[-1][0] == "rotate":
        r = (ops[-1][1] + op[1]) % 360
        return ops[:-1] + ([("rotate", r)] if r else [])
    if ops and op[0] == "flip" and ops[-1][0] == "flip":
        return ops[:-1]
    return ops + [op]


def _sig_op(ov):
    return ("sig", {k: v for k, v in ov.items() if k != "tex"})


class ImageItem(GObject.Object):
    thumb = GObject.Property(type=Gdk.Texture)
    name = GObject.Property(type=str, default="")
    dirty = GObject.Property(type=bool, default=False)

    def __init__(self, gfile=None, name=None, base=None):
        super().__init__()
        self.gfile = gfile
        self.path = gfile.get_path() if gfile is not None else None
        self.name = name or (gfile.get_basename() if gfile is not None else "Untitled")
        self.ops = []
        self.history = []
        self.future = []
        self.overlay = None          # live signature overlay (rendered-image coords)
        self.markup = []             # markup.Item list (rendered-image coords)
        self.base = base             # upright PIL image (only while shown / untitled)
        self.keep_base = base is not None
        self.exif = self.icc = None
        self.orig_size = base.size if base is not None else None
        self.rendered = None         # full-resolution result of ops
        self.display = None          # rendered, scaled to <= DISPLAY_MAX
        self.texture = None          # texture of `display`
        self.thumb_requested = False
        self.bound = 0               # sidebar rows currently showing this item
        self.error = None
        self.update_dirty()

    def update_dirty(self):
        self.dirty = bool(self.ops or self.overlay or self.markup or self.path is None)

    def final_ops(self):
        return (self.ops + ([_sig_op(self.overlay)] if self.overlay else []) +
                ([("markup", [m.to_dict() for m in self.markup])] if self.markup else []))

    def final_image(self):
        """Full-resolution result with all edits (call from a worker thread)."""
        base, exif, icc = self.base, self.exif, self.icc
        if base is None:
            base, exif, icc = load_base(self.path)
        return apply_ops(base, self.final_ops()), exif, icc


# --------------------------------------------------------------- canvas ---

class ImageCanvas(Gtk.Widget):
    """Draws one texture with pan/zoom, a selection and a signature overlay.
    All geometry is in image pixels of the displayed (rendered) image."""
    __gtype_name__ = "PreviewImageCanvas"
    __gsignals__ = {
        "view-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "selection-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "alpha-click": (GObject.SignalFlags.RUN_LAST, None, (float, float)),
        # visible region (x0, y0, x1, y1 in image px) + wanted device px per image px
        "need-detail": (GObject.SignalFlags.RUN_LAST, None,
                        (float, float, float, float, float)),
    }

    def __init__(self):
        super().__init__(focusable=True, hexpand=True, vexpand=True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.texture = None
        self.iw = self.ih = 0
        self.fit = True
        self.zoom = 1.0           # logical pixels per image pixel
        self.ox = self.oy = 0.0   # image-centre offset from view centre (logical px)
        self.overlay = None
        self.tool = "pan"         # pan | rect | ellipse | alpha
        self.sel = None           # [x0, y0, x1, y1] image coords
        self._ptr = None
        self._drag = None
        self.detail = None        # (texture, x0, y0, x1, y1, scale)
        self.text = None          # ocr.TextOverlay (coords in OCR-image px)
        self.text_k = 1.0         # displayed-image px per OCR-image px
        self.markup = None        # markup.MarkupController (page key "img")
        self.markup_items = lambda: []
        self.detail_enabled = True
        self._detail_timer = 0

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        click = Gtk.GestureClick()
        click.connect("pressed", self._pressed)
        click.connect("released", self._released)
        self.add_controller(click)

        pinch = Gtk.GestureZoom()
        pinch.connect("begin", self._pinch_begin)
        pinch.connect("scale-changed", self._pinch_changed)
        self.add_controller(pinch)
        self._pinch = pinch
        self._pinch_z0 = 1.0
        self._pinch_anchor = None

        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES | Gtk.EventControllerScrollFlags.KINETIC)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.add_controller(motion)

    # ---- content -------------------------------------------------------
    def set_content(self, texture, iw, ih, overlay=None, keep_view=False):
        if (iw, ih) != (self.iw, self.ih):
            keep_view = False
        if texture is not self.texture:
            self.detail = None
        self.texture, self.iw, self.ih = texture, iw, ih
        self.overlay = overlay
        if not keep_view:
            self.fit = True
            self.ox = self.oy = 0.0
            self.sel = None
        self.queue_draw()
        self.emit("view-changed")
        self._schedule_detail()

    def clear(self):
        self.texture = None
        self.detail = None
        self.overlay = None
        self.sel = None
        self.queue_draw()
        self.emit("view-changed")

    def set_tool(self, tool):
        self.tool = tool
        if tool not in ("rect", "ellipse"):
            self.sel = None
            self.emit("selection-changed")
        self.set_cursor_from_name("crosshair" if tool != "pan" else None)
        self.queue_draw()

    # ---- geometry ------------------------------------------------------
    def _sf(self):
        return device_scale(self)            # fractional on scaled monitors

    def fit_zoom(self):
        """Zoom that fits the image in the view, never above 100 %.
        Zoom is in logical pixels per image pixel (like Preview's points)."""
        vw, vh = self.get_width(), self.get_height()
        if not (self.iw and self.ih and vw and vh):
            return 1.0
        return min(vw / self.iw, vh / self.ih, 1.0)

    def eff_zoom(self):
        return self.fit_zoom() if self.fit else self.zoom

    def _scale(self):
        return self.eff_zoom()

    def _clamp(self):
        if self.fit:
            self.ox = self.oy = 0.0
            return
        s = self._scale()
        for attr, d, v in (("ox", self.iw, self.get_width()), ("oy", self.ih, self.get_height())):
            lim = max(0.0, (d * s - v) / 2)
            setattr(self, attr, max(-lim, min(lim, getattr(self, attr))))

    def can_pan(self):
        s = self._scale()
        return self.iw * s > self.get_width() + 0.5 or self.ih * s > self.get_height() + 0.5

    def zoom_to(self, z_new, ax=None, ay=None):
        if not self.texture:
            return
        fz = self.fit_zoom()
        z_old = self.eff_zoom()
        z_new = max(fz if fz < 1 else 0.25, min(MAX_ZOOM, z_new))
        vw, vh = self.get_width(), self.get_height()
        if ax is None:
            ax, ay = vw / 2, vh / 2
        if abs(z_new - fz) / fz < 0.005:
            self.fit = True
            self.ox = self.oy = 0.0
        else:
            ratio = z_new / z_old
            cx, cy = vw / 2 + self.ox, vh / 2 + self.oy
            cx = ax - (ax - cx) * ratio
            cy = ay - (ay - cy) * ratio
            self.ox, self.oy = cx - vw / 2, cy - vh / 2
            self.fit = False
            self.zoom = z_new
            self._clamp()
        self.queue_draw()
        self.emit("view-changed")
        self._schedule_detail()

    def zoom_by(self, factor, ax=None, ay=None):
        self.zoom_to(self.eff_zoom() * factor, ax, ay)

    def zoom_fit(self):
        self.fit = True
        self.ox = self.oy = 0.0
        self.queue_draw()
        self.emit("view-changed")
        self._schedule_detail()

    def _snap(self, x, y):
        """Widget point moved onto the nearest device pixel corner."""
        ds = self._sf()
        sx, sy = surface_origin(self)
        return round((sx + x) * ds) / ds - sx, round((sy + y) * ds) / ds - sy

    def _origin(self):
        """Image top-left in widget coordinates. Centring and panning are
        fractional; on a half device pixel LINEAR filtering blurs even 1:1."""
        s = self._scale()
        return self._snap(self.get_width() / 2 + self.ox - self.iw * s / 2,
                          self.get_height() / 2 + self.oy - self.ih * s / 2)

    def to_img(self, x, y):
        s = self._scale()
        x0, y0 = self._origin()
        return (x - x0) / s, (y - y0) / s

    def to_widget(self, ix, iy):
        s = self._scale()
        x0, y0 = self._origin()
        return x0 + ix * s, y0 + iy * s

    def _clamp_img(self, p):
        return (min(max(p[0], 0.0), float(self.iw)), min(max(p[1], 0.0), float(self.ih)))

    # ---- hit tests ------------------------------------------------------
    def _hit_overlay(self, x, y):
        ov = self.overlay
        if not ov:
            return None
        x0, y0 = self.to_widget(ov["cx"] - ov["w"] / 2, ov["cy"] - ov["h"] / 2)
        x1, y1 = self.to_widget(ov["cx"] + ov["w"] / 2, ov["cy"] + ov["h"] / 2)
        if abs(x - x1) <= 12 and abs(y - y1) <= 12:
            return "resize"
        if x0 <= x <= x1 and y0 <= y <= y1:
            return "move"
        return None

    def _sel_handles(self):
        x0, y0 = self.to_widget(self.sel[0], self.sel[1])
        x1, y1 = self.to_widget(self.sel[2], self.sel[3])
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return {"nw": (x0, y0), "n": (mx, y0), "ne": (x1, y0), "e": (x1, my),
                "se": (x1, y1), "s": (mx, y1), "sw": (x0, y1), "w": (x0, my)}

    def _hit_sel(self, x, y):
        if not self.sel:
            return None
        for name, (hx, hy) in self._sel_handles().items():
            if abs(x - hx) <= 10 and abs(y - hy) <= 10:
                return name
        x0, y0 = self.to_widget(self.sel[0], self.sel[1])
        x1, y1 = self.to_widget(self.sel[2], self.sel[3])
        if x0 <= x <= x1 and y0 <= y <= y1:
            return "move"
        return None

    # ---- input ----------------------------------------------------------
    _CURSORS = {"nw": "nw-resize", "se": "se-resize", "ne": "ne-resize", "sw": "sw-resize",
                "n": "n-resize", "s": "s-resize", "e": "e-resize", "w": "w-resize",
                "move": "move"}

    def _text_pt(self, x, y):
        ix, iy = self.to_img(x, y)
        return ix / self.text_k, iy / self.text_k

    def _text_hit(self, x, y):
        return (self.text is not None and self.tool == "pan" and self.text.has_text()
                and self.text.hit(*self._text_pt(x, y), margin=3.0 / max(self._scale(), 1e-6)))

    def _motion(self, _c, x, y):
        self._ptr = (x, y)
        if self._drag is not None:
            return
        if self.markup is not None:
            cur = self.markup.cursor_at(x, y)
            if cur:
                self.set_cursor_from_name(cur)
                return
            if self.markup.tool != "select":
                self.set_cursor_from_name("crosshair")
                return
        if self._text_hit(x, y):
            self.set_cursor_from_name("text")
            return
        hit = self._hit_overlay(x, y)
        if hit:
            self.set_cursor_from_name("nwse-resize" if hit == "resize" else "move")
            return
        if self.tool in ("rect", "ellipse"):
            h = self._hit_sel(x, y)
            self.set_cursor_from_name(self._CURSORS.get(h, "crosshair"))
        elif self.tool == "alpha":
            self.set_cursor_from_name("crosshair")
        else:
            self.set_cursor_from_name(None)

    def _drag_begin(self, g, x, y):
        self.grab_focus()
        self._moved = False
        if self.markup is not None and self.texture and self.markup.on_press(
                x, y, 1, g.get_current_event_state()):
            self._drag = ("markup", x, y)
            if self.text is not None:
                self.text.clear()
            return
        hit = self._hit_overlay(x, y)
        if hit:
            self._drag = ("ov-" + hit, dict(self.overlay))
        elif self.tool in ("rect", "ellipse") and self.texture:
            h = self._hit_sel(x, y)
            if h:
                self._drag = ("sel", h, list(self.sel))
            else:
                p = self._clamp_img(self.to_img(x, y))
                self.sel = [p[0], p[1], p[0], p[1]]
                self._drag = ("sel", "se", list(self.sel))
                self.emit("selection-changed")
        elif self._text_hit(x, y):
            self._drag = ("text", x, y, False)     # selection starts once it moves
        elif self.tool == "pan" and self.can_pan():
            self._drag = ("pan", (self.ox, self.oy))
            self.set_cursor_from_name("grabbing")
        else:
            self._drag = None

    def _drag_update(self, g, dx, dy):
        if not self._drag:
            return
        if abs(dx) + abs(dy) > 2:
            self._moved = True
        mode = self._drag[0]
        s = self._scale()
        if mode == "markup":
            _m, x0, y0 = self._drag
            self.markup.on_drag(x0 + dx, y0 + dy, g.get_current_event_state())
            self.queue_draw()
            return
        if mode == "text":
            _m, x0, y0, started = self._drag
            if not started:
                if abs(dx) + abs(dy) < 3:
                    return
                self.text.begin_select(*self._text_pt(x0, y0))
                self._drag = ("text", x0, y0, True)
            self.text.update_select(*self._text_pt(x0 + dx, y0 + dy))
            self.queue_draw()
            return
        if mode == "pan":
            start = self._drag[1]
            self.ox, self.oy = start[0] + dx, start[1] + dy
            self._clamp()
            self._schedule_detail()
        elif mode == "sel":
            _m, h, r = self._drag
            ddx, ddy = dx / s, dy / s
            x0, y0, x1, y1 = r
            if h == "move":
                w, hh = x1 - x0, y1 - y0
                nx0 = min(max(x0 + ddx, 0), self.iw - w)
                ny0 = min(max(y0 + ddy, 0), self.ih - hh)
                self.sel = [nx0, ny0, nx0 + w, ny0 + hh]
            else:
                if "w" in h:
                    x0 += ddx
                if "e" in h:
                    x1 += ddx
                if "n" in h:
                    y0 += ddy
                if "s" in h:
                    y1 += ddy
                a = self._clamp_img((min(x0, x1), min(y0, y1)))
                b = self._clamp_img((max(x0, x1), max(y0, y1)))
                self.sel = [a[0], a[1], b[0], b[1]]
            self.emit("selection-changed")
        else:
            start = self._drag[1]
            ov = self.overlay
            if mode == "ov-move":
                ov["cx"], ov["cy"] = start["cx"] + dx / s, start["cy"] + dy / s
            else:
                aspect = start["h"] / start["w"]
                nw = max(24.0, start["w"] + dx / s)
                ov["w"], ov["h"] = nw, nw * aspect
                ov["cx"] = start["cx"] - start["w"] / 2 + nw / 2
                ov["cy"] = start["cy"] - start["h"] / 2 + nw * aspect / 2
        self.queue_draw()

    def _drag_end(self, _g, dx, dy):
        if self._drag and self._drag[0] == "markup":
            _m, x0, y0 = self._drag
            self.markup.on_release(x0 + dx, y0 + dy)
            self._drag = None
            self.queue_draw()
            return
        if self._drag and self._drag[0] == "text":
            if self._drag[3]:
                self.text.end_select()
            elif self.text.has_selection():
                self.text.clear()
            self._drag = None
            self.queue_draw()
            return
        if self.text is not None and self.text.has_selection() and not self._moved:
            self.text.clear()
            self.queue_draw()
        if self._drag and self._drag[0] == "sel" and self.sel:
            x0, y0, x1, y1 = self.sel
            if (x1 - x0) * self._scale() < 4 or (y1 - y0) * self._scale() < 4:
                self.sel = None
                self.emit("selection-changed")
                self.queue_draw()
        self._drag = None
        if self.tool == "pan":
            self.set_cursor_from_name(None)

    def _pressed(self, g, n_press, x, y):
        self.grab_focus()
        if n_press >= 2 and self.markup is not None and self.markup.on_press(
                x, y, n_press, g.get_current_event_state()):
            self.markup.on_release(x, y)
            self.queue_draw()
            return
        if n_press >= 2 and self._text_hit(x, y):
            (self.text.select_word_at if n_press == 2 else self.text.select_line_at)(
                *self._text_pt(x, y))
            self.queue_draw()
            return
        if n_press == 2 and self.tool == "pan" and not self._hit_overlay(x, y):
            if self.fit and self.fit_zoom() < 1:
                self.zoom_to(1.0, x, y)
            else:
                self.zoom_fit()

    def _released(self, _g, n_press, x, y):
        if n_press == 1 and self.tool == "alpha" and self.texture and not getattr(self, "_moved", False):
            ix, iy = self.to_img(x, y)
            if 0 <= ix < self.iw and 0 <= iy < self.ih:
                self.emit("alpha-click", ix, iy)

    def _pinch_begin(self, _g, _seq):
        self._pinch_z0 = self.eff_zoom()
        ok, x, y = self._pinch.get_bounding_box_center()
        self._pinch_anchor = (x, y) if ok else None

    def _pinch_changed(self, _g, scale):
        a = self._pinch_anchor or (None, None)
        self.zoom_to(self._pinch_z0 * scale, a[0], a[1])

    def _scroll(self, ctl, dx, dy):
        if not self.texture:
            return False
        state = ctl.get_current_event_state()
        wheel = ctl.get_unit() == Gdk.ScrollUnit.WHEEL
        if state & Gdk.ModifierType.CONTROL_MASK:
            k = 0.1 if wheel else 0.01
            a = self._ptr or (None, None)
            self.zoom_by(math.exp(-dy * k), a[0], a[1])
            return True
        if not self.can_pan():
            return False
        m = 50 if wheel else 1
        self.ox -= dx * m
        self.oy -= dy * m
        self._clamp()
        self._schedule_detail()
        self.queue_draw()
        return True

    # ---- drawing -------------------------------------------------------
    def add_popover(self, pop):
        self._popovers = getattr(self, "_popovers", [])
        if pop not in self._popovers:
            pop.set_parent(self)
            self._popovers.append(pop)

    def remove_popover(self, pop):
        if pop in getattr(self, "_popovers", []):
            self._popovers.remove(pop)
            pop.unparent()

    def do_size_allocate(self, width, height, baseline):
        for pop in getattr(self, "_popovers", []):
            pop.present()
        if self.fit:
            self.emit("view-changed")
        self._schedule_detail()

    # ---- full-resolution detail tile ----------------------------------------
    def set_detail(self, texture, box, scale):
        self.detail = (texture,) + tuple(box) + (scale,)
        self.queue_draw()

    def _wanted_detail(self):
        """(box, scale) needed for a sharp view, or None if the texture suffices.
        Zoomed out (scale < 1) the tile is a high-quality downscale at exactly
        device resolution, drawn 1:1: its box spans whole device pixels from
        the (device-aligned) image origin."""
        if not (self.texture and self.iw and self.detail_enabled):
            return None
        need = self.eff_zoom() * self._sf()                 # device px per image px
        have = self.texture.get_width() / self.iw
        x0, y0 = self.to_img(0, 0)
        x1, y1 = self.to_img(self.get_width(), self.get_height())
        if need < 1:
            dw, dh = round(self.iw * need), round(self.ih * need)
            i0, j0 = max(0, math.floor(x0 * need)), max(0, math.floor(y0 * need))
            i1, j1 = min(dw, math.ceil(x1 * need)), min(dh, math.ceil(y1 * need))
            if max(i1 - i0, j1 - j0) > TILE_MAX:
                return None                # huge view: GPU mipmaps it is
            room = min(TILE_MAX / max(i1 - i0, 1), TILE_MAX / max(j1 - j0, 1))
            f = min(0.25, (room - 1) / 2)
            mi, mj = int((i1 - i0) * f), int((j1 - j0) * f)
            i0, j0 = max(0, i0 - mi), max(0, j0 - mj)
            i1, j1 = min(dw, i1 + mi), min(dh, j1 + mj)
            return (i0 / need, j0 / need, min(i1 / need, float(self.iw)),
                    min(j1 / need, float(self.ih))), need
        if have >= 1 or need <= have * 1.05:
            return None                    # the texture is already full resolution
        scale = 1.0
        # margin for panning, but keep the tile within GPU texture limits
        bw, bh = (x1 - x0), (y1 - y0)
        room = min(TILE_MAX / max(bw * scale, 1), TILE_MAX / max(bh * scale, 1))
        f = max(0.0, min(0.25, (room - 1) / 2))
        if room < 1:                       # view larger than a tile: lower the scale
            scale *= room
        mx, my = bw * f, bh * f
        box = (max(0.0, x0 - mx), max(0.0, y0 - my), min(float(self.iw), x1 + mx),
               min(float(self.ih), y1 + my))
        return box, scale

    def _schedule_detail(self):
        if self._detail_timer:
            GLib.source_remove(self._detail_timer)
        self._detail_timer = GLib.timeout_add(90, self._detail_check)

    def _detail_check(self):
        self._detail_timer = 0
        want = self._wanted_detail()
        if want is None:
            return False
        (bx0, by0, bx1, by1), scale = want
        d = self.detail
        if d is not None and self._covers(d) and (
                abs(d[5] - scale) <= scale * 1e-3 if scale < 1 else d[5] >= scale * 0.95):
            return False
        self.emit("need-detail", bx0, by0, bx1, by1, scale)
        return False

    def _covers(self, d):
        """Does detail tile `d` cover the visible part of the image?"""
        _t, x0, y0, x1, y1, _sc = d
        tol = max(0.5, 1 / (self.eff_zoom() * self._sf()))  # downscales end on a device px
        vx0, vy0 = self.to_img(0, 0)
        vx1, vy1 = self.to_img(self.get_width(), self.get_height())
        return (x0 <= max(vx0, 0) + tol and y0 <= max(vy0, 0) + tol and
                x1 >= min(vx1, self.iw) - tol and y1 >= min(vy1, self.ih) - tol)

    @staticmethod
    def _append(snapshot, tex, z, rect):
        """Draw `tex` at `z` device px per texel. Zoomed out, GSK's own choice
        (LINEAR down to 1/2, mipmaps below) is sharper than TRILINEAR, which
        blends in the half-size mip level from the first step down. Whole
        magnifications (100 % on a 2x screen) repeat pixels exactly, as sharp
        as the image on a 1x screen; LINEAR would smear them."""
        if z >= 4 or z >= 1.999 and abs(z - round(z)) < 1e-3:
            snapshot.append_scaled_texture(tex, Gsk.ScalingFilter.NEAREST, rect)
        elif z < 1:
            snapshot.append_texture(tex, rect)
        else:
            snapshot.append_scaled_texture(tex, Gsk.ScalingFilter.LINEAR, rect)

    def do_snapshot(self, snapshot):
        if not self.texture:
            return
        vw, vh = self.get_width(), self.get_height()
        self._clamp()
        s = self._scale()
        ds = self._sf()
        if ds != getattr(self, "_last_ds", ds):    # moved to a monitor with another scale
            self._schedule_detail()
        self._last_ds = ds
        need = self.eff_zoom() * ds                 # device px per image px
        have = self.texture.get_width() / max(1, self.iw)
        d, exact = self.detail, False
        if d is not None:
            exact = d[5] < 1 and abs(d[5] - need) <= need * 1e-3
            if not (exact or d[5] > have * 1.05):
                d = None                # stale downscale: the texture is as good meanwhile
        r = Graphene.Rect()
        # Draw in widget coordinates (no snapshot.scale): GSK in GTK 4.22 crashes
        # rendering textures under very small scale transforms (e.g. 0.03).
        x0, y0 = self.to_widget(0, 0)              # on a device pixel (_origin)
        if d is None or not self._covers(d):
            self._append(snapshot, self.texture, need / have,
                         r.init(x0, y0, self.iw * s, self.ih * s))
        if d is not None:
            dt, dx0, dy0, dx1, dy1, sc = d
            if exact:
                # device-resolution downscale: 1:1 on whole device pixels
                wx0, wy0 = self._snap(*self.to_widget(dx0, dy0))
                snapshot.append_texture(dt, r.init(wx0, wy0, dt.get_width() / ds,
                                                   dt.get_height() / ds))
            else:
                wx0, wy0 = self.to_widget(dx0, dy0)
                self._append(snapshot, dt, need / sc,
                             r.init(wx0, wy0, (dx1 - dx0) * s, (dy1 - dy0) * s))
        ov = self.overlay
        if ov:
            ox0, oy0 = self.to_widget(ov["cx"] - ov["w"] / 2, ov["cy"] - ov["h"] / 2)
            snapshot.append_scaled_texture(ov["tex"], Gsk.ScalingFilter.TRILINEAR,
                                           r.init(ox0, oy0, ov["w"] * s, ov["h"] * s))
        mk_items = self.markup_items() if self.markup is not None else []
        show_text = self.text is not None and (self.text.has_selection() or self.text.show_boxes)
        if mk_items or (self.markup is not None and self.markup.tool != "select") or show_text:
            cr = snapshot.append_cairo(r.init(0, 0, vw, vh))
            if self.markup is not None:
                cr.save()
                cr.translate(x0, y0)
                cr.scale(s, s)
                self.markup.draw(cr, "img", s)
                cr.restore()
            if show_text:
                self.text.draw(cr, scale=s * self.text_k, offset_x=x0, offset_y=y0)
        if not (ov or self.sel):
            return
        cr = snapshot.append_cairo(r.init(0, 0, vw, vh))
        if ov:
            x0, y0 = self.to_widget(ov["cx"] - ov["w"] / 2, ov["cy"] - ov["h"] / 2)
            x1, y1 = self.to_widget(ov["cx"] + ov["w"] / 2, ov["cy"] + ov["h"] / 2)
            cr.set_source_rgb(0.21, 0.52, 0.89)
            cr.set_line_width(1.5)
            cr.rectangle(x0, y0, x1 - x0, y1 - y0)
            cr.stroke()
            cr.rectangle(x1 - 5, y1 - 5, 10, 10)
            cr.fill()
        if self.sel:
            x0, y0 = self.to_widget(self.sel[0], self.sel[1])
            x1, y1 = self.to_widget(self.sel[2], self.sel[3])
            ix0, iy0 = self.to_widget(0, 0)
            ix1, iy1 = self.to_widget(self.iw, self.ih)

            def shape():
                if self.tool == "ellipse":
                    cr.save()
                    cr.translate((x0 + x1) / 2, (y0 + y1) / 2)
                    cr.scale(max((x1 - x0) / 2, 0.5), max((y1 - y0) / 2, 0.5))
                    cr.arc(0, 0, 1, 0, 2 * math.pi)
                    cr.restore()
                else:
                    cr.rectangle(x0, y0, x1 - x0, y1 - y0)
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.rectangle(ix0, iy0, ix1 - ix0, iy1 - iy0)
            shape()
            cr.set_source_rgba(0, 0, 0, 0.45)
            cr.fill()
            cr.set_line_width(1.0)
            shape()
            cr.set_source_rgb(1, 1, 1)
            cr.stroke_preserve()
            cr.set_dash([4, 4])
            cr.set_source_rgb(0, 0, 0)
            cr.stroke()
            cr.set_dash([])
            for hx, hy in self._sel_handles().values():
                cr.rectangle(hx - 4, hy - 4, 8, 8)
                cr.set_source_rgb(1, 1, 1)
                cr.fill_preserve()
                cr.set_source_rgb(0.21, 0.52, 0.89)
                cr.stroke()


# --------------------------------------------------------------- viewer ---

class ImageViewer(Viewer):
    def __init__(self, window):
        super().__init__(window)
        self.store = Gio.ListStore.new(ImageItem)
        self.current = None
        self.compact = False
        self._siblings = []
        self._sib_idx = -1
        self._reordering = False
        self._render_gen = 0
        self._slideshow = None
        self._slideshow_state = None
        self._color = None
        self._headers_built = False
        self.mk = None             # markup.MarkupController, created on first use
        self.mk_bar = None
        self._ocr = None           # {"item", "cancel": Gio.Cancellable, "timer"}
        self.live_text = True      # recognise text automatically (Live Text)

        self.split = Adw.OverlaySplitView(sidebar_position=Gtk.PackType.START,
                                          min_sidebar_width=170, max_sidebar_width=280,
                                          sidebar_width_fraction=0.2, show_sidebar=False)
        self.split.set_vexpand(True)
        self.append(self.split)
        self.split.set_sidebar(self._build_sidebar())
        content = Gtk.Box(hexpand=True)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.markup_rev = Gtk.Revealer(reveal_child=False,
                                       transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        left.append(self.markup_rev)
        left.append(self._build_canvas())
        content.append(left)
        content.append(self._build_color_panel())
        self.split.set_content(content)
        self._build_actions()
        self.connect("destroy", self._on_destroy)

    # ---- host helpers (window may be a minimal stub) ---------------------
    def _host(self):
        return self.window if (isinstance(self.window, Gtk.Window) and not self.compact) else self

    def _root(self):
        r = self.get_root()
        return r if isinstance(r, Gtk.Window) else None

    def _parent_widget(self):
        return self if self.get_root() is not None else (
            self.window if isinstance(self.window, Gtk.Widget) else self)

    # ---- construction --------------------------------------------------
    def _build_canvas(self):
        self.canvas = ImageCanvas()
        self.canvas.add_css_class("view")
        self.canvas.connect("view-changed", self._on_view_changed)
        self.canvas.connect("alpha-click", self._on_alpha_click)
        self.canvas.connect("selection-changed", lambda *_: self._update_actions())
        self.canvas.connect("need-detail", self._on_need_detail)
        self._detail_gen = 0
        self._tiles = None         # (rendered image, [(key, texture)]) newest first

        overlay = Gtk.Overlay(child=self.canvas, hexpand=True)
        overlay.add_css_class("view")
        self.spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                   width_request=32, height_request=32, visible=False,
                                   can_target=False)
        overlay.add_overlay(self.spinner)

        self.osd = Gtk.Box(spacing=2, halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
                           margin_bottom=14, css_classes=["preview-osd"])
        self.osd.append(Gtk.Button(icon_name="zoom-out-symbolic", action_name="img.zoom-out",
                                   tooltip_text="Zoom Out (−)", css_classes=["flat", "circular"],
                                   focus_on_click=False))
        self.zoom_btn = Gtk.Button(label="100%", action_name="img.zoom-toggle",
                                   css_classes=["flat"], width_request=64,
                                   tooltip_text="Toggle Fit / Actual Size", focus_on_click=False)
        self.osd.append(self.zoom_btn)
        self.osd.append(Gtk.Button(icon_name="zoom-in-symbolic", action_name="img.zoom-in",
                                   tooltip_text="Zoom In (+)", css_classes=["flat", "circular"],
                                   focus_on_click=False))
        self.osd.append(Gtk.Button(icon_name="zoom-fit-best-symbolic", action_name="img.zoom-fit",
                                   tooltip_text="Fit to Window (0)",
                                   css_classes=["flat", "circular"], focus_on_click=False))
        overlay.add_overlay(self.osd)

        # tool hint bar (selection / instant alpha)
        self.tool_bar = Gtk.Revealer(halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                                     margin_top=10, reveal_child=False,
                                     transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        tb = Gtk.Box(spacing=8, css_classes=["preview-osd"])
        self.tool_label = Gtk.Label(margin_start=8)
        tb.append(self.tool_label)
        self.tol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.tol_scale.set_value(25)
        self.tol_scale.set_size_request(140, -1)
        self.tol_scale.set_tooltip_text("Tolerance")
        tb.append(self.tol_scale)
        self.crop_btn = Gtk.Button(label="Crop", action_name="img.crop", css_classes=["flat"],
                                   focus_on_click=False)
        tb.append(self.crop_btn)
        done = Gtk.Button(label="Done", css_classes=["flat"], focus_on_click=False)
        done.connect("clicked", lambda *_: self._set_tool("pan"))
        tb.append(done)
        self.tool_bar.set_child(tb)
        overlay.add_overlay(self.tool_bar)
        self.canvas_overlay = overlay
        return overlay

    def _build_color_panel(self):
        rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_LEFT,
                           reveal_child=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=280,
                      margin_top=12, margin_bottom=12, margin_start=14, margin_end=14)
        head = Gtk.Box(spacing=6)
        head.append(Gtk.Label(label="Adjust Color", css_classes=["heading"], hexpand=True,
                              xalign=0))
        self.auto_btn = Gtk.ToggleButton(label="Auto Levels", css_classes=["flat"])
        self.auto_btn.connect("toggled", lambda *_: self._color_changed())
        head.append(self.auto_btn)
        box.append(head)
        self.color_scales = {}
        spec = [("exposure", "Exposure", -2, 2), ("contrast", "Contrast", -1, 1),
                ("highlights", "Highlights", -1, 1), ("shadows", "Shadows", -1, 1),
                ("saturation", "Saturation", -1, 1), ("temperature", "Temperature", -1, 1),
                ("tint", "Tint", -1, 1), ("sepia", "Sepia", 0, 1), ("sharpness", "Sharpness", 0, 1)]
        for key, label, lo, hi in spec:
            box.append(Gtk.Label(label=label, xalign=0, css_classes=["caption"]))
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 0.01)
            sc.set_value(0)
            sc.set_draw_value(False)
            sc.connect("value-changed", lambda *_: self._color_changed())
            box.append(sc)
            self.color_scales[key] = sc
        btns = Gtk.Box(spacing=6, margin_top=8, homogeneous=True)
        reset = Gtk.Button(label="Reset")
        reset.connect("clicked", lambda *_: self._color_reset())
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._color_close(apply=False))
        apply_ = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        apply_.connect("clicked", lambda *_: self._color_close(apply=True))
        for b in (reset, cancel, apply_):
            btns.append(b)
        box.append(btns)
        sw = Gtk.ScrolledWindow(child=box, vexpand=True, hexpand=False, width_request=300,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        row = Gtk.Box(css_classes=["background"], hexpand=False)
        row.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        row.append(sw)
        rev.set_child(row)
        # Explicit hexpand=False: otherwise the (hexpanding) sliders propagate
        # expansion and the hidden revealer steals half of the window's width.
        rev.set_hexpand(False)
        self.color_rev = rev
        return rev

    def _build_sidebar(self):
        tv = Adw.ToolbarView()
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._row_setup)
        factory.connect("bind", self._row_bind)
        factory.connect("unbind", self._row_unbind)
        self.selection = Gtk.MultiSelection(model=self.store)
        self.selection.connect("selection-changed", self._on_selection_changed)
        self.listview = Gtk.ListView(model=self.selection, factory=factory,
                                     enable_rubberband=True)
        self.listview.add_css_class("navigation-sidebar")
        sw = Gtk.ScrolledWindow(child=self.listview, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        tv.set_content(sw)
        bar = Gtk.ActionBar()
        bar.pack_start(Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Images…",
                                  action_name="img.add"))
        bar.pack_start(Gtk.Button(icon_name="list-remove-symbolic",
                                  tooltip_text="Remove from List (Delete)",
                                  action_name="img.remove"))
        bar.pack_end(Gtk.Button(icon_name="document-send-symbolic",
                                tooltip_text="Export Selected Images…",
                                action_name="img.export-selected"))
        self.sel_label = Gtk.Label(css_classes=["dim-label", "caption"])
        bar.set_center_widget(self.sel_label)
        tv.add_bottom_bar(bar)
        return tv

    def _row_setup(self, _f, li):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6,
                      margin_bottom=6, margin_start=4, margin_end=4)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                          height_request=104)
        lbl = Gtk.Label(ellipsize=3, css_classes=["caption"], max_width_chars=22)
        box.append(pic)
        box.append(lbl)
        box._pic, box._lbl, box._li = pic, lbl, li
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", self._drag_prepare, li)
        src.connect("drag-begin", self._drag_begin, box)
        box.add_controller(src)
        dt = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
        dt.connect("motion", self._drop_motion, box)
        dt.connect("leave", lambda *_: self._clear_drop_marks(box))
        dt.connect("drop", self._drop_drop, box)
        box.add_controller(dt)
        li.set_child(box)

    def _row_bind(self, _f, li):
        box, item = li.get_child(), li.get_item()

        def upd(*_):
            box._pic.set_paintable(item.thumb)
            box._lbl.set_text(item.name + (" •" if item.dirty else ""))
        box._ids = [item.connect("notify::thumb", upd), item.connect("notify::dirty", upd),
                    item.connect("notify::name", upd)]
        upd()
        item.bound += 1
        if item.thumb is None and not item.thumb_requested:
            self._request_thumb(item)

    def _row_unbind(self, _f, li):
        box, item = li.get_child(), li.get_item()
        for i in getattr(box, "_ids", []):
            item.disconnect(i)
        box._ids = []
        box._pic.set_paintable(None)
        item.bound = max(0, item.bound - 1)

    # ---- sidebar drag & drop -------------------------------------------
    def _drag_prepare(self, _src, _x, _y, li):
        v = GObject.Value()
        v.init(GObject.TYPE_INT)
        v.set_int(li.get_position())
        return Gdk.ContentProvider.new_for_value(v)

    def _drag_begin(self, src, _drag, box):
        paintable = box._pic.get_paintable()
        if paintable:
            src.set_icon(paintable, 0, 0)

    def _clear_drop_marks(self, box):
        box.remove_css_class("drop-before")
        box.remove_css_class("drop-after")

    def _drop_motion(self, _dt, _x, y, box):
        self._clear_drop_marks(box)
        box.add_css_class("drop-before" if y < box.get_height() / 2 else "drop-after")
        return Gdk.DragAction.MOVE

    def _drop_drop(self, _dt, value, _x, y, box):
        self._clear_drop_marks(box)
        pos = box._li.get_position()
        self._move_items(int(value), pos if y < box.get_height() / 2 else pos + 1)
        return True

    def _selected_positions(self):
        bs = self.selection.get_selection()
        return [i for i in range(self.store.get_n_items()) if bs.contains(i)]

    def _move_items(self, src_pos, target):
        n = self.store.get_n_items()
        if not 0 <= src_pos < n:
            return
        sel = self._selected_positions()
        moving = sel if (src_pos in sel and len(sel) > 1) else [src_pos]
        items = self._items()
        mset = {id(items[i]) for i in moving}
        moved = [items[i] for i in moving]
        new = ([x for x in items[:target] if id(x) not in mset] + moved +
               [x for x in items[target:] if id(x) not in mset])
        if [id(x) for x in new] == [id(x) for x in items]:
            return
        self._reordering = True
        self.store.splice(0, n, new)
        self.selection.unselect_all()
        for k, it in enumerate(new):
            if id(it) in mset:
                self.selection.select_item(k, False)
        self._reordering = False
        self._update_sel_label()
        self.refresh_title()

    # ---- actions ---------------------------------------------------------
    VIEW_KEYS = [
        ("<Control>c", "copy"), ("plus|equal|KP_Add|<Control>plus|<Control>equal", "zoom-in"),
        ("minus|KP_Subtract|<Control>minus", "zoom-out"), ("0|KP_0|<Control>0", "zoom-fit"),
        ("1|KP_1|<Control>1", "zoom-actual"),
    ]
    EDIT_KEYS = [
        ("<Control>s", "save"), ("<Control><Shift>s", "save-as"),
        ("<Control><Shift>e", "export-as"), ("<Control>l", "rotate-left"),
        ("<Control>r", "rotate-right"), ("<Control>a", "select-all"), ("F9", "toggle-sidebar"),
        ("Delete|KP_Delete", "delete"), ("Left", "prev"), ("Right", "next"),
        ("Escape", "cancel"), ("BackSpace", "discard-overlay"), ("<Control>k", "crop"),
        ("<Control><Shift>c", "adjust-color"), ("<Control>z", "undo"),
        ("<Control><Shift>z|<Control>y", "redo"), ("<Control>p", "print"),
        ("F5", "slideshow"), ("<Control><Shift>a", "markup"),
        ("<Control><Shift>t", "copy-text"),
    ]

    def _build_actions(self):
        grp = Gio.SimpleActionGroup()
        self._acts = {}

        def add(name, cb):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            grp.add_action(a)
            self._acts[name] = a

        add("rotate-left", lambda: self._transform(("rotate", 270)))
        add("rotate-right", lambda: self._transform(("rotate", 90)))
        add("flip", lambda: self._transform(("flip",)))
        add("zoom-in", lambda: self.canvas.zoom_by(1.25))
        add("zoom-out", lambda: self.canvas.zoom_by(0.8))
        add("zoom-fit", self.canvas.zoom_fit)
        add("zoom-actual", lambda: self.canvas.zoom_to(1.0))
        add("zoom-toggle", self._zoom_toggle)
        add("copy", self._copy)
        add("save", self.save_changes)
        add("save-as", self._save_as)
        add("export-as", self._export_as)
        add("export-pdf", self._export_pdf)
        add("export-selected", self._export_selected)
        add("adjust-size", self._adjust_size)
        add("adjust-color", self._color_open)
        add("crop", self._crop)
        add("undo", self._undo)
        add("redo", self._redo)
        add("revert", self._revert)
        add("remove", self._remove_selected)
        add("delete", self._delete_key)
        add("add", self._add_dialog)
        add("select-all", self._select_all)
        add("toggle-sidebar", self._toggle_sidebar)
        add("prev", lambda: self._step(-1))
        add("next", lambda: self._step(1))
        add("cancel", self._cancel)
        add("discard-overlay", self._discard_overlay)
        add("print", self._print)
        add("wallpaper", self._wallpaper)
        add("slideshow", self._toggle_slideshow)
        add("markup", self._toggle_markup)
        add("copy-text", self._copy_all_text)
        add("recognize", lambda: self._run_ocr(force=True))
        add("show-text", self._toggle_text_boxes)
        tool = Gio.SimpleAction.new_stateful("tool", GLib.VariantType.new("s"),
                                             GLib.Variant("s", "pan"))
        tool.connect("change-state", lambda a, v: self._set_tool(v.get_string()))
        grp.add_action(tool)
        self._acts["tool"] = tool
        self.action_group = grp
        self.insert_action_group("img", grp)

        self._view_ctl = Gtk.ShortcutController()
        self._edit_ctl = Gtk.ShortcutController()
        for ctl, keys in ((self._view_ctl, self.VIEW_KEYS), (self._edit_ctl, self.EDIT_KEYS)):
            for trig, act in keys:
                ctl.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trig),
                                                  Gtk.NamedAction.new(f"img.{act}")))
        self._attached_host = None
        self._attach_host()
        self._update_actions()

    def _attach_host(self):
        host = self._host()
        if host is self._attached_host:
            return
        self._detach_host()
        if host is not self:
            host.insert_action_group("img", self.action_group)
        host.add_controller(self._view_ctl)
        if not self.compact:
            host.add_controller(self._edit_ctl)
        self._attached_host = host

    def _detach_host(self):
        host = self._attached_host
        if host is None:
            return
        for ctl in (self._view_ctl, self._edit_ctl):
            if ctl.get_widget() is host:
                host.remove_controller(ctl)
        if host is not self:
            host.insert_action_group("img", None)
        self._attached_host = None

    EDIT_ACTIONS = ("rotate-left", "rotate-right", "flip", "save", "save-as", "export-as",
                    "export-pdf", "export-selected", "adjust-size", "adjust-color", "crop",
                    "undo", "redo", "revert", "remove", "delete", "add", "discard-overlay",
                    "wallpaper", "tool", "print", "slideshow", "select-all", "toggle-sidebar",
                    "markup")

    def _update_actions(self):
        if not hasattr(self, "_acts"):
            return
        items = self._items()
        it = self.current
        ready = bool(it is not None and it.rendered is not None)
        en = {
            "save": any(i.dirty for i in items),
            "save-as": ready, "export-as": ready, "copy": ready, "print": ready,
            "wallpaper": ready, "adjust-size": ready, "adjust-color": ready,
            "rotate-left": ready, "rotate-right": ready, "flip": ready,
            "crop": ready and self.canvas.sel is not None,
            "undo": bool(it and (it.history or it.overlay)),
            "redo": bool(it and it.future),
            "revert": bool(it and (it.ops or it.overlay or it.markup) and it.path),
            "discard-overlay": bool(it and it.overlay),
            "remove": len(items) > 1,
            "prev": len(items) > 1 or len(self._siblings) > 1,
            "next": len(items) > 1 or len(self._siblings) > 1,
            "export-selected": bool(items), "export-pdf": bool(items),
            "slideshow": len(items) > 1 or len(self._siblings) > 1,
            "markup": ready,
            "copy-text": bool(self.canvas.text is not None and self.canvas.text.has_text()),
            "show-text": bool(self.canvas.text is not None and self.canvas.text.has_text()),
            "recognize": ready,
        }
        for name, a in self._acts.items():
            ok = en.get(name, True)
            if self.compact and name in self.EDIT_ACTIONS:
                ok = False
            a.set_enabled(ok)

    def _on_destroy(self, *_):
        self._stop_slideshow()
        try:
            self._detach_host()
        except Exception:  # noqa: BLE001
            pass

    # ---- compact (Quick Look) ----------------------------------------------
    def release(self):
        """Drop decoded images, textures and pending work (Quick Look calls this
        when switching files). The viewer can be load()ed again afterwards."""
        self._clear_text()
        self._render_gen += 1          # stale-out running decodes / renders
        self._detail_gen += 1
        self._stop_slideshow()
        self._stop_animation()
        self._color = None
        self.color_rev.set_reveal_child(False)
        for it in self._items():
            it.base = it.rendered = it.display = it.texture = None
            it.keep_base = False
        self.current = None
        self.canvas.clear()
        self._reordering = True
        self.store.remove_all()
        self._reordering = False
        self._siblings, self._sib_idx = [], -1
        self._busy(False)


    def set_compact(self, compact=True):
        """Read-only presentation: no sidebar, editing tools or sibling browsing."""
        self.compact = bool(compact)
        if self.compact:
            self.split.set_show_sidebar(False)
            self.split.set_collapsed(True)
            self.color_rev.set_reveal_child(False)
            self._set_tool("pan")
            self._siblings, self._sib_idx = [], -1
            if getattr(self, "sidebar_btn", None) is not None:
                self.sidebar_btn.set_visible(False)
        else:
            self.split.set_collapsed(False)
            self._update_sidebar_ui()
        self._attach_host()
        self._update_actions()

    # ---- header ------------------------------------------------------------
    def build_header(self, header):
        self._headers_built = True
        self.sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                            tooltip_text="Show Thumbnails (F9)",
                                            focus_on_click=False, visible=False)
        self.split.bind_property("show-sidebar", self.sidebar_btn, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL |
                                 GObject.BindingFlags.SYNC_CREATE)
        header.pack_start(self.sidebar_btn)
        for icon, act, tip in (("object-rotate-left-symbolic", "rotate-left", "Rotate Left (Ctrl+L)"),
                               ("object-rotate-right-symbolic", "rotate-right",
                                "Rotate Right (Ctrl+R)")):
            header.pack_start(Gtk.Button(icon_name=icon, action_name=f"img.{act}",
                                         tooltip_text=tip, focus_on_click=False))

        tools = Gio.Menu()
        sel = Gio.Menu()
        sel.append("Move and Zoom", "img.tool::pan")
        sel.append("Rectangular Selection", "img.tool::rect")
        sel.append("Elliptical Selection", "img.tool::ellipse")
        sel.append("Instant Alpha", "img.tool::alpha")
        tools.append_section(None, sel)
        t2 = Gio.Menu()
        t2.append("Crop to Selection", "img.crop")
        t2.append("Adjust Color…", "img.adjust-color")
        t2.append("Adjust Size…", "img.adjust-size")
        t2.append("Flip Horizontal", "img.flip")
        tools.append_section(None, t2)
        t3 = Gio.Menu()
        t3.append("Undo", "img.undo")
        t3.append("Redo", "img.redo")
        t3.append("Revert to Saved", "img.revert")
        tools.append_section(None, t3)
        t5 = Gio.Menu()
        t5.append("Show Markup Toolbar", "img.markup")
        t5.append("Recognize Text", "img.recognize")
        t5.append("Show Recognized Text", "img.show-text")
        t5.append("Copy All Text", "img.copy-text")
        tools.append_section(None, t5)
        t4 = Gio.Menu()
        t4.append("Slideshow", "img.slideshow")
        if runtime.IS_LINUX or runtime.IS_WINDOWS:     # GNOME background settings / Win32
            t4.append("Set as Wallpaper…", "img.wallpaper")
        tools.append_section(None, t4)
        header.pack_start(Gtk.MenuButton(icon_name="applications-graphics-symbolic",
                                         menu_model=tools, tooltip_text="Tools",
                                         focus_on_click=False))

        # end side, packed right-to-left (the window's menu is already rightmost)
        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append("Save", "img.save")
        sec.append("Save As…", "img.save-as")
        sec.append("Export As…", "img.export-as")
        sec.append("Export as PDF…", "img.export-pdf")
        sec.append("Export Selected Images…", "img.export-selected")
        menu.append_section(None, sec)
        sec2 = Gio.Menu()
        sec2.append("Copy Image", "img.copy")
        sec2.append("Print…", "img.print")
        menu.append_section(None, sec2)
        header.pack_end(Gtk.MenuButton(icon_name="document-save-symbolic", menu_model=menu,
                                       tooltip_text="Save, Export and Print",
                                       focus_on_click=False))
        header.pack_end(Gtk.Button(icon_name="x-office-document-symbolic",
                                   action_name="img.export-pdf", focus_on_click=False,
                                   tooltip_text="Export as PDF…"))
        self.markup_btn = Gtk.ToggleButton(icon_name="document-edit-symbolic",
                                           tooltip_text="Markup (Ctrl+Shift+A)",
                                           focus_on_click=False)
        self.markup_rev.bind_property("reveal-child", self.markup_btn, "active",
                                      GObject.BindingFlags.SYNC_CREATE)
        self.markup_btn.connect("clicked", lambda b: self._toggle_markup(b.get_active()))
        header.pack_end(self.markup_btn)
        try:
            import importlib.util
            if importlib.util.find_spec("preview.markup") is None:
                # no markup toolbar: offer signatures from the header instead
                from .signature import SignatureButton
                header.pack_end(SignatureButton(self.window, self._place_signature))
        except Exception:  # noqa: BLE001 - signature module missing/broken
            pass
        self._update_sidebar_ui()

    # ---- loading -------------------------------------------------------------
    def _items(self):
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]

    def load(self, files):
        self._attach_host()
        new = [ImageItem(f) for f in files if f.get_path()]
        if not new:
            self.toast("Could not open the selected images (only local files are supported)")
            return
        self._load_items(new)
        if len(new) == 1 and not self.compact:
            self._scan_siblings(new[0].gfile)

    def load_texture(self, texture, name=None):
        """Open an in-memory image (e.g. pasted) as an untitled document."""
        data = texture.save_to_png_bytes().get_data()
        im = Image.open(io.BytesIO(data))
        im.load()
        self.add_untitled(im, name)

    def add_untitled(self, pil_image, name=None):
        """Add an in-memory image (e.g. pasted) as a new untitled document."""
        im = pil_image if pil_image.mode in ("RGB", "RGBA") else pil_image.convert(
            "RGBA" if "A" in pil_image.mode or "transparency" in pil_image.info else "RGB")
        if name is None:
            _untitled_counter[0] += 1
            n = _untitled_counter[0]
            name = "Untitled" if n == 1 else f"Untitled {n}"
        item = ImageItem(None, name, im)
        if self.store.get_n_items() == 0:
            self._load_items([item])
            return
        self._siblings, self._sib_idx = [], -1
        self.store.append(item)
        self._request_thumb(item)
        self._update_sidebar_ui()
        self.selection.unselect_all()
        self.selection.select_item(self.store.get_n_items() - 1, True)

    def _load_items(self, new):
        self.store.splice(0, self.store.get_n_items(), new)   # thumbnails load on bind
        self.selection.select_item(0, True)
        self._update_sidebar_ui()
        GLib.idle_add(lambda: (self.canvas.grab_focus(), False)[1])

    def _scan_siblings(self, gfile):
        self._siblings, self._sib_idx = [], -1
        parent = gfile.get_parent()
        if parent is None:
            return
        try:
            names = []
            en = parent.enumerate_children("standard::name,standard::type",
                                           Gio.FileQueryInfoFlags.NONE, None)
            for info in en:
                if info.get_file_type() == Gio.FileType.REGULAR:
                    f = parent.get_child(info.get_name())
                    if kind_of(f) == "image":
                        names.append(info.get_name())
            names.sort(key=convert.natural_key)
            self._siblings = [parent.get_child(n) for n in names]
            base = gfile.get_basename()
            self._sib_idx = names.index(base) if base in names else -1
        except GLib.Error:
            self._siblings, self._sib_idx = [], -1
        self._update_actions()

    def _request_thumb(self, item):
        """Thumbnail in the background (cache → JPEG draft → pixbuf); geometry
        edits are applied so rotated items show rotated."""
        item.thumb_requested = True
        if item.path is None:
            if item.display is not None or item.base is not None:
                item.thumb = pil_to_texture(_thumb_of(item.display or item.base))
            return
        path = item.path
        geo = [o for o in item.ops if o[0] in ("rotate", "flip")]

        multi = self.store.get_n_items() > 1

        def work():
            if item.bound == 0 and multi and item is not self.current:
                item.thumb_requested = False      # scrolled away: re-request on bind
                return
            try:
                im = apply_ops(load_thumb(path, THUMB_PX), geo)
                tex = pil_to_texture(im)
            except Exception:  # noqa: BLE001
                tex = None
            GLib.idle_add(done, tex)

        def done(tex):
            if tex is not None and item.rendered is None and item.path == path:
                item.thumb = tex
            return False
        _pool.submit(work)

    def _on_selection_changed(self, sel, pos, n):
        self._update_sel_label()
        if self._reordering:
            return
        for i in range(pos + n - 1, pos - 1, -1):
            if sel.is_selected(i):
                self._show_item(self.store.get_item(i))
                return

    def _update_sel_label(self):
        n = len(self._selected_positions())
        total = self.store.get_n_items()
        self.sel_label.set_text(f"{n} of {total} selected" if n > 1 else f"{total} images")

    def _update_sidebar_ui(self):
        multi = self.store.get_n_items() > 1 and not self.compact
        if getattr(self, "sidebar_btn", None) is not None:
            self.sidebar_btn.set_visible(multi)
        if multi and not getattr(self, "_sidebar_auto", False):
            self._sidebar_auto = True
            self.split.set_show_sidebar(True)
        if not multi:
            self._sidebar_auto = False
            self.split.set_show_sidebar(False)
        self._update_sel_label()
        self._update_actions()

    def _show_item(self, item):
        self._stop_animation()
        prev = self.current
        if prev is not item:
            self._clear_text()
            if self.mk is not None:
                self.mk.select(None)
        if prev is not None and prev is not item:
            if self._color is not None:
                self._color_close(apply=False)
            self._release(prev)
        self.current = item
        self.refresh_title()
        self._update_actions()
        if item.texture is not None:
            self.canvas.set_content(item.texture, *item.rendered.size, overlay=item.overlay)
            self.refresh_title()
            self._schedule_animation(item)
            return
        self.canvas.clear()
        if item.base is not None:
            self._render(item)
            return
        self._busy(True)
        self._render_gen += 1
        gen = self._render_gen
        path = item.path

        def stale():
            return gen != self._render_gen or self.current is not item

        def work():
            if stale():
                return
            if not item.ops:
                # big JPEG: show a DCT-scaled preview while the full image decodes
                pre = quick_preview(path, DISPLAY_MAX)
                if pre is not None and not stale():
                    size = oriented_size(path) or pre.size
                    GLib.idle_add(preview, pil_to_texture(pre), size)
                if stale():
                    return
            try:
                base, exif, icc = load_base(path)
                if stale():
                    return
                rendered = apply_ops(base, item.ops)
                display = fit_size(rendered, DISPLAY_MAX)
                tex = pil_to_texture(display)
                err = None
            except Exception as e:  # noqa: BLE001
                base = exif = icc = rendered = display = tex = None
                err = e
            GLib.idle_add(done, base, exif, icc, rendered, display, tex, err)

        def preview(tex, size):
            if not stale() and item.texture is None:
                self.canvas.set_content(tex, size[0], size[1])
            return False

        def done(base, exif, icc, rendered, display, tex, err):
            if stale():
                return False
            self._busy(False)
            if base is None:
                item.error = str(err)
                self.toast(f"Could not open {item.name}")
                self.refresh_title()
                return False
            item.base, item.exif, item.icc = base, exif, icc
            item.orig_size = base.size
            self._shown(item, rendered, display, tex, keep_view=True)
            return False
        _loader.submit(work)

    def _busy(self, on):
        self.spinner.set_visible(on)

    def _render(self, item, keep_view=True):
        """Re-run the op pipeline for `item` (the current one) in a thread."""
        self._stop_animation()          # restarted by _shown() with the new edits
        self._render_gen += 1
        gen = self._render_gen
        ops = list(item.ops)
        base = item.base
        state = {"done": False}
        GLib.timeout_add(200, lambda: (None if state["done"] or gen != self._render_gen
                                       else self._busy(True), False)[1])

        def work():
            try:
                im = apply_ops(base, ops)
                display = fit_size(im, DISPLAY_MAX)
                tex = pil_to_texture(display)
                err = None
            except Exception as e:  # noqa: BLE001
                im = display = tex = None
                err = e
            GLib.idle_add(done, im, display, tex, err)

        def done(im, display, tex, err):
            state["done"] = True
            if gen != self._render_gen:
                return False
            self._busy(False)
            if err is not None:
                import traceback
                traceback.print_exception(err)
                self.toast(f"Edit failed: {err}")
                return False
            if item is self.current:
                self._shown(item, im, display, tex, keep_view)
            return False
        threading.Thread(target=work, daemon=True).start()

    def _shown(self, item, rendered, display, tex, keep_view):
        item.rendered, item.display, item.texture = rendered, display, tex
        self._tiles = None
        self.canvas.set_content(tex, rendered.size[0], rendered.size[1], overlay=item.overlay,
                                keep_view=keep_view)
        self.canvas._schedule_detail()
        if item.ops or item.path is None:
            item.thumb = pil_to_texture(_thumb_of(display))
        elif item.thumb is None or getattr(item, "_thumb_edited", False):
            item.thumb = pil_to_texture(_thumb_of(display))
        item._thumb_edited = bool(item.ops)
        self._schedule_ocr(item)
        self._schedule_animation(item)
        self.refresh_title()
        self._update_actions()

    # ---- animated GIF / WebP / APNG -------------------------------------
    ANIM_EXTS = ANIM_EXTS
    ANIM_BUDGET = 512 * 2**20       # bytes of decoded frames we are willing to hold

    def _stop_animation(self):
        self._anim_gen = getattr(self, "_anim_gen", 0) + 1
        self._anim_playing = False
        src = getattr(self, "_anim_src", None)
        if src:
            GLib.source_remove(src)
        self._anim_src = None

    def _schedule_animation(self, item):
        """Play animated images with the item's edits applied to every frame. Runs
        after each (re)render; the edited first frame is already on screen, the
        animation replaces it when its frames are ready. Newer edits stale it out."""
        self._stop_animation()
        if (item.path is None or self._color is not None or
                Path(item.path).suffix.lower() not in self.ANIM_EXTS):
            return
        gen = self._anim_gen
        path = item.path
        ops = list(item.ops)

        def work():
            frames, budget = [], self.ANIM_BUDGET
            try:
                if anim_probe(path) is None:
                    return
                for fr, dur in iter_frames(path):
                    if gen != self._anim_gen:
                        return
                    fr = fit_size(apply_ops(fr, ops), DISPLAY_MAX)
                    budget -= fr.width * fr.height * 4
                    if budget < 0:
                        break           # too long to hold: loop over the frames that fit
                    frames.append((pil_to_texture(fr), max(20, dur or 100)))
            except Exception:  # noqa: BLE001 - a broken animation still shows frame 1
                return
            if len(frames) > 1 and gen == self._anim_gen:
                GLib.idle_add(start, frames)

        def alive():
            return gen == self._anim_gen and self.current is item and self._color is None

        def start(frames):
            if not alive():
                return False
            state = {"i": 0}
            self._anim_playing = True

            def tick():
                self._anim_src = None
                if not alive() or item.rendered is None:
                    return False
                state["i"] = (state["i"] + 1) % len(frames)
                tex, dur = frames[state["i"]]
                self.canvas.set_content(tex, item.rendered.size[0], item.rendered.size[1],
                                        overlay=item.overlay, keep_view=True)
                self._anim_src = GLib.timeout_add(dur, tick)
                return False

            self._anim_src = GLib.timeout_add(frames[0][1], tick)
            return False

        threading.Thread(target=work, daemon=True).start()

    def _on_need_detail(self, _c, x0, y0, x1, y1, scale):
        it = self.current
        if (it is None or it.rendered is None or self._color is not None or
                getattr(self, "_anim_playing", False)):
            return
        self._detail_gen += 1
        gen = self._detail_gen
        im = it.rendered
        if scale < 1:      # downscale to exactly this many device px (canvas draws it 1:1)
            box = (x0, y0, x1, y1)
            size = (max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale)))
        else:
            box = (int(x0), int(y0), int(math.ceil(x1)), int(math.ceil(y1)))
        key = (tuple(round(v, 4) for v in box), round(scale, 6))
        if self._tiles is None or self._tiles[0] is not im:
            self._tiles = (im, [])             # tiles of the current render only
        tiles = self._tiles[1]
        for i, (k, tex) in enumerate(tiles):
            if k == key:
                tiles.insert(0, tiles.pop(i))
                self.canvas.set_detail(tex, box, scale)
                return

        def work():
            if gen != self._detail_gen:
                return
            if scale < 1:
                src = im if im.mode in ("RGB", "RGBA", "L", "LA") else \
                    im.convert("RGBA" if "A" in im.mode else "RGB")
                tile = src.resize(size, Image.LANCZOS, box=box, reducing_gap=3.0)
            else:
                tile = im.crop(box)
            tex = pil_to_texture(tile)
            GLib.idle_add(done, tex)

        def done(tex):
            if (gen == self._detail_gen and self.current is it and it.rendered is im and
                    not getattr(self, "_anim_playing", False)):
                self.canvas.set_detail(tex, box, scale)
                if self._tiles is not None and self._tiles[0] is im:
                    tiles = self._tiles[1]
                    tiles.insert(0, (key, tex))
                    used = 0
                    for i, (_k, t) in enumerate(tiles):
                        used += t.get_width() * t.get_height() * 4
                        if used > TILE_CACHE:
                            del tiles[max(i, 1):]
                            break
            return False
        _loader.submit(work)

    def _release(self, item):
        if self._tiles is not None and self._tiles[0] is item.rendered:
            self._tiles = None
        item.rendered = item.display = item.texture = None
        if not item.keep_base:
            item.base = None

    def _on_view_changed(self, *_):
        if self.canvas.texture is None:
            self.zoom_btn.set_label("—")
        else:
            self.zoom_btn.set_label(f"{round(self.canvas.eff_zoom() * 100)}%")

    def _zoom_toggle(self):
        if self.canvas.fit and self.canvas.fit_zoom() < 1:
            self.canvas.zoom_to(1.0)
        else:
            self.canvas.zoom_fit()

    # ---- title / info --------------------------------------------------------------
    def get_title(self):
        it = self.current
        if it is None:
            return "View"
        return it.name + (" — Edited" if (it.ops or it.overlay or it.markup) else "")

    def get_subtitle(self):
        it = self.current
        if it is None or it.rendered is None:
            return ""
        w, h = it.rendered.size
        s = f"{w} × {h}"
        n = self.store.get_n_items()
        if n > 1:
            found, pos = self.store.find(it)
            s += f"  ·  {(pos + 1) if found else 1} of {n}"
        elif len(self._siblings) > 1 and self._sib_idx >= 0:
            s += f"  ·  {self._sib_idx + 1} of {len(self._siblings)}"
        return s

    def get_current_file(self):
        return self.current.gfile if self.current is not None else None

    def get_info(self):
        it = self.current
        if it is None:
            return []
        out = []
        if it.gfile is not None:
            out.append(file_info_section(it.gfile))
        else:
            out.append(("File", [("File name", it.name), ("Kind", "Untitled image (not saved)")]))
        size = it.orig_size
        secs = image_info_sections(it.path, size) if it.path else (
            [("Image", [("Dimensions", f"{size[0]} × {size[1]} px")])] if size else [])
        if it.rendered is not None and it.final_ops() and secs:
            w, h = it.rendered.size
            secs[0][1].insert(1, ("Edited size", f"{w} × {h} px"))
            secs[0][1].insert(2, ("Edits", ", ".join(o[0] for o in it.final_ops())))
        return out + secs

    def has_unsaved_changes(self):
        return any(i.dirty for i in self._items())

    # ---- drop in -----------------------------------------------------------------
    def accepts_drop(self, files):
        return (not self.compact) and bool(files) and all(kind_of(f) == "image" for f in files)

    def add_dropped(self, files):
        had = self.store.get_n_items()
        new = [ImageItem(f) for f in files if f.get_path() and kind_of(f) == "image"]
        if not new:
            return
        self._siblings, self._sib_idx = [], -1
        self.store.splice(self.store.get_n_items(), 0, new)
        self._update_sidebar_ui()
        if had == 0:
            self.selection.select_item(0, True)
        self.toast(f"Added {len(new)} image{'s' if len(new) != 1 else ''}")

    def _add_dialog(self):
        fd = Gtk.FileDialog(title="Add Images")
        f = Gtk.FileFilter(name="Images")
        for e in sorted(x[1:] for x in IMAGE_EXTS):
            f.add_suffix(e)
        fd.set_default_filter(f)

        def done(fd_, res):
            try:
                lst = fd_.open_multiple_finish(res)
            except GLib.Error:
                return
            self.add_dropped([lst.get_item(i) for i in range(lst.get_n_items())])
        fd.open_multiple(self._root(), None, done)

    # ---- navigation ---------------------------------------------------------------------
    def _step(self, delta, wrap=False):
        n = self.store.get_n_items()
        if n > 1:
            sel = self._selected_positions()
            cur = self.store.find(self.current)[1] if self.current else 0
            tgt = cur + delta
            tgt = tgt % n if wrap else max(0, min(n - 1, tgt))
            if tgt != cur or len(sel) > 1:
                self.selection.unselect_all()
                self.selection.select_item(tgt, True)
                self.listview.scroll_to(tgt, Gtk.ListScrollFlags.NONE, None)
            return
        if len(self._siblings) > 1 and self._sib_idx >= 0:
            if self.current and self.current.final_ops():
                self.toast("Save or revert your changes before browsing")
                return
            idx = self._sib_idx + delta
            if wrap:
                idx %= len(self._siblings)
            if not 0 <= idx < len(self._siblings):
                return
            self._sib_idx = idx
            item = ImageItem(self._siblings[idx])
            self._request_thumb(item)
            self.store.splice(0, 1, [item])
            self.selection.select_item(0, True)

    def _select_all(self):
        if self.store.get_n_items() > 1:
            self.selection.select_all()
            self.split.set_show_sidebar(True)

    def _toggle_sidebar(self):
        if self.store.get_n_items() > 1:
            self.split.set_show_sidebar(not self.split.get_show_sidebar())

    def _delete_key(self):
        if self.mk is not None and self.mk.delete_selected():
            return
        if self.current is not None and self.current.overlay:
            self._discard_overlay()
        else:
            self._remove_selected()

    def _remove_selected(self):
        n = self.store.get_n_items()
        pos = self._selected_positions() or (
            [self.store.find(self.current)[1]] if self.current else [])
        if not pos or n <= 1:
            return
        if len(pos) >= n:
            self.toast("At least one image has to stay open")
            return
        self._reordering = True
        for p in sorted(pos, reverse=True):
            self.store.remove(p)
        self._reordering = False
        new_pos = min(pos[0], self.store.get_n_items() - 1)
        self.current = None
        self.selection.unselect_all()
        self.selection.select_item(new_pos, True)
        self._update_sidebar_ui()

    # ---- edits / history -----------------------------------------------------------------
    @staticmethod
    def _snap(it):
        return (list(it.ops), [m.to_dict() for m in it.markup])

    def _restore(self, it, snap):
        ops, mk = snap
        changed = ops != it.ops
        it.ops = list(ops)
        if mk or it.markup:
            from .markup import Item as MkItem
            it.markup[:] = [MkItem.from_dict(d) for d in mk]
            if self.canvas.markup is not None:
                self.canvas.markup.select(None)
        self._after_edit(it, render=changed)

    def _edit(self, it, new_ops):
        it.history.append(self._snap(it))
        it.ops = new_ops
        it.future = []
        self._after_edit(it)

    def _after_edit(self, it, render=True):
        it.update_dirty()
        if it is self.current:
            self.canvas.sel = None
            if render:
                self._clear_text()
                if it.base is not None:
                    self._render(it)
            self.canvas.queue_draw()
        elif render and all(o[0] in ("rotate", "flip") for o in it.ops):
            self._request_thumb(it)
        self.refresh_title()
        self._update_actions()

    def _commit_overlay(self, it):
        """Flatten the live signature overlay and markup into the op list
        (before edits that change geometry or pixels)."""
        if it is None or not (it.overlay or it.markup):
            return
        extra = []
        if it.overlay:
            extra.append(_sig_op(it.overlay))
        if it.markup:
            extra.append(("markup", [m.to_dict() for m in it.markup]))
        snap = self._snap(it)
        it.overlay = None
        self.canvas.overlay = None
        it.markup.clear()
        if self.canvas.markup is not None:
            self.canvas.markup.select(None)
        it.history.append(snap)
        it.future = []
        it.ops = it.ops + extra
        self._after_edit(it)

    def _target_items(self):
        pos = self._selected_positions()
        items = [self.store.get_item(p) for p in pos] if len(pos) > 1 else []
        return items or ([self.current] if self.current else [])

    def _transform(self, op):
        for it in self._target_items():
            if it.error:
                continue
            self._commit_overlay(it)
            self._edit(it, _append_op(it.ops, op))
        self.canvas.overlay = self.current.overlay if self.current else None

    def _undo(self):
        it = self.current
        if it is None:
            return
        if it.overlay:
            self._discard_overlay()
            return
        if not it.history:
            return
        it.future.append(self._snap(it))
        self._restore(it, it.history.pop())

    def _redo(self):
        it = self.current
        if it is None or not it.future:
            return
        it.history.append(self._snap(it))
        self._restore(it, it.future.pop())

    def _revert(self):
        it = self.current
        if it is None or not (it.ops or it.overlay or it.markup):
            return
        it.overlay = None
        self.canvas.overlay = None
        it.history.append(self._snap(it))
        it.future = []
        it.markup.clear()
        if self.canvas.markup is not None:
            self.canvas.markup.select(None)
        changed = bool(it.ops)
        it.ops = []
        self._after_edit(it, render=changed)
        self.toast("Reverted to the saved version", "Undo", self._undo)

    # ---- tools ------------------------------------------------------------------------------
    def _set_tool(self, tool):
        if self.compact:
            tool = "pan"
        self._acts["tool"].set_state(GLib.Variant("s", tool))
        self.canvas.set_tool(tool)
        hints = {"rect": "Drag to select an area · Ctrl+K crops",
                 "ellipse": "Drag to select an ellipse · Ctrl+K crops",
                 "alpha": "Click a colour to make it transparent"}
        self.tool_label.set_text(hints.get(tool, ""))
        self.tol_scale.set_visible(tool == "alpha")
        self.crop_btn.set_visible(tool in ("rect", "ellipse"))
        self.tool_bar.set_reveal_child(tool != "pan")
        if tool != "pan":
            self.canvas.grab_focus()
        self._update_actions()

    def _crop(self):
        it, sel = self.current, self.canvas.sel
        if it is None or it.rendered is None:
            return
        if not sel:
            self.toast("Select an area first (Tools ▸ Rectangular Selection)")
            return
        self._commit_overlay(it)
        w, h = it.rendered.size
        x0, y0 = max(0, int(round(sel[0]))), max(0, int(round(sel[1])))
        x1, y1 = min(w, int(round(sel[2]))), min(h, int(round(sel[3])))
        if x1 - x0 < 1 or y1 - y0 < 1:
            return
        kind = "ellipse" if self.canvas.tool == "ellipse" else "crop"
        self.canvas.sel = None
        self._edit(it, it.ops + [(kind, x0, y0, x1, y1)])
        self._set_tool("pan")

    def _on_alpha_click(self, _c, x, y):
        it = self.current
        if it is None or it.rendered is None:
            return
        self._commit_overlay(it)
        self._edit(it, it.ops + [("alpha", int(x), int(y), float(self.tol_scale.get_value()))])

    def _cancel(self):
        """Escape: back out of the innermost tool or state.  Returns False when
        there was nothing to cancel (the screenshot editor then closes)."""
        if self.mk is not None and self.mk.escape():
            self.canvas.queue_draw()
            return True
        if self.canvas.text is not None and self.canvas.text.has_selection():
            self.canvas.text.clear()
            self.canvas.queue_draw()
            return True
        if self._slideshow is not None:
            self._stop_slideshow()
        elif self._color is not None:
            self._color_close(apply=False)
        elif self.canvas.sel:
            self.canvas.sel = None
            self.canvas.queue_draw()
            self._update_actions()
        elif self.current is not None and self.current.overlay:
            self._discard_overlay()
        elif self.canvas.tool != "pan":
            self._set_tool("pan")
        elif isinstance(self.window, Gtk.Window) and self.window.is_fullscreen():
            self.window.unfullscreen()
        else:
            return False
        return True

    def _place_signature(self, sig):
        it = self.current
        if it is None or it.rendered is None:
            return
        from .signature import signature_aspect, signature_png_bytes
        self._commit_overlay(it)
        dw, dh = it.rendered.size
        w = min(dw * 0.35, 900)
        h = w * signature_aspect(sig)
        tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(signature_png_bytes(sig, 800)))
        it.overlay = {"sig": sig, "tex": tex, "cx": dw / 2, "cy": dh * 0.8, "w": w, "h": h}
        it.update_dirty()
        self.canvas.overlay = it.overlay
        self.canvas.queue_draw()
        self.refresh_title()
        self._update_actions()
        self.toast("Drag to move, drag the corner to resize. Backspace removes it.")

    def _discard_overlay(self):
        it = self.current
        if it and it.overlay:
            it.overlay = None
            it.update_dirty()
            self.canvas.overlay = None
            self.canvas.queue_draw()
            self.refresh_title()
            self._update_actions()

    # ---- markup (Ctrl+Shift+A) -----------------------------------------------------------
    def _ensure_markup(self):
        if self.mk is not None:
            return True
        try:
            from .markup import MarkupController, MarkupToolbar
        except Exception as e:  # noqa: BLE001
            self.toast(f"Markup is unavailable: {e}")
            return False
        self.mk = MarkupController(self)
        extra = ()
        try:
            from .signature import SignatureButton
            extra = (SignatureButton(self.window, self._place_signature),)
        except Exception:  # noqa: BLE001
            pass
        self.mk_bar = MarkupToolbar(self.mk, extra=extra, text_tools=False)
        if hasattr(self.mk_bar, "pad_btn"):
            self.mk_bar.pad_btn.connect("toggled", self._on_pad_toggled)
        self.markup_rev.set_child(self.mk_bar)
        self.canvas.markup = self.mk
        self.canvas.markup_items = lambda: self.current.markup if self.current else []
        return True

    def _toggle_markup(self, show=None):
        if self.compact:
            return
        show = (not self.markup_rev.get_reveal_child()) if show is None else show
        if show and not self._ensure_markup():
            return
        self.markup_rev.set_reveal_child(show)
        if self.mk is not None and not show:
            self.mk.set_tool("select")
            self.mk.select(None)
        if show:
            self._set_tool("pan")
        self.canvas.queue_draw()

    def _on_pad_toggled(self, btn):
        if btn.get_active():
            if self.mk.pad is None:
                err = self.mk.start_touchpad("img")
                if err:
                    btn.set_active(False)
                    self.toast(f"Touchpad drawing unavailable: {err}")
                else:
                    self.toast("Draw with one finger on the touchpad · any key to finish")
        elif self.mk.pad is not None:
            self.mk.pad.stop()

    # host protocol for markup.MarkupController (single page key "img")
    def mk_page_at(self, wx, wy):
        if self.current is None or self.current.rendered is None:
            return None
        x, y = self.canvas.to_img(wx, wy)
        w, h = self.current.rendered.size
        if -8 <= x <= w + 8 and -8 <= y <= h + 8:
            return ("img", x, y)
        return None

    def mk_to_page(self, key, wx, wy):
        return self.canvas.to_img(wx, wy)

    def mk_to_widget(self, key, x, y):
        return self.canvas.to_widget(x, y)

    def mk_scale(self, key):
        return self.canvas._scale()

    def mk_items(self, key):
        return self.current.markup if self.current is not None else []

    def mk_page_size(self, key):
        it = self.current
        return it.rendered.size if it is not None and it.rendered is not None else (1, 1)

    def mk_keys(self):
        return ["img"] if self.current is not None else []

    def mk_begin_edit(self):
        it = self.current
        if it is not None:
            it.history.append(self._snap(it))
            it.future = []

    def mk_changed(self):
        it = self.current
        if it is not None:
            it.update_dirty()
        self.canvas.queue_draw()
        self.refresh_title()
        self._update_actions()

    def mk_widget(self):
        return self.canvas

    def mk_set_popover(self, pop, old=None):
        if pop is not None:
            self.canvas.add_popover(pop)
        elif old is not None:
            self.canvas.remove_popover(old)

    def mk_visible_rect(self, key):
        it = self.current
        w, h = self.mk_page_size(key)
        x0, y0 = self.canvas.to_img(0, 0)
        x1, y1 = self.canvas.to_img(self.canvas.get_width(), self.canvas.get_height())
        x0, y0 = max(0.0, x0), max(0.0, y0)
        x1, y1 = min(float(w), x1), min(float(h), y1)
        return x0, y0, max(x1 - x0, 1), max(y1 - y0, 1)

    def mk_tool_changed(self, tool):
        if tool != "select":
            self._set_tool("pan")
        self.canvas.queue_draw()

    def mk_touchpad_stopped(self):
        if self.mk_bar is not None and self.mk_bar.pad_btn.get_active():
            self.mk_bar.pad_btn.set_active(False)

    # ---- Live Text (OCR) ------------------------------------------------------------------
    def _clear_text(self):
        o = self._ocr
        if o is not None:
            if o.get("timer"):
                GLib.source_remove(o["timer"])
            o["cancel"].cancel()
        self._ocr = None
        if self.canvas.text is not None:
            self.canvas.text = None
            self.canvas.queue_draw()

    def _schedule_ocr(self, item, delay=700):
        """Recognise text shortly after an image is shown (like Live Text)."""
        if not self.live_text:
            return
        try:
            from . import ocr
            if not ocr.is_available():
                from . import ocr_install
                ocr_install.maybe_offer_once(self, lambda: self._run_ocr(force=True))
                return
        except Exception:  # noqa: BLE001
            return
        self._clear_text()
        cancel = Gio.Cancellable()
        o = {"item": item, "cancel": cancel, "timer": 0}
        o["timer"] = GLib.timeout_add(delay, lambda: (self._run_ocr(o), False)[1])
        self._ocr = o

    def _run_ocr(self, o=None, force=False):
        it = self.current
        if it is None or it.display is None:
            return
        try:
            from . import ocr
        except Exception as e:  # noqa: BLE001
            if force:
                self.toast(f"Text recognition is unavailable: {e}")
            return
        if not ocr.is_available():
            if force:
                from . import ocr_install
                ocr_install.offer(self, lambda: self._run_ocr(force=True))
            return
        if o is None:
            self._clear_text()
            o = {"item": it, "cancel": Gio.Cancellable(), "timer": 0}
            self._ocr = o
        o["timer"] = 0
        if o["item"] is not it or o["cancel"].is_cancelled():
            return
        display = it.display
        k = it.rendered.size[0] / max(1, display.size[0])

        def done(lines, err):
            if self._ocr is not o or self.current is not it or it.display is not display:
                return
            if err is not None:
                if force:
                    self.toast(f"Text recognition failed: {err}")
                return
            ov = ocr.TextOverlay(lines, display.size)
            ov.on_change = self.canvas.queue_draw
            self.canvas.text = ov
            self.canvas.text_k = k
            if force:
                self.toast(f"Recognized {len(lines)} line{'s' if len(lines) != 1 else ''} of text"
                           if lines else "No text found")
                if lines:
                    ov.set_show_boxes(True)
            self._update_actions()
        ocr.recognize_async(display, done, o["cancel"])

    def _toggle_text_boxes(self):
        t = self.canvas.text
        if t is not None:
            t.set_show_boxes(not t.show_boxes)

    def _copy_all_text(self):
        t = self.canvas.text
        if t is None or not t.has_text():
            self.toast("No recognized text")
            return
        from . import ocr
        self.get_clipboard().set(ocr.lines_to_text(t.lines))
        self.toast("All text copied")

    # ---- adjust colour ------------------------------------------------------------------------
    def _color_open(self):
        it = self.current
        if it is None or it.rendered is None or it.base is None or self.compact:
            return
        if self._color is not None:
            return
        self._commit_overlay(it)
        if it.ops and it.ops[-1][0] == "color":
            params = dict(COLOR_DEFAULTS, **it.ops[-1][1])
            base_ops = it.ops[:-1]
        else:
            params = dict(COLOR_DEFAULTS)
            base_ops = list(it.ops)
        self._color = {"item": it, "base_ops": base_ops, "src": None, "gen": 0,
                       "busy": False, "pending": False, "syncing": True}
        self._stop_animation()      # live preview is single-frame; resumes on close
        for k, sc in self.color_scales.items():
            sc.set_value(params[k])
        self.auto_btn.set_active(bool(params.get("auto")))
        self._color["syncing"] = False
        self.canvas.detail_enabled = False
        self.canvas.detail = None
        self.color_rev.set_reveal_child(True)
        base = it.base
        rendered = it.display if base_ops == it.ops else None

        def work():
            im = rendered if rendered is not None else apply_ops(base, base_ops)
            small = fit_size(im, PREVIEW_PX)
            GLib.idle_add(ready, small)

        def ready(small):
            if self._color is not None and self._color["item"] is it:
                self._color["src"] = small
                self._color_changed()
            return False
        threading.Thread(target=work, daemon=True).start()

    def _color_params(self):
        p = {k: round(sc.get_value(), 3) for k, sc in self.color_scales.items()}
        p["auto"] = self.auto_btn.get_active()
        return p

    def _color_changed(self):
        c = self._color
        if c is None or c["syncing"] or c["src"] is None:
            return
        if c["busy"]:
            c["pending"] = True
            return
        c["busy"] = True
        c["pending"] = False
        params = self._color_params()
        src = c["src"]
        it = c["item"]

        def work():
            try:
                tex = pil_to_texture(adjust_color(src, params))
            except Exception:  # noqa: BLE001
                tex = None
            GLib.idle_add(done, tex)

        def done(tex):
            if self._color is not c:
                return False
            c["busy"] = False
            if tex is not None and it is self.current and it.rendered is not None:
                self.canvas.set_content(tex, *it.rendered.size, overlay=None, keep_view=True)
            if c["pending"]:
                self._color_changed()
            return False
        threading.Thread(target=work, daemon=True).start()

    def _color_reset(self):
        if self._color is None:
            return
        self._color["syncing"] = True
        for sc in self.color_scales.values():
            sc.set_value(0)
        self.auto_btn.set_active(False)
        self._color["syncing"] = False
        self._color_changed()

    def _color_close(self, apply):
        c = self._color
        if c is None:
            return
        params = self._color_params()
        self._color = None
        self.canvas.detail_enabled = True
        self.color_rev.set_reveal_child(False)
        it = c["item"]
        new_ops = c["base_ops"] + ([] if color_is_neutral(params) else [("color", params)])
        if apply and new_ops != it.ops:
            self._edit(it, new_ops)
        elif it is self.current and it.texture is not None:
            self.canvas.set_content(it.texture, *it.rendered.size, overlay=it.overlay,
                                    keep_view=True)
            self._schedule_animation(it)

    # ---- copy ---------------------------------------------------------------------------------
    def _set_clip(self, texture):
        # typed as GdkTexture: a GdkMemoryTexture GValue finds no serializers,
        # so other apps would see an empty clipboard
        self.get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(GObject.Value(Gdk.Texture, texture)))

    def _copy(self):
        t = self.canvas.text
        if t is not None and t.has_selection():
            self.get_clipboard().set(t.selected_text())
            self.toast("Text copied")
            return
        if self.mk is not None and self.mk.copy_selected():
            return
        it = self.current
        if it is None or it.rendered is None:
            return
        sel = self.canvas.sel
        if it.overlay or sel:
            rendered, ov = it.rendered, it.overlay
            shape = self.canvas.tool

            def work():
                im = rendered
                if ov:
                    im = apply_ops(im, [_sig_op(ov)])
                if sel:
                    box = tuple(int(round(v)) for v in sel)
                    im = apply_ops(im, [("ellipse" if shape == "ellipse" else "crop",) + box])
                return pil_to_texture(im)

            def done(tex, err):
                if err:
                    self.toast(f"Copy failed: {err}")
                else:
                    self._set_clip(tex)
                    self.toast("Selection copied" if sel else "Image copied")
            self._run_bg(work, done)
        else:
            full = it.rendered
            self._run_bg(lambda: pil_to_texture(full),
                         lambda tex, err: (self._set_clip(tex), self.toast("Image copied"))
                         if err is None else self.toast(f"Copy failed: {err}"))

    # ---- save / export ----------------------------------------------------------------------------
    def _run_bg(self, fn, done):
        def work():
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                res, err = None, e
            GLib.idle_add(lambda: (done(res, err), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _anim_progress(self, k, n):
        """Frame progress from a save worker -> spinner tooltip (throttled)."""
        if k == n or k % max(1, n // 20) == 0:
            GLib.idle_add(lambda: (self.spinner.set_tooltip_text(f"Saving frame {k} of {n}"),
                                   False)[1])

    def _write_item(self, it, dest, quality=None, max_px=0, export=False):
        """Write `it` with every edit to `dest` (call from a worker thread).
        Animated GIF / WebP / APNG sources keep their animation when `dest` can hold
        one, streaming frame by frame at full resolution. Returns "anim", "still", or
        "flat" (an animation written as its first frame only)."""
        ext = Path(dest).suffix.lower()
        info = anim_probe(it.path) if it.path else None
        if info and anim_dest_ok("" if export else Path(it.path).suffix, ext):
            if info["n"] >= 12:
                GLib.idle_add(lambda: (self.toast(f"Saving {info['n']}-frame animation…"),
                                       False)[1])
            save_animated(it.path, it.final_ops(), dest, quality if export else None,
                          max_px, progress=self._anim_progress)
            return "anim"
        im, exif, icc = it.final_image()
        if max_px and max(im.size) > max_px:
            s_ = max_px / max(im.size)
            im = im.resize((max(1, round(im.width * s_)), max(1, round(im.height * s_))),
                           Image.LANCZOS, reducing_gap=3.0)
        save_any(im, exif, icc, dest, 92 if quality is None else quality)
        return "flat" if info else "still"

    def _run_saving(self, fn, done):
        """_run_bg with the busy spinner shown for the duration."""
        self._busy(True)
        self.spinner.set_tooltip_text("Saving…")

        def finished(res, err):
            self._busy(False)
            self.spinner.set_tooltip_text(None)
            done(res, err)
        self._run_bg(fn, finished)

    @staticmethod
    def _dest_for(it):
        """In-place path if the format can hold the edits, else a new PNG."""
        ext = Path(it.path).suffix.lower()
        ok = ext in WRITABLE or ext in PIXBUF_SAVE
        if ok and ops_need_alpha(it.final_ops()) and ext not in ALPHA_FORMATS | {".avif"}:
            ok = False
        return it.path if ok else str(convert.unique_path(Path(it.path).with_suffix(".png")))

    def save_changes(self, on_done=None):
        """Save every edited image (formats that can't hold the edits become PNG).
        Untitled images go through Save As. on_done(ok) when finished."""
        dirty = [i for i in self._items() if i.final_ops() and i.path]
        untitled = [i for i in self._items() if i.path is None]

        def after_files(ok):
            if not ok:
                if on_done:
                    on_done(False)
                return
            self._save_untitled(untitled, on_done)

        if not dirty:
            after_files(True)
            return

        def job():
            out = []
            for it in dirty:
                dest = self._dest_for(it)
                out.append((it, dest, self._write_item(it, dest)))
            return out

        def done(res, err):
            if err:
                self.toast(f"Could not save: {err}")
                after_files(False)
                return
            moved = [d for it, d, _k in res if d != it.path]
            for it, dest, _k in res:
                self._reset_item(it, dest)
            if moved:
                self.toast(f"Saved as {os.path.basename(moved[0])} (the original format "
                           "can't keep these edits)")
            else:
                self.toast("Saved " + (os.path.basename(res[0][1]) if len(res) == 1
                                       else f"{len(res)} images") +
                           (" (first frame only; the format can't animate)"
                            if any(k == "flat" for _i, _d, k in res) else ""))
            after_files(True)
        self._run_saving(job, done)

    def _save_untitled(self, items, on_done):
        if not items:
            if on_done:
                on_done(True)
            return
        it = items[0]
        self._save_item_as(it, lambda ok: self._save_untitled(items[1:], on_done) if ok
                           else (on_done(False) if on_done else None))

    def _reset_item(self, it, dest=None):
        if dest and dest != it.path:
            it.gfile = Gio.File.new_for_path(dest)
            it.path = dest
            it.name = os.path.basename(dest)
        it.ops, it.history, it.future, it.overlay = [], [], [], None
        it.markup.clear()
        it.keep_base = False
        it.base = it.rendered = it.display = it.texture = None
        it.update_dirty()
        self._request_thumb(it)
        if it is self.current:
            self.current = None
            self._show_item(it)
        self._note_recent(it.gfile)
        self.refresh_title()
        self._update_actions()

    def _note_recent(self, gfile):
        try:
            if gfile is not None:
                Gtk.RecentManager.get_default().add_item(gfile.get_uri())
        except Exception:  # noqa: BLE001
            pass

    def _file_dialog_save(self, title, name, cb, folder=None):
        fd = Gtk.FileDialog(title=title, initial_name=name)
        folder = folder or (os.path.dirname(self.current.path)
                            if self.current and self.current.path else
                            GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES))
        if folder:
            fd.set_initial_folder(Gio.File.new_for_path(folder))

        def done(fd_, res):
            try:
                gf = fd_.save_finish(res)
            except GLib.Error:
                cb(None)
                return
            cb(gf.get_path())
        fd.save(self._root(), None, done)

    def _save_as(self):
        if self.current is not None:
            self._save_item_as(self.current)

    def _save_item_as(self, it, on_done=None):
        if it.path:
            ext = Path(it.path).suffix.lower()
            ok_ext = ext in WRITABLE or ext in PIXBUF_SAVE
            name = it.name if ok_ext else Path(it.name).stem + ".png"
            if ok_ext and ops_need_alpha(it.final_ops()) and ext not in ALPHA_FORMATS:
                name = Path(it.name).stem + ".png"
        else:
            name = it.name + ".png"

        def go(path):
            if not path:
                if on_done:
                    on_done(False)
                return
            if Path(path).suffix.lower() not in WRITABLE and Path(path).suffix.lower() not in PIXBUF_SAVE:
                path += ".png"

            def job():
                return path, self._write_item(it, path)

            def done(res, err):
                if err:
                    self.toast(f"Could not save: {err}")
                    if on_done:
                        on_done(False)
                    return
                res, kind = res
                it.gfile = Gio.File.new_for_path(res)
                it.path, it.name = res, os.path.basename(res)
                self._reset_item(it)
                self.toast(f"Saved {it.name}" + (" (first frame only; this format can't "
                                                 "animate)" if kind == "flat" else ""))
                if on_done:
                    on_done(True)
            self._run_saving(job, done)
        self._file_dialog_save("Save As", name, go)

    def _format_dialog(self, title, formats, ok_label, on_ok, extra=None):
        """Small Adw.Dialog with Format + Quality (+ optional extra rows)."""
        dlg = Adw.Dialog(title=title, content_width=420)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dlg.close())
        ok = Gtk.Button(label=ok_label, css_classes=["suggested-action"])
        hb.pack_start(cancel)
        hb.pack_end(ok)
        tv.add_top_bar(hb)
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        page.add(g)
        fmt_row = Adw.ComboRow(title="Format", model=Gtk.StringList.new([f[0] for f in formats]))
        q_row = Adw.SpinRow(title="Quality", sensitive=False,
                            adjustment=Gtk.Adjustment(value=90, lower=1, upper=100,
                                                      step_increment=1, page_increment=10))
        g.add(fmt_row)
        g.add(q_row)
        fmt_row.connect("notify::selected",
                        lambda *_: q_row.set_sensitive(formats[fmt_row.get_selected()][2]))
        rows = extra(page, g) if extra else {}
        progress = Gtk.ProgressBar(visible=False, margin_start=12, margin_end=12,
                                   margin_bottom=12, show_text=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.set_vexpand(True)
        box.append(page)
        box.append(progress)
        tv.set_content(box)
        dlg.set_child(tv)
        ctx = {"dlg": dlg, "fmt": fmt_row, "q": q_row, "progress": progress, "ok": ok,
               "cancel": cancel, **rows}
        ok.connect("clicked", lambda *_: on_ok(ctx, formats[fmt_row.get_selected()],
                                               int(q_row.get_value())))
        dlg.present(self._parent_widget())
        return ctx

    def _export_as(self):
        it = self.current
        if it is None or it.rendered is None:
            return

        def on_ok(ctx, fmt, quality):
            _label, ext, _q = fmt
            ctx["dlg"].close()

            def chosen(path):
                if not path:
                    return
                if Path(path).suffix.lower() not in ({ext} | ({".jpeg", ".jpg"} if ext == ".jpg" else set())
                                                      | ({".tiff"} if ext == ".tif" else set())):
                    path += ext

                def job():
                    return path, self._write_item(it, path, quality, export=True)

                def done(res, err):
                    if err:
                        self.toast(f"Could not export: {err}")
                    else:
                        self.toast(f"Exported {os.path.basename(res[0])}" +
                                   (f" — first frame only, {ext[1:].upper()} can't hold an "
                                    "animation" if res[1] == "flat" else ""))
                self._run_saving(job, done)
            self._file_dialog_save("Export Image", Path(it.name).stem + ext, chosen)
        self._format_dialog("Export Image", EXPORT_FORMATS, "Export…", on_ok)

    def _export_selected(self):
        """Batch convert: selected (or all) images → format / max size → folder."""
        pos = self._selected_positions()
        items = [self.store.get_item(p) for p in pos] if pos else self._items()
        if not items:
            return
        first = next((i.path for i in items if i.path), None)
        state = {"folder": os.path.dirname(first) if first else
                 GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or
                 os.path.expanduser("~")}

        def extra(page, g):
            g.set_description(f"{len(items)} image{'s' if len(items) != 1 else ''}")
            size_row = Adw.SpinRow(title="Maximum Size", subtitle="Longest side in pixels, 0 = original",
                                   adjustment=Gtk.Adjustment(value=0, lower=0, upper=20000,
                                                             step_increment=100,
                                                             page_increment=1000))
            g.add(size_row)
            folder_row = Adw.ActionRow(title="Save To", subtitle=state["folder"], subtitle_lines=2)
            btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                             css_classes=["flat"], tooltip_text="Choose Folder…")

            def pick(*_):
                fd = Gtk.FileDialog(title="Choose Output Folder",
                                    initial_folder=Gio.File.new_for_path(state["folder"]))

                def done(fd_, res):
                    try:
                        gf = fd_.select_folder_finish(res)
                    except GLib.Error:
                        return
                    if gf.get_path():
                        state["folder"] = gf.get_path()
                        folder_row.set_subtitle(state["folder"])
                fd.select_folder(self._root(), None, done)
            btn.connect("clicked", pick)
            folder_row.add_suffix(btn)
            g.add(folder_row)
            return {"size": size_row, "folder_btn": btn}

        def on_ok(ctx, fmt, quality):
            _label, ext, _q = fmt
            maxpx = int(ctx["size"].get_value())
            folder = state["folder"]
            for w in ("fmt", "q", "size", "folder_btn", "ok"):
                ctx[w].set_sensitive(False)
            prog = ctx["progress"]
            prog.set_visible(True)
            prog.set_fraction(0)
            cancelled = {"v": False}
            ctx["cancel"].connect("clicked", lambda *_: cancelled.__setitem__("v", True))
            n = len(items)

            def work():
                written, errors, flat = [], [], []
                for k, it in enumerate(items):
                    if cancelled["v"]:
                        break
                    try:
                        stem = Path(it.name).stem if it.path else it.name
                        dest = str(convert.unique_path(Path(folder) / (stem + ext)))
                        plain = it.path and not it.final_ops() and not maxpx
                        if ext == ".pdf" and plain:
                            convert.images_to_pdf([it.path], dest)
                        else:
                            if ext == ".pdf":
                                im, exif, icc = it.final_image()
                                if maxpx and max(im.size) > maxpx:
                                    s_ = maxpx / max(im.size)
                                    im = im.resize((max(1, round(im.width * s_)),
                                                    max(1, round(im.height * s_))),
                                                   Image.LANCZOS, reducing_gap=3.0)
                                tmpd = tempfile.mkdtemp(prefix="preview-batch-")
                                try:
                                    tmp = os.path.join(tmpd, "page.png")
                                    im.save(tmp, "PNG")
                                    convert.images_to_pdf([tmp], dest)
                                finally:
                                    shutil.rmtree(tmpd, ignore_errors=True)
                            elif self._write_item(it, dest, quality, maxpx, export=True) == "flat":
                                flat.append(it.name)
                        written.append(dest)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{it.name}: {e}")
                    GLib.idle_add(lambda k=k: (prog.set_fraction((k + 1) / n),
                                               prog.set_text(f"{k + 1} of {n}"), False)[2])
                GLib.idle_add(finished, written, errors, flat)

            def finished(written, errors, flat):
                if ctx["dlg"].get_root() is not None:
                    ctx["dlg"].force_close()
                if errors:
                    self.toast(f"Exported {len(written)} of {n}; {len(errors)} failed "
                               f"({errors[0]})")
                else:
                    fol = Gio.File.new_for_path(folder)
                    self.toast(f"Exported {len(written)} image{'s' if len(written) != 1 else ''}" +
                               (f" ({len(flat)} animation{'s' if len(flat) != 1 else ''} "
                                f"saved as first frame only)" if flat else ""),
                               "Open Folder",
                               lambda: Gtk.FileLauncher.new(fol).launch(self._root(), None,
                                                                         None, None))
                return False
            threading.Thread(target=work, daemon=True).start()
        self._format_dialog("Export Selected Images", BATCH_FORMATS, "Export", on_ok, extra)

    def _export_pdf(self):
        pos = self._selected_positions()
        items = [self.store.get_item(p) for p in pos] if pos else self._items()
        if not items:
            return
        paths, tmp = [], None
        try:
            for it in items:
                if it.final_ops() or it.path is None:
                    if tmp is None:
                        tmp = tempfile.mkdtemp(prefix="preview-pdf-")
                    im, _e, _i = it.final_image()
                    dest = os.path.join(tmp, f"{len(paths):04d}-{Path(it.name).stem}.png")
                    im.save(dest, "PNG")
                    paths.append(dest)
                else:
                    paths.append(it.path)
        except Exception as e:  # noqa: BLE001
            self.toast(f"Could not prepare images: {e}")
            return
        first = next((i.path for i in items if i.path), None) or os.path.join(
            GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or
            os.path.expanduser("~"), items[0].name + ".png")

        def cleanup():
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        convert.show_export_dialog(self.window, paths, cleanup, default_dir_from=first,
                                   parent=self._parent_widget())

    # ---- print ----------------------------------------------------------------------------------
    def _print(self):
        pos = self._selected_positions()
        items = [self.store.get_item(p) for p in pos] if len(pos) > 1 else (
            [self.current] if self.current else [])
        if not items:
            return
        op = Gtk.PrintOperation(n_pages=len(items), job_name=items[0].name,
                                use_full_page=False, unit=Gtk.Unit.POINTS)
        cache = {}

        def image(i):
            if i not in cache:
                it = items[i]
                im = it.rendered if (it is self.current and it.rendered is not None
                                     and not it.overlay) else it.final_image()[0]
                im = fit_size(im, 3000)
                buf = io.BytesIO()
                im.convert("RGBA").save(buf, "PNG")
                buf.seek(0)
                cache.clear()
                cache[i] = cairo.ImageSurface.create_from_png(buf)
            return cache[i]

        def page_setup(_op, _ctx, page_nr, setup):
            surf = image(page_nr)
            setup.set_orientation(Gtk.PageOrientation.LANDSCAPE
                                  if surf.get_width() > surf.get_height()
                                  else Gtk.PageOrientation.PORTRAIT)

        def draw(_op, ctx, page_nr):
            surf = image(page_nr)
            cr = ctx.get_cairo_context()
            pw, ph = ctx.get_width(), ctx.get_height()
            iw, ih = surf.get_width(), surf.get_height()
            s = min(pw / iw, ph / ih)
            cr.translate((pw - iw * s) / 2, (ph - ih * s) / 2)
            cr.scale(s, s)
            cr.set_source_surface(surf, 0, 0)
            cr.paint()

        op.connect("request-page-setup", page_setup)
        op.connect("draw-page", draw)
        try:
            op.run(Gtk.PrintOperationAction.PRINT_DIALOG, self._root())
        except GLib.Error as e:
            self.toast(f"Printing failed: {e.message}")

    # ---- wallpaper ------------------------------------------------------------------------------
    def _wallpaper(self):
        it = self.current
        if it is None or it.rendered is None:
            return
        schema = "org.gnome.desktop.background"
        sch = None
        if not runtime.IS_WINDOWS:
            src = Gio.SettingsSchemaSource.get_default()
            sch = src.lookup(schema, True) if src else None
            if sch is None:
                self.toast("Desktop background settings are not available")
                return
        dlg = Adw.AlertDialog(heading="Set as Wallpaper?",
                              body=f"“{it.name}” will be used as the desktop background" +
                                   ("." if runtime.IS_WINDOWS else
                                    " for both light and dark styles."))
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("set", "Set Wallpaper")
        dlg.set_response_appearance("set", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("set")
        dlg.set_close_response("cancel")

        def response(_d, rid):
            if rid != "set":
                return

            def job():
                if it.path and not it.final_ops():
                    return it.path
                d = Path(GLib.get_user_data_dir()) / "backgrounds"
                d.mkdir(parents=True, exist_ok=True)
                dest = convert.unique_path(d / (Path(it.name).stem + ".png"))
                im, exif, icc = it.final_image()
                save_any(im, exif, icc, str(dest))
                return str(dest)

            def done(path, err):
                if err:
                    self.toast(f"Could not set wallpaper: {err}")
                    return
                if runtime.IS_WINDOWS:
                    import ctypes
                    # SPI_SETDESKWALLPAPER, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
                    if ctypes.windll.user32.SystemParametersInfoW(0x14, 0, str(path), 3):
                        self.toast("Wallpaper set")
                    else:
                        self.toast("Could not set wallpaper")
                    return
                s = Gio.Settings.new(schema)
                uri = Gio.File.new_for_path(path).get_uri()
                s.set_string("picture-uri", uri)
                if sch.has_key("picture-uri-dark"):
                    s.set_string("picture-uri-dark", uri)
                if s.get_string("picture-options") == "none":
                    s.set_string("picture-options", "zoom")
                self.toast("Wallpaper set")
            self._run_bg(job, done)
        dlg.connect("response", response)
        dlg.present(self._parent_widget())

    # ---- slideshow ----------------------------------------------------------------------------------
    def _toggle_slideshow(self):
        if self._slideshow is not None:
            self._stop_slideshow()
        else:
            self._start_slideshow()

    def _start_slideshow(self):
        if self.store.get_n_items() < 2 and len(self._siblings) < 2:
            self.toast("Open several images for a slideshow")
            return
        win = self.window if isinstance(self.window, Gtk.Window) else None
        self._slideshow_state = {
            "sidebar": self.split.get_show_sidebar(),
            "fullscreen": bool(win and win.is_fullscreen()),
        }
        self.split.set_show_sidebar(False)
        self.osd.set_visible(False)
        self._set_tool("pan")
        if win is not None:
            if hasattr(win, "set_chrome_visible"):
                win.set_chrome_visible(False)
            win.fullscreen()
        self.canvas.zoom_fit()
        self._slideshow = GLib.timeout_add_seconds(4, self._slideshow_tick)
        self.toast("Slideshow — Esc or F5 to stop")

    def _slideshow_tick(self):
        if self.current and self.current.final_ops() and self.store.get_n_items() < 2:
            return True
        self._step(1, wrap=True)
        return True

    def _stop_slideshow(self):
        if self._slideshow is None:
            return
        GLib.source_remove(self._slideshow)
        self._slideshow = None
        st = self._slideshow_state or {}
        self.split.set_show_sidebar(st.get("sidebar", False) and not self.compact)
        self.osd.set_visible(True)
        win = self.window if isinstance(self.window, Gtk.Window) else None
        if win is not None:
            if not st.get("fullscreen"):
                win.unfullscreen()
            if hasattr(win, "set_chrome_visible"):
                win.set_chrome_visible(True)

    # ---- adjust size ---------------------------------------------------------------------------------
    def _adjust_size(self):
        it = self.current
        if it is None or it.rendered is None:
            return
        bw, bh = it.rendered.size
        dlg = Adw.Dialog(title="Adjust Size", content_width=380)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dlg.close())
        ok = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        hb.pack_start(cancel)
        hb.pack_end(ok)
        tv.add_top_bar(hb)
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(description=f"Current size: {bw} × {bh} px")
        page.add(g)
        wr = Adw.SpinRow(title="Width", subtitle="Pixels",
                         adjustment=Gtk.Adjustment(value=bw, lower=1, upper=30000,
                                                   step_increment=1, page_increment=100))
        hr = Adw.SpinRow(title="Height", subtitle="Pixels",
                         adjustment=Gtk.Adjustment(value=bh, lower=1, upper=30000,
                                                   step_increment=1, page_increment=100))
        pct = Adw.SpinRow(title="Scale", subtitle="Percent",
                          adjustment=Gtk.Adjustment(value=100, lower=1, upper=1000,
                                                    step_increment=1, page_increment=10))
        lock = Adw.SwitchRow(title="Keep Aspect Ratio", active=True)
        for w in (pct, wr, hr, lock):
            g.add(w)
        busy = [False]
        ratio = bw / bh

        def sync(src):
            if busy[0]:
                return
            busy[0] = True
            if src is pct:
                wr.set_value(max(1, round(bw * pct.get_value() / 100)))
                hr.set_value(max(1, round(bh * pct.get_value() / 100)))
            elif lock.get_active():
                if src is wr:
                    hr.set_value(max(1, round(wr.get_value() / ratio)))
                else:
                    wr.set_value(max(1, round(hr.get_value() * ratio)))
                pct.set_value(round(100 * wr.get_value() / bw))
            busy[0] = False
        for r in (wr, hr, pct):
            r.connect("notify::value", lambda r_, *_: sync(r_))
        tv.set_content(page)
        dlg.set_child(tv)

        def apply(*_):
            nw, nh = int(wr.get_value()), int(hr.get_value())
            dlg.close()
            if (nw, nh) == (bw, bh):
                return
            self._commit_overlay(it)
            self._edit(it, it.ops + [("resize", nw, nh)])
        ok.connect("clicked", apply)
        dlg.present(self._parent_widget())
