"""Text / source-code viewer and simple editor.

A monospace Gtk.TextView with a line-number gutter, Pygments syntax
highlighting (applied as TextTags), find, go-to-line, zoom, word wrap, undo /
redo and atomic saving that keeps the file's encoding and line endings.

Big files (> 20 MB) open read-only without highlighting.  Loading always
happens in a worker thread and the text is inserted into the buffer in chunks
from the main loop, so the window stays responsive.
"""
from __future__ import annotations

import codecs
import itertools
import os
import re
import shutil
import tempfile
import threading
import weakref

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

from . import syntax  # noqa: E402
from .i18n import _, N_, ngettext
from .viewer_base import Viewer, file_info_section  # noqa: E402
from .runtime import replace_file

LARGE_BYTES = 20 * 1024 * 1024        # above this: read-only, no highlighting
MAX_LOAD_BYTES = 256 * 1024 * 1024    # never read more than this
HIGHLIGHT_CAP = 1_500_000             # characters; longer text is not highlighted
REHIGHLIGHT_CAP = 400_000             # re-lex after edits only up to this size
INSERT_CHUNK = 256 * 1024             # characters inserted per main-loop tick
MAX_MATCHES = 10_000
DEFAULT_PT = 11.0
MIN_PT, MAX_PT = 6.0, 48.0
PROSE_EXTS = {".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".srt", ".vtt"}

_counter = itertools.count(1)

CSS = """
.tx-status { padding: 3px 12px; font-size: 0.9em; color: alpha(currentColor, 0.7); }
progressbar.tx-progress { min-height: 3px; }
progressbar.tx-progress trough, progressbar.tx-progress progress { min-height: 3px; }
"""
_css_done = False


def _install_css():
    global _css_done
    if _css_done:
        return
    disp = Gdk.Display.get_default()
    if disp is None:
        return
    prov = Gtk.CssProvider()
    prov.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(disp, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_done = True


# ---------------------------------------------------------------------------
# Decoding / encoding
# ---------------------------------------------------------------------------
class Encoding:
    """How the file's bytes map to text, so it can be written back unchanged."""

    def __init__(self, codec="utf-8", label="UTF-8", bom=b""):
        self.codec, self.label, self.bom = codec, label, bom

    def encode(self, text: str) -> bytes:
        return self.bom + text.encode(self.codec)


def decode_bytes(data: bytes) -> tuple[str, Encoding]:
    """utf-8, utf-8-sig, utf-16/32 with BOM; anything else falls back to latin-1."""
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        le = data.startswith(codecs.BOM_UTF32_LE)
        enc = Encoding("utf-32-le" if le else "utf-32-be", "UTF-32 " + ("LE" if le else "BE"),
                       data[:4])
        return data[4:].decode(enc.codec, errors="replace"), enc
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        le = data.startswith(codecs.BOM_UTF16_LE)
        enc = Encoding("utf-16-le" if le else "utf-16-be", "UTF-16 " + ("LE" if le else "BE"),
                       data[:2])
        return data[2:].decode(enc.codec, errors="replace"), enc
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="replace"), \
            Encoding("utf-8", "UTF-8 with BOM", codecs.BOM_UTF8)
    try:
        return data.decode("utf-8"), Encoding("utf-8", "UTF-8")
    except UnicodeDecodeError:
        return data.decode("latin-1"), Encoding("latin-1", "Latin-1 (ISO-8859-1)")


def split_eol(text: str) -> tuple[str, str, bool]:
    """(text with only '\\n', dominant line ending 'LF'|'CRLF'|'CR', mixed?)."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if not crlf and not cr:
        return text, "LF", False
    text = text.replace("\r\n", "\n")
    if cr:
        text = text.replace("\r", "\n")
    kinds = [(lf, "LF"), (crlf, "CRLF"), (cr, "CR")]
    return text, max(kinds)[1], sum(1 for k in kinds if k[0] > 0) > 1


EOL_CHARS = {"LF": "\n", "CRLF": "\r\n", "CR": "\r"}
EOL_NAMES = {"LF": N_("LF (Unix)"), "CRLF": N_("CRLF (Windows)"), "CR": N_("CR (classic Mac)")}


def read_text_file(gfile: Gio.File):
    """Worker-thread loader: returns a dict describing the file's text."""
    path = gfile.get_path()
    truncated = False
    if path is not None:
        with open(path, "rb") as f:
            data = f.read(MAX_LOAD_BYTES + 1)
        if len(data) > MAX_LOAD_BYTES:
            truncated = True
            data = data[:MAX_LOAD_BYTES]
            cut = data.rfind(b"\n")
            if cut > 0:
                data = data[:cut + 1]
    else:
        _ok, data, _etag = gfile.load_contents(None)
        data = bytes(data)
    size = len(data)
    text, enc = decode_bytes(data)
    del data
    text, eol, mixed = split_eol(text)
    has_nul = "\x00" in text
    if has_nul:
        text = text.replace("\x00", "�")
    return {"text": text, "enc": enc, "eol": eol, "mixed": mixed, "size": size, "truncated": truncated,
            "nul": has_nul}


def count_words(text: str, step: int = 262144) -> int:
    """str.split() over slices (it holds the GIL, so keep each call short)."""
    n, i, ln = 0, 0, len(text)
    while i < ln:
        j = min(i + step, ln)
        if j < ln:
            while j < ln and not text[j].isspace():       # don't cut a word in half
                j += 1
        n += len(text[i:j].split())
        i = j
    return n


def atomic_write(path: str, data: bytes):
    """Write next to the target and os.replace() it; keeps permissions."""
    real = os.path.realpath(path)
    directory = os.path.dirname(real) or "."
    base = os.path.basename(real)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{base}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(real):
            shutil.copymode(real, tmp)
        else:
            um = os.umask(0)
            os.umask(um)
            os.chmod(tmp, 0o666 & ~um)
        replace_file(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Line-number gutter
# ---------------------------------------------------------------------------
class LineGutter(Gtk.Widget):
    """Line numbers, drawn in the TextView's left border window."""
    __gtype_name__ = "PreviewLineGutter"

    def __init__(self, view: Gtk.TextView):
        super().__init__()
        self.view = view
        self._digits = 3
        self.add_css_class("tx-gutter")
        self.set_can_target(False)

    def set_digits(self, n: int):
        n = max(3, n)
        if n != self._digits:
            self._digits = n
            self.queue_resize()

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            layout = self.create_pango_layout("8" * self._digits)
            w = layout.get_pixel_size()[0] + 22
            return (w, w, -1, -1)
        return (0, 0, -1, -1)

    def do_snapshot(self, snapshot):
        view = self.view
        buf = view.get_buffer()
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        fg = self.get_color()
        dim = Gdk.RGBA()
        dim.red, dim.green, dim.blue, dim.alpha = fg.red, fg.green, fg.blue, 0.45 * fg.alpha
        sep = Gdk.RGBA()
        sep.red, sep.green, sep.blue, sep.alpha = fg.red, fg.green, fg.blue, 0.12 * fg.alpha
        snapshot.append_color(sep, Graphene.Rect().init(w - 1, 0, 1, h))
        rect = view.get_visible_rect()
        top = rect.y
        it, _line_top = view.get_line_at_y(top)
        layout = self.create_pango_layout("")
        layout.set_width(Pango.units_from_double(w - 12))
        layout.set_alignment(Pango.Alignment.RIGHT)
        end_line = buf.get_end_iter().get_line()
        last_empty = buf.get_end_iter().starts_line() and buf.get_char_count() > 0
        while True:
            line = it.get_line()
            if line == end_line and last_empty:
                break
            y, lh = view.get_line_yrange(it)
            wy = y - top
            if wy > h:
                break
            if wy + lh >= 0:
                layout.set_text(str(line + 1), -1)
                snapshot.save()
                snapshot.translate(Graphene.Point().init(0, wy))
                snapshot.append_layout(layout, dim)
                snapshot.restore()
            if not it.forward_line():
                break


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------
# lexers of prose (markup around text): translated paragraph by paragraph
PROSE_LEXERS = {"reStructuredText", "TeX", "Org Mode", "Groff", "BBCode", "Creole",
                "MoinMoin/Trac Wiki markup", "Markdown", "Text only"}


class TextViewer(Viewer):
    def __init__(self, window):
        super().__init__(window)
        _install_css()
        self.compact = False
        self.gfile: Gio.File | None = None
        self.readonly = False
        self.readonly_reason = ""
        self.lexer = None
        self.language = "Plain Text"
        self.encoding = Encoding()
        self.eol = "LF"
        self.eol_mixed = False
        self._loading = False
        self._load_gen = 0
        self._load_chunks = None
        self._hl_gen = 0
        self._hl_timer = 0
        self._hl_tags: dict = {}
        self._can_highlight = True
        self._edit_gen = 0
        self._ro_text: str | None = None      # immutable full text (read-only files)
        self._wc_key = None
        self._wc_val = None
        self._wc_running = None
        self._matches: list[tuple[int, int]] = []
        self._match_idx = -1
        self._match_capped = False
        self._search_timer = 0
        self._osd_timer = 0
        self._status_pending = False
        self._title_pending = False
        self._alive = True
        self._md = None                       # MarkdownPreview for .md files
        self._translation = None              # translate_view.SideBySide, when open
        self._md_mode = "source"
        self._md_stale = False
        self.md_switch = None
        self.font_pt = DEFAULT_PT
        self._cls = f"txv{next(_counter)}"
        self._css = Gtk.CssProvider()
        self._css_display = Gdk.Display.get_default()
        if self._css_display is not None:
            Gtk.StyleContext.add_provider_for_display(
                self._css_display, self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self._apply_font()

        self._build_ui()
        self._build_actions()
        self._build_shortcuts()
        sm = Adw.StyleManager.get_default()
        ref = weakref.ref(self)

        def on_dark(*_):
            v = ref()
            if v is not None and v._alive:
                v._schedule_highlight(0)
        self._dark_id = sm.connect("notify::dark", on_dark)
        self.connect("destroy", lambda *_: self.release())

    # ===================================================================
    # UI
    # ===================================================================
    def _build_ui(self):
        # --- search bar
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Find in file"), hexpand=True)
        self.match_label = Gtk.Label(label="", css_classes=["dim-label"], width_chars=11,
                                     xalign=1.0)
        prev = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Previous match (Shift+F3)"))
        nxt = Gtk.Button(icon_name="go-down-symbolic", tooltip_text=_("Next match (F3)"))
        box = Gtk.Box(spacing=6)
        for w in (self.search_entry, self.match_label, prev, nxt):
            box.append(w)
        box.set_size_request(380, -1)
        self.search_bar = Gtk.SearchBar(child=Adw.Clamp(maximum_size=640, child=box))
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.set_show_close_button(False)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_: self._goto_match(True))
        self.search_entry.connect("next-match", lambda *_: self._goto_match(True))
        self.search_entry.connect("previous-match", lambda *_: self._goto_match(False))
        self.search_entry.connect("stop-search", lambda *_: self._close_search())
        prev.connect("clicked", lambda *_: self._goto_match(False))
        nxt.connect("clicked", lambda *_: self._goto_match(True))
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)
        self.append(self.search_bar)

        self.banner = Adw.Banner(title="", revealed=False)
        self.append(self.banner)
        self.progress = Gtk.ProgressBar(css_classes=["osd", "tx-progress"], visible=False)
        self.append(self.progress)

        # --- text view
        self.buf = Gtk.TextBuffer()
        self.buf.set_enable_undo(True)
        self.view = Gtk.TextView(buffer=self.buf, monospace=True, top_margin=8, bottom_margin=8,
                                 left_margin=10, right_margin=14, editable=True,
                                 wrap_mode=Gtk.WrapMode.NONE, accepts_tab=True,
                                 hexpand=True, vexpand=True)
        self.view.add_css_class(self._cls)
        self.view.add_css_class("tx-view")
        self.gutter = LineGutter(self.view)
        self.gutter.add_css_class(self._cls)
        self.view.set_gutter(Gtk.TextWindowType.LEFT, self.gutter)

        self.scroller = Gtk.ScrolledWindow(child=self.view, hexpand=True, vexpand=True)
        self.vadj = self.scroller.get_vadjustment()
        self.vadj.connect("value-changed", lambda *_: self.gutter.queue_draw())
        self.vadj.connect("changed", lambda *_: self.gutter.queue_draw())
        self.vadj.connect("notify::upper", lambda *_: self.gutter.queue_draw())

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll)
        self.scroller.add_controller(scroll)

        self.osd = Gtk.Label(css_classes=["preview-osd"], halign=Gtk.Align.CENTER,
                             valign=Gtk.Align.END, margin_bottom=36, visible=False,
                             can_target=False)
        self.overlay = Gtk.Overlay(child=self.scroller, hexpand=True, vexpand=True)
        self.overlay.add_overlay(self.osd)
        self.append(self.overlay)

        # --- status bar
        self.status = Gtk.Label(label="", xalign=0.0, css_classes=["tx-status"],
                                ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        self.status_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.status_bar.append(Gtk.Separator())
        self.status_bar.append(self.status)
        self.append(self.status_bar)

        self.buf.connect("changed", self._on_changed)
        self.buf.connect("modified-changed", lambda *_: self._on_modified_changed())
        self.buf.connect("notify::cursor-position", lambda *_: self._queue_status())
        self.buf.connect("notify::can-undo", lambda *_: self._update_sensitivity())
        self.buf.connect("notify::can-redo", lambda *_: self._update_sensitivity())
        self.buf.connect("mark-set", lambda _b, _i, mark: self._queue_status()
                         if mark.get_name() in ("insert", "selection_bound") else None)

        self.tag_match = self.buf.create_tag("tx-match")
        self.tag_current = self.buf.create_tag("tx-current")
        self._style_search_tags()
        self._update_status()

    def _apply_font(self):
        self._css.load_from_string(
            f".{self._cls}.tx-view, .{self._cls}.tx-gutter "
            f"{{ font-size: {self.font_pt:.2f}pt; }}\n"
            f".{self._cls}.tx-gutter {{ font-family: monospace; }}\n"
            f".{self._cls}.md-preview {{ font-size: {self.font_pt * 1.05:.2f}pt; }}")

    def _style_search_tags(self):
        self.tag_match.set_property("background", "rgba(255, 200, 0, 0.35)")
        self.tag_current.set_property("background", "rgba(255, 140, 0, 0.75)")
        self.tag_current.set_property("foreground", "#000000")

    # ---- actions / shortcuts -------------------------------------------
    def _build_actions(self):
        g = Gio.SimpleActionGroup()
        self._actions = g

        def add(name, cb, state=None):
            a = Gio.SimpleAction.new(name, None) if state is None else \
                Gio.SimpleAction.new_stateful(name, None, state)
            if state is None:
                a.connect("activate", lambda _a, _p: cb())
            g.add_action(a)
            return a
        add("undo", lambda: self.buf.undo())
        add("redo", lambda: self.buf.redo())
        add("save", lambda: self.save_changes(None))
        add("save-as", lambda: self.save_as())
        add("find", lambda: self.show_search())
        add("find-next", lambda: self._goto_match(True))
        add("find-prev", lambda: self._goto_match(False))
        add("goto", lambda: self.goto_line_dialog())
        add("zoom-in", lambda: self.zoom_by(1))
        add("zoom-out", lambda: self.zoom_by(-1))
        add("zoom-reset", lambda: self.set_zoom(DEFAULT_PT))
        add("translate", lambda: self._translate(selection=True))
        add("translate-doc", lambda: self.translate_document())
        self.act_wrap = add("wrap", None, GLib.Variant("b", False))
        self.act_wrap.connect("change-state", self._on_wrap_state)
        self.insert_action_group("text", g)

        m = Gio.Menu()
        s = Gio.Menu()
        s.append(_("Wrap Lines"), "text.wrap")
        s.append(_("Go to Line…"), "text.goto")
        s.append(_("Translate Document…"), "text.translate-doc")
        m.append_section(None, s)
        # in the text's right-click menu: Translate (the selection, in a card)
        # when there is one, Translate Document… (side by side) always
        self._menu_sel = Gio.Menu()
        self._menu_sel.append(_("Translate"), "text.translate")
        self._menu_sel.append(_("Translate Document…"), "text.translate-doc")
        self._menu_nosel = Gio.Menu()
        self._menu_nosel.append(_("Translate Document…"), "text.translate-doc")
        self.view.set_extra_menu(self._menu_nosel)
        self.buf.connect("notify::has-selection", lambda b, _p: self.view.set_extra_menu(
            self._menu_sel if b.get_has_selection() else self._menu_nosel))
        s = Gio.Menu()
        s.append(_("Larger Text"), "text.zoom-in")
        s.append(_("Smaller Text"), "text.zoom-out")
        s.append(_("Actual Size"), "text.zoom-reset")
        m.append_section(None, s)
        self.menu_save = Gio.Menu()
        self.menu_save.append(_("Save As…"), "text.save-as")
        m.append_section(None, self.menu_save)
        self.menu = m

    def _build_shortcuts(self):
        sc = Gtk.ShortcutController()
        sc.set_scope(Gtk.ShortcutScope.LOCAL)
        sc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def bind(accels, action):
            for acc in accels:
                sc.add_shortcut(Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(acc),
                    Gtk.NamedAction.new(action)))
        bind(["<Control>s"], "text.save")
        bind(["<Control><Shift>s"], "text.save-as")
        bind(["<Control>f"], "text.find")
        bind(["F3"], "text.find-next")
        bind(["<Shift>F3", "<Control><Shift>g"], "text.find-prev")
        bind(["<Control>g"], "text.goto")
        bind(["<Control>plus", "<Control>equal", "<Control>KP_Add", "<Control><Shift>equal"],
             "text.zoom-in")
        bind(["<Control>minus", "<Control>KP_Subtract"], "text.zoom-out")
        bind(["<Control>0", "<Control>KP_0"], "text.zoom-reset")
        self.add_controller(sc)

    def translate_document(self):
        """The whole text translated beside the original (Markdown: rendered,
        with its structure kept)."""
        from . import translate_view
        if self._loading:
            return
        if self._md is not None:
            kind = "markdown"
        elif self.lexer is not None and self.lexer.name not in PROSE_LEXERS:
            kind = "code"                       # comments and strings only
        else:
            kind = "text"
        if kind == "markdown" and self._md_mode == "split":
            self._set_md_mode("preview", save=False)      # original | translation
        main, put = translate_view.box_slot(
            self, lambda: self.md_paned if self._md is not None else self.overlay)
        translate_view.open_for(self, kind, main, put)

    def _translate(self, selection=True):
        """Translate the selection in a card beside it (nothing selected: the
        whole document, side by side)."""
        from . import translate_ui
        sel = self.buf.get_selection_bounds() if selection else ()
        if not sel:
            self.translate_document()
            return
        start, end = sel
        translate_ui.show(self.view, self.buf.get_text(start, end, False),
                          translate_ui.textview_rect(self.view, start, end),
                          side_by_side=self.translate_document)

    def _set_action(self, name, on):
        a = self._actions.lookup_action(name)
        if a is not None:
            a.set_enabled(bool(on))

    # ---- header --------------------------------------------------------
    def build_header(self, header: Adw.HeaderBar) -> None:
        if self.compact:
            return
        self.btn_undo = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text=_("Undo (Ctrl+Z)"),
                                   action_name="text.undo")
        self.btn_redo = Gtk.Button(icon_name="edit-redo-symbolic",
                                   tooltip_text=_("Redo (Ctrl+Shift+Z)"), action_name="text.redo")
        self.btn_save = Gtk.Button(icon_name="document-save-symbolic",
                                   tooltip_text=_("Save (Ctrl+S)"), action_name="text.save")
        box = Gtk.Box(css_classes=["linked"])
        box.append(self.btn_undo)
        box.append(self.btn_redo)
        header.pack_start(box)
        header.pack_start(self.btn_save)
        self._build_md_switch(header)
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                           tooltip_text=_("Find (Ctrl+F)"))
        self.search_btn.bind_property("active", self.search_bar, "search-mode-enabled",
                                      GObject.BindingFlags.BIDIRECTIONAL |
                                      GObject.BindingFlags.SYNC_CREATE)
        header.pack_end(self.search_btn)
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text=_("Text Options"),
                                       menu_model=self.menu))
        self._update_sensitivity()

    def set_compact(self, compact: bool = True):
        """Read-only, no editing chrome (Quick Look)."""
        self.compact = bool(compact)
        if self.compact:
            self.readonly = True
            self.view.set_editable(False)
        self.status_bar.set_visible(not self.compact)
        self._update_sensitivity()

    def _update_sensitivity(self):
        ro = self.readonly or self._loading
        self._set_action("undo", not ro and self.buf.get_can_undo())
        self._set_action("redo", not ro and self.buf.get_can_redo())
        self._set_action("save", not ro and self.buf.get_modified())
        self._set_action("save-as", not self._loading)

    # ===================================================================
    # Viewer API
    # ===================================================================
    def get_current_file(self):
        return self.gfile

    def get_title(self) -> str:
        name = (self.gfile.get_basename() if self.gfile is not None else None) or _("Text")
        if self.buf.get_modified() and not self.readonly:
            name += _(" — Edited")
        return name

    def _line_count(self) -> int:
        end = self.buf.get_end_iter()
        return end.get_line() + (0 if end.starts_line() else 1)

    def get_subtitle(self) -> str:
        if self._loading and self.buf.get_char_count() == 0:
            return _("Loading…")
        n = self._line_count()
        s = _("{language} · {lines} · {encoding}").format(
            language=self.language,
            lines=ngettext("{n:,} line", "{n:,} lines", n).format(n=n),
            encoding=self.encoding.label)
        if self._loading:
            s = _("Loading… ") + s
        return s

    def has_unsaved_changes(self) -> bool:
        return (not self.readonly) and self.buf.get_modified()

    def release(self):
        if not self._alive:
            return
        self._alive = False
        self._load_gen += 1
        self._hl_gen += 1
        if self._translation is not None:
            from . import translate_view
            translate_view.shutdown_for(self)
        if self._md is not None:
            self._md.release()
        for attr in ("_hl_timer", "_search_timer", "_osd_timer"):
            src = getattr(self, attr)
            if src:
                GLib.source_remove(src)
                setattr(self, attr, 0)
        try:
            Adw.StyleManager.get_default().disconnect(self._dark_id)
        except Exception:  # noqa: BLE001
            pass
        if self._css_display is not None:
            try:
                Gtk.StyleContext.remove_provider_for_display(self._css_display, self._css)
            except Exception:  # noqa: BLE001
                pass

    def get_info(self):
        sections = [file_info_section(self.gfile)] if self.gfile is not None else []
        n = self._line_count()
        words = self._words()
        rows = [(_("Language"), self.language),
                (_("Lines"), f"{n:,}" + ("…" if self._loading else "")),
                (_("Words"), "…" if words is None else f"{words:,}"),
                (_("Characters"), f"{self.buf.get_char_count():,}"),
                (_("Encoding"), self.encoding.label),
                (_("Line endings"), _(EOL_NAMES[self.eol]) + (_(" (mixed; saved as one style)")
                                                 if self.eol_mixed else ""))]
        if self.readonly_reason:
            rows.append((_("Mode"), _("Read-only — {reason}").format(reason=self.readonly_reason)))
        if self.has_unsaved_changes():
            rows.append((_("Status"), _("Edited (unsaved)")))
        sections.append((_("Text"), rows))
        return sections

    # ===================================================================
    # Loading
    # ===================================================================
    def load(self, files):
        gf = files[0]
        self.gfile = gf
        name = gf.get_basename() or ""
        ext = name[name.rfind("."):].lower() if "." in name else ""
        self.act_wrap.set_state(GLib.Variant("b", ext in PROSE_EXTS))
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if ext in PROSE_EXTS
                                else Gtk.WrapMode.NONE)
        if ext in (".md", ".markdown") and not self.compact:
            self._setup_markdown()
        self.language = "Plain Text"
        self._loading = True
        self.view.set_editable(False)
        self.progress.set_visible(True)
        self.progress.pulse()
        self._load_gen += 1
        gen = self._load_gen
        self._update_sensitivity()

        def work():
            try:
                res = read_text_file(gf)
                err = None
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                res, err = None, e
            GLib.idle_add(self._on_read, gen, res, err)
        threading.Thread(target=work, name="text-load", daemon=True).start()
        self.refresh_title()

    def _on_read(self, gen, res, err):
        if gen != self._load_gen or not self._alive:
            return False
        if err is not None:
            self._loading = False
            self.progress.set_visible(False)
            self.window._show_error(err)
            return False
        text = res["text"]
        self.encoding, self.eol, self.eol_mixed = res["enc"], res["eol"], res["mixed"]
        large = res["size"] > LARGE_BYTES
        reasons = []
        if large:
            reasons.append(_("large file"))
        if res["truncated"]:
            reasons.append(_("showing only the first {mb} MB").format(
                    mb=MAX_LOAD_BYTES // 1048576))
        if res["nul"]:
            reasons.append(_("contains NUL bytes"))
        if self.compact:
            self.readonly = True
        if reasons:
            self.readonly = True
            self.readonly_reason = ", ".join(reasons)
            mb = res["size"] / 1_000_000
            if res["truncated"]:
                msg = _("This file is very large; showing only the first {mb} MB, read-only.").format(
                    mb=MAX_LOAD_BYTES // 1048576)
            elif large:
                msg = _("This file is large ({mb:.1f} MB). It is open read-only "
                        "and syntax highlighting is off.").format(mb=mb)
            else:
                msg = _("This file contains NUL bytes, so it is open read-only.")
            self.banner.set_title(msg)
            self.banner.set_revealed(True)
        self._can_highlight = not large and not res["truncated"]
        name = (self.gfile.get_basename() if self.gfile else "") or ""
        ct = None
        try:
            ct = self.gfile.query_info("standard::content-type", Gio.FileQueryInfoFlags.NONE,
                                       None).get_content_type()
        except GLib.Error:
            pass
        if self._can_highlight and len(text) <= HIGHLIGHT_CAP:
            self.lexer = syntax.lexer_for(name, ct, text[:200])
            self.language = self.lexer.name if self.lexer is not None else "Plain Text"
        else:
            self.lexer = None
            ext = name[name.rfind("."):].lower() if "." in name else ""
            self.language = {".log": "Log", ".md": "Markdown", ".txt": "Plain Text"}.get(
                ext, "Plain Text")
        if self.readonly:
            self._ro_text = text
        self.view.set_editable(not self.readonly)
        if self._md is not None:
            self._md_source(text)
        # fill the buffer in chunks
        self.buf.set_enable_undo(False)
        self._load_text = text
        self._load_pos = 0
        self._first_chunk = True
        self.progress.set_fraction(0.0)
        self._pump_load(gen)
        return False

    def _pump_load(self, gen):
        if gen != self._load_gen or not self._alive:
            return False
        text, pos = self._load_text, self._load_pos
        n = len(text)
        end = min(pos + INSERT_CHUNK, n)
        if end < n:
            k = text.rfind("\n", pos, end)
            if k > pos:
                end = k + 1
        chunk = text[pos:end]
        if chunk:
            if self._first_chunk:
                self.buf.set_text(chunk, -1)
                self._first_chunk = False
            else:
                self.buf.insert(self.buf.get_end_iter(), chunk, -1)
        self._load_pos = end
        self.progress.set_fraction(end / n if n else 1.0)
        self._queue_title()
        self.gutter.set_digits(len(str(self._line_count())))
        if end < n:
            GLib.idle_add(self._pump_load, gen, priority=GLib.PRIORITY_LOW)
        else:
            self._finish_load()
        return False

    def _finish_load(self):
        self._load_text = None
        self._loading = False
        self.progress.set_visible(False)
        self.buf.set_enable_undo(True)
        self.buf.set_modified(False)
        self.buf.place_cursor(self.buf.get_start_iter())
        self.vadj.set_value(0)
        self._update_sensitivity()
        self._update_status()
        self.refresh_title()
        if not self.compact:
            focus = self._md.view if self._md is not None and self._md_mode == "preview" \
                else self.view
            GLib.idle_add(lambda: (focus.grab_focus(), False)[1])
        self._schedule_highlight(0)
        if self.search_bar.get_search_mode() and self.search_entry.get_text():
            self._run_search()

    # ===================================================================
    # Syntax highlighting
    # ===================================================================
    def _dark(self):
        return Adw.StyleManager.get_default().get_dark()

    def _current_text(self) -> str:
        if self._ro_text is not None:
            return self._ro_text
        return self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), True)

    def _schedule_highlight(self, delay_ms=400):
        if not self._alive or not self._can_highlight or self._loading:
            return
        self._hl_gen += 1
        if self._hl_timer:
            GLib.source_remove(self._hl_timer)
            self._hl_timer = 0
        if delay_ms <= 0:
            self._start_highlight()
        else:
            self._hl_timer = GLib.timeout_add(delay_ms, self._hl_timer_fire)

    def _hl_timer_fire(self):
        self._hl_timer = 0
        self._start_highlight()
        return False

    def _clear_syntax_tags(self):
        s, e = self.buf.get_bounds()
        for tag in self._hl_tags.values():
            self.buf.remove_tag(tag, s, e)

    def _start_highlight(self):
        if not self._alive or self._loading:
            return
        gen = self._hl_gen
        lexer = self.lexer
        n = self.buf.get_char_count()
        if lexer is None or n == 0 or n > HIGHLIGHT_CAP:
            self._clear_syntax_tags()
            return
        if self._edit_gen and n > REHIGHLIGHT_CAP:
            return                          # edited a big file: keep the old tags
        text = self._current_text()
        dark = self._dark()

        def work():
            try:
                runs = syntax.lex_runs(text, lexer)
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                runs = None
            GLib.idle_add(self._apply_runs, gen, runs, dark)
        threading.Thread(target=work, name="text-lex", daemon=True).start()

    def _apply_runs(self, gen, runs, dark):
        if gen != self._hl_gen or not self._alive or runs is None:
            return False
        self._clear_syntax_tags()
        table = self.buf.get_tag_table()
        tags = {}
        for ttype in {r[0] for r in runs}:
            st = syntax.style_for(ttype, dark)
            if st is None:
                continue
            key = "tx-" + str(ttype)
            tag = self._hl_tags.get(key) or table.lookup(key)
            if tag is None:
                tag = self.buf.create_tag(key)
                # syntax below search highlights in priority
                tag.set_priority(0)
            self._hl_tags[key] = tag
            tag.set_property("foreground-set", bool(st.get("color")))
            if st.get("color"):
                tag.set_property("foreground", "#" + st["color"])
            tag.set_property("weight", Pango.Weight.BOLD if st.get("bold")
                             else Pango.Weight.NORMAL)
            tag.set_property("style", Pango.Style.ITALIC if st.get("italic")
                             else Pango.Style.NORMAL)
            tags[ttype] = tag
        todo = [(tags[t], a, b) for t, a, b in runs if t in tags]
        buf = self.buf
        it = iter(todo)

        def chunk():
            if gen != self._hl_gen or not self._alive:
                return False
            s, e = buf.get_start_iter(), buf.get_start_iter()
            count = 0
            for tag, a, b in it:
                s.set_offset(a)
                e.set_offset(b)
                buf.apply_tag(tag, s, e)
                count += 1
                if count >= 3000:
                    return True
            return False
        if chunk():
            GLib.idle_add(chunk, priority=GLib.PRIORITY_LOW)
        return False

    # ===================================================================
    # Buffer events / status
    # ===================================================================
    def _on_changed(self, *_):
        if self._loading:
            return
        self._edit_gen += 1
        self._queue_title()
        self._queue_status()
        self.gutter.set_digits(len(str(self._line_count())))
        self.gutter.queue_draw()
        self._schedule_highlight(500)
        if self.search_bar.get_search_mode() and self.search_entry.get_text():
            self._schedule_search()
        self._update_sensitivity()
        if self._md is not None:
            self._md_edited()

    def _on_modified_changed(self):
        self._update_sensitivity()
        self._queue_title()

    def _queue_title(self):
        if self._title_pending:
            return
        self._title_pending = True

        def go():
            self._title_pending = False
            if self._alive:
                self.refresh_title()
            return False
        GLib.timeout_add(120, go)

    def _queue_status(self):
        if self._status_pending:
            return
        self._status_pending = True

        def go():
            self._status_pending = False
            if self._alive:
                self._update_status()
            return False
        GLib.idle_add(go)

    def _update_status(self):
        buf = self.buf
        it = buf.get_iter_at_mark(buf.get_insert())
        txt = _("Ln {line:,}, Col {col:,}").format(line=it.get_line() + 1,
                                                  col=it.get_line_offset() + 1)
        if buf.get_has_selection():
            a, b = buf.get_selection_bounds()
            txt += _("  ·  {n:,} selected").format(n=abs(b.get_offset() - a.get_offset()))
        txt += f"  ·  {self.language}  ·  {self.encoding.label}  ·  {self.eol}"
        if self.readonly:
            txt += _("  ·  Read-only")
        self.status.set_text(txt)

    def _words(self):
        n = self.buf.get_char_count()
        if n <= 2_000_000:
            return len(self._current_text().split())
        key = (self._edit_gen, self._loading)
        if self._wc_key == key:
            return self._wc_val
        if self._wc_running != key and not self._loading:
            self._wc_running = key
            text = self._current_text()

            def work():
                v = count_words(text)
                GLib.idle_add(done, v)

            def done(v):
                if self._alive:
                    self._wc_key, self._wc_val = key, v
                    self.refresh_title()
                return False
            threading.Thread(target=work, name="text-words", daemon=True).start()
        return None

    # ===================================================================
    # Wrap / zoom
    # ===================================================================
    def _on_wrap_state(self, action, value):
        action.set_state(value)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if value.get_boolean()
                                else Gtk.WrapMode.NONE)
        self.gutter.queue_draw()

    def set_zoom(self, pt):
        self.font_pt = max(MIN_PT, min(MAX_PT, pt))
        self._apply_font()
        self.gutter.queue_resize()
        self.gutter.queue_draw()
        self._osd(f"{round(self.font_pt / DEFAULT_PT * 100)}%")

    def zoom_by(self, steps):
        self.set_zoom(self.font_pt * (1.1 ** steps))

    def _on_scroll(self, ctl, _dx, dy):
        state = ctl.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            self.zoom_by(-1 if dy > 0 else 1)
            return True
        return False

    def _osd(self, text):
        self.osd.set_text(text)
        self.osd.set_visible(True)
        if self._osd_timer:
            GLib.source_remove(self._osd_timer)

        def hide():
            self._osd_timer = 0
            self.osd.set_visible(False)
            return False
        self._osd_timer = GLib.timeout_add(1100, hide)

    # ===================================================================
    # Find
    # ===================================================================
    def show_search(self):
        if self._md is not None and self._md_mode == "preview":
            self._set_md_mode("split", save=False)      # find works in the source
        self.search_bar.set_search_mode(True)
        self.search_entry.grab_focus()
        sel = self.buf.get_selection_bounds()
        if sel:
            s = self.buf.get_text(sel[0], sel[1], False)
            if s and "\n" not in s and len(s) < 200:
                self.search_entry.set_text(s)
        self.search_entry.select_region(0, -1)

    def _close_search(self):
        self.search_bar.set_search_mode(False)
        self.view.grab_focus()

    def _on_search_mode(self, *_):
        if not self.search_bar.get_search_mode():
            self._clear_matches()
            self.match_label.set_text("")
        elif self.search_entry.get_text():
            self._run_search()

    def _on_search_changed(self, *_):
        self._schedule_search()

    def _schedule_search(self):
        if self._search_timer:
            GLib.source_remove(self._search_timer)

        def go():
            self._search_timer = 0
            self._run_search()
            return False
        self._search_timer = GLib.timeout_add(150, go)

    def _clear_matches(self):
        s, e = self.buf.get_bounds()
        self.buf.remove_tag(self.tag_match, s, e)
        self.buf.remove_tag(self.tag_current, s, e)
        self._matches = []
        self._match_idx = -1

    def _run_search(self, jump=True):
        self._clear_matches()
        q = self.search_entry.get_text()
        if not q or self._loading:
            self.match_label.set_text("")
            return
        text = self._current_text()
        pat = re.compile(re.escape(q), re.IGNORECASE)
        found = [(m.start(), m.end()) for m in itertools.islice(pat.finditer(text), MAX_MATCHES + 1)]
        self._match_capped = len(found) > MAX_MATCHES
        self._matches = found[:MAX_MATCHES]
        buf = self.buf
        s, e = buf.get_start_iter(), buf.get_start_iter()
        for a, b in self._matches:
            s.set_offset(a)
            e.set_offset(b)
            buf.apply_tag(self.tag_match, s, e)
        if not self._matches:
            self.match_label.set_text(_("No matches"))
            return
        # first match at or after the cursor
        cur = buf.get_iter_at_mark(buf.get_insert()).get_offset()
        idx = next((i for i, (a, _b) in enumerate(self._matches) if a >= cur), 0)
        self._select_match(idx, jump)

    def _select_match(self, idx, scroll=True):
        if not self._matches:
            return
        buf = self.buf
        s, e = buf.get_bounds()
        buf.remove_tag(self.tag_current, s, e)
        self._match_idx = idx % len(self._matches)
        a, b = self._matches[self._match_idx]
        sa, sb = buf.get_iter_at_offset(a), buf.get_iter_at_offset(b)
        buf.apply_tag(self.tag_current, sa, sb)
        if scroll:
            buf.select_range(sa, sb)
            self.view.scroll_to_iter(sa, 0.1, True, 0.0, 0.4)
        total = f"{len(self._matches):,}" + ("+" if self._match_capped else "")
        self.match_label.set_text(f"{self._match_idx + 1:,} of {total}")

    def _goto_match(self, forward=True):
        if not self.search_bar.get_search_mode():
            self.show_search()
            return
        if not self._matches:
            self._run_search()
            return
        self._select_match(self._match_idx + (1 if forward else -1))

    # ===================================================================
    # Go to line
    # ===================================================================
    def goto_line_dialog(self):
        n = max(1, self._line_count())
        dlg = Adw.AlertDialog(heading=_("Go to Line"),
                              body=_("Enter a line number from 1 to {n:,} (or line:column).").format(n=n))
        entry = Gtk.Entry(input_purpose=Gtk.InputPurpose.DIGITS, activates_default=True,
                          placeholder_text=_("Line"))
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("go", _("Go"))
        dlg.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("go")
        dlg.set_close_response("cancel")

        def response(_d, rid):
            if rid == "go":
                self.goto_line(entry.get_text())
        dlg.connect("response", response)
        dlg.present(self.get_root())
        entry.grab_focus()

    def goto_line(self, spec):
        m = re.fullmatch(r"\s*(\d+)\s*(?:[:,]\s*(\d+))?\s*", spec or "")
        if not m:
            self.toast(_("Enter a line number"))
            return
        line = max(1, min(int(m.group(1)), max(1, self._line_count())))
        col = int(m.group(2)) if m.group(2) else 1
        ok, it = self.buf.get_iter_at_line(line - 1)
        if not ok:
            it = self.buf.get_end_iter()
        if col > 1:
            it.forward_chars(min(col - 1, it.get_chars_in_line() - 1))
        self.buf.place_cursor(it)
        self.view.scroll_to_iter(it, 0.1, True, 0.0, 0.4)
        self.view.grab_focus()

    # ===================================================================
    # Saving
    # ===================================================================
    def _encode(self, text):
        if EOL_CHARS[self.eol] != "\n":
            text = text.replace("\n", EOL_CHARS[self.eol])
        try:
            return self.encoding.encode(text)
        except UnicodeEncodeError:
            self.encoding = Encoding("utf-8", "UTF-8")
            self.toast(_("Some characters don’t exist in the original encoding; saved as UTF-8"))
            return self.encoding.encode(text)

    def _write(self, path):
        text = self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), True)
        atomic_write(path, self._encode(text))

    def save_changes(self, on_done=None):
        """Ctrl+S / close prompt: save to the current file; on_done(ok)."""
        if self.readonly or self._loading:
            if self.readonly:
                self.toast(_("This file is open read-only"))
            if on_done:
                on_done(False)
            return
        path = self.gfile.get_path() if self.gfile is not None else None
        if path is None:
            self.save_as(on_done)
            return
        try:
            self._write(path)
        except OSError as e:
            self.toast(_("Could not save: {v}").format(v=e.strerror or e))
            if on_done:
                on_done(False)
            return
        self.buf.set_modified(False)
        self.toast(_("Saved “{v}”").format(v=os.path.basename(path)))
        self.refresh_title()
        self._update_status()
        if on_done:
            on_done(True)

    def save_as(self, on_done=None):
        if self._loading:
            return
        dlg = Gtk.FileDialog(title=_("Save As"))
        if self.gfile is not None:
            dlg.set_initial_name(self.gfile.get_basename())
            if self.gfile.get_parent() is not None:
                dlg.set_initial_folder(self.gfile.get_parent())
        else:
            dlg.set_initial_name(_("Untitled.txt"))

        def done(d, res):
            try:
                gf = d.save_finish(res)
            except GLib.Error:
                if on_done:
                    on_done(False)
                return
            path = gf.get_path()
            if path is None:
                self.toast(_("Only local files are supported"))
                if on_done:
                    on_done(False)
                return
            try:
                self._write(path)
            except OSError as e:
                self.toast(_("Could not save: {v}").format(v=e.strerror or e))
                if on_done:
                    on_done(False)
                return
            self.gfile = gf
            self.buf.set_modified(False)
            name = gf.get_basename() or ""
            self.lexer = syntax.lexer_for(name, None, None) if self._can_highlight else None
            self.language = self.lexer.name if self.lexer is not None else "Plain Text"
            self._schedule_highlight(0)
            try:
                self.window.get_application().add_recent(gf)
            except Exception:  # noqa: BLE001
                pass
            self.toast(_("Saved “{name}”").format(name=name))
            self.refresh_title()
            self._update_status()
            if on_done:
                on_done(True)
        dlg.save(self.get_root(), None, done)

    # ===================================================================
    # Markdown: Source / Split / Preview
    # ===================================================================
    def _build_md_switch(self, header):
        """Hidden until load() finds a Markdown file."""
        self.md_switch = Adw.ToggleGroup(visible=False, valign=Gtk.Align.CENTER,
                                         css_classes=["round"])
        for name, label, tip in (("source", _("Source"), _("Markdown source")),
                                 ("split", _("Split"), _("Source and preview side by side")),
                                 ("preview", _("Preview"), _("Rendered Markdown"))):
            self.md_switch.add(Adw.Toggle(name=name, label=label, tooltip=tip))
        self.md_switch.connect("notify::active-name", lambda g, _p: self._set_md_mode(
            g.get_active_name(), save=True) if g.get_active_name() != self._md_mode else None)
        header.pack_start(self.md_switch)

    def _setup_markdown(self):
        from . import markdown_view
        if self._md is None:
            self._md = markdown_view.MarkdownPreview(self._cls)
            self._md.on_open_file = lambda gf: self.window.open_files([gf])
            self._md.on_translate_document = self.translate_document
            scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
            scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            scroll.connect("scroll", self._on_scroll)
            self._md.add_controller(scroll)
            self.remove(self.overlay)
            self.md_paned = Gtk.Paned(start_child=self.overlay, end_child=self._md,
                                      wide_handle=True, hexpand=True, vexpand=True,
                                      resize_start_child=True, resize_end_child=True,
                                      shrink_start_child=False, shrink_end_child=False)
            self.insert_child_after(self.md_paned, self.progress)
        parent = self.gfile.get_parent() if self.gfile is not None else None
        self._md.base_dir = parent.get_path() if parent is not None else None
        if self.md_switch is not None:
            self.md_switch.set_visible(True)
        self._set_md_mode(markdown_view.saved_mode(), save=False)

    def _set_md_mode(self, mode, save=True):
        if self._md is None:
            return
        self._md_mode = mode
        if self.md_switch is not None and self.md_switch.get_active_name() != mode:
            self.md_switch.set_active_name(mode)
        self.overlay.set_visible(mode != "preview")
        self._md.set_visible(mode != "source")
        self.status_bar.set_visible(not self.compact and mode != "preview")
        if mode == "split":
            def halve():
                w = self.md_paned.get_width()
                if w > 0:
                    self.md_paned.set_position(w // 2)
                return w <= 0 and self._alive         # not allocated yet: try again
            if halve():
                GLib.timeout_add(50, halve)
        if mode != "source" and self._md_stale and not self._loading:
            self._md_stale = False
            self._md.set_source(self._current_text())
        if save:
            from . import markdown_view
            markdown_view.save_mode(mode)
            (self._md.view if mode == "preview" else self.view).grab_focus()
        if self._translation is not None:
            self._translation.restart_sync()          # the original side changed

    def _md_source(self, text):
        from .markdown_view import PREVIEW_CAP
        if len(text) > PREVIEW_CAP:              # too big to render: source only
            self._set_md_mode("source", save=False)
            if self.md_switch is not None:
                self.md_switch.set_visible(False)
            self._md.release()
            self._md = None
            return
        self._md.set_source(text)

    def _md_edited(self):
        if self._md_mode == "source":
            self._md_stale = True           # rendered when the preview is shown again
        else:
            self._md.set_source(self._current_text(), delay_ms=350)
