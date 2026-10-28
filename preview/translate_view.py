"""Side-by-side translation of a whole document.

The original stays where it is; a translation pane opens beside it (a
Gtk.Paned, like Markdown's Split view), and can be moved to a window of its
own and back:

    PDF        the page in view, translated paragraph by paragraph (scanned
               pages are recognized first); it follows as you turn pages,
               with the next page translated ahead
    Markdown   the document translated with its structure kept
               (preview.md_translate), rendered by the same Markdown view;
               its source can be shown and saved
    Text       paragraph by paragraph, scrolled in step with the original
    Code       the file with its comments, docstrings and messages translated
               (preview.code_translate), line for line
    Table      CSV/TSV: the same rows and columns, text cells translated as
               they scroll into view (numbers, dates, IDs stay)
    Image      the recognized (Live Text) blocks in reading order

    open_for(viewer, kind, main, put)   from the viewers' menus

The engine runs in one worker thread per pane: translations are cached per
piece of text and language pair, the piece in view goes first, anything
else (the next page, a whole-document save) waits behind it, and a closed
pane or a changed language cancels what is running.
"""
from __future__ import annotations

import os
import re
import threading
import weakref

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from . import translate  # noqa: E402
from .i18n import _, ngettext  # noqa: E402

AUTO = None                         # "detect the source language"
_BULLET = re.compile(r"^\s*([-*•·▪◦–]|\d{1,3}[.)]|\([a-z0-9]{1,3}\)|[a-z][.)])\s")


# ---------------------------------------------------------------------------
# Paragraphs out of a PDF page / OCR lines
# ---------------------------------------------------------------------------
def pdf_paragraphs(page) -> list[tuple[str, bool]]:
    """A Poppler page's text as [(paragraph, is_heading)]: lines grouped by
    their gaps, indents, font size and bullets (Poppler gives lines only)."""
    text = page.get_text() or ""
    try:
        ok, rects = page.get_text_layout()
    except Exception:  # noqa: BLE001
        ok, rects = False, []
    if not ok or len(rects) != len(text):
        return [(p, False) for p in re.split(r"\n\s*\n", text) if p.strip()]
    lines = []                      # [text, x1, y1, x2, y2]
    start = 0
    for i in range(len(text) + 1):
        if i == len(text) or text[i] == "\n":
            seg = text[start:i]
            rs = [rects[j] for j in range(start, i) if not text[j].isspace()]
            if seg.strip() and rs:
                lines.append([seg.strip(), min(r.x1 for r in rs), min(r.y1 for r in rs),
                              max(r.x2 for r in rs), max(r.y2 for r in rs)])
            start = i + 1
    return group_lines(lines)


_NUMBER_ONLY = re.compile(r"[\d\s.,:–-]{1,7}")
_LINE_NUMBER = re.compile(r"^\d{1,4}\s+(?=\S)")


def strip_line_numbers(texts):
    """Lines -> lines without margin line numbers (review copies, legal
    text) and page numbers: lines that are only a number are dropped, and a
    leading number is cut when most lines have one."""
    texts = [t for t in texts if not _NUMBER_ONLY.fullmatch(t.strip())]
    numbered = sum(1 for t in texts if _LINE_NUMBER.match(t))
    if numbered >= 5 and numbered >= 0.3 * len(texts):
        texts = [_LINE_NUMBER.sub("", t, count=1) for t in texts]
    return texts


def group_lines(lines) -> list[tuple[str, bool]]:
    """[[text, x1, y1, x2, y2]] in reading order -> [(paragraph, is_heading)]."""
    lines = [ln for ln in lines if not _NUMBER_ONLY.fullmatch(ln[0])]
    kept = strip_line_numbers([ln[0] for ln in lines])
    if len(kept) == len(lines):
        for ln, t in zip(lines, kept):
            ln[0] = t
    if not lines:
        return []
    hs = sorted(ln[4] - ln[2] for ln in lines)
    med = max(1.0, hs[len(hs) // 2])
    paras, cur = [], [lines[0]]

    def flush():
        h = max(ln[4] - ln[2] for ln in cur)
        text = "\n".join(ln[0] for ln in cur)
        heading = h > med * 1.18 and len(text) < 200 and worth_translating(text)
        paras.append((text, heading))

    for ln in lines[1:]:
        prev = cur[-1]
        h_prev, h = prev[4] - prev[2], ln[4] - ln[2]
        gap = ln[2] - prev[4]
        left = min(c[1] for c in cur)
        right = max(max(c[3] for c in cur), ln[3])
        width = max(1.0, right - left)
        new = (gap > 0.75 * med or gap < -1.5 * med            # spacing, next column
               or abs(h - h_prev) > 0.22 * max(h, h_prev)        # font size
               or _BULLET.match(ln[0]) is not None
               or (prev[0].rstrip()[-1:] in ".!?:;。！？：；" and prev[3] < right - 0.12 * width)
               or (len(cur) > 1 and ln[1] - left > 1.2 * med and abs(prev[1] - left) < 2))
        if new:
            flush()
            cur = [ln]
        else:
            cur.append(ln)
    flush()
    return paras


def ocr_paragraphs(lines) -> list[tuple[str, bool]]:
    from . import ocr
    text = ocr.lines_to_text(lines)
    paras = [p.split("\n") for p in re.split(r"\n\s*\n", text) if p.strip()]
    flat = strip_line_numbers([t for p in paras for t in p])
    if len(flat) == sum(len(p) for p in paras):        # (nothing dropped: keep the blocks)
        out, i = [], 0
        for p in paras:
            out.append("\n".join(flat[i:i + len(p)]))
            i += len(p)
    else:
        out = ["\n".join(t for t in strip_line_numbers(p)) for p in paras]
    return [(p.strip(), False) for p in out if p.strip()]


def text_units(text: str, max_lines=30, max_chars=1500):
    """Plain text -> [(first line, line count, text)]: paragraphs (blank-line
    separated), long ones cut at line boundaries."""
    out = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        j = i
        size = 0
        while j < len(lines) and lines[j].strip() and j - i < max_lines and size < max_chars:
            size += len(lines[j]) + 1
            j += 1
        out.append((i, j - i, "\n".join(lines[i:j])))
        i = j
    return out


# ---------------------------------------------------------------------------
# The engine runner
# ---------------------------------------------------------------------------
def worth_translating(text: str) -> bool:
    """Not formulas, numbers, symbols (a PDF's equations, table figures):
    the models make things up for those."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    letters = sum(1 for c in chars if c.isalpha())
    return letters >= 0.5 * len(chars) and re.search(r"[^\W\d_]{3,}", text) is not None


class _Job:
    def __init__(self, texts, src, tgt, on_item, on_done, background, prefetch=False):
        self.texts = texts
        self.src, self.tgt = src, tgt
        self.on_item, self.on_done = on_item, on_done
        self.background = background        # prefetch / save: yields to the view
        self.prefetch = prefetch            # dropped when the view moves on
        self.cancelled = False
        self.done = 0


class _Runner:
    """One worker thread; a cache of finished pieces keyed by
    (text, src, tgt); callbacks on the main loop."""

    def __init__(self, on_error):
        self.cache = {}
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.queue = []
        self.running = None
        self.alive = True
        self.on_error = on_error           # (job, exception) on the main loop
        self.thread = None

    def get(self, text, src, tgt):
        if src == tgt or not worth_translating(text):
            return text
        with self.lock:
            return self.cache.get((text, src, tgt))

    def submit(self, job):
        with self.cv:
            if job.background:
                self.queue.append(job)
            else:
                for j in self.queue:         # the view changed: drop what it asked for before
                    if not j.background or j.prefetch:
                        j.cancelled = True
                r = self.running
                if r is not None and (not r.background or r.prefetch):
                    r.cancelled = True
                self.queue = [j for j in self.queue if not j.cancelled]
                self.queue.insert(0, job)
            self.cv.notify()
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True,
                                           name="translate-pane")
            self.thread.start()

    def cancel_all(self):
        with self.cv:
            for j in self.queue:
                j.cancelled = True
            if self.running is not None:
                self.running.cancelled = True
            self.queue = []

    def stop(self):
        self.cancel_all()
        with self.cv:
            self.alive = False
            self.cv.notify()

    def _preempted(self, job):
        if job.cancelled or not self.alive:
            return True
        if job.background:
            with self.lock:
                return any(not j.background for j in self.queue)
        return False

    # (results come back at default priority, not idle: an animating
    # spinner, a slow repaint must not hold them up)
    def _loop(self):
        while True:
            with self.cv:
                while self.alive and not self.queue:
                    self.cv.wait()
                if not self.alive:
                    return
                job = self.queue.pop(0)
                self.running = job
            try:
                self._run(job)
            finally:
                with self.lock:
                    self.running = None

    def _run(self, job):
        if job.cancelled:
            return
        todo = []
        with self.lock:
            for t in dict.fromkeys(job.texts):
                if (worth_translating(t) and job.src != job.tgt and
                        (t, job.src, job.tgt) not in self.cache):
                    todo.append(t)

        def item(i, res):
            with self.lock:
                self.cache[(todo[i], job.src, job.tgt)] = res
            GLib.idle_add(self._item, job, todo[i], res, priority=GLib.PRIORITY_DEFAULT)
        try:
            translate.translate_batch(todo, job.src, job.tgt, item,
                                      cancelled=lambda: self._preempted(job))
        except Exception as e:  # noqa: BLE001
            if str(e) == "Cancelled":
                if job.background and not job.cancelled and self.alive:
                    with self.cv:              # yielded to the view: carry on later
                        self.queue.append(job)
                return
            GLib.idle_add(self._error, job, e, priority=GLib.PRIORITY_DEFAULT)
            return
        GLib.idle_add(self._finish, job, priority=GLib.PRIORITY_DEFAULT)

    def _item(self, job, text, res):
        if not job.cancelled and job.on_item:
            job.on_item(text, res)
        return False

    def _finish(self, job):
        if not job.cancelled and job.on_done:
            job.on_done()
        return False

    def _error(self, job, err):
        if not job.cancelled:
            self.on_error(job, err)
        return False


# ---------------------------------------------------------------------------
# Scrolling two text views in step
# ---------------------------------------------------------------------------
class Side:
    """A Gtk.TextView whose content is split into the same units as the
    other side; ``start(i)`` -> Gtk.TextIter where unit i begins."""

    def __init__(self, view, count, start):
        self.view, self.count, self.start = view, count, start

    @property
    def vadj(self):
        return self.view.get_vadjustment()

    def y_of(self, i):
        n = self.count()
        if i >= n:
            return self.vadj.get_upper()
        it = self.start(i)
        if it is None:
            return 0
        y, _h = self.view.get_line_yrange(it)
        return y

    def locate(self, y):
        n = self.count()
        if n == 0:
            return None
        lo, hi = 0, n - 1
        while lo < hi:                       # last unit starting at or above y
            mid = (lo + hi + 1) // 2
            if self.y_of(mid) <= y:
                lo = mid
            else:
                hi = mid - 1
        a, b = self.y_of(lo), self.y_of(lo + 1)
        return lo, (y - a) / (b - a) if b > a else 0.0


class ScrollSync:
    """Keeps the unit at the top of one side at the top of the other; the
    side under the pointer (or with the focus) leads."""

    def __init__(self, a: Side, b: Side):
        self.sides = [a, b]
        self.lead = a
        self._ids = []
        self._busy = False
        for s in (a, b):
            m = Gtk.EventControllerMotion()
            m.connect("enter", lambda *_x, s=s: setattr(self, "lead", s))
            s.view.add_controller(m)
            f = Gtk.EventControllerFocus()
            f.connect("enter", lambda *_x, s=s: setattr(self, "lead", s))
            s.view.add_controller(f)
            self._ids.append((s, m, f, s.vadj, s.vadj.connect("value-changed", self._moved, s)))

    def release(self):
        for s, m, f, adj, hid in self._ids:
            try:
                adj.disconnect(hid)
                s.view.remove_controller(m)
                s.view.remove_controller(f)
            except Exception:  # noqa: BLE001
                pass
        self._ids = []

    def _moved(self, _adj, side):
        if self._busy or side is not self.lead:
            return
        other = self.sides[1] if side is self.sides[0] else self.sides[0]
        pos = side.locate(side.vadj.get_value())
        if pos is None or other.count() == 0:
            return
        i, f = pos
        i = min(i, other.count() - 1)
        a, b = other.y_of(i), other.y_of(i + 1)
        adj = other.vadj
        y = min(a + f * (b - a), adj.get_upper() - adj.get_page_size())
        self._busy = True
        try:
            adj.set_value(max(0.0, y))
        finally:
            self._busy = False


def _read_only_view(css=()):
    v = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                     top_margin=16, bottom_margin=24, left_margin=22, right_margin=22,
                     hexpand=True, vexpand=True, css_classes=list(css))
    return v


def _dim_color():
    dark = Adw.StyleManager.get_default().get_dark()
    return "rgba(255,255,255,0.5)" if dark else "rgba(0,0,0,0.45)"


# ---------------------------------------------------------------------------
# The pane
# ---------------------------------------------------------------------------
class TranslationPane(Gtk.Box):
    def __init__(self, adapter, on_close, on_popout):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True,
                         css_classes=["view-translation"], width_request=300)
        self.adapter = adapter
        self.on_close, self.on_popout = on_close, on_popout
        self.src_choice = AUTO
        self.tgt = translate.target()
        self.detected = None
        self.gen = 0
        self.alive = True
        self.runner = _Runner(self._job_error)
        self.current = None             # the job of what is in view
        self._busy_jobs = set()

        names = [translate.language_name(c) for c in translate.CODES]
        self.from_dd = Gtk.DropDown(model=Gtk.StringList.new([_("Detect Language")] + names),
                                    enable_search=True, tooltip_text=_("Translate from"),
                                    valign=Gtk.Align.CENTER)
        self.to_dd = Gtk.DropDown(model=Gtk.StringList.new(names), enable_search=True,
                                  tooltip_text=_("Translate into"), valign=Gtk.Align.CENTER)
        self.to_dd.set_selected(translate.CODES.index(self.tgt))
        self.from_dd.connect("notify::selected", self._from_changed)
        self.to_dd.connect("notify::selected", self._to_changed)

        # row 1: languages, then the actions; row 2: what is shown + adapter switches
        bar = Gtk.Box(spacing=4, css_classes=["toolbar"])
        bar.append(self.from_dd)
        bar.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
        bar.append(self.to_dd)
        bar.append(Gtk.Box(hexpand=True))
        self.copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", css_classes=["flat"],
                                   tooltip_text=_("Copy Translation"))
        self.copy_btn.connect("clicked", lambda *_: self._copy())
        self.save_btn = Gtk.Button(icon_name="document-save-symbolic", css_classes=["flat"],
                                   tooltip_text=adapter.save_label())
        self.save_btn.connect("clicked", lambda *_: self._save())
        self.pop_btn = Gtk.Button(icon_name="window-new-symbolic", css_classes=["flat"],
                                  tooltip_text=_("Open in a Separate Window"))
        self.pop_btn.connect("clicked", lambda *_: self.on_popout())
        self.close_btn = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"],
                                    tooltip_text=_("Close Translation"))
        self.close_btn.connect("clicked", lambda *_: self.on_close())
        for b in (self.copy_btn, self.save_btn, self.pop_btn, self.close_btn):
            bar.append(b)
        self.append(bar)
        row2 = Gtk.Box(spacing=8, margin_start=10, margin_end=8, margin_bottom=6)
        self.spinner = Adw.Spinner(visible=False, width_request=16, height_request=16)
        row2.append(self.spinner)
        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                                css_classes=["caption", "dim-label"])
        row2.append(self.status)
        for w in adapter.header_widgets():
            row2.append(w)
        self.append(row2)
        self.append(Gtk.Separator())

        # info page: engine install, pack download, errors
        self.info_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                    css_classes=["title-4"])
        self.info_msg = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                  max_width_chars=44, selectable=True)
        self.info_bar = Gtk.ProgressBar(visible=False)
        self.info_btn = Gtk.Button(halign=Gtk.Align.CENTER, visible=False,
                                   css_classes=["pill", "suggested-action"])
        self.info_btn.connect("clicked", lambda *_: self._info_action and self._info_action())
        self._info_action = None
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24,
                       margin_end=24, valign=Gtk.Align.CENTER, vexpand=True)
        info.append(Gtk.Image(icon_name="preferences-desktop-locale-symbolic", pixel_size=64,
                              css_classes=["dim-label"]))
        for w in (self.info_title, self.info_msg, self.info_bar, self.info_btn):
            info.append(w)

        self.progress = Gtk.ProgressBar(css_classes=["osd"], valign=Gtk.Align.START,
                                        visible=False, can_target=False)
        content = Gtk.Overlay(child=adapter.widget(), vexpand=True)
        content.add_overlay(self.progress)
        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.stack.add_named(content, "content")
        self.stack.add_named(info, "info")
        self.append(self.stack)
        adapter.pane = self

    # ---- languages ----------------------------------------------------------------------
    def src_for(self, sample: str, shown=True) -> str:
        """The source language: the chosen one, else detected from ``sample``
        (and, when ``shown``, reported in the status line)."""
        if self.src_choice is not None:
            return self.src_choice
        code = translate.detect(sample or "") or translate.PIVOT
        if shown:
            self.detected = code
        return code

    def _from_changed(self, dd, _p):
        i = dd.get_selected()
        self.src_choice = None if i == 0 else translate.CODES[i - 1]
        self.refresh()

    def _to_changed(self, dd, _p):
        self.tgt = translate.CODES[dd.get_selected()]
        try:
            translate.set_target(self.tgt)
        except OSError:
            pass
        self.refresh()

    def set_status(self, where: str = ""):
        parts = [where] if where else []
        if self.src_choice is None and self.detected:
            parts.append(_("{lang} detected").format(lang=translate.language_name(self.detected)))
        self.status.set_label(" · ".join(parts))

    # ---- running -------------------------------------------------------------------------
    def refresh(self):
        """(Re)translate what the adapter shows: after a language change, an
        engine install or a pack download."""
        if not self.alive:
            return
        self.gen += 1
        self.runner.cancel_all()
        self._busy_jobs.clear()
        self._sync_busy()
        if not translate.is_available():
            from . import translate_install
            ok, why = translate_install.can_install()
            if ok:
                self.show_info(_("Translation Engine Needed"),
                               _("Translation runs on this computer and needs a one-time "
                                 "download of the translation engine ({size}).").format(
                                   size=translate_install.DOWNLOAD_SIZE),
                               _("Install…"), lambda: translate_install.offer(self, self.refresh))
            else:
                self.show_info(_("Translation Unavailable"), why)
            return
        from .translate_ui import _Download
        if _Download.current is not None:
            self._follow(_Download.current)
            return
        self.stack.set_visible_child_name("content")
        self.adapter.refresh()

    def request(self, texts, src, on_item=None, on_done=None, background=False, prefetch=False):
        """Translate ``texts`` from ``src`` into the target language.  Returns
        what is cached already ({text: translation}); on_item(text,
        translation) as the others arrive, on_done() when all are there."""
        tgt = self.tgt
        have = {}
        missing = False
        for t in texts:
            r = self.runner.get(t, src, tgt)
            if r is None:
                missing = True
            else:
                have[t] = r
        if not missing:
            if on_done:
                GLib.idle_add(lambda: (self.alive and on_done(), False)[1])
            return have
        job = _Job(list(texts), src, tgt, None, None, background or prefetch, prefetch)
        gen = self.gen
        total = sum(1 for t in dict.fromkeys(texts) if t not in have)

        def item(text, res):
            if gen != self.gen:
                return
            job.done += 1
            if on_item:
                on_item(text, res)
            if job is self.current:
                self._progress(job.done / max(1, total))

        def done():
            self._busy_jobs.discard(job)
            if job is self.current:
                self.current = None
                self._progress(None)
            self._sync_busy()
            if gen == self.gen and on_done:
                on_done()
        job.on_item, job.on_done = item, done
        if not job.background:
            self.current = job
            self._progress(0.0)
        self._busy_jobs.add(job)
        self._sync_busy()
        self.runner.submit(job)
        return have

    def _sync_busy(self):
        live = {j for j in self._busy_jobs if not j.cancelled}
        self._busy_jobs = live
        self.spinner.set_visible(bool(live))

    def _progress(self, frac):
        if frac is None:
            self.progress.set_visible(False)
        else:
            self.progress.set_visible(True)
            self.progress.set_fraction(max(0.02, min(1.0, frac)))

    def _job_error(self, job, err):
        self._busy_jobs.discard(job)
        self._sync_busy()
        if job is self.current:
            self.current = None
            self._progress(None)
        if isinstance(err, translate.NeedsDownload):
            self._ask_download(err.pairs)
        elif isinstance(err, translate.NotInstalled):
            self.refresh()
        else:
            self.show_info(_("Could Not Translate"), str(err), _("Try Again"), self.refresh)

    # ---- info page ----------------------------------------------------------------------
    def show_info(self, title, text, button=None, action=None, progress=None):
        self.stack.set_visible_child_name("info")
        self.info_title.set_label(title or "")
        self.info_msg.set_label(text or "")
        self.info_msg.set_visible(bool(text))
        self.info_bar.set_visible(progress is not None)
        if progress is not None:
            self.info_bar.set_fraction(progress)
        self._info_action = action
        self.info_btn.set_visible(bool(button))
        if button:
            self.info_btn.set_label(button)

    def _ask_download(self, need):
        from .translate_ui import _Download
        names = ", ".join(f"{translate.language_name(a)} → {translate.language_name(b)}"
                          for a, b in need)
        self.show_info(_("Language Pack Needed"), ngettext(
            "This needs a language pack, downloaded once (70–300 MB): {names}.",
            "This needs {n} language packs, downloaded once (70–300 MB each): {names}.",
            len(need)).format(n=len(need), names=names),
            _("Download"), lambda: self._follow(_Download(need)))

    def _follow(self, dl):
        gen = self.gen

        def progress(frac, text):
            if self.alive and gen == self.gen:
                self.show_info(_("Downloading Language Pack"), text, _("Cancel"), cancel,
                               progress=frac)

        def done(err):
            dl.listeners.remove(entry)
            if not self.alive or gen != self.gen:
                return
            if err is None:
                self.refresh()
            elif str(err) == "Cancelled":
                self.show_info(_("Download cancelled."), "", _("Download"),
                               lambda: self._follow(type(dl)(dl.pairs)))
            else:
                self.show_info(_("Could Not Translate"), str(err), _("Try Again"), self.refresh)

        def cancel():
            dl.cancelled = True
        entry = (progress, done)
        dl.listeners.append(entry)
        progress(*dl.progress)

    # ---- copy / save ------------------------------------------------------------------------
    def _copy(self):
        text = self.adapter.text()
        if not text.strip():
            return
        self.get_clipboard().set(text)
        self.copy_btn.set_icon_name("object-select-symbolic")
        GLib.timeout_add(1200, lambda: (self.copy_btn.set_icon_name("edit-copy-symbolic"),
                                        False)[1])

    def _save(self):
        name = self.adapter.save_name()
        dlg = Gtk.FileDialog(title=self.adapter.save_label(), initial_name=name)
        folder = self.adapter.folder()
        if folder is not None:
            dlg.set_initial_folder(folder)

        def done(d, res):
            try:
                gf = d.save_finish(res)
            except GLib.Error:
                return
            if gf is not None and gf.get_path():
                self.adapter.save(gf.get_path())
        root = self.get_root()
        dlg.save(root if isinstance(root, Gtk.Window) else None, None, done)

    def write_file(self, path, text):
        try:
            tmp = path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text if text.endswith("\n") else text + "\n")
            os.replace(tmp, path)
        except OSError as e:
            self.show_info(_("Could Not Save"), str(e), _("OK"), self.refresh)
            return False
        self.status.set_label(_("Saved {name}").format(name=os.path.basename(path)))
        return True

    def release(self):
        if not self.alive:
            return
        self.alive = False
        self.gen += 1
        self.runner.stop()
        self.adapter.release()


# ---------------------------------------------------------------------------
# Adapters: what is translated and how it is shown
# ---------------------------------------------------------------------------
class _Adapter:
    pane: TranslationPane = None
    ext = ".txt"

    def __init__(self, viewer):
        self.viewer = viewer

    def header_widgets(self):
        return []

    def widget(self):
        raise NotImplementedError

    def refresh(self):
        raise NotImplementedError

    def text(self) -> str:
        return ""

    def save_label(self):
        return _("Save Translation As…")

    def _gfile(self):
        g = getattr(self.viewer, "get_current_file", None)
        try:
            return g() if callable(g) else getattr(self.viewer, "gfile", None)
        except Exception:  # noqa: BLE001
            return None

    def save_name(self):
        gf = self._gfile()
        base = (gf.get_basename() if gf is not None else None) or _("Translation")
        stem = base.rsplit(".", 1)[0] if "." in base else base
        return f"{stem}.{self.pane.tgt}{self.ext}"

    def folder(self):
        gf = self._gfile()
        return gf.get_parent() if gf is not None else None

    def save(self, path):
        self.pane.write_file(path, self.text())

    def release(self):
        pass

    def sync_sides(self):
        """(left Side, right Side) to scroll together, or None."""
        return None


class _ParagraphView:
    """A read-only text view of translated paragraphs; pieces not yet
    translated show their original, dimmed.  With ``align`` (unit -> y of
    the original's unit), each paragraph is moved down to start level with
    its original (when the translations above it leave room)."""

    def __init__(self, css=(), originals=False, top_margin=16):
        self.view = _read_only_view(css)
        self.view.set_top_margin(top_margin)
        self.scroll = Gtk.ScrolledWindow(child=self.view, hexpand=True, vexpand=True,
                                         hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.buf = self.view.get_buffer()
        self.originals = originals          # show the original under each translation
        self.marks = []                     # (start, end) per unit, both left gravity
        self.units = []
        self.align = None
        self._pads = []                     # (tag, pixels) per unit
        self._align_timer = 0
        self._tags()
        ref = weakref.ref(self)
        sm = Adw.StyleManager.get_default()
        hid = sm.connect("notify::dark", lambda *_: ref() is not None and ref()._tags())
        self.view.connect("destroy", lambda *_: sm.disconnect(hid))
        self.view.get_vadjustment().connect("changed", lambda *_: self.schedule_align())

    def _tags(self):
        table = self.buf.get_tag_table()
        dim = _dim_color()
        for name, props in (("pending", {"foreground": dim}),
                            ("orig", {"foreground": dim, "scale": 0.86,
                                      "pixels-above-lines": 2}),
                            ("h", {"weight": Pango.Weight.BOLD, "scale": 1.18}),
                            ("par", {"pixels-below-lines": 12}),
                            ("caption", {"foreground": dim, "scale": 0.86,
                                         "pixels-below-lines": 10})):
            tag = table.lookup(name) or self.buf.create_tag(name)
            for k, v in props.items():
                tag.set_property(k, v)

    def set_units(self, units, header=None, gap="\n"):
        """units: [(original, is_heading, translation | None)]"""
        self.buf.set_text("")
        self.marks = []
        self.units = [list(u) for u in units]
        end = self.buf.get_end_iter()
        if header:
            self.buf.insert_with_tags_by_name(end, header + "\n", "caption")
        for k, u in enumerate(self.units):
            s = self.buf.create_mark(None, end, True)
            self._insert(end, u)
            e = self.buf.create_mark(None, end, True)
            self.marks.append((s, e))
            if k < len(self.units) - 1:
                self.buf.insert_with_tags_by_name(end, gap, "par")
        self.view.get_vadjustment().set_value(0)
        self.schedule_align()

    def _insert(self, it, u):
        orig, heading, tr = u
        tags = ["par"] + (["h"] if heading else [])
        if tr is None:
            self.buf.insert_with_tags_by_name(it, orig, *(tags + ["pending"]))
        else:
            self.buf.insert_with_tags_by_name(it, tr, *tags)
            if self.originals:
                self.buf.insert_with_tags_by_name(it, "\n" + " ".join(orig.split()), "orig",
                                                  "par")

    def update(self, k, tr):
        if k >= len(self.units):
            return
        self.units[k][2] = tr
        s, e = self.marks[k]
        a, b = self.buf.get_iter_at_mark(s), self.buf.get_iter_at_mark(e)
        self.buf.delete(a, b)
        it = self.buf.get_iter_at_mark(s)
        self._insert(it, self.units[k])
        self.buf.move_mark(e, it)
        if k < len(self._pads):
            self._pads[k] = (self._pads[k][0], -1)        # (re-apply to the new text)
        self.schedule_align()

    def start(self, k):
        if k >= len(self.marks):
            return None
        return self.buf.get_iter_at_mark(self.marks[k][0])

    def text(self):
        return "\n\n".join((u[2] if u[2] is not None else u[0]) for u in self.units)

    # ---- lining paragraphs up with the original -------------------------------------
    def schedule_align(self):
        if self.align is None or self._align_timer:
            return

        def fire():
            self._align_timer = 0
            self._do_align()
            return False
        self._align_timer = GLib.timeout_add(80, fire)

    def _do_align(self):
        n = len(self.marks)
        if self.align is None or n == 0:
            return
        while len(self._pads) < n:
            self._pads.append((self.buf.create_tag(None), -1))
        v = self.view
        tops = [v.get_line_yrange(self.start(k))[0] for k in range(n)]
        it = self.buf.get_end_iter()
        y, h = v.get_line_yrange(it)
        tops.append(y + h)
        pads = [max(0, p) for _t, p in self._pads[:n]]
        natural, before = [], 0
        for k in range(n + 1):           # positions without any padding
            natural.append(tops[k] - before)
            if k < n:
                before += pads[k]
        pos = natural[0]
        for k in range(n):
            target = self.align(k)
            pad = 0 if target is None else max(0, int(round(target - pos)))
            pos += pad + (natural[k + 1] - natural[k])
            tag, old = self._pads[k]
            if old != pad:
                tag.set_property("pixels-above-lines", pad)
                a = self.start(k)
                b = a.copy()
                b.forward_to_line_end()
                if a.equal(b):
                    b.forward_char()
                self.buf.apply_tag(tag, a, b)
                self._pads[k] = (tag, pad)


# ---- PDF ---------------------------------------------------------------------------------
class PdfAdapter(_Adapter):
    def __init__(self, viewer):
        super().__init__(viewer)
        self.pv = _ParagraphView(("md-preview",))
        self.page = None              # (doc index, page position) shown
        self._timer = 0
        self._recognizing = set()

    def widget(self):
        return self.pv.scroll

    def save_label(self):
        return _("Save Translated Document As…")

    def _key(self, pos):
        v = self.viewer
        ref = v.pages[pos]
        return (ref.src, ref.index)

    def paragraphs(self, pos):
        """[(text, heading)] of a page, or None while it is being recognized."""
        v = self.viewer
        ref = v.pages[pos]
        k = (ref.src, ref.index)
        if v._page_has_text(ref):
            return pdf_paragraphs(v._ppage(ref))
        if k in v.ocr_lines:
            from . import ocr
            return ocr_paragraphs([ocr.OcrLine(list(poly), t, 1.0) for poly, t in v.ocr_lines[k]])
        return None

    def page_changed(self):
        """The viewer turned the page: follow, a moment after scrolling stops."""
        if self._timer:
            GLib.source_remove(self._timer)

        def fire():
            self._timer = 0
            if self.pane is not None and self.pane.alive:
                key = (self.viewer.active, self.viewer.current)
                if key != self.page:
                    self.refresh()
            return False
        self._timer = GLib.timeout_add(250, fire)

    def refresh(self):
        v = self.viewer
        if not v.pages:
            return
        pos = v.current
        self.page = (v.active, pos)
        where = _("Page {n} of {total}").format(n=pos + 1, total=len(v.pages))
        paras = self.paragraphs(pos)
        if paras is None:
            self.pv.set_units([])
            self.pane.set_status(where)
            self._recognize(pos)
            return
        if not paras:
            self.pv.set_units([(_("No text on this page."), False, None)])
            self.pane.set_status(where)
            self._prefetch(pos + 1)
            return
        texts = [p for p, _h in paras]
        src = self.pane.src_for("\n".join(texts))
        self.pane.set_status(where)
        page = self.page
        index = {}
        for k, t in enumerate(texts):
            index.setdefault(t, []).append(k)

        def item(text, tr):
            if self.page == page:
                for k in index.get(text, ()):
                    self.pv.update(k, tr)

        have = self.pane.request(texts, src, item, lambda: self._prefetch(pos + 1))
        self.pv.set_units([(t, h, have.get(t)) for t, h in paras])

    def _recognize(self, pos):
        v = self.viewer
        k = self._key(pos)
        from .pdf_view import _ocr_module, _ocr_offerable
        if _ocr_module() is None and not _ocr_offerable():
            self.pv.set_units([(_("This page is a scan, and text recognition is not "
                                  "available."), False, None)])
            return
        self.pv.set_units([(_("Recognizing text on this page…"), False, None)])
        if k in self._recognizing:
            return
        self._recognizing.add(k)

        def done():
            self._recognizing.discard(k)
            if self.pane is not None and self.pane.alive and self.page == (v.active, pos):
                self.refresh()
        v.recognize_text([pos], announce=False, done=done)

    def _prefetch(self, pos):
        v = self.viewer
        if pos >= len(v.pages) or not v._page_has_text(v.pages[pos]):
            return
        texts = [p for p, _h in pdf_paragraphs(v._ppage(v.pages[pos]))]
        if texts:
            self.pane.request(texts, self.pane.src_for("\n".join(texts), shown=False),
                              prefetch=True)

    def text(self):
        return self.pv.text()

    def save(self, path):
        """The whole document: every page translated (scans as far as they
        have been recognized), then written as text."""
        v = self.viewer
        pages = []
        for pos in range(len(v.pages)):
            paras = self.paragraphs(pos) or []
            pages.append([p for p, _h in paras])
        texts = [t for p in pages for t in p]
        src = self.pane.src_for("\n".join(texts[:200]), shown=False)
        pane = self.pane

        def write():
            out = []
            for n, p in enumerate(pages):
                out.append(_("Page {n}").format(n=n + 1))
                out.append("\n\n".join(pane.runner.get(t, src, pane.tgt) or t for t in p))
            pane.write_file(path, "\n\n".join(out))
            pane.spinner.set_visible(False)
        pane.status.set_label(_("Translating the whole document…"))
        pane.request(texts, src, None, write, background=True)

    def release(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0


# ---- image -------------------------------------------------------------------------------
class ImageAdapter(_Adapter):
    def __init__(self, viewer):
        super().__init__(viewer)
        self.pv = _ParagraphView(originals=True)
        self.item = None

    def widget(self):
        return self.pv.scroll

    def refresh(self):
        v = self.viewer
        it = v.current
        self.item = it
        t = v.canvas.text
        name = getattr(it, "name", None) or ""
        if it is None:
            self.pv.set_units([])
            return
        if t is None:
            self.pane.set_status(name)
            try:
                from . import ocr
                ok = ocr.is_available()
            except Exception:  # noqa: BLE001
                ok = False
            self.pv.set_units([(_("Recognizing text…") if ok else
                                _("Text recognition is not available"), False, None)])
            if v._ocr is None:            # (else Live Text is at it: it reports back)
                v._run_ocr(force=True)    # (offers to install the engine when missing)
            return
        if not t.has_text():
            self.pv.set_units([(_("No text found"), False, None)])
            self.pane.set_status(name)
            return
        paras = ocr_paragraphs(t.lines)
        texts = [p for p, _h in paras]
        src = self.pane.src_for("\n".join(texts))
        self.pane.set_status(name)
        index = {}
        for k, x in enumerate(texts):
            index.setdefault(x, []).append(k)
        item = it

        def on_item(text, tr):
            if self.item is item:
                for k in index.get(text, ()):
                    self.pv.update(k, tr)
        have = self.pane.request(texts, src, on_item)
        self.pv.set_units([(x, False, have.get(x)) for x in texts])

    def text(self):
        return "\n\n".join((u[2] if u[2] is not None else u[0]) for u in self.pv.units)


# ---- plain text / code ------------------------------------------------------------------
class TextAdapter(_Adapter):
    def __init__(self, viewer):
        super().__init__(viewer)
        self.pv = _ParagraphView((viewer._cls, "md-preview"),
                                 top_margin=viewer.view.get_top_margin())
        self.pv.align = self._original_y
        self.units = []
        self._timer = 0
        self._hid = viewer.buf.connect("changed", self._changed)
        self._adj = viewer.view.get_vadjustment()
        self._adj_id = self._adj.connect("changed", lambda *_: self.pv.schedule_align())

    def _original_y(self, k):
        """Where unit k starts in the original (buffer y)."""
        if k >= len(self.units):
            return None
        v = self.viewer
        ok, it = v.buf.get_iter_at_line(self.units[k][0])
        return v.view.get_line_yrange(it)[0] if ok else None

    def widget(self):
        return self.pv.scroll

    def save_name(self):
        gf = self._gfile()
        base = (gf.get_basename() if gf is not None else None) or _("Translation")
        stem, dot, ext = base.rpartition(".")
        return f"{stem}.{self.pane.tgt}.{ext}" if dot and stem else f"{base}.{self.pane.tgt}.txt"

    def _changed(self, *_):
        if self._timer:
            GLib.source_remove(self._timer)

        def fire():
            self._timer = 0
            if self.pane is not None and self.pane.alive:
                self.refresh()
            return False
        self._timer = GLib.timeout_add(900, fire)

    def refresh(self):
        text = self.viewer._current_text()
        self.units = text_units(text)
        texts = [u[2] for u in self.units]
        src = self.pane.src_for("\n".join(texts[:60]))
        self.pane.set_status(ngettext("{n} paragraph", "{n} paragraphs",
                                      len(texts)).format(n=len(texts)))
        index = {}
        for k, t in enumerate(texts):
            index.setdefault(t, []).append(k)

        def on_item(t, tr):
            for k in index.get(t, ()):
                self.pv.update(k, tr)
        vadj = self.pv.view.get_vadjustment()
        keep = vadj.get_value()
        have = self.pane.request(texts, src, on_item)
        self.pv.set_units([(t, False, have.get(t)) for t in texts])
        if keep:
            GLib.idle_add(lambda: (vadj.set_value(keep), False)[1])

    def sync_sides(self):
        v = self.viewer
        left = Side(v.view, lambda: len(self.units),
                    lambda i: v.buf.get_iter_at_line(self.units[i][0])[1]
                    if i < len(self.units) else None)
        right = Side(self.pv.view, lambda: len(self.pv.marks), self.pv.start)
        return left, right

    def text(self):
        return self.pv.text()

    def release(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        try:
            self.viewer.buf.disconnect(self._hid)
            self._adj.disconnect(self._adj_id)
        except Exception:  # noqa: BLE001
            pass
        self.pv.align = None


# ---- Markdown -----------------------------------------------------------------------------
class MarkdownAdapter(_Adapter):
    ext = ".md"

    def __init__(self, viewer):
        super().__init__(viewer)
        from . import markdown_view
        self.preview = markdown_view.MarkdownPreview(viewer._cls)
        self.preview.base_dir = viewer._md.base_dir if viewer._md is not None else None
        self.preview.on_open_file = lambda gf: viewer.window.open_files([gf])
        self.src_view = _read_only_view((viewer._cls, "tx-view"))
        self.src_view.set_monospace(True)
        self.src_scroll = Gtk.ScrolledWindow(child=self.src_view, hexpand=True, vexpand=True)
        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.stack.add_named(self.preview, "preview")
        self.stack.add_named(self.src_scroll, "source")
        self.toggle = Adw.ToggleGroup(css_classes=["round", "flat"], valign=Gtk.Align.CENTER)
        for name, label, tip in (("preview", _("Preview"), _("Rendered Markdown")),
                                 ("source", _("Source"), _("Translated Markdown source"))):
            self.toggle.add(Adw.Toggle(name=name, label=label, tooltip=tip))
        self.preview.translate_menu = False      # (no "translate the translation")
        self.preview.view.set_extra_menu(None)
        self.toggle.set_active_name("preview")
        self.toggle.connect("notify::active-name",
                            lambda g, _p: self.stack.set_visible_child_name(g.get_active_name()))
        self.plan = None
        self.result = ""
        self._render_timer = 0
        self._edit_timer = 0
        self._hid = viewer.buf.connect("changed", self._changed)

    def header_widgets(self):
        return [self.toggle]

    def widget(self):
        return self.stack

    def save_label(self):
        return _("Save Translation As…")

    def _changed(self, *_):
        if self._edit_timer:
            GLib.source_remove(self._edit_timer)

        def fire():
            self._edit_timer = 0
            if self.pane is not None and self.pane.alive:
                self.refresh()
            return False
        self._edit_timer = GLib.timeout_add(1200, fire)

    def refresh(self):
        from . import md_translate
        text = self.viewer._current_text()
        plan = md_translate.Plan(text)
        self.plan = plan
        src = self.pane.src_for(plan.sample())
        self.pane.set_status("")
        self._round(plan, src)
        self._render(now=True)

    def _round(self, plan, src):
        """Hand the pending pieces to the engine; pieces whose markup got lost
        come back as smaller pieces for a second round."""
        todo = plan.pending()
        if not todo:
            return
        ids = {}
        for sid, t in todo:
            ids.setdefault(t, []).append(sid)

        def on_item(t, tr):
            if self.plan is plan:
                for sid in ids.get(t, ()):
                    plan.put(sid, tr)
                self._render()

        def done():
            if self.plan is plan:
                self._render(now=True)
                self._round(plan, src)
        have = self.pane.request(list(ids), src, on_item, done)
        for t, tr in have.items():
            for sid in ids[t]:
                plan.put(sid, tr)

    def _render(self, now=False):
        if self._render_timer:
            GLib.source_remove(self._render_timer)
            self._render_timer = 0

        def fire():
            self._render_timer = 0
            if self.plan is None or self.pane is None or not self.pane.alive:
                return False
            self.result = self.plan.render()
            self.preview.set_source(self.result)
            vadj = self.src_scroll.get_vadjustment()
            keep = vadj.get_value()
            self.src_view.get_buffer().set_text(self.result)
            GLib.idle_add(lambda: (vadj.set_value(keep), False)[1])
            done, total = self.plan.progress()
            if done < total:
                self.pane.set_status(_("{done} of {total} parts").format(done=done, total=total))
            else:
                self.pane.set_status("")
            return False
        if now:
            fire()
        else:
            self._render_timer = GLib.timeout_add(250, fire)

    def text(self):
        return self.result

    def sync_sides(self):
        v = self.viewer
        md = v._md
        right = Side(self.preview.view, lambda: len(self.preview.block_marks()),
                     lambda i: self.preview.block_iter(i))
        if md is not None and v._md_mode == "preview":
            left = Side(md.view, lambda: len(md.block_marks()), lambda i: md.block_iter(i))
        else:
            from . import markdown_view

            def lines():
                return markdown_view.block_lines(v._current_text())
            cache = {}

            def count():
                cache["l"] = lines()
                return len(cache["l"])

            def start(i):
                ls = cache.get("l") or lines()
                return v.buf.get_iter_at_line(ls[i])[1] if i < len(ls) else None
            left = Side(v.view, count, start)
        return left, right

    def release(self):
        for t in (self._render_timer, self._edit_timer):
            if t:
                GLib.source_remove(t)
        self._render_timer = self._edit_timer = 0
        self.preview.release()
        try:
            self.viewer.buf.disconnect(self._hid)
        except Exception:  # noqa: BLE001
            pass


# ---- code ---------------------------------------------------------------------------------
class CodeAdapter(_Adapter):
    """Source code: the file as it is, with its comments, docstrings and
    messages translated (code_translate), line for line beside the original."""

    def __init__(self, viewer):
        super().__init__(viewer)
        v = viewer.view
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=v.get_wrap_mode(), top_margin=v.get_top_margin(),
                                 bottom_margin=v.get_bottom_margin(), left_margin=10,
                                 right_margin=14, hexpand=True, vexpand=True,
                                 css_classes=[viewer._cls, "tx-view"])
        self.view.set_monospace(True)       # (after css_classes, which would drop it)
        self.scroll = Gtk.ScrolledWindow(child=self.view, hexpand=True, vexpand=True)
        self.buf = self.view.get_buffer()
        self.plan = None
        self.marks = []                 # per block: [(start mark, end mark)] per line
        self.gen = 0
        self._timer = 0
        self._hid = viewer.buf.connect("changed", self._changed)
        self._wrap_id = v.connect("notify::wrap-mode",
                                  lambda w, _p: self.view.set_wrap_mode(w.get_wrap_mode()))
        self.tag_tr = self.buf.create_tag("translated")
        self._padded = {}               # line -> (side, pixels) of spacing added
        self._style_tags()

    def _style_tags(self):
        dark = Adw.StyleManager.get_default().get_dark()
        self.tag_tr.set_property("background", "rgba(98,160,234,0.16)" if dark
                                 else "rgba(53,132,228,0.10)")

    def widget(self):
        return self.scroll

    save_name = TextAdapter.save_name

    def _changed(self, *_):
        if self._timer:
            GLib.source_remove(self._timer)

        def fire():
            self._timer = 0
            if self.pane is not None and self.pane.alive:
                self.refresh()
            return False
        self._timer = GLib.timeout_add(900, fire)

    def refresh(self):
        """Lex the file in a thread (Pygments can take a while), then show
        it and ask for its prose."""
        from . import code_translate, syntax
        self.gen += 1
        gen = self.gen
        text = self.viewer._current_text()
        lexer = self.viewer.lexer
        dark = Adw.StyleManager.get_default().get_dark()
        self.pane.set_status(_("Reading the code…"))

        def work():
            try:
                plan = code_translate.CodePlan(text, lexer)
                runs = syntax.lex_runs(text, lexer) if len(text) < 2_000_000 else []
            except Exception as e:  # noqa: BLE001
                plan, runs = None, e
            GLib.idle_add(self._show, gen, text, plan, runs, dark,
                          priority=GLib.PRIORITY_DEFAULT)
        threading.Thread(target=work, daemon=True, name="code-plan").start()

    def _show(self, gen, text, plan, runs, dark):
        if gen != self.gen or self.pane is None or not self.pane.alive:
            return False
        from . import syntax
        if plan is None:
            self.pane.show_info(_("Could Not Translate"), str(runs), _("Try Again"),
                                self.pane.refresh)
            return False
        self.plan = plan
        self._unpad()
        vadj = self.scroll.get_vadjustment()
        keep = vadj.get_value()
        buf = self.buf
        buf.set_text(text)
        table = buf.get_tag_table()
        for ttype, a, b in runs:
            st = syntax.style_for(ttype, dark)
            if not st:
                continue
            name = "hl:" + str(ttype)
            tag = table.lookup(name)
            if tag is None:
                tag = buf.create_tag(name)
                if st.get("color"):
                    tag.set_property("foreground", "#" + st["color"])
                if st.get("bold"):
                    tag.set_property("weight", Pango.Weight.BOLD)
                if st.get("italic"):
                    tag.set_property("style", Pango.Style.ITALIC)
            buf.apply_tag(tag, buf.get_iter_at_offset(a), buf.get_iter_at_offset(b))
        self.marks = [[(buf.create_mark(None, buf.get_iter_at_offset(s[0]), True),
                        buf.create_mark(None, buf.get_iter_at_offset(s[1]), True))
                       for s in block] for block in plan.blocks]
        GLib.idle_add(lambda: (vadj.set_value(keep), False)[1])
        n = len(plan.pieces)
        self.pane.set_status(ngettext("{n} comment or text to translate",
                                      "{n} comments and texts to translate", n).format(n=n))
        if not n:
            return False
        src = self.pane.src_for("\n".join(plan.pieces[:200]))
        self.pane.set_status(ngettext("{n} comment or text to translate",
                                      "{n} comments and texts to translate", n).format(n=n))
        index = {}
        for k, piece in enumerate(plan.pieces):
            index.setdefault(piece, []).append(k)

        def on_item(piece, tr):
            if self.plan is plan:
                for k in index.get(piece, ()):
                    self._put(k, tr)
        have = self.pane.request(plan.pieces, src, on_item)
        for piece, tr in have.items():
            on_item(piece, tr)
        return False

    # ---- keeping the lines level --------------------------------------------------------
    # Chinese/Japanese text makes a line taller than the code font's; each
    # line whose two sides differ gets the difference as space below it on
    # the shorter side (a tag, on the original too: removed on close).
    def _pad_tag(self, buf, n):
        name = f"tr-pad:{n}"
        tag = buf.get_tag_table().lookup(name)
        if tag is None:
            tag = buf.create_tag(name, pixels_below_lines=n)
            tag.set_priority(buf.get_tag_table().get_size() - 1)
        return tag

    def _unpad(self):
        for buf in (self.viewer.buf, self.buf):
            table = buf.get_tag_table()
            names = []
            table.foreach(lambda t: names.append(t.get_property("name") or ""))
            for name in names:
                if name.startswith("tr-pad:"):
                    table.remove(table.lookup(name))
        self._padded = {}

    def _schedule_level(self):
        if getattr(self, "_level_timer", 0):
            return

        def fire():
            self._level_timer = 0
            if self.pane is not None and self.pane.alive and self.plan is not None:
                self._level()
            return False
        self._level_timer = GLib.timeout_add(120, fire)

    def _level(self):
        lv, lb = self.viewer.view, self.viewer.buf
        rv, rb = self.view, self.buf
        if lb.get_line_count() != rb.get_line_count():
            return                          # (an edit in flight: the next plan levels it)
        lines = {s[3] for block in self.plan.blocks for s in block}
        for i in sorted(lines | set(self._padded)):
            ok, li = lb.get_iter_at_line(i)
            ok2, ri = rb.get_iter_at_line(i)
            if not (ok and ok2):
                continue
            side, old = self._padded.get(i, (None, 0))
            hl = lv.get_line_yrange(li)[1] - (old if side == "l" else 0)
            hr = rv.get_line_yrange(ri)[1] - (old if side == "r" else 0)
            new = ("l", hr - hl) if hr > hl else ("r", hl - hr) if hl > hr else (None, 0)
            if new == (side, old):
                continue
            for s, buf, it in (("l", lb, li), ("r", rb, ri)):
                end = it.copy()
                if not end.ends_line():
                    end.forward_to_line_end()
                a = it.copy()
                if a.equal(end):
                    if not end.forward_char():
                        continue
                if side == s and old:
                    buf.remove_tag(self._pad_tag(buf, old), a, end)
                if new[0] == s and new[1]:
                    buf.apply_tag(self._pad_tag(buf, new[1]), a, end)
            self._padded[i] = new

    def _put(self, k, tr):
        from . import code_translate
        block, marks = self.plan.blocks[k], self.marks[k]
        if tr == self.plan.pieces[k]:
            return
        parts = code_translate.distribute(tr.replace("\n", " "), [len(s[2]) for s in block])
        buf = self.buf
        for (ms, me), part in zip(marks, parts):
            a, b = buf.get_iter_at_mark(ms), buf.get_iter_at_mark(me)
            tags = [t for t in a.get_tags() if t is not self.tag_tr]
            buf.delete(a, b)
            it = buf.get_iter_at_mark(ms)
            buf.insert_with_tags(it, part, self.tag_tr, *tags)
            buf.move_mark(me, it)
        self._schedule_level()

    def text(self):
        return self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), False)

    def sync_sides(self):
        v = self.viewer

        def lines(buf):
            return lambda: buf.get_line_count()

        def at(buf):
            def start(i):
                ok, it = buf.get_iter_at_line(i)
                return it if ok else None
            return start
        return Side(v.view, lines(v.buf), at(v.buf)), Side(self.view, lines(self.buf), at(self.buf))

    def release(self):
        self.gen += 1
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if getattr(self, "_level_timer", 0):
            GLib.source_remove(self._level_timer)
            self._level_timer = 0
        try:
            self._unpad()                   # (the original's spacing back as it was)
        except Exception:  # noqa: BLE001
            pass
        for obj, hid in ((self.viewer.buf, self._hid), (self.viewer.view, self._wrap_id)):
            try:
                obj.disconnect(hid)
            except Exception:  # noqa: BLE001
                pass


# ---- tables ----------------------------------------------------------------------------
class ProportionalSync:
    """Two scrolled lists scrolled in step (same rows: same fraction)."""

    def __init__(self, a, b):
        self.adjs = [a, b]
        self._busy = False
        self._ids = [(adj, adj.connect("value-changed", self._moved)) for adj in (a, b)]

    def _moved(self, adj):
        if self._busy:
            return
        other = self.adjs[1] if adj is self.adjs[0] else self.adjs[0]
        span = adj.get_upper() - adj.get_page_size()
        f = adj.get_value() / span if span > 0 else 0.0
        self._busy = True
        try:
            other.set_value(f * max(0.0, other.get_upper() - other.get_page_size()))
        finally:
            self._busy = False

    def release(self):
        for adj, hid in self._ids:
            try:
                adj.disconnect(hid)
            except Exception:  # noqa: BLE001
                pass
        self._ids = []


class TableAdapter(_Adapter):
    """CSV/TSV: the same rows and columns, text cells translated (numbers,
    dates, IDs, codes stay); cells are translated as they scroll into view."""
    MAX_COLS = 60

    def __init__(self, viewer):
        super().__init__(viewer)
        from .table_view import RowModel
        self.model = RowModel()
        self.cv = Gtk.ColumnView(model=Gtk.NoSelection.new(self.model), show_row_separators=True,
                                 show_column_separators=True, reorderable=False,
                                 hexpand=True, vexpand=True, css_classes=["tv", "data-table"])
        self.scroll = Gtk.ScrolledWindow(child=self.cv, hexpand=True, vexpand=True)
        self.bound = {}                 # text -> {Gtk.Inscription}
        self.src = None
        self.gen = 0
        self._flush_timer = 0
        self._sync = None
        self._cols = []

    def widget(self):
        return self.scroll

    save_name = TextAdapter.save_name

    def _tr(self, text):
        return self.pane.runner.get(text, self.src, self.pane.tgt) if self.src else None

    def _show_cell(self, ins, text):
        from .code_translate import cell_worth
        tr = self._tr(text) if cell_worth(text) else text
        if tr is None:
            ins.set_markup("<span alpha='50%'>" + GLib.markup_escape_text(text) + "</span>")
            self.bound.setdefault(text, set()).add(ins)
            self._schedule_flush()
        else:
            ins.set_attributes(None)        # (the dimming of a pending cell)
            ins.set_text(tr)

    def _factory(self, col):
        f = Gtk.SignalListItemFactory()

        def setup(_f, li):
            li.set_child(Gtk.Inscription(xalign=0.0, nat_chars=1, min_chars=1, hexpand=True,
                                         text_overflow=Gtk.InscriptionOverflow.ELLIPSIZE_END,
                                         css_classes=["tv-rownum" if col < 0 else "tv-cell"]))

        def bind(_f, li):
            ins = li.get_child()
            r = li.get_position()
            d = self.viewer.data
            if col < 0:
                ins.set_attributes(None)
                ins.set_text(str(r + 1))
                return
            text = d.cell_at(r, col) if r < d.n_view and col < d.ncols else ""
            ins._text = text
            self._show_cell(ins, text)

        def unbind(_f, li):
            ins = li.get_child()
            s = self.bound.get(getattr(ins, "_text", None))
            if s is not None:
                s.discard(ins)
        f.connect("setup", setup)
        f.connect("bind", bind)
        f.connect("unbind", unbind)
        return f

    def refresh(self):
        from .code_translate import cell_worth
        d = self.viewer.data
        self.gen += 1
        self.bound = {}
        sample = []
        for r in range(min(d.n_view, 400)):
            for c in range(min(d.ncols, self.MAX_COLS)):
                t = d.cell_at(r, c)
                if cell_worth(t):
                    sample.append(t)
        titles = [d.title(c) for c in range(min(d.ncols, self.MAX_COLS))]
        self.src = self.pane.src_for("\n".join(sample[:300] + titles))
        cols = self.cv.get_columns()
        for i in range(cols.get_n_items() - 1, -1, -1):
            self.cv.remove_column(cols.get_item(i))
        num = Gtk.ColumnViewColumn(title="", factory=self._factory(-1))
        num.set_fixed_width(int(self.viewer._rownum_w) if hasattr(self.viewer, "_rownum_w") else 56)
        self.cv.append_column(num)
        self._cols = []
        widths = getattr(d, "col_w", None) or []
        for c, title in enumerate(titles):
            col = Gtk.ColumnViewColumn(title=title, factory=self._factory(c), resizable=True)
            w = widths[c] if c < len(widths) and widths[c] else 140
            col.set_fixed_width(int(max(80, min(320, w))))
            self.cv.append_column(col)
            self._cols.append((col, title))
        self.model.set_n(0)
        self.model.set_n(d.n_view)
        if d.has_header:                  # column titles too
            heads = [t for t in titles if cell_worth(t)]
            if heads:
                def on_item(text, tr):
                    for col, title in self._cols:
                        if title == text:
                            col.set_title(tr)
                have = self.pane.request(heads, self.src, on_item, background=True)
                for t, tr in have.items():
                    on_item(t, tr)
        n = d.ncols
        self.pane.set_status(ngettext("{n} row", "{n} rows", d.n_view).format(n=d.n_view)
                             + ("" if n <= self.MAX_COLS else " · " + _(
                                 "the first {n} columns").format(n=self.MAX_COLS)))
        if self._sync is None:
            self._sync = ProportionalSync(self.viewer.vadj, self.scroll.get_vadjustment())

    def data_changed(self):
        """An edit, a sort, a filter in the original: follow it shortly."""
        if getattr(self, "_data_timer", 0):
            GLib.source_remove(self._data_timer)

        def fire():
            self._data_timer = 0
            if self.pane is not None and self.pane.alive:
                self.refresh()
            return False
        self._data_timer = GLib.timeout_add(400, fire)

    def _schedule_flush(self):
        if not self._flush_timer:
            self._flush_timer = GLib.timeout_add(120, self._flush)

    def _flush(self):
        """Translate the text cells in view (asking again for all of them:
        an earlier request is dropped when the view moves on)."""
        self._flush_timer = 0
        if self.pane is None or not self.pane.alive:
            return False
        todo = [t for t, s in self.bound.items() if s and self._tr(t) is None]
        if not todo:
            return False

        def on_item(text, tr):
            for ins in list(self.bound.get(text, ())):
                if getattr(ins, "_text", None) == text:
                    ins.set_attributes(None)
                    ins.set_text(tr)
            self.bound.pop(text, None)
        self.pane.request(todo, self.src, on_item)
        return False

    def _rows(self, translated):
        from .code_translate import cell_worth
        d = self.viewer.data
        rows = []
        if d.has_header:
            rows.append([translated(d.title(c)) if cell_worth(d.title(c)) else d.title(c)
                         for c in range(d.ncols)])
        for r in range(d.n_view):
            rows.append([translated(t) if cell_worth(t) else t
                         for t in (d.cell_at(r, c) for c in range(d.ncols))])
        return rows

    def text(self):
        rows = self._rows(lambda t: self._tr(t) or t)
        return "\n".join("\t".join(c.replace("\t", " ").replace("\n", " ") for c in row)
                         for row in rows[:20000])

    def save(self, path):
        """Every text cell translated, written with the file's delimiter."""
        import csv
        import io
        from .code_translate import cell_worth
        d = self.viewer.data
        texts = set()
        for row in self._rows(lambda t: t):
            texts.update(t for t in row if cell_worth(t))
        pane, src = self.pane, self.src

        def write():
            out = io.StringIO()
            w = csv.writer(out, delimiter=d.delim or ",", lineterminator="\n")
            for row in self._rows(lambda t: pane.runner.get(t, src, pane.tgt) or t):
                w.writerow(row)
            pane.write_file(path, out.getvalue())
            pane.spinner.set_visible(False)
        pane.status.set_label(_("Translating the whole document…"))
        pane.request(sorted(texts), src, None, write, background=True)

    def release(self):
        self.gen += 1
        for t in ("_flush_timer", "_data_timer"):
            if getattr(self, t, 0):
                GLib.source_remove(getattr(self, t))
                setattr(self, t, 0)
        if self._sync is not None:
            self._sync.release()
            self._sync = None


# ---------------------------------------------------------------------------
# Hosting: beside the original (a Gtk.Paned) or in its own window
# ---------------------------------------------------------------------------
class SideBySide:
    """``main()`` -> the viewer's main widget, ``put(widget)`` puts a widget
    in its place."""

    def __init__(self, viewer, adapter, main, put):
        self.viewer = viewer
        self.adapter = adapter
        self._main, self._put = main, put
        self.pane = TranslationPane(adapter, self.close, self.toggle_window)
        self.paned = None
        self.win = None
        self.sync = None
        # the viewer's window closing: stop the engine and close a separate
        # translation window ("destroy" does not come while Python still holds
        # the window; gtk_window_destroy() always unrealizes it)
        host = viewer.window if isinstance(viewer.window, Gtk.Window) else None
        self._host = host
        self._host_id = host.connect("unrealize", lambda *_: shutdown_for(viewer)) if host else 0
        self._dock()
        self.pane.refresh()
        GLib.idle_add(self._start_sync)

    def _start_sync(self):
        if self.sync is not None:
            self.sync.release()
            self.sync = None
        sides = self.adapter.sync_sides() if self.pane.alive else None
        if sides:
            self.sync = ScrollSync(*sides)
        return False

    def restart_sync(self):
        GLib.idle_add(self._start_sync)

    def source_changed(self):
        """The viewer shows something else (another image, recognized text)."""
        if self.pane.alive:
            self.pane.refresh()
            self.restart_sync()

    def _dock(self):
        content = self._main()
        self.paned = Gtk.Paned(wide_handle=True, hexpand=True, vexpand=True,
                               resize_start_child=True, resize_end_child=True,
                               shrink_start_child=False, shrink_end_child=False)
        self._put(self.paned)
        self.paned.set_start_child(content)
        self.paned.set_end_child(self.pane)

        def halve():
            if self.paned is None:
                return False
            w = self.paned.get_width()
            if w > 0:
                self.paned.set_position(w // 2)
            return w <= 0 and self.pane.alive
        if halve():
            GLib.timeout_add(50, halve)
        self.pane.pop_btn.set_icon_name("window-new-symbolic")
        self.pane.pop_btn.set_tooltip_text(_("Open in a Separate Window"))

    def _undock(self):
        if self.paned is None:
            return
        content = self.paned.get_start_child()
        self.paned.set_start_child(None)
        self.paned.set_end_child(None)
        self._put(content)
        self.paned = None

    def toggle_window(self):
        if self.win is None:
            self._undock()
            root = self.viewer.get_root()
            gf = self.adapter._gfile()
            name = gf.get_basename() if gf is not None else ""
            win = Adw.Window(title=_("Translation — {name}").format(name=name) if name
                             else _("Translation"), default_width=640, default_height=760)
            if isinstance(root, Gtk.Window) and root.get_application() is not None:
                win.set_application(root.get_application())
            tv = Adw.ToolbarView()
            tv.add_top_bar(Adw.HeaderBar())
            tv.set_content(self.pane)
            win.set_content(tv)
            win.connect("close-request", self._win_closed)
            self.win = win
            self.pane.pop_btn.set_icon_name("view-dual-symbolic")
            self.pane.pop_btn.set_tooltip_text(_("Show Beside the Original"))
            self.pane.close_btn.set_visible(False)
            win.present()
        else:
            win, self.win = self.win, None
            win.get_content().set_content(None)
            self.pane.close_btn.set_visible(True)
            self._dock()
            win.destroy()
        self.restart_sync()

    def _win_closed(self, win):
        if self.win is win:
            self.win = None
            win.get_content().set_content(None)
            self._finish()
        return False

    def close(self):
        if self.win is not None:
            win, self.win = self.win, None
            win.get_content().set_content(None)
            win.destroy()
        else:
            self._undock()
        self._finish()

    def _finish(self):
        if self.sync is not None:
            self.sync.release()
            self.sync = None
        self.pane.release()
        self._unhook()
        if getattr(self.viewer, "_translation", None) is self:
            self.viewer._translation = None

    def _unhook(self):
        if self._host_id:
            try:
                self._host.disconnect(self._host_id)
            except Exception:  # noqa: BLE001
                pass
            self._host_id = 0


def open_for(viewer, kind, main, put):
    """Show (or bring back) the side-by-side translation for ``viewer``;
    kind: "pdf", "image", "text", "code", "markdown" or "table"."""
    cur = getattr(viewer, "_translation", None)
    if cur is not None:
        if cur.win is not None:
            cur.win.present()
        else:
            cur.pane.refresh()
        return cur
    adapter = {"pdf": PdfAdapter, "image": ImageAdapter, "text": TextAdapter,
               "code": CodeAdapter, "table": TableAdapter,
               "markdown": MarkdownAdapter}[kind](viewer)
    sbs = SideBySide(viewer, adapter, main, put)
    viewer._translation = sbs
    return sbs


def close_for(viewer):
    cur = getattr(viewer, "_translation", None)
    if cur is not None:
        cur.close()


def shutdown_for(viewer):
    """The viewer is going away: stop translating, close a separate
    translation window, leave the widgets alone."""
    cur = getattr(viewer, "_translation", None)
    if cur is None:
        return
    viewer._translation = None
    if cur.sync is not None:
        cur.sync.release()
        cur.sync = None
    cur.pane.release()
    cur._unhook()
    if cur.win is not None:
        win, cur.win = cur.win, None
        win.destroy()


def box_slot(box, child_getter):
    """(main, put) for a viewer whose main widget is a child of a Gtk.Box."""
    state = {"widget": None}

    def main():
        w = child_getter()
        state["widget"] = w
        return w

    def put(new):
        old = state.get("current") or state["widget"]
        prev = old.get_prev_sibling()
        box.remove(old)
        box.insert_child_after(new, prev)
        state["current"] = new
    return main, put


def content_slot(split):
    """(main, put) for a viewer whose main widget is an Adw.OverlaySplitView's content."""
    def main():
        return split.get_content()

    def put(new):
        split.set_content(None)
        split.set_content(new)
    return main, put


__all__ = ["open_for", "close_for", "box_slot", "content_slot", "pdf_paragraphs",
           "group_lines", "text_units", "TranslationPane"]
