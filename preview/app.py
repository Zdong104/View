"""Application shell: windows, file routing, drag & drop, menus."""
import os
import signal
import sys
import threading
import time
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import runtime
from .i18n import _, N_, ngettext
from .viewer_base import (CAD_EXTS, IMAGE_EXTS, MEDIA_EXTS, MODEL_EXTS, PDF_EXTS, TABLE_EXTS, TEXT_EXTS,
                          TEXT_NAMES, VIDEO_EXTS, AUDIO_EXTS, kind_of)

APP_ID = runtime.APP_ID
APP_NAME = runtime.APP_NAME

CSS = """
.drop-highlight {
    border: 3px solid @accent_bg_color;
    border-radius: 14px;
    background-color: alpha(@accent_bg_color, 0.12);
    margin: 6px;
}
.preview-osd {
    background-color: alpha(black, 0.62);
    color: white;
    border-radius: 99px;
    padding: 3px 6px;
}
.preview-osd button { color: white; }
row.drop-before, .drop-before { box-shadow: inset 0 3px @accent_bg_color; }
.drop-after { box-shadow: inset 0 -3px @accent_bg_color; }
"""


def _recent_uris():
    """Recently used URIs, newest first (main thread: GtkRecentManager isn't
    thread-safe)."""
    try:
        infos = Gtk.RecentManager.get_default().get_items()
    except Exception:  # noqa: BLE001
        return []

    def stamp(i):
        best = 0
        for getter in (i.get_visited, i.get_modified, i.get_added):
            try:
                d = getter()
                best = max(best, d.to_unix() if d is not None else 0)
            except Exception:  # noqa: BLE001
                pass
        return best
    return [i.get_uri() for i in sorted(infos, key=stamp, reverse=True)]


def _existing_files(uris, limit=10):
    """The first `limit` of `uris` that are local files of a type we can open:
    [(uri, Gio.File), ...].  Touches the files (a missing network share can
    take many seconds), so run it in a thread."""
    out = []
    for uri in uris:
        if not uri or not uri.startswith("file://"):
            continue
        f = Gio.File.new_for_uri(uri)
        if kind_of(f, sniff=True) == "unknown" or not f.query_exists(None):
            continue
        out.append((uri, f))
        if len(out) >= limit:
            break
    return out


def open_files_dialog(parent, on_files):
    """The Open dialog; `on_files(list of Gio.File)` when files were chosen.
    `parent` may be None (the tray icon's Open… with no window open)."""
    fd = Gtk.FileDialog(title=_("Open"))
    filters, default = _make_filters()
    fd.set_filters(filters)
    fd.set_default_filter(default)

    def done(fd_, res):
        try:
            lst = fd_.open_multiple_finish(res)
        except GLib.Error:
            return
        on_files([lst.get_item(i) for i in range(lst.get_n_items())])
    fd.open_multiple(parent, None, done)


def _stop_engines():
    """Background mode: end llama-server, the OCR and translation helpers and
    running recognitions, and unload their models (only those that were
    used: nothing is imported here).  A thread: stopping a process waits."""
    for mod, fn in (("preview.ocr", "release"), ("preview.translate_llm", "stop"),
                    ("preview.translate", "release")):
        m = sys.modules.get(mod)
        if m is not None:
            try:
                getattr(m, fn)()
            except Exception:  # noqa: BLE001
                traceback.print_exc()


def _suffixes(exts):
    return sorted(e.lstrip(".") for e in exts)


def _make_filters():
    store = Gio.ListStore.new(Gtk.FileFilter)

    def mk(name, exts, names=()):
        f = Gtk.FileFilter(name=name)
        for e in _suffixes(exts):
            f.add_suffix(e)
        for n in names:
            f.add_pattern(n)
        return f
    text_names = sorted({v for n in TEXT_NAMES if "." not in n
                         for v in (n, n.capitalize(), n.upper())})
    allf = mk(_("All Supported Files"),
              PDF_EXTS | IMAGE_EXTS | MODEL_EXTS | CAD_EXTS | TABLE_EXTS | MEDIA_EXTS | TEXT_EXTS,
              text_names)
    store.append(allf)
    store.append(mk(_("PDF Documents"), PDF_EXTS))
    store.append(mk(_("Images"), IMAGE_EXTS))
    store.append(mk(_("Video and Audio"), VIDEO_EXTS | AUDIO_EXTS))
    store.append(mk(_("Text and Source Code"), TEXT_EXTS, text_names))
    store.append(mk(_("3D Models"), MODEL_EXTS))
    store.append(mk(_("CAD Drawings (DXF)"), CAD_EXTS))
    store.append(mk(_("Spreadsheets (CSV/TSV)"), TABLE_EXTS))
    anyf = Gtk.FileFilter(name=_("All Files"))
    anyf.add_pattern("*")
    store.append(anyf)
    return store, allf


SHORTCUT_SECTIONS = [
    (N_("General"), ((N_("Open…"), "<Control>o"), (N_("New Window"), "<Control>n"),
                     (N_("New Tab"), "<Control>t"),
                     (N_("New from Clipboard"), "<Control>v"), (N_("Show Inspector"), "<Control>i"),
                     (N_("Full Screen"), "F11"), (N_("Close Tab"), "<Control>w"),
                     (N_("Quit"), "<Control>q"), (N_("Keyboard Shortcuts"), "<Control>question"))),
    (N_("Tabs"), ((N_("Next Tab"), "<Control>Tab <Control>Page_Down"),
                  (N_("Previous Tab"), "<Control><Shift>Tab <Control>Page_Up"),
                  (N_("Move Tab Left"), "<Control><Shift>Page_Up"),
                  (N_("Move Tab Right"), "<Control><Shift>Page_Down"),
                  (N_("Go to Tab"), "<Alt>1...9"))),
    (N_("Video and Audio"), ((N_("Play / Pause"), "space"), (N_("Back 5 Seconds"), "Left"),
                     (N_("Forward 5 Seconds"), "Right"), (N_("Volume Up"), "Up"),
                     (N_("Volume Down"), "Down"), (N_("Mute"), "m"), (N_("Full Screen"), "f"),
                     (N_("Save Frame as PNG…"), "<Control>s"))),
    (N_("Text and Source Code"), ((N_("Save"), "<Control>s"), (N_("Save As…"), "<Control><Shift>s"),
                     (N_("Find"), "<Control>f"), (N_("Go to Line…"), "<Control>g"),
                     (N_("Next Match"), "F3"), (N_("Previous Match"), "<Shift>F3"),
                     (N_("Undo"), "<Control>z"), (N_("Redo"), "<Control><Shift>z"),
                     (N_("Larger Text"), "<Control>plus"), (N_("Smaller Text"), "<Control>minus"),
                     (N_("Actual Text Size"), "<Control>0"))),
    (N_("Images"), ((N_("Zoom In"), "plus"), (N_("Zoom Out"), "minus"), (N_("Fit to Window"), "0"),
                     (N_("Actual Size"), "1"), (N_("Rotate Left"), "<Control>l"),
                     (N_("Rotate Right"), "<Control>r"), (N_("Copy Image"), "<Control>c"),
                     (N_("Save"), "<Control>s"), (N_("Save As…"), "<Control><Shift>s"),
                     (N_("Export As…"), "<Control><Shift>e"), (N_("Toggle Thumbnails"), "F9"),
                     (N_("Select All Thumbnails"), "<Control>a"), (N_("Previous Image"), "Left"),
                     (N_("Next Image"), "Right"), (N_("Remove from List"), "Delete"),
                     (N_("Undo"), "<Control>z"), (N_("Redo"), "<Control><Shift>z"),
                     (N_("Crop"), "<Control>k"), (N_("Adjust Color"), "<Control><Shift>c"),
                     (N_("Print"), "<Control>p"), (N_("Slideshow"), "F5"),
                     (N_("Markup Toolbar"), "<Control><Shift>a"),
                     (N_("Copy All Recognized Text"), "<Control><Shift>t"),
                     (N_("Cancel Selection / Tool"), "Escape"))),
]


def _note_startup(widget, _clock):
    """First frame of the first window: how long startup took (About → Troubleshooting)."""
    if runtime.startup_seconds is None:
        runtime.mark("first frame")
        runtime.startup_seconds = runtime.startup_marks[-1][1]
        t0 = os.environ.get("VIEW_OPEN_T0")      # packaging/snap/open-latency.sh
        if t0:
            try:
                print(f"View: first frame {time.time() - float(t0):.3f} s after VIEW_OPEN_T0 "
                      "(new process)", file=sys.stderr, flush=True)
            except ValueError:
                pass
        if runtime.startup_trace:
            print(f"{APP_NAME} {runtime.VERSION} startup trace "
                  "(seconds since the process started):", flush=True)
            print(runtime.startup_report(), flush=True)
            mods = runtime.loaded_modules()
            if mods:
                windir = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows"))
                ours = [m for m in mods if not os.path.normcase(
                    m.removeprefix("\\\\?\\")).startswith(windir)]
                mb = sum(os.path.getsize(m) for m in ours if os.path.exists(m)) / 1e6
                print(f"{len(mods)} DLLs loaded, {len(ours)} of them View's own "
                      f"({mb:.0f} MB)", flush=True)
                gl = [m for m in mods if os.path.basename(m).lower() == "opengl32.dll"]
                print("opengl32.dll: " + (", ".join(gl) or "not loaded"), flush=True)
                if os.environ.get("VIEW_STARTUP_TRACE_DLLS"):
                    for m in sorted(ours, key=lambda m: -os.path.getsize(m)):
                        print(f"  {os.path.getsize(m) / 1e6:6.1f} MB  {m}", flush=True)
            # keep running a little: loading often goes on after the first frame
            app = widget.get_application()
            wait = float(os.environ.get("VIEW_STARTUP_TRACE_WAIT", "5"))

            def done():
                if runtime.stall_watch is not None:
                    print(runtime.stall_watch.report(), flush=True)
                for w in app.get_windows():
                    failed = getattr(getattr(w, "viewer", None), "_gl_failed", None)
                    if failed:
                        print(f"3D viewer: OpenGL failed: {failed}", flush=True)
                if "preview.opengl_win" in sys.modules:
                    print("OpenGL: " + str(sys.modules["preview.opengl_win"]._state), flush=True)
                app.quit()
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(int(wait * 1000), done)
    return GLib.SOURCE_REMOVE


def debug_info(window=None) -> str:
    """Plain-text system report for bug reports (About → Troubleshooting)."""
    import platform
    lines = [f"{APP_NAME} {runtime.VERSION}",
             f"System: {platform.platform()} ({platform.machine()})",
             "Build: " + ("Windows (frozen)" if runtime.FROZEN else
                          "snap" if runtime.IN_SNAP else "source / .deb"),
             f"Python: {platform.python_version()}",
             f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
             f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}."
             f"{Adw.get_micro_version()}, GLib {GLib.MAJOR_VERSION}.{GLib.MINOR_VERSION}"]
    if runtime.startup_seconds is not None:
        lines.append(f"Startup: {runtime.startup_seconds:.1f} s to the first window")
        lines += ["  " + ln for ln in runtime.startup_report().splitlines()]
    display = Gdk.Display.get_default()
    if display is not None:
        lines.append(f"Display: {type(display).__name__}")
    if window is not None and window.get_native() is not None:
        r = window.get_native().get_renderer()
        lines.append(f"Renderer: {type(r).__name__ if r is not None else '?'}")
    if runtime.IS_WINDOWS:
        from . import opengl_win
        lines.append("OpenGL (3D): " + (opengl_win._state or "not used yet"))
    for var in ("GSK_RENDERER", "GDK_BACKEND", "GDK_DEBUG", "GDK_SCALE"):
        if os.environ.get(var):
            lines.append(f"{var}={os.environ[var]}")
    try:
        from . import ocr
        helper = ocr.helper_path()
        lines.append("Text recognition: " + (f"helper {helper}" if helper else
                                             "in-process" if ocr.is_available() else
                                             "not installed"))
    except Exception as e:  # noqa: BLE001
        lines.append(f"Text recognition: error {e}")
    try:
        from .touchpad import Touchpad
        pad = Touchpad()
        try:
            lines.append(f"Touchpad: {pad.name}, {pad.width_mm:.0f} × {pad.height_mm:.0f} mm")
        finally:
            pad.close()
    except Exception as e:  # noqa: BLE001
        lines.append(f"Touchpad: unavailable ({e})")
    return "\n".join(lines) + "\n"


def _to_front(win):
    """Windows: present() activates a window, but a View that is not the
    foreground app then only flashes its taskbar button.  A forwarded launch
    (preview.instance: AllowSetForegroundWindow) or a click on the tray icon
    gave us the right to come to the front: use it."""
    if not runtime.IS_WINDOWS:
        return
    try:
        import ctypes
        surface = win.get_surface()
        if surface is None:
            return
        api = ctypes.pythonapi
        api.PyCapsule_GetPointer.restype = ctypes.c_void_p
        api.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
        ptr = api.PyCapsule_GetPointer(surface.__gpointer__, None)
        k32 = ctypes.windll.kernel32
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        for name in ("libgtk-4-1.dll", "gtk-4-1.dll", "gtk-4.dll"):     # MSYS2, gvsbuild
            if k32.GetModuleHandleW(name):
                get = ctypes.CDLL(name).gdk_win32_surface_get_handle
                get.argtypes, get.restype = [ctypes.c_void_p], ctypes.c_void_p
                hwnd = get(ptr)
                if hwnd:
                    user32 = ctypes.windll.user32
                    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                    user32.SetForegroundWindow(hwnd)
                return
    except Exception:  # noqa: BLE001
        pass


def _set_startup_id(win, token):
    """Present `win` with a launch's activation token (Wayland xdg-activation,
    X11 startup notification), so it is raised rather than flagged."""
    try:
        win.set_startup_id(token)
    except Exception:  # noqa: BLE001
        pass


class MainWindow(Adw.ApplicationWindow):
    """A window of tabs (Adw.TabView of preview.tabs.TabHost).  The tab bar
    shows only with two tabs or more, so one document looks as it always
    did.  `viewer`, `header`, load_files() and friends are the selected
    tab's.  Split view ("Open in Split View"): two panes side by side in a
    Gtk.Paned, each its own tab view and (always shown) tab bar; the pane
    last focused or clicked is the current one, and a pane that loses its
    last tab collapses the window back to one."""

    def __init__(self, app, empty_tab=True):
        super().__init__(application=app, default_width=1040, default_height=740,
                         title=APP_NAME)
        self._confirmed_close = False
        self._menu_page = None          # the tab whose context menu is open
        self._ask_queue = []            # unsaved-changes prompts, one at a time
        self._asking = False
        self.inspector = None

        self._shortcuts = None          # the tab views' keyboard shortcuts
        self._split_check = 0           # a pending _check_split
        self._panes = {}                # split view: pane -> (its view, its tab bar)
        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append(_("Move to New Window"), "win.tab-move")
        for label, action in ((_("Open in Split View"), "win.tab-split"),
                              (_("Move to Other Side"), "win.tab-other-side")):
            item = Gio.MenuItem.new(label, action)
            item.set_attribute_value("hidden-when", GLib.Variant.new_string("action-disabled"))
            sec.append_item(item)
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append(_("Close Other Tabs"), "win.tab-close-others")
        sec.append(_("Close Tab"), "win.tab-close")
        menu.append_section(None, sec)
        self._tab_menu = menu
        self.tab_view = self._new_view()
        self._views = [self.tab_view]   # one, or the left and right panes'
        self._cur_view = self.tab_view  # the pane last focused or clicked

        self.tab_bar = Adw.TabBar(view=self.tab_view, autohide=True)
        self.toolbar = Adw.ToolbarView(content=self.tab_view)
        # the empty part of the tab bar moves the window, like a header bar
        self._bar_handle = Gtk.WindowHandle(child=self.tab_bar)
        self.toolbar.add_top_bar(self._bar_handle)
        self.set_content(self.toolbar)
        self.connect("notify::focus-widget", lambda *_: self._on_focus())

        for name, cb in (("open", self._open_action),
                         ("inspector", self.toggle_inspector),
                         ("close", self.close),
                         ("close-tab", self.close_tab),
                         ("new-tab", lambda: self.add_tab()),
                         ("fullscreen", lambda: self.unfullscreen() if self.is_fullscreen()
                          else self.fullscreen()),
                         ("tab-move", lambda: self.move_to_new_window(self._menu_target())),
                         ("tab-split", lambda: self.split(self._menu_target())),
                         ("tab-other-side", lambda: self.move_to_other_side(self._menu_target())),
                         ("tab-close", lambda: self._close_page(self._menu_target())),
                         ("tab-close-others", self._close_others)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(act)

        # Ctrl+V on an empty or image tab: paste the clipboard as a new image.
        # Other viewers keep Ctrl+V (the callback declines so the event propagates).
        paste = Gtk.ShortcutController()
        paste.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>v"),
            Gtk.CallbackAction.new(self._paste_shortcut)))
        self.add_controller(paste)

        # Full screen: hide the bars, reveal them when the pointer nears the top.
        self._chrome_hidden = False
        self.connect("notify::fullscreened", lambda *_: self._sync_chrome())
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.toolbar.add_controller(motion)

        self.connect("close-request", self._on_close_request)
        self.connect("unrealize", self._on_unrealize)
        if runtime.startup_seconds is None:
            self.add_tick_callback(_note_startup)
        if empty_tab:
            self.add_tab()
        self._update_tab_actions()

    # ---- tabs ----------------------------------------------------------------------
    def _new_view(self):
        """A tab view with this window's tab menu and handlers (one, or one
        per pane of a split view)."""
        tv = Adw.TabView()
        try:
            # Ctrl+(Shift+)Home/End are the PDF viewer's first/last page and text
            # selection: leave them to the document
            S = Adw.TabViewShortcuts
            off = (S.CONTROL_HOME | S.CONTROL_END | S.CONTROL_SHIFT_HOME | S.CONTROL_SHIFT_END)
            self._shortcuts = S(int(S.ALL_SHORTCUTS) & ~int(off))
            tv.set_shortcuts(self._shortcuts)
        except (AttributeError, TypeError):
            pass
        tv.set_menu_model(self._tab_menu)
        tv.connect("notify::selected-page", lambda *_: self._on_selected())
        tv.connect("page-attached", self._on_page_attached)
        tv.connect("page-detached", self._on_page_detached)
        tv.connect("close-page", self._on_close_page)
        tv.connect("create-window", self._on_create_window)
        tv.connect("setup-menu", self._on_setup_menu)
        # a tab dragged out of an emptied pane: collapse once the drag ends
        tv.connect("notify::is-transferring-page", lambda *_: self._queue_split_check())
        return tv

    def hosts(self):
        return [p.get_child() for p in self._pages()]

    def current_host(self):
        for tv in [self._cur_view] + self._views:
            page = tv.get_selected_page()
            if page is not None:
                return page.get_child()
        return None

    def all_viewers(self):
        return [h.viewer for h in self.hosts() if h.viewer is not None]

    def add_tab(self, select=True):
        """A new empty tab (the Recent page) in the current pane, selected
        unless `select` is False."""
        from .tabs import TabHost
        host = TabHost(self.get_application())
        tv = self._cur_view
        page = tv.append(host)
        host.refresh_title()
        if select:
            tv.set_selected_page(page)
        return host

    def select_host(self, host):
        tv = self._view_of_host(host)
        if tv is not None:
            tv.set_selected_page(tv.get_page(host))
            self._set_current(tv)

    @staticmethod
    def _view_pages(tv):
        return [tv.get_nth_page(i) for i in range(tv.get_n_pages())]

    def _pages(self):
        return [p for tv in self._views for p in self._view_pages(tv)]

    def _view_of_host(self, host):
        tv = host.tab_view() if host is not None else None
        return tv if tv in self._views else None

    def _view_of(self, page):
        return next((tv for tv in self._views if page in self._view_pages(tv)), None)

    def _menu_target(self):
        page, self._menu_page = self._menu_page, None
        if page is None or page not in self._pages():
            page = self._cur_view.get_selected_page()
        return page

    def _on_setup_menu(self, _view, page):
        if page is not None:
            self._menu_page = page
        self._update_tab_actions()

    def _update_tab_actions(self):
        split = len(self._views) > 1
        several = len(self._pages()) > 1
        for name, on in (("tab-move", several), ("tab-close-others", several),
                         ("tab-split", not split and self.tab_view.get_n_pages() > 1),
                         ("tab-other-side", split)):
            act = self.lookup_action(name)
            if act is not None:
                act.set_enabled(on)

    def _on_selected(self):
        cur = self.current_host()
        for h in self.hosts():
            h.set_active(h is cur)
        self.sync_title()

    def _on_page_attached(self, _view, page, _pos):
        host = page.get_child()
        host.set_active(host is self.current_host())
        host.refresh_title()
        self._sync_title_buttons()
        self._sync_chrome()
        self._update_tab_actions()

    def _on_page_detached(self, _view, _page, _pos):
        self._update_tab_actions()
        if len(self._views) > 1:
            self._queue_split_check()
        elif self.tab_view.get_n_pages() == 0:
            # the last tab closed or moved to another window
            GLib.idle_add(self._close_if_empty)

    def _close_if_empty(self):
        if self.get_application() is not None and not self._pages():
            self._confirmed_close = True
            self.close()
        return GLib.SOURCE_REMOVE

    def _on_create_window(self, _view):
        """A tab dragged out of the tab bar: it gets a window of its own."""
        win = MainWindow(self.get_application(), empty_tab=False)
        win.set_default_size(self.get_width() or 1040, self.get_height() or 740)
        win.present()
        return win.tab_view

    def move_to_new_window(self, page):
        tv = self._view_of(page)
        if tv is None or len(self._pages()) < 2:
            return None
        win = MainWindow(self.get_application(), empty_tab=False)
        win.set_default_size(self.get_width() or 1040, self.get_height() or 740)
        tv.transfer_page(page, win.tab_view, 0)
        win.tab_view.set_selected_page(page)
        win.present()
        return win

    def close_tab(self):
        """Ctrl+W: close the tab (the window with its last one)."""
        h = self.current_host()
        page = h.page() if h is not None else None
        if page is None:
            self.close()
        else:
            self._close_page(page)

    def _close_page(self, page):
        tv = self._view_of(page)
        if tv is not None:
            tv.close_page(page)

    def _close_others(self):
        page = self._menu_target()
        tv = self._view_of(page)
        if tv is None:
            return
        tv.close_other_pages(page)
        for other in self._views:
            if other is not tv:
                for p in self._view_pages(other):
                    other.close_page(p)

    # ---- split view: two panes side by side --------------------------------------
    def is_split(self):
        return len(self._views) > 1

    def _set_current(self, tv):
        """The pane new tabs, Ctrl+W, the title and the Inspector follow."""
        if tv is self._cur_view or tv not in self._views:
            return
        self._cur_view = tv
        self._apply_shortcuts()
        self._on_selected()

    def _apply_shortcuts(self):
        # Ctrl+Tab and friends: the current pane's only
        if self._shortcuts is None:
            return
        for tv in self._views:
            tv.set_shortcuts(self._shortcuts if tv is self._cur_view
                             else Adw.TabViewShortcuts(0))

    def _on_focus(self):
        if not self.is_split():
            return
        w = self.get_focus()
        while w is not None:
            tv = w if w in self._views else self._panes.get(w, (None,))[0]
            if tv is not None:
                self._set_current(tv)
                return
            w = w.get_parent()

    def _make_pane(self, tv):
        bar = Adw.TabBar(view=tv, autohide=False)
        pane = Adw.ToolbarView(content=tv, width_request=280)
        pane.add_top_bar(Gtk.WindowHandle(child=bar))
        pane.set_reveal_top_bars(self.toolbar.get_reveal_top_bars())
        click = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_: self._set_current(tv))
        pane.add_controller(click)
        self._panes[pane] = (tv, bar)
        return pane

    def split(self, page):
        """Show `page` in a second pane, right of the selected tab."""
        if self.is_split() or page is None or self.tab_view.get_n_pages() < 2 \
                or self._view_of(page) is not self.tab_view:
            return
        right = self._new_view()
        self.toolbar.set_content(None)
        self.tab_bar.set_view(None)
        self._bar_handle.set_visible(False)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL,
                          start_child=self._make_pane(self.tab_view),
                          end_child=self._make_pane(right),
                          resize_start_child=True, resize_end_child=True,
                          shrink_start_child=False, shrink_end_child=False)
        if self.get_width() > 0:
            paned.set_position(self.get_width() // 2)
        self._views = [self.tab_view, right]
        self.toolbar.set_content(paned)
        self.tab_view.transfer_page(page, right, 0)
        right.set_selected_page(page)
        self._set_current(right)
        self._update_tab_actions()
        self._sync_chrome()
        GLib.idle_add(self._focus_current)

    def move_to_other_side(self, page):
        src = self._view_of(page)
        if not self.is_split() or src is None:
            return
        dst = next(tv for tv in self._views if tv is not src)
        src.transfer_page(page, dst, dst.get_n_pages())
        dst.set_selected_page(page)
        self._set_current(dst)
        GLib.idle_add(self._focus_current)

    def _queue_split_check(self):
        if self.is_split() and not self._split_check:
            # before the next frame: no frame with an empty pane
            self._split_check = GLib.idle_add(self._check_split, priority=GLib.PRIORITY_HIGH_IDLE)

    def _check_split(self):
        """A pane without tabs: back to one (not while a tab is being dragged:
        a cancelled drag puts it back)."""
        self._split_check = 0
        if not self.is_split() or any(tv.get_is_transferring_page() for tv in self._views):
            return GLib.SOURCE_REMOVE
        left = [tv for tv in self._views if tv.get_n_pages() > 0]
        if len(left) < 2:
            self._unsplit(left[0] if left else self.tab_view)
            if not left:
                self._close_if_empty()
        return GLib.SOURCE_REMOVE

    def _unsplit(self, keep):
        self.toolbar.set_content(None)
        for pane, (_tv, bar) in self._panes.items():
            bar.set_view(None)
            pane.set_content(None)
        self._panes = {}
        self.tab_view = self._cur_view = keep
        self._views = [keep]
        self.tab_bar.set_view(keep)
        self._bar_handle.set_visible(True)
        self.toolbar.set_content(keep)
        self._sync_title_buttons()
        self._apply_shortcuts()
        self._update_tab_actions()
        self._sync_chrome()
        self._on_selected()
        GLib.idle_add(self._focus_current)

    def _sync_title_buttons(self):
        # the window buttons only at the window's edges
        split = self.is_split()
        for i, tv in enumerate(self._views):
            for p in self._view_pages(tv):
                hb = p.get_child().header
                hb.set_show_start_title_buttons(not split or i == 0)
                hb.set_show_end_title_buttons(not split or i == len(self._views) - 1)

    def _focus_current(self):
        # the focus was in a pane that moved: give it to the current tab
        h = self.current_host()
        f = self.get_focus()
        if h is not None and (f is None or not f.is_ancestor(h)) and h.viewer is not None:
            h.viewer.grab_focus()
        return GLib.SOURCE_REMOVE

    def _on_close_page(self, view, page):
        host = page.get_child()

        def finish(ok):
            view.close_page_finish(page, bool(ok))
            if ok:
                GLib.idle_add(lambda: (host.release(), GLib.SOURCE_REMOVE)[1])
        if host.confirmed_close or not host.is_dirty():
            finish(True)
        else:
            self._ask(host, finish)
        return True

    def _ask(self, host, done):
        """Ask about one tab's unsaved changes at a time (Close Other Tabs, a
        window closing)."""
        self._ask_queue.append((host, done))
        self._ask_next()

    def _ask_next(self):
        if self._asking or not self._ask_queue:
            return
        host, done = self._ask_queue.pop(0)
        if host.get_root() is not self:            # moved away meanwhile
            done(False)
            self._ask_next()
            return
        self._asking = True
        self.select_host(host)

        def answered(ok):
            self._asking = False
            done(ok)
            self._ask_next()
        host.ask_discard(answered)

    # ---- facade: the selected tab ------------------------------------------------
    @property
    def viewer(self):
        h = self.current_host()
        return h.viewer if h is not None else None

    @property
    def header(self):
        h = self.current_host()
        return h.header if h is not None else None

    @property
    def card_overlay(self):
        """Where translate_ui's floating cards go: the selected tab's overlay."""
        h = self.current_host()
        return h.card_overlay if h is not None else None

    def is_empty(self):
        h = self.current_host()
        return h is None or h.is_empty()

    def show_recents(self, items):
        for h in self.hosts():
            if h.is_empty():
                h.show_recents(items)

    def show_toast(self, message, button_label=None, on_button=None):
        h = self.current_host() or self.add_tab()
        h.show_toast(message, button_label, on_button)

    def refresh_title(self):
        h = self.current_host()
        if h is not None:
            h.refresh_title()          # (calls sync_title)
        else:
            self.sync_title()

    def sync_title(self):
        """Window title and Inspector follow the selected tab."""
        if self.inspector is not None:
            self.inspector.refresh()
        h = self.current_host()
        if h is None or h.viewer is None:
            self.set_title(APP_NAME)
            return
        title, _sub = h.titles()
        self.set_title(f"{title} — {APP_NAME}")

    def ensure_viewer(self, kind):
        return (self.current_host() or self.add_tab()).ensure_viewer(kind)

    def load_files(self, kind, files):
        (self.current_host() or self.add_tab()).load_files(kind, files)

    def show_clipboard_image(self, pil_image):
        (self.current_host() or self.add_tab()).show_clipboard_image(pil_image)

    def open_files(self, files):
        self.get_application().route_files(list(files), self)

    def open_dialog(self):
        open_files_dialog(self, self.open_files)

    def _open_action(self):
        h = self.current_host()
        if h is not None:
            h.open_dialog()
        else:
            self.open_dialog()

    def _paste_shortcut(self, *_):
        if self.viewer is not None and not hasattr(self.viewer, "add_untitled"):
            return False
        self.get_application().new_from_clipboard(self)
        return True

    # ---- full screen -------------------------------------------------------------------
    def set_chrome_visible(self, visible):
        """Hide/show the tab bar and header bars (slideshow)."""
        self._chrome_hidden = not visible
        self._sync_chrome()

    def _reveal_bars(self, reveal):
        self.toolbar.set_reveal_top_bars(reveal)
        for pane in self._panes:
            pane.set_reveal_top_bars(reveal)
        for h in self.hosts():
            h.toolbar.set_reveal_top_bars(reveal)

    def _sync_chrome(self):
        self._reveal_bars(not (self._chrome_hidden or self.is_fullscreen()))

    def _on_motion(self, _c, _x, y):
        if self.is_fullscreen() and not self._chrome_hidden:
            if y < 6 and not self.toolbar.get_reveal_top_bars():
                self._reveal_bars(True)
            elif y > 90 and self.toolbar.get_reveal_top_bars():
                self._reveal_bars(False)

    def show_inspector(self):
        from .inspector import Inspector
        if self.inspector is None:
            self.inspector = Inspector(self)
            self.inspector.set_destroy_with_parent(True)
        self.inspector.present()

    def toggle_inspector(self):
        from .inspector import Inspector
        if self.inspector is None:
            self.inspector = Inspector(self)
            self.inspector.set_destroy_with_parent(True)
        if self.inspector.get_visible():
            self.inspector.close()
        else:
            self.inspector.present()

    # ---- closing ---------------------------------------------------------------------
    def _on_unrealize(self, *_):
        # the window is being destroyed: a separate translation window of one
        # of its documents goes too (its tabs are still here)
        tv = sys.modules.get("preview.translate_view")
        if tv is not None:
            for v in self.all_viewers():
                try:
                    tv.shutdown_for(v)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()

    def _on_close_request(self, *_a):
        """Every tab with unsaved changes asks first, one after the other; a
        Cancel keeps the window (and stops a Quit)."""
        if self._confirmed_close:
            return False
        dirty = [h for h in self.hosts() if not h.confirmed_close and h.is_dirty()]
        if not dirty:
            return False
        if self._asking or self._ask_queue:
            return True
        host = dirty[0]

        def decided(ok):
            if ok:
                host.confirmed_close = True
                # the next one, or close (once the dialog has gone: a window
                # with a dialog open only closes the dialog)
                GLib.idle_add(lambda: (self.close(), GLib.SOURCE_REMOVE)[1])
            else:
                for h in self.hosts():
                    h.confirmed_close = False
                app = self.get_application()
                if app is not None:
                    app.cancel_quit()      # a Quit that was closing every window stops
        self._ask(host, decided)
        return True


class PreviewApp(Adw.Application):
    def __init__(self):
        GLib.set_application_name(APP_NAME)
        # NON_UNIQUE everywhere: owning our ID on the session bus needs a dbus
        # slot in the snap (the Snap Store holds every upload with one for
        # manual review), so preview.instance hands launches to the running
        # View instead, on every platform.
        flags = Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.recent_menu = Gio.Menu()
        self.recent_items = []          # [(uri, Gio.File)], found by _refresh_recent
        self._recent_gen = 0
        self.primary_menu = self._build_menu()
        self._want_signature = False
        self.updater = None
        self._updater_started = False
        self._server = None             # preview.instance.Server: this is the running View
        self._background = False        # no window open, still running
        self._quitting = False          # Quit is closing the windows: then exit
        self._warming = False
        self.add_main_option("new-signature", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Draw a new signature with the touchpad", None)
        self.connect("handle-local-options", self._on_local_options)

    # ---- command line ------------------------------------------------------------
    def _on_local_options(self, _app, options):
        # (a running View gets --new-signature through preview.instance)
        if options.contains("new-signature"):
            self._want_signature = True
        return -1

    def _build_menu(self):
        m = Gio.Menu()
        s1 = Gio.Menu()
        s1.append(_("New Window"), "app.new-window")
        s1.append(_("New Tab"), "win.new-tab")
        s1.append(_("Open…"), "win.open")
        s1.append_submenu(_("Open Recent"), self.recent_menu)
        s1.append(_("New from Clipboard"), "app.new-from-clipboard")
        m.append_section(None, s1)
        s4 = Gio.Menu()
        s4.append(_("Show Inspector"), "win.inspector")
        s4.append(_("Full Screen"), "win.fullscreen")
        m.append_section(None, s4)
        s2 = Gio.Menu()
        s2.append(_("Combine Images into PDF…"), "app.combine-images")
        s2.append(_("New Signature…"), "app.new-signature")
        m.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append(_("Preferences"), "app.preferences")
        s3.append(_("Keyboard Shortcuts"), "app.shortcuts")
        s3.append(_("Check for Updates…"), "app.check-updates")
        s3.append(_("About View"), "app.about")
        s3.append(_("Quit"), "app.quit")
        m.append_section(None, s3)
        return m

    def do_startup(self):
        runtime.mark("application registered")
        if runtime.stall_watch is not None:
            GLib.timeout_add(50, runtime.stall_watch.beat)
        Adw.Application.do_startup(self)
        runtime.mark("GTK and libadwaita initialized")
        prov = Gtk.CssProvider()
        prov.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        from . import icons
        icons.install()
        for name, cb in (("new-window", self._new_window), ("quit", self._quit),
                         ("about", self._about), ("shortcuts", self._shortcuts),
                         ("combine-images", self._combine_images),
                         ("new-signature", self._new_signature),
                         ("new-from-clipboard", self._new_from_clipboard),
                         ("preferences", self._preferences),
                         ("check-updates", self._check_updates),
                         ("clear-recent", self._clear_recent)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(a)
        self.set_accels_for_action("app.new-window", ["<Control>n"])
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("app.shortcuts", ["<Control>question", "F1"])
        a = Gio.SimpleAction.new("open-recent", GLib.VariantType.new("s"))
        a.connect("activate", lambda _a, p: self.route_files([Gio.File.new_for_uri(p.get_string())]))
        self.add_action(a)
        self._recent_mgr = Gtk.RecentManager.get_default()
        self._recent_mgr.connect("changed", lambda *_: self._refresh_recent())
        self._rebuild_recent()
        self._refresh_recent()
        self.set_accels_for_action("win.open", ["<Control>o"])
        self.set_accels_for_action("win.inspector", ["<Control>i"])
        self.set_accels_for_action("app.new-from-clipboard", ["<Control><Shift>n"])
        self.set_accels_for_action("win.close-tab", ["<Control>w"])
        self.set_accels_for_action("win.new-tab", ["<Control>t"])
        self.set_accels_for_action("win.fullscreen", ["F11"])
        runtime.mark("menus and actions set up")
        self._setup_resident()

    def do_shutdown(self):
        if self._server is not None:
            self._server.close()
            # Windows keeps a dead process's notification icon until the
            # pointer passes over it: take ours away (Linux: it goes with us)
            if runtime.IS_WINDOWS:
                from . import tray
                tray.hide()
        Adw.Application.do_shutdown(self)

    # ---- recent files ------------------------------------------------------------
    def add_recent(self, gfile):
        try:
            if gfile is not None and gfile.get_path():
                Gtk.RecentManager.get_default().add_item(gfile.get_uri())
        except Exception:  # noqa: BLE001
            pass

    def _refresh_recent(self):
        """Find which recent files still exist in a thread (never delay the
        first window on a slow or missing drive), then update the Open Recent
        menu and the Recent list of empty windows."""
        self._recent_gen += 1
        gen, uris = self._recent_gen, _recent_uris()

        def work():
            items = _existing_files(uris, 10)

            def done():
                if gen == self._recent_gen:
                    self.recent_items = items
                    self._rebuild_recent()
                    for w in self.get_windows():
                        if isinstance(w, MainWindow):
                            w.show_recents(items)          # (its empty tabs)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True, name="view-recent").start()

    def _rebuild_recent(self):
        self.recent_menu.remove_all()
        items = self.recent_items
        sec = Gio.Menu()
        for uri, f in items:
            item = Gio.MenuItem.new(f.get_basename() or uri, None)
            item.set_action_and_target_value("app.open-recent", GLib.Variant("s", uri))
            sec.append_item(item)
        if not items:
            sec.append(_("No Recent Files"), "app.none")
        self.recent_menu.append_section(None, sec)
        if items:
            clear = Gio.Menu()
            clear.append(_("Clear Menu"), "app.clear-recent")
            self.recent_menu.append_section(None, clear)

    def _clear_recent(self):
        mgr = Gtk.RecentManager.get_default()
        for uri, _f in _existing_files(_recent_uris(), 1000):
            try:
                mgr.remove_item(uri)
            except GLib.Error:
                pass
        self.recent_items = []
        self._rebuild_recent()

    def _new_from_clipboard(self):
        self.new_from_clipboard(None)

    def new_from_clipboard(self, win=None):
        """Paste the clipboard image as a new untitled image (into `win`'s tab
        if it is empty or an image tab, else a new tab or window)."""
        from PIL import Image
        import io
        from .tabs import tabs_enabled
        win = win or self._active_main()
        host = None
        if win is not None:
            cur = win.current_host()
            if cur is not None and (cur.viewer is None or hasattr(cur.viewer, "add_untitled")):
                host = cur
            elif tabs_enabled():
                host = win.add_tab()
        if host is None:
            cand = next((w for w in self.get_windows()
                         if isinstance(w, MainWindow) and w.is_empty()), None)
            host = (cand or MainWindow(self)).current_host()
        host.present()
        win = host
        clip = win.get_clipboard()

        def done(c, res):
            try:
                tex = c.read_texture_finish(res)
            except GLib.Error:
                tex = None
            if tex is None:
                win.show_toast(_("No image in the clipboard"))
                return
            data = tex.save_to_png_bytes().get_data()
            im = Image.open(io.BytesIO(data))
            im.load()
            win.show_clipboard_image(im)
        clip.read_texture_async(None, done)

    def do_activate(self):
        wins = self.get_windows()
        if wins:
            self._present_any()
        else:
            win = MainWindow(self)
            runtime.mark("window built")
            win.present()
            runtime.mark("window shown")
        self._start_updater()
        self._start_screenshots()
        if self._want_signature:
            self._want_signature = False
            GLib.idle_add(lambda: (self._new_signature(), False)[1])

    def do_open(self, files, n_files, hint):
        self.route_files(list(files), None)
        self._start_updater()
        self._start_screenshots()

    def _start_updater(self):
        """Background update checks, only for the real app (not --selftest
        or --startup-trace)."""
        if self._updater_started or runtime.startup_trace or \
                self.get_application_id() != APP_ID:
            return
        self._updater_started = True
        # the first check is 20 s away: load the network code (urllib, ssl)
        # once the first window is up, not before
        GLib.idle_add(lambda: (self._get_updater().start(), GLib.SOURCE_REMOVE)[1])

    def _start_screenshots(self):
        """Screenshot quick edit: once, a little after the first window, make
        the login autostart and the watcher match Preferences (on by default),
        only for the real app (not --selftest or --startup-trace)."""
        if getattr(self, "_screenshots_synced", False) or runtime.startup_trace or \
                self.get_application_id() != APP_ID:
            return
        self._screenshots_synced = True

        def go():
            try:
                from . import screenshot
                if screenshot.supported():
                    screenshot.sync()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add_seconds(3, go)

    def _get_updater(self):
        if self.updater is None:
            from .updates import Updater
            self.updater = Updater(self)
        return self.updater

    # ---- routing -------------------------------------------------------------------
    def _active_main(self):
        w = self.get_active_window()
        if isinstance(w, MainWindow):
            return w
        for w in self.get_windows():
            if isinstance(w, MainWindow):
                return w
        return None

    def route_files(self, files, target=None, startup_id=""):
        """Open files: all images in one tab, each PDF/model in its own (PDFs
        share one when the viewer can show several).  With "Open files in new
        tabs" (Preferences, on by default) they are new tabs of `target` (a
        MainWindow or one of its tabs; else the active or most recently used
        window), otherwise new windows.  An empty `target` tab (or empty
        selected tab of the active window) is reused for the first group.
        `startup_id`: the launch's activation token, so the window comes to
        the front (Wayland) instead of a "View is ready" notification.
        Returns the first window shown."""
        from .tabs import TabHost, tabs_enabled
        images, pdfs, models, cads, tables, videos, texts, unknown = [], [], [], [], [], [], [], []
        files, empty = self._expand_folders(files)
        for f in files:
            k = kind_of(f, sniff=True)
            {"image": images, "pdf": pdfs, "model": models, "cad": cads, "table": tables,
             "video": videos, "text": texts}.get(k, unknown).append(f)
        tabbed = tabs_enabled()
        target_host = target if isinstance(target, TabHost) else None
        target_win = target_host.get_root() if target_host is not None else target
        if not isinstance(target_win, MainWindow):
            target_win = None
        if target_host is not None:
            spare = target_host if target_host.is_empty() else None
        elif target_win is not None:
            spare = target_win.current_host() if target_win.is_empty() else None
        else:
            cand = self._active_main()
            spare = cand.current_host() if cand is not None and cand.is_empty() else None
        dest = [target_win]             # where new tabs go
        first = [None]                  # the first tab filled
        token = [startup_id]

        def show(where):
            """Select the tab `where` (or take the window) and present its window."""
            if isinstance(where, TabHost):
                w = where.get_root()
                if isinstance(w, MainWindow):
                    w.select_host(where)
            else:
                w = where
            if token[0]:
                _set_startup_id(w, token[0])
                token[0] = ""
            w.present()
            _to_front(w)

        def host():
            nonlocal spare
            if spare is not None:
                h, spare = spare, None
                if tabbed:
                    dest[0] = h.get_root()
            elif tabbed:
                w = dest[0]
                if not isinstance(w, MainWindow) or w.get_application() is None:
                    w = self._active_main() if target is None else None
                    w = dest[0] = w or MainWindow(self, empty_tab=False)
                h = w.add_tab(select=False)
            else:
                h = MainWindow(self).current_host()
            if first[0] is None:
                first[0] = h
            return h

        groups = []
        if images:
            groups.append(("image", images))
        if len(pdfs) > 1 and self._pdf_multi():
            groups.append(("pdf", pdfs))
        else:
            groups += [("pdf", [f]) for f in pdfs]
        groups += [("model", [f]) for f in models]
        groups += [("cad", [f]) for f in cads]
        groups += [("table", [f]) for f in tables]
        groups += [("video", [f]) for f in videos]
        groups += [("text", [f]) for f in texts]
        for kind, fl in groups:
            h = host()
            h.load_files(kind, fl)
            if not tabbed:
                show(h)
        if tabbed and first[0] is not None:
            show(first[0])

        def anchor():
            a = first[0] or target_host or target_win or self._active_main() or MainWindow(self)
            show(a)
            first[0] = first[0] or a
            return a
        if unknown:
            names = ", ".join(f.get_basename() or "?" for f in unknown[:3])
            msg = (_("Unsupported file: {names}").format(names=names) if len(unknown) == 1
                   else ngettext("{n} unsupported file: {names}…",
                                 "{n} unsupported files: {names}…",
                                 len(unknown)).format(n=len(unknown), names=names))
            anchor().show_toast(msg)
        if empty:
            anchor().show_toast(_("No images in {folder}").format(
                folder=empty[0].get_basename() or _("this folder")))
        w = first[0]
        return w.get_root() if isinstance(w, TabHost) else w

    @staticmethod
    def _expand_folders(files):
        """A folder opens the images directly in it, sorted by name, like
        Preview. Folders without images come back separately."""
        out, empty = [], []
        for f in files:
            if f.query_file_type(Gio.FileQueryInfoFlags.NONE, None) != Gio.FileType.DIRECTORY:
                out.append(f)
                continue
            names = []
            try:
                for info in f.enumerate_children("standard::name,standard::type",
                                                 Gio.FileQueryInfoFlags.NONE, None):
                    if info.get_file_type() == Gio.FileType.REGULAR:
                        names.append(info.get_name())
            except GLib.Error:
                pass
            imgs = [f.get_child(n) for n in sorted(names, key=str.casefold)]
            imgs = [c for c in imgs if kind_of(c) == "image"]
            if imgs:
                out += imgs
            else:
                empty.append(f)
        return out, empty

    @staticmethod
    def _pdf_multi():
        try:
            from .pdf_view import PdfViewer
            return bool(getattr(PdfViewer, "SUPPORTS_MULTIPLE", False))
        except Exception:  # noqa: BLE001
            return False

    # ---- actions -----------------------------------------------------------------------
    def _new_window(self):
        MainWindow(self).present()

    def _quit(self):
        """Quit (menu, Ctrl+Q, tray icon): close every window (a document
        with unsaved changes asks first, and Cancel stops the quit), then exit
        even when View would otherwise stay in the background."""
        self._quitting = True
        wins = list(self.get_windows())
        if not wins:
            self._really_quit()
            return
        for w in wins:
            w.close()

    def cancel_quit(self):
        """A window asked about unsaved changes and the user kept it."""
        self._quitting = False

    def _present_any(self, startup_id=""):
        """Bring View to the front: the active (or any) window, else a new one."""
        wins = self.get_windows()
        win = self.get_active_window() or (wins[0] if wins else None) or MainWindow(self)
        if startup_id:
            _set_startup_id(win, startup_id)
        win.present()
        _to_front(win)
        return win

    # ---- resident (background) mode -----------------------------------------------
    def _setup_resident(self):
        """The running View (preview.instance made this process the primary):
        take other launches' files, and decide ourselves when to exit, so that
        View can stay in the background once its last window closes."""
        from . import instance
        server = instance.server
        if server is None or runtime.startup_trace or self.get_application_id() != APP_ID:
            return
        self._server = server
        self.hold()                     # released only by quit(): see _after_window_removed
        self.connect("window-added", self._on_window_added)
        self.connect("window-removed", self._on_window_removed)
        server.on_restart = lambda: GLib.idle_add(self._really_quit)
        server.set_handler(lambda req: GLib.idle_add(self._on_remote, req))
        if not runtime.IS_WINDOWS and hasattr(GLib, "unix_signal_add"):
            # (default SIGTERM would leave llama-server and the socket behind;
            # Windows: the tray window's WM_CLOSE / WM_ENDSESSION, see tray.py)
            try:
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM,
                                     lambda: (self._really_quit(), GLib.SOURCE_REMOVE)[1])
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        try:
            from . import tray
            tray.start(activate=lambda: self._present_any(tray.take_token()),
                       open_files=self._tray_open, new_window=self._new_window,
                       quit=self._quit)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def background_wanted(self) -> bool:
        """Stay running with no window open?  Preferences, or by default only
        where a tray icon lets the user quit."""
        from . import instance, tray
        choice = instance.background_choice()
        return tray.available() if choice is None else choice

    def _on_remote(self, req):
        """Another launch, handed over by preview.instance (main loop)."""
        try:
            args = [a for a in req.args if a != "--new-signature"]
            files = [Gio.File.new_for_commandline_arg_and_cwd(a, req.cwd) if req.cwd
                     else Gio.File.new_for_commandline_arg(a) for a in args]
            if files:
                win = self.route_files(files, None, startup_id=req.token)
            else:
                win = self._present_any(req.token)
            if "--new-signature" in req.args:
                self._new_signature()
            if win is not None and "VIEW_OPEN_T0" in req.env:
                win.add_tick_callback(lambda *_a: (req.report_shown(), GLib.SOURCE_REMOVE)[1])
            else:
                req.report_shown()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            req.report_shown()
        return GLib.SOURCE_REMOVE

    def _tray_open(self):
        open_files_dialog(self._active_main(), lambda files: self.route_files(files))

    def _on_window_added(self, _app, _win):
        if self._warming:
            return
        self._server.idle = False
        if self._background:
            self._background = False
            from . import tray
            tray.hide()

    def _on_window_removed(self, _app, win):
        if self._warming:
            return
        # signal closures keep a closed window (and its decoded documents)
        # alive through a GObject reference cycle: drop the content now.
        # (A tab that moved to another window is no longer in this one.)
        if isinstance(win, MainWindow):
            frees = [h.release for h in win.hosts()]
        else:
            viewer = getattr(win, "viewer", None)
            frees = [viewer.release] if viewer is not None and hasattr(viewer, "release") else []
        if frees:
            def free():
                for fn in frees:
                    try:
                        fn()
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(free)
        GLib.idle_add(self._after_window_removed)

    def _after_window_removed(self):
        if self.get_windows():
            return GLib.SOURCE_REMOVE
        self._server.idle = True
        if self._quitting or not self.background_wanted():
            self._really_quit()
        else:
            self._enter_background()
        return GLib.SOURCE_REMOVE

    def background_changed(self):
        """Preferences switched the background mode (it applies when the last
        window closes; with none open, now)."""
        if self._server is not None and not self.get_windows():
            self._after_window_removed()

    def _enter_background(self):
        """The last window closed and View stays: show the tray icon, give back
        what only open documents needed, then get ready to open the next file
        fast."""
        if self._background:
            return
        self._background = True
        try:
            from . import tray
            tray.show()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        threading.Thread(target=_stop_engines, daemon=True, name="view-release").start()
        GLib.timeout_add(300, self._warm_up)

    def _warm_up(self):
        """Like the screenshot watcher: import the image viewer and set up a
        renderer, CSS and icons with a window that is realized but never
        shown, then hand freed memory back to the system."""
        if self._background:
            t0 = time.monotonic()
            self._warming = True
            try:
                win = MainWindow(self)
                win.ensure_viewer("image")
                win.realize()
                win.destroy()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            finally:
                self._warming = False
            if os.environ.get("VIEW_DEBUG_BACKGROUND"):
                print(f"View: in the background, warmed up in "
                      f"{(time.monotonic() - t0) * 1000:.0f} ms", file=sys.stderr, flush=True)
        GLib.timeout_add(500, runtime.give_back_memory)
        return GLib.SOURCE_REMOVE

    def _really_quit(self):
        self._quitting = True
        if self._server is not None:
            self._server.close()        # the next launch starts a new View
        self.quit()
        return GLib.SOURCE_REMOVE

    def _any_window(self):
        return self.get_active_window() or self._active_main() or MainWindow(self)

    def _about(self):
        icon = APP_ID if Gtk.IconTheme.get_for_display(Gdk.Display.get_default()) \
            .has_icon(APP_ID) else "image-x-generic"
        dlg = Adw.AboutDialog(application_name=APP_NAME, application_icon=icon,
                              developer_name="Zihan", version=runtime.VERSION,
                              website=runtime.HOMEPAGE, issue_url=runtime.ISSUES,
                              comments=_("View PDFs, images, video, text and 3D models, "
                                         "combine images into PDFs and sign documents."),
                              license_type=Gtk.License.CUSTOM,
                              license="PolyForm Noncommercial License 1.0.0\n\n"
                                      "Free for personal, research, educational and other "
                                      "noncommercial use. Commercial use requires a separate "
                                      "license from the author.\n\n"
                                      "https://polyformproject.org/licenses/noncommercial/1.0.0/")
        dlg.add_link(_("All Versions"), runtime.HOMEPAGE + "/releases")
        dlg.add_link(_("Support Development"), runtime.DONATE)
        win = self._any_window()
        dlg.set_debug_info(debug_info(win))
        dlg.set_debug_info_filename("view-debug-info.txt")
        dlg.present(win)
        self._get_updater().check_now(win, toast=dlg)      # result as a toast in About

    def _check_updates(self):
        win = self._any_window()
        win.present()
        self._get_updater().check_now(win)

    def _preferences(self):
        from .preferences import PreferencesDialog
        PreferencesDialog(self).present(self._any_window())

    def _shortcuts(self):
        if not hasattr(Adw, "ShortcutsDialog"):         # libadwaita < 1.8
            return self._shortcuts_fallback()
        dlg = Adw.ShortcutsDialog()
        for title, items in SHORTCUT_SECTIONS:
            sec = Adw.ShortcutsSection(title=_(title))
            for t, a in items:
                sec.add(Adw.ShortcutsItem(title=_(t), accelerator=a))
            dlg.add(sec)
        dlg.present(self._any_window())

    def _shortcuts_fallback(self):
        page = Adw.PreferencesPage()
        for title, items in SHORTCUT_SECTIONS:
            grp = Adw.PreferencesGroup(title=_(title))
            for t, a in items:
                row = Adw.ActionRow(title=_(t))
                row.add_suffix(Gtk.ShortcutLabel(accelerator=a, valign=Gtk.Align.CENTER))
                grp.add(row)
            page.add(grp)
        tb = Adw.ToolbarView(content=page)
        tb.add_top_bar(Adw.HeaderBar())
        Adw.Dialog(title=_("Keyboard Shortcuts"), child=tb, content_width=520,
                   content_height=640).present(self._any_window())

    def _combine_images(self):
        from . import convert
        win = self._any_window()
        fd = Gtk.FileDialog(title=_("Choose Images to Combine into a PDF"))
        f = Gtk.FileFilter(name=_("Images"))
        for e in _suffixes(IMAGE_EXTS):
            f.add_suffix(e)
        fd.set_default_filter(f)

        def done(fd_, res):
            try:
                lst = fd_.open_multiple_finish(res)
            except GLib.Error:
                return
            paths = [lst.get_item(i).get_path() for i in range(lst.get_n_items())]
            paths = sorted((p for p in paths if p), key=lambda p: convert.natural_key(
                os.path.basename(p)))
            if not paths:
                win.show_toast(_("Only local files are supported"))
                return
            convert.show_export_dialog(win, paths)
        fd.open_multiple(win, None, done)

    def _new_signature(self):
        win = self._any_window()
        try:
            from .signature import SignatureCaptureWindow, copy_signature_to_clipboard
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            win.present()
            win.show_toast(_("Signatures are unavailable: {error}").format(error=e))
            return

        def saved(sig):
            win.show_toast(_("Signature saved"), _("Copy"),
                           lambda: copy_signature_to_clipboard(sig))
        win.present()
        SignatureCaptureWindow(win, saved).present()
