"""Application shell: windows, file routing, drag & drop, menus."""
import os
import sys
import threading
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
                     (N_("New from Clipboard"), "<Control>v"), (N_("Show Inspector"), "<Control>i"),
                     (N_("Full Screen"), "F11"), (N_("Close Window"), "<Control>w"),
                     (N_("Quit"), "<Control>q"), (N_("Keyboard Shortcuts"), "<Control>question"))),
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
                     (N_("Crop to Selection"), "<Control>k"), (N_("Adjust Color"), "<Control><Shift>c"),
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
        self.card_overlay = content     # floating cards (translate_ui) go here
        content.add_overlay(self.highlight)

        self.header = Adw.HeaderBar()
        self.win_title = Adw.WindowTitle(title=APP_NAME, subtitle="")
        self.header.set_title_widget(self.win_title)
        open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text=_("Open… (Ctrl+O)"),
                              action_name="win.open")
        self.header.pack_start(open_btn)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", primary=True,
                                  tooltip_text=_("Main Menu"), menu_model=app.primary_menu)
        self.header.pack_end(menu_btn)
        self.header.pack_end(Gtk.Button(icon_name="dialog-information-symbolic",
                                        tooltip_text=_("Inspector (Ctrl+I)"),
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
                              title=_("Drop a PDF, image, video, text file or spreadsheet here"),
                              description=_("Or open a file to get started."))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, halign=Gtk.Align.CENTER)
        box.append(Gtk.Button(label=_("Open…"), halign=Gtk.Align.CENTER,
                              css_classes=["pill", "suggested-action"], action_name="win.open"))
        self._recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self._recent_box)
        self.show_recents(self.get_application().recent_items)
        page.set_child(box)
        return page

    def show_recents(self, items):
        """Fill the empty page's Recent list (the app finds the files in the
        background and calls this again when it has them)."""
        box = getattr(self, "_recent_box", None)
        if box is None:
            return
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        recents = items[:5]
        if recents:
            box.append(Gtk.Label(label=_("Recent"), css_classes=["heading"], halign=Gtk.Align.START))
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
        elif kind == "cad":
            from .dxf_view import DxfViewer as cls
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
        # an error that explains itself (e.g. DxfReadError) is shown as is
        text = str(e) if getattr(e, "user_message", False) else f"{type(e).__name__}: {e}"
        page = Adw.StatusPage(icon_name="dialog-error-symbolic",
                              title=_("Could not open this file"),
                              description=GLib.markup_escape_text(text))
        btn = Gtk.Button(label=_("Open Another File…"), halign=Gtk.Align.CENTER,
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
        fd = Gtk.FileDialog(title=_("Open"))
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
    def _on_close_request(self, *_a):
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
        dlg = Adw.AlertDialog(heading=_("Discard Changes?"),
                              body=_("This document has unsaved changes that will be lost "
                                     "if you close it."))
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("discard", _("Discard"))
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        if can_save:
            dlg.add_response("save", _("Save"))
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
        self.recent_items = []          # [(uri, Gio.File)], found by _refresh_recent
        self._recent_gen = 0
        self.primary_menu = self._build_menu()
        self._want_signature = False
        self.updater = None
        self._updater_started = False
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
        s1.append(_("New Window"), "app.new-window")
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
                        if isinstance(w, MainWindow) and w.is_empty():
                            w.show_recents(items)
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
            (self.get_active_window() or wins[0]).present()
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

    def route_files(self, files, target=None):
        """Open files: all images in one window, each PDF/model in its own.
        An empty `target` (or empty active window) is reused for the first group."""
        images, pdfs, models, cads, tables, videos, texts, unknown = [], [], [], [], [], [], [], []
        files, empty = self._expand_folders(files)
        for f in files:
            k = kind_of(f, sniff=True)
            {"image": images, "pdf": pdfs, "model": models, "cad": cads, "table": tables,
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
        groups += [("cad", [f]) for f in cads]
        groups += [("table", [f]) for f in tables]
        groups += [("video", [f]) for f in videos]
        groups += [("text", [f]) for f in texts]
        for kind, fl in groups:
            w = win()
            w.load_files(kind, fl)
            w.present()
        if unknown:
            names = ", ".join(f.get_basename() or "?" for f in unknown[:3])
            msg = (_("Unsupported file: {names}").format(names=names) if len(unknown) == 1
                   else ngettext("{n} unsupported file: {names}…",
                                 "{n} unsupported files: {names}…",
                                 len(unknown)).format(n=len(unknown), names=names))
            anchor = first[0] or target or self._active_main() or MainWindow(self)
            anchor.present()
            anchor.show_toast(msg)
        if empty:
            anchor = first[0] or target or self._active_main() or MainWindow(self)
            anchor.present()
            anchor.show_toast(_("No images in {folder}").format(
                folder=empty[0].get_basename() or _("this folder")))

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
