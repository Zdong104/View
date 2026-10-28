"""Translating source code and tables: what is prose, and what stays.

Code (any file with a Pygments lexer): only comments, prose-like string
literals (docstrings, messages) and prose text (between markup tags) are
translated; names, keywords, numbers, pragmas ("# noqa", "-*- coding -*-"),
format placeholders and everything else stay byte for byte.  A comment block
over several lines ("# one\\n# two") is translated as one paragraph and laid
back over the same lines, so the file keeps its line count and structure and
lines up with the original line by line.

    plan = CodePlan(text, lexer)
    plan.pieces            [text to translate] (one per block)
    plan.render({piece: translation}) -> the translated file
    plan.edits({piece: translation}) -> [(start, end, new text)] in the original

Tables: cell_worth(text) tells a text cell from numbers, dates, IDs, codes,
URLs and e-mail addresses.
"""
from __future__ import annotations

import re

_PRAGMA = re.compile(r"^\s*(noqa|type:|pylint:|fmt:|isort:|eslint|@ts-|-\*-|vim?:|nolint|"
                     r"NOSONAR|pragma|region\b|endregion\b|#?\s*(if|endif|else)\b|"
                     r"SPDX-|Copyright\b|\(c\)|©|https?://)", re.I)
_COMMENT_CORE = re.compile(r"^(\s*(?:#+!?|//+!?|/\*+!?|\*+(?!/)|--+|;+|%+|<!--|\{-|\(\*|REM\b|"
                           r"'(?=\s)|\"(?=\s))?\s*)(.*?)(\s*(?:\*+/|-->|-\}|\*\)|#+)?\s*)$",
                           re.S)
_STRING_CORE = re.compile(r"^(\s*[rRbBuUfF]{0,2}(?:\"\"\"|'''|\"|'|`)?\s*)(.*?)"
                          r"(\s*(?:\"\"\"|'''|\"|'|`)?\s*)$", re.S)
_WORD = re.compile(r"[^\W\d_]{2,}")
_WIDE = re.compile(r"[⺀-鿿가-힯豈-﫿]")


def prose(text: str) -> bool:
    """Words a reader would want translated: two words or more (or CJK)."""
    t = text.strip()
    if not t or _PRAGMA.match(t):
        return False
    if _WIDE.search(t):
        return True
    letters = sum(c.isalpha() for c in t)
    nonspace = sum(not c.isspace() for c in t)
    words = [w for w in t.split() if _WORD.search(w)]      # ("user_id" is one word)
    return len(words) >= 2 and letters >= 0.55 * nonspace


def cell_worth(text: str) -> bool:
    """A table cell holding text, not a number, date, ID, code or address."""
    t = (text or "").strip()
    if not t or "@" in t or "://" in t or t.startswith(("=", "www.")):
        return False
    if _WIDE.search(t):
        return True
    if not re.search(r"[^\W\d_]{3,}", t):
        return False                     # numbers, dates, "12 kg", "A1"
    if " " not in t and re.search(r"\d|_|[a-z][A-Z]|^[A-Z0-9-]+$", t):
        return False                     # IDs: "SKU-1234", "user_id", "camelCase", "USD"
    letters = sum(c.isalpha() for c in t)
    return letters >= 0.5 * sum(not c.isspace() for c in t)


def _kind(ttype):
    from pygments.token import Comment, String, Text
    if ttype in Comment:
        if ttype in Comment.Preproc or ttype in Comment.PreprocFile or \
                ttype in Comment.Hashbang:
            return None
        return "comment"
    if ttype in String:
        if any(ttype in getattr(String, k) for k in
               ("Escape", "Interpol", "Regex", "Affix", "Backtick", "Symbol", "Char")):
            return None
        return "string"
    if ttype is Text or ttype in Text and ttype not in Text.Whitespace:
        return "text"
    return None


class CodePlan:
    def __init__(self, text: str, lexer):
        self.text = text
        tokens = []                      # [start, kind, value]
        for start, ttype, value in lexer.get_tokens_unprocessed(text):
            kind = _kind(ttype)
            if kind == "string" and tokens and tokens[-1][1] == "string" and \
                    tokens[-1][0] + len(tokens[-1][2]) == start:
                tokens[-1][2] += value   # one literal: quote + content + quote
            elif kind:
                tokens.append([start, kind, value])
        # slots: (start, end, core, line, group key)
        slots = []
        for start, kind, value in tokens:
            pos = start
            for k, line in enumerate(value.split("\n")):
                rx = _COMMENT_CORE if kind == "comment" else \
                    _STRING_CORE if kind == "string" else None
                if rx is not None:
                    m = rx.match(line)
                    a, core = len(m.group(1)), m.group(2)
                else:
                    a, core = len(line) - len(line.lstrip()), line.strip()
                if kind == "comment":
                    ok = bool(core) and not _PRAGMA.match(core)
                else:
                    ok = bool(core) and prose(core)
                if ok:
                    line_no = text.count("\n", 0, pos)
                    full = kind == "comment" and not text[text.rfind("\n", 0, pos) + 1:pos].strip()
                    slots.append([pos + a, pos + a + len(core), core, line_no,
                                  (kind, start) if kind != "comment" or not full else
                                  ("comment-line", (m.group(1) or "").strip())])
                elif kind != "text" or core:
                    slots.append(None)   # a break in a block (blank comment line)
                pos += len(line) + 1
        # blocks: neighbouring lines of one comment run / one literal
        self.blocks = []
        cur = None
        for s in slots:
            if s is None:
                cur = None
                continue
            if cur is not None and cur[-1][4] == s[4] and cur[-1][3] + 1 == s[3]:
                cur.append(s)
            else:
                cur = [s]
                self.blocks.append(cur)
        # a comment block must say something: two words over its lines
        self.blocks = [b for b in self.blocks if prose(" ".join(s[2] for s in b))]
        self.pieces = [self._joined(b) for b in self.blocks]

    @staticmethod
    def _joined(block):
        out = ""
        for s in block:
            core = s[2].strip()
            if out and not (_WIDE.match(core[:1]) and _WIDE.match(out[-1:])):
                if out.endswith("-") and core[:1].islower():
                    out = out[:-1]
                else:
                    out += " "
            out += core
        return out

    def edits(self, translations: dict):
        """[(start, end, new text)] for the blocks translated so far."""
        out = []
        for block, piece in zip(self.blocks, self.pieces):
            tr = translations.get(piece)
            if tr is None or tr == piece:
                continue
            parts = distribute(tr.replace("\n", " "), [len(s[2]) for s in block])
            for s, p in zip(block, parts):
                out.append((s[0], s[1], p))
        return out

    def render(self, translations: dict) -> str:
        text = self.text
        for a, b, new in sorted(self.edits(translations), key=lambda e: -e[0]):
            text = text[:a] + new + text[b:]
        return text


def distribute(text: str, weights) -> list[str]:
    """``text`` cut into len(weights) parts of about those proportions, at
    spaces (anywhere between wide characters); every part non-empty when it
    can be."""
    n = len(weights)
    if n <= 1:
        return [text]
    total = sum(weights) or n
    cuts, out, pos = [], [], 0
    acc = 0
    for w in weights[:-1]:
        acc += w
        target = round(len(text) * acc / total)
        cuts.append(target)
    for i, c in enumerate(cuts):
        c = max(c, pos)
        # the nearest break: a space, or a boundary between wide characters
        best = None
        for d in range(0, 12):           # after punctuation, if there is some near
            for q in (c + d, c - d):
                if pos < q < len(text) and text[q - 1] in "，。、；：！？,.;:!?)）":
                    best = q
                    break
            if best is not None:
                break
        for d in range(0, 40):
            if best is not None:
                break
            for q in (c + d, c - d):
                if pos < q < len(text) and (text[q] == " " or text[q - 1] == " " or
                                            _WIDE.match(text[q]) or _WIDE.match(text[q - 1])):
                    best = q
                    break
        if best is None:
            best = min(max(c, pos), len(text))
        out.append(text[pos:best].strip())
        pos = best
    out.append(text[pos:].strip())
    return out
