"""Pygments helpers shared by the text viewer and Quick Look.

Lexing is pure Python and may be slow: call lex_runs() from a worker thread.
Offsets are in characters (code points), which is what Gtk.TextBuffer uses.
"""
from __future__ import annotations

_style_cache: dict[bool, object] = {}


def lexer_for(name: str, content_type: str | None = None, text: str | None = None):
    """A Pygments lexer (stripnl/ensurenl off so offsets match the text) or
    None when the file has no useful lexer ("Text only")."""
    try:
        from pygments.lexers import (get_lexer_for_filename, get_lexer_for_mimetype,
                                     guess_lexer)
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    kw = dict(stripnl=False, ensurenl=False)
    lexer = None
    try:
        lexer = get_lexer_for_filename(name, **kw)
    except ClassNotFound:
        if content_type and content_type != "text/plain":
            try:
                lexer = get_lexer_for_mimetype(content_type, **kw)
            except ClassNotFound:
                lexer = None
        if lexer is None and text and text.startswith("#!"):
            try:
                lexer = guess_lexer(text[:4000], **kw)
            except ClassNotFound:
                lexer = None
    if lexer is None or lexer.name in ("Text only", "Text output"):
        return None
    return lexer


def _style(dark: bool):
    st = _style_cache.get(dark)
    if st is None:
        from pygments.styles import get_style_by_name
        from pygments.util import ClassNotFound
        try:
            st = get_style_by_name("github-dark" if dark else "default")
        except ClassNotFound:
            st = get_style_by_name("monokai" if dark else "default")
        _style_cache[dark] = st
    return st


def style_for(ttype, dark: bool) -> dict | None:
    """{'color': 'rrggbb'|None, 'bold': bool, 'italic': bool} or None if the
    token type has no visible styling."""
    style = _style(dark)
    while True:
        try:
            st = style.style_for_token(ttype)
            break
        except KeyError:        # a lexer's own token subtype (YAML's Literal.Scalar.Plain)
            if ttype.parent is None:
                return None
            ttype = ttype.parent
    if st.get("color") or st.get("bold") or st.get("italic"):
        return st
    return None


def lex_runs(text: str, lexer) -> list[tuple[object, int, int]]:
    """[(token_type, start, end)] for the non-whitespace tokens of `text`,
    with adjacent tokens of one type merged."""
    from pygments.token import Token
    runs: list = []
    off = 0
    skip = (Token.Text, Token.Text.Whitespace)
    for ttype, value in lexer.get_tokens(text):
        n = len(value)
        if n == 0:
            continue
        if ttype not in skip:
            if runs and runs[-1][0] is ttype and runs[-1][2] == off:
                runs[-1] = (ttype, runs[-1][1], off + n)
            else:
                runs.append((ttype, off, off + n))
        off += n
    return runs


def pygments_runs(text, name, content_type, dark):
    """Quick Look flavour: (styles, runs) with string keys, or None."""
    lexer = lexer_for(name, content_type, text)
    if lexer is None:
        return None
    styles, runs = {}, []
    for ttype, a, b in lex_runs(text, lexer):
        st = style_for(ttype, dark)
        if st is None:
            continue
        key = str(ttype).replace(".", "_")
        styles[key] = st
        if runs and runs[-1][0] == key and runs[-1][2] == a:
            runs[-1] = (key, runs[-1][1], b)
        else:
            runs.append((key, a, b))
    return styles, runs
