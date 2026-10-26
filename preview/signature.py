"""Signatures: drawn on the touchpad (or with a mouse / pen / finger in the
box), stored as vector strokes, rendered anywhere.

A signature is a dict:
    {"version": 1, "color": "#111111", "pen": 0.9,
     "strokes": [[[x_mm, y_mm, t_s(, pressure)], ...], ...], "path": Path}
Coordinates are millimetres on the touchpad surface (or on the on-screen
box), so the stroke width can follow the finger's speed (fast = thin, slow =
thick) like real ink.  Points drawn with a pressure-sensitive pen carry a 4th
value, the pressure (0..1), which then sets the width instead.
"""
import io
import json
import math
import time
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from .i18n import _, N_


_pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
SIG_DIR = Path(_pictures or Path.home() / "Pictures") / "Signatures"

INKS = [(N_("Black"), "#141414"), (N_("Blue"), "#1c3f94"), (N_("Red"), "#a51d2d")]
PENS = [(N_("Fine"), 0.6), (N_("Medium"), 0.9), (N_("Bold"), 1.3)]
PAD_MARGIN_MM = 1.5         # ignore the outermost edge of the pad
MIN_STEP_MM = 0.12          # drop points closer than this


# ---------------------------------------------------------------- model ---

def list_signatures() -> list[dict]:
    if not SIG_DIR.is_dir():
        return []
    files = sorted(SIG_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files:
        try:
            out.append(load_signature(f))
        except (OSError, ValueError, KeyError):
            continue
    return out


def load_signature(path) -> dict:
    sig = json.loads(Path(path).read_text())
    if not sig.get("strokes"):
        raise ValueError("empty signature")
    sig["path"] = Path(path)
    return sig


def save_signature(strokes, color, pen) -> dict:
    SIG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = SIG_DIR / f"signature-{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = SIG_DIR / f"signature-{stamp}-{n}.json"
    sig = {"version": 1, "color": color, "pen": pen,
           "strokes": [[[round(p[0], 3), round(p[1], 3), round(p[2], 4),
                         *([round(p[3], 3)] if len(p) > 3 else [])] for p in s]
                       for s in strokes if s]}
    path.write_text(json.dumps(sig))
    sig["path"] = path
    # PNG next to it, handy for pasting into other apps
    path.with_suffix(".png").write_bytes(signature_png_bytes(sig))
    return sig


def delete_signature(sig):
    for suffix in (".json", ".png"):
        try:
            sig["path"].with_suffix(suffix).unlink()
        except (OSError, KeyError):
            pass


def _bbox(sig):
    pad = sig.get("pen", 0.9) * 1.4 / 2 + 0.5
    xs = [p[0] for s in sig["strokes"] for p in s]
    ys = [p[1] for s in sig["strokes"] for p in s]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def signature_aspect(sig) -> float:
    x0, y0, x1, y1 = _bbox(sig)
    return (y1 - y0) / max(x1 - x0, 1e-6)


# -------------------------------------------------------------- render ---

def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _widths(pts):
    """Per-point width factor from pen pressure if recorded, else from finger
    speed (mm/s); smoothed."""
    out, wf = [], 1.0
    for i, p in enumerate(pts):
        if len(p) > 3:
            target = 0.35 + 1.1 * min(max(p[3], 0.0), 1.0)
            wf = target if i == 0 else wf + 0.5 * (target - wf)
        elif i:
            q = pts[i - 1]
            dt = max(p[2] - q[2], 0.004)
            v = math.hypot(p[0] - q[0], p[1] - q[1]) / dt
            target = 1.3 - 0.75 * min(v / 260.0, 1.0)
            wf += 0.3 * (target - wf)
        out.append(wf)
    return out


def render_strokes(cr, strokes, color, pen):
    """Draw strokes in the current user space (units = touchpad mm)."""
    cr.save()
    cr.set_source_rgb(*_rgb(color))
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for pts in strokes:
        if not pts:
            continue
        if len(pts) == 1:
            cr.arc(pts[0][0], pts[0][1], pen * 0.6, 0, 2 * math.pi)
            cr.fill()
            continue
        w = _widths(pts)
        n = len(pts)
        if n == 2:
            cr.set_line_width(pen * (w[0] + w[1]) / 2)
            cr.move_to(pts[0][0], pts[0][1])
            cr.line_to(pts[1][0], pts[1][1])
            cr.stroke()
            continue
        mids = [((pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2)
                for i in range(n - 1)]
        # segment k: from mids[k-1] (or start) through control pts[k] to mids[k] (or end)
        start = (pts[0][0], pts[0][1])
        cr.set_line_width(pen * w[0])
        cr.move_to(*start)
        cr.line_to(*mids[0])
        cr.stroke()
        for k in range(1, n - 1):
            a, c = mids[k - 1], (pts[k][0], pts[k][1])
            b = mids[k] if k < n - 2 else (pts[-1][0], pts[-1][1])
            cr.set_line_width(pen * w[k])
            cr.move_to(*a)
            cr.curve_to(a[0] + 2 / 3 * (c[0] - a[0]), a[1] + 2 / 3 * (c[1] - a[1]),
                        b[0] + 2 / 3 * (c[0] - b[0]), b[1] + 2 / 3 * (c[1] - b[1]),
                        b[0], b[1])
            cr.stroke()
    cr.restore()


def render_signature(cr, sig, x, y, width):
    """Draw sig with its ink bbox top-left at (x, y), scaled to `width`."""
    x0, y0, x1, _ = _bbox(sig)
    s = width / max(x1 - x0, 1e-6)
    cr.save()
    cr.translate(x, y)
    cr.scale(s, s)
    cr.translate(-x0, -y0)
    render_strokes(cr, sig["strokes"], sig.get("color", "#141414"), sig.get("pen", 0.9))
    cr.restore()


def signature_png_bytes(sig, px_width=1200) -> bytes:
    h = max(1, round(px_width * signature_aspect(sig)))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, px_width, h)
    cr = cairo.Context(surf)
    render_signature(cr, sig, 0, 0, px_width)
    buf = io.BytesIO()
    surf.write_to_png(buf)
    return buf.getvalue()


def copy_signature_to_clipboard(sig):
    tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(signature_png_bytes(sig)))
    Gdk.Display.get_default().get_clipboard().set_content(Gdk.ContentProvider.new_for_value(tex))


# -------------------------------------------------------------- capture ---

class SignaturePad(Gtk.DrawingArea):
    """Paper-like canvas whose area maps 1:1 to the touchpad surface."""

    def __init__(self, pad_w, pad_h):
        super().__init__(hexpand=True, vexpand=True)
        self.pad_w, self.pad_h = pad_w, pad_h
        self.strokes = []
        self.color, self.pen = INKS[0][1], PENS[1][1]
        self.live = False
        self.draw_mode = False      # drawing with mouse / pen / touch inside the box
        self.finger = None          # (x_mm, y_mm) while touching
        self.set_size_request(480, round(480 * pad_h / pad_w))
        self.set_draw_func(self._draw)

    def paper_rect(self, width, height):
        m = 12
        s = min((width - 2 * m) / self.pad_w, (height - 2 * m) / self.pad_h)
        pw, ph = self.pad_w * s, self.pad_h * s
        return (width - pw) / 2, (height - ph) / 2, pw, ph, s

    def to_mm(self, wx, wy):
        """Widget pixels -> paper millimetres (clamped to the paper)."""
        x, y, _pw, _ph, s = self.paper_rect(self.get_width(), self.get_height())
        mx = min(max((wx - x) / s, PAD_MARGIN_MM), self.pad_w - PAD_MARGIN_MM)
        my = min(max((wy - y) / s, PAD_MARGIN_MM), self.pad_h - PAD_MARGIN_MM)
        return mx, my

    def _draw(self, area, cr, width, height):
        x, y, pw, ph, s = self.paper_rect(width, height)
        r = 14
        cr.new_sub_path()
        cr.arc(x + pw - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + pw - r, y + ph - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + ph - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()
        path = cr.copy_path()
        cr.set_source_rgb(1, 1, 1)
        cr.fill_preserve()
        acc = Adw.StyleManager.get_default().get_accent_color_rgba()
        if self.live:
            cr.set_source_rgb(acc.red, acc.green, acc.blue)
            cr.set_line_width(3)
        else:
            cr.set_source_rgba(0, 0, 0, 0.18)
            cr.set_line_width(1)
        cr.stroke()

        # baseline with an "x", like a paper form
        base = y + ph * 0.72
        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.set_line_width(1)
        cr.move_to(x + pw * 0.08, base)
        cr.line_to(x + pw * 0.92, base)
        cr.stroke()
        cr.set_line_width(1.6)
        bx = x + pw * 0.08
        cr.move_to(bx, base - 18); cr.line_to(bx + 12, base - 6)
        cr.move_to(bx + 12, base - 18); cr.line_to(bx, base - 6)
        cr.stroke()

        if not self.strokes and not self.live:
            cr.set_source_rgba(0, 0, 0, 0.45)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(15)
            msg = (_("Sign here with your mouse, pen or finger") if self.draw_mode
                   else _("Press Space or click Start, then sign on the touchpad"))
            ext = cr.text_extents(msg)
            cr.move_to(x + (pw - ext.width) / 2, y + ph * 0.42)
            cr.show_text(msg)

        cr.append_path(path)
        cr.clip()
        cr.translate(x, y)
        cr.scale(s, s)
        render_strokes(cr, self.strokes, self.color, self.pen)
        if self.live and self.finger:
            fx, fy = self.finger
            cr.set_source_rgba(acc.red, acc.green, acc.blue, 0.4)
            cr.arc(fx, fy, 2.2, 0, 2 * math.pi)
            cr.fill()


class SignatureCaptureWindow(Adw.Window):
    """Modal window: sign on the touchpad (Linux evdev / Windows Raw Input) or draw with the
    mouse / pen / finger inside the box (everywhere), then Save."""

    def __init__(self, parent, on_saved=None, draw_mode=None):
        super().__init__(title=_("New Signature"), modal=True, transient_for=parent,
                         default_width=780, default_height=560)
        self.on_saved = on_saved
        self.pad = None
        self.active_id = None
        self.last_touch = 0.0
        self.history = []           # strokes before the current session, for Esc
        self._drag_stroke = None
        self._pressure = None
        self._drag_t0 = 0.0

        error = None
        try:
            from .touchpad import Touchpad
            self.pad = Touchpad(on_frame=self._on_frame, on_lost=self._on_lost)
            pad_w, pad_h = self.pad.width_mm, self.pad.height_mm
        except Exception as e:  # noqa: BLE001  (TouchpadError, OSError, no fcntl…)
            pad_w, pad_h, error = 130.0, 80.0, str(e)

        self.canvas = SignaturePad(pad_w, pad_h)
        self.touchpad_error = error

        # Mouse / pen / touch drawing inside the box.  The stylus gesture only
        # records pressure (it never claims the sequence); the drag gesture
        # turns pointer, pen and touch motion into strokes.
        stylus = Gtk.GestureStylus(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        stylus.connect("down", self._on_stylus)
        stylus.connect("motion", self._on_stylus)
        stylus.connect("up", lambda *_: setattr(self, "_pressure", None))
        self.canvas.add_controller(stylus)
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.canvas.add_controller(drag)

        header = Adw.HeaderBar()
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save_btn = Gtk.Button(label=_("Save"), sensitive=False)
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._save)
        header.pack_end(self.save_btn)

        self.status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.status.add_css_class("dim-label")

        # ink + pen choices
        tools = Gtk.Box(spacing=12)
        ink_box = Gtk.Box(css_classes=["linked"])
        first = None
        for name, color in INKS:
            b = Gtk.ToggleButton(tooltip_text=_(name))
            b.set_child(Gtk.Label(use_markup=True,
                                  label=f"<span foreground='{color}' size='x-large'>●</span>"))
            b.set_group(first)
            first = first or b
            b.connect("toggled", self._set_ink, color)
            ink_box.append(b)
        first.set_active(True)
        pen_box = Gtk.Box(css_classes=["linked"])
        first = None
        for name, width in PENS:
            b = Gtk.ToggleButton(label=_(name))
            b.set_group(first)
            first = first or b
            b.connect("toggled", self._set_pen, width)
            pen_box.append(b)
        pen_box.get_first_child().get_next_sibling().set_active(True)
        tools.append(ink_box)
        tools.append(pen_box)
        tools.append(Gtk.Box(hexpand=True))
        self.undo_btn = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text=_("Undo stroke (Backspace)"))
        self.undo_btn.connect("clicked", lambda *_: self._undo())
        self.clear_btn = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text=_("Clear (Delete)"))
        self.clear_btn.connect("clicked", lambda *_: self._clear())
        self.start_btn = Gtk.Button(label=_("Start"), sensitive=self.pad is not None)
        self.start_btn.add_css_class("pill")
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", lambda *_: self._toggle())
        # input source: touchpad (absolute) or mouse / pen / touch
        self.src_box = Gtk.Box(css_classes=["linked"], visible=self.pad is not None)
        self.src_pad = Gtk.ToggleButton(label=_("Touchpad"), tooltip_text=_("Sign on the touchpad"))
        self.src_draw = Gtk.ToggleButton(label=_("Mouse or Pen"), group=self.src_pad,
                                         tooltip_text=_("Draw with the mouse, a pen or a "
                                                      "finger inside the box"))
        self.src_box.append(self.src_pad)
        self.src_box.append(self.src_draw)
        tools.append(self.src_box)
        tools.append(self.undo_btn)
        tools.append(self.clear_btn)
        tools.append(self.start_btn)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        body.append(self.status)
        body.append(self.canvas)
        body.append(tools)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_content(view)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("notify::is-active", self._on_active)
        self.connect("close-request", self._on_close)
        self._idle_timer = GLib.timeout_add_seconds(1, self._check_idle)

        if draw_mode is None:
            draw_mode = self.pad is None
        (self.src_draw if draw_mode or self.pad is None else self.src_pad).set_active(True)
        self.src_draw.connect("toggled", lambda b: self._set_draw_mode(b.get_active()))
        self._set_draw_mode(draw_mode or self.pad is None)

    def _set_draw_mode(self, on):
        if self.canvas.live:
            self._stop()
        self.canvas.draw_mode = on
        self.start_btn.set_visible(not on)
        if on:
            hint = _("Sign in the box with your mouse, a pen or a finger. Backspace undoes.")
            if self.touchpad_error:
                hint = _("Touchpad signing is unavailable ({error}) {hint}").format(
                    error=self.touchpad_error, hint=hint)
            self._set_status(hint)
        elif self.pad is not None:
            self._set_status(_("Touchpad {w:.0f} × {h:.0f} mm — the whole pad maps onto the box.")
                             .format(w=self.pad.width_mm, h=self.pad.height_mm))
        self._sync()

    # ---- mouse / pen / touch drawing ----
    def _on_stylus(self, gesture, _x, _y):
        ok, value = gesture.get_axis(Gdk.AxisUse.PRESSURE)
        self._pressure = value if ok else None

    def _point(self, wx, wy):
        x, y = self.canvas.to_mm(wx, wy)
        t = time.monotonic() - self._drag_t0
        if self._pressure is not None:
            return (x, y, t, self._pressure)
        return (x, y, t)

    def _on_drag_begin(self, gesture, x, y):
        if not self.canvas.draw_mode:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._drag_t0 = time.monotonic()
        self._drag_stroke = [self._point(x, y)]
        self.canvas.strokes.append(self._drag_stroke)
        self._sync()

    def _on_drag_update(self, gesture, dx, dy):
        stroke = self._drag_stroke
        if stroke is None:
            return
        ok, sx, sy = gesture.get_start_point()
        if not ok:
            return
        p = self._point(sx + dx, sy + dy)
        last = stroke[-1]
        if math.hypot(p[0] - last[0], p[1] - last[1]) >= MIN_STEP_MM:
            stroke.append(p)
            self.canvas.queue_draw()

    def _on_drag_end(self, gesture, dx, dy):
        if self._drag_stroke is not None:
            self._on_drag_update(gesture, dx, dy)
        self._drag_stroke = None
        self._sync()

    # ---- state ----
    def _set_status(self, text):
        self.status.set_label(text)

    def _sync(self):
        has = bool(self.canvas.strokes)
        live = self.canvas.live
        self.save_btn.set_sensitive(has and not live)
        self.undo_btn.set_sensitive(has)
        self.clear_btn.set_sensitive(has)
        self.start_btn.set_label(_("Done") if live else (_("Sign Again") if has else _("Start")))
        self.canvas.queue_draw()

    def _toggle(self):
        if self.canvas.live:
            self._stop()
        else:
            self._start()

    def _start(self):
        if not self.pad or self.canvas.live or self.canvas.draw_mode:
            return
        self.history = [list(s) for s in self.canvas.strokes]
        self._set_status(_("Lift your finger to begin…"))

        def grabbed():
            self.canvas.live = True
            self.last_touch = time.monotonic()
            self._set_status(_("Sign with one finger on the touchpad. "
                               "Press any key when finished · Backspace undoes · Esc cancels"))
            self._sync()

        self.pad.grab(grabbed)

    def _stop(self, cancel=False):
        if self.pad:
            self.pad.release()
        if cancel:
            self.canvas.strokes = self.history
        self.canvas.live = False
        self.canvas.finger = None
        self.active_id = None
        if self.canvas.strokes:
            self._set_status(_("Looks good? Click Save. Or Sign Again to add more strokes."))
        elif not self.canvas.draw_mode:
            self._set_status(_("Press Space or click Start, then sign on the touchpad."))
        self._sync()

    def _undo(self):
        if self.canvas.strokes:
            self.canvas.strokes.pop()
            self.active_id = None
            self._sync()

    def _clear(self):
        self.canvas.strokes = []
        self.active_id = None
        self._sync()

    def _set_ink(self, btn, color):
        if btn.get_active():
            self.canvas.color = color
            self.canvas.queue_draw()

    def _set_pen(self, btn, width):
        if btn.get_active():
            self.canvas.pen = width
            self.canvas.queue_draw()

    # ---- touchpad frames ----
    def _on_frame(self, contacts, t):
        self.last_touch = time.monotonic()
        c = self.canvas
        if len(contacts) == 1:
            cid, x, y = contacts[0]
            m = PAD_MARGIN_MM
            x = min(max(x, m), c.pad_w - m)
            y = min(max(y, m), c.pad_h - m)
            c.finger = (x, y)
            if cid != self.active_id:
                self.active_id = cid
                c.strokes.append([(x, y, t)])
            else:
                stroke = c.strokes[-1]
                lx, ly, _ = stroke[-1]
                if math.hypot(x - lx, y - ly) >= MIN_STEP_MM:
                    stroke.append((x, y, t))
        else:
            if len(contacts) > 1 and self.active_id is not None and c.strokes:
                s = c.strokes[-1]
                if len(s) < 4 and s[-1][2] - s[0][2] < 0.12:
                    c.strokes.pop()     # second finger landed: not a stroke
            self.active_id = None
            c.finger = None
        self._sync()

    def _on_lost(self):
        self._stop()
        self._set_status(_("⚠ Lost the touchpad device."))

    # ---- safety + keys ----
    def _on_active(self, *_):
        if not self.is_active() and self.canvas.live:
            self._stop()        # never keep the touchpad when we lose focus

    def _check_idle(self):
        if self.canvas.live and time.monotonic() - self.last_touch > 45:
            self._stop()
            self._set_status(_("Stopped after 45 s without touch. Press Space to continue."))
        return GLib.SOURCE_CONTINUE

    MODIFIERS = {Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
                 Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R,
                 Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_Caps_Lock}

    def _on_key(self, ctrl, keyval, keycode, state):
        ctrl_held = state & Gdk.ModifierType.CONTROL_MASK
        if keyval in self.MODIFIERS:
            return False
        if keyval in (Gdk.KEY_BackSpace,) or (ctrl_held and keyval in (Gdk.KEY_z, Gdk.KEY_Z)):
            self._undo()
            return True
        if keyval == Gdk.KEY_Delete:
            self._clear()
            return True
        if self.canvas.live:
            self._stop(cancel=keyval == Gdk.KEY_Escape)
            return True
        if keyval == Gdk.KEY_space:
            self._start()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.save_btn.get_sensitive():
            self._save()
            return True
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _save(self, *_):
        if not self.canvas.strokes:
            return
        sig = save_signature(self.canvas.strokes, self.canvas.color, self.canvas.pen)
        if self.on_saved:
            self.on_saved(sig)
        self.close()

    def _on_close(self, *_):
        GLib.source_remove(self._idle_timer)
        if self.pad:
            self.pad.close()
            self.pad = None
        return False


# --------------------------------------------------------- header button ---

class _Thumb(Gtk.DrawingArea):
    def __init__(self, sig, w=220, h=64):
        super().__init__(content_width=w, content_height=h)
        self.sig = sig
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, width, height):
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        a = signature_aspect(self.sig)
        sw = min(width - 16, (height - 10) / a)
        render_signature(cr, self.sig, (width - sw) / 2, (height - sw * a) / 2, sw)


class SignatureButton(Gtk.MenuButton):
    """Header-bar button like Preview's: pick a saved signature or draw one."""

    def __init__(self, window, on_chosen):
        super().__init__(icon_name="document-edit-symbolic", tooltip_text=_("Sign"))
        self.window = window
        self.on_chosen = on_chosen
        self.pop = Gtk.Popover()
        self.set_popover(self.pop)
        self.pop.connect("show", lambda *_: self._rebuild())
        self._rebuild()

    def _rebuild(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        sigs = list_signatures()
        if sigs:
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
            for sig in sigs[:8]:
                row = Gtk.Box(spacing=6)
                pick = Gtk.Button(child=_Thumb(sig), css_classes=["flat"],
                                  tooltip_text=_("Place this signature"))
                pick.connect("clicked", self._pick, sig)
                more = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                      css_classes=["flat"])
                mp = Gtk.Popover()
                mbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                copy = Gtk.Button(label=_("Copy as Image"), css_classes=["flat"])
                copy.connect("clicked", self._copy, sig, mp)
                dele = Gtk.Button(label=_("Delete"), css_classes=["flat", "destructive-action"])
                dele.connect("clicked", self._delete, sig, mp)
                mbox.append(copy)
                mbox.append(dele)
                mp.set_child(mbox)
                more.set_popover(mp)
                row.append(pick)
                row.append(more)
                lb.append(row)
            box.append(lb)
        else:
            box.append(Gtk.Label(label=_("No signatures yet"), css_classes=["dim-label"],
                                 margin_top=6, margin_bottom=6))
        new = Gtk.Button(label=_("Create Signature…"))
        new.connect("clicked", self._new)
        box.append(new)
        self.pop.set_child(box)

    def _pick(self, _btn, sig):
        self.pop.popdown()
        self.on_chosen(sig)

    def _copy(self, _btn, sig, mp):
        mp.popdown()
        copy_signature_to_clipboard(sig)
        if hasattr(self.window, "show_toast"):
            self.window.show_toast(_("Signature copied to clipboard"))

    def _delete(self, _btn, sig, mp):
        mp.popdown()
        delete_signature(sig)
        self._rebuild()

    def _new(self, *_):
        self.pop.popdown()

        def saved(sig):
            self._rebuild()
            self.on_chosen(sig)

        SignatureCaptureWindow(self.window, saved).present()
