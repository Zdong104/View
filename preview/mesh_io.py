"""Mesh loading (STL / OBJ / PLY) and geometry statistics, vectorised with numpy.

Everything here is GUI-free so it can run in a worker thread and be unit tested.

    mesh = read_mesh("part.stl")   # fast: float32 triangle soup + bbox (MeshError)
    analyze(mesh)                   # weld, stats, packed smooth normals
    mesh = load_mesh("part.stl")   # both
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field

import numpy as np


class MeshError(Exception):
    """Raised for unreadable / malformed / empty mesh files."""


@dataclass
class Mesh:
    """A triangle mesh.

    After read_mesh() only the triangle soup (`corners`) and bbox stats exist;
    analyze() adds the welded indexed form, full stats and smooth normals."""
    vertices: np.ndarray | None = None   # (V, 3) float32 welded unique positions
    faces: np.ndarray | None = None      # (T, 3) int32/int64 indices into vertices
    fmt: str = ""                        # "Binary STL", "ASCII STL", "OBJ", "PLY"
    up: str = "z"                        # natural up axis of the format ("z" or "y")
    file_size: int = 0
    stats: dict = field(default_factory=dict)
    face_n: np.ndarray | None = None     # (T, 3) float32 area-weighted face normals
    header: str = ""                     # STL header / "solid" name
    corners: np.ndarray | None = None    # (T, 3, 3) float32 triangle soup
    normals: np.ndarray | None = None    # (3T,) uint32 packed corner normals
    analyzed: bool = False
    decimated_from: int = 0              # original triangle count of a preview mesh
    origin: np.ndarray | None = None     # corners/vertices are stored relative to this
                                         # (the bbox centre: float32 precision + no
                                         # separate GPU position copy)
    _raw: tuple | None = None            # (verts32, faces) for indexed formats

    @property
    def n_triangles(self) -> int:
        return int(len(self.corners) if self.corners is not None else len(self.faces))

    @property
    def n_vertices(self) -> int:
        return int(len(self.vertices)) if self.vertices is not None else 0


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------
def load_mesh(path: str, data: bytes | None = None) -> Mesh:
    """Load + fully analyse a mesh (welded vertices/faces, stats, normals)."""
    mesh = read_mesh(path, data)
    analyze(mesh)
    return mesh


def read_mesh(path: str, data: bytes | None = None) -> Mesh:
    """Fast first phase: parse into a float32 triangle soup and compute the
    bounding box. Enough to display the model (flat shaded) immediately."""
    import mmap
    ext = os.path.splitext(path)[1].lower()
    mm = None
    try:
        if data is None:
            try:
                with open(path, "rb") as fh:
                    size = os.fstat(fh.fileno()).st_size
                    if ext in (".obj", ".ply") or size == 0:
                        data = fh.read()
                    else:   # STL: map it, binary files are then parsed in place
                        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                        data = mm
            except OSError as exc:
                raise MeshError(f"Could not read file: {exc.strerror or exc}") from exc
        file_size = len(data)
        header = ""
        if ext == ".obj":
            verts, faces = _parse_obj(data)
            fmt, up = "OBJ", "y"
        elif ext == ".ply":
            verts, faces = _parse_ply(data)
            fmt, up = "PLY", "y"
        else:
            verts, faces, fmt, header = _parse_stl(data)
            up = "z"
        if faces is None:
            corners = np.array(verts, dtype=np.float32, order="C").reshape(-1, 3, 3)
            raw = None
            del verts                    # drop the view into the mmap before closing it
        else:
            v32 = np.ascontiguousarray(verts, dtype=np.float32)
            faces = np.asarray(faces).reshape(-1, 3)
            faces = faces.astype(np.int32) if len(v32) < 2**31 - 1 else faces.astype(np.int64)
            corners = v32[faces]
            raw = (v32, faces)
    finally:
        del data
        if mm is not None:
            try:
                mm.close()
            except BufferError:    # a view is still alive; GC will release it
                pass
    flat = corners.reshape(len(corners), 9)
    finite = np.isfinite(flat).all(axis=1)
    if not finite.all():
        corners = corners[finite]
        if raw is not None:
            raw = (raw[0], raw[1][finite])
    if len(corners) == 0:
        raise MeshError("The file contains no usable triangles.")
    pts = corners.reshape(-1, 3)
    lo = pts.min(axis=0).astype(np.float64)
    hi = pts.max(axis=0).astype(np.float64)
    mesh = Mesh(fmt=fmt, up=up, file_size=file_size, header=header, corners=corners, _raw=raw)
    mesh.stats = {"bbox_min": lo, "bbox_max": hi, "size": hi - lo,
                  "triangles": len(corners)}
    _recenter(mesh)
    return mesh


def _recenter(mesh: Mesh):
    """Shift corners (and the raw vertex table) so the bbox centre is the origin."""
    origin = (mesh.stats["bbox_min"] + mesh.stats["bbox_max"]) / 2
    o32 = origin.astype(np.float32)
    flat = mesh.corners.reshape(-1, 3)
    for a, b in _chunks(len(flat)):
        flat[a:b] -= o32
    if mesh._raw is not None:
        mesh._raw = (mesh._raw[0] - o32, mesh._raw[1])
    mesh.origin = o32.astype(np.float64)


# --------------------------------------------------------------------------
# STL
# --------------------------------------------------------------------------
_STL_DTYPE = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])
assert _STL_DTYPE.itemsize == 50


def _stl_is_binary(data: bytes) -> bool:
    size = len(data)
    if size >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * n == size:
            return True                      # exact size match: binary even if "solid"
    head = data[:1024].lstrip()
    if head[:5].lower() == b"solid":
        if b"\x00" in data[:1024] and size >= 84:
            return True
        # a real ASCII STL has "facet" (or is an empty solid) near the top
        if b"facet" in head or b"endsolid" in head:
            return False
        return size >= 84 and (size - 84) % 50 == 0
    return True


def _clean_text(raw: bytes) -> str:
    text = raw.split(b"\x00", 1)[0].decode("latin-1", "replace")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split())


def _parse_stl(data: bytes):
    if len(data) == 0:
        raise MeshError("The file is empty.")
    if _stl_is_binary(data):
        if len(data) < 84:
            raise MeshError("File is too small to be a binary STL.")
        n = struct.unpack_from("<I", data, 80)[0]
        avail = (len(data) - 84) // 50
        if n == 0 and (len(data) - 84) % 50 == 0:
            n = avail                 # count field left as 0 by some exporters
        if n > avail:
            raise MeshError(
                f"Binary STL is truncated: header promises {n:,} triangles "
                f"but only {avail:,} are present.")
        if n == 0:
            raise MeshError("The STL file contains no triangles.")
        rec = np.frombuffer(data, dtype=_STL_DTYPE, count=n, offset=84)
        corners = rec["v"].reshape(-1, 3)
        return corners, None, "Binary STL", _clean_text(data[:80])
    # ASCII
    first = bytes(data[:1024]).lstrip()[:256].split(b"\n", 1)[0]
    name = first[5:] if first[:5].lower() == b"solid" else b""
    corners = _ascii_stl_fast(data)
    if corners is not None:
        return corners, None, "ASCII STL", _clean_text(name)
    matches = []
    for a, b in _text_pieces(data, 0, len(data)):
        matches += re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data[a:b])
    if not matches:
        raise MeshError("This doesn't look like a valid STL file (no vertices found).")
    if len(matches) % 3:
        raise MeshError("Malformed ASCII STL: vertex count is not a multiple of 3.")
    try:
        corners = np.array(matches, dtype="S").astype(np.float32)
    except ValueError as exc:
        raise MeshError(f"Malformed ASCII STL: bad number ({exc}).") from exc
    return corners, None, "ASCII STL", _clean_text(name)


_PIECE = 4 << 20     # bytes of text handled per step (bounds each GIL hold)


def _text_pieces(data, start, end, piece=_PIECE):
    """Split [start, end) of a bytes-like object at newlines into ~piece-sized
    ranges. Python's bytes methods hold the GIL, so the parsers work piece by
    piece to keep the GTK main loop (another thread) responsive."""
    a = start
    while a < end:
        b = min(a + piece, end)
        if b < end:
            nl = data.find(b"\n", b, min(end, b + (1 << 20)))
            b = end if nl < 0 else nl + 1
        yield a, b
        a = b


def _ascii_stl_fast(data):
    """Strip the keywords and let numpy's C parser read the number stream
    (12 numbers per facet: normal + 3 vertices). None -> use the regex path."""
    import warnings
    start = data.find(b"\n", 0, 4096)
    end = data.rfind(b"endsolid", max(0, len(data) - 4096))
    if start < 0 or end < 0:
        return None                      # odd layout
    parts = []
    with warnings.catch_warnings():
        # Only numpy's "could not read to the end" warnings from this module
        # become errors: the filter list is process-global and this runs on a
        # worker thread, so a blanket "error" would also turn unrelated
        # warnings on the GTK thread into exceptions.
        warnings.filterwarnings("error", category=DeprecationWarning, module=__name__)
        for a, b in _text_pieces(data, start, end):
            body = data[a:b]
            if b"solid" in body:
                return None              # several solids in one file
            for kw in (b"endfacet", b"facet normal", b"outer loop", b"endloop", b"vertex"):
                body = body.replace(kw, b" ")
            try:
                parts.append(np.fromstring(body, dtype=np.float32, sep=" "))
            except (ValueError, DeprecationWarning):
                return None
    vals = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    if len(vals) == 0 or len(vals) % 12:
        return None
    return vals.reshape(-1, 12)[:, 3:].reshape(-1, 3)


def _line_runs(arr, starts, mask):
    """Byte ranges [(a, b)) covering consecutive lines selected by mask."""
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    brk = np.flatnonzero(np.diff(idx) != 1)
    run_first = np.concatenate([[idx[0]], idx[brk + 1]])
    run_last = np.concatenate([idx[brk], [idx[-1]]])
    ends = np.concatenate([starts[1:], [len(arr)]])
    return list(zip(starts[run_first].tolist(), ends[run_last].tolist()))


def _parse_obj_fast(data: bytes):
    """Vectorised OBJ reader for the common case ("v x y z", "f a/b/c ...",
    no comments inside v/f lines, positive indices). None -> slow path.
    Works piece by piece (see _text_pieces) so it never blocks the GIL long."""
    import warnings
    arr = np.frombuffer(data, np.uint8)
    nl = np.flatnonzero(arr == 10)
    starts = np.concatenate([[0], nl + 1])
    starts = starts[starts < len(arr)]
    if len(starts) == 0:
        return None
    c0 = arr[starts]
    c1 = arr[np.minimum(starts + 1, len(arr) - 1)]
    vmask = (c0 == ord("v")) & (c1 == ord(" "))
    fmask = (c0 == ord("f")) & (c1 == ord(" "))
    nv_lines, nf_lines = int(vmask.sum()), int(fmask.sum())
    if nv_lines == 0 or nf_lines == 0:
        return None
    vparts, fidx, fcounts = [], [], []
    fields = None
    with warnings.catch_warnings():
        # Only numpy's "could not read to the end" warnings from this module
        # become errors: the filter list is process-global and this runs on a
        # worker thread, so a blanket "error" would also turn unrelated
        # warnings on the GTK thread into exceptions.
        warnings.filterwarnings("error", category=DeprecationWarning, module=__name__)
        try:
            for ra, rb in _line_runs(arr, starts, vmask):
                for a, b in _text_pieces(data, ra, rb):
                    piece = data[a:b]
                    if b"#" in piece:
                        return None
                    vparts.append(np.fromstring(piece.replace(b"v ", b" "), dtype=np.float64, sep=" "))
            for ra, rb in _line_runs(arr, starts, fmask):
                for a, b in _text_pieces(data, ra, rb):
                    piece = data[a:b]
                    if b"#" in piece or b"\\\\" in piece:
                        return None
                    piece = piece.replace(b"f ", b" ")
                    if fields is None:
                        tok = piece.split(None, 1)[0]
                        fields = tok.count(b"/") + 1 - tok.count(b"//")
                    fa = np.frombuffer(piece, np.uint8)
                    space = (fa == 32) | (fa == 9) | (fa == 10) | (fa == 13)
                    tok_start = ~space & np.concatenate([[True], space[:-1]])
                    nlines = int(np.count_nonzero(fa == 10)) + (0 if piece.endswith(b"\n") else 1)
                    line_of = _cumsum(fa == 10, np.int32)
                    counts = np.bincount(line_of[tok_start], minlength=nlines)[:nlines]
                    nums = piece.replace(b"//", b" ").replace(b"/", b" ")
                    idx = np.fromstring(nums, dtype=np.int64, sep=" ")
                    if len(idx) != int(counts.sum()) * fields:
                        return None      # mixed "a/b/c" and "a" tokens etc.
                    fidx.append(idx[::fields])
                    fcounts.append(counts.astype(np.int64))
        except (ValueError, DeprecationWarning):
            return None
    vals = np.concatenate(vparts)
    k = len(vals) / nv_lines
    if k != int(k) or int(k) < 3:
        return None
    verts = vals.reshape(nv_lines, int(k))[:, :3]
    idx = np.concatenate(fidx)
    counts = np.concatenate(fcounts)
    if len(counts) != nf_lines or len(idx) == 0 or idx.min() < 1 or idx.max() > len(verts):
        return None                      # negative/relative indices: slow path
    faces = _poly_to_tris(idx - 1, counts)
    if len(faces) == 0:
        return None
    return verts, faces


def _parse_obj(data: bytes):
    fast = _parse_obj_fast(data)
    if fast is not None:
        return fast
    vlines, flines, fvcount = [], [], []
    nv = 0
    for line in data.splitlines():
        if line[:2] == b"v " or line[:2] == b"v\t":
            vlines.append(line[2:])
            nv += 1
        elif line[:2] == b"f " or line[:2] == b"f\t":
            flines.append(line[2:])
            fvcount.append(nv)
    if not vlines:
        raise MeshError("This doesn't look like a valid OBJ file (no vertices).")
    if not flines:
        raise MeshError("The OBJ file has no faces (point clouds aren't supported).")

    def strip_comment(lst):
        if any(b"#" in l for l in lst):
            return [l.split(b"#", 1)[0] for l in lst]
        return lst

    vlines = strip_comment(vlines)
    flines = strip_comment(flines)

    # vertices: first three numbers of each line (lines may carry w or r g b)
    vals = np.fromstring(b" ".join(vlines), dtype=np.float64, sep=" ")
    k = len(vals) / len(vlines)
    if k == int(k) and int(k) >= 3 and all(len(l.split()) == int(k) for l in vlines[:50]):
        verts = vals.reshape(len(vlines), int(k))[:, :3]
    else:
        try:
            verts = np.array([[float(t) for t in l.split()[:3]] for l in vlines])
        except ValueError as exc:
            raise MeshError(f"Malformed OBJ vertex line ({exc}).") from exc
        if verts.ndim != 2 or verts.shape[1] != 3:
            raise MeshError("Malformed OBJ: vertex with fewer than 3 coordinates.")

    # faces: keep only the position index of "v/vt/vn" tokens
    blob = re.sub(rb"/\S*", b"", b"\n".join(flines))
    rows = blob.split(b"\n")
    counts = np.fromiter((len(r.split()) for r in rows), dtype=np.int64, count=len(rows))
    try:
        idx = np.fromstring(blob, dtype=np.int64, sep=" ")
    except ValueError as exc:
        raise MeshError(f"Malformed OBJ face line ({exc}).") from exc
    if len(idx) != counts.sum():
        raise MeshError("Malformed OBJ face indices.")
    keep = counts >= 3
    if not keep.all():
        # drop degenerate faces (fewer than 3 vertices)
        idx = idx[np.repeat(keep, counts)]
        counts = counts[keep]
        fvcount = np.asarray(fvcount)[keep]
    if len(counts) == 0:
        raise MeshError("The OBJ file has no polygonal faces.")
    if (idx < 0).any():
        rel = np.repeat(np.asarray(fvcount, dtype=np.int64), counts)
        idx = np.where(idx < 0, rel + idx, idx - 1)
    else:
        idx = idx - 1
    if idx.min() < 0 or idx.max() >= len(verts):
        raise MeshError("The OBJ file references vertices that don't exist.")
    faces = _triangulate_fan(idx, counts)
    return verts, faces


def _triangulate_fan(idx: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Fan-triangulate a flat polygon index list (polygon i has counts[i] verts)."""
    if (counts == 3).all():
        return idx.reshape(-1, 3)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    ntri = counts - 2
    tri_start = np.repeat(starts, ntri)
    local = np.arange(ntri.sum()) - np.repeat(np.cumsum(ntri) - ntri, ntri)
    return np.stack([idx[tri_start], idx[tri_start + local + 1], idx[tri_start + local + 2]], axis=1)


# --------------------------------------------------------------------------
# PLY
# --------------------------------------------------------------------------
_PLY_TYPES = {
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
    "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
    "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}


def _parse_ply(data: bytes):
    if not data.startswith(b"ply"):
        raise MeshError("This doesn't look like a PLY file (missing 'ply' magic).")
    end = data.find(b"end_header")
    if end < 0:
        raise MeshError("Malformed PLY: no end_header.")
    nl = data.find(b"\n", end)
    body_start = nl + 1 if nl >= 0 else len(data)
    header = data[:end].decode("ascii", "replace").replace("\r", "").split("\n")
    fmt = None
    elements = []     # [name, count, [(name, type) | (name, ('list', ct, it))]]
    for line in header:
        tok = line.split()
        if not tok:
            continue
        if tok[0] == "format":
            fmt = tok[1]
        elif tok[0] == "element":
            elements.append([tok[1], int(tok[2]), []])
        elif tok[0] == "property" and elements:
            try:
                if tok[1] == "list":
                    elements[-1][2].append((tok[4], ("list", _PLY_TYPES[tok[2]], _PLY_TYPES[tok[3]])))
                else:
                    elements[-1][2].append((tok[2], _PLY_TYPES[tok[1]]))
            except (KeyError, IndexError) as exc:
                raise MeshError(f"Malformed PLY property line: {line!r}") from exc
    if fmt not in ("ascii", "binary_little_endian", "binary_big_endian"):
        raise MeshError(f"Unsupported PLY format: {fmt}")
    names = [e[0] for e in elements]
    if "vertex" not in names:
        raise MeshError("The PLY file has no vertex element.")
    if "face" not in names:
        raise MeshError("The PLY file has no faces (point clouds aren't supported).")

    verts = faces = None
    if fmt == "ascii":
        lines = data[body_start:].split(b"\n")
        pos = 0
        for name, count, props in elements:
            chunk = lines[pos:pos + count]
            pos += count
            if len(chunk) < count:
                raise MeshError("Malformed PLY: file ends early.")
            if name == "vertex":
                pn = [p[0] for p in props]
                try:
                    ix = [pn.index(a) for a in "xyz"]
                    arr = np.array([l.split()[:len(props)] for l in chunk], dtype=np.float64)
                except (ValueError, IndexError) as exc:
                    raise MeshError("Malformed PLY vertex data.") from exc
                if arr.ndim != 2:
                    raise MeshError("Malformed PLY vertex data.")
                verts = arr[:, ix]
            elif name == "face":
                lp = [i for i, p in enumerate(props) if isinstance(p[1], tuple)]
                if not lp:
                    raise MeshError("PLY face element has no index list.")
                if lp[0] != 0:
                    raise MeshError("PLY face lists after scalar properties aren't supported.")
                idx_parts, cnt = [], []
                try:
                    for l in chunk:
                        t = l.split()
                        k = int(t[0])
                        cnt.append(k)
                        idx_parts.append(t[1:1 + k])
                    flat = np.array([int(x) for p in idx_parts for x in p], dtype=np.int64)
                except (ValueError, IndexError) as exc:
                    raise MeshError("Malformed PLY face data.") from exc
                faces = _poly_to_tris(flat, np.array(cnt, dtype=np.int64))
    else:
        bo = "<" if fmt == "binary_little_endian" else ">"
        off = body_start
        for name, count, props in elements:
            has_list = any(isinstance(p[1], tuple) for p in props)
            if not has_list:
                dt = np.dtype([(p[0], bo + p[1]) for p in props])
                if off + dt.itemsize * count > len(data):
                    raise MeshError("Malformed PLY: file ends early.")
                arr = np.frombuffer(data, dtype=dt, count=count, offset=off)
                off += dt.itemsize * count
                if name == "vertex":
                    try:
                        verts = np.stack([arr[a].astype(np.float64) for a in "xyz"], axis=1)
                    except (KeyError, ValueError) as exc:
                        raise MeshError("PLY vertex element lacks x/y/z.") from exc
                continue
            # element with list property: try the fixed-size fast path
            arr, used = _ply_list_fast(data, off, count, props, bo)
            if arr is None:
                arr, used = _ply_list_slow(data, off, count, props, bo)
            off += used
            if name == "face":
                faces = arr
    if verts is None or faces is None:
        raise MeshError("The PLY file is missing vertices or faces.")
    if len(faces) == 0:
        raise MeshError("The PLY file contains no faces.")
    if faces.min() < 0 or faces.max() >= len(verts):
        raise MeshError("The PLY file references vertices that don't exist.")
    return verts, faces


def _poly_to_tris(flat, counts):
    keep = counts >= 3
    if not keep.all():
        flat = flat[np.repeat(keep, counts)]
        counts = counts[keep]
    return _triangulate_fan(flat, counts)


def _ply_list_fast(data, off, count, props, bo):
    """Assume every list in the element has the same length as the first one."""
    if count == 0:
        return np.zeros((0, 3), np.int64), 0
    fields, list_name = [], None
    pos = off
    for pname, ptype in props:
        if isinstance(ptype, tuple):
            ct = np.dtype(bo + ptype[1])
            if pos + ct.itemsize > len(data):
                return None, 0
            k = int(np.frombuffer(data, dtype=ct, count=1, offset=pos)[0])
            fields += [(pname + "_n", ct), (pname, bo + ptype[2], (k,))]
            if list_name is None and pname in ("vertex_indices", "vertex_index"):
                list_name = (pname, k)
            pos += ct.itemsize + np.dtype(ptype[2]).itemsize * k
        else:
            fields.append((pname, bo + ptype))
            pos += np.dtype(ptype).itemsize
    dt = np.dtype(fields)
    if off + dt.itemsize * count > len(data):
        return None, 0
    arr = np.frombuffer(data, dtype=dt, count=count, offset=off)
    for pname, ptype in props:
        if isinstance(ptype, tuple) and not (arr[pname + "_n"] == arr.dtype[pname].shape[0]).all():
            return None, 0
    if list_name is None:
        lists = [p for p, t in props if isinstance(t, tuple)]
        if not lists:
            return None, 0
        list_name = (lists[0], arr.dtype[lists[0]].shape[0])
    pname, k = list_name
    idx = arr[pname].astype(np.int64)
    if k < 3:
        return np.zeros((0, 3), np.int64), dt.itemsize * count
    return _triangulate_fan(idx.ravel(), np.full(count, k, np.int64)), dt.itemsize * count


def _ply_list_slow(data, off, count, props, bo):
    """Variable-length lists (e.g. mixed triangles and quads): parse row by row."""
    start = off
    flat, counts = [], []
    lists = [p for p, t in props if isinstance(t, tuple)]
    want = next((p for p in lists if p in ("vertex_indices", "vertex_index")), lists[0])
    try:
        for _ in range(count):
            for pname, ptype in props:
                if isinstance(ptype, tuple):
                    ct, it = np.dtype(bo + ptype[1]), np.dtype(bo + ptype[2])
                    k = int(np.frombuffer(data, ct, 1, off)[0])
                    off += ct.itemsize
                    vals = np.frombuffer(data, it, k, off)
                    off += it.itemsize * k
                    if pname == want:
                        flat.append(vals.astype(np.int64))
                        counts.append(k)
                else:
                    off += np.dtype(ptype).itemsize
    except ValueError as exc:
        raise MeshError("Malformed PLY: file ends early.") from exc
    flat = np.concatenate(flat) if flat else np.zeros(0, np.int64)
    return _poly_to_tris(flat, np.array(counts, np.int64)), off - start


# --------------------------------------------------------------------------
# welding, normals, stats
# --------------------------------------------------------------------------
_POOL = None


def _pool():
    global _POOL
    if _POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _POOL = ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2),
                                   thread_name_prefix="mesh")
    return _POOL


# A few numpy primitives (cumsum, ufunc.at, unique) hold the GIL for their
# whole run; in a worker thread that freezes the GTK main loop. These
# chunked versions bound each GIL hold to a few milliseconds.
_GIL_CHUNK = 2_000_000


def _cumsum(a, dtype=np.int64):
    a = np.asarray(a)
    out = np.empty(len(a), dtype)
    carry = dtype(0) if isinstance(dtype, type) else np.dtype(dtype).type(0)
    for i in range(0, len(a), _GIL_CHUNK):
        np.cumsum(a[i:i + _GIL_CHUNK], dtype=dtype, out=out[i:i + _GIL_CHUNK])
        out[i:i + _GIL_CHUNK] += carry
        carry = out[min(i + _GIL_CHUNK, len(a)) - 1]
    return out


def _minimum_at(target, idx, vals):
    for i in range(0, len(idx), _GIL_CHUNK // 2):
        np.minimum.at(target, idx[i:i + _GIL_CHUNK // 2], vals[i:i + _GIL_CHUNK // 2])


def _unique_first(keys):
    """Indices of the first occurrence of each distinct uint64 key (sorted by
    key) - np.unique(return_index) without the long GIL hold."""
    if len(keys) < 1_000_000:
        return np.unique(keys, return_index=True)[1]
    res = _sorted_buckets(keys)
    order = np.concatenate([r[0] for r in res])
    hs = np.concatenate([r[1] for r in res])
    flag = np.empty(len(hs), bool)
    flag[:1] = True
    np.not_equal(hs[1:], hs[:-1], out=flag[1:])
    return order[flag]


def _chunks(n, k=8):
    b = np.linspace(0, n, k + 1).astype(np.int64)
    return [(int(b[i]), int(b[i + 1])) for i in range(k) if b[i + 1] > b[i]]


def _hash_rows(p32: np.ndarray) -> np.ndarray:
    """64-bit hash of float32 xyz rows (bit-exact, -0 == +0), computed in
    parallel chunks."""
    p32 = np.ascontiguousarray(p32, dtype=np.float32)
    out = np.empty(len(p32), np.uint64)

    def job(r):
        a, b = r
        bits = (p32[a:b] + np.float32(0.0)).view(np.uint32)
        h = bits[:, 0].astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15)
        h ^= bits[:, 1].astype(np.uint64) * np.uint64(0xC2B2AE3D27D4EB4F)
        h ^= bits[:, 2].astype(np.uint64) * np.uint64(0x165667B19E3779F9)
        h ^= h >> np.uint64(29)
        h *= np.uint64(0xBF58476D1CE4E5B9)
        h ^= h >> np.uint64(32)
        out[a:b] = h
    if len(p32) < 1_000_000:
        job((0, len(p32)))
    else:
        list(_pool().map(job, _chunks(len(p32))))
    return out


def _sorted_buckets(h: np.ndarray):
    """Parallel argsort of uint64 keys: each thread radix-partitions a chunk
    on the top 10 bits, then the 1024 buckets are argsorted concurrently
    (numpy sorts release the GIL). Yields (sorted_index, sorted_keys) per
    bucket group, in key order. ~5x faster than np.argsort on 30M keys."""
    nb = 1024
    chunks = _chunks(len(h))

    def part(r):
        a, b = r
        bk = (h[a:b] >> np.uint64(54)).astype(np.uint16)
        o = np.argsort(bk, kind="stable") + a
        cnt = np.bincount(bk, minlength=nb)
        e = np.cumsum(cnt)
        return o, e - cnt, e
    parts = list(_pool().map(part, chunks))

    def job(ids):
        idx = np.concatenate([o[s[i]:e[i]] for i in ids for o, s, e in parts])
        keys = h[idx]
        # keys of different buckets never interleave, so one sort per group works
        order = np.argsort(keys)
        return idx[order], keys[order]
    return list(_pool().map(job, np.array_split(np.arange(nb), 64)))


def _weld(points: np.ndarray):
    """Merge bit-identical (float32) positions. Returns (unique_idx, inverse):
    points[unique_idx] are the unique positions, inverse maps each point to one."""
    h = _hash_rows(points)
    n = len(h)
    if n < 1_000_000:
        order = np.argsort(h)
        hs = h[order]
    else:
        res = _sorted_buckets(h)
        order = np.concatenate([r[0] for r in res])
        hs = np.concatenate([r[1] for r in res])
    flag = np.empty(n, bool)
    flag[:1] = True
    np.not_equal(hs[1:], hs[:-1], out=flag[1:])
    del hs
    ids = _cumsum(flag, np.int64) - 1
    inverse = np.empty(n, np.int32 if n < 2**31 else np.int64)
    inverse[order] = ids
    return order[flag], inverse


def _finish(verts, faces, fmt, up) -> Mesh:
    """Build + analyse a Mesh from in-memory arrays (faces None = soup)."""
    verts = np.asarray(verts)
    if faces is None:
        corners = np.ascontiguousarray(verts, dtype=np.float32).reshape(-1, 3, 3)
        raw = None
    else:
        v32 = np.ascontiguousarray(verts, dtype=np.float32)
        f = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
        corners, raw = v32[f], (v32, f)
    finite = np.isfinite(corners.reshape(len(corners), 9)).all(axis=1)
    if not finite.all():
        corners = corners[finite]
        raw = None if raw is None else (raw[0], raw[1][finite])
    if len(corners) == 0:
        raise MeshError("The file contains no usable triangles.")
    pts = corners.reshape(-1, 3)
    lo, hi = pts.min(0).astype(np.float64), pts.max(0).astype(np.float64)
    mesh = Mesh(fmt=fmt, up=up, corners=np.array(corners, np.float32), _raw=raw)
    mesh.stats = {"bbox_min": lo, "bbox_max": hi, "size": hi - lo, "triangles": len(corners)}
    _recenter(mesh)
    analyze(mesh)
    return mesh


def analyze(mesh: Mesh, normals: bool = True, stats: bool = True) -> Mesh:
    """Second phase: weld, full statistics (area, volume, watertightness,
    centre of mass) and crease-aware smooth normals. Runs the independent
    parts concurrently (numpy releases the GIL)."""
    c = mesh.corners
    if mesh._raw is not None:           # indexed input: weld the vertex table only
        v32, f = mesh._raw
        first, inv = _weld(v32)
        mesh.vertices = v32[first]
        mesh.faces = inv[f]
    else:
        first, inv = _weld(c.reshape(-1, 3))
        mesh.vertices = c.reshape(-1, 3)[first]
        mesh.faces = inv.reshape(-1, 3)
    mesh._raw = None
    mesh.face_n = np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0])
    # previews (decimated) are noisy: plain smoothing + face-normal fallback is
    # enough there and avoids the costly smoothing-group pass
    fut_norm = (_pool().submit(corner_normals_packed, mesh, exact=not mesh.decimated_from)
                if normals else None)
    if stats:
        fut_edges = _pool().submit(_edge_stats, mesh.faces, len(mesh.vertices))
        st = dict(mesh.stats)
        st.update(_solid_stats(mesh))
        st.update(fut_edges.result())
        st["vertices"] = len(mesh.vertices)
        st["triangles"] = len(mesh.faces)
        mesh.stats = st
        mesh.analyzed = True
    if fut_norm is not None:
        mesh.normals = fut_norm.result()
    return mesh


def face_normals(vertices, faces):
    """Area-weighted (unnormalised) face normals."""
    v0 = vertices[faces[:, 0]]
    return np.cross(vertices[faces[:, 1]] - v0, vertices[faces[:, 2]] - v0)


def _fn(mesh):
    fn = getattr(mesh, "face_n", None)
    if fn is None:
        fn = mesh.face_n = face_normals(mesh.vertices, mesh.faces)
    return fn


def _solid_stats(mesh: Mesh) -> dict:
    c, fn = mesh.corners, _fn(mesh)
    lo, hi = mesh.stats["bbox_min"], mesh.stats["bbox_max"]
    origin = mesh.origin if mesh.origin is not None else np.zeros(3)
    ctr = ((lo + hi) / 2 - origin).astype(np.float32)      # ~0 after recentring
    area = 0.5 * float(np.sqrt(np.einsum("ij,ij->i", fn, fn)).sum(dtype=np.float64))
    # signed tetrahedron sum about the bbox centre (better precision)
    tet = np.einsum("ij,ij->i", c[:, 0] - ctr, fn)          # 6 x signed tet volumes
    volume = float(tet.sum(dtype=np.float64)) / 6.0
    if abs(volume) > 0:
        tc = c.sum(axis=1, dtype=np.float64) - 3 * ctr      # 4 x tet centroid offset
        com = origin + ctr + (tet[:, None] * tc).sum(axis=0) / 6.0 / 4.0 / volume
    else:
        com = origin + ctr.astype(np.float64)
    return {"area": area, "volume": abs(volume), "volume_signed": volume,
            "center_of_mass": np.asarray(com, np.float64)}


def _edge_stats(f: np.ndarray, nv: int) -> dict:
    """Watertightness: every undirected edge used by exactly two triangles
    (triangles with a repeated vertex are degenerate and ignored)."""
    good = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])
    g = f if good.all() else f[good]
    e0 = np.concatenate([g[:, 0], g[:, 1], g[:, 2]]).astype(np.int64)
    e1 = np.concatenate([g[:, 1], g[:, 2], g[:, 0]]).astype(np.int64)
    key = np.minimum(e0, e1) * nv + np.maximum(e0, e1)
    del e0, e1
    key.sort()
    if len(key):
        change = np.flatnonzero(key[1:] != key[:-1]) + 1
        cnt = np.diff(np.concatenate([[0], change, [len(key)]]))
    else:
        cnt = np.zeros(0, np.int64)
    boundary = int((cnt == 1).sum())
    nonmanifold = int((cnt > 2).sum())
    return {"closed": boundary == 0 and nonmanifold == 0 and len(key) > 0,
            "boundary_edges": boundary, "nonmanifold_edges": nonmanifold}


def compute_stats(mesh: Mesh) -> dict:
    """Full statistics for an analysed (welded) mesh."""
    st = dict(mesh.stats)
    st.update(_solid_stats(mesh))
    st.update(_edge_stats(mesh.faces, len(mesh.vertices)))
    st["vertices"], st["triangles"] = len(mesh.vertices), len(mesh.faces)
    return st


def _normalize(a):
    n = np.sqrt(np.einsum("ij,ij->i", a, a))
    ok = n > 0
    out = a / np.where(ok, n, 1.0)[:, None]
    return out, ok


def corner_normals(mesh: Mesh, crease_deg: float = 40.0) -> np.ndarray:
    """Per-corner unit normals (3T, 3) float32 (unpacked; for tests/tools)."""
    return unpack_normals(corner_normals_packed(mesh, crease_deg))


def corner_normals_packed(mesh: Mesh, crease_deg: float = 40.0,
                          pair_budget: int = 30_000_000, exact: bool = True) -> np.ndarray:
    """Per-corner normals, packed GL_INT_2_10_10_10_REV (3T,) uint32: smooth
    across soft edges, sharp across edges whose faces differ by more than
    crease_deg (smoothing groups: CAD parts keep crisp edges, scans and
    organic shapes look smooth)."""
    faces = mesh.faces
    T, V = len(faces), len(mesh.vertices)
    fn = np.asarray(_fn(mesh), np.float32)
    fnu, fok = _normalize(fn)
    cos_t = np.float32(np.cos(np.radians(crease_deg)))

    # 1) area-weighted vertex normals (components in parallel)
    vn = np.empty((V, 3), np.float32)

    def comp(c):
        acc = np.zeros(V, np.float64)
        for k in range(3):
            acc += np.bincount(faces[:, k], weights=fn[:, c], minlength=V)
        vn[:, c] = acc
    list(_pool().map(comp, range(3)))
    vnu, _ = _normalize(vn)
    del vn
    packed_v = pack_normals(vnu)

    # 2) a vertex is "soft" if every incident face is within crease/2 of the
    #    average; all its corners then simply use the average
    dots = np.empty((T, 3), np.float32)

    def dot(k):
        dots[:, k] = np.einsum("ij,ij->i", vnu[faces[:, k]], fnu)
    list(_pool().map(dot, range(3)))
    dots[~fok] = 1.0                                       # ignore degenerate faces
    vmin = np.full(V, 2.0, np.float32)
    for k in range(3):
        _minimum_at(vmin, faces[:, k], dots[:, k])
    sharp_v = vmin < np.cos(np.radians(crease_deg / 2))
    out = packed_v[faces].ravel()
    if not sharp_v.any():
        return out
    inc_v = faces.ravel()
    sharp_c = np.flatnonzero(sharp_v[inc_v])
    dots = dots.ravel()
    # cheap fallback for sharp corners: face normal where the average bends too far
    bad = sharp_c[dots[sharp_c] <= cos_t]
    out[bad] = pack_normals(fnu[bad // 3])

    if not exact:
        return out
    # 3) exact smoothing groups for corners at sharp vertices (pairwise test
    #    against the other faces around the same vertex)
    order = sharp_c[np.argsort(inc_v[sharp_c], kind="stable")]
    sv = inc_v[order]
    deg = np.bincount(sv, minlength=V)
    d = deg[sv]
    gstart = (np.cumsum(deg) - deg)[sv]
    sel = np.flatnonzero(d <= 64)                        # huge fans keep the fallback
    dsel = d[sel].astype(np.int64)
    if dsel.sum() > pair_budget:
        return out
    csum = np.cumsum(dsel)
    lo = 0
    while lo < len(sel):
        target = (csum[lo - 1] if lo else 0) + 4_000_000
        hi = max(lo + 1, int(np.searchsorted(csum, target, side="right")))
        p = sel[lo:hi]
        dc = dsel[lo:hi]
        n = int(dc.sum())
        rep = np.repeat(np.arange(len(p)), dc)
        within = np.arange(n) - np.repeat(np.cumsum(dc) - dc, dc)
        fi = order[p][rep] // 3
        fj = order[gstart[p][rep] + within] // 3
        m = np.einsum("ij,ij->i", fnu[fi], fnu[fj]) > cos_t
        w = fn[fj] * m[:, None]
        acc = np.empty((len(p), 3), np.float32)
        for c in range(3):
            acc[:, c] = np.bincount(rep, weights=w[:, c], minlength=len(p))
        accu, ok = _normalize(acc)
        tgt = order[p]
        out[tgt] = pack_normals(np.where(ok[:, None], accu, fnu[tgt // 3]))
        lo = hi
    return out


def unpack_normals(p: np.ndarray) -> np.ndarray:
    p = p.astype(np.int64)
    comps = [((p >> s) & 0x3FF) for s in (0, 10, 20)]
    q = np.stack([np.where(c >= 512, c - 1024, c) for c in comps], -1).astype(np.float32) / 511.0
    return q


def pack_normals(n: np.ndarray) -> np.ndarray:
    """Pack (N, 3) unit normals into GL_INT_2_10_10_10_REV uint32."""
    q = np.round(np.clip(n, -1.0, 1.0) * 511.0).astype(np.int32) & 0x3FF
    return (q[:, 0] | (q[:, 1] << 10) | (q[:, 2] << 20)).astype(np.uint32)


def center_of(mesh: Mesh) -> np.ndarray:
    return (mesh.stats["bbox_min"] + mesh.stats["bbox_max"]) / 2


def positions(mesh: Mesh, center=None) -> np.ndarray:
    """(3T, 3) float32 corner positions relative to center (GPU layout).
    Without center this is a zero-copy view (corners are origin-relative)."""
    flat = mesh.corners.reshape(-1, 3)
    origin = mesh.origin if mesh.origin is not None else np.zeros(3)
    if center is None or np.allclose(center, origin):
        return flat
    return flat - np.asarray(np.asarray(center) - origin, np.float32)


def build_gpu_buffer(mesh: Mesh, center=None) -> np.ndarray:
    """Interleaved (3T, 4) float32: xyz relative to center + packed normal bits."""
    if not mesh.analyzed:
        analyze(mesh)
    if mesh.normals is None:
        mesh.normals = corner_normals_packed(mesh)
    buf = np.empty((3 * mesh.n_triangles, 4), np.float32)
    buf[:, :3] = positions(mesh, center)
    buf.view(np.uint32)[:, 3] = mesh.normals
    return buf


def decimate(mesh: Mesh, target: int = 1_500_000) -> Mesh:
    """Fast preview decimation by vertex clustering: snap corners to a grid
    sized for ~target triangles, drop collapsed and duplicate triangles.
    Returns a new (unanalysed) Mesh; the bbox stats are kept from the source."""
    c = mesh.corners
    T = len(c)
    if T <= target:
        return mesh
    rng = np.random.default_rng(0)
    sample = c[rng.integers(0, T, min(T, 20000))]
    edge = np.linalg.norm(sample[:, 1] - sample[:, 0], axis=1).mean()
    origin = mesh.origin if mesh.origin is not None else np.zeros(3)
    lo = (mesh.stats["bbox_min"] - origin).astype(np.float32)
    size = np.maximum(mesh.stats["size"], 1e-12)
    cell = np.float32(max(edge * np.sqrt(T / target) * 0.75, float(size.max()) / 2_000_000))
    for _ in range(4):
        n = np.floor(size / cell).astype(np.int64) + 1
        inv_cell = np.float32(1.0) / cell

        def keys(r, n=n, inv_cell=inv_cell):
            a, b = r
            q = ((c[a:b].reshape(-1, 3) - lo) * inv_cell).astype(np.int64)
            np.clip(q, 0, n - 1, out=q)
            k = ((q[:, 0] * n[1] + q[:, 1]) * n[2] + q[:, 2]).reshape(-1, 3)
            keep = (k[:, 0] != k[:, 1]) & (k[:, 1] != k[:, 2]) & (k[:, 0] != k[:, 2])
            k = k[keep]
            p = c[a:b][keep]
            # canonical rotation (smallest key first, winding kept) for dedupe
            rot = np.argmin(k, axis=1)
            rows = np.arange(len(k))
            sel = [rot, (rot + 1) % 3, (rot + 2) % 3]
            return (np.stack([k[rows, j] for j in sel], 1),
                    np.stack([p[rows, j] for j in sel], 1))
        res = list(_pool().map(keys, _chunks(T, 16)))
        kc = np.concatenate([r[0] for r in res])
        pc = np.concatenate([r[1] for r in res])
        del res
        ku = kc.view(np.uint64)
        hk = (ku[:, 0] * np.uint64(0x9E3779B97F4A7C15)) ^ (ku[:, 1] * np.uint64(0xC2B2AE3D27D4EB4F)) \
            ^ (ku[:, 2] * np.uint64(0x165667B19E3779F9))
        hk ^= hk >> np.uint64(31)
        first = _unique_first(hk)
        if len(first) <= target * 1.3:
            break
        cell = np.float32(cell * np.sqrt(len(first) / target))
    first = np.sort(first)               # keep the source order (GPU cache locality)
    # each cluster's vertex = mean of the source corners that landed in it
    # (cell centres would give a terraced, noisy-looking surface)
    kc, pc = kc[first], pc[first]
    flat_k = kc.reshape(-1).view(np.uint64)
    hk = flat_k * np.uint64(0x9E3779B97F4A7C15)
    hk ^= hk >> np.uint64(29)
    res = _sorted_buckets(hk) if len(hk) >= 1_000_000 else [(np.argsort(hk), None)]
    order = np.concatenate([r[0] for r in res])
    hs = hk[order]
    flag = np.empty(len(hs), bool)
    flag[:1] = True
    np.not_equal(hs[1:], hs[:-1], out=flag[1:])
    inv = np.empty(len(hs), np.int64)
    inv[order] = _cumsum(flag) - 1
    ng = int(flag.sum())
    cnt = np.bincount(inv, minlength=ng).astype(np.float32)
    flat_p = pc.reshape(-1, 3)
    mean = np.empty((ng, 3), np.float32)
    for j in range(3):
        mean[:, j] = np.bincount(inv, weights=flat_p[:, j], minlength=ng) / cnt
    corners = mean[inv].reshape(-1, 3, 3)
    out = Mesh(fmt=mesh.fmt, up=mesh.up, file_size=mesh.file_size, header=mesh.header,
               corners=np.ascontiguousarray(corners), decimated_from=T)
    out.stats = dict(mesh.stats)
    out.origin = mesh.origin
    return out
