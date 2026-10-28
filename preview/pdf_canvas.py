"""The scrolling page canvas (one widget for all pages, like Evince's EvView).

Layout modes: continuous (one column), two (pairs side by side), single (one
page at a time). Zoom: fit-width / fit-page / free, Ctrl+scroll, +/-, and the
touchpad pinch (Gtk.GestureZoom) anchored at the point between the fingers.
Painting and pointer interaction are delegated to the viewer.
"""
from __future__ import annotations

import bisect
import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk

from .viewer_base import device_scale, surface_origin

GAP = 14
MARGIN = 18
ZOOM_MIN, ZOOM_MAX = 0.1, 8.0


class PageCanvas(Gtk.Widget, Gtk.Scrollable):
    __gtype_name__ = "PreviewPdfCanvas"

    _hadj = None
    _vadj = None

    def _get_h(self):
        return self._hadj

    def _set_h(self, adj):
        self._set_adj("_hadj", adj)

    def _get_v(self):
        return self._vadj

    def _set_v(self, adj):
        self._set_adj("_vadj", adj)

    hadjustment = GObject.Property(type=Gtk.Adjustment, getter=_get_h, setter=_set_h)
    vadjustment = GObject.Property(type=Gtk.Adjustment, getter=_get_v, setter=_set_v)
    hscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy,
                                      default=Gtk.ScrollablePolicy.MINIMUM)
    vscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy,
                                      default=Gtk.ScrollablePolicy.MINIMUM)

    def __init__(self, viewer):
        super().__init__()
        self.v = viewer
        self.add_css_class("pdf-canvas")
        self.set_focusable(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.zoom = 1.0
        self.mode = "fit-width"          # fit-width | fit-page | free
        self.layout_mode = "continuous"  # continuous | two | single
        self.view_w = self.view_h = 0
        self.content_w = self.content_h = 0
        self.rects = []                  # (x, y, w, h) or None per page, content px
        self.rows = []                   # (top, height, [positions])
        self.row_tops = []
        self.settled = True
        self._settle_id = 0
        self._pending = None             # (pos, frac) to scroll to once allocated
        self.popovers = []
        self._pointer = (0, 0)
        self._pinch = None
        self._flip_acc = 0.0

        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        self._drag_gesture = drag
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.connect("pressed", self._on_click)
        self.add_controller(click)
        click.group(drag)
        rclick = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        rclick.connect("pressed", lambda g, n, x, y: self.v.canvas_context_menu(x, y))
        self.add_controller(rclick)

        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._on_pinch_begin)
        zoom.connect("scale-changed", self._on_pinch_scale)
        zoom.connect("end", self._on_pinch_end)
        zoom.connect("cancel", self._on_pinch_end)
        self.add_controller(zoom)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

    # ---------------------------------------------------- scrollable ----
    def _set_adj(self, attr, adj):
        old = getattr(self, attr)
        if old is not None:
            old.disconnect_by_func(self._on_adj_changed)
        setattr(self, attr, adj)
        if adj is not None:
            adj.connect("value-changed", self._on_adj_changed)
        self.queue_allocate()

    def _on_adj_changed(self, adj):
        self.queue_draw()
        self.v.on_scrolled()

    def hv(self):
        return self._hadj.get_value() if self._hadj else 0.0

    def vv(self):
        return self._vadj.get_value() if self._vadj else 0.0

    def do_measure(self, orientation, for_size):
        return 1, 300, -1, -1

    def do_size_allocate(self, width, height, baseline):
        first = self.view_w == 0
        anchor = None if first else self.widget_to_doc(width / 2, 0)
        resized = (width, height) != (self.view_w, self.view_h)
        self.view_w, self.view_h = width, height
        if self.mode != "free" and (resized or first):
            self.zoom = self.fit_zoom(self.mode)
            self.v.on_zoom_changed()
        self.relayout()
        if self._pending is not None:
            pos, frac = self._pending
            self._pending = None
            self._configure(0, self.page_value(pos, frac))
        elif anchor is not None and resized:
            i, px, py = anchor
            cx, cy = self.doc_to_content(i, px, py)
            self._configure(cx - width / 2, cy)
        else:
            self._configure(self.hv(), self.vv())
        for p in self.popovers:
            p.present()

    def do_snapshot(self, s):
        self.v.paint(self, s)

    def do_dispose(self):
        for p in list(self.popovers):
            p.unparent()
        self.popovers = []
        Gtk.Widget.do_dispose(self)

    def add_popover(self, pop):
        pop.set_parent(self)
        self.popovers.append(pop)

    def remove_popover(self, pop):
        if pop in self.popovers:
            self.popovers.remove(pop)
            pop.unparent()

    # ------------------------------------------------------- layout ----
    def device_scale(self):
        return device_scale(self)

    def surface_origin(self):
        """This widget's top-left in its surface's logical coordinates."""
        return surface_origin(self)

    def fit_zoom(self, mode):
        sizes = self.v.sizes
        if not sizes or self.view_w <= 2 * MARGIN + GAP or self.view_h <= 2 * MARGIN:
            return self.zoom
        if self.layout_mode == "two":
            maxw = max(sizes[i][0] + (sizes[i + 1][0] if i + 1 < len(sizes) else sizes[i][0])
                       for i in range(0, len(sizes), 2))
            z = (self.view_w - 2 * MARGIN - GAP) / maxw
        else:
            maxw = max(w for w, h in sizes)
            z = (self.view_w - 2 * MARGIN) / maxw
        if mode == "fit-page" or self.layout_mode == "single" and mode != "fit-width":
            cur = min(self.v.current, len(sizes) - 1)
            h = sizes[cur][1]
            if self.layout_mode == "two":
                a = cur - cur % 2
                h = max(sizes[a][1], sizes[a + 1][1] if a + 1 < len(sizes) else 0)
            z = min(z, (self.view_h - 2 * MARGIN) / h)
        return max(ZOOM_MIN, min(z, 4.0))

    def relayout(self):
        z, sizes = self.zoom, self.v.sizes
        ds = self.device_scale()
        n = len(sizes)
        snap = lambda v: round(v * ds) / ds  # noqa: E731
        rects = [None] * n
        rows = []
        if self.layout_mode == "single":
            groups = [[min(self.v.current, n - 1)]] if n else []
        elif self.layout_mode == "two":
            groups = [list(range(i, min(i + 2, n))) for i in range(0, n, 2)]
        else:
            groups = [[i] for i in range(n)]
        widths = [sum(sizes[i][0] * z for i in g) + GAP * (len(g) - 1) for g in groups]
        self.content_w = max(self.view_w, (max(widths) if widths else 0) + 2 * MARGIN)
        y = MARGIN
        for g, gw in zip(groups, widths):
            rh = max(sizes[i][1] * z for i in g)
            x = (self.content_w - gw) / 2
            for i in g:
                pw, ph = sizes[i][0] * z, sizes[i][1] * z
                rects[i] = (snap(x), snap(y + (rh - ph) / 2), pw, ph)
                x += pw + GAP
            rows.append((snap(y), rh, g))
            y += rh + GAP
        self.rects, self.rows = rects, rows
        self.row_tops = [r[0] for r in rows]
        self.content_h = max((y - GAP + MARGIN) if rows else 0,
                             self.view_h if self.layout_mode == "single" else 0)

    def _configure(self, hval, vval):
        if self._hadj is None or self._vadj is None:
            return
        cw, ch = self.content_w, self.content_h
        hval = max(0, min(hval, cw - self.view_w))
        vval = max(0, min(vval, ch - self.view_h))
        # (upper >= page size: the layout can lag a resize by one allocation)
        self._hadj.configure(hval, 0, max(cw, self.view_w), 40, self.view_w * 0.9, self.view_w)
        self._vadj.configure(vval, 0, max(ch, self.view_h), 40, self.view_h * 0.9, self.view_h)
        self.queue_draw()

    def row_at(self, cy):
        if not self.rows:
            return 0
        return max(0, bisect.bisect_right(self.row_tops, cy) - 1)

    def page_at(self, cx, cy):
        """Page position nearest to a content point."""
        if not self.rows:
            return 0
        g = self.rows[self.row_at(cy)][2]
        best, bd = g[0], 1e18
        for i in g:
            x, y, w, h = self.rects[i]
            d = 0 if x <= cx <= x + w else min(abs(cx - x), abs(cx - x - w))
            if d < bd:
                best, bd = i, d
        return best

    def visible_range(self):
        if not self.rows:
            return []
        top, bot = self.vv(), self.vv() + self.view_h
        a, b = self.row_at(top), self.row_at(bot)
        out = []
        for r in range(a, b + 1):
            ry, rh, g = self.rows[r]
            if ry + rh >= top and ry <= bot:
                out += g
        return out

    def widget_to_doc(self, wx, wy):
        """Widget point -> (page, x_pt, y_pt) in that page's display frame."""
        if not self.rows:
            return 0, 0.0, 0.0
        cx, cy = self.hv() + wx, self.vv() + wy
        i = self.page_at(cx, cy)
        x, y, w, h = self.rects[i]
        return i, (cx - x) / self.zoom, (cy - y) / self.zoom

    def page_hit(self, wx, wy):
        """(page, x, y) if the widget point is on a page, else None."""
        i, px, py = self.widget_to_doc(wx, wy)
        if not self.rects or self.rects[i] is None:
            return None
        w, h = self.v.sizes[i]
        if 0 <= px <= w and 0 <= py <= h:
            return i, px, py
        return None

    def doc_to_content(self, i, px, py):
        r = self.rects[i] if 0 <= i < len(self.rects) else None
        if r is None:
            return px * self.zoom, py * self.zoom
        return r[0] + px * self.zoom, r[1] + py * self.zoom

    def doc_to_widget(self, i, px, py):
        cx, cy = self.doc_to_content(i, px, py)
        return cx - self.hv(), cy - self.vv()

    def page_value(self, pos, frac=0.0):
        if not self.rects or pos >= len(self.rects) or self.rects[pos] is None:
            return 0
        x, y, w, h = self.rects[pos]
        if frac == 0 and self.row_at(y) == 0:
            return 0
        return y - MARGIN / 2 + frac * h

    def scroll_to_page(self, pos, frac=0.0):
        if self.layout_mode == "single":
            self.relayout()
        if self.view_w == 0:
            self._pending = (pos, frac)
            return
        self._configure(self.hv(), self.page_value(pos, frac))

    def scroll_into_view(self, pos, x, y, w, h):
        if self.rects[pos] is None:
            return
        cx, cy = self.doc_to_content(pos, x, y)
        bw, bh = w * self.zoom, h * self.zoom
        hv, vv = self.hv(), self.vv()
        if cy < vv + 20 or cy + bh > vv + self.view_h - 20:
            vv = cy + bh / 2 - self.view_h / 2
        if cx < hv or cx + bw > hv + self.view_w:
            hv = cx + bw / 2 - self.view_w / 2
        self._configure(hv, vv)

    # --------------------------------------------------------- zoom ----
    def set_mode(self, mode):
        self.mode = mode
        if mode != "free":
            self.zoom_to(self.fit_zoom(mode), *self.center_anchor(), keep_mode=True)
            if mode == "fit-page":
                self.scroll_to_page(self.v.current)

    def set_layout_mode(self, lm):
        cur = self.v.current
        self.layout_mode = lm
        if self.mode != "free":
            self.zoom = self.fit_zoom(self.mode)
            self.v.on_zoom_changed()
        self.relayout()
        self._configure(0, self.page_value(cur))

    def center_anchor(self):
        wx, wy = self.view_w / 2, self.view_h / 2
        return self.widget_to_doc(wx, wy), wx, wy

    def zoom_by(self, factor, wx=None, wy=None):
        if wx is None:
            wx, wy = self.view_w / 2, self.view_h / 2
        self.zoom_to(self.zoom * factor, self.widget_to_doc(wx, wy), wx, wy)

    def zoom_to(self, z, anchor, wx, wy, keep_mode=False):
        if not keep_mode:
            self.mode = "free"
        z = max(ZOOM_MIN, min(ZOOM_MAX, z))
        i, px, py = anchor
        self.zoom = z
        self.relayout()
        if self.rects and 0 <= i < len(self.rects) and self.rects[i] is not None:
            cx, cy = self.doc_to_content(i, px, py)
            self._configure(cx - wx, cy - wy)
        else:
            self._configure(self.hv(), self.vv())
        self._unsettle()
        self.v.on_zoom_changed()

    def _unsettle(self):
        self.settled = False
        if self._settle_id:
            GLib.source_remove(self._settle_id)
        self._settle_id = GLib.timeout_add(140, self._settle)

    def _settle(self):
        if self._pinch is not None:
            return True
        self._settle_id = 0
        self.settled = True
        self.queue_draw()
        return False

    def _on_pinch_begin(self, gesture, seq):
        ok, x, y = gesture.get_bounding_box_center()
        if not ok:
            x, y = self.view_w / 2, self.view_h / 2
        self._pinch = (self.zoom, self.widget_to_doc(x, y))

    def _on_pinch_scale(self, gesture, scale):
        if self._pinch is None:
            return
        ok, x, y = gesture.get_bounding_box_center()
        if not ok:
            x, y = self.view_w / 2, self.view_h / 2
        z0, anchor = self._pinch
        self.zoom_to(z0 * scale, anchor, x, y)

    def _on_pinch_end(self, gesture, seq):
        self._pinch = None
        self._unsettle()

    def _on_scroll(self, ctrl, dx, dy):
        state = ctrl.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            if ctrl.get_unit() == Gdk.ScrollUnit.WHEEL:
                f = 1.15 ** (-dy)
            else:
                f = math.exp(-dy * 0.012)
            self.zoom_by(f, *self._pointer)
            return True
        if self.layout_mode == "single" and dy:
            # flip pages when scrolling past the top/bottom of the page
            at_end = self.vv() >= self.content_h - self.view_h - 0.5
            at_top = self.vv() <= 0.5
            if (dy > 0 and at_end) or (dy < 0 and at_top):
                unit = 1 if ctrl.get_unit() == Gdk.ScrollUnit.WHEEL else 40
                self._flip_acc += dy / unit
                if abs(self._flip_acc) >= 1:
                    step = 1 if self._flip_acc > 0 else -1
                    self._flip_acc = 0
                    self.v.goto_page(self.v.current + step, at_end=step < 0)
                return True
            self._flip_acc = 0
        return False

    # --------------------------------------------------- interaction ----
    def _on_motion(self, ctrl, x, y):
        self._pointer = (x, y)
        self.set_cursor_from_name(self.v.canvas_cursor(x, y))

    def _on_click(self, gesture, n_press, x, y):
        if n_press >= 2:
            self.v.canvas_multi_click(x, y, n_press,
                                      gesture.get_current_event_state())

    def _on_drag_begin(self, gesture, x, y):
        self.grab_focus()
        state = gesture.get_current_event_state()
        if self.v.canvas_press(x, y, state):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        else:
            gesture.set_state(Gtk.EventSequenceState.DENIED)

    def _on_drag_update(self, gesture, dx, dy):
        ok, x0, y0 = gesture.get_start_point()
        self.v.canvas_drag(x0 + dx, y0 + dy, gesture.get_current_event_state())

    def _on_drag_end(self, gesture, dx, dy):
        ok, x0, y0 = gesture.get_start_point()
        self.v.canvas_release(x0 + dx, y0 + dy)
