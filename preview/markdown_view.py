"""Rendered Markdown for the text viewer (Source / Split / Preview).

No web engine (WebKitGTK is not available on Windows): markdown-it-py parses
the source in a worker thread into a flat list of insert operations, which
the main loop replays into a fresh Gtk.TextBuffer with tags (headings,
emphasis, inline code, highlighted code blocks, lists, block quotes, rules,
links), tables as child widgets and local images as paintables.  The new
buffer replaces the shown one in a single step, so re-rendering after an
edit does not flicker.
"""
from __future__ import annotations

import json
import os
import re
import threading
import weakref

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from . import i18n  # noqa: E402
from .i18n import _  # noqa: E402

MD_EXTS = {".md", ".markdown"}
MODES = ("source", "split", "preview")
PREVIEW_CAP = 3_000_000         # characters; longer files only get the source view
IMAGE_MAX_W = 760               # logical px
BULLETS = ("•", "◦", "▪")
HEADING_SCALE = {1: 1.85, 2: 1.5, 3: 1.25, 4: 1.1, 5: 1.0, 6: 0.95}
LS = "\u2028"                   # line break inside a paragraph (hard break)

CSS = """
.md-preview { padding: 0; }
.md-table { border: 1px solid alpha(currentColor, 0.18); border-radius: 6px; margin: 2px 0; }
.md-cell { padding: 4px 10px; border-right: 1px solid alpha(currentColor, 0.12);
           border-bottom: 1px solid alpha(currentColor, 0.12); }
.md-head { font-weight: bold; background-color: alpha(currentColor, 0.05); }
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


def is_markdown(name: str) -> bool:
    name = (name or "").lower()
    return "." in name and name[name.rfind("."):] in MD_EXTS


# ---- the remembered Source / Split / Preview choice (view/settings.json) ----
def saved_mode() -> str:
    try:
        with open(i18n.settings_path(), encoding="utf-8") as f:
            mode = json.load(f).get("markdown_view")
    except (OSError, ValueError, AttributeError):
        mode = None
    return mode if mode in MODES else "preview"


def save_mode(mode: str):
    path = i18n.settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["markdown_view"] = mode
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Markdown → operations (worker thread; no widgets here)
# ---------------------------------------------------------------------------
class Ops:
    """[("t", text, tags) | ("img", texture, alt, tags) | ("table", rows, aligns)
    | ("mark", name)], plus link targets and syntax styles."""

    def __init__(self):
        self.ops = []
        self.links = []             # link:N → href
        self.styles = {}            # hl:key → pygments style dict
        self.headings = 0

    def text(self, s, tags):
        if not s:
            return
        tags = tuple(tags)
        if self.ops and self.ops[-1][0] == "t" and self.ops[-1][2] == tags:
            self.ops[-1] = ("t", self.ops[-1][1] + s, tags)
        else:
            self.ops.append(("t", s, tags))


def slugify(text):
    """GitHub-style heading anchor: lower case, punctuation dropped, spaces → '-'."""
    s = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return s.replace(" ", "-")


def _lexer_for_lang(lang):
    if not lang:
        return None
    try:
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    try:
        return get_lexer_by_name(lang, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def _load_image(src, base_dir):
    """A texture for a local image (scaled to IMAGE_MAX_W), or None."""
    if not src or re.match(r"^[a-z][a-z0-9+.-]*:", src, re.I) and not src.startswith("file:"):
        return None                         # remote images are not fetched
    try:
        path = Gio.File.new_for_uri(src).get_path() if src.startswith("file:") else \
            os.path.join(base_dir or "", GLib.uri_unescape_string(src.split("#")[0], None) or src)
        if not path or not os.path.isfile(path):
            return None
        _fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(path)
        if not w:
            return None
        if w > IMAGE_MAX_W:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, IMAGE_MAX_W, -1, True)
        else:
            pb = GdkPixbuf.Pixbuf.new_from_file(path)
        return Gdk.Texture.new_for_pixbuf(pb)
    except (GLib.Error, TypeError, ValueError):
        return None


def _inline_markup(children, out_links):
    """Inline tokens as Pango markup (table cells)."""
    esc = GLib.markup_escape_text
    parts = []
    for c in children or ():
        t = c.type
        if t == "text":
            parts.append(esc(c.content))
        elif t in ("softbreak", "hardbreak"):
            parts.append(" " if t == "softbreak" else "\n")
        elif t == "code_inline":
            parts.append(f"<tt>{esc(c.content)}</tt>")
        elif t in ("strong_open", "em_open", "s_open"):
            parts.append({"strong_open": "<b>", "em_open": "<i>", "s_open": "<s>"}[t])
        elif t in ("strong_close", "em_close", "s_close"):
            parts.append({"strong_close": "</b>", "em_close": "</i>", "s_close": "</s>"}[t])
        elif t == "link_open":
            href = c.attrGet("href") or ""
            out_links.append(href)
            parts.append(f'<a href="{esc(href)}">')
        elif t == "link_close":
            parts.append("</a>")
        elif t == "image":
            parts.append(esc(c.content or ""))
        elif t == "html_inline":
            parts.append(esc(c.content))
    return "".join(parts)


def build_ops(text: str, dark: bool, base_dir: str | None) -> Ops:
    """Parse Markdown into insert operations (worker thread)."""
    from markdown_it import MarkdownIt
    from . import syntax
    md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
    ops = Ops()
    # YAML front matter: shown as a code block, not as a rule and a heading
    fm = re.match(r"\A---[ \t]*\n(.*?\n)(?:---|\.\.\.)[ \t]*(?:\n|\Z)", text, re.S)
    if fm:
        _code_block(ops, fm.group(1), "yaml", dark, 0, 0, syntax)
        text = text[fm.end():]
    tokens = md.parse(text)
    lists = []                  # [ordered, number]
    quote = 0
    block = "p"                 # paragraph kind for the next inline run
    item_start = False          # the next paragraph starts a list item
    heading = 0

    def btag():
        kind = block
        if lists:
            kind = "li" if item_start else "lic"
        return f"blk:{kind}:{quote}:{len(lists)}"

    def end_par(tags):
        ops.text("\n", tags)

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        t = tok.type
        if t == "heading_open":
            heading = int(tok.tag[1])
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            title = "".join(c.content for c in (inline.children or ()) if c.type in
                            ("text", "code_inline")) if inline is not None else ""
            ops.ops.append(("mark", "h:" + slugify(title)))
            ops.headings += 1
        elif t == "heading_close":
            tags = (f"blk:h{heading}:{quote}:{len(lists)}", f"h{heading}")
            end_par(tags)
            if heading <= 2 and not lists:
                ops.text("\u200b\n", (f"blk:rule2:{quote}:0",))
                ops.text("\u200b\n", (f"blk:gap:{quote}:0",))
            heading = 0
        elif t == "paragraph_open":
            block = "p"
        elif t == "paragraph_close":
            end_par((btag(),))
            item_start = False
        elif t in ("bullet_list_open", "ordered_list_open"):
            start = int(tok.attrGet("start") or 1) if t == "ordered_list_open" else 1
            lists.append([t == "ordered_list_open", start - 1])
        elif t in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
            if not lists:
                ops.text("\u200b\n", (f"blk:gap:{quote}:0",))
        elif t == "list_item_open":
            lst = lists[-1]
            lst[1] += 1
            item_start = True
            marker = f"{lst[1]}." if lst[0] else BULLETS[(len(lists) - 1) % len(BULLETS)]
            ops.text(marker + "\t", (btag(), "marker"))
            # a GitHub task list item: "[ ] " / "[x] " → a check box
            inline = tokens[i + 2] if i + 2 < len(tokens) else None
            if inline is not None and inline.type == "inline" and inline.children:
                first = inline.children[0]
                m = re.match(r"\[([ xX])\] ", first.content) if first.type == "text" else None
                if m:
                    first.content = ("☑ " if m.group(1) != " " else "☐ ") + first.content[4:]
        elif t == "list_item_close":
            if item_start:                  # empty item
                end_par((btag(),))
            item_start = False
        elif t == "blockquote_open":
            quote += 1
        elif t == "blockquote_close":
            quote -= 1
        elif t in ("fence", "code_block"):
            lang = (tok.info or "").strip().split()[0] if t == "fence" and tok.info else ""
            _code_block(ops, tok.content, lang, dark, quote, len(lists), syntax)
            item_start = False
        elif t == "hr":
            ops.text("\u200b\n", (f"blk:gap:{quote}:{len(lists)}",))
            ops.text("\u200b\n", (f"blk:rule:{quote}:{len(lists)}",))
            ops.text("\u200b\n", (f"blk:gap:{quote}:{len(lists)}",))
        elif t == "html_block":
            ops.text(tok.content.rstrip("\n") + "\n", (f"blk:html:{quote}:{len(lists)}", "html"))
        elif t == "table_open":
            j = i
            rows, aligns, row = [], [], None
            while j < len(tokens) and tokens[j].type != "table_close":
                tj = tokens[j]
                if tj.type == "tr_open":
                    row = []
                elif tj.type == "tr_close":
                    rows.append(row)
                elif tj.type in ("th_open", "td_open"):
                    style = tj.attrGet("style") or ""
                    al = "center" if "center" in style else "right" if "right" in style else "left"
                    if len(rows) == 0:
                        aligns.append(al)
                    links = []
                    cell = _inline_markup(tokens[j + 1].children, links)
                    row.append(cell)
                j += 1
            ops.ops.append(("table", rows, aligns, (f"blk:table:{quote}:{len(lists)}",)))
            i = j
        elif t == "inline":
            tags = [btag()]
            if heading:
                tags = [f"blk:h{heading}:{quote}:{len(lists)}", f"h{heading}"]
            _inline(ops, tok.children or (), tags, base_dir)
        i += 1
    return ops


def _code_block(ops, code, lang, dark, quote, depth, syntax):
    code = code.rstrip("\n")
    pad = f"blk:pad:{quote}:{depth}"
    blk = f"blk:code:{quote}:{depth}"
    ops.text("\u200b\n", (pad,))
    lexer = _lexer_for_lang(lang)
    runs = []
    if lexer is not None and len(code) < 200_000:
        try:
            runs = syntax.lex_runs(code, lexer)
        except Exception:  # noqa: BLE001
            runs = []
    pos = 0
    for ttype, a, b in runs:
        st = syntax.style_for(ttype, dark)
        if st is None:
            continue
        if a > pos:
            ops.text(code[pos:a], (blk, "mono"))
        key = "hl:" + str(ttype)
        ops.styles[key] = st
        ops.text(code[a:b], (blk, "mono", key))
        pos = b
    ops.text(code[pos:] + "\n", (blk, "mono"))
    ops.text("\u200b\n", (pad,))
    ops.text("\u200b\n", (f"blk:gap:{quote}:{depth}",))


def _inline(ops, children, base, base_dir):
    stack = []
    for c in children:
        t = c.type
        if t == "text":
            ops.text(c.content, base + stack)
        elif t == "softbreak":
            ops.text(" ", base + stack)
        elif t == "hardbreak":
            ops.text(LS, base + stack)
        elif t in ("strong_open", "em_open", "s_open"):
            stack.append({"strong_open": "b", "em_open": "i", "s_open": "s"}[t])
        elif t in ("strong_close", "em_close", "s_close"):
            name = {"strong_close": "b", "em_close": "i", "s_close": "s"}[t]
            if name in stack:
                stack.reverse()
                stack.remove(name)
                stack.reverse()
        elif t == "code_inline":
            ops.text(c.content, base + stack + ["code"])
        elif t == "link_open":
            ops.links.append(c.attrGet("href") or "")
            stack += ["link", f"link:{len(ops.links) - 1}"]
        elif t == "link_close":
            while stack and stack[-1].startswith("link"):
                stack.pop()
        elif t == "image":
            src = c.attrGet("src") or ""
            alt = "".join(x.content for x in (c.children or ()) if x.type == "text") or c.content
            tex = _load_image(src, base_dir)
            if tex is not None:
                ops.ops.append(("img", tex, alt, tuple(base + stack)))
            else:
                ops.links.append(src)
                ops.text(f"🖼 {alt or src}", base + stack + ["link", f"link:{len(ops.links) - 1}"])
        elif t == "html_inline":
            if re.fullmatch(r"<br\s*/?>", c.content.strip(), re.I):
                ops.text(LS, base + stack)
            else:
                ops.text(c.content, base + stack + ["html"])


# ---------------------------------------------------------------------------
# The preview widget
# ---------------------------------------------------------------------------
class MarkdownPreview(Gtk.ScrolledWindow):
    """Read-only rendered view of Markdown source; call set_source()."""

    def __init__(self, css_class=None):
        super().__init__(hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        _install_css()
        self.view = Gtk.TextView(editable=False, cursor_visible=False,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=18,
                                 bottom_margin=24, left_margin=28, right_margin=28,
                                 hexpand=True, vexpand=True, css_classes=["md-preview"])
        if css_class:
            self.view.add_css_class(css_class)
        self.set_child(self.view)
        self.base_dir = None
        self.links = []
        self.marks = {}
        self._gen = 0
        self._timer = 0
        self._text = None
        self._alive = True
        self._dark = Adw.StyleManager.get_default().get_dark()
        self._table = None
        self._styles = {}
        self.on_open_file = None        # callback(Gio.File) for links to local files

        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.view.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.view.add_controller(motion)
        sm = Adw.StyleManager.get_default()
        ref = weakref.ref(self)

        def on_dark(m, *_):
            p = ref()
            if p is not None and p._alive and m.get_dark() != p._dark:
                p._dark = m.get_dark()
                p._table = None
                if p._text is not None:
                    p._render(p._text)
        self._dark_id = sm.connect("notify::dark", on_dark)

    def release(self):
        if not self._alive:
            return
        self._alive = False
        self._gen += 1
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        try:
            Adw.StyleManager.get_default().disconnect(self._dark_id)
        except Exception:  # noqa: BLE001
            pass

    # ---- input ------------------------------------------------------------
    def set_source(self, text: str, delay_ms: int = 0):
        """Render `text` (after `delay_ms`, coalescing quick edits)."""
        self._text = text
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if delay_ms <= 0:
            self._render(text)
            return

        def fire():
            self._timer = 0
            if self._alive:
                self._render(self._text)
            return False
        self._timer = GLib.timeout_add(delay_ms, fire)

    def _render(self, text):
        self._gen += 1
        gen, dark, base = self._gen, self._dark, self.base_dir

        def work():
            try:
                ops, err = build_ops(text, dark, base), None
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                ops, err = None, e
            GLib.idle_add(self._apply, gen, ops, err)
        threading.Thread(target=work, name="md-render", daemon=True).start()

    # ---- tags -------------------------------------------------------------------
    def _tag(self, name):
        tag = self._table.lookup(name)
        if tag is not None:
            return tag
        tag = Gtk.TextTag(name=name)
        dark = self._dark
        dim = "rgba(255,255,255,0.62)" if dark else "rgba(0,0,0,0.6)"
        shade = "rgba(255,255,255,0.07)" if dark else "rgba(0,0,0,0.045)"
        rule = "rgba(255,255,255,0.22)" if dark else "rgba(0,0,0,0.18)"
        if name.startswith("blk:"):
            _b, kind, quote, depth = name.split(":")
            quote, depth = int(quote), int(depth)
            left = 28 + 22 * quote + 24 * max(0, depth - 1) + (6 if depth else 0)
            tag.set_property("left-margin", left)
            tag.set_property("pixels-inside-wrap", 2)
            if kind == "p":
                tag.set_property("pixels-below-lines", 10)
            elif kind in ("li", "lic"):
                tag.set_property("pixels-below-lines", 4)
                if kind == "li":
                    # hanging indent: the marker sits in the margin, wrapped
                    # lines line up with the text after the tab
                    tag.set_property("left-margin", left + 24)
                    tag.set_property("indent", -24)
                    tabs = Pango.TabArray.new(1, True)
                    tabs.set_tab(0, Pango.TabAlign.LEFT, 0)
                    tag.set_property("tabs", tabs)
                else:
                    tag.set_property("left-margin", left + 24)
            elif kind in HEADING_TAGS:
                level = int(kind[1:])
                tag.set_property("pixels-above-lines", {1: 14, 2: 12}.get(level, 8))
                tag.set_property("pixels-below-lines", 2 if level <= 2 else 6)
            elif kind in ("code", "pad"):
                tag.set_property("paragraph-background", shade)
                tag.set_property("left-margin", left)
                tag.set_property("right-margin", 28)
                tag.set_property("wrap-mode", Gtk.WrapMode.CHAR)
                if kind == "pad":
                    tag.set_property("size-points", 4)
                    tag.set_property("pixels-below-lines", 0)
                tag.set_property("pixels-inside-wrap", 0)
            elif kind in ("rule", "rule2"):
                # a one-line paragraph of a tiny font: its background is the rule
                tag.set_property("paragraph-background", rule)
                tag.set_property("size-points", 1.0 if kind == "rule" else 0.6)
                tag.set_property("right-margin", 28)
            elif kind == "gap":
                tag.set_property("size-points", 2)
                tag.set_property("pixels-below-lines", 2)
            elif kind in ("html", "table"):
                tag.set_property("pixels-below-lines", 10)
            if quote and kind not in ("rule", "rule2"):
                tag.set_property("foreground", dim)
                if kind not in ("code", "pad"):
                    tag.set_property("paragraph-background", shade)
            # paragraph-level tags lose to inline ones (fonts, colors)
        elif name in HEADING_TAGS:
            level = int(name[1])
            tag.set_property("weight", Pango.Weight.BOLD)
            tag.set_property("scale", HEADING_SCALE[level])
            if level == 6:
                tag.set_property("foreground", dim)
        elif name == "b":
            tag.set_property("weight", Pango.Weight.BOLD)
        elif name == "i":
            tag.set_property("style", Pango.Style.ITALIC)
        elif name == "s":
            tag.set_property("strikethrough", True)
        elif name in ("code", "mono", "html"):
            tag.set_property("family", "monospace")
            if name == "code":
                tag.set_property("background", "rgba(127,127,127,0.18)")
                tag.set_property("scale", 0.92)
            elif name == "html":
                tag.set_property("foreground", dim)
            else:
                tag.set_property("scale", 0.92)
        elif name == "link":
            tag.set_property("foreground", "#78aeed" if dark else "#1c71d8")
            tag.set_property("underline", Pango.Underline.SINGLE)
        elif name == "marker":
            tag.set_property("foreground", dim)
        elif name.startswith("hl:"):
            st = self._styles.get(name) or {}
            if st.get("color"):
                tag.set_property("foreground", "#" + st["color"])
            if st.get("bold"):
                tag.set_property("weight", Pango.Weight.BOLD)
            if st.get("italic"):
                tag.set_property("style", Pango.Style.ITALIC)
        self._table.add(tag)
        if name.startswith("blk:"):
            tag.set_priority(0)
        return tag

    # ---- apply ----------------------------------------------------------------------
    def _apply(self, gen, ops, err):
        if gen != self._gen or not self._alive:
            return False
        if self._table is None:
            self._table = Gtk.TextTagTable()
        buf = Gtk.TextBuffer(tag_table=self._table)
        if ops is None:
            buf.set_text(_("Could not render this Markdown: {error}").format(error=err), -1)
            self.view.set_buffer(buf)
            return False
        self._styles = ops.styles
        marks = {}
        tables = []
        end = buf.get_end_iter()
        for op in ops.ops:
            kind = op[0]
            if kind == "t":
                buf.insert_with_tags(end, op[1], *[self._tag(n) for n in op[2]])
            elif kind == "img":
                start_off = end.get_offset()
                buf.insert_paintable(end, op[1])
                s = buf.get_iter_at_offset(start_off)
                for n in op[3]:
                    buf.apply_tag(self._tag(n), s, end)
            elif kind == "mark":
                name = op[1]
                if name not in marks:
                    marks[name] = buf.create_mark(None, end, True)
            elif kind == "table":
                start_off = end.get_offset()
                anchor = buf.create_child_anchor(end)
                buf.insert(end, "\n", -1)
                s = buf.get_iter_at_offset(start_off)
                for n in op[3]:
                    buf.apply_tag(self._tag(n), s, end)
                tables.append((anchor, op[1], op[2]))
        # keep the reading position across re-renders
        vadj = self.get_vadjustment()
        old = vadj.get_value()
        self.view.set_buffer(buf)
        self.links, self.marks = ops.links, marks
        for anchor, rows, aligns in tables:
            self.view.add_child_at_anchor(self._table_widget(rows, aligns), anchor)
        if old > 0:
            def restore():
                vadj.set_value(min(old, vadj.get_upper() - vadj.get_page_size()))
                return False
            GLib.idle_add(restore, priority=GLib.PRIORITY_LOW)
        return False

    def _table_widget(self, rows, aligns):
        grid = Gtk.Grid(css_classes=["md-table"], overflow=Gtk.Overflow.HIDDEN,
                        halign=Gtk.Align.START)
        xal = {"left": 0.0, "center": 0.5, "right": 1.0}
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                lab = Gtk.Label(wrap=True, max_width_chars=48, selectable=True,
                                xalign=xal.get(aligns[c] if c < len(aligns) else "left", 0.0),
                                css_classes=["md-cell"] + (["md-head"] if r == 0 else []))
                lab.set_markup(cell)
                lab.connect("activate-link", lambda _l, uri: self._open_link(uri) or True)
                grid.attach(lab, c, r, 1, 1)
        return grid

    # ---- links --------------------------------------------------------------------------
    def _link_at(self, x, y):
        bx, by = self.view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        ok, it = self.view.get_iter_at_location(bx, by)
        if not ok:
            return None
        for tag in it.get_tags():
            name = tag.get_property("name") or ""
            if name.startswith("link:"):
                n = int(name[5:])
                return self.links[n] if n < len(self.links) else None
        return None

    def _on_motion(self, _c, x, y):
        self.view.set_cursor_from_name("pointer" if self._link_at(x, y) else None)

    def _on_click(self, gesture, n_press, x, y):
        if n_press != 1 or gesture.get_current_button() != 1:
            return
        if self.view.get_buffer().get_has_selection():
            return
        href = self._link_at(x, y)
        if href:
            self._open_link(href)

    def _open_link(self, href):
        if href.startswith("#"):
            mark = self.marks.get("h:" + slugify(GLib.uri_unescape_string(href[1:], None) or ""))
            if mark is not None:
                self.view.scroll_to_mark(mark, 0.0, True, 0.0, 0.0)
            return
        root = self.get_root()
        if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I) and not href.lower().startswith("file:"):
            Gtk.UriLauncher.new(href).launch(root, None, None, None)
            return
        if href.lower().startswith("file:"):
            gf = Gio.File.new_for_uri(href)
        else:
            path = GLib.uri_unescape_string(href.split("#")[0], None) or href
            gf = Gio.File.new_for_path(os.path.join(self.base_dir or "", path))
        if self.on_open_file is not None:
            self.on_open_file(gf)
        else:
            Gtk.FileLauncher.new(gf).launch(root, None, None, None)


HEADING_TAGS = {f"h{i}" for i in range(1, 7)}
