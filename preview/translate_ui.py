"""The Translate popover, shared by the PDF, image and text viewers.

    show(parent, text, rect=None)

``parent`` is the widget the popover points into (a canvas with
add_popover()/remove_popover() is used through those), ``rect`` a
Gdk.Rectangle in its coordinates (default: top centre).  The popover shows
the detected source language -> the target (both can be changed; the target
is remembered), the translation as it arrives, and Copy.  Detection is
instant; installing the engine, downloading a language pack and translating
all run off the main thread.
"""
from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from . import translate  # noqa: E402
from .i18n import _, ngettext  # noqa: E402


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


class TranslatePopover(Gtk.Popover):
    def __init__(self, text, reopen=None):
        super().__init__(position=Gtk.PositionType.BOTTOM)
        self.add_css_class("view-translate")
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
        head = Gtk.Box(spacing=6)
        head.append(self.from_dd)
        head.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
        head.append(self.to_dd)
        self.caption = Gtk.Label(label=_("Language detected automatically"), xalign=0,
                                 css_classes=["caption", "dim-label"])

        # result
        self.buf = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.buf, editable=False, cursor_visible=False,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                            left_margin=6, right_margin=6)
        self.scroll = Gtk.ScrolledWindow(child=view, propagate_natural_height=True,
                                         max_content_height=360, min_content_height=48,
                                         hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         css_classes=["card"])
        # busy / message / download
        self.spinner = Adw.Spinner(halign=Gtk.Align.CENTER)
        self.msg = Gtk.Label(wrap=True, xalign=0, selectable=True, max_width_chars=48)
        self.bar = Gtk.ProgressBar(visible=False)
        self.action_btn = Gtk.Button(halign=Gtk.Align.END, visible=False,
                                     css_classes=["suggested-action"])
        self.action_btn.connect("clicked", lambda *_: self._action and self._action())
        self._action = None
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=6, margin_bottom=6)
        for w in (self.spinner, self.msg, self.bar, self.action_btn):
            info.append(w)
        self.stack = Gtk.Stack(vhomogeneous=False, interpolate_size=True)
        self.stack.add_named(info, "info")
        self.stack.add_named(self.scroll, "result")

        self.copy_btn = Gtk.Button(label=_("Copy"), sensitive=False)
        self.copy_btn.connect("clicked", lambda *_: self._copy())
        self.note = Gtk.Label(xalign=0, hexpand=True, wrap=True,
                              css_classes=["caption", "dim-label"])
        foot = Gtk.Box(spacing=6)
        foot.append(self.note)
        foot.append(self.copy_btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10,
                      margin_bottom=10, margin_start=10, margin_end=10, width_request=380)
        for w in (head, self.caption, self.stack, foot):
            box.append(w)
        self.set_child(box)

        self.from_dd.connect("notify::selected", self._from_changed)
        self.to_dd.connect("notify::selected", self._to_changed)
        self.connect("closed", self._on_closed)
        if len(self.text) > translate.MAX_CHARS:
            self.note.set_label(_("Only the first {n} characters are translated.").format(
                n=f"{translate.MAX_CHARS:,}"))
        self._run()

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
        self.caption.set_visible(self.src == self.detected)
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
        self.popdown()
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


def show(parent, text, rect=None):
    """Pop up a translation of ``text`` from ``parent`` (see the module doc)."""
    if parent is None or not (text or "").strip():
        return None
    if rect is None:
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = max(0, parent.get_width() // 2), 8, 1, 1
    else:     # e.g. a selection that starts above the visible part
        rect.x = max(0, min(rect.x, parent.get_width() - 1))
        rect.y = max(0, min(rect.y, parent.get_height() - 1))

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
    pop.popup()
    return pop


def rect(x, y, w=1, h=1):
    r = Gdk.Rectangle()
    r.x, r.y, r.width, r.height = int(x), int(y), max(1, int(w)), max(1, int(h))
    return r
