"""Text recognition ("Live Text") for View.

Engine: RapidOCR (PP-OCR ONNX models on onnxruntime, CPU only). The models ship
inside the ``rapidocr`` wheel (~31 MB); if they are ever missing they are
downloaded once into the user cache dir (view/ocr). Without the
wheels installed, preview.ocr_install offers a per-user download. The engine is a lazily created
process-wide singleton.

On Windows the engine runs in a helper process (view-ocr.exe, see
preview.ocr_helper): onnxruntime only ships for python.org's Python, while
the app runs on MSYS2's Python (where GTK comes from), and one Python cannot
load the other's binary modules.  ``VIEW_OCR_HELPER`` names a helper
executable explicitly (any platform; used by tests).

Public API (kept stable, other viewers integrate it):

    OcrLine(box, text, confidence)       box = 4 (x, y) corners in image pixels,
                                         clockwise from top-left
    is_available() -> bool
    recognize(image) -> list[OcrLine]    PIL.Image | path | bytes; blocking
    recognize_async(image, callback, cancellable=None)
                                         callback(lines, error) on the GTK main loop
    lines_to_text(lines) -> str          reading order
    TextOverlay                          selectable-text layer for any host widget

Paths / bytes are decoded with EXIF orientation applied (what a viewer shows);
a PIL image is used exactly as given.

CLI:  python -m preview.ocr image.png [...]
"""
from __future__ import annotations

import io
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

def _cache_dir():
    try:
        from gi.repository import GLib
        base = GLib.get_user_cache_dir()
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "view", "ocr")


CACHE_DIR = _cache_dir()

# A user-level engine installed by View's "Install text recognition" offer.
try:
    from .ocr_install import activate as _activate_private_engine
    _activate_private_engine()
except Exception:  # noqa: BLE001
    pass

_engine = None
_engine_lock = threading.Lock()
_run_lock = threading.Lock()  # onnxruntime sessions are fine concurrently, but one at a time keeps latency predictable
_unavailable_reason: str | None = None
_LOCAL_ONLY = False  # set inside the helper process itself


@dataclass
class OcrLine:
    box: list  # [(x, y)] * 4, clockwise from top-left, source-image pixels
    text: str
    confidence: float


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def is_available() -> bool:
    """True if the engine can be used (does not load models): rapidocr +
    onnxruntime importable here, or a helper executable is present."""
    global _unavailable_reason
    if helper_path():
        return True
    try:
        import importlib.util
        for mod in ("rapidocr", "onnxruntime", "numpy", "PIL"):
            if importlib.util.find_spec(mod) is None:
                _unavailable_reason = f"missing Python module '{mod}'"
                return False
        return True
    except Exception as e:  # noqa: BLE001
        _unavailable_reason = str(e)
        return False


def helper_path() -> str | None:
    """The out-of-process engine (view-ocr.exe), if this build has one."""
    if _LOCAL_ONLY:
        return None
    env = os.environ.get("VIEW_OCR_HELPER")
    if env:
        return env if os.path.isfile(env) else None
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        p = os.path.join(os.path.dirname(sys.executable), "ocr", "view-ocr.exe")
        if os.path.isfile(p):
            return p
    return None


class _Helper:
    """Client for preview.ocr_helper over the child's stdin/stdout.

    Request:  one JSON line {"w", "h"} followed by w*h*3 bytes of RGB.
    Response: one JSON line {"lines": [[box, text, confidence], ...]}
              or {"error": "..."}.
    The child starts on first use and stays up; it is restarted once if it
    dies.  Calls are serialized by the caller (_run_lock)."""

    def __init__(self, exe):
        self.exe = exe
        self.proc = None

    def _start(self):
        flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
        self.proc = subprocess.Popen([self.exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
        hello = self._read()
        if not hello.get("ready"):
            raise RuntimeError(f"OCR helper did not start: {hello.get('error', hello)}")

    def _read(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("OCR helper exited")
        return json.loads(line)

    def _call(self, header, payload=b""):
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        self.proc.stdin.write(json.dumps(header).encode() + b"\n" + payload)
        self.proc.stdin.flush()
        return self._read()

    def request(self, header, payload=b""):
        try:
            res = self._call(header, payload)
        except (OSError, ValueError, RuntimeError):
            self.close()
            res = self._call(header, payload)        # one restart
        if "error" in res:
            raise RuntimeError(res["error"])
        return res

    def close(self):
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(2)
            except Exception:  # noqa: BLE001
                pass


def _bundled_models_present() -> bool:
    try:
        import importlib.util
        spec = importlib.util.find_spec("rapidocr")
        if spec is None or not spec.submodule_search_locations:
            return False
        d = os.path.join(list(spec.submodule_search_locations)[0], "models")
        return os.path.isdir(d) and any(f.endswith(".onnx") for f in os.listdir(d))
    except Exception:  # noqa: BLE001
        return False


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        exe = helper_path()
        if exe:
            helper = _Helper(exe)
            with _run_lock:
                helper.request({"cmd": "load"})
            _engine = helper
            return _engine
        if not is_available():
            raise RuntimeError("Text recognition is unavailable: "
                               + (_unavailable_reason or "rapidocr is not installed"))
        from rapidocr import RapidOCR

        threads = max(2, min(8, (os.cpu_count() or 4) // 2 or 2))
        params = {
            "Global.log_level": "warning",
            "Global.max_side_len": 4000,
            "Det.limit_side_len": 1600,
            "Det.limit_type": "max",
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
        if not _bundled_models_present():
            os.makedirs(CACHE_DIR, exist_ok=True)
            params["Global.model_root_dir"] = CACHE_DIR
        _engine = RapidOCR(params=params)
        return _engine


def warm_up_async():
    """Create the engine in the background so the first real call is fast."""
    def run():
        try:
            _get_engine()
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=run, daemon=True, name="ocr-warmup").start()


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------
def _to_rgb_image(image):
    from PIL import Image, ImageOps

    if isinstance(image, (str, os.PathLike)):
        im = Image.open(image)
        im.load()
        im = ImageOps.exif_transpose(im)
    elif isinstance(image, (bytes, bytearray, memoryview)):
        im = Image.open(io.BytesIO(bytes(image)))
        im.load()
        im = ImageOps.exif_transpose(im)
    elif isinstance(image, Image.Image):
        im = image
    else:
        raise TypeError(f"unsupported image type: {type(image).__name__}")
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(rgba)
        im = bg.convert("RGB")
    elif im.mode != "RGB":
        im = im.convert("RGB")
    return im


def _rgb_to_bgr_array(im):
    import numpy as np
    arr = np.asarray(im)
    return np.ascontiguousarray(arr[:, :, ::-1])  # RGB -> BGR


def recognize_raw(arr):
    """Run the in-process engine on a BGR array: [(box, text, confidence)]
    with raw corner order (also what the helper process sends back)."""
    engine = _get_engine()
    with _run_lock:
        res = engine(arr)
    boxes = getattr(res, "boxes", None)
    txts = getattr(res, "txts", None)
    scores = getattr(res, "scores", None)
    if boxes is None or txts is None:
        return []
    return [([(float(p[0]), float(p[1])) for p in b], t or "", float(s))
            for b, t, s in zip(boxes, txts, scores)]


def recognize(image) -> list[OcrLine]:
    """Run OCR on a PIL image / path / bytes. Blocking (1-3 s on a CPU)."""
    im = _to_rgb_image(image)
    w, h = im.size
    if h < 8 or w < 8:
        return []
    engine = _get_engine()
    if isinstance(engine, _Helper):
        with _run_lock:
            raw = engine.request({"w": w, "h": h}, im.tobytes())["lines"]
    else:
        raw = recognize_raw(_rgb_to_bgr_array(im))
    out = []
    for b, t, s in raw:
        t = (t or "").strip()
        if not t:
            continue
        pts = [(float(p[0]), float(p[1])) for p in b]
        out.append(OcrLine(_order_corners(pts), t, float(s)))
    return out


def _order_corners(pts):
    """Ensure clockwise from top-left (image coords, y down)."""
    if len(pts) != 4:
        return pts
    cx = sum(p[0] for p in pts) / 4
    cy = sum(p[1] for p in pts) / 4
    pts = sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))  # clockwise on screen
    # start at the corner nearest the top-left (min x + y)
    i = min(range(4), key=lambda k: pts[k][0] + pts[k][1])
    return pts[i:] + pts[:i]


def recognize_async(image, callback, cancellable=None):
    """Run recognize() in a worker thread; callback(lines, error) runs on the GTK
    main loop (lines is [] and error an Exception on failure). Skipped when
    `cancellable` (Gio.Cancellable) is cancelled."""
    from gi.repository import GLib

    # Snapshot a PIL image now so the caller may keep editing it.
    try:
        from PIL import Image
        if isinstance(image, Image.Image):
            image = image.copy()
    except Exception:  # noqa: BLE001
        pass

    def finish(lines, err):
        if cancellable is not None and cancellable.is_cancelled():
            return False
        callback(lines, err)
        return False

    def work():
        if cancellable is not None and cancellable.is_cancelled():
            return
        try:
            lines, err = recognize(image), None
        except Exception as e:  # noqa: BLE001
            lines, err = [], e
        GLib.idle_add(finish, lines, err)

    threading.Thread(target=work, daemon=True, name="ocr").start()


# ---------------------------------------------------------------------------
# Reading order
# ---------------------------------------------------------------------------
def _is_wide(ch: str) -> bool:
    o = ord(ch)
    return (0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
            or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6
            or 0x20000 <= o <= 0x3FFFF)


def _geom(line: OcrLine, rot=None):
    b = line.box
    if rot is not None:
        c, sn = rot
        b = [(x * c - y * sn, x * sn + y * c) for x, y in b]
    xs = [p[0] for p in b]
    ys = [p[1] for p in b]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    # height measured across the text direction (left edge length), width along it
    ex, ey = b[1][0] - b[0][0], b[1][1] - b[0][1]
    fx, fy = b[3][0] - b[0][0], b[3][1] - b[0][1]
    along = math.hypot(ex, ey)
    across = math.hypot(fx, fy)
    return x0, y0, x1, y1, max(1.0, min(along, across) if along > 0 and across > 0 else max(along, across))


def _reading_order(lines):
    """Return [(index, separator_before)] for the lines in reading order.
    Separator is '' (first), ' ' (same row), '\\n' (next line) or '\\n\\n'
    (paragraph break)."""
    n = len(lines)
    if n == 0:
        return []
    # De-skew: rotate by the median text angle so slanted photos/scans keep their rows.
    angs = []
    for l in lines:
        b = l.box
        dx, dy = b[1][0] - b[0][0], b[1][1] - b[0][1]
        if math.hypot(dx, dy) > 1.5 * math.hypot(b[3][0] - b[0][0], b[3][1] - b[0][1]):
            a = math.atan2(dy, dx)
            if abs(a) < math.pi / 4:
                angs.append(a)
    rot = None
    if angs:
        angs.sort()
        med = angs[len(angs) // 2]
        if abs(med) > math.radians(1.0):
            rot = (math.cos(-med), math.sin(-med))
    g = [_geom(l, rot) for l in lines]
    order = sorted(range(n), key=lambda i: ((g[i][1] + g[i][3]) / 2, g[i][0]))
    rows: list[list[int]] = []
    row_bounds: list[list[float]] = []  # [top, bottom]
    for i in order:
        x0, y0, x1, y1, h = g[i]
        placed = False
        if rows:
            top, bot = row_bounds[-1]
            overlap = min(bot, y1) - max(top, y0)
            if overlap > 0.5 * min(bot - top, y1 - y0):
                rows[-1].append(i)
                row_bounds[-1] = [min(top, y0), max(bot, y1)]
                placed = True
        if not placed:
            rows.append([i])
            row_bounds.append([y0, y1])
    result = []
    prev_bot = None
    for r, row in enumerate(rows):
        row.sort(key=lambda i: g[i][0])
        top, bot = row_bounds[r]
        rh = sum(g[i][4] for i in row) / len(row)
        for k, i in enumerate(row):
            if r == 0 and k == 0:
                sep = ""
            elif k > 0:
                sep = " "
            else:
                gap = top - prev_bot
                sep = "\n\n" if gap > 0.7 * rh else "\n"
            result.append((i, sep))
        prev_bot = bot
    return result


def _join(a: str, b: str, sep: str) -> str:
    if sep == " " and a and b and _is_wide(a[-1]) and _is_wide(b[0]):
        sep = ""
    return a + sep + b


def lines_to_text(lines) -> str:
    """Plain text in reading order: rows by vertical overlap, left to right
    within a row, blank line between paragraphs."""
    out = ""
    for i, sep in _reading_order(list(lines)):
        out = _join(out, lines[i].text, sep) if out else lines[i].text
    return out


# ---------------------------------------------------------------------------
# Selectable text overlay
# ---------------------------------------------------------------------------
@dataclass
class _Item:
    line: OcrLine
    sep: str
    cum: list = field(default_factory=list)  # cumulative width fraction at each char boundary (len = n+1)


def _char_cum(text: str):
    ws = [1.0 if _is_wide(c) else (0.35 if c in " .,;:!'|il" else 0.6) for c in text]
    total = sum(ws) or 1.0
    cum = [0.0]
    for w in ws:
        cum.append(cum[-1] + w / total)
    return cum


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _in_poly(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _seg_dist2(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    t = 0.0 if d2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / d2))
    qx, qy = ax + t * dx, ay + t * dy
    return (px - qx) ** 2 + (py - qy) ** 2


class TextOverlay:
    """Host-agnostic selectable-text layer over recognized lines.

    All coordinates are image pixels except draw(), which maps image pixels to
    widget space as  widget = image * scale + offset. Set `on_change` to a
    zero-arg callable to be told when the selection / hover state changes
    (typically widget.queue_draw)."""

    SELECTION_RGBA = (0.20, 0.52, 0.98, 0.35)
    BOX_RGBA = (1.0, 0.85, 0.2, 0.55)

    def __init__(self, lines, image_size):
        self.image_size = tuple(image_size)
        self.lines = list(lines)
        self.on_change = None
        self.show_boxes = False  # faint outlines (e.g. while hovering the image)
        self._items = [_Item(self.lines[i], sep) for i, sep in _reading_order(self.lines)]
        for it in self._items:
            it.cum = _char_cum(it.line.text)
        self._anchor = None  # (item_index, char_index)
        self._focus = None
        self._dragging = False

    # -- state helpers ------------------------------------------------------
    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass

    def set_show_boxes(self, on: bool):
        if on != self.show_boxes:
            self.show_boxes = on
            self._changed()

    def has_text(self) -> bool:
        return bool(self._items)

    def has_selection(self) -> bool:
        return self._anchor is not None and self._focus is not None and self._anchor != self._focus

    def is_selecting(self) -> bool:
        return self._dragging

    # -- hit testing -----------------------------------------------------------
    def hit(self, x, y, margin: float = 2.0) -> bool:
        """True if (x, y) is over recognized text."""
        for it in self._items:
            b = it.line.box
            if _in_poly(x, y, b):
                return True
            if margin > 0 and any(_seg_dist2(x, y, b[k], b[(k + 1) % 4]) <= margin * margin
                                  for k in range(4)):
                return True
        return False

    def _locate(self, x, y):
        """(item index, char index) nearest to the point."""
        if not self._items:
            return None
        best, best_d = 0, float("inf")
        for idx, it in enumerate(self._items):
            b = it.line.box
            if _in_poly(x, y, b):
                best, best_d = idx, -1.0
                break
            d = min(_seg_dist2(x, y, b[k], b[(k + 1) % 4]) for k in range(4))
            if d < best_d:
                best, best_d = idx, d
        it = self._items[best]
        b = it.line.box
        ax, ay = b[1][0] - b[0][0], b[1][1] - b[0][1]
        d2 = ax * ax + ay * ay
        t = 0.0 if d2 == 0 else ((x - b[0][0]) * ax + (y - b[0][1]) * ay) / d2
        t = max(0.0, min(1.0, t))
        cum = it.cum
        # nearest char boundary
        ci = min(range(len(cum)), key=lambda k: abs(cum[k] - t))
        return best, ci

    # -- selection ------------------------------------------------------------------
    def begin_select(self, x, y):
        self._anchor = self._focus = self._locate(x, y)
        self._dragging = True
        self._changed()

    def update_select(self, x, y):
        if self._anchor is None:
            return
        f = self._locate(x, y)
        if f != self._focus:
            self._focus = f
            self._changed()

    def end_select(self):
        self._dragging = False
        if not self.has_selection():
            self._anchor = self._focus = None
        self._changed()

    def clear(self):
        self._anchor = self._focus = None
        self._dragging = False
        self._changed()

    def select_all(self):
        if not self._items:
            return
        self._anchor = (0, 0)
        last = len(self._items) - 1
        self._focus = (last, len(self._items[last].line.text))
        self._changed()

    def select_line_at(self, x, y):
        loc = self._locate(x, y)
        if loc is None:
            return
        i = loc[0]
        self._anchor, self._focus = (i, 0), (i, len(self._items[i].line.text))
        self._changed()

    def select_word_at(self, x, y):
        loc = self._locate(x, y)
        if loc is None:
            return
        i, c = loc
        s = self._items[i].line.text
        c = min(c, max(0, len(s) - 1))
        if not s:
            return

        def cls(ch):
            return "w" if (ch.isalnum() or ch == "_") and not _is_wide(ch) else ("c" if _is_wide(ch) else "s")
        k = cls(s[c])
        a = c
        while a > 0 and cls(s[a - 1]) == k and k != "c":
            a -= 1
        b = c + 1
        while b < len(s) and cls(s[b]) == k and k != "c":
            b += 1
        self._anchor, self._focus = (i, a), (i, b)
        self._changed()

    def _range(self):
        if not self.has_selection():
            return None
        a, b = self._anchor, self._focus
        return (a, b) if a <= b else (b, a)

    def selected_text(self) -> str:
        r = self._range()
        if r is None:
            return ""
        (i0, c0), (i1, c1) = r
        out = ""
        for i in range(i0, i1 + 1):
            it = self._items[i]
            s = it.line.text
            a = c0 if i == i0 else 0
            b = c1 if i == i1 else len(s)
            piece = s[a:b]
            if not piece:
                continue
            out = _join(out, piece, it.sep) if out else piece
        return out

    # -- drawing --------------------------------------------------------------------
    def _slice_quad(self, it: _Item, a: int, b: int):
        bx = it.line.box
        t0, t1 = it.cum[a], it.cum[b]
        tl, tr, br, bl = bx
        return [_lerp(tl, tr, t0), _lerp(tl, tr, t1), _lerp(bl, br, t1), _lerp(bl, br, t0)]

    def draw(self, cr, scale=1.0, offset_x=0.0, offset_y=0.0):
        """Draw onto a cairo context; image px -> widget px = p*scale + offset."""
        if not self._items:
            return
        cr.save()
        cr.translate(offset_x, offset_y)
        cr.scale(scale, scale)
        lw = 1.0 / max(scale, 1e-6)
        if self.show_boxes:
            cr.set_line_width(lw * 1.2)
            cr.set_source_rgba(*self.BOX_RGBA)
            for it in self._items:
                b = it.line.box
                cr.move_to(*b[0])
                for p in b[1:]:
                    cr.line_to(*p)
                cr.close_path()
                cr.stroke()
        r = self._range()
        if r is not None:
            (i0, c0), (i1, c1) = r
            cr.set_source_rgba(*self.SELECTION_RGBA)
            for i in range(i0, i1 + 1):
                it = self._items[i]
                n = len(it.line.text)
                a = c0 if i == i0 else 0
                b = c1 if i == i1 else n
                if b <= a:
                    continue
                q = self._slice_quad(it, a, b)
                cr.move_to(*q[0])
                for p in q[1:]:
                    cr.line_to(*p)
                cr.close_path()
                cr.fill()
        cr.restore()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv):
    if len(argv) < 2:
        print("usage: python -m preview.ocr image [image ...]")
        return 2
    t0 = time.perf_counter()
    _get_engine()
    print(f"[engine load: {time.perf_counter() - t0:.2f} s]")
    for path in argv[1:]:
        t = time.perf_counter()
        lines = recognize(path)
        dt = time.perf_counter() - t
        print(f"--- {path}: {len(lines)} lines, {dt:.2f} s")
        print(lines_to_text(lines))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv))
