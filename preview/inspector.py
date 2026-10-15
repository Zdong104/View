"""Non-modal Inspector window (Get Info) that follows the window's viewer."""
import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

_LINK_RE = re.compile(r"^\[(.+)\]\((https?://.+)\)$")


class Inspector(Adw.Window):
    """`main_window` may be any object with a `viewer` attribute (or pass
    `get_viewer`); show_toast() is used when present."""

    def __init__(self, main_window, get_viewer=None):
        super().__init__(title="Inspector", modal=False, hide_on_close=True,
                         default_width=360, default_height=640)
        if isinstance(main_window, Gtk.Window):
            self.set_transient_for(main_window)
        self.main_window = main_window
        self.set_destroy_with_parent(True)
        self._get_viewer = get_viewer or (lambda: getattr(main_window, "viewer", None))
        self._groups = []
        self._pending = False

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        self.empty = Adw.StatusPage(icon_name="document-properties-symbolic",
                                    title="No Information",
                                    description="Open a document to see its details.")
        self.stack = Gtk.Stack()
        self.stack.add_named(self.page, "info")
        self.stack.add_named(self.empty, "empty")
        tv.set_content(self.stack)
        self.set_content(tv)

        esc = Gtk.ShortcutController()
        esc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                          Gtk.CallbackAction.new(lambda *_: (self.close(), True)[1])))
        self.add_controller(esc)
        self.connect("notify::visible", lambda *_: self.refresh() if self.get_visible() else None)

    # ---- refresh (coalesced) --------------------------------------------
    def refresh(self):
        if self._pending or not self.get_visible():
            return
        self._pending = True
        GLib.idle_add(self._do_refresh)

    def _do_refresh(self):
        self._pending = False
        if not self.get_visible():
            return False
        viewer = self._get_viewer()
        sections = []
        if viewer is not None:
            try:
                sections = list(viewer.get_info() or [])
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
            if not any(t == "File" for t, _r in sections):
                sections = self._file_section(viewer) + sections
        for g in self._groups:
            self.page.remove(g)
        self._groups = []
        if not sections:
            self.stack.set_visible_child_name("empty")
            return False
        for title, rows in sections:
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(title))
            for label, value in rows:
                group.add(self._make_row(label, str(value)))
            self.page.add(group)
            self._groups.append(group)
        self.stack.set_visible_child_name("info")
        return False

    @staticmethod
    def _file_section(viewer):
        from gi.repository import Gio
        from .viewer_base import file_info_section
        gf = None
        getter = getattr(viewer, "get_current_file", None)
        if callable(getter):
            try:
                gf = getter()
            except Exception:  # noqa: BLE001
                gf = None
        if gf is None:
            for attr in ("gfile", "file", "current_file"):
                v = getattr(viewer, attr, None)
                if isinstance(v, Gio.File):
                    gf = v
                    break
        return [file_info_section(gf)] if gf is not None else []

    # ---- rows ---------------------------------------------------------------
    def _make_row(self, label, value):
        m = _LINK_RE.match(value)
        row = Adw.ActionRow(title=GLib.markup_escape_text(label), subtitle_lines=0,
                            title_lines=1)
        if m:
            text, url = m.group(1), m.group(2)
            row.set_subtitle(f'<a href="{GLib.markup_escape_text(url)}">'
                             f'{GLib.markup_escape_text(text)}</a>')
            row.set_activatable(True)
            row.connect("activated", lambda *_: self._open_url(url))
            copy_value = url
        else:
            row.set_subtitle(GLib.markup_escape_text(value))
            copy_value = value
        row.set_subtitle_selectable(True)
        row.add_css_class("property")
        btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Copy", css_classes=["flat", "circular"])
        btn.connect("clicked", lambda *_: self._copy(copy_value))
        row.add_suffix(btn)
        return row

    def _copy(self, text):
        self.get_clipboard().set(text)
        if hasattr(self.main_window, "show_toast"):
            self.main_window.show_toast("Copied")

    def _open_url(self, url):
        try:
            Gtk.UriLauncher.new(url).launch(self, None, None, None)
        except Exception:  # noqa: BLE001
            pass
