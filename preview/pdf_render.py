"""Background page rendering for the PDF viewer.

Poppler is not thread-safe per document, so every RenderWorker thread opens
its own Poppler.Document per source. Two workers run: one for the main view
(whole pages, or 1024 px tiles when zoomed in far), one for thumbnails, so a
slow thumbnail never delays the page you're looking at. Job queues are
replaced wholesale on every submit, which cancels stale requests for free.
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict, defaultdict

import cairo
import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib

from . import pdf_ops

TILE = 1024                 # device px
TILE_ABOVE = 4096           # whole-page textures above this many device px get tiled
MAX_PAGE_PX = 24_000_000    # cap for one whole-page texture


def apply_rotation(cr, rot, dw, dh):
    rot %= 360
    if rot == 90:
        cr.translate(dw, 0)
        cr.rotate(math.pi / 2)
    elif rot == 180:
        cr.translate(dw, dh)
        cr.rotate(math.pi)
    elif rot == 270:
        cr.translate(0, dh)
        cr.rotate(-math.pi / 2)


class Job:
    """key: cache key. pk: (src, index, rot, forms_fp). scale: device px/pt.
    tile: None or (tx, ty). forms: dict to apply first."""
    __slots__ = ("key", "src", "index", "rot", "scale", "tile", "forms")

    def __init__(self, key, src, index, rot, scale, tile=None, forms=None):
        self.key, self.src, self.index, self.rot = key, src, index, rot
        self.scale, self.tile, self.forms = scale, tile, forms or {}


class _ThreadState:
    def __init__(self):
        self.docs = {}
        self.gen = -1
        self.applied = {}           # (src, index) -> forms dict applied in our docs
        self.orig = {}              # (src, index) -> original field values


class RenderWorker:
    """A pool of `threads` render threads sharing one job list. Every thread
    owns its own Poppler documents."""

    def __init__(self, deliver, name="pdf-render", threads=1):
        self._deliver = deliver
        self._cv = threading.Condition()
        self._jobs: list[Job] = []
        self._sources: list = []
        self._gen = 0
        self._inflight = set()
        self._quit = False
        self.busy_time = 0.0
        self._threads = [threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True)
                         for i in range(max(1, threads))]
        for t in self._threads:
            t.start()
        self._thread = self._threads[0]

    def is_alive(self):
        return any(t.is_alive() for t in self._threads)

    def reset(self, sources, gen):
        with self._cv:
            self._sources = list(sources)
            self._gen = gen
            self._jobs = []
            self._cv.notify_all()

    def set_sources(self, sources):
        with self._cv:
            self._sources = list(sources)

    def submit(self, jobs):
        with self._cv:
            self._jobs = [j for j in jobs if j.key not in self._inflight]
            self._cv.notify_all()

    def stop(self):
        with self._cv:
            self._quit = True
            self._jobs = []
            self._cv.notify_all()

    def _run(self):
        import time
        st = _ThreadState()
        while True:
            with self._cv:
                while not self._jobs and not self._quit:
                    self._cv.wait()
                if self._quit:
                    st.docs = {}
                    return
                job = self._jobs.pop(0)
                gen, sources = self._gen, self._sources
                self._inflight.add(job.key)
            if gen != st.gen:
                st.docs, st.applied, st.orig = {}, {}, {}
                st.gen = gen
            t0 = time.monotonic()
            try:
                tex = self._render(st, job, sources)
            except Exception as e:  # keep the thread alive no matter what
                print("pdf render error:", repr(e))
                tex = None
            self.busy_time += time.monotonic() - t0
            with self._cv:
                self._inflight.discard(job.key)
            if tex is not None:
                GLib.idle_add(self._deliver, gen, job, tex, time.monotonic() - t0)

    def _render(self, st, job, sources):
        doc = st.docs.get(job.src)
        if doc is None:
            doc = st.docs[job.src] = pdf_ops.poppler_open(sources[job.src])
        page = doc.get_page(job.index)
        pid = (job.src, job.index)
        if job.forms or st.applied.get(pid):
            if st.applied.get(pid) != job.forms:
                orig = st.orig.setdefault(pid, {})
                restore = {k: v for k, v in orig.items() if k not in job.forms}
                pdf_ops.apply_forms(page, {**restore, **job.forms}, orig)
                st.applied[pid] = dict(job.forms)
        w, h = page.get_size()
        rot = job.rot
        dw, dh = (h, w) if rot % 180 else (w, h)
        scale = job.scale
        if job.tile is None:
            if dw * dh * scale * scale > MAX_PAGE_PX:
                scale = math.sqrt(MAX_PAGE_PX / (dw * dh))
            pw = max(1, round(dw * scale))
            ph = max(1, round(dh * scale))
            ox = oy = 0
        else:
            tx, ty = job.tile
            fw, fh = round(dw * scale), round(dh * scale)
            ox, oy = tx * TILE, ty * TILE
            pw, ph = min(TILE, fw - ox), min(TILE, fh - oy)
            if pw <= 0 or ph <= 0:
                return None
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
        cr = cairo.Context(surf)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        if job.tile is None:
            cr.scale(pw / dw, ph / dh)
        else:
            cr.translate(-ox, -oy)
            cr.scale(scale, scale)
        apply_rotation(cr, rot, dw, dh)
        page.render(cr)
        surf.flush()
        data = GLib.Bytes.new(bytes(surf.get_data()))
        return Gdk.MemoryTexture.new(pw, ph, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                     data, surf.get_stride())


class TexCache:
    """LRU of textures bounded by bytes. Keys start with a page key pk
    (4-tuple); whole-page textures are pk + (scale_key,), tiles add (tx, ty)."""

    def __init__(self, budget):
        self.budget = budget
        self.d = OrderedDict()
        self.size = 0
        self.by_page = defaultdict(set)

    def get(self, key):
        v = self.d.get(key)
        if v is None:
            return None
        self.d.move_to_end(key)
        return v[0]

    def has(self, key):
        return key in self.d

    def put(self, key, tex):
        if key in self.d:
            self.size -= self.d.pop(key)[1]
        n = tex.get_width() * tex.get_height() * 4
        self.d[key] = (tex, n)
        self.size += n
        self.by_page[key[:4]].add(key)
        while self.size > self.budget and len(self.d) > 1:
            k, (_, m) = self.d.popitem(last=False)
            self.size -= m
            s = self.by_page.get(k[:4])
            if s is not None:
                s.discard(k)
                if not s:
                    del self.by_page[k[:4]]

    def best(self, pk):
        """Largest whole-page texture cached for a page (any scale)."""
        best = None
        for k in self.by_page.get(pk, ()):
            if len(k) != 5:
                continue
            t = self.d[k][0]
            if best is None or t.get_width() > best.get_width():
                best = t
        return best

    def clear(self):
        self.d.clear()
        self.by_page.clear()
        self.size = 0
