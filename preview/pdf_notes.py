"""Margin comments for sticky notes, like Google Docs.

On the page a note is only its icon (a markup "note" Item); its text lives in
a card in a strip to the right of the pages, level with the icon. Cards stack
so they never overlap; the selected one sits exactly at its icon and pushes
the others up and down. Click a card to edit it in place (Ctrl+Enter or
leaving it keeps the text, Escape drops the change).

Host: the PdfViewer (pages, canvas, mk, compact, push_undo, _changed).
"""
from __future__ import annotations

import datetime
import re
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk, Pango

from .i18n import _, ngettext

PANEL_W = 264
PAD = 10            # card inset from the strip's edges
GAP = 8             # between stacked cards

CSS = """
.pdf-notes { background: var(--view-bg-color); border-left: 1px solid var(--border-color); }
.pdf-note-card { padding: 8px 10px; }
.pdf-note-card.selected { box-shadow: 0 0 0 2px var(--accent-bg-color); }
.pdf-note-card textview, .pdf-note-card textview text { background: transparent; }
"""

_DATE = re.compile(r"(?:D:)?(\d{4})(\d\d)?(\d\d)?(\d\d)?(\d\d)?(\d\d)?([Zz+-])?(\d\d)?'?(\d\d)?")


def parse_pdf_date(s):
    """PDF date string (D:YYYYMMDDHHmmSS+HH'mm') -> epoch seconds, or None."""
    m = _DATE.match((s or "").strip())
    if not m:
        return None
    g = m.groups()
    parts = [int(v) if v else d for v, d in zip(g[:6], (0, 1, 1, 0, 0, 0))]
    sign = g[6]
    if sign in ("+", "-"):
        off = datetime.timedelta(hours=int(g[7] or 0), minutes=int(g[8] or 0))
        tz = datetime.timezone(off if sign == "+" else -off)
    elif sign in ("Z", "z"):
        tz = datetime.timezone.utc
    else:
        tz = None                           # no zone: local time
    try:
        return datetime.datetime(*parts, tzinfo=tz).timestamp()
    except (ValueError, OverflowError):
        return None


def when(ts, now=None):
    """Short relative time, like Google Docs' comment headers."""
    if ts is None:
        return ""
    now = time.time() if now is None else now
    d = now - ts
    if d < 60:
        return _("Just now")
    if d < 3600:
        n = int(d // 60)
        return ngettext("{n} minute ago", "{n} minutes ago", n).format(n=n)
    if d < 86400:
        n = int(d // 3600)
        return ngettext("{n} hour ago", "{n} hours ago", n).format(n=n)
    lt, nt = time.localtime(ts), time.localtime(now)
    fmt = "%b %d" if lt.tm_year == nt.tm_year else "%b %d, %Y"
    return time.strftime(fmt, lt)


class _Card(Gtk.Box):
    def __init__(self, panel, pos, iid):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                         css_classes=["card", "pdf-note-card"])
        self.panel, self.pos, self.iid = panel, pos, iid
        self.editing = False
        head = Gtk.Box(spacing=6)
        self.author = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True,
                                css_classes=["heading"])
        self.time = Gtk.Label(xalign=1, css_classes=["dim-label", "caption"])
        self.delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=_("Delete Note"),
                                 css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
        self.delete.connect("clicked", lambda *_: panel.delete(self))
        head.append(self.author)
        head.append(self.time)
        head.append(self.delete)
        self.append(head)
        self.label = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                               natural_wrap_mode=Gtk.NaturalWrapMode.WORD)
        self.view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.stack = Gtk.Stack(vhomogeneous=False, interpolate_size=False)
        self.stack.add_named(self.label, "view")
        self.stack.add_named(self.view, "edit")
        self.append(self.stack)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: panel.clicked(self))
        self.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.view.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self.finish(True))
        self.view.add_controller(focus)

    def show_item(self, it, selected, editable):
        m = it.meta or {}
        author = m.get("author") or self.panel.local_author()
        if m.get("reply"):
            author = _("{author} (reply)").format(author=author)
        self.author.set_label(author)
        if m:
            ts = parse_pdf_date(m.get("date"))
            stamp = when(ts) if it.text == m.get("text") else _("Edited")
        else:
            stamp = when(self.panel.first_seen(it.id))
        self.time.set_label(stamp)
        if not self.editing:
            self.label.set_label(it.text or _("Empty note"))
            (self.label.remove_css_class if it.text else self.label.add_css_class)("dim-label")
        self.delete.set_visible(editable)
        (self.add_css_class if selected else self.remove_css_class)("selected")

    def begin_edit(self, it):
        if not self.editing:
            self.editing = True
            self.view.get_buffer().set_text(it.text or "")
            self.stack.set_visible_child_name("edit")
        buf = self.view.get_buffer()
        buf.place_cursor(buf.get_end_iter())
        self.view.grab_focus()

    def finish(self, keep):
        if not self.editing:
            return
        self.editing = False
        buf = self.view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        self.stack.set_visible_child_name("view")
        self.panel.commit(self, text if keep else None)

    def _on_key(self, ctrl, keyval, code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            self.finish(True)
            self.panel.v.canvas.grab_focus()
            return True
        if keyval == Gdk.KEY_Escape:
            self.finish(False)
            self.panel.v.canvas.grab_focus()
            return True
        return False


class NotesPanel(Gtk.Widget):
    """The strip of note cards; lives right of the page scroller."""

    __gtype_name__ = "PdfNotesPanel"

    def __init__(self, viewer):
        super().__init__(css_classes=["pdf-notes"], overflow=Gtk.Overflow.HIDDEN,
                         visible=False)
        self.v = viewer
        self.cards = {}                 # item id -> _Card
        self.place = {}                 # item id -> y
        self._idle = 0
        self._seen = {}                 # item id -> first time seen (View's own notes)
        self._author = None
        _install_css()

    # ------------------------------------------------------------ helpers ----
    def local_author(self):
        if self._author is None:
            from .pdf_ops import _author
            self._author = _author() or _("You")
        return self._author

    def first_seen(self, iid):
        return self._seen.setdefault(iid, time.time())

    def _item(self, pos, iid):
        pages = self.v.pages
        if 0 <= pos < len(pages):
            for it in pages[pos].items:
                if it.id == iid and it.kind == "note":
                    return it
        return None

    # ------------------------------------------------------------- layout ----
    def queue_update(self):
        if not self._idle:
            self._idle = GLib.idle_add(self._idle_update)

    def _idle_update(self):
        self._idle = 0
        self.update()
        return False

    def update(self):
        v = self.v
        c = v.canvas
        pages = v.pages if getattr(v, "_loaded", False) else []
        show = bool(pages) and (v.mk.tool == "note" or any(
            it.kind == "note" for ref in pages for it in ref.items))
        if show != self.get_visible():
            self.set_visible(show)
        want = []
        if show and c.rects:
            for pos in c.visible_range():
                for it in pages[pos].items:
                    if it.kind == "note":
                        wx, wy = c.doc_to_widget(pos, it.x, it.y)
                        want.append((wy, wx, pos, it))
        ids = {it.id for _y, _x, _p, it in want}
        for iid, card in list(self.cards.items()):     # keep the card being typed in
            if iid not in ids:
                it = self._item(card.pos, iid) if card.editing else None
                if it is not None:
                    wx, wy = c.doc_to_widget(card.pos, it.x, it.y)
                    want.append((wy, wx, card.pos, it))
                    continue
                card.unparent()
                del self.cards[iid]
        want.sort(key=lambda w: (w[0], w[1]))
        sel = v.mk.selected
        editable = not v.compact and v.mk.enabled
        width = max(self.get_width(), PANEL_W) - 2 * PAD
        rows = []
        for wy, _wx, pos, it in want:
            card = self.cards.get(it.id)
            if card is None:
                card = self.cards[it.id] = _Card(self, pos, it.id)
                card.set_parent(self)
            card.pos = pos
            card.show_item(it, sel == (pos, it.id), editable)
            h = card.measure(Gtk.Orientation.VERTICAL, width)[1]
            rows.append([wy - 4, h, card])
        # stack: the selected card sits at its icon, the others make room
        s = next((i for i, r in enumerate(rows) if sel == (r[2].pos, r[2].iid)), None)
        if s is None:
            s = 0
        for i in range(s + 1, len(rows)):
            rows[i][0] = max(rows[i][0], rows[i - 1][0] + rows[i - 1][1] + GAP)
        for i in range(s - 1, -1, -1):
            rows[i][0] = min(rows[i][0], rows[i + 1][0] - GAP - rows[i][1])
        self.place = {r[2].iid: (r[0], r[1]) for r in rows}
        self.queue_allocate()

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return PANEL_W, PANEL_W, -1, -1
        return 0, 0, -1, -1

    def do_size_allocate(self, width, height, baseline):
        w = width - 2 * PAD
        for iid, card in self.cards.items():
            y, h = self.place.get(iid, (-10000, 0))
            h = max(h, card.measure(Gtk.Orientation.VERTICAL, w)[0])
            card.set_child_visible(y < height and y + h > 0)
            pt = Graphene.Point()
            pt.init(PAD, round(y))
            card.allocate(w, round(h), -1, Gsk.Transform.new().translate(pt))

    def do_dispose(self):
        if self._idle:
            GLib.source_remove(self._idle)
            self._idle = 0
        for card in self.cards.values():
            card.unparent()
        self.cards = {}
        Gtk.Widget.do_dispose(self)

    # -------------------------------------------------------------- actions ---
    def focus_note(self, pos, iid):
        """Open a note's card for typing (double-click on its icon, a new note).
        False when there is no card to type in (Quick Look)."""
        v = self.v
        it = self._item(pos, iid)
        if it is None or v.compact or not v.mk.enabled:
            return False
        v.mk.select((pos, iid))
        self.update()
        card = self.cards.get(iid)
        if card is None:
            return False
        card.begin_edit(it)
        return True

    def clicked(self, card):
        v = self.v
        it = self._item(card.pos, card.iid)
        if it is None:
            return
        v.mk.select((card.pos, card.iid))
        v.canvas.scroll_into_view(card.pos, it.x, it.y, it.w, it.h)
        if not v.compact and v.mk.enabled:
            card.begin_edit(it)
        self.queue_update()

    def commit(self, card, text):
        it = self._item(card.pos, card.iid)
        if it is None or text is None or text == it.text:
            self.queue_update()
            return
        self.v.push_undo()
        it.text = text
        self.v._changed(structure=False)

    def delete(self, card):
        v = self.v
        if self._item(card.pos, card.iid) is None:
            return
        card.editing = False
        v.push_undo()
        items = v.pages[card.pos].items
        items[:] = [i for i in items if i.id != card.iid]
        if v.mk.selected == (card.pos, card.iid):
            v.mk.select(None)
        v._changed(structure=False)


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
