"""Translate Markdown while keeping its structure (no widgets, no engine).

markdown-it-py parses the source; only the human text is handed to the
translator: headings, paragraphs (in lists and block quotes too), table
cells, link text and image alt text.  Everything else stays byte for byte:
code blocks, front matter, HTML blocks, reference definitions, blank lines
and every line the translation does not touch, plus each touched line's
container prefix (``> ``, ``- ``, ``1. ``, ``## `` …).

Inside a paragraph, inline code, inline HTML and autolinks are opaque; they
become placeholders ``X1``, ``X2`` … in the sentence sent to the engine (the
models copy such tokens through) and are put back verbatim.  Emphasis, links
and images are placeholders too, their own text translated as a separate
piece.  When a translation loses or repeats a placeholder, that sentence is
translated again run by run (the text between placeholders), so the markup
always survives.  The new text is escaped where Markdown would read it as
markup.

    plan = Plan(text)
    while plan.pending():                     # [(id, text to translate)]
        ...  plan.put(id, translation)
    plan.render() -> translated Markdown (untranslated parts kept as is)

or ``translate(text, fn)`` with fn(list of str) -> list of str.
"""
from __future__ import annotations

import re
import unicodedata

FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?\n)(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.S)
_TASK = re.compile(r"\[[ xX]\] ")
_LETTER = re.compile(r"[^\W\d_]")


def parser():
    from markdown_it import MarkdownIt
    md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
    md.normalizeLink = lambda url: url          # keep URLs exactly as written
    md.normalizeLinkText = lambda url: url
    return md


# ---------------------------------------------------------------------------
# Escaping new text so Markdown reads it as text
# ---------------------------------------------------------------------------
def escape(s: str, table: bool = False) -> str:
    s = s.replace("\\", "\\\\")
    s = re.sub(r"([`*_\[\]<])", r"\\\1", s)
    s = s.replace("~~", "\\~\\~")
    s = re.sub(r"&(?=#?\w+;)", "&amp;", s)
    if table:
        s = s.replace("|", "\\|")
    return s


def _line_start(s: str) -> str:
    """Escape what would start a block at the beginning of a line."""
    s = s.lstrip()
    if s[:1] in ("#", ">", "-", "+", "=", "~"):
        return "\\" + s
    m = re.match(r"(\d{1,9})([.)])(\s|$)", s)
    if m:
        return m.group(1) + "\\" + s[len(m.group(1)):]
    return s


def _is_punct(ch: str) -> bool:
    return bool(ch) and unicodedata.category(ch).startswith("P")


# ---------------------------------------------------------------------------
# Inline items
#   ("t", text)                 human text (decoded)
#   ("sb",)                     soft line break
#   ("atom", markdown)          kept verbatim (code, inline HTML, autolink)
#   ("span", open, run, close, emph)   emphasis / strikethrough / link
#   ("img", run, tail)          image: run is its alt text, tail "](src)"
# A run is a list of items translated together as one piece (a _Seg).
# ---------------------------------------------------------------------------
def _dest(href: str, title: str | None) -> str:
    if not href or re.search(r"[\s<>()]", href):
        d = "<" + href.replace("<", "%3C").replace(">", "%3E") + ">"
    else:
        d = href
    if title:
        d += ' "' + title.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return d


def _code(c) -> str:
    content, ticks = c.content, c.markup or "`"
    pad = " " if (content[:1] == "`" or content[-1:] == "`" or
                  (content[:1] == " " and content[-1:] == " " and content.strip())) else ""
    return ticks + pad + content + pad + ticks


def _items(children, i=0, stop=None):
    """Children tokens -> (items, next index); stops after the ``stop`` close."""
    out = []
    while i < len(children):
        c = children[i]
        t = c.type
        if stop is not None and t == stop:
            return out, i + 1
        if t == "text":
            if out and out[-1][0] == "t":
                out[-1] = ("t", out[-1][1] + c.content)
            else:
                out.append(("t", c.content))
        elif t == "softbreak":
            out.append(("sb",))
        elif t == "hardbreak":
            out.append(("hb",))
        elif t == "code_inline":
            out.append(("atom", _code(c)))
        elif t == "html_inline":
            out.append(("atom", c.content))
        elif t in ("em_open", "strong_open", "s_open"):
            inner, i = _items(children, i + 1, t.replace("_open", "_close"))
            out.append(("span", c.markup, inner, c.markup, True))
            continue
        elif t == "link_open":
            inner, j = _items(children, i + 1, "link_close")
            if c.markup == "autolink" or c.info == "auto":
                text = "".join(x[1] for x in inner if x[0] == "t")
                out.append(("atom", "<" + text + ">"))
            else:
                out.append(("span", "[", inner,
                            "](" + _dest(c.attrGet("href") or "", c.attrGet("title")) + ")",
                            False))
            i = j
            continue
        elif t == "image":
            alt = "".join(x.content for x in (c.children or ()) if x.type in
                          ("text", "code_inline")) or ""
            tail = "](" + _dest(c.attrGet("src") or "", c.attrGet("title")) + ")"
            out.append(("img", [("t", alt)] if alt else [], tail))
        elif t.endswith("_close"):
            pass                         # unbalanced close: ignore
        else:                            # unknown inline token: keep its text
            if c.content:
                out.append(("atom", escape(c.content)))
        i += 1
    return out, i


class _Seg:
    """One piece of text for the engine: a run with its markup masked."""
    __slots__ = ("run", "letter", "n", "masked", "result", "fallback", "id")

    def __init__(self, plan, run):
        self.run = run
        self.result = None
        self.fallback = None             # [_Seg] per text run, when result lost markup
        self.letter = plan.letter
        parts, n = [], 0
        for k, it in enumerate(run):
            if it[0] == "t":
                parts.append(it[1])
            elif it[0] in ("sb", "hb"):
                a = run[k - 1][1][-1:] if k and run[k - 1][0] == "t" else ""
                b = run[k + 1][1][:1] if k + 1 < len(run) and run[k + 1][0] == "t" else ""
                parts.append("" if a and b and ord(a) > 0x2e80 and ord(b) > 0x2e80 else " ")
            else:
                n += 1
                parts.append(f"{self.letter}{n}")
                if it[0] == "span":
                    plan.seg(it[2])      # their own text is a piece of its own
                elif it[0] == "img" and it[1]:
                    plan.seg(it[1])
        self.n = n
        self.masked = re.sub(r"[ \t]+", " ", "".join(parts)).strip()
        self.id = len(plan.segs)
        plan.segs.append(self)
        if not self.needs_translation():
            self.result = self.masked

    def needs_translation(self):
        return bool(_LETTER.search(re.sub(self.letter + r"\d+", "", self.masked)))

    def valid(self, text):
        found = re.findall(self.letter + r"(\d+)", text)
        return sorted(int(x) for x in found) == list(range(1, self.n + 1))


class Plan:
    def __init__(self, text: str):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.text = text
        self.lines = text.split("\n")
        used = set(re.findall(r"([A-Z])\d", text))
        self.letter = next((c for c in "XYZWQKJV" if c not in used), "X")
        self.segs: list[_Seg] = []
        self._by_run = {}               # id(run) -> _Seg
        self.blocks = []                # [(line0, nlines, kind, prefixes, suffix, runs)]
        self.rows = []                  # [(line, [(start, end, run) per cell])]
        fm = FRONT_MATTER.match(text)
        off = fm.group(0).count("\n") if fm else 0
        body = text[fm.end():] if fm else text
        self._walk(parser().parse(body), off)

    # ---- building -------------------------------------------------------------
    def seg(self, run):
        s = self._by_run.get(id(run))
        if s is None:
            s = _Seg(self, run)
            self._by_run[id(run)] = s
        return s

    def _walk(self, tokens, off):
        n = len(tokens)
        i = 0
        while i < n:
            tok = tokens[i]
            if tok.type == "inline" and tok.map and i > 0:
                opener = tokens[i - 1]
                if opener.type == "paragraph_open":
                    first_in_item = i > 1 and tokens[i - 2].type == "list_item_open"
                    self._para(tok, off, "p", first_in_item)
                elif opener.type == "heading_open":
                    self._para(tok, off, "atx" if opener.markup.startswith("#") else "p", False)
            elif tok.type == "tr_open" and tok.map:
                j = i + 1
                cells = []
                while j < n and tokens[j].type != "tr_close":
                    if tokens[j].type == "inline":
                        cells.append(tokens[j])
                    j += 1
                self._row(tok.map[0] + off, cells)
                i = j
            i += 1

    def _para(self, inline, off, kind, first_in_item):
        a, b = inline.map[0] + off, inline.map[1] + off
        items, _ = _items(inline.children or [])
        content = inline.content.split("\n")
        if not inline.content.strip() or b - a < 1:
            return
        nlines = len(content) if kind == "p" else 1
        if a + nlines > len(self.lines):
            return
        prefixes, suffix = [], ""
        for j in range(nlines):
            src = self.lines[a + j]
            cl = content[j] if j < len(content) else ""
            if kind == "atx":
                c = inline.content
                idx = src.rfind(c)
                if idx < 0 or not re.fullmatch(r"(\s+#+)?\s*", src[idx + len(c):]):
                    return
                suffix = src[idx + len(c):]
            else:
                r, c = src.rstrip(), cl.rstrip()
                idx = len(r) - len(c)
                if not c or idx < 0 or r[idx:] != c:
                    idx = src.find(cl.strip())
                    if idx < 0 or not cl.strip():
                        return
            prefixes.append(src[:idx])
        task = ""
        if first_in_item and items and items[0][0] == "t":
            m = _TASK.match(items[0][1])
            if m:
                task = m.group(0)
                items[0] = ("t", items[0][1][len(task):])
        # runs between hard breaks; remember on which line each run starts
        runs, starts, cur, line = [], [0], [], 0
        for it in items:
            if it[0] == "hb":
                runs.append(cur)
                cur = []
                line += 1
                starts.append(min(line, nlines - 1))
            else:
                if it[0] == "sb":
                    line += 1
                cur.append(it)
        runs.append(cur)
        for r in runs:
            self.seg(r)
        self.blocks.append((a, nlines, kind, prefixes, suffix, task, runs, starts))

    def _row(self, line, cells):
        if line >= len(self.lines) or not cells:
            return
        src = self.lines[line]
        bounds, start, k = [], 0, 0
        while k < len(src):
            if src[k] == "\\":
                k += 2
                continue
            if src[k] == "|":
                bounds.append((start, k))
                start = k + 1
            k += 1
        bounds.append((start, len(src)))
        if bounds and not src[bounds[0][0]:bounds[0][1]].strip() and src.lstrip().startswith("|"):
            bounds = bounds[1:]
        if bounds and not src[bounds[-1][0]:bounds[-1][1]].strip():
            bounds = bounds[:-1]
        out = []
        for (s, e), cell in zip(bounds, cells):
            piece = src[s:e].strip()
            if not piece or (piece != cell.content and piece.replace("\\|", "|") != cell.content):
                continue
            items, _ = _items(cell.children or [])
            items = [("sb",) if it[0] == "hb" else it for it in items]
            lead = len(src[s:e]) - len(src[s:e].lstrip())
            trail = len(src[s:e]) - len(src[s:e].rstrip())
            out.append((s + lead, e - trail, items))
            self.seg(items)
        if out:
            self.rows.append((line, out))

    # ---- translating -------------------------------------------------------------
    def pending(self) -> list[tuple[int, str]]:
        """Pieces still to translate: [(id, text)]."""
        out = []
        for s in self.segs:
            if s.result is None:
                out.append((s.id, s.masked))
            elif s.fallback:
                out += [(f.id, f.masked) for f in s.fallback if f.result is None]
        return out

    def put(self, sid: int, text: str):
        s = self.segs[sid]
        s.result = (text or "").strip()
        if s.n and s.result and not s.valid(s.result) and s.fallback is None:
            # the markup got lost: translate the text between placeholders alone
            s.fallback = []
            run_t = []
            for it in s.run:
                if it[0] in ("t", "sb"):
                    run_t.append(it)
                elif run_t:
                    s.fallback.append(self._sub(run_t))
                    run_t = []
            if run_t:
                s.fallback.append(self._sub(run_t))

    def _sub(self, run):
        return self.seg(list(run))

    def progress(self) -> tuple[int, int]:
        done = total = 0
        for s in self.segs:
            total += 1
            if s.result is not None and (not s.fallback or
                                         all(f.result is not None for f in s.fallback)):
                done += 1
        return done, total

    def sample(self, limit=4000) -> str:
        """Plain human text, for detecting the language."""
        out, n = [], 0
        for s in self.segs:
            t = re.sub(self.letter + r"\d+", " ", s.masked).strip()
            if t:
                out.append(t)
                n += len(t)
                if n > limit:
                    break
        return "\n".join(out)

    # ---- rendering -------------------------------------------------------------
    def _run_md(self, run, table=False):
        """A run as Markdown: its translation when there is a usable one."""
        s = self._by_run.get(id(run))
        if s is None or s.result is None:
            return None
        if s.fallback is not None:
            return self._run_fallback(s, table)
        parts = re.split("(" + s.letter + r"\d+)", s.result)
        atoms = [it for it in run if it[0] not in ("t", "sb", "hb")]
        out = []
        for p in parts:
            m = re.fullmatch(s.letter + r"(\d+)", p)
            if m:
                out.append(self._item_md(atoms[int(m.group(1)) - 1], table))
            else:
                out.append(escape(p, table))
        return "".join(out)

    def _run_fallback(self, s, table):
        out, k = [], 0
        pending_t = False
        for it in s.run:
            if it[0] in ("t", "sb", "hb"):
                if not pending_t:
                    f = s.fallback[k]
                    k += 1
                    md = self._run_md(f.run, table) if f.result is not None else None
                    out.append(md if md is not None else self._run_orig(f.run, table))
                    out.append(" ")
                    pending_t = True
            else:
                pending_t = False
                out.append(self._item_md(it, table))
        return re.sub(r" +", " ", "".join(out)).strip()

    def _run_orig(self, run, table=False):
        out = []
        for it in run:
            if it[0] == "t":
                out.append(escape(it[1], table))
            elif it[0] in ("sb", "hb"):
                out.append(" ")
            else:
                out.append(self._item_md(it, table, translated=False))
        return "".join(out)

    def _item_md(self, it, table, translated=True):
        kind = it[0]
        if kind == "atom":
            return it[1].replace("|", "\\|") if table and "\\|" not in it[1] else it[1]
        if kind == "span":
            _k, op, run, cl, emph = it
            inner = self._run_md(run, table) if translated else None
            if inner is None:
                return op + self._run_orig(run, table) + cl
            inner = inner.strip()
            if emph:
                # "*" works between any characters ("_" not inside words, as
                # in Chinese), and punctuation at the edges of the new text
                # goes outside the markers: "**注意：**这" would not be bold
                op = cl = "*" * len(op) if op[0] == "_" else op
                a, b = 0, len(inner)
                while a < b and _is_punct(inner[a]):
                    a += 1
                while b > a and _is_punct(inner[b - 1]):
                    b -= 1
                if a == b:
                    return op + inner + cl
                return inner[:a] + op + inner[a:b] + cl + inner[b:]
            return op + inner + cl
        if kind == "img":
            run, tail = it[1], it[2]
            alt = (self._run_md(run, table) if translated and run else None)
            if alt is None:
                alt = self._run_orig(run, table) if run else ""
            return "![" + alt + tail
        return ""

    def _touched(self, run) -> bool:
        """Some text in this run (or its links, emphasis…) is translated."""
        s = self._by_run.get(id(run))
        if s is not None and s.result is not None and s.needs_translation():
            return True
        return any(self._touched(it[2] if it[0] == "span" else it[1])
                   for it in run if it[0] in ("span", "img"))

    def render(self) -> str:
        lines = list(self.lines)
        edits = []                       # (line, count, new lines)
        for a, nlines, kind, prefixes, suffix, task, runs, starts in self.blocks:
            if not any(self._touched(r) for r in runs):
                continue                 # nothing new: keep the lines as written
            new = []
            for k, r in enumerate(runs):
                md = self._run_md(r)
                if md is None:
                    md = self._run_orig(r)
                md = _line_start(md.strip()) if md.strip() else ""
                if kind == "atx":
                    md = re.sub(r"(\s)(#+)\s*$", r"\1\\\2", md)
                    new.append(prefixes[0] + md + suffix)
                    continue
                if k == 0:
                    pre = prefixes[0] + task
                elif nlines > 1:
                    pre = prefixes[max(1, min(starts[k], nlines - 1))]
                else:                    # a continuation line: no list marker
                    pre = re.sub(r"([-+*]|\d{1,9}[.)])(?=\s|$)",
                                 lambda m: " " * len(m.group(1)), prefixes[0])
                new.append(pre + md + ("\\" if k < len(runs) - 1 else ""))
            edits.append((a, nlines, new))
        for line, cells in self.rows:
            src = lines[line]
            out, pos = [], 0
            changed = False
            for s, e, run in cells:
                if not self._touched(run):
                    continue
                md = self._run_md(run, table=True)
                if md is None:
                    md = self._run_orig(run, table=True)
                changed = True
                out.append(src[pos:s])
                out.append(md.strip() or src[s:e])
                pos = e
            if changed:
                out.append(src[pos:])
                edits.append((line, 1, ["".join(out)]))
        for a, n, new in sorted(edits, key=lambda e: -e[0]):
            lines[a:a + n] = new
        return "\n".join(lines)


def translate(text: str, fn, cancelled=None) -> str:
    """Translate Markdown ``text`` with fn([str]) -> [str] (blocking)."""
    plan = Plan(text)
    for _round in range(3):
        todo = plan.pending()
        if not todo:
            break
        if cancelled and cancelled():
            raise RuntimeError("Cancelled")
        for (sid, _t), res in zip(todo, fn([t for _i, t in todo])):
            plan.put(sid, res)
    return plan.render()


def structure(text: str) -> list:
    """Block and inline markup of ``text`` (no text): equal for a document
    and its translation."""
    fm = FRONT_MATTER.match(text)
    body = text[fm.end():] if fm else text
    out = [("fm", fm.group(0)) if fm else ("fm", None)]
    for tok in parser().parse(body):
        if tok.type == "inline":
            kids = []
            for c in tok.children or ():
                if c.type in ("text", "softbreak"):
                    continue
                if c.type == "code_inline":
                    kids.append(("code", c.content))
                elif c.type == "link_open":
                    kids.append(("link", c.attrGet("href")))
                elif c.type == "image":
                    kids.append(("image", c.attrGet("src")))
                elif c.type == "html_inline":
                    kids.append(("html", c.content))
                else:
                    kids.append((c.type,))
            out.append(("inline", tuple(kids)))
        elif tok.type in ("fence", "code_block", "html_block"):
            out.append((tok.type, tok.info, tok.content))
        else:
            out.append((tok.type, tok.tag))
    return out
