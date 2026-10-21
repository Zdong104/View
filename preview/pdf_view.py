"""PDF viewer: continuous / two-page / single-page views, thumbnails (several
documents in one window), page editing, Markup (shapes, ink, text, notes,
highlights, signatures), forms, text selection, search, OCR, export.

Rendering lives in pdf_render (worker threads), layout and gestures in
pdf_canvas, the edit model and the save pipeline in pdf_ops, markup in markup.
All per-page geometry is in the "display frame" described in pdf_ops.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from datetime import date

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Poppler", "0.18")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Poppler

from . import markup, pdf_ops
from .i18n import _, N_, ngettext
from .markup import Item, MarkupController, MarkupToolbar, TEXTMARK_KINDS
from .pdf_canvas import MARGIN, PageCanvas
from .pdf_ops import PageRef, Source, poppler_open, rotate_point
from .pdf_render import TILE, TILE_ABOVE, Job, RenderWorker, TexCache
from .viewer_base import Viewer, file_info_section, kind_of

try:
    from . import signature as _sig
except Exception:  # pragma: no cover
    _sig = None

ACTUAL = 96 / 72          # zoom at which a page shows at its physical size
THUMB_W = 112
MAIN_CACHE = 160 * 1024 * 1024
THUMB_CACHE = 40 * 1024 * 1024
RENDER_THREADS = max(1, min(3, (os.cpu_count() or 2) - 1))
DRAG_PREFIX = "preview-pdf-drag:"
_DRAG = {}                # token -> drag info, for drags between our windows

CSS = """
.pdf-canvas { background-color: color-mix(in srgb, var(--window-bg-color) 88%, black); }
.pdf-thumb picture { background-color: white; box-shadow: 0 1px 3px rgba(0,0,0,.35); }
.pdf-thumb.current picture { outline: 3px solid alpha(var(--accent-bg-color), .55); outline-offset: 2px; }
.pdf-thumb label.pageno { font-size: smaller; }
.pdf-thumb.drop-before { box-shadow: inset 0 3px var(--accent-bg-color); }
.pdf-thumb.drop-after { box-shadow: inset 0 -3px var(--accent-bg-color); }
.pdf-doc-header { padding: 4px 2px; font-weight: bold; }
.pdf-doc-header.drop-after { box-shadow: inset 0 -3px var(--accent-bg-color); }
.pdf-markup-bar { border-bottom: 1px solid var(--border-color); background: var(--headerbar-bg-color); }
"""
_css_done = False


def _install_css():
    global _css_done
    if _css_done or Gdk.Display.get_default() is None:
        return
    prov = Gtk.CssProvider()
    prov.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_done = True


def R(x, y, w, h):
    r = Graphene.Rect()
    r.init(x, y, w, h)
    return r


def RR(rect, radius):
    # init_from_rect() returns a pointer into the struct: keep the struct alive
    rr = Gsk.RoundedRect()
    rr.init_from_rect(rect, radius)
    return rr


def _rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def _is_encrypted_error(e) -> bool:
    try:
        return e.domain == GLib.quark_to_string(Poppler.error_quark()) and \
            e.code == Poppler.Error.ENCRYPTED
    except Exception:
        return "encrypt" in str(e).lower()


def _forms_fp(forms):
    return hash(tuple(sorted(forms.items()))) & 0xFFFFFFFF if forms else 0


def _pk(ref):
    return (ref.src, ref.index, ref.rotation, _forms_fp(ref.forms))


def _human(n):
    for u in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or u == "GB":
            return f"{n} bytes" if u == "bytes" else f"{n:.1f} {u}"
        n /= 1000


def _ocr_module():
    try:
        from . import ocr
        return ocr if ocr.is_available() else None
    except Exception:
        return None


def _ocr_offerable():
    """OCR is missing but View can offer to download it."""
    try:
        from . import ocr_install
        return ocr_install.can_install()[0]
    except Exception:  # noqa: BLE001
        return False


class Doc:
    """One opened PDF inside the viewer."""

    def __init__(self, name, src, path, gfile=None):
        self.name, self.src, self.path, self.gfile = name, src, path, gfile
        self.pages: list[PageRef] = []
        self.clean_key = ()
        self.expanded = True

    def key(self):
        return tuple(r.key() for r in self.pages)

    def dirty(self):
        return self.key() != self.clean_key


class RowItem(GObject.Object):
    """A sidebar row: a document header (pos == -1) or a page."""

    def __init__(self, di, pos):
        super().__init__()
        self.di, self.pos = di, pos


# ====================================================================== viewer

class PdfViewer(Viewer):
    SUPPORTS_MULTIPLE = True

    def __init__(self, window):
        super().__init__(window)
        _install_css()
        self.sources: list[Source] = []
        self.docs: list[Doc] = []
        self.active = 0
        self.sizes: list[tuple] = []
        self._base = {}                 # (src, index) -> (w, h) Poppler size
        self._pdocs = {}                # main-thread Poppler docs per source
        self._readers = {}              # pypdf readers (main thread, lazy)
        self.current = 0
        self._gen = 0
        self._undo, self._redo = [], []
        self._busy = False
        self._loaded = False
        self.compact = False
        self._bound = defaultdict(set)  # pk -> bound thumbnail boxes
        self._boxes = set()
        self._render_idle = 0
        self._search = None
        self.text_sel = None            # {"pos", "p0", "p1", "rects", "text"}
        self._press = None
        self._fields = {}               # (src, index) -> [field dicts]
        self._has_text = {}             # (src, index) -> bool
        self.ocr_lines = {}             # (src, index) -> [(poly, text)] Poppler frame
        self._ocr_running = set()
        self._explicit_sel = False
        self._open_queue = []
        self._released = False
        self.stats = {"first_page": None, "renders": [], "t_load": None}
        self.cache = TexCache(MAIN_CACHE)
        self.thumbs = TexCache(THUMB_CACHE)
        self.worker = RenderWorker(self._deliver, "pdf-render", RENDER_THREADS)
        self.tworker = RenderWorker(self._deliver, "pdf-thumbs")
        self.mk = MarkupController(self)
        self.mk.connect("tool-changed", lambda *_: self.canvas.queue_draw())
        self.connect("destroy", lambda *_: self._shutdown())
        self._build()
        self._build_actions()

    def _shutdown(self):
        self.worker.stop()
        self.tworker.stop()

    def release(self):
        """Free everything (Quick Look calls this when it switches files):
        stop the render threads, drop textures and Poppler documents."""
        self._released = True
        self._gen += 1
        if self.mk.pad is not None:
            self.mk.pad.stop()
        self._search_reset()
        self._shutdown()
        if self._render_idle:
            GLib.source_remove(self._render_idle)
            self._render_idle = 0
        self.cache.clear()
        self.thumbs.clear()
        self._pdocs.clear()
        for r in self._readers.values():
            fh = getattr(r, "_preview_fh", None)
            if fh is not None:
                fh.close()
        self._readers.clear()
        self._fields.clear()
        self.ocr_lines.clear()
        self._loaded = False
        self.canvas.queue_draw()

    def do_unroot(self):
        if self.mk.pad is not None:
            self.mk.pad.stop()
        Gtk.Widget.do_unroot(self)

    # ---- pages of the active document --------------------------------------
    @property
    def pages(self):
        return self.docs[self.active].pages if self.docs else []

    @pages.setter
    def pages(self, value):
        self.docs[self.active].pages = value

    @property
    def doc(self):
        return self.docs[self.active] if self.docs else None

    # ================================================================== UI ====
    def _build(self):
        self.stack = Gtk.Stack()
        self.append(self.stack)
        self.status = Adw.StatusPage(icon_name="x-office-document-symbolic")
        self.stack.add_named(self.status, "status")
        loading = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        loading.append(Adw.Spinner(width_request=32, height_request=32))
        self.stack.add_named(loading, "loading")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack.add_named(box, "doc")
        self.stack.set_visible_child_name("status")

        # markup toolbar row (Ctrl+Shift+A)
        self.mk_bar = MarkupToolbar(self.mk, text_tools=True)
        self.mk_bar.pad_btn.connect("toggled", self._on_pad_toggled)
        self.mk_revealer = Gtk.Revealer(child=self.mk_bar, reveal_child=False,
                                        css_classes=["pdf-markup-bar"])
        box.append(self.mk_revealer)

        # search bar
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search document"), hexpand=True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_: self._search_step(1))
        self.search_entry.connect("next-match", lambda *_: self._search_step(1))
        self.search_entry.connect("previous-match", lambda *_: self._search_step(-1))
        self.search_entry.connect("stop-search", lambda *_: self.search_bar.set_search_mode(False))
        sbox = Gtk.Box(spacing=6)
        sbox.append(self.search_entry)
        nav = Gtk.Box(css_classes=["linked"])
        for icon, tip, d in (("go-up-symbolic", _("Previous Match"), -1),
                             ("go-down-symbolic", _("Next Match"), 1)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", lambda _b, d=d: self._search_step(d))
            nav.append(b)
        sbox.append(nav)
        self.search_label = Gtk.Label(css_classes=["dim-label", "numeric"], width_chars=10)
        sbox.append(self.search_label)
        self.search_bar = Gtk.SearchBar(child=Adw.Clamp(maximum_size=560, child=sbox),
                                        show_close_button=True)
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)
        box.append(self.search_bar)

        # canvas
        self.canvas = PageCanvas(self)
        self.scroller = Gtk.ScrolledWindow(child=self.canvas, hexpand=True, vexpand=True)
        drop = Gtk.DropTarget(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drop.set_gtypes([GObject.TYPE_STRING])
        drop.connect("drop", self._on_canvas_drop)
        self.canvas.add_controller(drop)

        # sidebar: thumbnails / contents / annotations
        self.store = Gio.ListStore(item_type=RowItem)
        self.sel = Gtk.MultiSelection(model=self.store)
        self.sel.connect("selection-changed", self._on_sel_changed)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._row_setup)
        factory.connect("bind", self._row_bind)
        factory.connect("unbind", self._row_unbind)
        self.thumb_list = Gtk.ListView(model=self.sel, factory=factory,
                                       css_classes=["navigation-sidebar"])
        self.thumb_scroller = Gtk.ScrolledWindow(child=self.thumb_list, vexpand=True,
                                                 hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.thumb_scroller.get_vadjustment().connect(
            "value-changed", lambda *_: self._queue_renders())
        self.toc_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.toc_list.connect("row-activated", self._on_toc_row)
        self.annot_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.annot_list.connect("row-activated", self._on_annot_row)
        self.side_stack = Gtk.Stack(vexpand=True)
        self.side_stack.add_named(self.thumb_scroller, "thumbs")
        self.side_stack.add_named(Gtk.ScrolledWindow(child=self.toc_list,
                                                     hscrollbar_policy=Gtk.PolicyType.NEVER),
                                  "toc")
        self.side_stack.add_named(Gtk.ScrolledWindow(child=self.annot_list,
                                                     hscrollbar_policy=Gtk.PolicyType.NEVER),
                                  "annots")
        self.side_toggle = Adw.ToggleGroup(css_classes=["flat"], halign=Gtk.Align.CENTER,
                                           margin_top=6, margin_bottom=6)
        for name, icon, tip in (("thumbs", "view-grid-symbolic", _("Thumbnails")),
                                ("toc", "view-list-symbolic", _("Table of Contents")),
                                ("annots", "chat-symbolic", _("Highlights and Notes"))):
            self.side_toggle.add(Adw.Toggle(name=name, icon_name=icon, tooltip=tip))
        self.side_toggle.set_active_name("thumbs")
        self.side_toggle.connect("notify::active-name", self._on_side_view)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.append(self.side_toggle)
        side.append(self.side_stack)
        self.split = Adw.OverlaySplitView(content=self.scroller, sidebar=side,
                                          min_sidebar_width=150, max_sidebar_width=200,
                                          sidebar_width_fraction=0.18, show_sidebar=False,
                                          vexpand=True)
        self.split.connect("notify::show-sidebar", lambda *_: self._queue_renders())
        box.append(self.split)

    def build_header(self, header: Adw.HeaderBar) -> None:
        self._header = header
        self.sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                            tooltip_text=_("Sidebar (F9)"))
        self.split.bind_property("show-sidebar", self.sidebar_btn, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL |
                                 GObject.BindingFlags.SYNC_CREATE)
        header.pack_start(self.sidebar_btn)
        pbox = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.page_entry = Gtk.Entry(width_chars=3, max_width_chars=4, xalign=1,
                                    input_purpose=Gtk.InputPurpose.DIGITS,
                                    tooltip_text=_("Go to Page (Ctrl+G)"))
        self.page_entry.connect("activate", self._on_page_entry)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._sync_page_entry())
        self.page_entry.add_controller(focus)
        self.page_total = Gtk.Label(css_classes=["dim-label", "numeric"])
        pbox.append(self.page_entry)
        pbox.append(self.page_total)
        header.pack_start(pbox)

        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=self._menu(),
                                       tooltip_text=_("Document Menu")))
        self.save_btn = Gtk.Button(label=_("Save"), action_name="pdf.save",
                                   tooltip_text=_("Save (Ctrl+S)"))
        header.pack_end(self.save_btn)
        if _sig is not None and hasattr(_sig, "SignatureButton"):
            try:
                sb = _sig.SignatureButton(self.window, self.place_signature)
                sb.set_tooltip_text(_("Sign"))
                header.pack_end(sb)
            except Exception as e:
                print("signature button:", e)
        self.markup_btn = Gtk.ToggleButton(icon_name="marker-symbolic",
                                           tooltip_text=_("Markup Toolbar (Ctrl+Shift+A)"))
        self.mk_revealer.bind_property("reveal-child", self.markup_btn, "active",
                                       GObject.BindingFlags.BIDIRECTIONAL |
                                       GObject.BindingFlags.SYNC_CREATE)
        header.pack_end(self.markup_btn)
        zbox = Gtk.Box(css_classes=["linked"])
        zbox.append(Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text=_("Zoom Out"),
                               action_name="pdf.zoom-out"))
        zmenu = Gio.Menu()
        zmenu.append(_("Fit Width"), "pdf.zoom-fit-width")
        zmenu.append(_("Fit Page"), "pdf.zoom-fit-page")
        zmenu.append(_("Actual Size"), "pdf.zoom-actual")
        self.zoom_btn = Gtk.MenuButton(label="100%", menu_model=zmenu, tooltip_text=_("Zoom"),
                                       css_classes=["numeric"])
        zbox.append(self.zoom_btn)
        zbox.append(Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text=_("Zoom In"),
                               action_name="pdf.zoom-in"))
        header.pack_end(zbox)
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                           tooltip_text=_("Find (Ctrl+F)"))
        self.search_bar.bind_property("search-mode-enabled", self.search_btn, "active",
                                      GObject.BindingFlags.BIDIRECTIONAL |
                                      GObject.BindingFlags.SYNC_CREATE)
        header.pack_end(self.search_btn)
        # header widgets live outside the viewer: expose the action group there too
        if isinstance(self.window, Gtk.Widget):
            self.window.insert_action_group("pdf", self.action_group)
        self._on_zoom_changed_label()
        self._sync_page_entry()
        self._sync_save_label()

    def _menu(self):
        menu = Gio.Menu()

        def sec(label, *entries):
            s = Gio.Menu()
            for e in entries:
                if isinstance(e, Gio.MenuItem):
                    s.append_item(e)
                else:
                    s.append(*e)
            menu.append_section(label, s)

        sec(None, (_("Save"), "pdf.save"), (_("Save As…"), "pdf.save-as"),
            (_("Save Selected Pages as New PDF…"), "pdf.save-selected"),
            (_("Merge All Documents into One PDF…"), "pdf.merge"), (_("Print…"), "pdf.print"))
        exp = Gio.Menu()
        exp.append(_("Export as Images…"), "pdf.export-images")
        exp.append(_("Export with Password…"), "pdf.export-encrypted")
        exp.append(_("Export as Searchable PDF (OCR)…"), "pdf.export-searchable")
        exp.append(_("Reduce File Size…"), "pdf.reduce-size")
        menu.append_submenu(_("Export"), exp)
        edit = Gio.Menu()
        edit.append(_("Insert Blank Page"), "pdf.insert-blank")
        edit.append(_("Insert Pages from File…"), "pdf.insert-file")
        edit.append(_("Rotate Left"), "pdf.rotate-left")
        edit.append(_("Rotate Right"), "pdf.rotate-right")
        edit.append(_("Delete Selected Pages"), "pdf.delete-pages")
        edit.append(_("Recognize Text (OCR)"), "pdf.ocr-page")
        menu.append_submenu(_("Pages"), edit)
        view = Gio.Menu()
        view.append(_("Continuous Scroll"), "pdf.view-mode::continuous")
        view.append(_("Single Page"), "pdf.view-mode::single")
        view.append(_("Two Pages"), "pdf.view-mode::two")
        menu.append_submenu(_("View"), view)
        sec(None, (_("Find…"), "pdf.find"), (_("Go to Page…"), "pdf.goto"),
            (_("Undo"), "pdf.undo"), (_("Redo"), "pdf.redo"))
        return menu

    def _build_actions(self):
        g = Gio.SimpleActionGroup()
        self.action_group = g
        self.actions = {}
        sc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.GLOBAL)

        def add(name, cb, *accels, param=None, state=None):
            if state is not None:
                a = Gio.SimpleAction.new_stateful(name, GLib.VariantType.new("s"),
                                                  GLib.Variant.new_string(state))
                a.connect("activate", lambda act, p: cb(p.get_string()))
            else:
                a = Gio.SimpleAction.new(name, None)
                a.connect("activate", lambda *_: cb())
            g.add_action(a)
            self.actions[name] = a
            for acc in accels:
                sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(acc),
                                                 Gtk.NamedAction.new("pdf." + name)))

        c = lambda: self.canvas  # noqa: E731
        add("save", self.save, "<Control>s")
        add("save-as", self.save_as, "<Control><Shift>s")
        add("save-selected", self.save_selected)
        add("merge", self.merge_documents)
        add("print", self.print_doc, "<Control>p")
        add("export-images", self.export_images)
        add("export-encrypted", self.export_encrypted)
        add("export-searchable", self.export_searchable)
        add("reduce-size", self.reduce_size)
        add("find", self._find, "<Control>f")
        add("goto", self._goto, "<Control>g|<Control>l")
        add("zoom-in", lambda: c().zoom_by(1.25), "<Control>plus|<Control>equal|<Control>KP_Add")
        add("zoom-out", lambda: c().zoom_by(0.8), "<Control>minus|<Control>KP_Subtract")
        add("zoom-fit-width", lambda: c().set_mode("fit-width"), "<Control>0")
        add("zoom-fit-page", lambda: c().set_mode("fit-page"), "<Control>9")
        add("zoom-actual", lambda: c().zoom_to(ACTUAL, *c().center_anchor()), "<Control>1")
        add("view-mode", self._set_view_mode, state="continuous")
        add("toggle-sidebar", lambda: self.split.set_show_sidebar(
            not self.split.get_show_sidebar()), "F9")
        add("toggle-markup", lambda: self.mk_revealer.set_reveal_child(
            not self.mk_revealer.get_reveal_child()), "<Control><Shift>a")
        add("rotate-left", lambda: self.rotate_pages(-1), "<Control>bracketleft")
        add("rotate-right", lambda: self.rotate_pages(1), "<Control>bracketright")
        add("delete-pages", self.delete_selected_pages)
        add("insert-blank", self.insert_blank)
        add("insert-file", self.insert_from_file)
        add("ocr-page", lambda: self.recognize_text([self.current], announce=True))
        add("add-text", self.add_text)
        add("undo", self.undo, "<Control>z")
        add("redo", self.redo, "<Control><Shift>z|<Control>y")
        add("copy", self.copy, "<Control>c")
        add("cut", lambda: self.mk.cut_selected(), "<Control>x")
        add("paste", lambda: self.mk.paste(self.current), "<Control>v")
        add("select-all-text", self._select_all_text, "<Control>a")
        add("highlight-selection", lambda: self._markup_selection("highlight"))
        add("note-here", self._note_here)
        add("next-page", lambda: self.goto_page(self.current + 1), "Page_Down")
        add("prev-page", lambda: self.goto_page(self.current - 1), "Page_Up")
        add("first-page", lambda: self.goto_page(0), "Home|<Control>Home")
        add("last-page", lambda: self.goto_page(len(self.pages) - 1), "End|<Control>End")
        add("delete", self._delete_key, "Delete|BackSpace|KP_Delete")
        add("escape", self._escape, "Escape")
        for name, dx, dy in (("left", -1, 0), ("right", 1, 0), ("up", 0, -1), ("down", 0, 1)):
            add("nudge-" + name, lambda dx=dx, dy=dy: self._nudge(dx, dy, 1), name.capitalize())
            add("nudge10-" + name, lambda dx=dx, dy=dy: self._nudge(dx, dy, 10),
                "<Shift>" + name.capitalize())
        self.insert_action_group("pdf", g)
        self.add_controller(sc)
        self._update_actions()

    EDIT_ACTIONS = {"save", "save-as", "save-selected", "merge", "rotate-left", "rotate-right",
                    "delete-pages", "insert-blank", "insert-file", "add-text", "undo", "redo",
                    "cut", "paste", "highlight-selection", "note-here", "toggle-markup",
                    "export-encrypted", "export-searchable", "reduce-size", "delete"}

    def _update_actions(self):
        if not hasattr(self, "actions"):
            return
        loaded = self._loaded
        for name, a in self.actions.items():
            a.set_enabled(loaded and not (self.compact and name in self.EDIT_ACTIONS))
        if loaded and not self.compact:
            self.actions["save"].set_enabled(not self._busy)
            self.actions["save-as"].set_enabled(not self._busy)
            self.actions["undo"].set_enabled(bool(self._undo))
            self.actions["redo"].set_enabled(bool(self._redo))
            self.actions["merge"].set_enabled(len(self.docs) > 1 and not self._busy)
            self.actions["save-selected"].set_enabled(bool(self._selected_rows()))
            self.actions["reduce-size"].set_enabled(bool(pdf_ops.ghostscript()))
            ocr_ok = _ocr_module() is not None or _ocr_offerable()
            self.actions["export-searchable"].set_enabled(ocr_ok)
            self.actions["ocr-page"].set_enabled(ocr_ok)
        self._sync_save_label()

    def set_compact(self, compact: bool):
        """Read-only presentation for Quick Look: no sidebar, no markup."""
        self.compact = compact
        if compact:
            self.split.set_show_sidebar(False)
            self.mk_revealer.set_reveal_child(False)
            self.mk.enabled = False
            self.search_bar.set_search_mode(False)
        else:
            self.mk.enabled = True
        self._update_actions()

    # ============================================================= loading ====
    def load(self, files: list[Gio.File]) -> None:
        self.stats["t_load"] = time.monotonic()
        self._open_queue = list(files)
        if not self.docs:
            self.stack.set_visible_child_name("loading")
        self._open_next()

    def _open_next(self):
        """Open queued files one at a time, off the main thread."""
        if not self._open_queue:
            if not self.docs and self.stack.get_visible_child_name() == "loading":
                self._show_error(_("Could not open the document"), "")
            return
        f = self._open_queue.pop(0)
        path = f.get_path()
        name = f.get_basename() or "Document.pdf"
        self._try_open(Source(name, path=path), f)

    def _try_open(self, src, gfile, bad_password=False):
        """Open src in a thread; attach on the main loop (or ask a password)."""
        gen = self._gen

        def work():
            try:
                if src.path is None and src.data is None:
                    ok, data, _etag = gfile.load_contents(None)
                    src.data = bytes(data)
                pdoc = poppler_open(src)
                sizes = [pdoc.get_page(i).get_size() for i in range(pdoc.get_n_pages())]
                GLib.idle_add(done, pdoc, sizes, None)
            except GLib.Error as e:
                GLib.idle_add(done, None, None, e)
            except Exception as e:
                GLib.idle_add(done, None, None, e)

        def done(pdoc, sizes, err):
            if self._released or gen != self._gen:
                return False
            if err is not None:
                if isinstance(err, GLib.Error) and _is_encrypted_error(err):
                    self._ask_password(src, gfile, bad_password or src.password is not None)
                    return False
                self._error(src.name, getattr(err, "message", None) or str(err))
            else:
                self._add_document(src, pdoc, gfile, sizes)
            self._open_next()
            return False

        threading.Thread(target=work, name="pdf-open", daemon=True).start()
        return False

    def _error(self, name, msg):
        if self.docs:
            self.toast(_("Could not open “{name}”: {msg}").format(name=name, msg=msg))
        else:
            self._show_error(_("Could not open “{name}”").format(name=name), msg)

    def _parent(self):
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    def _present(self, dialog):
        dialog.present(self if self.get_root() is not None else None)

    def _ask_password(self, src, gfile, wrong):
        if not self.docs:
            self.status.set_title(_("Password Required"))
            self.status.set_description(_("“{name}” is password-protected.").format(name=src.name))
            self.status.set_icon_name("system-lock-screen-symbolic")
            btn = Gtk.Button(label=_("Unlock…"), halign=Gtk.Align.CENTER,
                             css_classes=["pill", "suggested-action"])
            btn.connect("clicked", lambda *_: self._ask_password(src, gfile, False))
            self.status.set_child(btn)
            self.stack.set_visible_child_name("status")

        def present():
            entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
            d = Adw.AlertDialog(
                heading=_("Password Required"),
                body=(_("Incorrect password. Try again.") if wrong else
                      _("Enter the password to open “{name}”.").format(name=src.name)),
                extra_child=entry, default_response="unlock", close_response="cancel")
            d.add_response("cancel", _("Cancel"))
            d.add_response("unlock", _("Unlock"))
            d.set_response_appearance("unlock", Adw.ResponseAppearance.SUGGESTED)

            def done(_d, response):
                if response == "unlock":
                    src.password = entry.get_text()

                    GLib.idle_add(lambda: self._try_open(src, gfile, True) and False)
                else:
                    GLib.idle_add(lambda: self._open_next() and False)
            d.connect("response", done)
            self._present(d)
            entry.grab_focus()

        if self.get_mapped():
            present()
        else:
            hid = []

            def on_map(*_):
                self.disconnect(hid[0])
                GLib.idle_add(lambda: present() and False)
            hid.append(self.connect("map", on_map))

    def _show_error(self, title, msg):
        self.status.set_title(title)
        self.status.set_description(msg)
        self.status.set_icon_name("dialog-error-symbolic")
        self.status.set_child(None)
        self.stack.set_visible_child_name("status")

    def _add_source(self, src, pdoc=None):
        sid = len(self.sources)
        self.sources.append(src)
        self._pdocs[sid] = pdoc if pdoc is not None else poppler_open(src)
        self.worker.set_sources(self.sources)
        self.tworker.set_sources(self.sources)
        return sid

    def _add_document(self, src, pdoc, gfile, sizes=None):
        if not self.sources:
            self._gen += 1
            self.worker.reset([], self._gen)
            self.tworker.reset([], self._gen)
        sid = self._add_source(src, pdoc)
        for i, sz in enumerate(sizes or ()):
            self._base[(sid, i)] = sz
        d = Doc(src.name, sid, src.path, gfile or (Gio.File.new_for_path(src.path)
                                                   if src.path else None))
        d.pages = [PageRef(sid, i) for i in range(pdoc.get_n_pages())]
        d.clean_key = d.key()
        self.docs.append(d)
        first = len(self.docs) == 1
        self._loaded = bool(d.pages) or self._loaded
        self.stack.set_visible_child_name("doc")
        if first:
            self.active = 0
            self.current = 0
            self._show_sidebar_later(not self.compact and len(d.pages) > 1)
            self._activate_doc(0, 0)
        else:
            self._show_sidebar_later(not self.compact)
            self._rebuild_store()
        self._update_actions()
        self.refresh_title()

    def _show_sidebar_later(self, show):
        """Showing the sidebar animates; doing it before the split view has a
        size makes AdwOverlaySplitView allocate garbage widths. Wait for it."""
        if self.split.get_show_sidebar() == show:
            return
        if self.split.get_width() > 0:
            self.split.set_show_sidebar(show)
            return

        def later():
            if self.split.get_width() > 0 or not self.split.get_mapped():
                self.split.set_show_sidebar(show)
                return False
            return True
        GLib.timeout_add(30, later)

    def _activate_doc(self, di, pos=0):
        self.active = di
        self.mk.select(None)
        self.text_sel = None
        self._search_reset()
        self.current = max(0, min(pos, len(self.pages) - 1))
        self._recompute_sizes()
        c = self.canvas
        if c.view_w and c.mode != "free":
            c.zoom = c.fit_zoom(c.mode)
            self.on_zoom_changed()
        c.relayout()
        c.scroll_to_page(self.current)
        c.queue_allocate()
        self._rebuild_store()
        self._sync_page_entry()
        self._refresh_side_lists()
        self.refresh_title()

    def _base_size(self, src, index):
        k = (src, index)
        s = self._base.get(k)
        if s is None:
            s = self._base[k] = self._pdocs[src].get_page(index).get_size()
        return s

    def _disp_size(self, ref):
        w, h = self._base_size(ref.src, ref.index)
        return (h, w) if ref.rotation % 180 else (w, h)

    def _recompute_sizes(self):
        self.sizes = [self._disp_size(r) for r in self.pages]

    def _reader(self, sid):
        r = self._readers.get(sid)
        if r is None:
            r = self._readers[sid] = pdf_ops.open_reader(self.sources[sid])
        return r

    # ===================================================== viewer contract ====
    def get_title(self) -> str:
        d = self.doc
        if d is None:
            return _("View")
        return d.name + (_(" — Edited") if d.dirty() and not self.compact else "")

    def get_subtitle(self) -> str:
        if not self._loaded or not self.pages:
            return ""
        s = _("Page {page} of {total}").format(page=self.current + 1, total=len(self.pages))
        if len(self.docs) > 1:
            s += _(" · {n} of {total} documents").format(n=self.active + 1, total=len(self.docs))
        return s

    def has_unsaved_changes(self) -> bool:
        return self._loaded and not self.compact and any(d.dirty() for d in self.docs)

    def get_current_file(self):
        return self.doc.gfile if self.doc else None

    def accepts_drop(self, files):
        return self._loaded and not self.compact and bool(files) and all(
            kind_of(f) in ("pdf", "image") and f.get_path() for f in files)

    def add_dropped(self, files):
        self.insert_files(files, self.active, self.current + 1)

    def save_changes(self, on_done):
        """Close-prompt hook: save every edited document in place."""
        dirty = [d for d in self.docs if d.dirty() and d.path]
        if any(d.dirty() and not d.path for d in self.docs):
            self.toast(_("Some documents can only be saved with Save As"))
            on_done(False)
            return
        if not dirty:
            on_done(True)
            return
        results = []

        def next_one():
            if not dirty:
                on_done(all(results))
                return
            d = dirty.pop(0)
            self._write_doc(self.docs.index(d), d.path,
                            done=lambda ok: (results.append(ok), next_one()))
        next_one()

    # ============================================================ painting ====
    def _accent(self):
        try:
            c = Adw.StyleManager.get_default().get_accent_color_rgba()
            return (c.red, c.green, c.blue)
        except Exception:
            return (0.21, 0.52, 0.89)

    def paint(self, c: PageCanvas, s: Gtk.Snapshot):
        if not self._loaded or not c.rects:
            return
        z = c.zoom
        ds = c.device_scale()
        skey = round(z * ds * 1000)
        hv, vv = c.hv(), c.vv()
        s.save()
        pt = Graphene.Point()
        pt.init(-hv, -vv)
        s.translate(pt)
        white = _rgba(1, 1, 1)
        shadow = _rgba(0, 0, 0, 0.28)
        acc = self._accent()
        sel_col = _rgba(acc[0], acc[1], acc[2], 0.32)
        hl = _rgba(1.0, 0.85, 0.1, 0.38)
        hl_cur = _rgba(1.0, 0.5, 0.0, 0.5)
        # Page textures are rendered at device resolution, so they must land on
        # whole device pixels or LINEAR filtering blurs them. Snap here, at paint
        # time, against the current scale and the canvas's position in the
        # surface: scroll offsets are fractional and the window can move between
        # monitors with different scales after relayout() ran.
        ox, oy = c.surface_origin()
        snap_x = lambda v: (round((ox + v - hv) * ds) / ds) - ox + hv  # noqa: E731
        snap_y = lambda v: (round((oy + v - vv) * ds) / ds) - oy + vv  # noqa: E731
        vis = c.visible_range()
        for pos in vis:
            x, y, w, h = c.rects[pos]
            x, y = snap_x(x), snap_y(y)
            ref = self.pages[pos]
            rect = R(x, y, w, h)
            s.append_outset_shadow(RR(rect, 0), shadow, 0, 1, 0, 5)
            s.append_color(white, rect)
            pk = _pk(ref)
            tiled = max(w, h) * ds > TILE_ABOVE
            tex = None if tiled else self.cache.get(pk + (skey,))
            if tex is not None:
                # exact-scale texture: draw 1:1 instead of stretching it into a
                # rect that can be a fraction of a pixel off
                s.append_texture(tex, R(x, y, tex.get_width() / ds, tex.get_height() / ds))
            else:
                tex = self.cache.best(pk) or self.thumbs.best(pk)
                if tex is not None:
                    s.append_scaled_texture(tex, Gsk.ScalingFilter.LINEAR, rect)
            if tiled:
                for tx, ty, tr in self._visible_tiles(pos, ds):
                    t = self.cache.get(pk + (skey, tx, ty))
                    if t is not None:
                        s.append_texture(t, R(x + tr[0], y + tr[1],
                                              t.get_width() / ds, t.get_height() / ds))
            if self._search and self._search["results"].get(pos):
                cur = self._search["cur"]
                for k, (rx, ry, rw, rh) in enumerate(self._search["results"][pos]):
                    s.append_color(hl_cur if cur == (pos, k) else hl,
                                   R(x + rx * z, y + ry * z, rw * z, rh * z))
            if not self.compact:
                fields = self._fields_for(ref)
                if fields:
                    tint = _rgba(0.3, 0.55, 1.0, 0.13)
                    for f in fields:
                        fx, fy, fw, fh = self._from_poppler_rect(ref, *f["rect"])
                        s.append_color(tint, R(x + fx * z, y + fy * z, fw * z, fh * z))
            if self.text_sel and self.text_sel["pos"] == pos:
                for rx, ry, rw, rh in self.text_sel["rects"]:
                    s.append_color(sel_col, R(x + rx * z, y + ry * z, rw * z, rh * z))
            need_mk = ref.items or (self.mk.selected and self.mk.selected[0] == pos) or \
                (self.mk._preview and self.mk._preview[0] == pos) or \
                (self.mk.pad is not None and self.mk.pad.key == pos)
            if need_mk:
                ix0, iy0 = max(x, hv), max(y, vv)
                ix1, iy1 = min(x + w, hv + c.view_w), min(y + h, vv + c.view_h)
                if ix1 > ix0 and iy1 > iy0:
                    pad = 24
                    cr = s.append_cairo(R(ix0 - pad, iy0 - pad, ix1 - ix0 + 2 * pad,
                                          iy1 - iy0 + 2 * pad))
                    cr.translate(x, y)
                    cr.scale(z, z)
                    self.mk.draw(cr, pos, z, acc)
                    del cr
        s.restore()
        self._queue_renders()
        if self.stats["first_page"] is None and vis and self.cache.d:
            self.stats["first_page"] = time.monotonic() - (self.stats["t_load"] or 0)

    def _visible_tiles(self, pos, ds):
        """(tx, ty, (ox, oy) in logical px relative to the page) of visible tiles."""
        c = self.canvas
        x, y, w, h = c.rects[pos]
        fw, fh = w * ds, h * ds
        vx0, vy0 = (c.hv() - x) * ds, (c.vv() - y) * ds
        vx1, vy1 = vx0 + c.view_w * ds, vy0 + c.view_h * ds
        out = []
        for ty in range(max(0, int(vy0 // TILE)), min(int((fh - 1) // TILE), int(vy1 // TILE)) + 1):
            for tx in range(max(0, int(vx0 // TILE)), min(int((fw - 1) // TILE), int(vx1 // TILE)) + 1):
                out.append((tx, ty, (tx * TILE / ds, ty * TILE / ds)))
        return out

    # =========================================================== rendering ====
    def _queue_renders(self):
        if not self._render_idle and not self._released:
            self._render_idle = GLib.idle_add(self._submit_renders)

    def _submit_renders(self):
        self._render_idle = 0
        c = self.canvas
        if not self._loaded or self._released or c.view_w < 4 or c.view_h < 4 \
                or not c.get_mapped():
            return False
        ds = c.device_scale()
        jobs = []
        urgent = []                 # visible pages with nothing at all: quick low-res first
        if c.rects:
            vis = c.visible_range()
            if vis:
                extra = [p for p in (vis[0] - 1, vis[-1] + 1)
                         if 0 <= p < len(self.pages) and c.rects[p] is not None]
                skey = round(c.zoom * ds * 1000)
                for pos in vis + extra:
                    ref = self.pages[pos]
                    pk = _pk(ref)
                    x, y, w, h = c.rects[pos]
                    tiled = max(w, h) * ds > TILE_ABOVE
                    if pos not in extra and self.cache.best(pk) is None \
                            and self.thumbs.best(pk) is None:
                        urgent.append(self._thumb_job(ref, ds))
                    if not c.settled:
                        if self.cache.best(pk) is None and self.thumbs.best(pk) is None:
                            jobs.append(self._thumb_job(ref, ds))
                        continue
                    if not tiled:
                        key = pk + (skey,)
                        if not self.cache.has(key):
                            jobs.append(Job(key, ref.src, ref.index, ref.rotation,
                                            skey / 1000, None, ref.forms))
                        continue
                    if pos in extra:
                        continue
                    # a capped whole-page base layer, then the visible tiles
                    bscale = TILE_ABOVE / max(w / c.zoom, h / c.zoom) * 0.5
                    bkey = pk + (round(bscale * 1000),)
                    if self.cache.best(pk) is None:
                        jobs.append(Job(bkey, ref.src, ref.index, ref.rotation, bscale,
                                        None, ref.forms))
                    for tx, ty, _o in self._visible_tiles(pos, ds):
                        key = pk + (skey, tx, ty)
                        if not self.cache.has(key):
                            jobs.append(Job(key, ref.src, ref.index, ref.rotation,
                                            skey / 1000, (tx, ty), ref.forms))
        self.worker.submit(jobs)
        tjobs = list(urgent)
        if self.split.get_show_sidebar() and self.side_stack.get_visible_child_name() == "thumbs":
            for box in self._visible_thumb_boxes():
                ref = box._ref
                if ref is not None and self.thumbs.best(_pk(ref)) is None:
                    tjobs.append(self._thumb_job(ref, ds))
        self.tworker.submit(tjobs)
        return False

    def _visible_thumb_boxes(self):
        sc = self.thumb_scroller
        h = sc.get_height()
        out = []
        for box in self._boxes:
            if box._ref is None or not box.get_mapped():
                continue
            b = box.compute_bounds(sc)
            if b[0]:
                r = b[1]
                if r.get_y() + r.get_height() >= -200 and r.get_y() <= h + 200:
                    out.append((r.get_y(), box))
        out.sort(key=lambda t: t[0])
        return [b for _y, b in out]

    def _thumb_job(self, ref, ds):
        dw = self._disp_size(ref)[0]
        return Job(_pk(ref) + ("T",), ref.src, ref.index, ref.rotation,
                   THUMB_W * ds / dw, None, ref.forms)

    def _deliver(self, gen, job, tex, dt=0.0):
        if gen != self._gen or self._released:
            return False
        key = job.key
        if key[-1] == "T":
            self.thumbs.put(key, tex)
            for box in list(self._bound.get(key[:4], ())):
                box._pic.set_paintable(tex)
        else:
            self.cache.put(key, tex)
            self.stats["renders"].append((dt, tex.get_width(), tex.get_height()))
            del self.stats["renders"][:-2000]
        self.canvas.queue_draw()
        return False

    # ======================================================== scroll state ====
    def on_scrolled(self):
        c = self.canvas
        if not c.rects or c.layout_mode == "single":
            self._queue_renders()
            return
        cur = c.page_at(c.hv() + c.view_w / 2, c.vv() + c.view_h * 0.4)
        if c.vv() >= c.content_h - c.view_h - 1 and len(self.pages) > 1:
            cur = c.visible_range()[-1]
        if cur != self.current:
            self.current = cur
            self._current_changed()
        self._queue_renders()

    def _current_changed(self):
        self._sync_page_entry()
        self.refresh_title()
        for box in self._boxes:
            if box._row is not None:
                (box.add_css_class if box._row == (self.active, self.current)
                 else box.remove_css_class)("current")
        if self.split.get_show_sidebar() and not self._explicit_sel:
            row = self._row_index(self.active, self.current)
            if row is not None:
                self.thumb_list.scroll_to(row, Gtk.ListScrollFlags.NONE, None)

    def on_zoom_changed(self):
        self._on_zoom_changed_label()

    def _on_zoom_changed_label(self):
        if hasattr(self, "zoom_btn"):
            self.zoom_btn.set_label(f"{round(self.canvas.zoom / ACTUAL * 100)}%")

    def goto_page(self, pos, at_end=False):
        if not self.pages:
            return
        pos = max(0, min(pos, len(self.pages) - 1))
        self.current = pos
        c = self.canvas
        if c.layout_mode == "single":
            c.relayout()
            c._configure(c.hv(), c.content_h if at_end else 0)
        else:
            c.scroll_to_page(pos)
        self._current_changed()

    def _sync_page_entry(self):
        if not hasattr(self, "page_entry"):
            return
        self.page_entry.set_text(str(self.current + 1) if self.pages else "")
        self.page_total.set_label(_("of {total}").format(total=len(self.pages)) if self.pages else "")

    def _on_page_entry(self, entry):
        try:
            n = int(entry.get_text().strip())
        except ValueError:
            self._sync_page_entry()
            return
        self.goto_page(n - 1)
        self.canvas.grab_focus()

    def _goto(self):
        if hasattr(self, "page_entry"):
            self.page_entry.grab_focus()

    def _set_view_mode(self, mode):
        self.actions["view-mode"].set_state(GLib.Variant.new_string(mode))
        self.canvas.set_layout_mode(mode)

    # ================================================== markup host (mk_*) ====
    def mk_page_at(self, wx, wy):
        return self.canvas.page_hit(wx, wy)

    def mk_to_page(self, key, wx, wy):
        c = self.canvas
        r = c.rects[key] if 0 <= key < len(c.rects) else None
        if r is None:
            return 0.0, 0.0
        return (c.hv() + wx - r[0]) / c.zoom, (c.vv() + wy - r[1]) / c.zoom

    def mk_to_widget(self, key, x, y):
        return self.canvas.doc_to_widget(key, x, y)

    def mk_scale(self, key):
        return self.canvas.zoom

    def mk_items(self, key):
        return self.pages[key].items

    def mk_page_size(self, key):
        return self.sizes[key]

    def mk_keys(self):
        return self.canvas.visible_range()

    def mk_begin_edit(self):
        self.push_undo()

    def mk_changed(self):
        self._changed(structure=False)

    def mk_widget(self):
        return self.canvas

    def mk_set_popover(self, pop, old=None):
        if pop is not None:
            self.canvas.add_popover(pop)
        elif old is not None:
            self.canvas.remove_popover(old)

    def mk_visible_rect(self, key):
        c = self.canvas
        x, y, w, h = c.rects[key]
        W, H = self.sizes[key]
        x0 = max(0.0, (c.hv() - x) / c.zoom)
        y0 = max(0.0, (c.vv() - y) / c.zoom)
        x1 = min(W, (c.hv() + c.view_w - x) / c.zoom)
        y1 = min(H, (c.vv() + c.view_h - y) / c.zoom)
        return x0, y0, max(x1 - x0, 1), max(y1 - y0, 1)

    def mk_text_quads(self, key, x0, y0, x1, y1):
        text, rects = self._select_text(key, x0, y0, x1, y1)
        return text, rects

    def mk_tool_changed(self, tool):
        if tool in TEXTMARK_KINDS and self.text_sel and self.text_sel["rects"]:
            self._markup_selection(tool)
            GLib.idle_add(lambda: self.mk.set_tool("select") and False)

    def mk_touchpad_stopped(self):
        if self.mk_bar.pad_btn.get_active():
            self.mk_bar.pad_btn.set_active(False)

    def _on_pad_toggled(self, btn):
        if btn.get_active():
            if self.mk.pad is None:
                err = self.mk.start_touchpad(self.current)
                if err:
                    btn.set_active(False)
                    self.toast(_("Touchpad drawing unavailable: {err}").format(err=err))
                else:
                    self.toast(_("Draw with one finger on the touchpad · any key to finish"))
        elif self.mk.pad is not None:
            self.mk.pad.stop()

    # ============================================== canvas pointer routing ====
    def canvas_cursor(self, x, y):
        if not self._loaded:
            return None
        cur = self.mk.cursor_at(x, y)
        if cur:
            return cur
        hit = self.canvas.page_hit(x, y)
        if hit and self._field_at(*hit):
            return "pointer"
        return "text" if hit else None

    def canvas_press(self, x, y, state):
        self._press = None
        if not self._loaded:
            return False
        if self.mk.on_press(x, y, 1, state):
            self._press = "mk"
            self.text_sel = None
            return True
        hit = self.canvas.page_hit(x, y)
        if self._explicit_sel and hit:
            self.sel.unselect_all()
        if hit is None or self.mk.tool != "select":
            if self.text_sel:
                self.text_sel = None
                self.canvas.queue_draw()
            return False
        pos, px, py = hit
        if not self.compact:
            f = self._field_at(pos, px, py)
            if f is not None:
                self._press = "form"
                self._activate_field(pos, f)
                return True
        self.text_sel = {"pos": pos, "p0": (px, py), "p1": (px, py), "rects": [], "text": ""}
        self._press = "text"
        self.canvas.queue_draw()
        return True

    def canvas_drag(self, x, y, state):
        if self._press == "mk":
            self.mk.on_drag(x, y, state)
        elif self._press == "text" and self.text_sel:
            ts = self.text_sel
            px, py = self.mk_to_page(ts["pos"], x, y)
            ts["p1"] = (px, py)
            ts["text"], ts["rects"] = self._select_text(ts["pos"], *ts["p0"], px, py)
            self.canvas.queue_draw()

    def canvas_release(self, x, y):
        if self._press == "mk":
            self.mk.on_release(x, y)
        elif self._press == "text" and self.text_sel:
            ts = self.text_sel
            if not ts["rects"]:
                pos = ts["pos"]
                self.text_sel = None
                moved = abs(ts["p1"][0] - ts["p0"][0]) + abs(ts["p1"][1] - ts["p0"][1])
                if moved > 3 and not self._page_has_text(self.pages[pos]):
                    self._offer_ocr(pos)
            self.canvas.queue_draw()
        self._press = None

    def canvas_multi_click(self, x, y, n, state):
        key, it = self.mk.item_at(x, y)
        if it is not None and it.kind in ("text", "note") and self.mk.enabled:
            self.mk.edit_item(key, it)
            return
        hit = self.canvas.page_hit(x, y)
        if hit is None or self.mk.tool != "select":
            return
        pos, px, py = hit
        style = Poppler.SelectionStyle.WORD if n == 2 else Poppler.SelectionStyle.LINE
        text, rects = self._select_text(pos, px, py, px, py, style)
        if rects:
            self.text_sel = {"pos": pos, "p0": (px, py), "p1": (px, py),
                             "rects": rects, "text": text}
            self.canvas.queue_draw()

    def canvas_context_menu(self, x, y):
        if not self._loaded:
            return
        hit = self.canvas.page_hit(x, y)
        self._ctx_point = hit
        menu = Gio.Menu()
        if self.text_sel and self.text_sel["rects"]:
            menu.append(_("Copy"), "pdf.copy")
            if not self.compact:
                menu.append(_("Highlight"), "pdf.highlight-selection")
        if not self.compact and hit is not None:
            menu.append(_("Add Note Here"), "pdf.note-here")
            if _ocr_module() is not None and not self._page_has_text(self.pages[hit[0]]):
                menu.append(_("Recognize Text"), "pdf.ocr-page")
        if not self.compact:
            s = Gio.Menu()
            s.append(_("Rotate Left"), "pdf.rotate-left")
            s.append(_("Rotate Right"), "pdf.rotate-right")
            menu.append_section(None, s)
        if menu.get_n_items() == 0:
            return
        pm = Gtk.PopoverMenu.new_from_model(menu)
        pm.set_has_arrow(False)
        self.canvas.add_popover(pm)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        pm.set_pointing_to(r)
        pm.connect("closed", lambda p: GLib.idle_add(
            lambda: self.canvas.remove_popover(p) and False))
        pm.popup()

    # ===================================================== text selection ====
    def _ppage(self, ref):
        return self._pdocs[ref.src].get_page(ref.index)

    def _page_has_text(self, ref):
        k = (ref.src, ref.index)
        v = self._has_text.get(k)
        if v is None:
            try:
                v = bool((self._ppage(ref).get_text() or "").strip())
            except Exception:
                v = True
            self._has_text[k] = v
        return v

    def _to_poppler(self, ref, x, y):
        W, H = self._disp_size(ref)
        turns = (4 - ref.rotation // 90) % 4
        return rotate_point(x, y, W, H, turns)[:2]

    def _from_poppler_rect(self, ref, x, y, w, h):
        pw, ph = self._base_size(ref.src, ref.index)
        turns = ref.rotation // 90
        a = rotate_point(x, y, pw, ph, turns)
        b = rotate_point(x + w, y + h, pw, ph, turns)
        return (min(a[0], b[0]), min(a[1], b[1]), abs(a[0] - b[0]), abs(a[1] - b[1]))

    def _select_text(self, pos, x0, y0, x1, y1, style=Poppler.SelectionStyle.GLYPH):
        """Text between two display-frame points -> (text, rects)."""
        ref = self.pages[pos]
        k = (ref.src, ref.index)
        a, b = self._to_poppler(ref, x0, y0), self._to_poppler(ref, x1, y1)
        if not self._page_has_text(ref):
            if k in self.ocr_lines:
                return self._select_ocr(ref, a, b)
            return "", []
        page = self._ppage(ref)
        r = Poppler.Rectangle()
        r.x1, r.y1, r.x2, r.y2 = a[0], a[1], b[0], b[1]
        try:
            text = page.get_selected_text(style, r) or ""
            reg = page.get_selected_region(4.0, style, r)
        except Exception:
            return "", []
        rects = []
        for i in range(reg.num_rectangles()):
            q = reg.get_rectangle(i)
            rects.append(self._from_poppler_rect(ref, q.x / 4, q.y / 4,
                                                 q.width / 4, q.height / 4))
        return text, rects

    def _select_ocr(self, ref, a, b):
        lines = self.ocr_lines[(ref.src, ref.index)]
        (ax, ay), (bx, by) = a, b
        if (ay, ax) > (by, bx):
            (ax, ay), (bx, by) = (bx, by), (ax, ay)
        texts, rects = [], []
        for poly, text in lines:
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            lx0, ly0, lx1, ly1 = min(xs), min(ys), max(xs), max(ys)
            cy = (ly0 + ly1) / 2
            if ly1 < ay or ly0 > by or not text:
                continue
            s0, s1 = 0.0, 1.0
            if ly0 <= ay <= ly1 or (cy < ay):
                s0 = (ax - lx0) / max(lx1 - lx0, 1e-6)
            if ly0 <= by <= ly1 or (cy > by):
                s1 = (bx - lx0) / max(lx1 - lx0, 1e-6)
            if ay > ly1 or by < ly0:
                continue
            s0, s1 = max(0.0, min(1.0, s0)), max(0.0, min(1.0, s1))
            if ly0 <= ay <= ly1 and ly0 <= by <= ly1:
                s0, s1 = min(s0, s1), max(s0, s1)
            n = len(text)
            i0, i1 = int(round(s0 * n)), int(round(s1 * n))
            if i1 <= i0:
                continue
            texts.append(text[i0:i1])
            rects.append(self._from_poppler_rect(ref, lx0 + (lx1 - lx0) * i0 / n, ly0,
                                                 (lx1 - lx0) * (i1 - i0) / n, ly1 - ly0))
        return "\n".join(texts), rects

    def _select_all_text(self):
        if not self.pages:
            return
        W, H = self.sizes[self.current]
        text, rects = self._select_text(self.current, 0, 0, W, H)
        self.text_sel = {"pos": self.current, "p0": (0, 0), "p1": (W, H),
                         "rects": rects, "text": text}
        self.canvas.queue_draw()

    def copy(self):
        if self.mk.copy_selected():
            return
        if self.text_sel and self.text_sel["text"]:
            self.get_clipboard().set(self.text_sel["text"])
            self.toast(_("Copied text"))

    def _markup_selection(self, kind):
        ts = self.text_sel
        if not ts or not ts["rects"]:
            return
        color = self.mk.highlight_color if kind == "highlight" else self.mk.stroke
        it = Item(kind, points=[list(r) for r in ts["rects"]], stroke=color, text=ts["text"])
        self.mk.add_item(ts["pos"], it, select=False)
        self.text_sel = None
        self.canvas.queue_draw()

    def _note_here(self):
        hit = getattr(self, "_ctx_point", None) or (self.current, 40, 40)
        pos, x, y = hit
        W, H = self.sizes[pos]
        it = Item("note", min(x, W - markup.NOTE_SIZE), min(y, H - markup.NOTE_SIZE),
                  markup.NOTE_SIZE, markup.NOTE_SIZE, stroke=markup.NOTE_COLOR)
        self.mk.add_item(pos, it)
        self.mk.edit_item(pos, it, new=True)

    # ================================================================ OCR ====
    def _offer_ocr(self, pos):
        if _ocr_module() is None:
            return
        ref = self.pages[pos]
        if (ref.src, ref.index) in self._ocr_running:
            return
        self.recognize_text([pos], announce=True)

    def recognize_text(self, positions, announce=False, done=None):
        ocr = _ocr_module()
        if ocr is None:
            if announce and _ocr_offerable():
                from . import ocr_install
                ocr_install.offer(self, lambda: self.recognize_text(positions, announce, done))
            else:
                self.toast(_("Text recognition is not available"))
            return
        refs = [self.pages[p] for p in positions
                if (self.pages[p].src, self.pages[p].index) not in self.ocr_lines]
        if not refs:
            if done:
                done()
            return
        for r in refs:
            self._ocr_running.add((r.src, r.index))
        if announce:
            self.toast(ngettext("Recognizing text on {n} page…", "Recognizing text on {n} pages…", len(refs)).format(n=len(refs)))
        sources = list(self.sources)

        def work():
            out = {}
            docs = {}
            for r in refs:
                try:
                    out[(r.src, r.index)] = _ocr_page(sources, docs, r.src, r.index, ocr)
                except Exception as e:
                    print("OCR failed:", e)
            GLib.idle_add(finish, out)

        def finish(out):
            for k, v in out.items():
                self.ocr_lines[k] = v
                self._has_text[k] = False
            for r in refs:
                self._ocr_running.discard((r.src, r.index))
            n = sum(len(v) for v in out.values())
            if announce:
                self.toast(ngettext("Recognized {n} line of text", "Recognized {n} lines of text", n).format(n=n) if n
                           else _("No text found"))
            if done:
                done()
            return False

        threading.Thread(target=work, name="pdf-ocr", daemon=True).start()

    # ============================================================== forms ====
    def _fields_for(self, ref):
        k = (ref.src, ref.index)
        if k in self._fields:
            return self._fields[k]
        out = []
        try:
            page = self._ppage(ref)
            maps = page.get_form_field_mapping()
        except Exception:
            maps = []
        if maps:
            try:
                rot = int(self._reader(ref.src).pages[ref.index].rotation or 0) % 360
            except Exception:
                rot = 0
            pw, ph = self._base_size(ref.src, ref.index)
            cw, ch = (ph, pw) if rot % 180 else (pw, ph)
            for m in maps:
                f = m.field
                t = f.get_field_type()
                if t not in (Poppler.FormFieldType.TEXT, Poppler.FormFieldType.BUTTON,
                             Poppler.FormFieldType.CHOICE) or f.is_read_only():
                    continue
                if t == Poppler.FormFieldType.BUTTON and \
                        f.button_get_button_type() == Poppler.FormButtonType.PUSH:
                    continue
                a = m.area
                p = rotate_point(a.x1, ch - a.y2, cw, ch, rot // 90)
                q = rotate_point(a.x2, ch - a.y1, cw, ch, rot // 90)
                rect = (min(p[0], q[0]), min(p[1], q[1]), abs(p[0] - q[0]), abs(p[1] - q[1]))
                info = {"name": f.get_name(), "type": t, "rect": rect,
                        "value": pdf_ops.form_value(f), "multiline": False, "options": []}
                if t == Poppler.FormFieldType.TEXT:
                    info["multiline"] = f.text_get_text_type() == Poppler.FormTextType.MULTILINE
                elif t == Poppler.FormFieldType.CHOICE:
                    info["options"] = [f.choice_get_item(i) for i in range(f.choice_get_n_items())]
                if info["name"]:
                    out.append(info)
        self._fields[k] = out
        return out

    def _field_at(self, pos, x, y):
        ref = self.pages[pos]
        for f in self._fields_for(ref):
            rx, ry, rw, rh = self._from_poppler_rect(ref, *f["rect"])
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return f
        return None

    def _activate_field(self, pos, f):
        ref = self.pages[pos]
        name = f["name"]
        cur = ref.forms.get(name, f["value"])
        t = f["type"]
        if t == Poppler.FormFieldType.BUTTON:
            self.push_undo()
            ref.forms[name] = not bool(cur)
            self._changed(structure=False)
            return
        rx, ry, rw, rh = self._from_poppler_rect(ref, *f["rect"])
        wx, wy = self.canvas.doc_to_widget(pos, rx, ry)
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(wx), int(wy)
        rect.width, rect.height = max(1, int(rw * self.canvas.zoom)), max(1, int(rh * self.canvas.zoom))

        def commit(value):
            if value != cur:
                self.push_undo()
                ref.forms[name] = value
                self._changed(structure=False)

        if t == Poppler.FormFieldType.TEXT:
            entry = Gtk.Entry(text=str(cur or ""), width_chars=max(12, int(rw / 7)))
            pop = Gtk.Popover(child=entry, position=Gtk.PositionType.BOTTOM)
            entry.connect("activate", lambda *_: pop.popdown())
            pop.connect("closed", lambda p: (commit(entry.get_text()), GLib.idle_add(
                lambda: self.canvas.remove_popover(p) and False)))
        else:
            lb = Gtk.ListBox(css_classes=["navigation-sidebar"])
            for opt in f["options"]:
                lb.append(Gtk.Label(label=opt, xalign=0))
            pop = Gtk.Popover(child=lb, position=Gtk.PositionType.BOTTOM)
            state = {"v": cur}

            def picked(_lb, row):
                state["v"] = f["options"][row.get_index()]
                pop.popdown()
            lb.connect("row-activated", picked)
            pop.connect("closed", lambda p: (commit(state["v"]), GLib.idle_add(
                lambda: self.canvas.remove_popover(p) and False)))
        self.canvas.add_popover(pop)
        pop.set_pointing_to(rect)
        pop.popup()

    # ========================================================= sidebar rows ====
    def _rows(self):
        out = []
        multi = len(self.docs) > 1
        for di, d in enumerate(self.docs):
            if multi:
                out.append((di, -1))
            if d.expanded or not multi:
                out += [(di, p) for p in range(len(d.pages))]
        return out

    def _row_index(self, di, pos):
        for i in range(self.store.get_n_items()):
            it = self.store.get_item(i)
            if it.di == di and it.pos == pos:
                return i
        return None

    def _rebuild_store(self, select=None):
        """select: list of (di, pos) rows to select afterwards."""
        rows = self._rows()
        self._suppress_sel = True
        self.store.splice(0, self.store.get_n_items(), [RowItem(di, p) for di, p in rows])
        self.sel.unselect_all()
        if select:
            index = {r: i for i, r in enumerate(rows)}
            for r in select:
                if r in index:
                    self.sel.select_item(index[r], False)
        self._suppress_sel = False
        # programmatic selections (after a move/insert) only highlight; the
        # "selected pages" mode of Save/Delete needs a click in the sidebar
        self._explicit_sel = False
        self._update_actions()

    def _selected_rows(self):
        bits = self.sel.get_selection()
        out = []
        for i in range(bits.get_size()):
            it = self.store.get_item(bits.get_nth(i))
            if it is not None and it.pos >= 0:
                out.append((it.di, it.pos))
        return out

    def _on_sel_changed(self, *_):
        if getattr(self, "_suppress_sel", False):
            return
        self._explicit_sel = bool(self._selected_rows())
        self._update_actions()

    def _sync_save_label(self):
        if not hasattr(self, "save_btn"):
            return
        mode = self._save_mode()
        if mode == "selected":
            n = len(self._selected_rows())
            self.save_btn.set_label(ngettext("Save {n} Page…", "Save {n} Pages…", n).format(n=n))
            self.save_btn.set_tooltip_text(_("Save the selected pages as a new PDF (Ctrl+S)"))
        else:
            self.save_btn.set_label(_("Save"))
            self.save_btn.set_tooltip_text(_("Save (Ctrl+S)"))
        self.save_btn.set_visible(not self.compact)

    def _save_mode(self):
        rows = self._selected_rows() if self._explicit_sel else []
        if rows:
            total = sum(len(self.docs[di].pages) for di in {r[0] for r in rows})
            if len(rows) < total:
                return "selected"
        return "document"

    def _target_rows(self):
        """Pages a page command acts on: sidebar selection, else current page."""
        rows = self._selected_rows() if self.split.get_show_sidebar() else []
        return rows or ([(self.active, self.current)] if self.pages else [])

    def _row_setup(self, factory, item):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=6, margin_bottom=6, css_classes=["pdf-thumb"])
        header = Gtk.Box(spacing=6, css_classes=["pdf-doc-header"])
        arrow = Gtk.Image(icon_name="pan-down-symbolic")
        hl = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        header.append(arrow)
        header.append(hl)
        pic = Gtk.Picture(can_shrink=True, halign=Gtk.Align.CENTER,
                          content_fit=Gtk.ContentFit.FILL)
        label = Gtk.Label(css_classes=["dim-label", "numeric", "pageno"])
        box.append(header)
        box.append(pic)
        box.append(label)
        box._header, box._arrow, box._hl = header, arrow, hl
        box._pic, box._label, box._row, box._ref, box._pk = pic, label, None, None, None
        item.set_child(box)
        self._boxes.add(box)

        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._row_pressed, box)
        click.connect("released", self._row_released, box)
        box.add_controller(click)
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE | Gdk.DragAction.COPY)
        src.connect("prepare", self._row_drag_prepare, box)
        src.connect("drag-end", lambda s, d, delete: _DRAG.clear())
        box.add_controller(src)
        drop = Gtk.DropTarget(actions=Gdk.DragAction.MOVE | Gdk.DragAction.COPY)
        drop.set_gtypes([GObject.TYPE_STRING, Gdk.FileList])
        drop.connect("motion", self._row_drop_motion, box)
        drop.connect("leave", lambda t, b=box: self._row_drop_clear(b))
        drop.connect("drop", self._row_drop, box)
        box.add_controller(drop)

    def _row_bind(self, factory, item):
        box = item.get_child()
        ri = item.get_item()
        di, pos = ri.di, ri.pos
        box._row = (di, pos)
        if di >= len(self.docs):
            return
        d = self.docs[di]
        is_header = pos < 0
        box._header.set_visible(is_header)
        box._pic.set_visible(not is_header)
        box._label.set_visible(not is_header)
        if is_header:
            box._ref = None
            box._hl.set_label(f"{d.name}{' •' if d.dirty() else ''}  ({len(d.pages)})")
            box._arrow.set_from_icon_name("pan-down-symbolic" if d.expanded
                                          else "pan-end-symbolic")
            box.set_tooltip_text(d.path or d.name)
            box.remove_css_class("current")
            return
        if pos >= len(d.pages):
            return
        ref = d.pages[pos]
        box._ref = ref
        pk = _pk(ref)
        box._pk = pk
        w, h = self._disp_size(ref)
        box._pic.set_size_request(THUMB_W, round(THUMB_W * h / w))
        box._pic.set_paintable(self.thumbs.best(pk) or self.cache.best(pk))
        box._label.set_label(str(pos + 1))
        box.set_tooltip_text(None)
        (box.add_css_class if (di, pos) == (self.active, self.current)
         else box.remove_css_class)("current")
        self._bound[pk].add(box)
        self._queue_renders()

    def _row_unbind(self, factory, item):
        box = item.get_child()
        if box._pk is not None:
            s = self._bound.get(box._pk)
            if s is not None:
                s.discard(box)
                if not s:
                    del self._bound[box._pk]
        box._pk = None
        box._ref = None

    def _row_pressed(self, gesture, n, x, y, box):
        if box._row is None:
            return
        di, pos = box._row
        if pos < 0:
            if gesture.get_current_button() == Gdk.BUTTON_PRIMARY:
                d = self.docs[di]
                if x < 28:
                    d.expanded = not d.expanded
                    self._rebuild_store()
                elif di != self.active:
                    self._activate_doc(di, 0)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            return
        if gesture.get_current_button() == Gdk.BUTTON_SECONDARY:
            if (di, pos) not in self._selected_rows():
                idx = self._row_index(di, pos)
                if idx is not None:
                    self.sel.select_item(idx, True)
            menu = Gio.Menu()
            menu.append(_("Rotate Left"), "pdf.rotate-left")
            menu.append(_("Rotate Right"), "pdf.rotate-right")
            s = Gio.Menu()
            s.append(_("Save Selected Pages as New PDF…"), "pdf.save-selected")
            s.append(_("Delete"), "pdf.delete-pages")
            menu.append_section(None, s)
            s = Gio.Menu()
            s.append(_("Insert Blank Page"), "pdf.insert-blank")
            s.append(_("Insert Pages from File…"), "pdf.insert-file")
            menu.append_section(None, s)
            pm = Gtk.PopoverMenu.new_from_model(menu)
            pm.set_has_arrow(False)
            pm.set_parent(box)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            pm.set_pointing_to(rect)
            pm.connect("closed", lambda p: GLib.idle_add(lambda: p.unparent() and False))
            pm.popup()
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _row_released(self, gesture, n, x, y, box):
        if box._row is None or gesture.get_current_button() != Gdk.BUTTON_PRIMARY:
            return
        di, pos = box._row
        if pos < 0:
            return

        def go():
            if di != self.active:
                self._activate_doc(di, pos)
                idx = self._row_index(di, pos)
                if idx is not None:
                    self.sel.select_item(idx, True)
            self.goto_page(pos)
            self._explicit_sel = bool(self._selected_rows())
            self._update_actions()
            return False
        GLib.idle_add(go)

    def _row_drag_prepare(self, source, x, y, box):
        if box._row is None or box._row[1] < 0 or self.compact:
            return None
        sel = self._selected_rows()
        rows = sel if box._row in sel else [box._row]
        token = uuid.uuid4().hex
        refs = [self.docs[di].pages[p].copy() for di, p in rows]
        _DRAG.clear()
        _DRAG[token] = {"viewer": self, "rows": rows}
        paintable = box._pic.get_paintable()
        if paintable is not None:
            source.set_icon(paintable, int(x), int(y))
        sources = list(self.sources)
        base = self.docs[rows[0][0]]
        stem = os.path.splitext(base.name)[0]
        nums = sorted(p + 1 for _d, p in rows)
        name = f"{stem}-page{'s' if len(nums) > 1 else ''}-{_ranges(nums)}.pdf"

        # The file list is for other apps (Files, desktop): a PDF of the pages.
        # Small drags are written right away; big ones in a thread (they are
        # done long before a drop on another app can happen).
        _clean_drag_dir()
        d = os.path.join(GLib.get_user_cache_dir(), "view", "drag", token[:8])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)

        def make_file():
            try:
                pdf_ops.build_pdf(sources, refs, path, base_src=refs[0].src, keep_password=False)
            except Exception as e:
                print("drag export failed:", e)
        if len(refs) <= 12:
            make_file()
        else:
            threading.Thread(target=make_file, daemon=True).start()
        files = Gdk.FileList.new_from_list([Gio.File.new_for_path(path)])
        return Gdk.ContentProvider.new_union([
            Gdk.ContentProvider.new_for_value(DRAG_PREFIX + token),
            Gdk.ContentProvider.new_for_value(files)])

    def _row_drop_motion(self, target, x, y, box):
        if box._row is None:
            return 0
        if box._row[1] < 0:
            box.add_css_class("drop-after")
            return Gdk.DragAction.MOVE
        before = y < box.get_height() / 2
        box.remove_css_class("drop-after" if before else "drop-before")
        box.add_css_class("drop-before" if before else "drop-after")
        drag = target.get_current_drop().get_drag() if target.get_current_drop() else None
        return Gdk.DragAction.MOVE if drag is not None else Gdk.DragAction.COPY

    def _row_drop_clear(self, box):
        box.remove_css_class("drop-before")
        box.remove_css_class("drop-after")

    def _row_drop(self, target, value, x, y, box):
        self._row_drop_clear(box)
        if box._row is None:
            return False
        di, pos = box._row
        dest = 0 if pos < 0 else pos + (0 if y < box.get_height() / 2 else 1)
        return self._drop_value(value, di, dest)

    def _on_canvas_drop(self, target, value, x, y):
        return self._drop_value(value, self.active, self.current + 1)

    def _drop_value(self, value, di, dest):
        if self.compact:
            return False
        if isinstance(value, str):
            if not value.startswith(DRAG_PREFIX):
                return False
            info = _DRAG.get(value[len(DRAG_PREFIX):])
            if not info:
                return False
            src_viewer, rows = info["viewer"], info["rows"]
            if src_viewer is self:
                self.move_rows(rows, di, dest)
            else:
                self.copy_pages_from(src_viewer, rows, di, dest)
            return True
        if isinstance(value, Gdk.FileList):
            files = [f for f in value.get_files()
                     if kind_of(f) in ("pdf", "image") and f.get_path()]
            if files:
                self.insert_files(files, di, dest)
                return True
        return False

    # ========================================================== edit model ====
    def _snapshot(self):
        return ([[r.copy() for r in d.pages] for d in self.docs], self.active)

    def push_undo(self):
        self._undo.append(self._snapshot())
        del self._undo[:-100]
        self._redo.clear()
        self._update_actions()

    def _restore(self, snap):
        pages, active = snap
        for d, p in zip(self.docs, pages):
            d.pages = p
        self.active = min(active, len(self.docs) - 1)
        self.mk.select(None)
        self._changed()

    def undo(self):
        if self.mk.pad is not None:
            return
        if self._undo:
            self._redo.append(self._snapshot())
            self._restore(self._undo.pop())

    def redo(self):
        if self._redo:
            self._undo.append(self._snapshot())
            self._restore(self._redo.pop())

    def _changed(self, structure=True, select=None):
        if structure:
            self._recompute_sizes()
            self._search_reset()
            self.text_sel = None
            self.current = min(self.current, max(len(self.pages) - 1, 0))
            c = self.canvas
            c.relayout()
            c._configure(c.hv(), c.vv())
            self._rebuild_store(select)
            self._sync_page_entry()
        else:
            # thumbnails of edited pages (forms) and the dirty dot in headers
            if len(self.docs) > 1:
                for box in self._boxes:
                    if box._row and box._row[1] < 0:
                        d = self.docs[box._row[0]]
                        box._hl.set_label(f"{d.name}{' •' if d.dirty() else ''}  ({len(d.pages)})")
        self._refresh_annots()
        self.canvas.queue_draw()
        self._update_actions()
        self.refresh_title()

    def move_rows(self, rows, di, dest):
        rows = sorted(set(rows))
        moving = [self.docs[d].pages[p] for d, p in rows]
        self.push_undo()
        dset = set(rows)
        before = sum(1 for d, p in rows if d == di and p < dest)
        for d_i, d in enumerate(self.docs):
            d.pages = [r for i, r in enumerate(d.pages) if (d_i, i) not in dset]
        dest = max(0, min(dest - before, len(self.docs[di].pages)))
        if any(not d.pages for d in self.docs) and not self.docs[di].pages == []:
            pass
        self.docs[di].pages[dest:dest] = moving
        self._drop_empty_docs()
        di = min(di, len(self.docs) - 1)
        if di != self.active:
            self.active = di
        self.current = dest
        self._changed(select=[(di, dest + i) for i in range(len(moving))])
        self.goto_page(dest)

    def _drop_empty_docs(self):
        """A document whose pages were all dragged away stays (empty docs can't
        render) only if it is the last one; otherwise it is removed."""
        keep = [d for d in self.docs if d.pages]
        if keep and len(keep) != len(self.docs):
            act = self.docs[self.active]
            self.docs = keep
            self.active = keep.index(act) if act in keep else 0

    def copy_pages_from(self, other, rows, di, dest):
        refs = []
        remap = {}
        for d, p in rows:
            ref = other.docs[d].pages[p].copy()
            if ref.src not in remap:
                remap[ref.src] = self._add_source(other.sources[ref.src])
            ref.src = remap[ref.src]
            refs.append(ref)
        self.push_undo()
        self.docs[di].pages[dest:dest] = refs
        if di != self.active:
            self.active = di
        self._changed(select=[(di, dest + i) for i in range(len(refs))])
        self.toast(ngettext("Copied {n} page", "Copied {n} pages", len(refs)).format(n=len(refs)), _("Undo"), self.undo)

    def rotate_pages(self, direction):
        rows = self._target_rows()
        if not rows:
            return
        self.push_undo()
        turns = 1 if direction > 0 else 3
        for di, pos in rows:
            ref = self.docs[di].pages[pos]
            w, h = self._disp_size(ref)
            for it in ref.items:
                markup.rotate_item(it, w, h, turns)
            ref.rotation = (ref.rotation + 90 * turns) % 360
        self._changed(select=rows if self._explicit_sel else None)

    def delete_selected_pages(self, rows=None):
        rows = set(rows or self._target_rows())
        if not rows:
            return
        for di in {d for d, _p in rows}:
            if len([1 for d, _p in rows if d == di]) >= len(self.docs[di].pages) \
                    and len(self.docs) == 1:
                self.toast(_("A document needs at least one page"))
                return
        self.push_undo()
        for di, d in enumerate(self.docs):
            d.pages = [r for i, r in enumerate(d.pages) if (di, i) not in rows]
        self._drop_empty_docs()
        self.mk.select(None)
        self._changed()
        n = len(rows)
        self.toast(ngettext("Deleted {n} page", "Deleted {n} pages", n).format(n=n), _("Undo"), self.undo)

    def insert_blank(self):
        if not self.pages:
            return
        ref = self.pages[self.current]
        w, h = self._base_size(ref.src, ref.index)
        sid = self._add_source(Source(_("Blank page"), data=pdf_ops.blank_pdf_bytes(w, h)))
        self.push_undo()
        pos = self.current + 1
        self.pages.insert(pos, PageRef(sid, 0, ref.rotation))
        self._changed(select=[(self.active, pos)])
        self.goto_page(pos)

    def insert_from_file(self):
        dlg = Gtk.FileDialog(title=_("Insert Pages from File"), modal=True)
        f = Gtk.FileFilter(name=_("PDFs and images"))
        f.add_mime_type("application/pdf")
        f.add_pixbuf_formats()
        store = Gio.ListStore(item_type=Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.insert_files([files.get_item(i) for i in range(files.get_n_items())],
                              self.active, self.current + 1)
        dlg.open_multiple(self._parent(), None, done)

    def insert_files(self, files, di, at):
        if not self._loaded:
            return
        ref = self.pages[min(self.current, len(self.pages) - 1)]
        ref_w, ref_h = self._base_size(ref.src, ref.index)
        paths = [(f.get_path(), f.get_basename(), kind_of(f)) for f in files]

        def work():
            out = []
            for path, name, kind in paths:
                try:
                    if kind == "pdf":
                        out.append(Source(name, path=path))
                    else:
                        out.append(Source(name, data=pdf_ops.image_to_pdf_bytes(
                            path, ref_w, ref_h)))
                except Exception as e:
                    out.append(e)
            GLib.idle_add(finish, out)

        def finish(results):
            new_refs, errors = [], []
            for (path, name, kind), res in zip(paths, results):
                if isinstance(res, Exception):
                    errors.append(f"“{name}”: {res}")
                    continue
                try:
                    pdoc = poppler_open(res)
                except GLib.Error as e:
                    errors.append(_("“{name}” is password-protected").format(name=name)
                                  if _is_encrypted_error(e)
                                  else f"“{name}”: {e.message}")
                    continue
                sid = self._add_source(res, pdoc)
                new_refs += [PageRef(sid, i) for i in range(pdoc.get_n_pages())]
            if new_refs:
                pos = max(0, min(at, len(self.docs[di].pages)))
                self.push_undo()
                self.docs[di].pages[pos:pos] = new_refs
                self.active = di
                self._changed(select=[(di, pos + i) for i in range(len(new_refs))])
                self.goto_page(pos)
                n = len(new_refs)
                self.toast(ngettext("Inserted {n} page", "Inserted {n} pages", n).format(n=n), _("Undo"), self.undo)
            if errors:
                self.toast(_("Could not insert {errors}").format(errors="; ".join(errors)))
            return False

        threading.Thread(target=work, daemon=True).start()

    # ---- placements -----------------------------------------------------------
    def _place_item(self, pos, it, cy_frac=0.78):
        W, H = self.sizes[pos]
        it.x = (W - it.w) / 2
        cy = H * cy_frac
        vx, vy, vw, vh = self.mk_visible_rect(pos) if self.canvas.rects[pos] else (0, 0, W, H)
        if not (vy + it.h / 2 <= cy <= vy + vh - it.h / 2):
            cy = vy + vh * 0.7
        it.y = min(max(cy - it.h / 2, 0), max(H - it.h, 0))
        self.mk.add_item(pos, it)
        self.canvas.scroll_into_view(pos, it.x, it.y, it.w, it.h)
        self.canvas.grab_focus()

    def place_signature(self, sig):
        if not self._loaded or self.compact:
            return
        pos = self.current
        W, H = self.sizes[pos]
        w = min(160.0, 0.35 * W)
        self._place_item(pos, Item("sig", 0, 0, w, w * markup.sig_aspect(sig), sig=sig))

    def add_text(self):
        if not self._loaded:
            return
        pos = self.current
        text = date.today().strftime("%B %-d, %Y")
        w, h = markup.text_extent(text, self.mk.font, 12)
        it = Item("text", 0, 0, w, h, text=text, size=12, font=self.mk.font,
                  stroke=(0.05, 0.05, 0.05, 1))
        self._place_item(pos, it, cy_frac=0.86)
        GLib.idle_add(lambda: self.mk.edit_item(pos, it) and False)

    def _delete_key(self):
        if self.mk.delete_selected():
            return
        if self._explicit_sel and self._selected_rows():
            self.delete_selected_pages(self._selected_rows())

    def _escape(self):
        if self.mk.escape():
            return
        if self.text_sel:
            self.text_sel = None
            self.canvas.queue_draw()
        elif self._explicit_sel:
            self.sel.unselect_all()
        elif self.search_bar.get_search_mode():
            self.search_bar.set_search_mode(False)

    def _nudge(self, dx, dy, step):
        if self.mk.nudge(dx * step, dy * step):
            return
        c = self.canvas
        if c.layout_mode == "single" and dx and not dy:
            self.goto_page(self.current + dx)
            return
        c._configure(c.hv() + dx * 40 * step, c.vv() + dy * 40 * step)

    # ================================================================ saving ====
    def _default_name(self, doc, suffix=None):
        stem = os.path.splitext(doc.name)[0]
        if suffix is None:
            signed = any(i.kind == "sig" for r in doc.pages for i in r.items)
            suffix = "-signed" if signed else "-edited"
        return f"{stem}{suffix}.pdf"

    def save(self):
        if not self._loaded or self._busy or self.compact:
            return
        if self._save_mode() == "selected":
            self.save_selected()
            return
        d = self.doc
        if d.path is None:
            self.save_as()
            return
        if not d.dirty():
            self.toast(_("No changes to save"))
            return
        dlg = Adw.AlertDialog(
            heading=_("Overwrite “{name}”?").format(name=d.name),
            body=_("Your changes, including any signatures, will be written into the "
                 "original file. This cannot be undone. Use Save As to keep the original."),
            default_response="save", close_response="cancel")
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("save-as", _("Save As…"))
        dlg.add_response("save", _("Overwrite"))
        dlg.set_response_appearance("save", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(_d, response):
            if response == "save":
                self._write_doc(self.active, d.path)
            elif response == "save-as":
                self.save_as()
        dlg.connect("response", done)
        self._present(dlg)

    def _file_dialog(self, title, name, doc, cb, folder=False):
        dlg = Gtk.FileDialog(title=title, modal=True)
        if name:
            dlg.set_initial_name(name)
        if doc is not None and doc.path:
            dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(doc.path)))

        def done(d, res):
            try:
                g = d.select_folder_finish(res) if folder else d.save_finish(res)
            except GLib.Error:
                return
            path = g.get_path()
            if not path:
                self.toast(_("Can only save to local files"))
                return
            if not folder and not path.lower().endswith(".pdf"):
                path += ".pdf"
            cb(path)
        if folder:
            dlg.select_folder(self._parent(), None, done)
        else:
            dlg.save(self._parent(), None, done)

    def save_as(self):
        if not self._loaded or self._busy:
            return
        d = self.doc
        self._file_dialog(_("Save As"), self._default_name(d), d,
                          lambda path: self._write_doc(self.active, path))

    def save_selected(self):
        rows = self._selected_rows() or [(self.active, self.current)]
        rows.sort()
        refs = [self.docs[di].pages[p].copy() for di, p in rows]
        doc = self.docs[rows[0][0]]
        nums = [p + 1 for _d, p in rows]
        name = self._default_name(doc, f"-page{'s' if len(nums) > 1 else ''}-{_ranges(nums)}")
        self._file_dialog(_("Save Selected Pages as New PDF"), name, doc,
                          lambda path: self._write_new(path, refs, base=refs[0].src))

    def merge_documents(self):
        refs = [r.copy() for d in self.docs for r in d.pages]
        self._file_dialog(_("Merge into One PDF"), self._default_name(self.docs[0], "-merged"),
                          self.docs[0], lambda path: self._write_new(path, refs, base=refs[0].src))

    def _write_new(self, path, refs, base=0, encrypt=None, text_layers=None, then=None):
        """Write pages to a new file in a thread; toast with Open."""
        sources = list(self.sources)
        self._busy = True
        self._update_actions()

        def work():
            err = None
            try:
                pdf_ops.build_pdf(sources, refs, path, base_src=base, keep_password=False,
                                  encrypt=encrypt, text_layers=text_layers)
                if then:
                    then(path)
            except Exception as e:
                import traceback
                traceback.print_exc()
                err = e
            GLib.idle_add(finish, err)

        def finish(err):
            self._busy = False
            self._update_actions()
            name = os.path.basename(path)
            if err is not None:
                self.toast(_("Could not save “{name}”: {err}").format(name=name, err=err))
            else:
                g = Gio.File.new_for_path(path)
                self.toast(_("Saved “{name}”").format(name=name), _("Open"), lambda *_: self.window.open_files([g]))
            return False
        threading.Thread(target=work, name="pdf-save", daemon=True).start()

    def _write_doc(self, di, path, done=None):
        """Save document di to path (in a worker thread), then show the saved file."""
        d = self.docs[di]
        sources = list(self.sources)
        refs = [r.copy() for r in d.pages]
        # pages elsewhere that still come from the file we overwrite: keep its bytes
        for sid, s in enumerate(self.sources):
            if s.path and os.path.abspath(s.path) == os.path.abspath(path):
                used = any(r.src == sid for o in self.docs if o is not d for r in o.pages)
                if used and s.data is None:
                    try:
                        with open(s.path, "rb") as fh:
                            s.data = fh.read()
                    except OSError:
                        pass
        keep = self.current
        self._busy = True
        self._update_actions()

        def work():
            err = None
            try:
                pdf_ops.build_pdf(sources, refs, path, base_src=d.src)
            except Exception as e:
                import traceback
                traceback.print_exc()
                err = e
            GLib.idle_add(finish, err)

        def finish(err):
            self._busy = False
            name = os.path.basename(path)
            if err is not None:
                self._update_actions()
                self.toast(_("Could not save “{name}”: {err}").format(name=name, err=err))
                if done:
                    done(False)
                return False
            src = Source(name, path=path, password=self.sources[d.src].password)
            try:
                pdoc = poppler_open(src)
            except GLib.Error as e:
                self.toast(_("Saved, but could not reopen “{name}”: {message}").format(name=name, message=e.message))
                if done:
                    done(True)
                return False
            sid = self._add_source(src, pdoc)
            d.src, d.path, d.name = sid, path, name
            d.gfile = Gio.File.new_for_path(path)
            d.pages = [PageRef(sid, i) for i in range(pdoc.get_n_pages())]
            d.clean_key = d.key()
            self._undo.clear()
            self._redo.clear()
            if di == self.active:
                self.current = min(keep, len(d.pages) - 1)
            self._changed()
            gfile = d.gfile
            self.toast(_("Saved “{name}”").format(name=name), _("Show in Files"), lambda *_: self._show_in_files(gfile))
            if done:
                done(True)
            return False

        threading.Thread(target=work, name="pdf-save", daemon=True).start()

    def _show_in_files(self, gfile):
        try:
            Gtk.FileLauncher(file=gfile).open_containing_folder(self._parent(), None, None)
        except Exception as e:
            self.toast(_("Could not open the file manager: {e}").format(e=e))

    # ================================================================ export ====
    def export_images(self):
        d = self.doc
        fmt = Gtk.DropDown(model=Gtk.StringList.new(["PNG", "JPEG", "TIFF"]), selected=0)
        dpi = Gtk.SpinButton(adjustment=Gtk.Adjustment(value=150, lower=36, upper=1200,
                                                       step_increment=25), numeric=True)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8, halign=Gtk.Align.CENTER)
        grid.attach(Gtk.Label(label=_("Format"), xalign=1), 0, 0, 1, 1)
        grid.attach(fmt, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("Resolution (dpi)"), xalign=1), 0, 1, 1, 1)
        grid.attach(dpi, 1, 1, 1, 1)
        rows = self._selected_rows() if self._explicit_sel else []
        refs = ([self.docs[a].pages[b].copy() for a, b in sorted(rows)] if rows
                else [r.copy() for r in self.pages])
        dlg = Adw.AlertDialog(heading=_("Export as Images"),
                              body=ngettext(
                                  "Each of the {n} page becomes one image file in the folder you choose.",
                                  "Each of the {n} pages becomes one image file in the folder you choose.",
                                  len(refs)).format(n=len(refs)),
                              extra_child=grid, default_response="export",
                              close_response="cancel")
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("export", _("Choose Folder…"))
        dlg.set_response_appearance("export", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "export":
                return
            f = ["png", "jpeg", "tiff"][fmt.get_selected()]
            res = int(dpi.get_value())
            self._file_dialog(_("Export to Folder"), None, d,
                              lambda folder: self._run_export(folder, refs, f, res), folder=True)
        dlg.connect("response", resp)
        self._present(dlg)

    def _run_export(self, folder, refs, fmt, dpi):
        sources = list(self.sources)
        stem = os.path.splitext(self.doc.name)[0]

        def work():
            try:
                paths = pdf_ops.export_images(sources, refs, folder, stem, fmt, dpi)
                GLib.idle_add(lambda: self.toast(
                    ngettext("Exported {n} image", "Exported {n} images", len(paths)).format(n=len(paths)),
                    _("Show in Files"), lambda *_: self._show_in_files(
                        Gio.File.new_for_path(paths[0]))) and False)
            except Exception as e:
                GLib.idle_add(lambda: self.toast(_("Export failed: {e}").format(e=e)) and False)
        self.toast(_("Exporting…"))
        threading.Thread(target=work, daemon=True).start()

    def export_encrypted(self):
        d = self.doc
        user = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text=_("Password to open"))
        owner = Gtk.PasswordEntry(show_peek_icon=True,
                                  placeholder_text=_("Owner password (optional)"))
        checks = {k: Gtk.CheckButton(label=lbl, active=True) for k, lbl in (
            ("allow_print", _("Allow printing")),
            ("allow_copy", _("Allow copying text and images")),
            ("allow_modify", _("Allow changes and page assembly")),
            ("allow_annotate", _("Allow comments and form filling")))}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(user)
        box.append(owner)
        for c in checks.values():
            box.append(c)
        dlg = Adw.AlertDialog(heading=_("Export with Password"),
                              body=_("The new PDF is encrypted with AES-256. Permissions are "
                                   "enforced by the owner password."),
                              extra_child=box, default_response="export", close_response="cancel")
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("export", _("Save As…"))
        dlg.set_response_appearance("export", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "export":
                return
            if not user.get_text() and not owner.get_text():
                self.toast(_("Enter a password"))
                return
            enc = {"user": user.get_text(), "owner": owner.get_text() or None}
            enc.update({k: c.get_active() for k, c in checks.items()})
            refs = [r_.copy() for r_ in d.pages]
            self._file_dialog(_("Export with Password"), self._default_name(d, "-protected"), d,
                              lambda path: self._write_new(path, refs, d.src, encrypt=enc))
        dlg.connect("response", resp)
        self._present(dlg)

    def reduce_size(self):
        d = self.doc
        if not pdf_ops.ghostscript():
            self.toast(_("Reduce File Size needs Ghostscript (gs)"))
            return
        refs = [r.copy() for r in d.pages]
        sources = list(self.sources)

        def chosen(path):
            def work():
                err = None
                try:
                    tmp = path + ".src.part"
                    pdf_ops.build_pdf(sources, refs, tmp, base_src=d.src, keep_password=False)
                    before = os.path.getsize(d.path) if d.path else os.path.getsize(tmp)
                    pdf_ops.reduce_size(tmp, path)
                    os.unlink(tmp)
                    after = os.path.getsize(path)
                except Exception as e:
                    err, before, after = e, 0, 0
                GLib.idle_add(lambda: self._reduced(path, err, before, after) and False)
            self.toast(_("Reducing file size…"))
            threading.Thread(target=work, daemon=True).start()
        self._file_dialog(_("Reduce File Size"), self._default_name(d, "-reduced"), d, chosen)

    def _reduced(self, path, err, before, after):
        if err:
            self.toast(_("Could not reduce the file: {err}").format(err=err))
            return
        if before and after >= before:
            try:
                os.unlink(path)
            except OSError:
                pass
            self.toast(_("This PDF is already compact — nothing to reduce"))
            return
        g = Gio.File.new_for_path(path)
        self.toast(_("Saved {v} (was {v2})").format(v=_human(after), v2=_human(before)), _("Open"),
                   lambda *_: self.window.open_files([g]))

    def export_searchable(self):
        ocr = _ocr_module()
        if ocr is None:
            if _ocr_offerable():
                from . import ocr_install
                ocr_install.offer(self, self.export_searchable)
            else:
                self.toast(_("Text recognition is not available"))
            return
        d = self.doc
        refs = [r.copy() for r in d.pages]
        sources = list(self.sources)
        need = [i for i, r in enumerate(refs) if not self._page_has_text(r)
                and (r.src, r.index) not in self.ocr_lines]
        known = dict(self.ocr_lines)

        def chosen(path):
            def then(_p):
                pass

            def work():
                docs = {}
                lines = dict(known)
                for i in need:
                    r = refs[i]
                    k = (r.src, r.index)
                    try:
                        lines[k] = _ocr_page(sources, docs, r.src, r.index, ocr)
                    except Exception as e:
                        print("OCR failed:", e)
                layers = {}
                for i, r in enumerate(refs):
                    k = (r.src, r.index)
                    if k in lines and lines[k]:
                        layers[i] = _ocr_to_display(lines[k], r, self._base_size_threadsafe(
                            sources, docs, r))
                GLib.idle_add(lambda: (self.ocr_lines.update(lines), self._write_new(
                    path, refs, d.src, text_layers=layers)) and False)
            self.toast(ngettext("Recognizing text on {n} page…", "Recognizing text on {n} pages…", len(need)).format(n=len(need))
                       if need else _("Writing searchable PDF…"))
            threading.Thread(target=work, daemon=True).start()
        self._file_dialog(_("Export as Searchable PDF"), self._default_name(d, "-searchable"), d,
                          chosen)

    def _base_size_threadsafe(self, sources, docs, ref):
        doc = docs.get(ref.src)
        if doc is None:
            doc = docs[ref.src] = poppler_open(sources[ref.src])
        return doc.get_page(ref.index).get_size()

    # ================================================================ search ====
    def _find(self):
        if self.compact:
            return
        self.search_bar.set_search_mode(True)
        self.search_entry.grab_focus()

    def _on_search_mode(self, bar, _p):
        if not bar.get_search_mode():
            self._search_reset()
            self.canvas.grab_focus()

    def _search_reset(self):
        if self._search:
            self._search["cancel"] = True
        self._search = None
        if hasattr(self, "search_label"):
            self.search_label.set_label("")
        if hasattr(self, "canvas"):
            self.canvas.queue_draw()

    def _on_search_changed(self, entry):
        self._search_reset()
        text = entry.get_text()
        if not text or not self._loaded:
            return
        st = self._search = {"text": text, "results": {}, "cur": None, "count": 0,
                             "done": False, "cancel": False}
        n = len(self.pages)
        order = [(self.current + i) % n for i in range(n)]
        refs = [(pos, self.pages[pos].copy()) for pos in order]
        sources = list(self.sources)
        ocr = {k: v for k, v in self.ocr_lines.items()}
        sizes = {pos: self._base_size(r.src, r.index) for pos, r in refs}

        def work():
            docs = {}
            batch = {}
            last = time.monotonic()
            for pos, ref in refs:
                if st["cancel"]:
                    return
                try:
                    found = _find_on_page(sources, docs, ref, text, ocr, sizes[pos])
                except Exception:
                    found = []
                if found:
                    batch[pos] = found
                if batch and time.monotonic() - last > 0.1:
                    GLib.idle_add(deliver, dict(batch), False)
                    batch.clear()
                    last = time.monotonic()
            GLib.idle_add(deliver, batch, True)

        def deliver(found, done):
            if self._search is not st:
                return False
            for pos, rects in found.items():
                st["results"][pos] = rects
                st["count"] += len(rects)
                if st["cur"] is None:
                    st["cur"] = (pos, 0)
                    self._show_match()
            st["done"] = done
            self._update_search_label()
            self.canvas.queue_draw()
            return False

        self.search_label.set_label(_("Searching…"))
        threading.Thread(target=work, name="pdf-search", daemon=True).start()

    def _update_search_label(self):
        st = self._search
        if not st:
            self.search_label.set_label("")
            return
        if st["count"] == 0:
            self.search_label.set_label(_("No matches") if st["done"] else _("Searching…"))
            return
        idx = 0
        for pos in sorted(st["results"]):
            if st["cur"] and pos == st["cur"][0]:
                idx += st["cur"][1] + 1
                break
            idx += len(st["results"][pos])
        self.search_label.set_label(f"{idx} of {st['count']}" + ("" if st["done"] else "…"))

    def _search_step(self, d):
        st = self._search
        if not st or not st["results"]:
            return
        flat = [(pos, k) for pos in sorted(st["results"]) for k in range(len(st["results"][pos]))]
        i = flat.index(st["cur"]) if st["cur"] in flat else -1
        st["cur"] = flat[(i + d) % len(flat)]
        self._show_match()
        self._update_search_label()

    def _show_match(self):
        st = self._search
        pos, k = st["cur"]
        x, y, w, h = st["results"][pos][k]
        if self.canvas.layout_mode == "single" and pos != self.current:
            self.goto_page(pos)
        self.canvas.scroll_into_view(pos, x, y, w, h)
        self.canvas.queue_draw()

    # ================================================================ print ====
    def print_doc(self):
        if not self._loaded:
            return
        refs = [r.copy() for r in self.pages]
        sizes = list(self.sizes)
        op = Gtk.PrintOperation(n_pages=len(refs), unit=Gtk.Unit.POINTS,
                                job_name=self.get_title(), allow_async=True,
                                embed_page_setup=True)

        def setup(op, ctx, n, ps):
            w, h = sizes[n]
            ps.set_orientation(Gtk.PageOrientation.LANDSCAPE if w > h
                               else Gtk.PageOrientation.PORTRAIT)

        def draw(op, ctx, n):
            from .pdf_render import apply_rotation
            ref = refs[n]
            dw, dh = sizes[n]
            cr = ctx.get_cairo_context()
            cw, ch = ctx.get_width(), ctx.get_height()
            s = min(cw / dw, ch / dh)
            cr.translate((cw - dw * s) / 2, (ch - dh * s) / 2)
            cr.scale(s, s)
            cr.save()
            apply_rotation(cr, ref.rotation, dw, dh)
            page = self._pdocs[ref.src].get_page(ref.index)
            if ref.forms:
                orig = {}
                pdf_ops.apply_forms(page, ref.forms, orig)
                page.render_for_printing(cr)
                pdf_ops.apply_forms(page, orig)
            else:
                page.render_for_printing(cr)
            cr.restore()
            markup.render_items(cr, ref.items)

        op.connect("request-page-setup", setup)
        op.connect("draw-page", draw)
        try:
            op.run(Gtk.PrintOperationAction.PRINT_DIALOG, self._parent())
        except GLib.Error as e:
            self.toast(_("Printing failed: {message}").format(message=e.message))

    # ========================================================= side panels ====
    def _on_side_view(self, *_):
        name = self.side_toggle.get_active_name() or "thumbs"
        self.side_stack.set_visible_child_name(name)
        self._refresh_side_lists()
        self._queue_renders()

    def _refresh_side_lists(self):
        name = self.side_stack.get_visible_child_name()
        if name == "toc":
            self._refresh_toc()
        elif name == "annots":
            self._refresh_annots(force=True)

    def _refresh_toc(self):
        lb = self.toc_list
        while (row := lb.get_row_at_index(0)) is not None:
            lb.remove(row)
        d = self.doc
        if d is None:
            return
        pdoc = self._pdocs[d.src]
        entries = []
        try:
            it = Poppler.IndexIter.new(pdoc)
        except Exception:
            it = None

        def walk(it, depth):
            while it is not None:
                act = it.get_action()
                title = getattr(getattr(act, "any", None), "title", None) or ""
                page = None
                if act is not None and act.type == Poppler.ActionType.GOTO_DEST:
                    dest = act.goto_dest.dest
                    if dest is not None and dest.type == Poppler.DestType.NAMED:
                        dest = pdoc.find_dest(dest.named_dest)
                    if dest is not None:
                        page = dest.page_num - 1
                entries.append((depth, title, page))
                child = it.get_child()
                if child is not None and depth < 6:
                    walk(child, depth + 1)
                if not it.next():
                    break

        if it is not None:
            walk(it, 0)
        if not entries:
            lb.append(Gtk.Label(label=_("No table of contents"), css_classes=["dim-label"],
                                margin_top=12))
            return
        for depth, title, page in entries:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=6, margin_start=6 + depth * 14)
            box.append(Gtk.Label(label=title, xalign=0, hexpand=True, ellipsize=3,
                                 css_classes=[] if depth else ["heading"]))
            if page is not None:
                box.append(Gtk.Label(label=str(page + 1), css_classes=["dim-label", "numeric"]))
            row.set_child(box)
            row._target = (d.src, page)
            lb.append(row)

    def _on_toc_row(self, lb, row):
        tgt = getattr(row, "_target", None)
        if not tgt or tgt[1] is None:
            return
        src, idx = tgt
        for pos, r in enumerate(self.pages):
            if r.src == src and r.index == idx:
                self.goto_page(pos)
                return

    def _refresh_annots(self, force=False):
        if not force and self.side_stack.get_visible_child_name() != "annots":
            return
        lb = self.annot_list
        while (row := lb.get_row_at_index(0)) is not None:
            lb.remove(row)
        names = {"highlight": _("Highlight"), "underline": _("Underline"),
                 "strike": _("Strikethrough"), "note": _("Note"), "text": _("Text"),
                 "sig": _("Signature")}
        n = 0
        for pos, ref in enumerate(self.pages):
            for it in ref.items:
                if it.kind not in names:
                    continue
                n += 1
                self._annot_row(_("{kind} · page {page}").format(kind=names[it.kind], page=pos + 1),
                                it.text.strip().replace("\n", " ")[:80], (pos, it.id))
            try:
                for m in self._ppage(ref).get_annot_mapping():
                    a = m.annot
                    t = a.get_annot_type()
                    if t in (Poppler.AnnotType.LINK, Poppler.AnnotType.WIDGET,
                             Poppler.AnnotType.POPUP):
                        continue
                    n += 1
                    label = _(t.value_nick.replace("-", " ").title())
                    self._annot_row(_("{label} · page {page}").format(label=label, page=pos + 1),
                                    (a.get_contents() or "").strip()[:80], (pos, None))
            except Exception:
                pass
        if n == 0:
            lb.append(Gtk.Label(label=_("No highlights or notes"), css_classes=["dim-label"],
                                margin_top=12))

    def _annot_row(self, title, text, target):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=6, margin_end=6,
                      margin_top=4, margin_bottom=4)
        box.append(Gtk.Label(label=title, xalign=0, css_classes=["caption-heading"]))
        if text:
            box.append(Gtk.Label(label=text, xalign=0, wrap=True, lines=2, ellipsize=3))
        row.set_child(box)
        row._target = target
        self.annot_list.append(row)

    def _on_annot_row(self, lb, row):
        tgt = getattr(row, "_target", None)
        if not tgt:
            return
        pos, iid = tgt
        self.goto_page(pos)
        if iid is not None:
            for it in self.pages[pos].items:
                if it.id == iid:
                    x, y, w, h = markup.bbox(it)
                    self.canvas.scroll_into_view(pos, x, y, w, h)
            self.mk.select((pos, iid))

    # ================================================================= info ====
    def get_info(self):
        d = self.doc
        if d is None:
            return []
        out = []
        if d.gfile is not None:
            out.append(file_info_section(d.gfile))
        pdoc = self._pdocs[d.src]
        rows = []

        def add(label, value):
            if value not in (None, ""):
                rows.append((label, str(value)))

        add(_("Title"), pdoc.get_title())
        add(_("Author"), pdoc.get_author())
        add(_("Subject"), pdoc.get_subject())
        add(_("Keywords"), pdoc.get_keywords())
        add(_("Creator"), pdoc.get_creator())
        add(_("Encoding software"), pdoc.get_producer())
        add(_("PDF version"), pdoc.get_pdf_version_string())

        def dt(t):
            if not t or t < 0:
                return None
            return GLib.DateTime.new_from_unix_local(t).format("%x, %X")
        add(_("Created"), dt(pdoc.get_creation_date()))
        add(_("Modified"), dt(pdoc.get_modification_date()))
        add(_("Pages"), len(d.pages))
        if self.pages:
            w, h = self.sizes[self.current]
            paper = pdf_ops.paper_name(w, h)
            add(_("Page size"), f"{w:.0f} × {h:.0f} pt ({w * 25.4 / 72:.0f} × {h * 25.4 / 72:.0f} mm, "
                             f"{w / 72:.2f} × {h / 72:.2f} in)" + (f" — {paper}" if paper else ""))
        src = self.sources[d.src]
        try:
            reader = self._reader(d.src)
            enc = reader.is_encrypted
        except Exception:
            reader, enc = None, False
        if enc or src.password:
            perms = pdoc.get_permissions()
            P = Poppler.Permissions
            allowed = [n for n, f in ((_("printing"), P.OK_TO_PRINT), (_("copying"), P.OK_TO_COPY),
                                      (_("modifying"), P.OK_TO_MODIFY),
                                      (_("annotating"), P.OK_TO_ADD_NOTES),
                                      (_("form filling"), P.OK_TO_FILL_FORM)) if perms & f]
            add(_("Security"), _("Encrypted") + (_(" (password to open)") if src.password else "")
                + (_(" — allows {list}").format(list=", ".join(allowed)) if allowed
                   else _(" — no permissions")))
        else:
            add(_("Security"), _("None"))
        add(_("Linearized (fast web view)"), _("Yes") if pdoc.is_linearized() else _("No"))
        if reader is not None:
            try:
                mi = reader.trailer["/Root"].get("/MarkInfo")
                add(_("Tagged"), _("Yes") if mi and mi.get_object().get("/Marked") else _("No"))
            except Exception:
                pass
            try:
                af = reader.trailer["/Root"].get("/AcroForm")
                if af is not None:
                    add(_("Form fields"), len(af.get_object().get("/Fields", [])))
            except Exception:
                pass
        try:
            fi = Poppler.FontInfo.new(pdoc)
            names = set()
            ok, it = fi.scan(min(pdoc.get_n_pages(), 40))
            while ok and it is not None:
                n = it.get_name()
                if n:
                    names.add(n.split("+")[-1] + ("" if it.is_embedded() else _(" (not embedded)")))
                if not it.next():
                    break
            if names:
                add(_("Fonts"), ", ".join(sorted(names)[:25]) + (" …" if len(names) > 25 else ""))
        except Exception:
            pass
        out.append((_("Document"), rows))
        if len(self.docs) > 1:
            out.append((_("Window"), [(_("Documents"), ", ".join(x.name for x in self.docs))]))
        return out


# ==================================================================== helpers

def _ranges(nums):
    nums = sorted(set(nums))
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    s = "_".join(out)
    return s if len(s) < 40 else f"{nums[0]}-{nums[-1]}"


def _clean_drag_dir():
    d = os.path.join(GLib.get_user_cache_dir(), "view", "drag")
    try:
        now = time.time()
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if now - os.path.getmtime(p) > 3600:
                for f in os.listdir(p):
                    os.unlink(os.path.join(p, f))
                os.rmdir(p)
    except OSError:
        pass


def _ocr_page(sources, docs, src, index, ocr, dpi=200):
    """OCR one page; returns [(poly, text)] in Poppler display-frame points."""
    import cairo
    import numpy as np
    from PIL import Image
    doc = docs.get(src)
    if doc is None:
        doc = docs[src] = poppler_open(sources[src])
    page = doc.get_page(index)
    w, h = page.get_size()
    s = dpi / 72
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, round(w * s)), max(1, round(h * s)))
    cr = cairo.Context(surf)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    cr.scale(s, s)
    page.render_for_printing(cr)
    surf.flush()
    a = np.frombuffer(surf.get_data(), np.uint8).reshape(
        surf.get_height(), surf.get_stride() // 4, 4)[:, :surf.get_width(), :]
    im = Image.fromarray(a[..., [2, 1, 0]].copy(), "RGB")
    lines = ocr.recognize(im)
    return [([(px / s, py / s) for px, py in ln.box], ln.text) for ln in lines if ln.text]


def _ocr_to_display(lines, ref, base_size):
    pw, ph = base_size
    turns = ref.rotation // 90
    return [([rotate_point(x, y, pw, ph, turns)[:2] for x, y in poly], text)
            for poly, text in lines]


def _find_on_page(sources, docs, ref, text, ocr_lines, base_size):
    """Search one page (own Poppler doc: runs in the search thread)."""
    doc = docs.get(ref.src)
    if doc is None:
        doc = docs[ref.src] = poppler_open(sources[ref.src])
    page = doc.get_page(ref.index)
    W, H = base_size
    turns = ref.rotation // 90
    out = []
    k = (ref.src, ref.index)
    if k in ocr_lines:
        t = text.lower()
        for poly, line in ocr_lines[k]:
            low = line.lower()
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            i = low.find(t)
            while i >= 0 and line:
                a = x0 + (x1 - x0) * i / len(line)
                b = x0 + (x1 - x0) * (i + len(t)) / len(line)
                out.append((a, y0, b, y1))
                i = low.find(t, i + 1)
    else:
        try:
            found = page.find_text_with_options(text, Poppler.FindFlags.DEFAULT)
        except Exception:
            found = page.find_text(text)
        for r in found or []:
            out.append((r.x1, H - r.y2, r.x2, H - r.y1))
    rects = []
    for x1, y1, x2, y2 in out:
        a = rotate_point(x1, y1, W, H, turns)
        b = rotate_point(x2, y2, W, H, turns)
        rects.append((min(a[0], b[0]), min(a[1], b[1]), abs(a[0] - b[0]), abs(a[1] - b[1])))
    rects.sort(key=lambda q: (round(q[1]), q[0]))
    return rects
