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

    OcrLine(box, text, confidence[, cuts])
                                         box = 4 (x, y) corners in image pixels,
                                         clockwise from top-left; cuts = where
                                         each character boundary falls (or None)
    is_available() -> bool
    recognize(image) -> list[OcrLine]    PIL.Image | path | bytes; blocking
    recognize_async(image, callback, cancellable=None)
                                         callback(lines, error) on the GTK main loop
    lines_to_text(lines) -> str          reading order
    TextOverlay                          selectable-text layer for any host widget
    char_bounds(text, cuts) -> [float]   character boundaries along a line (0..1)
    nearest_bound(bounds, t) -> int      the boundary nearest a position on it
    release()                            stop running work, free the engine

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
_gen = 0                      # +1 on release(): recognitions running then give up
_starting = None              # the _Helper that _get_engine() is loading (release() ends it)
_tls = threading.local()      # the _gen a recognition started under
_LOCAL_ONLY = False  # set inside the helper process itself


@dataclass
class OcrLine:
    box: list  # [(x, y)] * 4, clockwise from top-left, source-image pixels
    text: str
    confidence: float
    # where each character boundary falls: len(text)+1 fractions along the
    # box's midline (0 at the box[0]-box[3] edge, 1 at box[1]-box[2]);
    # cuts[0] is before the first character, cuts[-1] after the last.  From
    # where the recognizer read each character, put in the blank between
    # glyphs.  None when unknown (char_bounds() estimates them then).
    cuts: list | None = None


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
    Response: one JSON line {"lines": [[box, text, confidence, cuts], ...]}
              or {"error": "..."}  (cuts: see recognize_raw; a helper from
              before they existed sends three items per line).
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
        gen = _gen
        try:
            res = self._call(header, payload)
        except (OSError, ValueError, RuntimeError):
            self.close()
            if gen != _gen:
                raise RuntimeError("Cancelled") from None    # release() stopped it
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
    global _engine, _starting
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        gen = _gen
        exe = helper_path()
        if exe:
            helper = _starting = _Helper(exe)
            try:
                with _run_lock:
                    helper.request({"cmd": "load"})
            finally:
                _starting = None
            if gen != _gen:           # release() came while it loaded: don't keep it
                helper.close()
                raise RuntimeError("Cancelled")
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
        engine = RapidOCR(params=params)
        _make_abortable()
        _track_characters(engine)
        if gen != _gen:               # release() came while it loaded: not kept
            return engine
        _engine = engine
        return _engine


class _Released(RuntimeError):
    pass


def _make_abortable():
    """Let release() stop a recognition that is running in this process: one
    image can take RapidOCR many model calls (and 15-30 s of CPU); before each
    one, give up if release() came since the recognition started."""
    try:
        from rapidocr.inference_engine.onnxruntime import main as ort
        cls = ort.OrtInferSession
        if getattr(cls, "_view_abortable", False):
            return
        run = cls.__call__

        def call(self, *a, **kw):
            gen = getattr(_tls, "gen", None)
            if gen is not None and gen != _gen:
                raise _Released("Cancelled")
            return run(self, *a, **kw)
        cls.__call__ = call
        cls._view_abortable = True
    except Exception:  # noqa: BLE001
        pass            # another rapidocr layout: running work just finishes


def _track_characters(engine):
    """Have RapidOCR report where each character of a line sits (OcrLine.cuts).

    Recognition is CTC: the model reads the line crop in narrow columns and
    emits each character at one of them.  Asked for word boxes, RapidOCR
    keeps those columns (WordInfo) and turns them into rough boxes; here that
    last step is replaced by _line_cuts, which places every character
    boundary in the blank between the glyphs around it.  The text-direction
    classifier may have turned a crop upside down before recognition: such
    crops are noted so their columns can be read from the other end.
    Another rapidocr layout: nothing is hooked and lines carry no cuts."""
    try:
        import inspect
        cls_and_rotate = engine.cls_and_rotate
        calc_word_boxes = engine.calc_word_boxes
        if "return_word_box" not in inspect.signature(engine.__call__).parameters:
            return
        flipped = {}

        def classify(imgs, *a, **kw):
            before = list(imgs)
            out, res = cls_and_rotate(imgs, *a, **kw)    # (copies every crop)
            flipped.clear()
            try:
                import numpy as np
                for o, b in zip(out, before):
                    if not (o.shape == b.shape and np.array_equal(o, b)):
                        flipped[id(o)] = (o, True if np.array_equal(o, b[::-1, ::-1]) else None)
            except Exception:  # noqa: BLE001
                flipped[None] = True                     # (unknown: no cuts this time)
            return out, res

        def word_boxes(*args, **kw):
            try:
                _crops, boxes, rec_res = args
                out = []
                for k, box in enumerate(boxes):
                    cuts = None
                    try:
                        img = rec_res.imgs[k]
                        turned = flipped[id(img)][1] if id(img) in flipped else False
                        if (None not in flipped and turned is not None
                                and not _is_vertical_crop(box)):
                            xs = _line_cuts(rec_res.txts[k], rec_res.word_results[k], img)
                            if xs is not None:
                                cuts = _crop_to_box(xs, img.shape[1], box, turned)
                    except Exception:  # noqa: BLE001
                        cuts = None
                    out.append((cuts,))       # (non-empty: RapidOCR keeps it per line)
                return tuple(out)
            except Exception:  # noqa: BLE001  (another layout: its own boxes, no cuts)
                return calc_word_boxes(*args, **kw)
            finally:
                flipped.clear()

        engine.cls_and_rotate = classify
        engine.calc_word_boxes = word_boxes
        engine._view_cuts = True
    except Exception:  # noqa: BLE001
        pass


def _is_vertical_crop(box):
    """RapidOCR turns tall crops a quarter turn (get_rotate_crop_image):
    their columns run down the box, not along box[0] -> box[1]."""
    def d(a, b):
        return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))
    w = int(max(d(box[0], box[1]), d(box[2], box[3])))
    h = int(max(d(box[0], box[3]), d(box[1], box[2])))
    return w <= 0 or h / w >= 1.5


def _line_cuts(text, info, img):
    """Character boundaries of a recognized line crop, as x positions in it
    (pixels): len(text)+1 values, or None if they can't be placed.

    `info` is RapidOCR's WordInfo: the CTC column at which each non-space
    character was read (word_cols) and how many columns the line spans
    (line_txt_len).  Those readings say roughly where each character is;
    the boundaries themselves go in the blanks between glyphs, matched to
    the readings by _place_gaps (in the middle of a blank, or split evenly
    among the spaces it holds).  The first and last boundaries hug the ink,
    not the detector's padded box."""
    import numpy as np

    cols = [c for wc in (getattr(info, "word_cols", None) or []) for c in wc]
    chars = [c for w in (getattr(info, "words", None) or []) for c in w]
    span = float(getattr(info, "line_txt_len", 0) or 0)
    idx = [i for i, ch in enumerate(text) if not ch.isspace()]
    if (not idx or span <= 0 or len(cols) != len(idx)
            or chars != [text[i] for i in idx]
            or any(b <= a for a, b in zip(cols, cols[1:]))):
        return None
    a = np.asarray(img)
    if a.ndim == 3:
        a = a.mean(axis=2)
    a = a.astype(np.float32)
    H, W = a.shape[:2]
    if W < 2 or H < 2:
        return None
    # ink: what differs from the background (dark on light or light on dark).
    # Rows: the line's own, grown from its densest row across gaps no taller
    # than an underscore's, so the next line's ascenders/descenders reaching
    # into the padded box (past a wider blank) are left out.
    diff = np.abs(a - np.median(a))
    thr = max(20.0, 0.4 * float(np.percentile(diff, 99)))
    rows = (diff > thr).sum(axis=1) >= 1 + 0.002 * W
    q = H // 4
    seed = q + int(np.argmax((diff > thr)[q:H - q].sum(axis=1))) if H - q > q else H // 2
    gap = max(2, int(0.12 * H))
    top = bot = seed
    for step, edge in ((-1, -1), (1, H)):
        blank, r = 0, seed + step
        while r != edge and blank <= gap:
            if rows[r]:
                blank = 0
                top, bot = min(top, r), max(bot, r)
            else:
                blank += 1
            r += step
    prof = (diff[top:bot + 1] > thr).sum(axis=0).astype(np.float32)
    pmax = float(prof.max())
    if pmax <= 0:
        return None
    tol = 0.02 * pmax
    xs = [(c + 0.5) / span * W for c in cols]       # where each character was read
    # the text's ink: from the first to the last inked column near the first
    # and last readings (a glyph is never much wider than the line is high)
    ink = np.nonzero(prof > tol)[0]
    ink = ink[(ink >= xs[0] - H) & (ink < xs[-1] + H)]
    if not len(ink):
        return None
    left, right = int(ink[0]), int(ink[-1]) + 1
    cuts = [0.0] * (len(text) + 1)
    if len(idx) > 1:
        _place_gaps(prof[:right], tol, pmax, text, idx, xs, H, left, cuts)
    # outer edges: just outside the ink (not the detector's padded box)
    margin = 0.06 * H
    left, right = max(0.0, left - margin), min(float(W), right + margin)
    for k in range(idx[0] + 1):                     # (leading spaces: before the ink)
        cuts[k] = left * k / idx[0] if idx[0] else left
    n = len(text) - idx[-1]
    for k in range(idx[-1] + 1, len(text) + 1):     # (trailing spaces: after it)
        cuts[k] = right + (W - right) * (k - idx[-1] - 1) / max(1, n - 1)
    out, prev = [], 0.0
    for c in cuts:
        prev = max(prev, min(float(W), c))
        out.append(prev)
    return out


def _rel_width(ch: str) -> float:
    """Typical advance of a character in a proportional font, in em."""
    if _is_wide(ch):
        return 1.0
    if ch in "ijl.,:;!'|`":
        return 0.28
    if ch == " ":
        return 0.3
    if ch in "frtI()[]{}/\\-\"":
        return 0.38
    if ch in "mwMW%@":
        return 0.85
    if "A" <= ch <= "Z":
        return 0.68
    return 0.55


def _place_gaps(prof, tol, pmax, text, idx, xs, H, start, cuts):
    """Set cuts[ia+1 .. ib] for each pair of neighbouring non-space characters
    text[ia], text[ib] read at xs (crop pixels), from the column ink profile
    (the text's ink runs from column `start` to the end of `prof`).

    A reading lies somewhere in or near its glyph, about one reading column
    either way, which for narrow glyphs can be the neighbour's.  So rather
    than pick each gap on its own, the gaps are matched to the boundaries
    all at once, in order (a small dynamic program): every blank run within
    the text should part two characters, each boundary should get one, close
    to the midpoint of its two readings, and each character should be about
    as wide as its kind usually is.  Where glyphs touch, the thinnest spot
    (a local ink minimum) stands in for a blank."""
    W = len(prof)
    lo_x, hi_x = start, W - 1
    # candidates: (start, end, ink 0..1, blank?) over the text's own stretch
    cands = []
    x = lo_x
    while x <= hi_x:
        if prof[x] <= tol:
            j = x
            while j + 1 <= hi_x and prof[j + 1] <= tol:
                j += 1
            cands.append((x, j + 1, 0.0, True))
            x = j + 1
            continue
        j = x
        while j + 1 <= hi_x and prof[j + 1] == prof[x]:
            j += 1
        left = prof[x - 1] if x > 0 else float("inf")
        right = prof[j + 1] if j + 1 < W else float("inf")
        if left > prof[x] and right > prof[x]:
            cands.append((x, j + 1, float(prof[x]) / pmax, False))
        x = j + 1
    # boundaries: one per pair of neighbouring readings (a run of spaces
    # between them is one boundary, split later)
    bounds = []
    for (ia, xa), (ib, xb) in zip(zip(idx, xs), zip(idx[1:], xs[1:])):
        bounds.append((ia, ib - ia - 1, (xa + xb) / 2))
    steps = sorted(b - a for (a, b), (ia, ib) in zip(zip(xs, xs[1:]), zip(idx, idx[1:]))
                   if ib == ia + 1) or [xs[-1] - xs[0]]
    pitch = max(2.0, steps[len(steps) // 2])
    # each boundary also gets a fallback at its own expected place
    pts = [(c[0], c[1], c[2], c[3], None) for c in cands]
    pts += [(e, e, 1.0, False, j) for j, (_ia, _m, e) in enumerate(bounds)]
    pts.sort(key=lambda c: (c[0] + c[1]) / 2)
    K = len(pts)
    skip = [1.0 if c[3] else 0.0 for c in pts]       # leaving a blank unused
    pre = [0.0]
    for v in skip:
        pre.append(pre[-1] + v)

    def cost(j, k):
        ia, m, e = bounds[j]
        s0, s1, ink, _blank, own = pts[k]
        if own is not None:
            return 3.0 if own == j else None
        c = (s0 + s1) / 2
        d = abs(c - e) / pitch
        if d > 1.6:
            return None
        v = d * d + 3.0 * ink
        if m:                                       # spaces: want a wide blank
            v -= min(1.0, (s1 - s0) / max(1.0, 0.3 * H))
        return v

    # ... and a character's width should be about what its kind is
    rel = [_rel_width(text[i]) for i in idx]
    sp = _rel_width(" ")
    em = (xs[-1] - xs[0]) / max(1e-6, sum(rel[1:-1]) + (rel[0] + rel[-1]) / 2
                                + sp * sum(m for _ia, m, _e in bounds))

    def width_cost(j, k0, k):
        want = em * (rel[j] + sp * (bounds[j - 1][1] + bounds[j][1]) / 2)
        got = (pts[k][0] + pts[k][1] - pts[k0][0] - pts[k0][1]) / 2
        r = math.log(max(got, 0.5) / max(want, 0.5))
        return r * r

    INF = float("inf")
    prev = {}                                       # k -> (cost, path)
    for k in range(K):
        c = cost(0, k)
        if c is not None:
            prev[k] = (c + pre[k], (k,))
    for j in range(1, len(bounds)):
        cur = {}
        opts = [(k, cost(j, k)) for k in range(K)]
        for k, c in opts:
            if c is None:
                continue
            best = (INF, None)
            for k0, (c0, path) in prev.items():
                if k0 < k:
                    t = c0 + (pre[k] - pre[k0 + 1]) + c + width_cost(j, k0, k)
                    if t < best[0]:
                        best = (t, path)
            if best[1] is not None:
                cur[k] = (best[0], best[1] + (k,))
        prev = cur
        if not prev:
            break
    if prev and len(next(iter(prev.values()))[1]) == len(bounds):
        path = min(prev.values(), key=lambda v: v[0] + (pre[K] - pre[v[1][-1] + 1]))[1]
    else:
        path = None
    for j, (ia, m, e) in enumerate(bounds):
        g0, g1 = (pts[path[j]][0], pts[path[j]][1]) if path else (e, e)
        for k in range(m + 1):
            cuts[ia + 1 + k] = g0 + (g1 - g0) * (2 * k + 1) / (2 * (m + 1))


def _crop_to_box(xs, width, box, turned=False):
    """Positions across a line crop -> fractions along the box's midline
    (from the middle of box[0]-box[3] to the middle of box[1]-box[2]).

    The crop is a perspective warp of the box: where its top and bottom
    edges differ in length, equal steps across the crop are not equal steps
    along the box (a few pixels off mid-line), so each position is carried
    back through that warp.  `turned`: the crop was turned upside down."""
    import numpy as np
    import cv2

    b = np.asarray(box, dtype=np.float32).reshape(4, 2)
    w = float(max(np.linalg.norm(b[0] - b[1]), np.linalg.norm(b[2] - b[3])))
    h = float(max(np.linalg.norm(b[0] - b[3]), np.linalg.norm(b[1] - b[2])))
    if w < 1 or h < 1 or width <= 0:
        return None
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    inv = cv2.getPerspectiveTransform(dst, b)
    k = w / float(width)
    pts = np.array([[[(width - x if turned else x) * k, h / 2] for x in xs]], dtype=np.float32)
    img = cv2.perspectiveTransform(pts, inv)[0]
    ml, mr = (b[0] + b[3]) / 2, (b[1] + b[2]) / 2
    axis = mr - ml
    d2 = float(axis @ axis)
    if d2 <= 0:
        return None
    return [min(1.0, max(0.0, float((p - ml) @ axis) / d2)) for p in img]


def release():
    """A resident View with no window open: stop recognitions in progress (at
    their next model call; a helper process is ended) and drop the engine so
    its memory can go back to the system.  The next recognition loads it again."""
    global _engine, _gen
    _gen += 1
    engine = _engine
    if isinstance(engine, _Helper):
        engine.close()
    starting = _starting                         # one still loading: _get_engine drops it
    if starting is not None:
        starting.close()
    if _engine_lock.acquire(blocking=False):     # (not while one is being created)
        try:
            if _engine is engine:
                _engine = None
        finally:
            _engine_lock.release()


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
    """Run the in-process engine on a BGR array: [(box, text, confidence,
    cuts)] with raw corner order, cuts along the box's midline (from the
    box[0]-box[3] edge) or None (also
    what the helper process sends back)."""
    engine = _get_engine()
    tracked = getattr(engine, "_view_cuts", False)
    with _run_lock:
        res = engine(arr, return_word_box=True) if tracked else engine(arr)
    boxes = getattr(res, "boxes", None)
    txts = getattr(res, "txts", None)
    scores = getattr(res, "scores", None)
    if boxes is None or txts is None:
        return []
    words = getattr(res, "word_results", None) if tracked else None
    if not isinstance(words, (tuple, list)) or len(words) != len(txts):
        words = [None] * len(txts)
    out = []
    for b, t, s, w in zip(boxes, txts, scores, words):
        cuts = w[0] if isinstance(w, tuple) and len(w) == 1 and isinstance(w[0], list) else None
        out.append(([(float(p[0]), float(p[1])) for p in b], t or "", float(s), cuts))
    return out


def recognize(image) -> list[OcrLine]:
    """Run OCR on a PIL image / path / bytes. Blocking (1-3 s on a CPU)."""
    _tls.gen = _gen
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
    for item in raw:
        b, t0, s = item[:3]
        t0 = t0 or ""
        t = t0.strip()
        if not t:
            continue
        pts = [(float(p[0]), float(p[1])) for p in b]
        box = _order_corners(pts)
        cuts = item[3] if len(item) > 3 else None    # (an older helper sends none)
        if cuts is not None:
            cuts = _box_cuts(cuts, t0, pts, box)
        out.append(OcrLine(box, t, float(s), cuts))
    return out


def _box_cuts(cuts, raw_text, pts, box):
    """Cuts given from the pts[0]-pts[3] edge for raw_text, as OcrLine.cuts:
    from the ordered box's box[0]-box[3] edge, for the stripped text.  None if the box
    turned a quarter (the cuts would run across the line)."""
    try:
        cuts = [float(c) for c in cuts]
    except (TypeError, ValueError):
        return None
    if len(cuts) != len(raw_text) + 1 or len(pts) != 4:
        return None
    lead = len(raw_text) - len(raw_text.lstrip())
    cuts = cuts[lead:lead + len(raw_text.strip()) + 1]
    shift = next((k for k in range(4) if box == pts[k:] + pts[:k]), None)
    if shift == 2:                     # same box, read from the other end
        return [1.0 - c for c in cuts]
    return cuts if shift == 0 else None


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
    cum: list = field(default_factory=list)  # char_bounds(): fraction along the line at each char boundary (len = n+1)


def _char_cum(text: str):
    """Boundaries estimated from typical character widths (no alignment)."""
    ws = [1.0 if _is_wide(c) else (0.35 if c in " .,;:!'|il" else 0.6) for c in text]
    total = sum(ws) or 1.0
    cum = [0.0]
    for w in ws:
        cum.append(cum[-1] + w / total)
    return cum


def char_bounds(text: str, cuts=None):
    """The len(text)+1 character boundaries of a line, as fractions along it
    (its midline, from the box[0]-box[3] edge): the recognizer's `cuts` when they fit the text, else
    an estimate from typical character widths.  Selection highlights, hit
    tests and copied text all go by these, so they always agree."""
    if cuts is not None:
        try:
            cum = [min(1.0, max(0.0, float(c))) for c in cuts]
        except (TypeError, ValueError):
            cum = None
        if cum and len(cum) == len(text) + 1 and (
                all(b >= a for a, b in zip(cum, cum[1:]))
                or all(b <= a for a, b in zip(cum, cum[1:]))):
            return cum
    return _char_cum(text)


def nearest_bound(bounds, t: float) -> int:
    """Index of the boundary nearest fraction t (a caret position)."""
    return min(range(len(bounds)), key=lambda k: abs(bounds[k] - t)) if bounds else 0


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _along(box, x, y) -> float:
    """Where (x, y) falls along a line box, 0..1 on its midline (the axis
    char_bounds() measure on)."""
    ml, mr = _lerp(box[0], box[3], 0.5), _lerp(box[1], box[2], 0.5)
    ax, ay = mr[0] - ml[0], mr[1] - ml[1]
    d2 = ax * ax + ay * ay
    t = 0.0 if d2 == 0 else ((x - ml[0]) * ax + (y - ml[1]) * ay) / d2
    return max(0.0, min(1.0, t))


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
            it.cum = char_bounds(it.line.text, getattr(it.line, "cuts", None))
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
        return best, nearest_bound(it.cum, _along(it.line.box, x, y))

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

    def selection_box(self):
        """(x0, y0, x1, y1) around the selected lines (OCR image px), or None."""
        r = self._range()
        if r is None:
            return None
        pts = [p for i in range(r[0][0], r[1][0] + 1) for p in self._items[i].line.box]
        if not pts:
            return None
        return (min(p[0] for p in pts), min(p[1] for p in pts),
                max(p[0] for p in pts), max(p[1] for p in pts))

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
