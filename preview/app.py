"""Application shell: windows, file routing, drag & drop, menus."""
import os
import sys
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import runtime
from .viewer_base import (IMAGE_EXTS, MEDIA_EXTS, MODEL_EXTS, PDF_EXTS, TABLE_EXTS, TEXT_EXTS,
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


def _recent_files(limit=10):
    """Recently used local files of types we can open: [(uri, Gio.File), ...]."""
    out = []
    try:
        infos = Gtk.RecentManager.get_default().get_items()
    except Exception:  # noqa: BLE001
        return out

    def stamp(i):
        best = 0
        for getter in (i.get_visited, i.get_modified, i.get_added):
            try:
                d = getter()
                best = max(best, d.to_unix() if d is not None else 0)
            except Exception:  # noqa: BLE001
                pass
        return best
    for info in sorted(infos, key=stamp, reverse=True):
        uri = info.get_uri()
        if not uri or not uri.startswith("file://"):
            continue
        f = Gio.File.new_for_uri(uri)
        if kind_of(f, sniff=True) == "unknown" or not f.query_exists(None):
            continue
        out.append((uri, f))
        if len(out) >= limit:
            break
    return out


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
    allf = mk("All Supported Files",
              PDF_EXTS | IMAGE_EXTS | MODEL_EXTS | TABLE_EXTS | MEDIA_EXTS | TEXT_EXTS,
              text_names)
    store.append(allf)
    store.append(mk("PDF Documents", PDF_EXTS))
    store.append(mk("Images", IMAGE_EXTS))
    store.append(mk("Video and Audio", VIDEO_EXTS | AUDIO_EXTS))
    store.append(mk("Text and Source Code", TEXT_EXTS, text_names))
    store.append(mk("3D Models", MODEL_EXTS))
    store.append(mk("Spreadsheets (CSV/TSV)", TABLE_EXTS))
    anyf = Gtk.FileFilter(name="All Files")
    anyf.add_pattern("*")
    store.append(anyf)
    return store, allf


SHORTCUT_SECTIONS = [
    ("General", (("Open…", "<Control>o"), ("New Window", "<Control>n"),
                     ("New from Clipboard", "<Control>v"), ("Show Inspector", "<Control>i"),
                     ("Full Screen", "F11"), ("Close Window", "<Control>w"),
                     ("Quit", "<Control>q"), ("Keyboard Shortcuts", "<Control>question"))),
    ("Video and Audio", (("Play / Pause", "space"), ("Back 5 Seconds", "Left"),
                     ("Forward 5 Seconds", "Right"), ("Volume Up", "Up"),
                     ("Volume Down", "Down"), ("Mute", "m"), ("Full Screen", "f"),
                     ("Save Frame as PNG…", "<Control>s"))),
    ("Text and Source Code", (("Save", "<Control>s"), ("Save As…", "<Control><Shift>s"),
                     ("Find", "<Control>f"), ("Go to Line…", "<Control>g"),
                     ("Next Match", "F3"), ("Previous Match", "<Shift>F3"),
                     ("Undo", "<Control>z"), ("Redo", "<Control><Shift>z"),
                     ("Larger Text", "<Control>plus"), ("Smaller Text", "<Control>minus"),
                     ("Actual Text Size", "<Control>0"))),
    ("Images", (("Zoom In", "plus"), ("Zoom Out", "minus"), ("Fit to Window", "0"),
                     ("Actual Size", "1"), ("Rotate Left", "<Control>l"),
                     ("Rotate Right", "<Control>r"), ("Copy Image", "<Control>c"),
                     ("Save", "<Control>s"), ("Save As…", "<Control><Shift>s"),
                     ("Export As…", "<Control><Shift>e"), ("Toggle Thumbnails", "F9"),
                     ("Select All Thumbnails", "<Control>a"), ("Previous Image", "Left"),
                     ("Next Image", "Right"), ("Remove from List", "Delete"),
                     ("Undo", "<Control>z"), ("Redo", "<Control><Shift>z"),
                     ("Crop to Selection", "<Control>k"), ("Adjust Color", "<Control><Shift>c"),
                     ("Print", "<Control>p"), ("Slideshow", "F5"),
                     ("Markup Toolbar", "<Control><Shift>a"),
                     ("Copy All Recognized Text", "<Control><Shift>t"),
                     ("Cancel Selection / Tool", "Escape"))),
]


def _note_startup(widget, _clock):
    """First frame of the first window: how long startup took (About → Troubleshooting)."""
    if runtime.startup_seconds is None:
        runtime.mark("first frame")
        runtime.startup_seconds = runtime.startup_marks[-1][1]
        if runtime.startup_trace:
            print(f"{APP_NAME} {runtime.VERSION} startup trace "
                  "(seconds since the process started):", flush=True)
            print(runtime.startup_report(), flush=True)
            mods = runtime.loaded_modules()
            if mods:
                ours = [m for m in mods if m.lower().startswith(
                    os.path.dirname(sys.executable).lower())]
                mb = sum(os.path.getsize(m) for m in ours if os.path.exists(m)) / 1e6
                print(f"{len(mods)} DLLs loaded, {len(ours)} of them View's own "
                      f"({mb:.0f} MB)", flush=True)
                if os.environ.get("VIEW_STARTUP_TRACE_DLLS"):
                    for m in sorted(ours, key=lambda m: -os.path.getsize(m)):
                        print(f"  {os.path.getsize(m) / 1e6:6.1f} MB  {m}", flush=True)
            app = widget.get_application()
            GLib.timeout_add(300, lambda: (app.quit(), False)[1])
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


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, default_width=1040, default_height=740,
                         title=APP_NAME)
        self.viewer = None
        self._confirmed_close = False

        self.toast_overlay = Adw.ToastOverlay()
        self.bin = Adw.Bin()
        self.toast_overlay.set_child(self.bin)
        self.highlight = Gtk.Box(css_classes=["drop-highlight"], visible=False,
                                 can_target=False)
        content = Gtk.Overlay(child=self.toast_overlay)
        content.add_overlay(self.highlight)

        self.header = Adw.HeaderBar()
        self.win_title = Adw.WindowTitle(title=APP_NAME, subtitle="")
        self.header.set_title_widget(self.win_title)
        open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Open… (Ctrl+O)",
                              action_name="win.open")
        self.header.pack_start(open_btn)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", primary=True,
                                  tooltip_text="Main Menu", menu_model=app.primary_menu)
        self.header.pack_end(menu_btn)
        self.header.pack_end(Gtk.Button(icon_name="dialog-information-symbolic",
                                        tooltip_text="Inspector (Ctrl+I)",
                                        action_name="win.inspector"))
        self.inspector = None

        self.toolbar = Adw.ToolbarView()
        self.toolbar.add_top_bar(self.header)
        self.toolbar.set_content(content)
        self.set_content(self.toolbar)

        self.bin.set_child(self._build_empty())

        act = Gio.SimpleAction.new("open", None)
        act.connect("activate", lambda *_: self.open_dialog())
        self.add_action(act)
        act = Gio.SimpleAction.new("inspector", None)
        act.connect("activate", lambda *_: self.toggle_inspector())
        self.add_action(act)
        act = Gio.SimpleAction.new("close", None)
        act.connect("activate", lambda *_: self.close())
        self.add_action(act)
        act = Gio.SimpleAction.new("fullscreen", None)
        act.connect("activate", lambda *_: self.unfullscreen() if self.is_fullscreen()
                    else self.fullscreen())
        self.add_action(act)

        # Ctrl+V on an empty or image window: paste the clipboard as a new image.
        # Other viewers keep Ctrl+V (the callback declines so the event propagates).
        paste = Gtk.ShortcutController()
        paste.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>v"),
            Gtk.CallbackAction.new(self._paste_shortcut)))
        self.add_controller(paste)

        # Full screen: hide the header bar, reveal it when the pointer nears the top.
        self._chrome_hidden = False
        self.connect("notify::fullscreened", lambda *_: self._sync_chrome())
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.toolbar.add_controller(motion)

        dt = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        dt.connect("enter", self._on_drag_enter)
        dt.connect("leave", lambda *_: self.highlight.set_visible(False))
        dt.connect("drop", self._on_drop)
        self.toolbar.add_controller(dt)

        self.connect("close-request", self._on_close_request)
        if runtime.startup_seconds is None:
            self.add_tick_callback(_note_startup)

    # ---- empty state -------------------------------------------------------
    def _build_empty(self):
        page = Adw.StatusPage(icon_name="document-open-symbolic",
                              title="Drop a PDF, image, video, text file or spreadsheet here",
                              description="Or open a file to get started.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, halign=Gtk.Align.CENTER)
        box.append(Gtk.Button(label="Open…", halign=Gtk.Align.CENTER,
                              css_classes=["pill", "suggested-action"], action_name="win.open"))
        recents = _recent_files(5)
        if recents:
            box.append(Gtk.Label(label="Recent", css_classes=["heading"], halign=Gtk.Align.START))
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                             width_request=380)
            for uri, f in recents:
                parent = f.get_parent()
                row = Adw.ActionRow(title=GLib.markup_escape_text(f.get_basename() or uri),
                                    subtitle=GLib.markup_escape_text(
                                        (parent.get_path() if parent else "") or ""),
                                    activatable=True, subtitle_lines=1)
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda _r, f=f: self.open_files([f]))
                lb.append(row)
            box.append(lb)
        page.set_child(box)
        return page

    def _paste_shortcut(self, *_):
        if self.viewer is not None and not hasattr(self.viewer, "add_untitled"):
            return False
        self.get_application().new_from_clipboard(self)
        return True

    def set_chrome_visible(self, visible):
        """Hide/show the header bar (slideshow)."""
        self._chrome_hidden = not visible
        self._sync_chrome()

    def _sync_chrome(self):
        self.toolbar.set_reveal_top_bars(not (self._chrome_hidden or self.is_fullscreen()))

    def _on_motion(self, _c, _x, y):
        if self.is_fullscreen() and not self._chrome_hidden:
            if y < 6 and not self.toolbar.get_reveal_top_bars():
                self.toolbar.set_reveal_top_bars(True)
            elif y > 90 and self.toolbar.get_reveal_top_bars():
                self.toolbar.set_reveal_top_bars(False)

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

    def is_empty(self):
        return self.viewer is None

    # ---- toast / title -------------------------------------------------------
    def show_toast(self, message, button_label=None, on_button=None):
        toast = Adw.Toast(title=message, timeout=6 if button_label else 4)
        try:
            toast.set_use_markup(False)
        except AttributeError:
            pass
        if button_label:
            toast.set_button_label(button_label)
            if on_button:
                toast.connect("button-clicked", lambda *_: on_button())
        self.toast_overlay.add_toast(toast)

    def refresh_title(self):
        if self.inspector is not None:
            self.inspector.refresh()
        if self.viewer is None:
            self.win_title.set_title(APP_NAME)
            self.win_title.set_subtitle("")
            self.set_title(APP_NAME)
            return
        title = self.viewer.get_title() or APP_NAME
        self.win_title.set_title(title)
        self.win_title.set_subtitle(self.viewer.get_subtitle() or "")
        self.set_title(f"{title} — {APP_NAME}")

    # ---- loading ---------------------------------------------------------------
    def ensure_viewer(self, kind):
        """Create and install the viewer for `kind` in this (empty) window."""
        if kind == "image":
            from .image_view import ImageViewer as cls
        elif kind == "pdf":
            from .pdf_view import PdfViewer as cls
        elif kind == "model":
            from .stl_view import ModelViewer as cls
        elif kind == "table":
            from .table_view import TableViewer as cls
        elif kind == "video":
            from .video_view import VideoViewer as cls
        elif kind == "text":
            from .text_view import TextViewer as cls
        else:
            raise ValueError(f"Unsupported file type: {kind}")
        runtime.mark(f"{kind} viewer imported")
        viewer = cls(self)
        self.viewer = viewer
        self.bin.set_child(viewer)
        viewer.build_header(self.header)
        return viewer

    def _show_error(self, e):
        self.viewer = None
        page = Adw.StatusPage(icon_name="dialog-error-symbolic",
                              title="Could not open this file",
                              description=f"{type(e).__name__}: {e}")
        btn = Gtk.Button(label="Open Another File…", halign=Gtk.Align.CENTER,
                         css_classes=["pill"], action_name="win.open")
        page.set_child(btn)
        self.bin.set_child(page)

    def load_files(self, kind, files):
        """Create the viewer for `kind` and load files into this (empty) window."""
        try:
            viewer = self.ensure_viewer(kind)
            viewer.load(files)
            runtime.mark(f"{kind} loaded")
            app = self.get_application()
            for f in files[:10]:          # don't flood Recent when opening a whole folder
                app.add_recent(f)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._show_error(e)
        self.refresh_title()

    def show_clipboard_image(self, pil_image):
        """Show a pasted image (as a new untitled image) in this window."""
        try:
            if self.viewer is None:
                self.ensure_viewer("image")
            self.viewer.add_untitled(pil_image)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._show_error(e)
        self.refresh_title()

    def open_files(self, files):
        self.get_application().route_files(list(files), self)

    def open_dialog(self):
        fd = Gtk.FileDialog(title="Open")
        filters, default = _make_filters()
        fd.set_filters(filters)
        fd.set_default_filter(default)

        def done(fd_, res):
            try:
                lst = fd_.open_multiple_finish(res)
            except GLib.Error:
                return
            self.open_files([lst.get_item(i) for i in range(lst.get_n_items())])
        fd.open_multiple(self, None, done)

    # ---- drag & drop --------------------------------------------------------------
    def _on_drag_enter(self, _dt, _x, _y):
        self.highlight.set_visible(True)
        return Gdk.DragAction.COPY

    def _on_drop(self, _dt, value, _x, _y):
        self.highlight.set_visible(False)
        files = value.get_files() if value is not None else []
        if not files:
            return False
        if self.viewer is not None:
            try:
                if self.viewer.accepts_drop(files):
                    self.viewer.add_dropped(files)
                    for f in files:
                        self.get_application().add_recent(f)
                    return True
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self.open_files(files)
        return True

    # ---- closing ---------------------------------------------------------------------
    def _on_close_request(self, *_):
        v = self.viewer
        if self._confirmed_close or v is None:
            return False
        try:
            dirty = v.has_unsaved_changes()
        except Exception:  # noqa: BLE001
            dirty = False
        if not dirty:
            return False
        can_save = hasattr(v, "save_changes")
        dlg = Adw.AlertDialog(heading="Discard Changes?",
                              body="This document has unsaved changes that will be lost "
                                   "if you close it.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("discard", "Discard")
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        if can_save:
            dlg.add_response("save", "Save")
            dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("save")
        else:
            dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def response(_d, rid):
            if rid == "discard":
                self._confirmed_close = True
                self.close()
            elif rid == "save":
                def after(ok):
                    if ok:
                        self._confirmed_close = True
                        self.close()
                v.save_changes(after)
        dlg.connect("response", response)
        dlg.present(self)
        return True


class PreviewApp(Adw.Application):
    def __init__(self):
        GLib.set_application_name(APP_NAME)
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if runtime.IN_SNAP:
            # Owning our ID on the session bus needs a dbus slot, and the
            # Snap Store holds every upload with one for manual review.
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.recent_menu = Gio.Menu()
        self.primary_menu = self._build_menu()
        self._want_signature = False
        self.updater = None
        self.add_main_option("new-signature", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Draw a new signature with the touchpad", None)
        self.connect("handle-local-options", self._on_local_options)

    # ---- command line ------------------------------------------------------------
    def _on_local_options(self, _app, options):
        if options.contains("new-signature"):
            try:
                self.register(None)
            except GLib.Error:
                return -1
            if self.get_is_remote():
                self.activate_action("new-signature", None)
                return 0
            self._want_signature = True
        return -1

    def _build_menu(self):
        m = Gio.Menu()
        s1 = Gio.Menu()
        s1.append("New Window", "app.new-window")
        s1.append("Open…", "win.open")
        s1.append_submenu("Open Recent", self.recent_menu)
        s1.append("New from Clipboard", "app.new-from-clipboard")
        m.append_section(None, s1)
        s4 = Gio.Menu()
        s4.append("Show Inspector", "win.inspector")
        s4.append("Full Screen", "win.fullscreen")
        m.append_section(None, s4)
        s2 = Gio.Menu()
        s2.append("Combine Images into PDF…", "app.combine-images")
        s2.append("New Signature…", "app.new-signature")
        m.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append("Preferences", "app.preferences")
        s3.append("Keyboard Shortcuts", "app.shortcuts")
        s3.append("Check for Updates…", "app.check-updates")
        s3.append("About View", "app.about")
        s3.append("Quit", "app.quit")
        m.append_section(None, s3)
        return m

    def do_startup(self):
        runtime.mark("application registered")
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
        self._recent_mgr.connect("changed", lambda *_: self._rebuild_recent())
        self._rebuild_recent()
        self.set_accels_for_action("win.open", ["<Control>o"])
        self.set_accels_for_action("win.inspector", ["<Control>i"])
        self.set_accels_for_action("app.new-from-clipboard", ["<Control><Shift>n"])
        self.set_accels_for_action("win.close", ["<Control>w"])
        self.set_accels_for_action("win.fullscreen", ["F11"])
        runtime.mark("menus and actions set up")

    # ---- recent files ------------------------------------------------------------
    def add_recent(self, gfile):
        try:
            if gfile is not None and gfile.get_path():
                Gtk.RecentManager.get_default().add_item(gfile.get_uri())
        except Exception:  # noqa: BLE001
            pass

    def _rebuild_recent(self):
        self.recent_menu.remove_all()
        items = _recent_files(10)
        sec = Gio.Menu()
        for uri, f in items:
            item = Gio.MenuItem.new(f.get_basename() or uri, None)
            item.set_action_and_target_value("app.open-recent", GLib.Variant("s", uri))
            sec.append_item(item)
        if not items:
            sec.append("No Recent Files", "app.none")
        self.recent_menu.append_section(None, sec)
        if items:
            clear = Gio.Menu()
            clear.append("Clear Menu", "app.clear-recent")
            self.recent_menu.append_section(None, clear)

    def _clear_recent(self):
        mgr = Gtk.RecentManager.get_default()
        for uri, _f in _recent_files(1000):
            try:
                mgr.remove_item(uri)
            except GLib.Error:
                pass
        self._rebuild_recent()

    def _new_from_clipboard(self):
        self.new_from_clipboard(None)

    def new_from_clipboard(self, win=None):
        """Paste the clipboard image as a new untitled image (into `win` if it
        is empty or an image window, else a new window)."""
        from PIL import Image
        import io
        win = win or self._active_main()
        if win is not None and win.viewer is not None and not hasattr(win.viewer, "add_untitled"):
            win = None
        if win is None:
            cand = next((w for w in self.get_windows()
                         if isinstance(w, MainWindow) and w.is_empty()), None)
            win = cand or MainWindow(self)
        win.present()
        clip = win.get_clipboard()

        def done(c, res):
            try:
                tex = c.read_texture_finish(res)
            except GLib.Error:
                tex = None
            if tex is None:
                win.show_toast("No image in the clipboard")
                return
            data = tex.save_to_png_bytes().get_data()
            im = Image.open(io.BytesIO(data))
            im.load()
            win.show_clipboard_image(im)
        clip.read_texture_async(None, done)

    def do_activate(self):
        wins = self.get_windows()
        if wins:
            (self.get_active_window() or wins[0]).present()
        else:
            win = MainWindow(self)
            runtime.mark("window built")
            win.present()
            runtime.mark("window shown")
        self._start_updater()
        if self._want_signature:
            self._want_signature = False
            GLib.idle_add(lambda: (self._new_signature(), False)[1])

    def do_open(self, files, n_files, hint):
        self.route_files(list(files), None)
        self._start_updater()

    def _start_updater(self):
        """Background update checks, only for the real app (not --selftest
        or --startup-trace)."""
        if self.updater is not None or runtime.startup_trace or \
                self.get_application_id() != APP_ID:
            return
        from .updates import Updater
        self.updater = Updater(self)
        self.updater.start()

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

    def route_files(self, files, target=None):
        """Open files: all images in one window, each PDF/model in its own.
        An empty `target` (or empty active window) is reused for the first group."""
        images, pdfs, models, tables, videos, texts, unknown = [], [], [], [], [], [], []
        for f in files:
            k = kind_of(f, sniff=True)
            {"image": images, "pdf": pdfs, "model": models, "table": tables,
             "video": videos, "text": texts}.get(k, unknown).append(f)
        spare = target if (target is not None and target.is_empty()) else None
        if spare is None and target is None:
            cand = self._active_main()
            if cand is not None and cand.is_empty():
                spare = cand
        first = [None]

        def win():
            nonlocal spare
            if spare is not None:
                w, spare = spare, None
            else:
                w = MainWindow(self)
            if first[0] is None:
                first[0] = w
            return w

        groups = []
        if images:
            groups.append(("image", images))
        if len(pdfs) > 1 and self._pdf_multi():
            groups.append(("pdf", pdfs))
        else:
            groups += [("pdf", [f]) for f in pdfs]
        groups += [("model", [f]) for f in models]
        groups += [("table", [f]) for f in tables]
        groups += [("video", [f]) for f in videos]
        groups += [("text", [f]) for f in texts]
        for kind, fl in groups:
            w = win()
            w.load_files(kind, fl)
            w.present()
        if unknown:
            names = ", ".join(f.get_basename() or "?" for f in unknown[:3])
            msg = (f"Unsupported file: {names}" if len(unknown) == 1
                   else f"{len(unknown)} unsupported files: {names}…")
            anchor = first[0] or target or self._active_main() or MainWindow(self)
            anchor.present()
            anchor.show_toast(msg)

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
        for w in list(self.get_windows()):
            w.close()

    def _any_window(self):
        return self.get_active_window() or self._active_main() or MainWindow(self)

    def _about(self):
        icon = APP_ID if Gtk.IconTheme.get_for_display(Gdk.Display.get_default()) \
            .has_icon(APP_ID) else "image-x-generic"
        dlg = Adw.AboutDialog(application_name=APP_NAME, application_icon=icon,
                              developer_name="Zihan", version=runtime.VERSION,
                              website=runtime.HOMEPAGE, issue_url=runtime.ISSUES,
                              comments="View PDFs, images, video, text and 3D models, "
                                       "combine images into PDFs and sign documents.",
                              license_type=Gtk.License.CUSTOM,
                              license="PolyForm Noncommercial License 1.0.0\n\n"
                                      "Free for personal, research, educational and other "
                                      "noncommercial use. Commercial use requires a separate "
                                      "license from the author.\n\n"
                                      "https://polyformproject.org/licenses/noncommercial/1.0.0/")
        dlg.add_link("All Versions", runtime.HOMEPAGE + "/releases")
        dlg.add_link("Support Development", runtime.DONATE)
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
            sec = Adw.ShortcutsSection(title=title)
            for t, a in items:
                sec.add(Adw.ShortcutsItem(title=t, accelerator=a))
            dlg.add(sec)
        dlg.present(self._any_window())

    def _shortcuts_fallback(self):
        page = Adw.PreferencesPage()
        for title, items in SHORTCUT_SECTIONS:
            grp = Adw.PreferencesGroup(title=title)
            for t, a in items:
                row = Adw.ActionRow(title=t)
                row.add_suffix(Gtk.ShortcutLabel(accelerator=a, valign=Gtk.Align.CENTER))
                grp.add(row)
            page.add(grp)
        tb = Adw.ToolbarView(content=page)
        tb.add_top_bar(Adw.HeaderBar())
        Adw.Dialog(title="Keyboard Shortcuts", child=tb, content_width=520,
                   content_height=640).present(self._any_window())

    def _combine_images(self):
        from . import convert
        win = self._any_window()
        fd = Gtk.FileDialog(title="Choose Images to Combine into a PDF")
        f = Gtk.FileFilter(name="Images")
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
                win.show_toast("Only local files are supported")
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
            win.show_toast(f"Signatures are unavailable: {e}")
            return

        def saved(sig):
            win.show_toast("Signature saved", "Copy",
                           lambda: copy_signature_to_clipboard(sig))
        win.present()
        SignatureCaptureWindow(win, saved).present()
