"""Translating a selection: a floating card over the viewer.

    show(parent, text, rect=None, side_by_side=None)

``parent`` is the widget the selection is in, ``rect`` the selection's
bounds in its coordinates (a Gdk.Rectangle: rect() / bounds_rect()), and
``side_by_side`` what "Open Side by Side" does (the viewer's whole-document
translation; the button is hidden without it).

The card lives in the window's overlay (MainWindow.card_overlay), not in a
popover (popovers cannot be moved, and cover what they point at): it goes
next to the selection without covering it (below, else above, else beside
it), can be dragged by its header anywhere in the window and resized from
its corner, and closes with its × or Esc.  It shows the detected source
language -> the target (both can be changed; the target is remembered), the
translation as it arrives, and Copy.  Installing the engine, downloading a
language pack and translating run off the main thread.  Where there is no
overlay to put it in, a popover is the fallback.
"""
from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk, Pango  # noqa: E402

from . import translate  # noqa: E402
from .i18n import _, ngettext  # noqa: E402


WIDTH = 400                     # the card's / popover's width
GAP = 10                        # between the selection and the card
MIN_BODY = 90

CSS = """
.view-translate-card { border-radius: 12px; background-color: @popover_bg_color;
  color: @popover_fg_color;
  box-shadow: 0 0 0 1px alpha(black, 0.14), 0 4px 18px alpha(black, 0.28); }
.view-translate-card .grab { min-height: 5px; min-width: 40px; border-radius: 3px;
  background-color: alpha(currentColor, 0.25); }
.view-translate-card .resize-grip { min-width: 14px; min-height: 14px;
  background-image: linear-gradient(135deg, transparent 55%, alpha(currentColor, 0.3) 55%,
  alpha(currentColor, 0.3) 63%, transparent 63%, transparent 75%,
  alpha(currentColor, 0.3) 75%, alpha(currentColor, 0.3) 83%, transparent 83%); }
"""
_css_done = False


def _install_css():
    global _css_done
    disp = Gdk.Display.get_default()
    if _css_done or disp is None:
        return
    prov = Gtk.CssProvider()
    prov.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(disp, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_done = True


class _Download:
    """One language-pack download at a time, shared by every popover that
    needs it (closing a popover does not stop it)."""
    current = None

    def __init__(self, pairs):
        self.pairs = list(pairs)
        self.progress = (0.0, _("Starting…"))
        self.listeners = []          # (on_progress, on_done)
        self.cancelled = False
        _Download.current = self
        threading.Thread(target=self._run, daemon=True, name="translate-download").start()

    def _run(self):
        def prog(frac, text):
            GLib.idle_add(self._progress, frac, text)
        try:
            translate.download(self.pairs, prog, lambda: self.cancelled)
            err = None
        except Exception as e:  # noqa: BLE001
            err = e
        GLib.idle_add(self._done, err)

    def _progress(self, frac, text):
        self.progress = (frac, text)
        for on_progress, _d in list(self.listeners):
            on_progress(frac, text)
        return False

    def _done(self, err):
        _Download.current = None
        for _p, on_done in list(self.listeners):
            on_done(err)
        return False


class _Body:
    """The translation UI and its flow, shared by the card and the popover:
    subclasses call _build() and place the widgets it returns."""

    def _build(self, text, reopen):
        self.text = text or ""
        self.reopen = reopen
        self.gen = 0
        self.result = ""
        self.closed = False
        self.detected = translate.detect(self.text) or translate.PIVOT
        self.src = self.detected
        self.tgt = translate.target()

        names = [translate.language_name(c) for c in translate.CODES]
        self.from_dd = Gtk.DropDown(model=Gtk.StringList.new(names), enable_search=True,
                                    tooltip_text=_("Translate from"), hexpand=True)
        self.to_dd = Gtk.DropDown(model=Gtk.StringList.new(names), enable_search=True,
                                  tooltip_text=_("Translate into"), hexpand=True)
        self.from_dd.set_selected(translate.CODES.index(self.src))
        self.to_dd.set_selected(translate.CODES.index(self.tgt))
        langs = Gtk.Box(spacing=6, hexpand=True)
        langs.append(self.from_dd)
        langs.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
        langs.append(self.to_dd)
        self.caption = Gtk.Label(label=_("Language detected automatically"), xalign=0,
                                 css_classes=["caption", "dim-label"],
                                 ellipsize=Pango.EllipsizeMode.END)

        # result
        self.buf = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.buf, editable=False, cursor_visible=False,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                            left_margin=6, right_margin=6)
        self.scroll = Gtk.ScrolledWindow(child=view, vexpand=True,
                                         hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         css_classes=["card"])
        # busy / message / download
        self.spinner = Adw.Spinner(halign=Gtk.Align.CENTER)
        self.msg = Gtk.Label(wrap=True, xalign=0, selectable=True, max_width_chars=40,
                             wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.bar = Gtk.ProgressBar(visible=False)
        self.action_btn = Gtk.Button(halign=Gtk.Align.END, visible=False,
                                     css_classes=["suggested-action"])
        self.action_btn.connect("clicked", lambda *_: self._action and self._action())
        self._action = None
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=6, margin_bottom=6, valign=Gtk.Align.CENTER)
        for w in (self.spinner, self.msg, self.bar, self.action_btn):
            info.append(w)
        self.stack = Gtk.Stack(vhomogeneous=True, hhomogeneous=True, vexpand=True)
        self.stack.add_named(Gtk.ScrolledWindow(child=info, hscrollbar_policy=Gtk.PolicyType.NEVER),
                             "info")
        self.stack.add_named(self.scroll, "result")

        self.copy_btn = Gtk.Button(label=_("Copy"), sensitive=False, width_request=96)
        self.copy_btn.connect("clicked", lambda *_: self._copy())
        self.note = Gtk.Label(xalign=0, hexpand=True, wrap=True,
                              css_classes=["caption", "dim-label"])
        foot = Gtk.Box(spacing=6)
        foot.append(self.note)
        foot.append(self.copy_btn)

        self.from_dd.connect("notify::selected", self._from_changed)
        self.to_dd.connect("notify::selected", self._to_changed)
        if len(self.text) > translate.MAX_CHARS:
            self.note.set_label(_("Only the first {n} characters are translated.").format(
                n=f"{translate.MAX_CHARS:,}"))
        return langs, foot

    def _body_height(self, text):
        """Room for about this much text in the result (the translation is
        usually as long), within reason."""
        layout = self.create_pango_layout(text[:4000])
        layout.set_width((WIDTH - 44) * Pango.SCALE)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        _w, h = layout.get_pixel_size()
        return max(110, min(340, h + 28))

    # ---- state ----
    def _info(self, text, button=None, action=None, busy=False, progress=None):
        self.stack.set_visible_child_name("info")
        self.spinner.set_visible(busy)
        self.msg.set_label(text)
        self.msg.set_visible(bool(text))
        self.bar.set_visible(progress is not None)
        if progress is not None:
            self.bar.set_fraction(progress)
        self._action = action
        self.action_btn.set_visible(bool(button))
        if button:
            self.action_btn.set_label(button)
        self.copy_btn.set_sensitive(False)

    def _show_result(self, text, final):
        self.stack.set_visible_child_name("result")
        self.buf.set_text(text)
        self.result = text
        self.copy_btn.set_sensitive(final and bool(text.strip()))

    def _from_changed(self, dd, _p):
        self.src = translate.CODES[dd.get_selected()]
        # (a label hidden or shown would resize a popover: see TranslatePopover)
        self.caption.set_label(_("Language detected automatically")
                               if self.src == self.detected else "")
        self._run()

    def _to_changed(self, dd, _p):
        self.tgt = translate.CODES[dd.get_selected()]
        try:
            translate.set_target(self.tgt)
        except OSError:
            pass
        self._run()

    def _on_closed(self, *_):
        self.closed = True
        self.gen += 1

    def _alive(self, gen):
        return not self.closed and gen == self.gen

    # ---- flow: engine -> packs -> translate ----
    def _run(self):
        self.gen += 1
        gen = self.gen
        src, tgt = self.src, self.tgt
        if not self.text.strip():
            self._info(_("There is no text to translate."))
            return
        if src == tgt:
            self._info(_("The text is already in {lang}. Choose another language to "
                         "translate into.").format(lang=translate.language_name(tgt)))
            return
        if not translate.is_available():
            from . import translate_install
            ok, why = translate_install.can_install()
            if ok:
                self._info(_("Translation runs on this computer and needs a one-time "
                             "download of the translation engine ({size}).").format(
                                 size=translate_install.DOWNLOAD_SIZE),
                           _("Install…"), self._install)
            else:
                self._info(why)
            return
        dl = _Download.current
        if dl is not None:
            self._follow(dl, gen)
            return
        self._info(_("Translating…"), busy=True)

        def work():
            try:
                need = translate.missing(src, tgt)
            except Exception as e:  # noqa: BLE001  (offline without an index, no such pair)
                GLib.idle_add(self._failed, gen, e)
                return
            if need:
                GLib.idle_add(self._ask_download, gen, need)
                return
            self._translate(gen, src, tgt)
        threading.Thread(target=work, daemon=True, name="translate").start()

    def _install(self):
        from . import translate_install
        parent = self.get_parent()
        reopen = self.reopen
        self.dismiss()
        translate_install.offer(parent, reopen)

    def _ask_download(self, gen, need):
        if not self._alive(gen):
            return False
        names = ", ".join(f"{translate.language_name(a)} → {translate.language_name(b)}"
                          for a, b in need)
        self._info(ngettext(
            "This needs a language pack, downloaded once (70–300 MB): {names}.",
            "This needs {n} language packs, downloaded once (70–300 MB each): {names}.",
            len(need)).format(n=len(need), names=names),
            _("Download"), lambda: self._follow(_Download(need), self.gen))
        return False

    def _follow(self, dl, gen):
        def progress(frac, text):
            if self._alive(gen):
                self._info(text, progress=frac, button=_("Cancel"), action=cancel)

        def done(err):
            dl.listeners.remove(entry)
            if not self._alive(gen):
                return
            if err is None:
                self._run()
            elif str(err) == "Cancelled":
                self._info(_("Download cancelled."), button=_("Download"),
                           action=lambda: self._follow(_Download(dl.pairs), self.gen))
            else:
                self._failed(gen, err)

        def cancel():
            dl.cancelled = True
        entry = (progress, done)
        dl.listeners.append(entry)
        progress(*dl.progress)

    def _translate(self, gen, src, tgt):
        def partial(text):
            GLib.idle_add(lambda: (self._alive(gen) and self._show_result(text, False), False)[1])
        try:
            res = translate.translate(self.text, src, tgt, partial,
                                      cancelled=lambda: not self._alive(gen))
        except translate.NeedsDownload as e:
            GLib.idle_add(self._ask_download, gen, e.pairs)
            return
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(self._failed, gen, e)
            return
        GLib.idle_add(lambda: (self._alive(gen) and self._show_result(res, True), False)[1])

    def _failed(self, gen, err):
        if self._alive(gen) and str(err) != "Cancelled":
            self._info(_("Could not translate: {error}").format(error=err),
                       _("Try Again"), self._run)
        return False

    def _copy(self):
        if not self.result:
            return
        self.get_clipboard().set(self.result)
        self.copy_btn.set_label(_("Copied"))
        GLib.timeout_add(1500, lambda: (self.copy_btn.set_label(_("Copy")), False)[1])



class TranslatePopover(_Body, Gtk.Popover):
    """The fallback where there is no overlay for a card."""

    def __init__(self, text, reopen=None):
        Gtk.Popover.__init__(self, position=Gtk.PositionType.BOTTOM)
        self.add_css_class("view-translate")
        langs, foot = self._build(text, reopen)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10,
                      margin_bottom=10, margin_start=10, margin_end=10)
        for w in (langs, self.caption, self.stack, foot):
            box.append(w)
        # A popover that changes size while it is up gets dismissed by the
        # compositor (GNOME Shell): its size is fixed, the result scrolls.
        frame = Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                   propagate_natural_width=False,
                                   propagate_natural_height=False)
        frame.set_size_request(WIDTH, self._body_height(self.text) + 150)
        self.set_child(frame)
        self.connect("closed", self._on_closed)
        self._run()

    def dismiss(self):
        self.popdown()


class TranslateCard(_Body, Gtk.Box):
    """The floating card: see the module doc."""

    def __init__(self, text, reopen=None, side_by_side=None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         halign=Gtk.Align.START, valign=Gtk.Align.START,
                         css_classes=["view-translate-card"])
        _install_css()
        langs, foot = self._build(text, reopen)
        self.on_close = None

        # header: a grab handle and the title (drag here), Side by Side, close
        grab = Gtk.Box(css_classes=["grab"], halign=Gtk.Align.CENTER, margin_top=6)
        title = Gtk.Label(label=_("Translation"), xalign=0, hexpand=True,
                          css_classes=["heading"])
        self.sbs_btn = Gtk.Button(icon_name="view-dual-symbolic", css_classes=["flat"],
                                  tooltip_text=_("Open Side by Side"), visible=bool(side_by_side))
        self.sbs_btn.connect("clicked", lambda *_: (self.dismiss(), side_by_side()))
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                           tooltip_text=_("Close (Esc)"))
        close.connect("clicked", lambda *_: self.dismiss())
        top = Gtk.Box(spacing=4, margin_start=12, margin_end=6)
        top.append(title)
        top.append(self.sbs_btn)
        top.append(close)
        handle = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        handle.append(grab)
        handle.append(top)
        handle.set_cursor_from_name("grab")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", lambda *_: handle.set_cursor_from_name("grab"))
        handle.add_controller(drag)
        self._handle = handle

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12,
                        margin_end=12)
        for w in (langs, self.caption, self.stack):
            inner.append(w)
        grip = Gtk.Box(css_classes=["resize-grip"], valign=Gtk.Align.END,
                       tooltip_text=_("Drag to resize"))
        grip.set_cursor_from_name("se-resize")
        rs = Gtk.GestureDrag()
        rs.connect("drag-begin", self._resize_begin)
        rs.connect("drag-update", self._resize_update)
        grip.add_controller(rs)
        foot.append(grip)
        foot.set_margin_start(12)
        foot.set_margin_end(4)
        foot.set_margin_bottom(4)
        self.append(handle)
        self.append(inner)
        self.append(foot)
        self.stack.set_size_request(-1, self._body_height(self.text))
        self.set_size_request(WIDTH, -1)
        self._run()

    # ---- moving / sizing (its margins in the overlay) -------------------------------
    def _bounds(self):
        ov = self.get_parent()
        return (ov.get_width(), ov.get_height()) if ov is not None else (0, 0)

    def move_to(self, x, y):
        ow, oh = self._bounds()
        w, h = self.get_width() or WIDTH, self.get_height() or 300
        self.set_margin_start(max(0, min(int(x), ow - w)))
        self.set_margin_top(max(0, min(int(y), oh - h)))

    def _drag_begin(self, _g, _x, _y):
        self._origin = (self.get_margin_start(), self.get_margin_top())
        self._handle.set_cursor_from_name("grabbing")

    def _drag_update(self, _g, dx, dy):
        self.move_to(self._origin[0] + dx, self._origin[1] + dy)

    def _resize_begin(self, _g, _x, _y):
        self._size0 = (self.get_width(), self.stack.get_height())

    def _resize_update(self, _g, dx, dy):
        ow, oh = self._bounds()
        w0, h0 = self._size0
        extra = self.get_height() - self.stack.get_height()
        w = max(300, min(int(w0 + dx), ow - self.get_margin_start()))
        h = max(MIN_BODY, min(int(h0 + dy), oh - self.get_margin_top() - extra))
        self.set_size_request(w, -1)
        self.stack.set_size_request(-1, h)

    def place(self, sel, ow, oh):
        """Next to ``sel`` (x, y, w, h in the overlay) without covering it:
        below, else above (the result area shrinks to fit, within reason),
        else to its right or left; else in the corner farthest from it."""
        x, y, w, h = sel
        _m, nat_h, _b, _c = self.measure(Gtk.Orientation.VERTICAL, WIDTH)
        body = self.stack.get_size_request()[1]
        extra = nat_h - body
        below = oh - (y + h + GAP)
        above = y - GAP
        cx = x if w < WIDTH else x + 24
        for room, at in ((below, lambda H: y + h + GAP), (above, lambda H: y - GAP - H)):
            if room >= extra + MIN_BODY:
                b = min(body, room - extra)
                self.stack.set_size_request(-1, b)
                return self._put(cx, at(extra + b), ow, oh, extra + b)
        H = min(nat_h, oh)
        if ow - (x + w + GAP) >= WIDTH:
            return self._put(x + w + GAP, y, ow, oh, H)
        if x - GAP >= WIDTH:
            return self._put(x - GAP - WIDTH, y, ow, oh, H)
        fx = 0 if x + w / 2 > ow / 2 else ow - WIDTH       # no free side: the far corner
        fy = 0 if y + h / 2 > oh / 2 else oh - H
        return self._put(fx, fy, ow, oh, H)

    def _put(self, x, y, ow, oh, H):
        x = max(0, min(int(x), ow - WIDTH))
        y = max(0, min(int(y), oh - H))
        self.set_margin_start(x)
        self.set_margin_top(y)
        return x, y

    def dismiss(self):
        if self.closed:
            return
        self._on_closed()
        if self.on_close:
            self.on_close(self)


def _menu_open(widget) -> bool:
    """Is a popover (the context menu, a header menu) still open in the
    window of ``widget``?"""
    root = widget.get_root()
    if root is None:
        return False
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, Gtk.Popover) and w.get_mapped():
            return True
        c = w.get_first_child()
        while c is not None:
            stack.append(c)
            c = c.get_next_sibling()
    return False


def _overlay_for(widget):
    """Where a card goes: the window's card overlay, else the nearest
    Gtk.Overlay around ``widget``."""
    root = widget.get_root()
    ov = getattr(root, "card_overlay", None)
    if isinstance(ov, Gtk.Overlay):
        return ov
    w = widget.get_parent()
    while w is not None:
        if isinstance(w, Gtk.Overlay):
            return w
        w = w.get_parent()
    return None


_cards = {}                     # overlay -> card


def close_card(widget=None):
    """Close the card in ``widget``'s window (every card without one)."""
    ov = _overlay_for(widget) if widget is not None else None
    for o, card in list(_cards.items()):
        if ov is None or o is ov:
            card.dismiss()


def current_card(widget):
    return _cards.get(_overlay_for(widget))


def show(parent, text, rect=None, side_by_side=None):
    """Translate ``text`` in a card beside ``rect`` (see the module doc).
    Returns the card (or the fallback popover)."""
    if parent is None or not (text or "").strip() or parent.get_root() is None:
        return None
    ov = _overlay_for(parent)
    if ov is None:
        return _popover(parent, text, rect)
    close_card(parent)

    def reopen():
        GLib.idle_add(lambda: (show(parent, text, rect, side_by_side), False)[1])
    card = TranslateCard(text, reopen, side_by_side)
    ov.add_overlay(card)
    root = ov.get_root()
    keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)

    def key(_c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape and not card.closed:
            card.dismiss()
            return True
        return False
    keys.connect("key-pressed", key)
    root.add_controller(keys)
    _cards[ov] = card

    def closed(c):
        if _cards.get(ov) is c:
            del _cards[ov]
        try:
            root.remove_controller(keys)
        except Exception:  # noqa: BLE001
            pass
        if c.get_parent() is ov:
            ov.remove_overlay(c)
    card.on_close = closed

    # the visible part of the selection, in the overlay's coordinates
    pw, ph = parent.get_width(), parent.get_height()
    if rect is None:
        x0, y0, x1, y1 = pw / 2 - 1, 0, pw / 2 + 1, 2
    else:
        x0, y0 = max(0, rect.x), max(0, rect.y)
        x1, y1 = min(pw, rect.x + rect.width), min(ph, rect.y + rect.height)
        x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    ok, p0 = parent.compute_point(ov, Graphene.Point().init(x0, y0))
    ok2, p1 = parent.compute_point(ov, Graphene.Point().init(x1, y1))
    sel = (p0.x, p0.y, p1.x - p0.x, p1.y - p0.y) if ok and ok2 else (0, 0, 1, 1)
    card.place(sel, ov.get_width(), ov.get_height())
    return card


def _popover(parent, text, rect=None):
    if rect is None:
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = max(0, parent.get_width() // 2), 8, 1, 1
    else:     # under the selection (which may start above the visible part)
        r = Gdk.Rectangle()
        r.x = max(0, min(rect.x, parent.get_width() - 1))
        r.y = max(0, min(rect.y + rect.height, parent.get_height() - 1))
        r.width, r.height = max(1, min(rect.width, 200)), 1
        rect = r

    def reopen():
        GLib.idle_add(lambda: (show(parent, text, rect), False)[1])
    pop = TranslatePopover(text, reopen)
    add = getattr(parent, "add_popover", None)
    if add is not None:
        add(pop)
    else:
        pop.set_parent(parent)

    def closed(p):
        def drop():
            rm = getattr(parent, "remove_popover", None)
            if rm is not None:
                rm(p)
            elif p.get_parent() is not None:
                p.unparent()
            return False
        GLib.idle_add(drop)
    pop.connect("closed", closed)
    pop.set_pointing_to(rect)

    # (popped up once the menu that offered Translate has closed: a popup
    # shown next to a closing one is dismissed by the compositor)
    tries = [0]

    def attempt():
        if pop.get_root() is None or not parent.get_mapped():
            return False
        if _menu_open(parent) and tries[0] < 40:
            tries[0] += 1
            return True
        pop.popup()
        return False
    GLib.idle_add(lambda: (attempt() and GLib.timeout_add(25, attempt), False)[1],
                  priority=GLib.PRIORITY_LOW)
    return pop


def rect(x, y, w=1, h=1):
    r = Gdk.Rectangle()
    r.x, r.y, r.width, r.height = int(x), int(y), max(1, int(w)), max(1, int(h))
    return r


def textview_rect(view, start, end):
    """The bounds of a Gtk.TextView selection (widget coordinates); several
    lines take the view's width."""
    a, b = view.get_iter_location(start), view.get_iter_location(end)
    x0, y0 = view.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, a.x, a.y)
    x1, y1 = view.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, b.x, b.y + b.height)
    if a.y != b.y:
        x0, x1 = 0, view.get_width()
    return bounds_rect(x0, y0, x1, y1)


def bounds_rect(x0, y0, x1, y1):
    """A Gdk.Rectangle from two corners."""
    return rect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
