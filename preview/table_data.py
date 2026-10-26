"""Document model behind the table viewer (CSV / TSV).  GTK-free.

Design (built for ~1M rows):
  * The file stays in memory as raw bytes (or an mmap for huge files); a numpy
    int64 array holds each record's byte offset.  Rows are parsed lazily, only
    when something asks for them (visible cells, search hits, sort keys, ...).
  * Rows are identified by *keys*: base rows by their record index in the file,
    new rows by negative numbers.  `order` is the document order (array of
    keys), `view` the displayed order (sorted / filtered), `edits` maps key ->
    full replacement row.  Nothing is copied per cell edit.
  * Structural edits are undone by restoring numpy-array snapshots (arrays are
    never mutated in place), cell edits by per-row deltas.
  * Saving copies unmodified runs of the original bytes verbatim, so quoting,
    line endings and encoding of untouched rows are preserved exactly.
"""
from __future__ import annotations

import bisect
import codecs
import csv
import io
import json
import mmap
import os
import re
import tempfile
import time

import numpy as np
from .i18n import _
from .runtime import replace_file

csv.field_size_limit(1 << 30)

MMAP_ABOVE = 512 * 1024 * 1024
DELIMS = ",\t;|"
DELIM_NAMES = {",": "Comma", "\t": "Tab", ";": "Semicolon", "|": "Pipe"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


_CUR = "$€£¥"


def to_num(s: str):
    """Float value of a numeric-looking cell, else None (handles 1,234.5 / $9 / 12%)."""
    s = s.strip()
    if not s:
        return None
    c = s[0]
    if not (c.isdigit() or c in "+-.(" or c in _CUR):
        return None
    try:
        if "_" not in s:
            v = float(s)
            return None if v != v else v
    except ValueError:
        pass
    t = s
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    t = t.strip().lstrip(_CUR).replace(",", "").rstrip("%").strip()
    if not t or not (t[0].isdigit() or t[0] in "+-."):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    if v != v:
        return None
    return -v if neg else v


_DATE_RES = [
    re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?$"),
    re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(\s+\d{1,2}:\d{2}(:\d{2})?)?$"),
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日?$"),
    re.compile(r"^[A-Za-z]{3,9}\.? \d{1,2},? \d{4}$"),
    re.compile(r"^\d{1,2} [A-Za-z]{3,9}\.? \d{4}$"),
]


def is_date(s: str) -> bool:
    s = s.strip()
    return bool(s) and any(r.match(s) for r in _DATE_RES)


def infer_type(values) -> str:
    n = num = dt = 0
    for v in values:
        if not v or v.isspace():
            continue
        n += 1
        if to_num(v) is not None:
            num += 1
        elif is_date(v):
            dt += 1
    if n == 0:
        return "empty"
    if num >= 0.9 * n:
        return "number"
    if dt >= 0.9 * n:
        return "date"
    return "text"


def _can_encode(text: str, enc: str) -> bool:
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class Command:
    __slots__ = ("label", "undo", "redo", "nbytes")

    def __init__(self, label, undo, redo, nbytes=0):
        self.label, self.undo, self.redo, self.nbytes = label, undo, redo, nbytes


class TableError(Exception):
    pass


# ---------------------------------------------------------------------------
class TableData:
    UNDO_BYTES = 768 * 1024 * 1024

    def __init__(self):
        self.path: str | None = None
        self.data = b""
        self.enc = "utf-8"            # internal (ASCII-compatible) encoding of self.data
        self.out_enc = "utf-8"        # encoding written on save
        self.enc_label = "UTF-8"
        self.bom = b""
        self.base_off = 0
        self.delim = ","
        self.quote = '"'
        self.quoting = "minimal"      # or "all"
        self.eol = b"\n"
        self.out_eol = b"\n"
        self._scan_nl = 10
        self.has_header = False
        self.header_key = -1
        self.ends_with_newline = True
        self.file_size = 0
        self.read_seconds = 0.0

        self._starts = np.zeros(4096, np.int64)
        self.nbase = 0
        self._voff = 0
        self.loaded = False
        self.cancelled = False

        self.ncols = 1
        self.base_ncols = 1
        self.headers: list[str] = [""]
        self._orig_headers: list[str] | None = None
        self.colmap: list[int] = [0]
        self._cm_identity = True
        self.col_w: list = [None]

        self.edits: dict[int, list[str]] = {}
        self._next_new = -1
        self.order = np.zeros(0, np.int64)
        self.view = self.order

        self.sort_col: int | None = None
        self.sort_asc = True
        self.filter_on = False
        self.search_text = ""
        self.match_keys = np.zeros(0, np.int64)
        self.match_vpos = np.zeros(0, np.int64)

        self._rc: dict[int, list[str]] = {}
        self._colvals: dict[int, list[str]] = {}
        self._types: list[str] | None = None

        self.undo_stack: list[Command] = []
        self.redo_stack: list[Command] = []
        self._undo_bytes = 0
        self._group: list[Command] | None = None
        self._saved_marker: object = None
        self.mut = 0
        self.listeners: list = []

    # ---- events ----------------------------------------------------------------
    def _emit(self, kind, *a):
        for cb in list(self.listeners):
            try:
                cb(kind, *a)
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()

    # ======================================================================
    # Opening / detection
    # ======================================================================
    def open_path(self, path, encoding=None, delimiter=None, header=None):
        """Read the file and detect encoding / dialect (blocking, ~100 ms for
        100 MB).  Rows are indexed afterwards with scan()."""
        t0 = time.perf_counter()
        self.path = path
        size = os.path.getsize(path)
        self.file_size = size
        with open(path, "rb") as f:
            if size > MMAP_ABOVE:
                data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            else:
                data = f.read()
        self.data = data
        self._detect_encoding(encoding)
        self._starts[0] = self.base_off
        self._detect_dialect(delimiter, header, path)
        self.read_seconds = time.perf_counter() - t0

    def _detect_encoding(self, forced):
        data = self.data
        head = bytes(data[:4])
        self.base_off = 0
        self.bom = b""
        enc = None
        forced_l = (forced or "").lower()
        # BOMs
        if head.startswith(codecs.BOM_UTF32_LE) or head.startswith(codecs.BOM_UTF32_BE):
            be = head.startswith(codecs.BOM_UTF32_BE)
            self._transcode("utf-32-be" if be else "utf-32-le", head[:4])
            return
        if head.startswith(codecs.BOM_UTF8):
            self.bom = codecs.BOM_UTF8
            self.base_off = 3
            self.enc, self.out_enc, self.enc_label = "utf-8", "utf-8", "UTF-8 (BOM)"
            return
        if head[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
            self._transcode("utf-16-be" if head[:2] == codecs.BOM_UTF16_BE else "utf-16-le", head[:2])
            return
        if forced_l in ("utf-16", "utf-16-le", "utf-16-be", "utf-32"):
            self._transcode({"utf-16": "utf-16-le", "utf-32": "utf-32-le"}.get(forced_l, forced_l), b"")
            return
        if forced_l in ("utf-8-sig",):
            self.bom = codecs.BOM_UTF8
            self.enc, self.out_enc, self.enc_label = "utf-8", "utf-8", "UTF-8 (BOM)"
            return
        if forced:
            codecs.lookup(forced)
            self.enc = self.out_enc = forced
            self.enc_label = _enc_label(forced)
            return
        # no BOM: utf-8 if valid, else a legacy encoding
        if self._valid_utf8():
            self.enc = self.out_enc = "utf-8"
            self.enc_label = "UTF-8"
            return
        sample = bytes(data[: 256 * 1024])
        guess = None
        try:
            import chardet
            r = chardet.detect(sample)
            name = (r.get("encoding") or "").lower()
            if name and r.get("confidence", 0) >= 0.75 and name not in ("ascii", "utf-8", "windows-1252", "iso-8859-1"):
                if name in ("gb2312", "gbk"):
                    name = "gb18030"
                codecs.lookup(name)
                sample.decode(name, "strict")
                guess = name
        except Exception:  # noqa: BLE001
            guess = None
        if guess is None:
            try:
                bytes(data[: 1 << 20]).decode("cp1252", "strict")
                guess = "cp1252"
            except UnicodeDecodeError:
                guess = "latin-1"
        self.enc = self.out_enc = guess
        self.enc_label = _enc_label(guess)

    def _valid_utf8(self) -> bool:
        data = self.data
        n = len(data)
        dec = codecs.getincrementaldecoder("utf-8")()
        step = 8 << 20
        try:
            for i in range(0, n, step):
                dec.decode(bytes(data[i:i + step]), final=(i + step >= n))
            if n == 0:
                dec.decode(b"", final=True)
        except UnicodeDecodeError:
            return False
        return True

    def _transcode(self, enc, bom):
        text = bytes(self.data).decode(enc, "replace")
        if text.startswith("﻿"):
            text = text[1:]
        self.data = text.encode("utf-8")
        self.bom = bom
        self.enc = "utf-8"
        self.out_enc = enc
        self.enc_label = enc.upper().replace("-LE", "").replace("-BE", "") + (" (BOM)" if bom else "")

    def _detect_dialect(self, delimiter, header, path):
        data = self.data
        s0 = self.base_off
        raw = bytes(data[s0:s0 + 200_000])
        # line ending
        crlf, lf, cr = raw.count(b"\r\n"), raw.count(b"\n"), raw.count(b"\r")
        if lf == 0 and cr == 0 and len(data) > len(raw):
            probe = bytes(data[s0:s0 + 4_000_000])
            crlf, lf, cr = probe.count(b"\r\n"), probe.count(b"\n"), probe.count(b"\r")
        if lf == 0 and cr > 0:
            self.eol, self._scan_nl = b"\r", 13
        elif crlf and crlf >= lf * 0.5:
            self.eol, self._scan_nl = b"\r\n", 10
        else:
            self.eol, self._scan_nl = b"\n", 10
        self.out_eol = self.eol
        tail = bytes(data[-4:]) if len(data) else b""
        self.ends_with_newline = (not tail) or tail.endswith(b"\n") or (self._scan_nl == 13 and tail.endswith(b"\r"))
        # sample text cut at a line boundary
        cut = raw.rfind(bytes([self._scan_nl]))
        if cut > 0 and len(data) - s0 > len(raw):
            raw = raw[:cut + 1]
        sample = raw.decode(self.enc, "replace")

        ext = os.path.splitext(path or "")[1].lower()
        if delimiter:
            self.delim = delimiter
        else:
            self.delim = self._pick_delim(sample, ext)
        # quoting style: every field of the first lines quoted -> QUOTE_ALL
        lines = [l for l in sample.splitlines() if l.strip()][:5]
        allq = bool(lines)
        for l in lines:
            fields = l.split(self.delim)
            if not all(f.startswith('"') and f.endswith('"') and len(f) >= 2 for f in fields):
                allq = False
                break
        self.quoting = "all" if allq else "minimal"
        # header
        self._header_guess = self._guess_header(sample)
        self.has_header = self._header_guess if header is None else bool(header)

    def _pick_delim(self, sample: str, ext: str) -> str:
        best, best_score = ",", -1.0
        text = sample[:120_000]
        sniff = None
        try:
            sniff = csv.Sniffer().sniff(text[:30_000], delimiters=DELIMS).delimiter
        except Exception:  # noqa: BLE001
            pass
        for d in DELIMS:
            try:
                rd = csv.reader(io.StringIO(text, newline=""), delimiter=d)
                rows = []
                for r in rd:
                    if r:
                        rows.append(len(r))
                    if len(rows) >= 200:
                        break
            except csv.Error:
                continue
            if not rows:
                continue
            from collections import Counter
            cnt = Counter(rows)
            mode, freq = cnt.most_common(1)[0]
            score = (freq / len(rows)) if mode > 1 else 0.0
            if mode > 1:
                score += min(mode, 20) * 0.002
            if d == sniff:
                score += 0.15
            if ext in (".tsv", ".tab") and d == "\t":
                score += 0.3
            elif ext == ".csv" and d == ",":
                score += 0.05
            if score > best_score:
                best, best_score = d, score
        return best

    def _guess_header(self, sample: str) -> bool:
        try:
            rows = []
            for r in csv.reader(io.StringIO(sample[:60_000], newline=""), delimiter=self.delim):
                if r:
                    rows.append(r)
                if len(rows) >= 60:
                    break
        except csv.Error:
            return True
        if not rows:
            return False
        first = [c.strip() for c in rows[0]]
        filled = [c for c in first if c]
        if not filled or len(filled) < 0.6 * len(first):
            return False
        nums = sum(1 for c in filled if to_num(c) is not None)
        if nums > 0.3 * len(filled):
            return False
        if len(set(filled)) != len(filled):
            return False
        if len(rows) > 1:
            width = len(rows[0])
            for c in range(width):
                col = [r[c] for r in rows[1:] if c < len(r) and r[c].strip()]
                if len(col) >= 3 and sum(1 for v in col if to_num(v) is not None) >= 0.9 * len(col) \
                        and to_num(first[c]) is None:
                    return True
        try:
            if csv.Sniffer().has_header(sample[:30_000]):
                return True
        except Exception:  # noqa: BLE001
            pass
        return len(first) > 1

    # ======================================================================
    # Row index
    # ======================================================================
    def scan(self):
        """Generator yielding np.int64 arrays of record end offsets (exclusive,
        after the line terminator), chunk by chunk.  Quote-aware: a newline
        inside "…" does not end a record.  Runs on a worker thread."""
        data = self.data
        n = len(data)
        nlb = self._scan_nl
        qb = ord(self.quote)
        db = ord(self.delim)
        pos = self.base_off
        carry = 0
        size = 256 * 1024
        last_end = pos
        while pos < n:
            if self.cancelled:
                return
            end = min(n, pos + size)
            lo = pos - 1 if pos > self.base_off else pos
            hi = min(n, end + 1)
            view = np.frombuffer(data, np.uint8, count=hi - lo, offset=lo)
            m = end - pos
            core = view[pos - lo: pos - lo + m]
            isnl = core == nlb
            isq = core == qb
            if carry == 0 and not isq.any():
                nl = np.flatnonzero(isnl)
            else:
                buf = np.empty(m + 2, np.uint8)
                buf[0] = view[0] if pos > lo else nlb
                buf[1:-1] = core
                buf[-1] = view[-1] if hi > end else nlb
                p, nx = buf[:-2], buf[2:]
                sp = (p == db) | (p == 10) | (p == 13)
                sn = (nx == db) | (nx == 10) | (nx == 13)
                cnt = isq & (sp | sn | (p == qb) | (nx == qb))
                par = np.bitwise_xor.accumulate(cnt.view(np.uint8)) ^ np.uint8(carry)
                nl = np.flatnonzero(isnl & (par == 0))
                carry = int(par[-1])
            if len(nl):
                ends = nl.astype(np.int64) + (pos + 1)
                last_end = int(ends[-1])
                yield ends
            pos = end
            if size < (16 << 20):
                size *= 4
        if last_end < n:
            yield np.array([n], np.int64)

    def _ensure_starts(self, extra):
        need = self.nbase + 1 + extra
        if need > len(self._starts):
            new = np.zeros(max(need, len(self._starts) * 2), np.int64)
            new[: self.nbase + 1] = self._starts[: self.nbase + 1]
            self._starts = new

    def append_rows(self, ends: np.ndarray):
        self._ensure_starts(len(ends))
        self._starts[self.nbase + 1: self.nbase + 1 + len(ends)] = ends
        self.nbase += len(ends)

    # ---- parsing -----------------------------------------------------------
    def _parse_raw(self, key: int) -> list[str]:
        s, e = int(self._starts[key]), int(self._starts[key + 1])
        text = self.data[s:e].decode(self.enc, "replace")
        try:
            return next(csv.reader(io.StringIO(text, newline=""), delimiter=self.delim,
                                   quotechar=self.quote), [])
        except csv.Error:
            return text.rstrip("\r\n").split(self.delim)

    def _parse_range(self, i: int, j: int) -> list[list[str]]:
        if j <= i:
            return []
        s, e = int(self._starts[i]), int(self._starts[j])
        text = self.data[s:e].decode(self.enc, "replace")
        try:
            rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=self.delim, quotechar=self.quote))
        except csv.Error:
            rows = []
        if len(rows) != j - i:
            rows = [self._parse_raw(k) for k in range(i, j)]
        return rows

    def _map_cols(self, r: list[str]) -> list[str]:
        n = self.ncols
        if self._cm_identity:
            ln = len(r)
            if ln == n:
                return r
            return r + [""] * (n - ln) if ln < n else r[:n]
        ln = len(r)
        return [r[c] if 0 <= c < ln else "" for c in self.colmap]

    def row(self, key: int) -> list[str]:
        """Cells of a row (len == ncols)."""
        r = self.edits.get(key)
        if r is not None:
            return r
        if key < 0 or key >= self.nbase:
            return [""] * self.ncols
        r = self._rc.get(key)
        if r is None:
            if len(self._rc) > 8000:
                self._rc.clear()
            r = self._map_cols(self._parse_raw(key))
            self._rc[key] = r
        return r

    def cell(self, key: int, col: int) -> str:
        r = self.row(key)
        return r[col] if col < len(r) else ""

    # ---- bookkeeping after the index is complete -----------------------------------
    def init_columns(self):
        """Estimate the column count from the rows indexed so far; builds headers.
        Call once after the first chunk of rows is indexed."""
        width = max([len(self._parse_raw(k)) for k in self._sample_keys()] or [1])
        self.base_ncols = max(width, 1)
        self.colmap = list(range(self.base_ncols))
        self.ncols = self.base_ncols
        self._cm_identity = True
        if self.has_header and self.nbase > 0:
            hdr = self._parse_raw(0)
            self.header_key = 0
            self._voff = 1
        else:
            hdr = []
            self.header_key = -1
            self._voff = 0
        self.headers = (hdr + [""] * self.ncols)[: self.ncols]
        self._orig_headers = list(self.headers)
        self.col_w = [None] * self.ncols
        self._rc.clear()
        self._types = None

    def _sample_keys(self):
        n = self.nbase
        keys = set(range(min(n, 2500)))
        keys.update(range(max(0, n - 600), n))
        if n > 3000:
            keys.update(range(0, n, max(1, n // 700)))
        return sorted(keys)

    def finish_load(self):
        self.order = np.arange(self._voff, self.nbase, dtype=np.int64)
        self.view = self.order
        self.loaded = True
        # resample columns across the whole file
        prev = self.ncols
        self.init_columns_after_scan()
        self._saved_marker = self._top_marker()
        self._emit("loaded", prev != self.ncols)

    def init_columns_after_scan(self):
        width = max([len(self._parse_raw(k)) for k in self._sample_keys()] or [1])
        if width > self.ncols:
            for c in range(self.ncols, width):
                self.colmap.append(c)
                self.headers.append("")
                self._orig_headers.append("")
                self.col_w.append(None)
            self.ncols = self.base_ncols = width
            self._cm_identity = True
            self._rc.clear()
        self._types = None

    @property
    def n_view(self) -> int:
        return len(self.view) if self.loaded else max(0, self.nbase - self._voff)

    @property
    def n_rows(self) -> int:
        return len(self.order) if self.loaded else max(0, self.nbase - self._voff)

    def key_at(self, i: int) -> int:
        return int(self.view[i]) if self.loaded else i + self._voff

    def pos_of_key(self, key: int):
        if not self.loaded:
            return key - self._voff
        w = np.flatnonzero(self.view == key)
        return int(w[0]) if len(w) else None

    def title(self, col: int) -> str:
        h = self.headers[col] if col < len(self.headers) else ""
        return h if (self.has_header and h) else col_letter(col)

    def cell_at(self, vrow: int, col: int) -> str:
        return self.cell(self.key_at(vrow), col)

    # ======================================================================
    # Undo / redo
    # ======================================================================
    def _top_marker(self):
        return id(self.undo_stack[-1]) if self.undo_stack else 0

    def has_unsaved_changes(self) -> bool:
        return self._top_marker() != self._saved_marker

    def mark_saved(self):
        self._saved_marker = self._top_marker()

    def can_undo(self):
        return bool(self.undo_stack)

    def can_redo(self):
        return bool(self.redo_stack)

    def _push(self, cmd: Command):
        if self._group is not None:
            self._group.append(cmd)
            return
        self.mut += 1
        self.undo_stack.append(cmd)
        self.redo_stack.clear()
        self._undo_bytes += cmd.nbytes
        while self._undo_bytes > self.UNDO_BYTES and len(self.undo_stack) > 1:
            old = self.undo_stack.pop(0)
            self._undo_bytes -= old.nbytes
        self._emit("history")

    class _Group:
        def __init__(self, d, label):
            self.d, self.label = d, label

        def __enter__(self):
            self.prev = self.d._group
            self.d._group = []
            return self

        def __exit__(self, et, ev, tb):
            cmds = self.d._group
            self.d._group = self.prev
            if et is None and cmds:
                def undo():
                    for c in reversed(cmds):
                        c.undo()

                def redo():
                    for c in cmds:
                        c.redo()
                self.d._push(Command(self.label, undo, redo, sum(c.nbytes for c in cmds)))

    def group(self, label):
        return TableData._Group(self, label)

    def undo(self):
        if not self.undo_stack:
            return None
        cmd = self.undo_stack.pop()
        self._undo_bytes -= cmd.nbytes
        cmd.undo()
        self.redo_stack.append(cmd)
        self.mut += 1
        self._after_change(True)
        self._emit("history")
        return cmd.label

    def redo(self):
        if not self.redo_stack:
            return None
        cmd = self.redo_stack.pop()
        cmd.redo()
        self.undo_stack.append(cmd)
        self._undo_bytes += cmd.nbytes
        self.mut += 1
        self._after_change(True)
        self._emit("history")
        return cmd.label

    def _after_change(self, struct):
        self._colvals.clear()
        self._types = None
        self._rc.clear() if struct else None
        self._emit("struct" if struct else "cells")

    # ---- snapshots ------------------------------------------------------------------
    def _snap(self):
        return (self.order, self.view, list(self.headers), list(self.colmap), self.ncols,
                self.has_header, self.header_key, self._voff, dict(self.edits), list(self.col_w),
                self.sort_col, self.sort_asc, self.filter_on)

    def _restore(self, s):
        (self.order, self.view, hd, cm, self.ncols, self.has_header, self.header_key, self._voff,
         ed, cw, self.sort_col, self.sort_asc, self.filter_on) = s
        self.headers, self.colmap, self.edits, self.col_w = list(hd), list(cm), dict(ed), list(cw)
        self._cm_identity = self.colmap == list(range(self.ncols))
        self._rc.clear()

    def _struct_cmd(self, label, before, after):
        nb = before[0].nbytes + before[1].nbytes + after[0].nbytes + after[1].nbytes
        # arrays shared between snapshots are only held once
        if before[0] is after[0]:
            nb -= before[0].nbytes * 2
        self._push(Command(label, lambda: self._restore(before), lambda: self._restore(after), nb // 2))

    # ======================================================================
    # Cell / header edits
    # ======================================================================
    def set_cells(self, changes, label=None):
        """changes: iterable of (key, col, text).  Returns number of cells changed."""
        label = label or _("Edit Cells")
        by_key: dict[int, dict[int, str]] = {}
        for k, c, t in changes:
            if 0 <= c < self.ncols:
                by_key.setdefault(k, {})[c] = t
        olds, news = {}, {}
        count = 0
        for k, cs in by_key.items():
            cur = self.row(k)
            new = list(cur)
            for c, t in cs.items():
                if new[c] != t:
                    new[c] = t
                    count += 1
            if new != cur:
                olds[k] = self.edits.get(k)
                news[k] = new
        if not news:
            return 0
        self.edits.update(news)
        cols = {c for cs in by_key.values() for c in cs}

        def undo():
            for k, o in olds.items():
                if o is None:
                    self.edits.pop(k, None)
                else:
                    self.edits[k] = o
            self._colvals.clear()

        def redo():
            self.edits.update(news)
            self._colvals.clear()
        self._push(Command(label, undo, redo, 120 * len(news)))
        self._colvals.clear()
        self._types = None
        self._emit("cells")
        return count

    def set_header(self, col: int, text: str):
        if not self.has_header or not (0 <= col < self.ncols) or self.headers[col] == text:
            return
        old = self.headers[col]

        def undo():
            self.headers[col] = old

        def redo():
            self.headers[col] = text
        self.headers[col] = text
        self._push(Command("Rename Column", undo, redo))
        self._emit("header")

    # ---- structure ------------------------------------------------------------------
    def set_has_header(self, flag: bool):
        if flag == self.has_header or not self.loaded:
            return
        before = self._snap()
        if flag:
            if len(self.order) == 0:
                return
            k = int(self.order[0])
            hdr = list(self.row(k))
            self.headers = hdr
            self.header_key = k
            self.order = self.order[1:]
            self.view = self.view[self.view != k]
            self.has_header = True
        else:
            nk = self._next_new
            self._next_new -= 1
            self.edits[nk] = (list(self.headers) + [""] * self.ncols)[: self.ncols]
            self.order = np.insert(self.order, 0, nk)
            self.view = np.insert(self.view, 0, nk)
            self.headers = [""] * self.ncols
            self.has_header = False
            self.header_key = -1
        self._voff = 1 if self.has_header else 0
        self._after_struct(_("Toggle Header"), before)

    def _after_struct(self, label, before):
        after = self._snap()
        self._struct_cmd(label, before, after)
        self._colvals.clear()
        self._types = None
        self._rc.clear()
        self._cm_identity = self.colmap == list(range(self.ncols))
        self._emit("struct")

    def insert_rows(self, vpos: int, count: int = 1) -> list[int]:
        """Insert `count` empty rows before view position vpos (n_view appends)."""
        before = self._snap()
        keys = []
        for _i in range(count):
            k = self._next_new
            self._next_new -= 1
            self.edits[k] = [""] * self.ncols
            keys.append(k)
        vpos = max(0, min(vpos, len(self.view)))
        if vpos < len(self.view):
            anchor = int(self.view[vpos])
            w = np.flatnonzero(self.order == anchor)
            opos = int(w[0]) if len(w) else len(self.order)
        else:
            # after the last displayed row: after that row in document order
            if len(self.view):
                w = np.flatnonzero(self.order == int(self.view[-1]))
                opos = int(w[0]) + 1 if len(w) else len(self.order)
            else:
                opos = len(self.order)
        self.order = np.insert(self.order, opos, keys)
        self.view = np.insert(self.view, vpos, keys)
        self._after_struct(_("Insert Rows"), before)
        return keys

    def delete_rows(self, vpositions) -> int:
        vp = np.unique(np.asarray(list(vpositions), dtype=np.int64))
        vp = vp[(vp >= 0) & (vp < len(self.view))]
        if not len(vp):
            return 0
        before = self._snap()
        keys = self.view[vp]
        self.order = self.order[~np.isin(self.order, keys)]
        self.view = np.delete(self.view, vp)
        self._after_struct(_("Delete Rows"), before)
        return len(vp)

    def insert_cols(self, at: int, count: int = 1):
        before = self._snap()
        at = max(0, min(at, self.ncols))
        for _i in range(count):
            self.colmap.insert(at, -1)
            self.headers.insert(at, "")
            self.col_w.insert(at, None)
        self.ncols += count
        self.edits = {k: r[:at] + [""] * count + r[at:] for k, r in self.edits.items()}
        self._after_struct(_("Insert Columns"), before)

    def delete_cols(self, cols):
        cols = sorted({c for c in cols if 0 <= c < self.ncols})
        if not cols or len(cols) >= self.ncols:
            return False
        before = self._snap()
        keep = [c for c in range(self.ncols) if c not in set(cols)]
        self.colmap = [self.colmap[c] for c in keep]
        self.headers = [self.headers[c] for c in keep]
        self.col_w = [self.col_w[c] for c in keep]
        self.edits = {k: [r[c] for c in keep] for k, r in self.edits.items()}
        self.ncols = len(keep)
        if self.sort_col is not None:
            self.sort_col = None
        self._after_struct(_("Delete Columns"), before)
        return True

    def ensure_size(self, rows: int, cols: int):
        """Grow the table so (rows x cols) exist (used by paste)."""
        if cols > self.ncols:
            self.insert_cols(self.ncols, cols - self.ncols)
        if rows > len(self.view):
            self.insert_rows(len(self.view), rows - len(self.view))

    def set_order(self, new_order: np.ndarray, label=None):
        label = label or _("Sort")
        before = self._snap()
        self.order = new_order
        self.sort_col = None
        self.view = self._filtered(new_order)
        self._after_struct(label, before)

    def _filtered(self, order):
        if self.filter_on and self.search_text:
            return order[np.isin(order, self.match_keys)]
        return order

    # ======================================================================
    # Sorting / searching  (pure computations: safe on a worker thread)
    # ======================================================================
    def column_values(self, col: int) -> list[str]:
        """Base-file values of data column `col` for every base record."""
        bc = self.colmap[col] if 0 <= col < len(self.colmap) else -1
        if bc < 0:
            return [""] * self.nbase
        got = self._colvals.get(bc)
        if got is not None:
            return got
        out: list[str] = []
        step = 20_000
        for i in range(0, self.nbase, step):
            rows = self._parse_range(i, min(self.nbase, i + step))
            out.extend([r[bc] if bc < len(r) else "" for r in rows])
        if len(self._colvals) >= 2:
            self._colvals.pop(next(iter(self._colvals)))
        self._colvals[bc] = out
        return out

    def _values_for(self, keys: np.ndarray, col: int) -> list[str]:
        base = self.column_values(col)
        nb = len(base)
        ks = keys.tolist()
        vals = [base[k] if 0 <= k < nb else "" for k in ks]
        if self.edits:
            ek = set(self.edits)
            pos = {k: i for i, k in enumerate(ks) if k in ek}
            for k, i in pos.items():
                vals[i] = self.edits[k][col]
        return vals

    def sorted_keys(self, keys: np.ndarray, col: int, asc: bool, should_stop=lambda: False) -> np.ndarray:
        vals = self._values_for(keys, col)
        n = len(vals)
        cls = np.empty(n, np.int8)
        sec = np.zeros(n, np.float64)
        tidx, tkeys = [], []
        for i, s in enumerate(vals):
            if not s or s.isspace():
                cls[i] = 2
                continue
            v = to_num(s)
            if v is not None:
                cls[i] = 0
                sec[i] = v
            else:
                cls[i] = 1
                tidx.append(i)
                tkeys.append(s.casefold())
        if tkeys:
            o = sorted(range(len(tkeys)), key=tkeys.__getitem__)
            rank = np.empty(len(tkeys), np.float64)
            rank[o] = np.arange(len(tkeys))
            sec[tidx] = rank
        eff = cls.copy()
        if not asc:
            sec = -sec
            eff = np.where(cls == 2, 2, 1 - cls).astype(np.int8)
        perm = np.lexsort((sec, eff))
        return keys[perm]

    def compute_view(self, sort_col, asc, filter_on, match_keys) -> np.ndarray:
        keys = self.order
        if filter_on and match_keys is not None:
            keys = keys[np.isin(keys, match_keys)]
        if sort_col is not None and 0 <= sort_col < self.ncols and len(keys):
            keys = self.sorted_keys(keys, sort_col, asc)
        return keys

    def set_view(self, view, sort_col, asc, filter_on):
        self.view = view
        self.sort_col, self.sort_asc, self.filter_on = sort_col, asc, filter_on
        self._update_match_positions()
        self._emit("view")

    # ---- search ---------------------------------------------------------------------
    def find_matches(self, text: str) -> np.ndarray:
        """Sorted array of row keys (in `order`) having a cell containing `text`
        (case-insensitive)."""
        if not text or not self.nbase:
            return np.zeros(0, np.int64)
        variants = {text, text.lower(), text.upper(), text.title(), text.casefold()}
        pats = set()
        for v in variants:
            try:
                b = v.encode(self.enc).lower()  # ASCII-only lowercase; other bytes untouched
            except (UnicodeEncodeError, LookupError):
                continue
            if b:
                pats.add(b)
        keys = set()
        if pats:
            offs_list = []
            step = 16 << 20
            overlap = max(len(p) for p in pats) - 1
            n = len(self.data)
            for i in range(0, n, step):
                chunk = bytes(self.data[i:i + step + overlap]).lower()
                lim = min(step, n - i)
                for pat in pats:
                    j = chunk.find(pat)
                    while j != -1 and j < lim:
                        offs_list.append(i + j)
                        j = chunk.find(pat, j + 1)
            offs = np.unique(np.array(offs_list, np.int64))
            if len(offs):
                rows = np.unique(np.searchsorted(self._starts[: self.nbase + 1], offs, side="right") - 1)
                rows = rows[(rows >= 0) & (rows < self.nbase)]
                needs_verify = (not self._cm_identity) or any(
                    ch in text for ch in (self.delim, self.quote, "\n", "\r"))
                cf = text.casefold()
                ek = self.edits
                for k in rows.tolist():
                    if k in ek:
                        continue
                    if needs_verify:
                        if not any(cf in c.casefold() for c in self.row(k)):
                            continue
                    keys.add(k)
        cf = text.casefold()
        for k, r in self.edits.items():
            if any(cf in c.casefold() for c in r):
                keys.add(k)
        if not keys:
            return np.zeros(0, np.int64)
        arr = np.fromiter(keys, np.int64, len(keys))
        arr.sort()
        return arr[np.isin(arr, self.order)]

    def set_matches(self, text, keys):
        self.search_text = text
        self.match_keys = keys
        self._update_match_positions()

    def _update_match_positions(self):
        if self.search_text and len(self.match_keys) and self.loaded:
            self.match_vpos = np.flatnonzero(np.isin(self.view, self.match_keys))
        else:
            self.match_vpos = np.zeros(0, np.int64)

    # ======================================================================
    # Statistics
    # ======================================================================
    def _type_sample(self):
        if getattr(self, "_tsample_mut", None) != (self.mut, id(self.order)):
            n = len(self.order)
            if n:
                idx = set(range(min(n, 60)))
                if n > 60:
                    idx.update(np.linspace(0, n - 1, 60).astype(int).tolist())
                keys = [int(self.order[i]) for i in sorted(idx)]
            else:
                keys = []
            self._tsample = [self.row(k) for k in keys]
            self._tsample_mut = (self.mut, id(self.order))
            self._types = {}
        return self._tsample

    def col_type(self, c: int) -> str:
        """'number' | 'date' | 'text' | 'empty' for column c (sampled, cached)."""
        rows = self._type_sample()
        if not isinstance(self._types, dict):
            self._types = {}
        t = self._types.get(c)
        if t is None:
            t = self._types[c] = infer_type([r[c] for r in rows if c < len(r)])
        return t

    def column_types(self) -> list[str]:
        return [self.col_type(c) for c in range(self.ncols)]

    def empty_counts(self, should_stop=lambda: False) -> list[int] | None:
        """Exact empty-cell count per column (full scan of the base rows)."""
        cnt = [0] * self.ncols
        edited = set(self.edits)
        n = len(self.order)
        if n == 0:
            return cnt
        # base rows that are untouched and in `order`
        step = 20_000
        order = self.order
        for i in range(0, n, step):
            if should_stop():
                return None
            blk = order[i:i + step]
            k0 = int(blk[0])
            contiguous = bool(np.all(np.diff(blk) == 1)) and k0 >= 0
            if contiguous and not edited:
                rows = self._parse_range(k0, k0 + len(blk))
                rows = [self._map_cols(r) for r in rows]
            else:
                rows = [self.row(int(k)) for k in blk]
            for c, col in enumerate(zip(*rows)):
                cnt[c] += col.count("")
        return cnt

    # ======================================================================
    # Saving / exporting
    # ======================================================================
    def _row_batches(self, progress=None, batch=20_000):
        """Yield lists of rows (each len == ncols) in document order."""
        order = self.order
        n = len(order)
        ek = np.fromiter(self.edits.keys(), np.int64, len(self.edits)) if self.edits else np.zeros(0, np.int64)
        for i in range(0, n, batch):
            blk = order[i:i + batch]
            special = (blk < 0)
            if len(ek):
                special |= np.isin(blk, ek)
            brk = np.flatnonzero((np.diff(blk) != 1) | special[:-1] | special[1:]) + 1
            bounds = [0] + brk.tolist() + [len(blk)]
            out = []
            for a, b in zip(bounds, bounds[1:]):
                k0 = int(blk[a])
                if special[a]:
                    out.append(list(self.row(k0)))
                else:
                    rows = self._parse_range(k0, k0 + (b - a))
                    out.extend(self._map_cols(r) if not self._cm_identity or len(r) != self.ncols else r
                               for r in rows)
            yield out
            if progress:
                progress(min(1.0, (i + batch) / max(1, n)))

    def _text_encoder(self, extra_text_iter=None):
        """Pick the output encoding; falls back to UTF-8 (BOM for legacy) when edited
        text cannot be represented.  Returns (encode_fn, note or None)."""
        enc = self.out_enc
        note = None
        probe = [self.headers] + [r for r in self.edits.values()]
        ok = True
        for r in probe:
            for c in r:
                if c and not c.isascii() and not _can_encode(c, enc):
                    ok = False
                    break
            if not ok:
                break
        bom = self.bom
        if not ok:
            note = f"Saved as UTF-8 because the text cannot be stored in {self.enc_label}"
            enc, bom = "utf-8", (codecs.BOM_UTF8 if self.enc not in ("utf-8",) else self.bom)
        return enc, bom, note

    def write_delimited(self, f, delim=None, quoting=None, eol=None, progress=None) -> str | None:
        delim = delim or self.delim
        quoting = quoting or self.quoting
        eol_b = eol or self.out_eol
        enc, bom, note = self._text_encoder()
        transcode = enc != self.enc
        raw_ok = (self._cm_identity and self.ncols == self.base_ncols and delim == self.delim
                  and quoting == self.quoting and eol_b == self.eol and not transcode)
        eol_t = eol_b.decode("ascii")

        def enc_text(t):
            return t.encode(enc, "replace")

        buf = io.StringIO()
        w = csv.writer(buf, delimiter=delim, quotechar=self.quote,
                       quoting=csv.QUOTE_ALL if quoting == "all" else csv.QUOTE_MINIMAL,
                       lineterminator=eol_t)

        def ser(rows):
            buf.seek(0)
            buf.truncate()
            w.writerows(rows)
            return enc_text(buf.getvalue())

        if bom:
            f.write(bom)
        if self.has_header:
            if (raw_ok and self.header_key == 0 and self.headers == self._orig_headers):
                f.write(self._raw_segment(0, 1))
            else:
                f.write(ser([self.headers]))
        order = self.order
        n = len(order)
        if raw_ok:
            ek = np.fromiter(self.edits.keys(), np.int64, len(self.edits)) if self.edits else np.zeros(0, np.int64)
            blkn = 200_000
            for i in range(0, n, blkn):
                blk = order[i:i + blkn]
                special = (blk < 0)
                if len(ek):
                    special |= np.isin(blk, ek)
                brk = np.flatnonzero((np.diff(blk) != 1) | special[:-1] | special[1:]) + 1
                bounds = [0] + brk.tolist() + [len(blk)]
                for a, b in zip(bounds, bounds[1:]):
                    k0 = int(blk[a])
                    if special[a]:
                        f.write(ser([self.row(k0)]))
                    else:
                        f.write(self._raw_segment(k0, k0 + (b - a)))
                if progress:
                    progress(min(1.0, (i + blkn) / max(1, n)))
        else:
            for rows in self._row_batches(progress):
                f.write(ser(rows))
        if not self.ends_with_newline:
            f.flush()
            tail = eol_b.decode("ascii").encode(enc)
            size = f.tell()
            if size >= len(tail):
                f.seek(size - len(tail))
                if f.read(len(tail)) == tail:
                    f.seek(size - len(tail))
                    f.truncate()
                else:
                    f.seek(size)
        return note

    def _raw_segment(self, k0, k1) -> bytes:
        seg = bytes(self.data[int(self._starts[k0]): int(self._starts[k1])])
        if seg and not (seg.endswith(b"\n") or (self._scan_nl == 13 and seg.endswith(b"\r"))):
            seg += self.eol
        return seg

    def _typed_cell(self, col, s, types):
        if not s:
            return None
        if types[col] == "number":
            v = to_num(s)
            if v is not None and not (len(s) > 1 and s.lstrip("+-").startswith("0") and "." not in s
                                      and not s.lstrip("+-").startswith("0.") and len(s.lstrip("+-")) > 1):
                if v == int(v) and abs(v) < 1e15 and "." not in s and "e" not in s.lower():
                    return int(v)
                return v
        return s

    def write_json(self, f, progress=None):
        types = self.column_types()
        names = []
        seen = {}
        for c in range(self.ncols):
            nm = self.headers[c] if self.has_header and self.headers[c] else f"col{c + 1}"
            if nm in seen:
                seen[nm] += 1
                nm = f"{nm}_{seen[nm]}"
            else:
                seen[nm] = 1
            names.append(nm)
        f.write(b"[\n")
        first = True
        for rows in self._row_batches(progress):
            parts = []
            for r in rows:
                if self.has_header:
                    obj = {names[c]: self._typed_cell(c, r[c], types) for c in range(self.ncols)}
                else:
                    obj = [self._typed_cell(c, r[c], types) for c in range(self.ncols)]
                parts.append("  " + json.dumps(obj, ensure_ascii=False))
            if parts:
                f.write((("" if first else ",\n") + ",\n".join(parts)).encode("utf-8"))
                first = False
        f.write(b"\n]\n")

    def write_xlsx(self, path, progress=None):
        import openpyxl
        if len(self.order) + 1 > 1_048_576:
            raise TableError("Too many rows for an Excel sheet (limit 1,048,576).")
        if self.ncols > 16384:
            raise TableError("Too many columns for an Excel sheet (limit 16,384).")
        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet("Sheet1")
        types = self.column_types()
        if self.has_header:
            ws.append(list(self.headers))
        illegal = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
        for rows in self._row_batches(progress):
            for r in rows:
                ws.append([(lambda v: illegal.sub("", v) if isinstance(v, str) else v)(self._typed_cell(c, r[c], types))
                           for c in range(self.ncols)])
        wb.save(path)

    def save_to(self, path, fmt="csv", delim=None, progress=None) -> str | None:
        """Atomically write the document.  fmt: csv | tsv | json | xlsx."""
        d = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=os.path.splitext(path)[1], dir=d)
        note = None
        try:
            if fmt == "xlsx":
                os.close(fd)
                self.write_xlsx(tmp, progress)
            else:
                with os.fdopen(fd, "w+b", buffering=1 << 20) as f:
                    if fmt == "json":
                        self.write_json(f, progress)
                    else:
                        dl = delim or ("\t" if fmt == "tsv" else self.delim if fmt == "csv" else ",")
                        note = self.write_delimited(f, delim=dl, progress=progress)
                    f.flush()
                    os.fsync(f.fileno())
            try:
                st = os.stat(path)
                os.chmod(tmp, st.st_mode & 0o7777)
            except OSError:
                os.chmod(tmp, 0o644)
            replace_file(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return note

    def adopt_saved_dialect(self, path, delim):
        """After saving the same document as CSV/TSV to a new path/dialect the in-memory
        base still describes the old bytes; only the *output* settings change."""
        self.path = path
        self.mark_saved()


def _enc_label(enc: str) -> str:
    e = enc.lower().replace("_", "-")
    return {"utf-8": "UTF-8", "cp1252": "Windows-1252", "latin-1": "Latin-1", "gb18030": "GB18030",
            "big5": "Big5", "shift-jis": "Shift-JIS", "euc-kr": "EUC-KR", "euc-jp": "EUC-JP",
            "utf-16": "UTF-16"}.get(e, enc)
