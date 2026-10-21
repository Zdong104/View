"""CSV / TSV viewer and editor (see table_data.py for the document model).

A Gtk.ColumnView on a *virtual* Gio.ListModel (rows are created on demand, so a
million rows cost nothing until they scroll into view).  Wide tables are
virtualised horizontally as well: only a window of ~36 real ColumnViewColumns
exists, framed by two blank "padding" columns that stand in for the rest, and
the window follows the horizontal scrollbar.
"""
from __future__ import annotations

import bisect
import csv
import io
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

import numpy as np  # noqa: E402

from .table_data import Command, TableData, TableError, col_letter, to_num  # noqa: E402
from .i18n import _, N_, ngettext
from .viewer_base import Viewer, file_info_section  # noqa: E402

CSS = """
columnview.tv > listview > row > cell { padding: 0; }
columnview.tv > listview > row { min-height: 0; }
.tv-cell { padding: 4px 9px; font-feature-settings: "tnum"; }
.tv-rownum { padding: 4px 7px; color: alpha(currentColor, 0.5); font-feature-settings: "tnum"; }
.tv-sel { background: alpha(@accent_bg_color, 0.22); }
.tv-cur { box-shadow: inset 0 0 0 2px @accent_bg_color; background: alpha(@accent_bg_color, 0.08); }
.tv-match { background: alpha(@warning_bg_color, 0.55); }
.tv-newrow { font-style: italic; }
.tv-status { padding: 3px 12px; font-size: 0.9em; color: alpha(currentColor, 0.7); }
progressbar.tv-progress { min-height: 3px; }
progressbar.tv-progress trough, progressbar.tv-progress progress { min-height: 3px; }
entry.tv-edit { border-radius: 0; box-shadow: inset 0 0 0 2px @accent_bg_color; background: @view_bg_color;
                padding: 0 8px; min-height: 0; }
"""
_css_done = False

ROW_NL = "↵"
MAX_W, MIN_W = 360, 56
SLOTS = 36
MAX_RENDER_CHARS = 300


def _install_css():
    global _css_done
    if _css_done:
        return
    prov = Gtk.CssProvider()
    prov.load_from_string(CSS)
    disp = Gdk.Display.get_default()
    if disp is not None:
        Gtk.StyleContext.add_provider_for_display(disp, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _css_done = True


# ---------------------------------------------------------------------------
class RowObj(GObject.Object):
    __gtype_name__ = "PreviewTableRowObj"


class RowModel(GObject.Object, Gio.ListModel):
    """Virtual list model: n rows, objects created on demand."""
    __gtype_name__ = "PreviewTableRowModel"

    def __init__(self):
        super().__init__()
        self._n = 0

    def do_get_item_type(self):
        return RowObj.__gtype__

    def do_get_n_items(self):
        return self._n

    def do_get_item(self, position):
        return RowObj() if 0 <= position < self._n else None

    def set_n(self, n):
        old, self._n = self._n, n
        if old or n:
            self.items_changed(0, old, n)

    def splice(self, pos, removed, added):
        self._n += added - removed
        self.items_changed(pos, removed, added)


class Cell(Gtk.Inscription):
    """Light-weight text cell (much cheaper than Gtk.Label for 100s of cells per frame)."""
    __gtype_name__ = "PreviewTableCell"

    def __init__(self, kind="data", slot=-1):
        super().__init__(xalign=0.0, text_overflow=Gtk.InscriptionOverflow.ELLIPSIZE_END,
                         min_chars=1, nat_chars=1, hexpand=True, halign=Gtk.Align.FILL,
                         valign=Gtk.Align.FILL, vexpand=True)
        self.kind = kind
        self.slot = slot
        self.pos = -1
        self._txt = None
        self._flags = 0
        self._num = False
        self.add_css_class("tv-rownum" if kind == "rownum" else "tv-cell")


def _weight(s: str) -> int:
    n = len(s)
    if s.isascii():
        return n
    return sum(2 if ord(c) >= 0x2E80 else 1 for c in s)


def _fmt_num(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    return f"{v:,.6g}"


# ---------------------------------------------------------------------------
class TableViewer(Viewer):
    def __init__(self, window):
        super().__init__(window)
        _install_css()
        self.compact = False
        self.gfile: Gio.File | None = None
        self.data = TableData()
        self.data.listeners.append(self._on_data_event)
        self._gen = 0
        self._loading = False
        self._busy = 0
        self._view_gen = 0
        self._stats = None
        self._stats_key = None

        # cursor / selection / editing
        self.cur = (0, 0)
        self.anchor = (0, 0)
        self._editing = None
        self._drag = False
        self._cells = set()
        self.win_start = 0
        self.slots: list[Gtk.ColumnViewColumn] = []
        self.pad_l = self.pad_r = self.num_col = None
        self.widths: list[int] = []
        self.prefix: list[int] = [0]
        self._guard = False
        self._search_lc = ""
        self._out_delim = ","
        self._status_pending = False
        self._numcache: dict[int, bool] = {}
        self._rownum_w = 56
        self._shown_view = None

        self.model = RowModel()
        self._build_ui()
        self._build_actions()
        self._update_sensitivity()

    # ======================================================================
    # UI construction
    # ======================================================================
    def _build_ui(self):
        # search bar
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Find in table"), hexpand=True)
        self.match_label = Gtk.Label(label="", css_classes=["dim-label"], width_chars=10, xalign=1.0)
        self.filter_btn = Gtk.ToggleButton(label=_("Filter rows"), tooltip_text=_("Show only rows that match"))
        prev = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Previous match (Shift+F3)"))
        nxt = Gtk.Button(icon_name="go-down-symbolic", tooltip_text=_("Next match (F3)"))
        box = Gtk.Box(spacing=6)
        for w in (self.search_entry, self.match_label, prev, nxt, self.filter_btn):
            box.append(w)
        box.set_size_request(420, -1)
        clamp = Adw.Clamp(maximum_size=700, child=box)
        self.search_bar = Gtk.SearchBar(child=clamp)
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.set_show_close_button(False)
        self._search_timer = 0
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_: self._goto_match(True))
        self.search_entry.connect("next-match", lambda *_: self._goto_match(True))
        self.search_entry.connect("previous-match", lambda *_: self._goto_match(False))
        self.search_entry.connect("stop-search", lambda *_: self._close_search())
        prev.connect("clicked", lambda *_: self._goto_match(False))
        nxt.connect("clicked", lambda *_: self._goto_match(True))
        self.filter_btn.connect("toggled", self._on_filter_toggled)
        self.append(self.search_bar)

        self.progress = Gtk.ProgressBar(css_classes=["tv-progress", "osd"], visible=False)
        self.append(self.progress)

        # column view
        self.cv = Gtk.ColumnView(model=Gtk.NoSelection.new(self.model), show_row_separators=True,
                                 show_column_separators=True, reorderable=False,
                                 enable_rubberband=False, hexpand=True, vexpand=True)
        self.cv.add_css_class("tv")
        self.cv.add_css_class("data-table")
        self.scroller = Gtk.ScrolledWindow(child=self.cv, hexpand=True, vexpand=True)
        self.vadj = self.scroller.get_vadjustment()
        self.hadj = self.scroller.get_hadjustment()
        self.hadj.connect("value-changed", self._on_hscroll)
        self.hadj.connect("notify::page-size", lambda *_: self._on_hscroll())
        self.vadj.connect("value-changed", lambda *_: self._on_vscroll())
        self.cv.get_sorter().connect("changed", self._on_sorter_changed)

        self.edit_entry = Gtk.Entry(visible=False, halign=Gtk.Align.START, valign=Gtk.Align.START,
                                    css_classes=["tv-edit"])
        self.edit_entry.set_has_frame(False)
        ek = Gtk.EventControllerKey()
        ek.connect("key-pressed", self._on_edit_key)
        self.edit_entry.add_controller(ek)
        fc = Gtk.EventControllerFocus()
        self._edit_fc = fc
        fc.connect("leave", self._on_edit_focus_out)
        self.edit_entry.add_controller(fc)

        self.overlay = Gtk.Overlay(child=self.scroller, hexpand=True, vexpand=True)
        self.overlay.add_overlay(self.edit_entry)
        self.append(self.overlay)

        self.status = Gtk.Label(label="", xalign=0.0, css_classes=["tv-status"], ellipsize=Pango.EllipsizeMode.END)
        self.status_bar = Gtk.Box()
        self.status_bar.append(self.status)
        self.append(Gtk.Separator())
        self.append(self.status_bar)

        # input controllers
        click = Gtk.GestureClick(button=0)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.cv.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        motion.connect("motion", self._on_motion)
        self.cv.add_controller(motion)
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self._key_ctrl = key
        self._key_root = None
        self._autoscroll = 0
        self._ptr = (0.0, 0.0)

        self._build_menus()
        self.cv.set_can_focus(False)
        self.scroller.set_focusable(False)
        self.connect('map', self._on_map)
        self.connect('unmap', self._on_unmap)

        # empty column set until data arrives
        self._rebuild_columns()

    def _on_map(self, *_):
        root = self.get_root()
        if root is not None and self._key_root is not root:
            self._on_unmap()
            root.add_controller(self._key_ctrl)
            self._key_root = root

    def _on_unmap(self, *_):
        if self._key_root is not None:
            try:
                self._key_root.remove_controller(self._key_ctrl)
            except Exception:  # noqa: BLE001
                pass
            self._key_root = None

    def _focus_table(self):
        root = self.get_root()
        if root is not None:
            root.set_focus(None)

    # ---- menus & actions ---------------------------------------------------------------
    def _build_menus(self):
        def sec(items):
            m = Gio.Menu()
            for label, act in items:
                m.append(label, act)
            return m
        self.menu_rows_cols = Gio.Menu()
        self.menu_rows_cols.append_section(None, sec([
            (_("Insert Row Above"), "table.insert-row-above"), (_("Insert Row Below"), "table.insert-row-below"),
            (_("Delete Row(s)"), "table.delete-rows")]))
        self.menu_rows_cols.append_section(None, sec([
            (_("Insert Column Left"), "table.insert-col-left"), (_("Insert Column Right"), "table.insert-col-right"),
            (_("Delete Column(s)"), "table.delete-cols")]))
        self.menu_cell = Gio.Menu()
        self.menu_cell.append_section(None, sec([
            (_("Cut"), "table.cut"), (_("Copy"), "table.copy"), (_("Paste"), "table.paste"),
            (_("Clear Contents"), "table.clear")]))
        self.menu_cell.append_section(None, self.menu_rows_cols)
        self.menu_cell.append_section(None, sec([(_("Select All"), "table.select-all")]))
        self.cell_popover = Gtk.PopoverMenu.new_from_model(self.menu_cell)
        self.cell_popover.set_has_arrow(False)
        self.cell_popover.set_halign(Gtk.Align.START)
        self.cell_popover.set_parent(self.cv)

        self.menu_format = Gio.Menu()
        s = Gio.Menu()
        s.append(_("First Row Is Header"), "table.header")
        s.append(_("Quote All Fields When Saving"), "table.quote-all")
        self.menu_format.append_section(None, s)
        d = Gio.Menu()
        for lab, v in ((_("Comma"), ","), (_("Tab"), "\t"), (_("Semicolon"), ";"), (_("Pipe"), "|")):
            d.append_item(self._radio_item(lab, "table.delimiter", v))
        self.menu_format.append_submenu(_("Delimiter"), d)
        e = Gio.Menu()
        for lab, v in ((_("Unicode (UTF-8)"), "utf-8"), (_("Unicode (UTF-16)"), "utf-16"),
                       (_("Western (Windows-1252)"), "cp1252"), (_("Western (Latin-1)"), "latin-1"),
                       (_("Chinese (GB18030)"), "gb18030"), (_("Chinese (Big5)"), "big5"),
                       (_("Japanese (Shift-JIS)"), "shift_jis"), (_("Korean (EUC-KR)"), "euc-kr")):
            e.append_item(self._radio_item(lab, "table.encoding", v))
        self.menu_format.append_submenu(_("Reopen With Encoding"), e)
        le = Gio.Menu()
        le.append_item(self._radio_item(_("Unix (LF)"), "table.eol", "lf"))
        le.append_item(self._radio_item(_("Windows (CRLF)"), "table.eol", "crlf"))
        self.menu_format.append_submenu(_("Line Endings When Saving"), le)
        s2 = Gio.Menu()
        s2.append(_("Clear Sorting"), "table.clear-sort")
        self.menu_format.append_section(None, s2)

    @staticmethod
    def _radio_item(label, action, value):
        it = Gio.MenuItem.new(label, None)
        it.set_action_and_target_value(action, GLib.Variant("s", value))
        return it

    def _build_actions(self):
        g = Gio.SimpleActionGroup()
        self._actions = g

        def add(name, cb, param=None, state=None):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None) if state is None \
                else Gio.SimpleAction.new_stateful(name, GLib.VariantType.new(param) if param else None, state)
            a.connect("activate", lambda _a, p: cb(p))
            g.add_action(a)
            return a
        add("undo", lambda p: self.do_undo())
        add("redo", lambda p: self.do_redo())
        add("save", lambda p: self.save_changes(None))
        add("save-as", lambda p: self.save_as())
        add("copy", lambda p: self.copy_selection())
        add("cut", lambda p: self.copy_selection(cut=True))
        add("paste", lambda p: self.paste())
        add("clear", lambda p: self.clear_selection())
        add("select-all", lambda p: self.select_all())
        add("find", lambda p: self.show_search())
        add("insert-row-above", lambda p: self.insert_rows(False))
        add("insert-row-below", lambda p: self.insert_rows(True))
        add("delete-rows", lambda p: self.delete_rows())
        add("insert-col-left", lambda p: self.insert_cols(False))
        add("insert-col-right", lambda p: self.insert_cols(True))
        add("delete-cols", lambda p: self.delete_cols())
        add("clear-sort", lambda p: self._set_sort(None, True))
        self.act_header = add("header", lambda p: self._toggle_header(), state=GLib.Variant("b", False))
        self.act_quote = add("quote-all", lambda p: self._toggle_quote(), state=GLib.Variant("b", False))
        self.act_delim = add("delimiter", lambda p: self._choose_delimiter(p.get_string()), "s",
                             GLib.Variant("s", ","))
        self.act_enc = add("encoding", lambda p: self._choose_encoding(p.get_string()), "s",
                           GLib.Variant("s", "utf-8"))
        self.act_eol = add("eol", lambda p: self._choose_eol(p.get_string()), "s", GLib.Variant("s", "lf"))
        # per-column header-menu actions: parameter = slot index
        add("col-sort-asc", lambda p: self._col_sort(p.get_int32(), True), "i")
        add("col-sort-desc", lambda p: self._col_sort(p.get_int32(), False), "i")
        add("col-sort-perm-asc", lambda p: self._col_sort_permanent(p.get_int32(), True), "i")
        add("col-sort-perm-desc", lambda p: self._col_sort_permanent(p.get_int32(), False), "i")
        add("col-rename", lambda p: self._col_rename(p.get_int32()), "i")
        add("col-fit", lambda p: self._col_fit(p.get_int32()), "i")
        add("col-select", lambda p: self._col_select(p.get_int32()), "i")
        add("col-insert-left", lambda p: self._col_insert(p.get_int32(), False), "i")
        add("col-insert-right", lambda p: self._col_insert(p.get_int32(), True), "i")
        add("col-delete", lambda p: self._col_delete(p.get_int32()), "i")
        self.insert_action_group("table", g)
        try:
            self.window.insert_action_group("table", g)
        except Exception:  # noqa: BLE001
            pass

    def _set_action_enabled(self, name, on):
        a = self._actions.lookup_action(name)
        if a is not None:
            a.set_enabled(bool(on))

    def _update_sensitivity(self):
        d = self.data
        editable = not self.compact and d.loaded and not self._busy
        for n in ("cut", "paste", "clear", "insert-row-above", "insert-row-below", "delete-rows",
                  "insert-col-left", "insert-col-right", "delete-cols", "header", "quote-all",
                  "col-rename", "col-sort-perm-asc", "col-sort-perm-desc", "col-insert-left",
                  "col-insert-right", "col-delete"):
            self._set_action_enabled(n, editable)
        self._set_action_enabled("undo", editable and d.can_undo())
        self._set_action_enabled("redo", editable and d.can_redo())
        self._set_action_enabled("save", editable)
        self._set_action_enabled("save-as", d.loaded and not self._busy)
        self._set_action_enabled("delimiter", d.loaded and not self._busy and not self.compact)
        self._set_action_enabled("encoding", d.loaded and not self._busy and not self.compact)
        self._set_action_enabled("eol", editable)
        self._set_action_enabled("clear-sort", d.sort_col is not None)
        if hasattr(self, "btn_undo"):
            self.btn_undo.set_sensitive(editable and d.can_undo())
            self.btn_redo.set_sensitive(editable and d.can_redo())
            self.btn_save.set_sensitive(editable and d.has_unsaved_changes())

    # ---- header bar ---------------------------------------------------------------------
    def build_header(self, header: Adw.HeaderBar) -> None:
        if self.compact:
            return
        self.btn_undo = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text=_("Undo (Ctrl+Z)"),
                                   action_name="table.undo")
        self.btn_redo = Gtk.Button(icon_name="edit-redo-symbolic", tooltip_text=_("Redo (Ctrl+Shift+Z)"),
                                   action_name="table.redo")
        self.btn_save = Gtk.Button(icon_name="document-save-symbolic", tooltip_text=_("Save (Ctrl+S)"),
                                   action_name="table.save")
        box = Gtk.Box(css_classes=["linked"])
        box.append(self.btn_undo)
        box.append(self.btn_redo)
        header.pack_start(box)
        header.pack_start(self.btn_save)
        search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text=_("Find (Ctrl+F)"))
        search_btn.bind_property("active", self.search_bar, "search-mode-enabled",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        header.pack_end(search_btn)
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text=_("Table Format"),
                                       menu_model=self.menu_format))
        header.pack_end(Gtk.MenuButton(icon_name="list-add-symbolic", tooltip_text=_("Rows and Columns"),
                                       menu_model=self.menu_rows_cols))
        self._update_sensitivity()

    def set_compact(self, compact: bool = True):
        """Read-only mode without editing chrome (Quick Look)."""
        self.compact = bool(compact)
        self.status_bar.set_visible(not self.compact)
        self._update_sensitivity()

    # ======================================================================
    # Viewer API
    # ======================================================================
    def get_current_file(self):
        return self.gfile

    def get_title(self) -> str:
        name = (self.gfile.get_basename() if self.gfile is not None else None) or _("Table")
        if self.data.has_unsaved_changes():
            name += _(" — Edited")
        return name

    def get_subtitle(self) -> str:
        d = self.data
        if d.nbase == 0 and self._loading:
            return _("Loading…")
        n = d.n_rows
        s = _("{rows} × {columns}").format(
            rows=ngettext("{n:,} row", "{n:,} rows", n).format(n=n),
            columns=ngettext("{n:,} column", "{n:,} columns", d.ncols).format(n=d.ncols))
        if self._loading:
            s = _("Loading… {n:,} rows").format(n=n)
        elif d.filter_on and d.search_text:
            s += _(" · {n:,} shown").format(n=d.n_view)
        if d.sort_col is not None and not self._loading:
            s += _(" · sorted by {column}").format(column=d.title(d.sort_col))
        return s

    def has_unsaved_changes(self) -> bool:
        return self.data.has_unsaved_changes()

    def load(self, files):
        gf = files[0]
        self.gfile = gf
        path = gf.get_path()
        if path is None:
            import tempfile
            ok, contents, _ = gf.load_contents(None)
            fd, path = tempfile.mkstemp(suffix=os.path.splitext(gf.get_basename() or "")[1])
            with os.fdopen(fd, "wb") as f:
                f.write(contents)
        self._start_load(path)

    def get_info(self):
        sections = [file_info_section(self.gfile)] if self.gfile is not None else []
        d = self.data
        if not d.nbase and not d.loaded:
            return sections
        eol = {b"\n": _("LF (Unix)"), b"\r\n": _("CRLF (Windows)"),
               b"\r": _("CR (classic Mac)")}.get(d.eol, "LF")
        rows = [(_("Rows"), f"{d.n_rows:,}" + ("…" if self._loading else "")),
                (_("Columns"), f"{d.ncols:,}"),
                (_("Delimiter"), {",": _("Comma"), "\t": _("Tab"), ";": _("Semicolon"),
                                  "|": _("Pipe")}.get(d.delim, repr(d.delim))),
                (_("Encoding"), d.enc_label),
                (_("First row is header"), _("Yes") if d.has_header else _("No")),
                (_("Line endings"), eol),
                (_("Quoting"), _("All fields") if d.quoting == "all" else _("When needed"))]
        if d.has_unsaved_changes():
            rows.append((_("Status"), _("Edited (unsaved)")))
        sections.append((_("Table"), rows))
        if d.loaded:
            stats = self._get_stats()
            crow = []
            for c in range(min(d.ncols, 60)):
                t = d.col_type(c)
                e = stats[c] if stats is not None and c < len(stats) else None
                est = "…" if e is None else _("{n:,} empty").format(n=e)
                crow.append((d.title(c), f"{t} · {est}"))
            if d.ncols > 60:
                crow.append(("…", _("{n:,} more columns").format(n=d.ncols - 60)))
            sections.append((_("Columns"), crow))
        return sections

    def _get_stats(self):
        d = self.data
        key = (d.mut, id(d.order), d.ncols)
        if self._stats_key == key and self._stats is not None:
            return self._stats
        if getattr(self, "_stats_running", None) == key:
            return None
        self._stats_running = key

        def work():
            try:
                res = d.empty_counts()
            except Exception:  # noqa: BLE001
                res = None
            GLib.idle_add(done, res)

        def done(res):
            self._stats_running = None
            if res is not None and (d.mut, id(d.order), d.ncols) == key:
                self._stats, self._stats_key = res, key
                insp = getattr(self.window, "inspector", None)
                if insp is not None:
                    insp.refresh()
            return False
        threading.Thread(target=work, daemon=True).start()
        return None

    # ======================================================================
    # Loading
    # ======================================================================
    def _start_load(self, path, encoding=None, delimiter=None, header=None):
        self._gen += 1
        gen = self._gen
        self.data.cancelled = True
        old = self.data
        try:
            old.listeners.remove(self._on_data_event)
        except ValueError:
            pass
        d = self.data = TableData()
        d.listeners.append(self._on_data_event)
        self._loading = True
        self._stats = None
        self.cur = self.anchor = (0, 0)
        self._numcache.clear()
        self._search_lc = ""
        self._shown_view = None
        self.model.set_n(0)
        self._rebuild_columns()
        self.progress.set_fraction(0.0)
        self.progress.set_visible(True)
        self._update_sensitivity()
        self.refresh_title()

        def post(fn, *a):
            GLib.idle_add(lambda: (fn(*a), False)[1] if gen == self._gen else False)

        def work():
            try:
                d.open_path(path, encoding=encoding, delimiter=delimiter, header=header)
                it = d.scan()
                first = next(it, None)
                post(self._load_ready, gen, d, first)
                total = max(1, len(d.data))
                for ends in it:
                    if d.cancelled:
                        return
                    post(self._load_chunk, gen, d, ends, float(ends[-1]) / total)
                post(self._load_done, gen, d)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                post(self._load_failed, gen, e)
        threading.Thread(target=work, daemon=True, name="table-load").start()

    def _load_ready(self, gen, d, first):
        if gen != self._gen:
            return
        if first is not None:
            d.append_rows(first)
        d.init_columns()
        self._out_delim = d.delim
        self._sync_format_actions()
        self.model.set_n(d.n_view)
        self._rebuild_columns()
        self.refresh_title()
        GLib.idle_add(self._focus_table)

    def _load_chunk(self, gen, d, ends, frac):
        if gen != self._gen:
            return
        d.append_rows(ends)
        self.model.splice(self.model._n, 0, d.n_view - self.model._n)
        self.progress.set_fraction(min(0.99, frac))
        self.refresh_title()

    def _load_done(self, gen, d):
        if gen != self._gen:
            return
        d.finish_load()
        self._shown_view = d.view
        self._loading = False
        self.progress.set_visible(False)
        self._update_sensitivity()
        self.refresh_title()
        self._schedule_status()

    def _load_failed(self, gen, e):
        if gen != self._gen:
            return
        self._loading = False
        self.progress.set_visible(False)
        self.toast(_("Could not read this file: {e}").format(e=e))

    def _sync_format_actions(self):
        d = self.data
        self.act_header.set_state(GLib.Variant("b", d.has_header))
        self.act_quote.set_state(GLib.Variant("b", d.quoting == "all"))
        self.act_delim.set_state(GLib.Variant("s", d.delim))
        enc = d.out_enc
        self.act_enc.set_state(GLib.Variant("s", enc if enc != "utf-16-le" and enc != "utf-16-be" else "utf-16"))
        self.act_eol.set_state(GLib.Variant("s", "crlf" if d.out_eol == b"\r\n" else "lf"))

    # ======================================================================
    # Data events
    # ======================================================================
    def _on_data_event(self, kind, *args):
        d = self.data
        if kind == "loaded":
            self._rebuild_if_cols_changed(args[0] if args else False)
            self._sync_format_actions()
            self._numcache.clear()
            self._repaint_all()
        elif kind == "cells":
            self._repaint_all()
            self._after_edit()
        elif kind == "header":
            self._retitle_slots()
            self._after_edit()
        elif kind in ("struct", "view"):
            self._sync_model_full()
            self._clamp_cursor()
            self._refresh_columns()
            self._repaint_all()
            self._after_edit()
            if kind == "view":
                self._sync_sort_indicator()
        elif kind == "history":
            self._update_sensitivity()
            self.refresh_title()

    def _after_edit(self):
        self._numcache.clear()
        self._update_sensitivity()
        self.refresh_title()
        self._schedule_status()
        if self.data.search_text:
            self._queue_search(150)

    def _sync_model_full(self):
        """Cells always read live data, so the list model only has to carry the row
        *count*: grow / shrink at the tail and repaint (cheap even for 1M rows)."""
        n, old = self.data.n_view, self.model._n
        if n > old:
            self.model.splice(old, 0, n - old)
        elif n < old:
            self.model.splice(n, old - n, 0)
        self._repaint_all()

    def _refresh_columns(self):
        d = self.data
        if len(self.widths) != d.ncols:
            self._rebuild_columns()
        else:
            self.widths = [w if w is not None else self._auto_width(c) for c, w in enumerate(d.col_w)]
            for c, w in enumerate(self.widths):
                d.col_w[c] = w
            self._recompute_prefix()
            self._apply_window(self.win_start)

    def _restore_scroll(self, val):
        if val <= 0:
            return
        cnt = [0]

        def tick(*_):
            cnt[0] += 1
            if cnt[0] >= 3:
                self.vadj.set_value(min(val, max(0.0, self.vadj.get_upper() - self.vadj.get_page_size())))
                return False
            return True
        self.cv.add_tick_callback(tick)

    def _clamp_cursor(self):
        n, nc = self.data.n_view, self.data.ncols
        r = max(0, min(self.cur[0], max(0, n - 1)))
        c = max(0, min(self.cur[1], max(0, nc - 1)))
        self.cur = (r, c)
        ar = max(0, min(self.anchor[0], max(0, n - 1)))
        ac = max(0, min(self.anchor[1], max(0, nc - 1)))
        self.anchor = (ar, ac)

    # ======================================================================
    # Columns (with horizontal virtualisation)
    # ======================================================================
    def _char_px(self):
        if not hasattr(self, "_cpx"):
            lay = self.create_pango_layout("0123456789")
            self._cpx = max(6.0, lay.get_pixel_size()[0] / 10.0)
        return self._cpx

    def _auto_width(self, c):
        d = self.data
        cpx = self._char_px()
        m = _weight(d.title(c)) + (5 if d.has_header else 0)
        n = min(d.n_view, 80)
        for i in range(n):
            t = d.cell(d.key_at(i), c)
            if t:
                w = _weight(t[:60])
                if w > m:
                    m = w
        return int(max(MIN_W, min(MAX_W, m * cpx * 1.02 + 26)))

    def _compute_widths(self):
        d = self.data
        self.widths = []
        for c in range(d.ncols):
            w = d.col_w[c]
            if w is None:
                w = self._auto_width(c)
                d.col_w[c] = w
            self.widths.append(w)
        self._recompute_prefix()

    def _recompute_prefix(self):
        p = [0]
        for w in self.widths:
            p.append(p[-1] + w + 1)
        self.prefix = p

    def _make_factory(self, kind, slot):
        f = Gtk.SignalListItemFactory()

        def setup(_f, li):
            cell = Cell(kind, slot)
            li.set_child(cell)
            self._cells.add(cell)

        def bind(_f, li):
            cell = li.get_child()
            cell.pos = li.get_position()
            self._paint(cell)

        def unbind(_f, li):
            li.get_child().pos = -1

        def teardown(_f, li):
            self._cells.discard(li.get_child())
        f.connect("teardown", teardown)
        f.connect("setup", setup)
        f.connect("bind", bind)
        f.connect("unbind", unbind)
        return f

    @staticmethod
    def _blank_factory():
        f = Gtk.SignalListItemFactory()
        f.connect("setup", lambda _f, li: li.set_child(Gtk.Box()))
        return f

    def _dummy_sorter(self):
        return Gtk.CustomSorter.new(lambda a, b, _u: 0, None)

    def _rebuild_columns(self):
        d = self.data
        cols = self.cv.get_columns()
        for i in range(cols.get_n_items() - 1, -1, -1):
            self.cv.remove_column(cols.get_item(i))
        self.slots = []
        self._guard = True
        digits = max(3, len(str(max(1, d.n_rows + 1))))
        self._rownum_w = int(self._char_px() * digits + 24)
        self.num_col = Gtk.ColumnViewColumn(title="", factory=self._make_factory("rownum", -1))
        self.num_col.set_fixed_width(self._rownum_w)
        self.cv.append_column(self.num_col)
        self.pad_l = Gtk.ColumnViewColumn(title="", factory=self._blank_factory())
        self.pad_l.set_visible(False)
        self.cv.append_column(self.pad_l)
        self._compute_widths()
        k = min(SLOTS, max(1, d.ncols))
        for j in range(k):
            col = Gtk.ColumnViewColumn(title="", factory=self._make_factory("data", j))
            col.set_resizable(True)
            col.set_sorter(self._dummy_sorter())
            col.set_header_menu(self._header_menu(j))
            col.connect("notify::fixed-width", self._on_col_resized, j)
            self.cv.append_column(col)
            self.slots.append(col)
        self.pad_r = Gtk.ColumnViewColumn(title="", factory=self._blank_factory())
        self.pad_r.set_visible(False)
        self.cv.append_column(self.pad_r)
        self._guard = False
        self.win_start = 0
        self._apply_window(0)

    def _header_menu(self, slot):
        m = Gio.Menu()

        def item(label, action):
            it = Gio.MenuItem.new(label, None)
            it.set_action_and_target_value("table." + action, GLib.Variant("i", slot))
            return it
        s1 = Gio.Menu()
        s1.append_item(item(_("Sort Ascending"), "col-sort-asc"))
        s1.append_item(item(_("Sort Descending"), "col-sort-desc"))
        s1.append_item(item(_("Sort Permanently, A → Z"), "col-sort-perm-asc"))
        s1.append_item(item(_("Sort Permanently, Z → A"), "col-sort-perm-desc"))
        s2 = Gio.Menu()
        s2.append_item(item(_("Rename Column…"), "col-rename"))
        s2.append_item(item(_("Select Column"), "col-select"))
        s2.append_item(item(_("Fit Column Width"), "col-fit"))
        s3 = Gio.Menu()
        s3.append_item(item(_("Insert Column Left"), "col-insert-left"))
        s3.append_item(item(_("Insert Column Right"), "col-insert-right"))
        s3.append_item(item(_("Delete Column"), "col-delete"))
        for s in (s1, s2, s3):
            m.append_section(None, s)
        return m

    def _rebuild_if_cols_changed(self, changed):
        if changed or len(self.widths) != self.data.ncols:
            self._rebuild_columns()
            self._repaint_all()

    def _retitle_slots(self):
        d = self.data
        for j, col in enumerate(self.slots):
            c = self.win_start + j
            if c < d.ncols:
                col.set_title(d.title(c))

    def _apply_window(self, start):
        d = self.data
        nc = d.ncols
        k = len(self.slots)
        start = max(0, min(start, max(0, nc - k)))
        self.win_start = start
        self._guard = True
        for j, col in enumerate(self.slots):
            c = start + j
            if c < nc:
                col.set_visible(True)
                col.set_title(d.title(c))
                col.set_fixed_width(self.widths[c])
            else:
                col.set_visible(False)
        end = min(nc, start + k)
        left = self.prefix[start] if self.widths else 0
        right = (self.prefix[nc] - self.prefix[end]) if self.widths else 0
        self.pad_l.set_visible(left > 0)
        if left > 0:
            self.pad_l.set_fixed_width(left)
        self.pad_r.set_visible(right > 0)
        if right > 0:
            self.pad_r.set_fixed_width(right)
        self._guard = False
        self._repaint_all()
        self._sync_sort_indicator()

    def _grow_slots(self, newk):
        newk = min(newk, self.data.ncols)
        while len(self.slots) < newk:
            j = len(self.slots)
            col = Gtk.ColumnViewColumn(title="", factory=self._make_factory("data", j))
            col.set_resizable(True)
            col.set_sorter(self._dummy_sorter())
            col.set_header_menu(self._header_menu(j))
            col.connect("notify::fixed-width", self._on_col_resized, j)
            self.cv.insert_column(2 + j, col)
            self.slots.append(col)

    def _on_hscroll(self, *_):
        d = self.data
        if not self.slots or d.ncols <= len(self.slots) and self.win_start == 0:
            return
        x = max(0.0, self.hadj.get_value() - self._rownum_w)
        w = self.hadj.get_page_size()
        p = self.prefix
        nc = d.ncols
        fv = max(0, min(nc - 1, bisect.bisect_right(p, x) - 1))
        lv = max(0, min(nc - 1, bisect.bisect_right(p, x + w) - 1))
        k = len(self.slots)
        span = lv - fv + 1
        if span + 6 > k and k < nc:
            self._grow_slots(span + 12)
            k = len(self.slots)
        first, last = self.win_start, self.win_start + k - 1
        if (fv < first + 2 and first > 0) or (lv > last - 2 and last < nc - 1):
            new = fv - max(0, (k - span) // 2)
            new = max(0, min(new, nc - k))
            if new != first:
                self._apply_window(new)

    def _on_col_resized(self, col, _pspec, slot):
        if self._guard:
            return
        c = self.win_start + slot
        if c >= self.data.ncols:
            return
        w = col.get_fixed_width()
        if w <= 0 or w == self.widths[c]:
            return
        self.widths[c] = w
        self.data.col_w[c] = w
        self._recompute_prefix()
        # keep the paddings consistent with the new widths
        k = len(self.slots)
        end = min(self.data.ncols, self.win_start + k)
        right = self.prefix[self.data.ncols] - self.prefix[end]
        left = self.prefix[self.win_start]
        self._guard = True
        if right > 0:
            self.pad_r.set_visible(True)
            self.pad_r.set_fixed_width(right)
        if left > 0:
            self.pad_l.set_fixed_width(left)
        self._guard = False

    def _scroll_to_col(self, c):
        if not self.widths:
            return
        c = max(0, min(c, self.data.ncols - 1))
        x0 = self._rownum_w + self.prefix[c]
        x1 = x0 + self.widths[c]
        v, page = self.hadj.get_value(), self.hadj.get_page_size()
        if x0 < v + self._rownum_w:
            self.hadj.set_value(max(0, x0 - self._rownum_w))
        elif x1 > v + page:
            self.hadj.set_value(x1 - page)
        self._on_hscroll()

    # ======================================================================
    # Painting
    # ======================================================================
    def _is_num_col(self, c):
        v = self._numcache.get(c)
        if v is None:
            v = self._numcache[c] = (self.data.col_type(c) == "number") if self.data.loaded else False
        return v

    def _paint(self, cell: Cell):
        r = cell.pos
        if r < 0:
            return
        d = self.data
        if cell.kind == "rownum":
            t = str(r + 1)
            if t != cell._txt:
                cell._txt = t
                cell.set_text(t)
            return
        c = self.win_start + cell.slot
        if c >= d.ncols or r >= d.n_view:
            if cell._txt != "":
                cell._txt = ""
                cell.set_text("")
            self._set_flags(cell, 0)
            return
        key = d.key_at(r)
        txt = d.cell(key, c)
        disp = txt
        if len(disp) > MAX_RENDER_CHARS or "\n" in disp or "\r" in disp:
            disp = disp[:MAX_RENDER_CHARS].replace("\r\n", ROW_NL).replace("\n", ROW_NL).replace("\r", ROW_NL)
        if disp != cell._txt:
            cell._txt = disp
            cell.set_text(disp)
        num = self._is_num_col(c) if d.loaded else False
        if num != cell._num:
            cell._num = num
            cell.set_xalign(1.0 if num else 0.0)
        flags = 0
        r0, c0, r1, c1 = self._sel_rect()
        if r0 <= r <= r1 and c0 <= c <= c1 and not (r0 == r1 and c0 == c1):
            flags |= 1
        if (r, c) == self.cur:
            flags |= 2
        if self._search_lc and txt and self._search_lc in txt.casefold():
            flags |= 4
        if key < 0:
            flags |= 8
        self._set_flags(cell, flags)

    _FLAG_CLASSES = ((1, "tv-sel"), (2, "tv-cur"), (4, "tv-match"), (8, "tv-newrow"))

    def _set_flags(self, cell, flags):
        old = cell._flags
        if old == flags:
            return
        for bit, name in self._FLAG_CLASSES:
            if (old ^ flags) & bit:
                if flags & bit:
                    cell.add_css_class(name)
                else:
                    cell.remove_css_class(name)
        cell._flags = flags

    def _repaint_all(self):
        for cell in list(self._cells):
            self._paint(cell)

    # ======================================================================
    # Cell cursor and selection
    # ======================================================================
    def _sel_rect(self):
        (ar, ac), (cr, cc) = self.anchor, self.cur
        return min(ar, cr), min(ac, cc), max(ar, cr), max(ac, cc)

    def _set_cursor(self, r, c, extend=False, scroll=True):
        d = self.data
        n, nc = d.n_view, d.ncols
        if n == 0 or nc == 0:
            return
        r = max(0, min(n - 1, r))
        c = max(0, min(nc - 1, c))
        self.cur = (r, c)
        if not extend:
            self.anchor = self.cur
        self._repaint_all()
        if scroll:
            self._ensure_visible(r, c)
        self._schedule_status()

    def _find_cell(self, r, c):
        slot = c - self.win_start
        for cell in self._cells:
            if cell.kind == "data" and cell.pos == r and cell.slot == slot:
                return cell
        return None

    def _header_h(self):
        ch = self.cv.get_first_child()
        h = ch.get_height() if ch is not None else 0
        return h if 8 < h < 120 else 34

    def _ensure_visible(self, r, c):
        self._scroll_to_col(c)
        # vertical
        row_cell = None
        for cell in self._cells:
            if cell.kind == "rownum" and cell.pos == r:
                row_cell = cell
                break
        if row_cell is None:
            self.cv.scroll_to(r, None, Gtk.ListScrollFlags.NONE, None)
            return
        ok, rect = row_cell.compute_bounds(self.cv)
        if not ok:
            return
        top = rect.get_y()
        bot = top + rect.get_height()
        hh = self._header_h()
        view_h = self.scroller.get_height()
        if top < hh:
            self.vadj.set_value(self.vadj.get_value() - (hh - top))
        elif bot > view_h:
            self.vadj.set_value(self.vadj.get_value() + (bot - view_h))

    def _row_height(self):
        for cell in self._cells:
            if cell.kind == "rownum" and cell.pos >= 0:
                p = cell.get_parent()
                if p is not None and p.get_height() > 4:
                    return p.get_height()
        return 28

    def _visible_rows(self):
        return max(1, int((self.scroller.get_height() - self._header_h()) / self._row_height()) - 1)

    def select_all(self):
        d = self.data
        if d.n_view and d.ncols:
            self.anchor = (0, 0)
            self.cur = (d.n_view - 1, d.ncols - 1)
            self._repaint_all()
            self._schedule_status()

    def _col_select(self, slot):
        c = self.win_start + slot
        n = self.data.n_view
        if n:
            self.anchor, self.cur = (0, c), (n - 1, c)
            self._repaint_all()
            self._schedule_status()

    def _schedule_status(self):
        if self._status_pending:
            return
        self._status_pending = True
        GLib.timeout_add(60, self._update_status)

    def _update_status(self):
        self._status_pending = False
        d = self.data
        if not d.nbase:
            self.status.set_text("")
            return False
        r0, c0, r1, c1 = self._sel_rect()
        cnt = (r1 - r0 + 1) * (c1 - c0 + 1)
        if d.n_view == 0:
            self.status.set_text(_("No rows"))
            return False
        if cnt == 1:
            t = d.cell_at(self.cur[0], self.cur[1])
            self.status.set_text(f"{col_letter(self.cur[1])}{self.cur[0] + 1}"
                                 + (f"  ·  {d.title(self.cur[1])}" if d.has_header else "")
                                 + (_("  ·  {n:,} characters").format(n=len(t))
                                    if len(t) > 80 else ""))
            return False
        s = _("{rows:,} × {cols:,} cells selected").format(rows=r1 - r0 + 1, cols=c1 - c0 + 1)
        if cnt <= 40000:
            vals = []
            for r in range(r0, r1 + 1):
                row = d.row(d.key_at(r))
                for c in range(c0, c1 + 1):
                    v = to_num(row[c]) if c < len(row) else None
                    if v is not None:
                        vals.append(v)
            if vals:
                s += _("  ·  {n:,} numeric  ·  Sum {sum}  ·  Avg {avg}  ·  Min {min}  ·  Max {max}").format(
                    n=len(vals), sum=_fmt_num(sum(vals)), avg=_fmt_num(sum(vals) / len(vals)),
                    min=_fmt_num(min(vals)), max=_fmt_num(max(vals)))
        self.status.set_text(s)
        return False

    # ======================================================================
    # Mouse
    # ======================================================================
    def _cell_at(self, x, y):
        w = self.cv.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and not isinstance(w, Cell):
            if w is self.cv:
                return None
            w = w.get_parent()
        return w

    def _on_pressed(self, gesture, n_press, x, y):
        cell = self._cell_at(x, y)
        if cell is None or cell.pos < 0 or self.data.n_view == 0:
            return
        if self._editing:
            self._commit_edit()
        button = gesture.get_current_button()
        shift = bool(gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
        self._focus_table()
        if button == 1:
            if cell.kind == "rownum":
                r = cell.pos
                nc = self.data.ncols
                if shift:
                    self.cur = (r, nc - 1)
                    self.anchor = (self.anchor[0], 0)
                else:
                    self.anchor, self.cur = (r, 0), (r, nc - 1)
                self._drag = "row"
                self._repaint_all()
                self._schedule_status()
                return
            c = self.win_start + cell.slot
            if n_press >= 2 and not shift:
                self._set_cursor(cell.pos, c)
                self._start_edit()
                return
            self._set_cursor(cell.pos, c, extend=shift, scroll=False)
            self._drag = "cells"
        elif button == 3:
            r, c = cell.pos, (self.win_start + cell.slot) if cell.kind == "data" else 0
            r0, c0, r1, c1 = self._sel_rect()
            if not (r0 <= r <= r1 and c0 <= c <= c1):
                if cell.kind == "rownum":
                    self.anchor, self.cur = (r, 0), (r, self.data.ncols - 1)
                    self._repaint_all()
                else:
                    self._set_cursor(r, c, scroll=False)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            self.cell_popover.set_pointing_to(rect)
            self.cell_popover.popup()

    def _on_released(self, gesture, n_press, x, y):
        self._drag = False
        self._stop_autoscroll()

    def _on_motion(self, ctrl, x, y):
        self._ptr = (x, y)
        if not self._drag:
            return
        st = ctrl.get_current_event_state()
        if not (st & Gdk.ModifierType.BUTTON1_MASK):
            self._drag = False
            self._stop_autoscroll()
            return
        cell = self._cell_at(x, y)
        if cell is not None and cell.pos >= 0:
            if self._drag == "row":
                self.cur = (cell.pos, self.data.ncols - 1)
            else:
                if cell.kind == "rownum":
                    c = self.cur[1]
                else:
                    c = self.win_start + cell.slot
                self.cur = (cell.pos, c)
            self._repaint_all()
            self._schedule_status()
        h = self.scroller.get_height()
        if y < self._header_h() + 8 or y > h - 8 or x < 8 or x > self.scroller.get_width() - 8:
            if not self._autoscroll:
                self._autoscroll = GLib.timeout_add(40, self._autoscroll_step)
        else:
            self._stop_autoscroll()

    def _autoscroll_step(self):
        if not self._drag:
            self._autoscroll = 0
            return False
        x, y = self._ptr
        h, w = self.scroller.get_height(), self.scroller.get_width()
        dy = -20 if y < self._header_h() + 8 else (20 if y > h - 8 else 0)
        dx = -30 if x < 8 else (30 if x > w - 8 else 0)
        if dy:
            self.vadj.set_value(self.vadj.get_value() + dy)
        if dx:
            self.hadj.set_value(self.hadj.get_value() + dx)
        cell = self._cell_at(min(max(x, 4), w - 4), min(max(y, self._header_h() + 4), h - 4))
        if cell is not None and cell.pos >= 0 and cell.kind == "data":
            self.cur = (cell.pos, self.win_start + cell.slot)
            self._repaint_all()
            self._schedule_status()
        return True

    def _stop_autoscroll(self):
        if self._autoscroll:
            GLib.source_remove(self._autoscroll)
            self._autoscroll = 0

    def _on_vscroll(self):
        if self._editing:
            self._commit_edit()

    # ======================================================================
    # Keyboard
    # ======================================================================
    def _focus_in_table(self):
        """False while a text field (search box, cell editor, ...) has the keyboard."""
        root = self.get_root()
        f = root.get_focus() if root is not None else None
        if f is None:
            return True
        if isinstance(f, (Gtk.Text, Gtk.Editable, Gtk.TextView)):
            return False
        return True

    def _on_key(self, ctrl, keyval, keycode, state):
        if self._editing or not self.get_mapped() or not self._focus_in_table():
            return False
        d = self.data
        ctl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        K = Gdk
        name = Gdk.keyval_name(keyval) or ""
        r, c = self.cur
        n, nc = d.n_view, d.ncols
        editable = not self.compact and d.loaded and not self._busy

        if self.compact:
            if ctl and name.lower() in ("c", "a", "f"):
                {"c": self.copy_selection, "a": self.select_all, "f": self.show_search}[name.lower()]()
                return True
            return False
        if ctl and not alt:
            lk = name.lower()
            if lk == "c":
                self.copy_selection()
                return True
            if lk == "x" and editable:
                self.copy_selection(cut=True)
                return True
            if lk == "v" and editable:
                self.paste()
                return True
            if lk == "a":
                self.select_all()
                return True
            if lk == "z" and editable:
                self.do_redo() if shift else self.do_undo()
                return True
            if lk == "y" and editable:
                self.do_redo()
                return True
            if lk == "s" and editable:
                self.save_as() if shift else self.save_changes(None)
                return True
            if lk == "f":
                self.show_search()
                return True
            if lk == "g":
                self._goto_match(not shift)
                return True
            if name == "Home":
                self._set_cursor(0, 0, shift)
                return True
            if name == "End":
                self._set_cursor(n - 1, nc - 1, shift)
                return True
            if name in ("Up", "Down", "Left", "Right"):
                if name == "Up":
                    self._set_cursor(0, c, shift)
                elif name == "Down":
                    self._set_cursor(n - 1, c, shift)
                elif name == "Left":
                    self._set_cursor(r, 0, shift)
                else:
                    self._set_cursor(r, nc - 1, shift)
                return True
            return False
        if name == "F3":
            self._goto_match(not shift)
            return True
        if n == 0 or nc == 0:
            return False
        moves = {"Up": (-1, 0), "Down": (1, 0), "Left": (0, -1), "Right": (0, 1)}
        if name in moves:
            self._set_cursor(r + moves[name][0], c + moves[name][1], shift)
            return True
        if name == "Home":
            self._set_cursor(r, 0, shift)
            return True
        if name == "End":
            self._set_cursor(r, nc - 1, shift)
            return True
        if name in ("Page_Up", "Page_Down"):
            pg = self._visible_rows()
            self._set_cursor(r + (pg if name == "Page_Down" else -pg), c, shift)
            return True
        if name in ("Tab", "ISO_Left_Tab"):
            back = shift or name == "ISO_Left_Tab"
            if back:
                if c > 0:
                    self._set_cursor(r, c - 1)
                elif r > 0:
                    self._set_cursor(r - 1, nc - 1)
            else:
                if c < nc - 1:
                    self._set_cursor(r, c + 1)
                elif r < n - 1:
                    self._set_cursor(r + 1, 0)
            return True
        if name in ("Return", "KP_Enter", "F2"):
            if editable:
                self._start_edit()
            return True
        if name in ("Delete", "BackSpace"):
            if editable:
                self.clear_selection()
            return True
        if name == "Escape":
            if self.search_bar.get_search_mode():
                self._close_search()
            else:
                self.anchor = self.cur
                self._repaint_all()
                self._schedule_status()
            return True
        if name == "space" and shift:
            self.anchor, self.cur = (r, 0), (r, nc - 1)
            self._repaint_all()
            return True
        if editable and not alt:
            u = Gdk.keyval_to_unicode(keyval)
            if u >= 32 and u != 127 and not (state & Gdk.ModifierType.SUPER_MASK):
                self._start_edit(initial=chr(u), typed=True)
                return True
        return False

    # ======================================================================
    # Cell editing
    # ======================================================================
    def _guard_edit(self):
        if self.compact:
            return False
        if self._loading or not self.data.loaded:
            self.toast(_("The file is still loading"))
            return False
        if self._busy:
            self.toast(_("Please wait for the current operation to finish"))
            return False
        return True

    def _start_edit(self, initial=None, typed=False):
        if not self._guard_edit():
            return
        d = self.data
        r, c = self.cur
        if d.n_view == 0 or d.ncols == 0:
            return
        text = initial if initial is not None else d.cell_at(r, c)
        if initial is None and ("\n" in text or "\r" in text):
            self._edit_dialog(r, c)
            return
        cell = self._find_cell(r, c)
        if cell is None:
            self._ensure_visible(r, c)
            GLib.timeout_add(60, lambda: (self._start_edit(initial, typed) if self._find_cell(r, c) else None, False)[1])
            return
        ok, rect = cell.compute_bounds(self.overlay)
        if not ok:
            return
        e = self.edit_entry
        e.set_margin_start(int(rect.get_x()))
        e.set_margin_top(int(rect.get_y()))
        e.set_size_request(max(40, int(rect.get_width())), max(20, int(rect.get_height())))
        e.set_text(text)
        e.set_alignment(1.0 if cell._num else 0.0)
        self._editing = (r, c, d.cell_at(r, c), typed)
        self._edit_t0 = GLib.get_monotonic_time()
        e.set_visible(True)
        self._focus_editor(0)

    def _focus_editor(self, tries):
        if not self._editing:
            return False
        e = self.edit_entry
        e.grab_focus()
        root = self.get_root()
        f = root.get_focus() if root is not None else None
        if f is not None and (f is e or f.is_ancestor(e)):
            e.set_position(-1)
            return False
        if tries < 15:
            GLib.timeout_add(20, self._focus_editor, tries + 1)
        return False

    def _on_edit_key(self, ctrl, keyval, keycode, state):
        if not self._editing:
            return False
        name = Gdk.keyval_name(keyval) or ""
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        typed = self._editing[3]
        if name in ("Return", "KP_Enter"):
            self._commit_edit(move=(-1 if shift else 1, 0))
            return True
        if name == "Escape":
            self._cancel_edit()
            return True
        if name in ("Tab", "ISO_Left_Tab"):
            self._commit_edit(move=(0, -1 if (shift or name == "ISO_Left_Tab") else 1))
            return True
        if typed and name in ("Up", "Down"):
            self._commit_edit(move=(-1 if name == "Up" else 1, 0))
            return True
        if typed and name in ("Left", "Right"):
            e = self.edit_entry
            pos = e.get_position()
            if (name == "Left" and pos <= 0) or (name == "Right" and pos >= len(e.get_text())):
                self._commit_edit(move=(0, -1 if name == "Left" else 1))
                return True
        return False

    def _on_edit_focus_out(self, *_):
        if self._editing and self.edit_entry.get_visible():
            def check():
                if self._editing:
                    root = self.get_root()
                    f = root.get_focus() if root is not None else None
                    inside = f is not None and (f is self.edit_entry or f.is_ancestor(self.edit_entry))
                    if not inside and root is not None and root.is_active() and \
                            GLib.get_monotonic_time() - self._edit_t0 > 400_000:
                        self._commit_edit(refocus=False)
                return False
            GLib.timeout_add(80, check)

    def _close_editor(self):
        self.edit_entry.set_visible(False)
        self._editing = None

    def _cancel_edit(self):
        self._close_editor()
        self._focus_table()

    def _commit_edit(self, move=None, refocus=True):
        if not self._editing:
            return
        r, c, old, _typed = self._editing
        text = self.edit_entry.get_text()
        self._close_editor()
        d = self.data
        if r < d.n_view and text != old:
            d.set_cells([(d.key_at(r), c, text)], _("Edit Cell"))
        if refocus:
            self._focus_table()
        if move:
            self._set_cursor(r + move[0], c + move[1])
        else:
            self._repaint_all()

    def _edit_dialog(self, r, c):
        d = self.data
        dlg = Adw.AlertDialog(heading=_("Edit Cell {cell}").format(cell=f"{col_letter(c)}{r + 1}"))
        tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                          left_margin=8, right_margin=8, accepts_tab=False)
        tv.get_buffer().set_text(d.cell_at(r, c))
        sw = Gtk.ScrolledWindow(child=tv, min_content_height=160, min_content_width=420,
                                css_classes=["card"])
        dlg.set_extra_child(sw)
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("ok", _("Apply"))
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def resp(_d, rid):
            if rid == "ok":
                buf = tv.get_buffer()
                text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
                if r < d.n_view:
                    d.set_cells([(d.key_at(r), c, text)], _("Edit Cell"))
            self._focus_table()
        dlg.connect("response", resp)
        dlg.present(self)

    # ======================================================================
    # Clipboard
    # ======================================================================
    @staticmethod
    def _tsv_field(s: str) -> str:
        if "\t" in s or "\n" in s or "\r" in s or '"' in s:
            return '"' + s.replace('"', '""') + '"'
        return s

    def selection_text(self):
        d = self.data
        r0, c0, r1, c1 = self._sel_rect()
        if (r1 - r0 + 1) * (c1 - c0 + 1) > 6_000_000:
            raise TableError("The selection is too large to copy.")
        lines = []
        for r in range(r0, min(r1, d.n_view - 1) + 1):
            row = d.row(d.key_at(r))
            lines.append("\t".join(self._tsv_field(row[c] if c < len(row) else "") for c in range(c0, c1 + 1)))
        return "\n".join(lines)

    def selection_html(self, limit=20000):
        d = self.data
        r0, c0, r1, c1 = self._sel_rect()
        if (r1 - r0 + 1) * (c1 - c0 + 1) > limit:
            return None
        esc = GLib.markup_escape_text
        parts = ["<table>"]
        for r in range(r0, r1 + 1):
            row = d.row(d.key_at(r))
            parts.append("<tr>" + "".join(
                f"<td>{esc(row[c] if c < len(row) else '').replace(chr(10), '<br>')}</td>"
                for c in range(c0, c1 + 1)) + "</tr>")
        parts.append("</table>")
        return "".join(parts)

    def copy_selection(self, cut=False):
        d = self.data
        if d.n_view == 0:
            return
        try:
            text = self.selection_text()
        except TableError as e:
            self.toast(str(e))
            return
        provs = [Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new(text.encode()))]
        html = self.selection_html()
        if html is not None:
            provs.append(Gdk.ContentProvider.new_for_bytes(
                "text/html", GLib.Bytes.new(("<meta charset='utf-8'>" + html).encode())))
        self.get_clipboard().set_content(Gdk.ContentProvider.new_union(provs))
        if cut and not self.compact:
            self.clear_selection()

    def paste(self):
        if not self._guard_edit():
            return
        self.get_clipboard().read_text_async(None, self._on_paste_text)

    def _on_paste_text(self, clip, res):
        try:
            text = clip.read_text_finish(res)
        except GLib.Error:
            return
        if not text:
            return
        self._paste_text(text)

    def _paste_text(self, text):
        d = self.data
        if text.endswith("\r\n"):
            text = text[:-2]
        elif text.endswith("\n"):
            text = text[:-1]
        if "\t" in text or "\n" in text:
            delim = "\t" if "\t" in text else (d.delim if d.delim in text else "\t")
            try:
                block = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delim)]
            except csv.Error:
                block = [l.split("\t") for l in text.splitlines()]
        else:
            block = [[text]]
        if not block:
            return
        h = len(block)
        w = max(len(r) for r in block)
        if h * w > 5_000_000:
            self.toast(_("The clipboard data is too large to paste."))
            return
        r0, c0, r1, c1 = self._sel_rect()
        single = h == 1 and w == 1
        with d.group(_("Paste")):
            if single and (r1 > r0 or c1 > c0):
                # fill the whole selection with the one value
                ch = [(d.key_at(r), c, block[0][0]) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]
                d.set_cells(ch, _("Paste"))
            else:
                d.ensure_size(r0 + h, c0 + w)
                ch = []
                for i, row in enumerate(block):
                    key = d.key_at(r0 + i)
                    for j, v in enumerate(row):
                        ch.append((key, c0 + j, v))
                d.set_cells(ch, _("Paste"))
        self.anchor = (r0, c0)
        self.cur = (min(d.n_view - 1, r0 + (r1 - r0 if single else h - 1)),
                    min(d.ncols - 1, c0 + (c1 - c0 if single else w - 1)))
        self._repaint_all()

    def clear_selection(self):
        if not self._guard_edit():
            return
        d = self.data
        r0, c0, r1, c1 = self._sel_rect()
        ch = [(d.key_at(r), c, "") for r in range(r0, min(r1, d.n_view - 1) + 1) for c in range(c0, c1 + 1)]
        d.set_cells(ch, _("Clear Cells"))

    # ======================================================================
    # Rows and columns
    # ======================================================================
    def insert_rows(self, below):
        if not self._guard_edit():
            return
        d = self.data
        r0, c0, r1, c1 = self._sel_rect()
        count = r1 - r0 + 1
        pos = (r1 + 1) if below else r0
        if d.n_view == 0:
            pos, count = 0, 1
        d.insert_rows(pos, count)
        self.anchor = (pos, self.cur[1])
        self.cur = (pos + count - 1, self.cur[1])
        self._repaint_all()
        self._ensure_visible(pos, self.cur[1])

    def delete_rows(self):
        if not self._guard_edit():
            return
        d = self.data
        r0, c0, r1, c1 = self._sel_rect()
        if d.n_view == 0:
            return
        d.delete_rows(range(r0, r1 + 1))
        self.cur = self.anchor = (min(r0, max(0, d.n_view - 1)), self.cur[1])
        self._repaint_all()

    def insert_cols(self, right):
        if not self._guard_edit():
            return
        d = self.data
        r0, c0, r1, c1 = self._sel_rect()
        count = c1 - c0 + 1
        pos = (c1 + 1) if right else c0
        d.insert_cols(pos, count)
        self.anchor = (self.anchor[0], pos)
        self.cur = (self.cur[0], pos + count - 1)
        self._repaint_all()
        self._ensure_visible(self.cur[0], pos)

    def delete_cols(self):
        if not self._guard_edit():
            return
        d = self.data
        r0, c0, r1, c1 = self._sel_rect()
        if (c1 - c0 + 1) >= d.ncols:
            self.toast(_("A table needs at least one column"))
            return
        d.delete_cols(range(c0, c1 + 1))
        c = min(c0, d.ncols - 1)
        self.anchor, self.cur = (self.anchor[0], c), (self.cur[0], c)
        self._repaint_all()

    def _col_insert(self, slot, right):
        c = self.win_start + slot
        self.anchor = (self.cur[0], c)
        self.cur = (self.cur[0], c)
        self.insert_cols(right)

    def _col_delete(self, slot):
        c = self.win_start + slot
        r0, c0, r1, c1 = self._sel_rect()
        if not (c0 <= c <= c1 and r0 == 0 and r1 == self.data.n_view - 1):
            self.anchor = (self.cur[0], c)
            self.cur = (self.cur[0], c)
        self.delete_cols()

    def _col_fit(self, slot):
        c = self.win_start + slot
        if c >= self.data.ncols:
            return
        w = self._auto_width(c)
        self.widths[c] = w
        self.data.col_w[c] = w
        self._recompute_prefix()
        self._apply_window(self.win_start)

    def _col_rename(self, slot):
        d = self.data
        c = self.win_start + slot
        if not d.has_header:
            self.toast(_("Turn on “First Row Is Header” to name columns"))
            return
        if not self._guard_edit():
            return
        dlg = Adw.AlertDialog(heading=_("Rename Column"))
        entry = Gtk.Entry(text=d.headers[c], activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("ok", _("Rename"))
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def resp(_d, rid):
            if rid == "ok":
                d.set_header(c, entry.get_text())
            self._focus_table()
        dlg.connect("response", resp)
        dlg.present(self)
        entry.grab_focus()

    # ======================================================================
    # Undo / redo
    # ======================================================================
    def do_undo(self):
        if self._guard_edit():
            self._cancel_edit()
            self.data.undo()

    def do_redo(self):
        if self._guard_edit():
            self._cancel_edit()
            self.data.redo()

    # ======================================================================
    # Sorting
    # ======================================================================
    def _on_sorter_changed(self, sorter, change):
        if self._guard:
            return
        col = sorter.get_primary_sort_column()
        if col is None:
            return
        order = sorter.get_primary_sort_order()
        for j, sc in enumerate(self.slots):
            if sc == col:
                self._set_sort(self.win_start + j, order == Gtk.SortType.ASCENDING)
                return

    def _sync_sort_indicator(self):
        d = self.data
        self._guard = True
        try:
            col = None
            if d.sort_col is not None:
                j = d.sort_col - self.win_start
                if 0 <= j < len(self.slots):
                    col = self.slots[j]
            self.cv.sort_by_column(col, Gtk.SortType.ASCENDING if d.sort_asc else Gtk.SortType.DESCENDING)
        except Exception:  # noqa: BLE001
            pass
        self._guard = False

    def _col_sort(self, slot, asc):
        self._set_sort(self.win_start + slot, asc)

    def _set_sort(self, col, asc):
        d = self.data
        if not d.loaded:
            return
        if col is None and d.sort_col is None:
            return
        self._recompute_view(col, asc, d.filter_on, label=_("Sorting…"))

    def _recompute_view(self, sort_col, asc, filter_on, label=N_("Working…"), then=None):
        d = self.data
        self._view_gen += 1
        gen = self._view_gen
        mut = d.mut
        match_keys = d.match_keys
        self._begin_busy(_(label))

        def work():
            try:
                view = d.compute_view(sort_col, asc, filter_on, match_keys)
                err = None
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                view, err = None, e
            GLib.idle_add(done, view, err)

        def done(view, err):
            if gen != self._view_gen or d is not self.data:
                return False
            self._end_busy()
            if err is not None:
                self.toast(_("Could not sort: {err}").format(err=err))
            elif d.mut != mut:
                self._recompute_view(sort_col, asc, filter_on, label, then)
            else:
                d.set_view(view, sort_col, asc, filter_on)
                self._set_cursor(0, self.cur[1], scroll=True)
                self.vadj.set_value(0)
                self._update_match_label()
                if then:
                    then()
            return False
        threading.Thread(target=work, daemon=True).start()

    def _col_sort_permanent(self, slot, asc):
        if not self._guard_edit():
            return
        d = self.data
        col = self.win_start + slot
        self._begin_busy(_("Sorting…"))

        def work():
            try:
                new = d.sorted_keys(d.order, col, asc)
                err = None
            except Exception as e:  # noqa: BLE001
                new, err = None, e
            GLib.idle_add(done, new, err)

        def done(new, err):
            self._end_busy()
            if err is not None:
                self.toast(_("Could not sort: {err}").format(err=err))
            else:
                d.set_order(new, _("Sort"))
                self.vadj.set_value(0)
            return False
        threading.Thread(target=work, daemon=True).start()

    # ---- busy indicator ---------------------------------------------------------------
    def _begin_busy(self, label):
        self._busy += 1
        self._busy_label = label
        self.progress.set_visible(True)
        if self._busy == 1:
            self._pulse = GLib.timeout_add(80, self._pulse_step)
        self._update_sensitivity()
        self.status.set_text(label)

    def _pulse_step(self):
        if self._busy:
            self.progress.pulse()
            return True
        return False

    def _end_busy(self):
        self._busy = max(0, self._busy - 1)
        if not self._busy and not self._loading:
            self.progress.set_visible(False)
        self._update_sensitivity()
        self._schedule_status()
        self.refresh_title()

    # ======================================================================
    # Search
    # ======================================================================
    def show_search(self):
        self.search_bar.set_search_mode(True)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def _close_search(self):
        self.search_bar.set_search_mode(False)
        self.search_entry.set_text("")
        self._focus_table()

    def _on_search_changed(self, entry):
        self._queue_search(120)

    def _queue_search(self, ms):
        if self._search_timer:
            GLib.source_remove(self._search_timer)
        self._search_timer = GLib.timeout_add(ms, self._run_search)

    def _run_search(self):
        self._search_timer = 0
        d = self.data
        if not d.loaded:
            return False
        text = self.search_entry.get_text() if self.search_bar.get_search_mode() or self.search_entry.get_text() else ""
        if not self.search_bar.get_search_mode():
            text = ""
        gen = self._view_gen = self._view_gen + 1
        self._search_lc = text.casefold()
        if not text:
            d.set_matches("", np_empty())
            if d.filter_on:
                d.filter_on = False
                self._recompute_view(d.sort_col, d.sort_asc, False)
            self._update_match_label()
            self._repaint_all()
            return False
        self._begin_busy(_("Searching…"))
        mut = d.mut

        def work():
            try:
                keys = d.find_matches(text)
                err = None
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                keys, err = None, e
            GLib.idle_add(done, keys, err)

        def done(keys, err):
            if d is not self.data:
                return False
            self._end_busy()
            if err is not None or gen != self._view_gen:
                return False
            if d.mut != mut:
                self._queue_search(50)
                return False
            d.set_matches(text, keys)
            if self.filter_btn.get_active() and not d.filter_on or d.filter_on:
                d.filter_on = self.filter_btn.get_active()
                if d.filter_on or self._view_is_filtered():
                    self._recompute_view(d.sort_col, d.sort_asc, d.filter_on)
            self._update_match_label()
            self._repaint_all()
            if len(d.match_vpos) and not d.filter_on:
                self._goto_match(True, include_current=True)
            return False
        threading.Thread(target=work, daemon=True).start()
        return False

    def _view_is_filtered(self):
        return len(self.data.view) != len(self.data.order)

    def _on_filter_toggled(self, btn):
        d = self.data
        if not d.loaded:
            return
        on = btn.get_active() and bool(d.search_text)
        if on != d.filter_on or (not btn.get_active() and self._view_is_filtered()):
            self._recompute_view(d.sort_col, d.sort_asc, on, label=_("Filtering…"))

    def _update_match_label(self):
        d = self.data
        if not d.search_text:
            self.match_label.set_text("")
        elif len(d.match_keys) == 0:
            self.match_label.set_text(_("No matches"))
        else:
            n = len(d.match_keys)
            self.match_label.set_text(f"{n:,} row{'s' if n != 1 else ''}")

    def _goto_match(self, forward=True, include_current=False):
        d = self.data
        mv = d.match_vpos
        if not len(mv):
            return
        r = self.cur[0]
        if forward:
            i = int(np.searchsorted(mv, r, side="left" if include_current else "right"))
            if i >= len(mv):
                i = 0
        else:
            i = int(np.searchsorted(mv, r, side="left")) - 1
            if i < 0:
                i = len(mv) - 1
        row = int(mv[i])
        cf = d.search_text.casefold()
        cells = d.row(d.key_at(row))
        col = next((c for c, t in enumerate(cells) if cf in t.casefold()), 0)
        self._set_cursor(row, col)

    # ======================================================================
    # Format changes (header toggle, delimiter, encoding)
    # ======================================================================
    def _toggle_header(self):
        d = self.data
        if not self._guard_edit():
            return
        d.set_has_header(not d.has_header)
        self.act_header.set_state(GLib.Variant("b", d.has_header))
        self._numcache.clear()

    def _toggle_quote(self):
        d = self.data
        if not self._guard_edit():
            return
        old = d.quoting
        new = "minimal" if old == "all" else "all"

        def setv(v):
            d.quoting = v
            self.act_quote.set_state(GLib.Variant("b", v == "all"))
        setv(new)
        d._push(Command(_("Quoting"), lambda: setv(old), lambda: setv(new)))
        self.refresh_title()

    def _reload_with(self, **kw):
        if self.data.has_unsaved_changes():
            self.toast(_("Save or undo your changes first"))
            self._sync_format_actions()
            return
        if self.gfile is None:
            return
        path = self.gfile.get_path() or self.data.path
        self._start_load(path, **kw)

    def _choose_delimiter(self, v):
        if not self._guard_edit():
            self._sync_format_actions()
            return
        self._reload_with(delimiter=v, encoding=self.data.out_enc if self.data.out_enc != self.data.enc else self.data.enc,
                          header=self.data.has_header)

    def _choose_encoding(self, v):
        if not self._guard_edit():
            self._sync_format_actions()
            return
        self._reload_with(encoding=v, delimiter=self.data.delim, header=self.data.has_header)

    def _choose_eol(self, v):
        d = self.data
        if not self._guard_edit():
            return
        new = b"\r\n" if v == "crlf" else b"\n"
        if new == d.out_eol:
            return
        old = d.out_eol

        def setv(x):
            d.out_eol = x
            self.act_eol.set_state(GLib.Variant("s", "crlf" if x == b"\r\n" else "lf"))
        setv(new)
        d._push(Command(_("Line Endings"), lambda: setv(old), lambda: setv(new)))
        self.refresh_title()

    # ======================================================================
    # Saving
    # ======================================================================
    def save_changes(self, on_done=None):
        """Save to the current file (CSV/TSV, preserving its dialect); the shell
        calls this from the close prompt: on_done(ok)."""
        d = self.data
        if self.gfile is None or self.gfile.get_path() is None:
            self.save_as(on_done)
            return
        if self._loading or not d.loaded:
            self.toast(_("The file is still loading"))
            if on_done:
                on_done(False)
            return
        path = self.gfile.get_path()
        ext = os.path.splitext(path)[1].lower()
        fmt = "tsv" if ext in (".tsv", ".tab") else "csv"
        self._save_async(path, fmt, self._out_delim, on_done, same_doc=True)

    def save_as(self, on_done=None):
        d = self.data
        if self._loading or not d.loaded:
            return
        dlg = Gtk.FileDialog(title=_("Save As"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        for name, pats in ((_("CSV (comma-separated)"), ["*.csv"]), (_("TSV (tab-separated)"), ["*.tsv", "*.tab"]),
                           ("JSON", ["*.json"]), (_("Excel workbook"), ["*.xlsx"])):
            f = Gtk.FileFilter(name=name)
            for p in pats:
                f.add_pattern(p)
            filters.append(f)
        dlg.set_filters(filters)
        base = os.path.splitext(self.gfile.get_basename() if self.gfile else "table")[0]
        ext = ".tsv" if self._out_delim == "\t" else ".csv"
        dlg.set_initial_name(base + ext)
        if self.gfile is not None and self.gfile.get_parent() is not None:
            dlg.set_initial_folder(self.gfile.get_parent())

        def done(dialog, res):
            try:
                gf = dialog.save_finish(res)
            except GLib.Error:
                if on_done:
                    on_done(False)
                return
            path = gf.get_path()
            e = os.path.splitext(path)[1].lower()
            if e in (".tsv", ".tab"):
                fmt, delim = "tsv", "\t"
            elif e == ".json":
                fmt, delim = "json", None
            elif e in (".xlsx",):
                fmt, delim = "xlsx", None
            else:
                fmt, delim = "csv", ","
                if e not in (".csv", ".txt"):
                    path += ".csv"
                    gf = Gio.File.new_for_path(path)
            self._save_async(path, fmt, delim, on_done, same_doc=fmt in ("csv", "tsv"), gfile=gf)
        dlg.save(self.get_root(), None, done)

    def _save_async(self, path, fmt, delim, on_done, same_doc, gfile=None):
        d = self.data
        if self._busy:
            self.toast(_("Please wait for the current operation to finish"))
            if on_done:
                on_done(False)
            return
        self._cancel_edit()
        self._begin_busy(_("Saving…"))
        self.progress.set_fraction(0.0)
        frac = [0.0]

        def prog(f):
            frac[0] = f

        def work():
            try:
                note = d.save_to(path, fmt, delim, prog)
                err = None
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                note, err = None, e
            GLib.idle_add(done, note, err)

        def tick():
            if self._busy and self.progress.get_visible():
                self.progress.set_fraction(frac[0])
                return True
            return False

        def done(note, err):
            self._end_busy()
            if err is not None:
                self.toast(_("Could not save: {err}").format(err=err))
                if on_done:
                    on_done(False)
                return False
            if same_doc:
                d.mark_saved()
                if gfile is not None:
                    self.gfile = gfile
                    d.path = path
                if delim:
                    self._out_delim = delim
                self.toast(note or _("Saved “{name}”").format(name=os.path.basename(path)))
            else:
                self.toast(_("Exported “{v}”").format(v=os.path.basename(path)))
            self._update_sensitivity()
            self.refresh_title()
            if on_done:
                on_done(True)
            return False
        GLib.timeout_add(100, tick)
        threading.Thread(target=work, daemon=True).start()


def np_empty():
    return np.zeros(0, np.int64)
