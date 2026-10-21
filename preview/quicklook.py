"""Quick Look for GNOME Files: a drop-in replacement for GNOME Sushi.

Owns the session-bus name org.gnome.NautilusPreviewer and implements the
org.gnome.NautilusPreviewer2 interface that Nautilus calls when Space is
pressed.  One window is reused for every file; the process stays resident
(and exits after IDLE_EXIT_S seconds without use) so the next Space press is
instant.  Run as:  python -m preview.quicklook [--debug] [FILE…]
"""
import json
import os
import sys
import time

from .runtime import APP_ID as _MAIN_APP_ID, configure_gl_env  # noqa: E402
from .i18n import _

configure_gl_env()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk  # noqa: E402

BUS_NAME = "org.gnome.NautilusPreviewer"
OBJ_PATH = "/org/gnome/NautilusPreviewer"
IFACE = "org.gnome.NautilusPreviewer2"
DEBUG_IFACE = _MAIN_APP_ID + ".QuickLookDebug"
APP_ID = _MAIN_APP_ID + ".QuickLook"
PREVIEW_DESKTOP_ID = _MAIN_APP_ID + ".desktop"
IDLE_EXIT_S = 15 * 60

INTROSPECTION_XML = f"""
<node>
  <interface name="{IFACE}">
    <method name="ShowFile">
      <arg type="s" direction="in" name="uri"/>
      <arg type="s" direction="in" name="windowHandle"/>
      <arg type="b" direction="in" name="closeIfAlreadyShown"/>
      <arg type="s" direction="in" name="activationToken"/>
    </method>
    <method name="Close"/>
    <property name="ParentHandle" type="s" access="read"/>
    <property name="Visible" type="b" access="read"/>
    <signal name="SelectionEvent">
      <arg type="q" name="direction"/>
    </signal>
  </interface>
</node>
"""

DEBUG_XML = f"""
<node>
  <interface name="{DEBUG_IFACE}">
    <method name="Snapshot">
      <arg type="s" direction="in" name="path"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="Key">
      <arg type="u" direction="in" name="keyval"/>
      <arg type="b" direction="out" name="handled"/>
    </method>
    <method name="Stats">
      <arg type="s" direction="out" name="json"/>
    </method>
  </interface>
</node>
"""

CONFIG_PATH = os.path.join(GLib.get_user_config_dir(), "view", "quicklook.json")

CSS = """
.ql-card-title { font-size: 1.5em; font-weight: 800; }
.ql-dim { opacity: 0.6; }
.ql-key { opacity: 0.6; }
.ql-tile { padding: 6px; border-radius: 10px; }
.ql-tile label { font-size: 0.85em; }
.ql-code { font-family: monospace; font-size: 10.5pt; }
.ql-code text { background: transparent; }
.ql-pdfpages { background-color: color-mix(in srgb, var(--window-bg-color) 85%, black); }
.ql-pdfpage { box-shadow: 0 1px 4px alpha(black, 0.35); background: white; }
.ql-media-dark { background: black; }
"""


def _log(*a):
    print("[quicklook]", *a, file=sys.stderr, flush=True)


def _process_age_ms():
    """Milliseconds since this process was exec'd (from /proc)."""
    try:
        with open("/proc/self/stat") as f:
            start_ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        return (up - start_ticks / os.sysconf("SC_CLK_TCK")) * 1000.0
    except Exception:  # noqa: BLE001
        return -1.0


def _load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f)
        os.replace(tmp, CONFIG_PATH)
    except Exception:  # noqa: BLE001
        pass


# ===================================================================== window ===

def _decoration_layout():
    """Like Sushi: keep only the close button (no minimize / maximize)."""
    try:
        layout = Gtk.Settings.get_default().get_property("gtk-decoration-layout") or ""
    except Exception:  # noqa: BLE001
        layout = ""
    left, _, right = layout.partition(":")
    keep = lambda side: ",".join(x for x in side.split(",") if x in ("close", "icon"))
    return f"{keep(left)}:{keep(right)}" if "close" in layout else ":close"


class QuickLookWindow(Adw.Window):
    def __init__(self, app):
        cfg = app.config
        super().__init__(application=app, title=_("Quick Look"),
                         default_width=int(cfg.get("width", 900)),
                         default_height=int(cfg.get("height", 650)))
        self.app = app
        self.gfile = None
        self.renderer = None
        self.kind = None
        self._engaged = False        # user clicked into the content deliberately
        self._handle = ""            # xdg-foreign parent handle currently applied
        self._paint_cb = None
        self.add_css_class("quicklook")

        self.header = Adw.HeaderBar(decoration_layout=_decoration_layout())
        self.wtitle = Adw.WindowTitle(title=_("Quick Look"), subtitle="")
        self.header.set_title_widget(self.wtitle)

        self.fs_btn = Gtk.Button(icon_name="view-fullscreen-symbolic",
                                 tooltip_text=_("Full Screen (F)"))
        self.fs_btn.connect("clicked", lambda *_: self.toggle_fullscreen())
        self.header.pack_start(self.fs_btn)

        self.open_default_btn = Gtk.Button(label=_("Open"))
        self.open_default_btn.connect("clicked", lambda *_: self.open_default())
        self.header.pack_end(self.open_default_btn)
        self.open_preview_btn = Gtk.Button(label=_("Open with View"),
                                           css_classes=["suggested-action"],
                                           tooltip_text=_("Open with View (Enter)"))
        self.open_preview_btn.connect("clicked", lambda *_: self.open_in_preview())
        self.header.pack_end(self.open_preview_btn)

        self.toasts = Adw.ToastOverlay()
        self.bin = Adw.Bin()
        self.toasts.set_child(self.bin)
        self.toolbar = Adw.ToolbarView()
        self.toolbar.add_top_bar(self.header)
        self.toolbar.set_content(self.toasts)
        self.set_content(self.toolbar)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        click = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.CAPTURE, button=0)
        click.connect("pressed", self._on_content_click)
        self.bin.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        self.connect("close-request", self._on_close_request)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)
        self.connect("map", lambda *_: self.app.set_visible(True))
        self.connect("unmap", lambda *_: self.app.set_visible(False))

    # ------------------------------------------------------------ showing ---
    def show_file(self, gfile, handle="", token=""):
        t0 = time.monotonic()
        self.set_file(gfile)
        self.set_parent_handle(handle)
        if token:
            try:
                self.set_startup_id(token)
            except Exception:  # noqa: BLE001
                pass
        self._watch_first_paint(t0)
        self.present()
        # GDK re-applies the imported parent when the toplevel is (re)mapped, but
        # only if it was set while realized; do it again right after mapping.
        if handle:
            GLib.idle_add(lambda: self._apply_parent(handle, force=False) and False)

    def set_file(self, gfile):
        from . import quicklook_renderers as R
        self._teardown()
        self._engaged = False
        self.gfile = gfile
        try:
            renderer = R.create_renderer(self, gfile)
            widget = renderer.build()
        except Exception as e:  # noqa: BLE001 - never fail on a file
            import traceback
            traceback.print_exc()
            renderer = R.ErrorRenderer(self, gfile, e)
            widget = renderer.build()
        self.renderer = renderer
        self.kind = renderer.kind
        self.bin.set_child(widget)
        try:
            renderer.attached()
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        self.refresh_header()
        _log(f"{gfile.get_uri()}: {type(renderer).__name__}"
             + (" (embedded viewer)" if getattr(renderer, "viewer", None) is not None
                else ""))
        self.app.stats["last_kind"] = renderer.kind
        self.app.stats["last_renderer"] = type(renderer).__name__

    def refresh_header(self):
        r = self.renderer
        if r is None:
            return
        try:
            title, subtitle = r.get_title(), r.get_subtitle()
        except Exception:  # noqa: BLE001
            title, subtitle = (self.gfile.get_basename() if self.gfile else "Quick Look"), ""
        self.wtitle.set_title(title or "")
        self.wtitle.set_subtitle(subtitle or "")
        self.set_title(title or "Quick Look")
        self.open_preview_btn.set_visible(r.preview_can_open())
        label = r.default_app_label()
        self.open_default_btn.set_label(label)
        self.open_default_btn.set_visible(
            not (r.preview_can_open() and label == "Open With View"))

    def _teardown(self):
        r, self.renderer = self.renderer, None
        if r is not None:
            try:
                r.destroy()
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
        self.bin.set_child(None)

    def _watch_first_paint(self, t0):
        clock = self.get_frame_clock()
        if self._paint_cb is not None and self._paint_cb[0] is not None:
            try:
                self._paint_cb[0].disconnect(self._paint_cb[1])
            except Exception:  # noqa: BLE001
                pass
        self._paint_cb = None

        def painted(clk, *_):
            ms = (time.monotonic() - t0) * 1000
            self.app.stats.setdefault("paint_ms", []).append(round(ms, 1))
            if self.app.stats.get("cold_first_paint_ms") is None:
                self.app.stats["cold_first_paint_ms"] = round(_process_age_ms(), 1)
                _log(f"cold start: process exec -> first frame "
                     f"{self.app.stats['cold_first_paint_ms']:.0f} ms")
            _log(f"ShowFile -> frame painted {ms:.1f} ms ({self.kind})")
            clk.disconnect(hid[0])
            self._paint_cb = None

        hid = []
        if clock is not None:
            hid.append(clock.connect("after-paint", painted))
            self._paint_cb = (clock, hid[0])
        else:
            def on_map(*_):
                self.disconnect(mid[0])
                c = self.get_frame_clock()
                if c is not None:
                    hid.append(c.connect("after-paint", painted))
                    self._paint_cb = (c, hid[0])
            mid = [self.connect("map", on_map)]

    # ------------------------------------------------------- parent window ---
    def set_parent_handle(self, handle):
        handle = handle or ""
        if handle != self.app.parent_handle:
            self.app.set_parent_handle(handle)
        if not self.get_realized():
            self.realize()
        self._apply_parent(handle, force=True)

    def _apply_parent(self, handle, force):
        if not handle.startswith("wayland:"):
            return
        if handle == self._handle and not force:
            return
        try:
            gi.require_version("GdkWayland", "4.0")
            from gi.repository import GdkWayland
            surface = self.get_surface()
            if isinstance(surface, GdkWayland.WaylandToplevel):
                # (GDK drops any previously imported parent itself)
                ok = surface.set_transient_for_exported(handle[len("wayland:"):])
                if self.app.debug:
                    _log(f"set_transient_for_exported -> {ok}")
                self._handle = handle if ok else ""
        except Exception as e:  # noqa: BLE001
            _log("set_transient_for_exported failed:", e)

    # ------------------------------------------------------------- actions ---
    def close_preview(self):
        if self.is_fullscreen():
            self.unfullscreen()
        self.set_visible(False)
        self._teardown()
        self.gfile = None
        self._save_size()
        self.app.schedule_idle_exit()

    def _on_close_request(self, *_):
        self.close_preview()
        return True   # keep the window object around for the next Space press

    def _save_size(self):
        if self.is_fullscreen() or self.is_maximized():
            return
        w, h = self.get_default_size()
        if w > 200 and h > 150:
            cfg = self.app.config
            if cfg.get("width") != w or cfg.get("height") != h:
                cfg["width"], cfg["height"] = w, h
                _save_config(cfg)

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen_changed(self, *_):
        fs = self.is_fullscreen()
        self.fs_btn.set_icon_name("view-restore-symbolic" if fs else "view-fullscreen-symbolic")
        self.toolbar.set_reveal_top_bars(not fs)

    def _on_motion(self, _ctl, _x, y):
        if self.is_fullscreen():
            self.toolbar.set_reveal_top_bars(y < 60)

    def _launch_context(self):
        try:
            return self.get_display().get_app_launch_context()
        except Exception:  # noqa: BLE001
            return None

    def open_in_preview(self, files=None):
        if files is None:
            if self.renderer is None or not self.renderer.preview_can_open():
                return self.open_default()
            files = [self.renderer.preview_file()]
        uris = [f.get_uri() for f in files]
        launched = False
        try:
            info = Gio.DesktopAppInfo.new(PREVIEW_DESKTOP_ID)
        except (TypeError, GLib.Error):
            info = None
        if info is not None:
            try:
                launched = info.launch_uris(uris, self._launch_context())
            except GLib.Error as e:
                _log("launching View failed:", e.message)
        if not launched:
            launched = _spawn_preview_fallback(files)
        if launched:
            self.close_preview()
        else:
            self.show_toast(_("Could not start View"))

    def open_default(self):
        if self.gfile is None:
            return
        uri = self.renderer.default_open_uri() if self.renderer else self.gfile.get_uri()

        def done(_src, res):
            try:
                Gio.AppInfo.launch_default_for_uri_finish(res)
            except GLib.Error as e:
                self.present()
                self.show_toast(_("Could not open: {message}").format(message=e.message))
        try:
            Gio.AppInfo.launch_default_for_uri_async(uri, self._launch_context(), None, done)
            self.close_preview()
        except Exception as e:  # noqa: BLE001
            self.show_toast(_("Could not open: {e}").format(e=e))

    def show_toast(self, message, button_label=None, on_button=None):
        t = Adw.Toast(title=GLib.markup_escape_text(message), timeout=4)
        if button_label:
            t.set_button_label(button_label)
            if on_button:
                t.connect("button-clicked", lambda *_: on_button())
        self.toasts.add_toast(t)

    # ---------------------------------------------------------------- keys ---
    def _on_content_click(self, *_):
        self._engaged = True

    def _on_key(self, _ctl, keyval, _keycode, state):
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                   | Gdk.ModifierType.SUPER_MASK):
            return False
        focus = self.get_focus()
        editing = isinstance(focus, (Gtk.Text, Gtk.Editable)) or (
            isinstance(focus, Gtk.TextView) and focus.get_editable())
        K = Gdk
        if keyval == K.KEY_Escape:
            if editing:
                return False
            if self.is_fullscreen():
                self.unfullscreen()
            else:
                self.close_preview()
            return True
        if editing:
            return False
        if keyval in (K.KEY_space, K.KEY_q):
            self.close_preview()
            return True
        if keyval in (K.KEY_Return, K.KEY_KP_Enter, K.KEY_ISO_Enter):
            self.open_in_preview()
            return True
        if keyval in (K.KEY_f, K.KEY_F, K.KEY_F11):
            self.toggle_fullscreen()
            return True
        dirs = {K.KEY_Left: Gtk.DirectionType.LEFT, K.KEY_Right: Gtk.DirectionType.RIGHT,
                K.KEY_Up: Gtk.DirectionType.UP, K.KEY_Down: Gtk.DirectionType.DOWN,
                K.KEY_KP_Left: Gtk.DirectionType.LEFT, K.KEY_KP_Right: Gtk.DirectionType.RIGHT,
                K.KEY_KP_Up: Gtk.DirectionType.UP, K.KEY_KP_Down: Gtk.DirectionType.DOWN}
        if keyval in dirs:
            if mods & Gdk.ModifierType.SHIFT_MASK:
                return False
            vertical = dirs[keyval] in (Gtk.DirectionType.UP, Gtk.DirectionType.DOWN)
            # Like Sushi, arrows move Nautilus's selection.  Only after the user
            # deliberately clicked into a scrollable preview do Up/Down scroll it.
            if vertical and self._engaged and self.renderer is not None \
                    and self.renderer.wants_vertical_keys:
                return False
            self.app.emit_selection_event(int(dirs[keyval]))
            return True
        return False

    # ------------------------------------------------------------ snapshot ---
    def snapshot_png(self, path):
        widget = self.get_content()
        w, h = widget.get_width(), widget.get_height()
        if w <= 0 or h <= 0:
            return False
        paintable = Gtk.WidgetPaintable.new(widget)
        snap = Gtk.Snapshot()
        # opaque background (the window background is drawn by the toplevel)
        rgba = Gdk.RGBA()
        rgba.parse("#242424" if Adw.StyleManager.get_default().get_dark() else "#fafafa")
        rect = Graphene.Rect().init(0, 0, w, h)
        snap.append_color(rgba, rect)
        paintable.snapshot(snap, w, h)
        node = snap.to_node()
        if node is None:
            return False
        tex = self.get_renderer().render_texture(node, rect)
        return bool(tex.save_to_png(path))


def _spawn_preview_fallback(files):
    import subprocess
    from .runtime import launcher_argv, launcher_env
    paths = [f.get_path() or f.get_uri() for f in files]
    try:
        subprocess.Popen([*launcher_argv(), *paths], env=launcher_env(),
                         start_new_session=True)
        return True
    except OSError as e:
        _log("fallback launch failed:", e)
        return False


# ================================================================ application ===

class QuickLookApp(Adw.Application):
    def __init__(self, debug=False, initial_files=()):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.debug = debug
        self.initial_files = list(initial_files)
        self.config = _load_config()
        self.window = None
        self.conn = None
        self.parent_handle = ""
        self.visible = False
        self._reg_ids = []
        self._own_id = 0
        self._owned = False
        self._warmed = False
        self._idle_src = 0
        self.stats = {"started_ms": round(_process_age_ms(), 1)}

    # ---------------------------------------------------------- lifecycle ---
    def do_startup(self):
        Adw.Application.do_startup(self)
        self.hold()   # stay resident; we quit ourselves after IDLE_EXIT_S
        prov = Gtk.CssProvider()
        prov.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self._reg_ids.append(self._register(
            OBJ_PATH, node.interfaces[0], self._on_method_call, self._on_get_property, None))
        if self.debug:
            dnode = Gio.DBusNodeInfo.new_for_xml(DEBUG_XML)
            self._reg_ids.append(self._register(
                OBJ_PATH, dnode.interfaces[0], self._on_debug_call, None, None))
        self._own_id = Gio.bus_own_name_on_connection(
            self.conn, BUS_NAME,
            Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
            self._on_name_acquired, self._on_name_lost)
        self.schedule_idle_exit()
        if self.debug:          # main-loop stall detector (reported/reset by Stats())
            self._hb = time.monotonic()
            self.stats["max_stall_ms"] = 0.0

            def beat():
                now = time.monotonic()
                gap = (now - self._hb) * 1000 - 20
                self._hb = now
                if gap > self.stats["max_stall_ms"]:
                    self.stats["max_stall_ms"] = round(gap, 1)
                return True
            GLib.timeout_add(20, beat)

    def _register(self, path, iface, method_cb, get_cb, set_cb):
        reg = getattr(self.conn, "register_object_with_closures2", None)
        if reg is None:
            reg = self.conn.register_object
        return reg(path, iface, method_cb, get_cb, set_cb)

    def do_activate(self):
        if self.initial_files:
            self.show_file(self.initial_files[0].get_uri(), "", False, "")
            self.initial_files = []

    def do_shutdown(self):
        if self.window is not None:
            self.window._save_size()
            self.window._teardown()
        if self._own_id:
            Gio.bus_unown_name(self._own_id)
            self._own_id = 0
        for rid in self._reg_ids:
            try:
                self.conn.unregister_object(rid)
            except Exception:  # noqa: BLE001
                pass
        Adw.Application.do_shutdown(self)

    def _on_name_acquired(self, _conn, name):
        self._owned = True
        self.stats["name_acquired_ms"] = round(_process_age_ms(), 1)
        _log(f"owns {name} ({self.stats['name_acquired_ms']:.0f} ms after exec)")
        if not self._warmed:
            self._warmed = True
            from . import quicklook_renderers as R
            GLib.timeout_add(1500, lambda: R.warm_up() and False)

    def _on_name_lost(self, _conn, name):
        _log(f"lost {name}")
        if self._owned:     # replaced by another previewer: get out of the way
            self.quit()

    def schedule_idle_exit(self):
        if self._idle_src:
            GLib.source_remove(self._idle_src)

        def expire():
            self._idle_src = 0
            if not self.visible:
                _log("idle; exiting")
                self.quit()
            return False
        self._idle_src = GLib.timeout_add_seconds(IDLE_EXIT_S, expire)

    # --------------------------------------------------------------- D-Bus ---
    def _on_method_call(self, conn, sender, path, iface, method, params, invocation):
        try:
            if method == "ShowFile":
                uri, handle, close_if_shown, token = params.unpack()
                invocation.return_value(None)
                GLib.idle_add(self._idle_show, uri, handle, close_if_shown, token,
                              time.monotonic())
            elif method == "Close":
                invocation.return_value(None)
                GLib.idle_add(lambda: self.close() and False)
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod",
                                             f"No such method {method}")
        except Exception as e:  # noqa: BLE001
            _log("method call failed:", e)
            try:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))
            except Exception:  # noqa: BLE001
                pass

    def _on_get_property(self, conn, sender, path, iface, prop):
        if prop == "Visible":
            return GLib.Variant("b", self.visible)
        if prop == "ParentHandle":
            return GLib.Variant("s", self.parent_handle)
        return None

    def _on_debug_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "Snapshot":
            (p,) = params.unpack()
            ok = False
            try:
                ok = self.window is not None and self.window.snapshot_png(p)
            except Exception as e:  # noqa: BLE001
                _log("snapshot failed:", e)
            invocation.return_value(GLib.Variant("(b)", (bool(ok),)))
        elif method == "Key":
            (kv,) = params.unpack()
            h = False
            if self.window is not None:
                h = bool(self.window._on_key(None, kv, 0, Gdk.ModifierType(0)))
            invocation.return_value(GLib.Variant("(b)", (h,)))
        elif method == "Stats":
            invocation.return_value(GLib.Variant("(s)", (json.dumps(self.stats),)))
            self.stats["max_stall_ms"] = 0.0
            self.stats["paint_ms"] = []

    def _emit_props(self, changed):
        if self.conn is None:
            return
        try:
            self.conn.emit_signal(None, OBJ_PATH, "org.freedesktop.DBus.Properties",
                                  "PropertiesChanged",
                                  GLib.Variant("(sa{sv}as)", (IFACE, changed, [])))
        except GLib.Error as e:
            _log("PropertiesChanged failed:", e.message)

    def set_visible(self, visible):
        visible = bool(visible)
        if visible == self.visible:
            return
        self.visible = visible
        self._emit_props({"Visible": GLib.Variant("b", visible)})

    def set_parent_handle(self, handle):
        if self.debug:
            _log(f"ParentHandle -> {handle!r}")
        self.parent_handle = handle
        self._emit_props({"ParentHandle": GLib.Variant("s", handle)})

    def emit_selection_event(self, direction):
        if self.conn is None:
            return
        # Sushi sends the direction as 'u' on the wire (despite 'q' in its XML) and
        # Nautilus's generated proxy accepts it; match the proven wire format.
        try:
            self.conn.emit_signal(None, OBJ_PATH, IFACE, "SelectionEvent",
                                  GLib.Variant("(u)", (direction,)))
        except GLib.Error as e:
            _log("SelectionEvent failed:", e.message)

    # ------------------------------------------------------------- actions ---
    def _idle_show(self, uri, handle, close_if_shown, token, t_recv):
        try:
            self.show_file(uri, handle, close_if_shown, token)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        self.stats.setdefault("dispatch_ms", []).append(
            round((time.monotonic() - t_recv) * 1000, 1))
        return False

    def show_file(self, uri, handle, close_if_shown, token):
        if self._idle_src:
            GLib.source_remove(self._idle_src)
            self._idle_src = 0
        gfile = Gio.File.new_for_uri(uri) if "://" in uri or uri.startswith("file:") \
            else Gio.File.new_for_commandline_arg(uri)
        win = self.window
        if close_if_shown and win is not None and win.get_visible() \
                and win.gfile is not None and win.gfile.equal(gfile):
            win.close_preview()
            return
        if win is None:
            win = self.window = QuickLookWindow(self)
        win.show_file(gfile, handle, token)

    def close(self):
        if self.window is not None and self.window.get_visible():
            self.window.close_preview()


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    debug = bool(os.environ.get("PREVIEW_QL_DEBUG"))
    files = []
    for a in argv[1:]:
        if a == "--debug":
            debug = True
        elif a.startswith("--"):
            continue          # e.g. --gapplication-service: accepted and ignored
        else:
            files.append(Gio.File.new_for_commandline_arg(a))
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Quick Look")
    app = QuickLookApp(debug=debug, initial_files=files)
    return app.run([argv[0]])


if __name__ == "__main__":
    sys.exit(main())
