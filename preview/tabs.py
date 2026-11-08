"""Tabs: each document is a page of the window's Adw.TabView.

A page's child is a TabHost: its own header bar (title, Open, main menu,
Inspector, plus whatever the viewer packs there), toast overlay and the
viewer, the empty (Recent) page or an error page.  The viewer gets the
TabHost as its `window`, so its action groups, key controllers and header
buttons travel with the page when it moves to another window, and two tabs
of the same kind never share an action group.  Everything a viewer asks of
"its window" (fullscreen, the Inspector, a parent for a dialog...) is
looked up on the window the tab is in at that moment: never cached, since
"Move to New Window" and dragging a tab out re-parent it.
"""
import json
import os
import sys
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from . import i18n, runtime  # noqa: E402
from .i18n import _  # noqa: E402

APP_NAME = runtime.APP_NAME


# ---- preference -------------------------------------------------------------

def tabs_enabled() -> bool:
    """Preferences: open files as new tabs of the current window (default)
    rather than one window per file."""
    v = i18n._read().get("tabs")
    return v if isinstance(v, bool) else True


def set_tabs_enabled(on: bool):
    data = i18n._read()
    data["tabs"] = bool(on)
    path = i18n.settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _viewer_class(kind):
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
    return cls


class TabHost(Gtk.Box):
    """One tab: what a viewer knows as its window."""

    is_tab_host = True          # viewer_base.is_shell()

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.app = app
        self.viewer = None
        self.kind = None
        self.confirmed_close = False    # Discard was chosen: close without asking
        self._viewer_groups = set()     # action groups a viewer put on this tab
        self._viewer_chrome = []        # and the buttons it packed into the header
        self._global_ctls = []          # the viewer's GLOBAL shortcut controllers
        self._file_title = ""           # the file's name while an error page shows

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
        self._header_boxes = (open_btn.get_parent(), menu_btn.get_parent())
        self.header.pack_end(Gtk.Button(icon_name="dialog-information-symbolic",
                                        tooltip_text=_("Inspector (Ctrl+I)"),
                                        action_name="win.inspector"))

        self.toolbar = Adw.ToolbarView()
        self.toolbar.add_top_bar(self.header)
        self.toolbar.set_content(content)
        self.append(self.toolbar)

        dt = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        dt.connect("enter", self._on_drag_enter)
        dt.connect("leave", lambda *_: self.highlight.set_visible(False))
        dt.connect("drop", self._on_drop)
        self.toolbar.add_controller(dt)

        self.bin.set_child(self._build_empty())

    # ---- where this tab is now ---------------------------------------------
    def _win(self):
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    def tab_view(self):
        return self.get_ancestor(Adw.TabView)

    def page(self):
        tv = self.tab_view()
        return tv.get_page(self) if tv is not None else None

    def is_selected(self):
        tv = self.tab_view()
        page = tv.get_selected_page() if tv is not None else None
        return page is not None and page.get_child() is self

    def present(self):
        """Select this tab and bring its window to the front."""
        tv, page = self.tab_view(), self.page()
        if tv is not None and page is not None:
            tv.set_selected_page(page)
        w = self._win()
        if w is not None:
            w.present()

    # ---- what viewers call on their "window" --------------------------------
    def get_application(self):
        return self.app

    @property
    def inspector(self):
        return getattr(self._win(), "inspector", None)

    def fullscreen(self):
        w = self._win()
        if w is not None:
            w.fullscreen()

    def unfullscreen(self):
        w = self._win()
        if w is not None:
            w.unfullscreen()

    def is_fullscreen(self):
        w = self._win()
        return bool(w is not None and w.is_fullscreen())

    def set_chrome_visible(self, visible):
        fn = getattr(self._win(), "set_chrome_visible", None)
        if fn is not None:
            fn(visible)

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

    def titles(self):
        """(title, subtitle) of this tab's document."""
        v = self.viewer
        if v is None:
            return self._file_title or APP_NAME, ""
        try:
            return v.get_title() or APP_NAME, v.get_subtitle() or ""
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return APP_NAME, ""

    def refresh_title(self):
        title, sub = self.titles()
        self.win_title.set_title(title)
        self.win_title.set_subtitle(sub)
        page = self.page()
        if page is not None:
            named = self.viewer is not None or self._file_title
            page.set_title(title if named else _("New Tab"))
            tip = f"{title}\n{sub}" if sub else title
            page.set_tooltip(GLib.markup_escape_text(tip) if named else "")
        win = self._win()
        if win is not None and self.is_selected() and hasattr(win, "sync_title"):
            win.sync_title()

    def open_files(self, files):
        self.app.route_files(list(files), self)

    def open_dialog(self):
        from .app import open_files_dialog
        open_files_dialog(self._win(), self.open_files)

    def _show_error(self, e):
        self.viewer = None
        self._drop_viewer_chrome()
        # an error that explains itself (e.g. DxfReadError) is shown as is
        text = str(e) if getattr(e, "user_message", False) else f"{type(e).__name__}: {e}"
        page = Adw.StatusPage(icon_name="dialog-error-symbolic",
                              title=_("Could not open this file"),
                              description=GLib.markup_escape_text(text))
        btn = Gtk.Button(label=_("Open Another File…"), halign=Gtk.Align.CENTER,
                         css_classes=["pill"], action_name="win.open")
        page.set_child(btn)
        self.bin.set_child(page)
        self.refresh_title()

    # ---- action groups and header buttons -----------------------------------
    def insert_action_group(self, name, group):
        # viewers put their actions here, where the header bar finds them:
        # remember which, so the next viewer in this tab starts clean
        if group is None:
            self._viewer_groups.discard(name)
        else:
            self._viewer_groups.add(name)
        Gtk.Widget.insert_action_group(self, name, group)

    def _header_children(self):
        out = []
        for box in self._header_boxes:
            child = box.get_first_child()
            while child is not None:
                out.append(child)
                child = child.get_next_sibling()
        return out

    def _drop_viewer_chrome(self):
        """Remove the previous (failed) viewer's header buttons and actions."""
        for w in self._viewer_chrome:
            self.header.remove(w)
        self._viewer_chrome = []
        for name in list(self._viewer_groups):
            self.insert_action_group(name, None)
        self._global_ctls = []

    def set_active(self, active):
        """The selected tab of its window, or not.  A viewer's GLOBAL shortcut
        controllers (PDF, 3D, DXF: their keys work wherever the focus is in
        the window) are root-managed, so in a tab in the background they
        would still catch the keys: they act LOCAL there."""
        scope = Gtk.ShortcutScope.GLOBAL if active else Gtk.ShortcutScope.LOCAL
        for ctl in self._global_ctls:
            if ctl.get_scope() != scope:
                ctl.set_scope(scope)

    # ---- empty state ----------------------------------------------------------
    def _build_empty(self):
        page = Adw.StatusPage(icon_name="document-open-symbolic",
                              title=_("Drop a PDF, image, video, text file or spreadsheet here"),
                              description=_("Or open a file to get started."))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, halign=Gtk.Align.CENTER)
        box.append(Gtk.Button(label=_("Open…"), halign=Gtk.Align.CENTER,
                              css_classes=["pill", "suggested-action"], action_name="win.open"))
        self._recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self._recent_box)
        self.show_recents(self.app.recent_items)
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

    def is_empty(self):
        return self.viewer is None

    # ---- loading ----------------------------------------------------------------
    def ensure_viewer(self, kind):
        """Create and install the viewer for `kind` in this (empty) tab."""
        cls = _viewer_class(kind)
        runtime.mark(f"{kind} viewer imported")
        self._drop_viewer_chrome()
        viewer = cls(self)
        self.viewer = viewer
        self.kind = kind
        self._recent_box = None
        self.bin.set_child(viewer)
        before = set(self._header_children())
        viewer.build_header(self.header)
        self._viewer_chrome = [w for w in self._header_children() if w not in before]
        ctls = viewer.observe_controllers()
        self._global_ctls = [c for c in (ctls.get_item(i) for i in range(ctls.get_n_items()))
                             if isinstance(c, Gtk.ShortcutController)
                             and c.get_scope() == Gtk.ShortcutScope.GLOBAL]
        if self.tab_view() is not None:
            self.set_active(self.is_selected())
        return viewer

    def load_files(self, kind, files):
        """Create the viewer for `kind` and load files into this (empty) tab."""
        self._file_title = (files[0].get_basename() or "") if files else ""
        try:
            viewer = self.ensure_viewer(kind)
            viewer.load(files)
            runtime.mark(f"{kind} loaded")
            for f in files[:10]:          # don't flood Recent when opening a whole folder
                self.app.add_recent(f)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._show_error(e)
        self.refresh_title()

    def show_clipboard_image(self, pil_image):
        """Show a pasted image (as a new untitled image) in this tab."""
        try:
            if self.viewer is None:
                self.ensure_viewer("image")
            self.viewer.add_untitled(pil_image)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._show_error(e)
        self.refresh_title()

    # ---- drag & drop (files) --------------------------------------------------------
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
                        self.app.add_recent(f)
                    return True
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self.open_files(files)
        return True

    # ---- closing ------------------------------------------------------------------------
    def is_dirty(self):
        v = self.viewer
        try:
            return bool(v is not None and v.has_unsaved_changes())
        except Exception:  # noqa: BLE001
            return False

    def ask_discard(self, done):
        """Ask about this tab's unsaved changes: done(True) once it may close
        (Discard, or Save worked), done(False) to keep it."""
        v = self.viewer
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

        answered = []

        def response(_d, rid):
            if answered:                    # (only the first answer counts)
                return
            answered.append(rid)
            if rid == "discard":
                done(True)
            elif rid == "save":
                v.save_changes(done)
            else:
                done(False)
        dlg.connect("response", response)
        dlg.present(self)

    def release(self):
        """The tab closed (or its window did): drop the document now (signal
        closures keep widgets alive through reference cycles), stop a
        translation of it.  Not when the tab only moves to another window."""
        v = self.viewer
        if v is None:
            return
        tv = sys.modules.get("preview.translate_view")
        if tv is not None:
            try:
                tv.shutdown_for(v)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        if hasattr(v, "release"):
            try:
                v.release()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
