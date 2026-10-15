"""Pure image operations (Pillow / numpy): loading, edit pipeline, saving, metadata.

An image's edits are a list of op tuples applied in order to the EXIF-upright
base image, so display, save, export and print all share one code path:

    ("rotate", 90|180|270)            clockwise
    ("flip",)                         horizontal mirror
    ("resize", w, h)
    ("crop", x0, y0, x1, y1)          coordinates in the image at that point
    ("ellipse", x0, y0, x1, y1)       crop + transparent outside the ellipse
    ("alpha", x, y, tolerance)        flood-fill a colour region to transparent
    ("color", {params})               see adjust_color()
    ("sig", overlay_dict)             signature composite
    ("markup", [item_dict, ...])      markup.Item shapes/text/ink flattened via cairo
"""
import io
import math
import os
import re
import struct
import time
import zlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
from .runtime import replace_file

# A local viewer: allow big images (Pillow's default bomb limit is ~179 MP).
Image.MAX_IMAGE_PIXELS = 1_500_000_000

WRITABLE = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".webp": "WEBP",
            ".tif": "TIFF", ".tiff": "TIFF", ".bmp": "BMP", ".gif": "GIF", ".apng": "PNG"}
ALPHA_FORMATS = {".png", ".webp", ".tif", ".tiff", ".gif", ".apng"}
GEOMETRY_OPS = ("rotate", "flip")

COLOR_DEFAULTS = {"auto": False, "exposure": 0.0, "contrast": 0.0, "highlights": 0.0,
                  "shadows": 0.0, "saturation": 0.0, "temperature": 0.0, "tint": 0.0,
                  "sepia": 0.0, "sharpness": 0.0}


def color_is_neutral(p):
    return all(p.get(k, v) == v for k, v in COLOR_DEFAULTS.items())


# ------------------------------------------------------------- loading ---

def _pixbuf_load(path):
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    if Path(path).suffix.lower() == ".svg":
        info = GdkPixbuf.Pixbuf.get_file_info(str(path))
        w, h = (info[1], info[2]) if info[0] else (0, 0)
        if w and h:
            s = max(1.0, 2048 / max(w, h))
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), int(w * s), int(h * s))
        else:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), 2048, 2048)
    else:
        pb = GdkPixbuf.Pixbuf.new_from_file(str(path))
        pb = pb.apply_embedded_orientation() or pb
    return pb


def pixbuf_to_pil(pb):
    mode = "RGBA" if pb.get_has_alpha() else "RGB"
    return Image.frombuffer(mode, (pb.get_width(), pb.get_height()), pb.get_pixels(),
                            "raw", mode, pb.get_rowstride(), 1).copy()


def normalize_mode(im):
    """Return im as RGB or RGBA."""
    if im.mode in ("RGB", "RGBA"):
        return im
    has_alpha = "A" in im.mode or "transparency" in im.info
    return im.convert("RGBA" if has_alpha else "RGB")


def load_base(path):
    """Load an image upright: (PIL RGB/RGBA image, exif, icc_profile)."""
    exif = icc = None
    try:
        with Image.open(path) as im:
            if im.mode.startswith("I") or im.mode == "F":
                raise ValueError("high bit depth")
            im.load()
            icc = im.info.get("icc_profile")
            exif = im.getexif()
            if exif.get(0x0112, 1) != 1:
                im = ImageOps.exif_transpose(im)
                exif = im.getexif()
            im = normalize_mode(im)
            return im, exif, icc
    except Exception:  # noqa: BLE001 - HEIC/AVIF/JXL/SVG/16-bit etc.
        return normalize_mode(pixbuf_to_pil(_pixbuf_load(path))), None, None


def oriented_size(path):
    """Upright (w, h) of an image file without decoding it, or None."""
    try:
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                w, h = h, w
            return w, h
    except Exception:  # noqa: BLE001
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf
            fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(str(path))
            return (w, h) if fmt else None
        except Exception:  # noqa: BLE001
            return None


def quick_preview(path, max_px):
    """Fast reduced decode for big JPEGs (DCT scaling via draft); None otherwise."""
    try:
        with Image.open(path) as im:
            if im.format != "JPEG" or max(im.size) < 2 * max_px:
                return None
            im.draft("RGB", (max_px, max_px))
            im.load()
            im = ImageOps.exif_transpose(im)
            return normalize_mode(im)
    except Exception:  # noqa: BLE001
        return None


def fit_size(im, max_px):
    """`im` scaled down so its longest side is <= max_px (same object if smaller)."""
    w, h = im.size
    if max(w, h) <= max_px:
        return im
    s = max_px / max(w, h)
    return im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BILINEAR,
                     reducing_gap=2.0)


def _cached_thumbnail(path):
    """Path of a valid GNOME thumbnail-cache entry for `path`, or None."""
    try:
        from gi.repository import Gio
        info = Gio.File.new_for_path(str(path)).query_info(
            "thumbnail::path,thumbnail::is-valid,thumbnail::failed",
            Gio.FileQueryInfoFlags.NONE, None)
        p = info.get_attribute_byte_string("thumbnail::path")
        if p and info.get_attribute_boolean("thumbnail::is-valid") and os.path.exists(p):
            return p
    except Exception:  # noqa: BLE001
        pass
    return None


def load_thumb(path, size=256):
    """Small upright thumbnail: GNOME thumbnail cache, then Pillow draft (JPEG),
    then GdkPixbuf at-size decoding."""
    cached = _cached_thumbnail(path)
    if cached:
        try:
            with Image.open(cached) as im:
                im.load()
                im = normalize_mode(im)
                if max(im.size) >= size * 0.75:
                    im.thumbnail((size, size))
                    return im
        except Exception:  # noqa: BLE001
            pass
    try:
        with Image.open(path) as im:
            if im.format == "JPEG":
                im.draft("RGB", (size, size))
                im.load()
                im = ImageOps.exif_transpose(im)
                im = normalize_mode(im)
                im.thumbnail((size, size))
                return im
    except Exception:  # noqa: BLE001
        pass
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), size, size)
    pb = pb.apply_embedded_orientation() or pb
    return pixbuf_to_pil(pb)


# ------------------------------------------------------------- pipeline ---

def _propagate(mask, region, axis):
    """Grow `region` to every run of `mask` along `axis` that touches it."""
    m = mask if axis == 1 else mask.T
    r = (region & mask) if axis == 1 else (region & mask).T
    prev = np.zeros_like(m)
    prev[:, 1:] = m[:, :-1]
    seg = np.cumsum((m & ~prev).ravel())
    seg = np.where(m.ravel(), seg, 0)
    hit = np.zeros(int(seg.max()) + 1, dtype=bool)
    hit[seg[r.ravel()]] = True
    hit[0] = False
    out = hit[seg].reshape(m.shape)
    return out if axis == 1 else out.T


def flood_region(cand, x, y):
    """Connected (4-neighbour) component of boolean mask `cand` containing (x, y)."""
    reg = np.zeros_like(cand)
    if not cand[y, x]:
        return reg
    reg[y, x] = True
    for _ in range(10000):
        new = _propagate(cand, _propagate(cand, reg, 1), 0)
        if np.array_equal(new, reg):
            break
        reg = new
    return reg


def _alpha_fill(im, x, y, tol):
    """Make the colour region connected to (x, y) transparent (Instant Alpha)."""
    im = im.convert("RGBA")
    a = np.asarray(im).copy()
    h, w = a.shape[:2]
    x = min(max(int(x), 0), w - 1)
    y = min(max(int(y), 0), h - 1)
    seed = a[y, x, :3].astype(np.int16)
    thresh = float(tol) * 2.55
    cand = np.abs(a[..., :3].astype(np.int16) - seed).max(axis=2) <= thresh
    cand &= a[..., 3] > 0
    cand[y, x] = True
    mask = flood_region(cand, x, y)
    a[..., 3] = np.where(mask, 0, a[..., 3])
    return Image.fromarray(a, "RGBA")


def adjust_color(im, p):
    """Apply colour-adjustment params (see COLOR_DEFAULTS) to a PIL image."""
    if color_is_neutral(p):
        return im
    alpha = im.getchannel("A") if im.mode == "RGBA" else None
    rgb = im.convert("RGB")
    a = np.asarray(rgb, dtype=np.float32) / 255.0
    if p.get("auto"):
        lo = np.percentile(a, 0.5, axis=(0, 1))
        hi = np.percentile(a, 99.5, axis=(0, 1))
        span = np.maximum(hi - lo, 1e-3)
        a = np.clip((a - lo) / span, 0, 1)
    ev = p.get("exposure", 0.0)
    if ev:
        a = a * (2.0 ** ev)
    c = p.get("contrast", 0.0)
    if c:
        a = (a - 0.5) * (1.0 + c) + 0.5
    lum = (a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114)[..., None]
    lum_c = np.clip(lum, 0, 1)
    sh, hl = p.get("shadows", 0.0), p.get("highlights", 0.0)
    if sh:
        a = a + sh * 0.4 * (1.0 - lum_c) ** 2
    if hl:
        a = a + hl * 0.4 * lum_c ** 2
    sat = p.get("saturation", 0.0)
    if sat:
        gray = (a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114)[..., None]
        a = gray + (a - gray) * (1.0 + sat)
    t = p.get("temperature", 0.0)
    if t:
        a[..., 0] *= 1.0 + 0.25 * t
        a[..., 2] *= 1.0 - 0.25 * t
    tint = p.get("tint", 0.0)
    if tint:
        a[..., 1] *= 1.0 - 0.2 * tint
    sep = p.get("sepia", 0.0)
    if sep:
        m = np.array([[0.393, 0.769, 0.189], [0.349, 0.686, 0.168], [0.272, 0.534, 0.131]],
                     dtype=np.float32)
        a = a * (1 - sep) + np.clip(a @ m.T, 0, 1) * sep
    out = Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8), "RGB")
    shp = p.get("sharpness", 0.0)
    if shp:
        out = out.filter(ImageFilter.UnsharpMask(radius=2, percent=int(shp * 120), threshold=2))
    if alpha is not None:
        out = out.convert("RGBA")
        out.putalpha(alpha)
    return out


def _ellipse(im):
    w, h = im.size
    ss = 4
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, w * ss - 1, h * ss - 1), fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    im = im.convert("RGBA")
    im.putalpha(ImageChops.multiply(im.getchannel("A"), mask))
    return im


def _composite_sig(im, ov):
    from .signature import signature_png_bytes
    w = max(2, round(ov["w"]))
    h = max(1, round(ov["h"]))
    sig = Image.open(io.BytesIO(signature_png_bytes(ov["sig"], min(max(w, 64), 4000))))
    sig = sig.convert("RGBA").resize((w, h), Image.LANCZOS)
    base = im.convert("RGBA")
    base.alpha_composite(sig, (round(ov["cx"] - w / 2), round(ov["cy"] - h / 2)))
    return base


def _composite_markup(im, items):
    import cairo
    from .markup import Item, render_items
    w, h = im.size
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    render_items(cr, [Item.from_dict(d) for d in items])
    surf.flush()
    stride = surf.get_stride()
    buf = np.frombuffer(surf.get_data(), dtype=np.uint8).reshape(h, stride // 4, 4)[:, :w]
    a = buf[..., 3:4].astype(np.float32)
    rgb = buf[..., 2::-1].astype(np.float32)            # BGRA (premultiplied) -> RGB
    rgb = np.where(a > 0, np.clip(rgb * 255.0 / np.maximum(a, 1), 0, 255), 0)
    layer = Image.fromarray(np.concatenate([rgb, a], axis=2).astype(np.uint8), "RGBA")
    had_alpha = im.mode == "RGBA"
    base = im.convert("RGBA")
    base.alpha_composite(layer)
    return base if had_alpha else base.convert("RGB")


def apply_op(im, op):
    k = op[0]
    if k == "rotate":
        return im.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                             270: Image.Transpose.ROTATE_90}[op[1] % 360])
    if k == "flip":
        return im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if k == "resize":
        return im.resize((max(1, op[1]), max(1, op[2])), Image.LANCZOS)
    if k == "crop":
        x0, y0, x1, y1 = op[1:]
        return im.crop((x0, y0, x1, y1))
    if k == "ellipse":
        x0, y0, x1, y1 = op[1:]
        return _ellipse(im.crop((x0, y0, x1, y1)))
    if k == "alpha":
        return _alpha_fill(im, op[1], op[2], op[3])
    if k == "color":
        return adjust_color(im, op[1])
    if k == "sig":
        return _composite_sig(im, op[1])
    if k == "markup":
        return _composite_markup(im, op[1])
    return im


def apply_ops(im, ops):
    for op in ops:
        im = apply_op(im, op)
    return im


def ops_need_alpha(ops):
    return any(op[0] in ("alpha", "ellipse") for op in ops)


# ---------------------------------------------------------------- saving ---

def save_pil(im, exif, icc, path, fmt=None, quality=92):
    """Write `im` to `path` atomically. fmt defaults from the extension."""
    ext = Path(path).suffix.lower()
    fmt = fmt or WRITABLE.get(ext, "PNG")
    kw = {}
    if fmt == "JPEG":
        if im.mode not in ("L", "RGB"):
            if "A" in im.mode:
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                im = bg
            else:
                im = im.convert("RGB")
        kw = {"quality": quality, "optimize": True}
    elif fmt == "WEBP":
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.mode else "RGB")
        kw = {"quality": quality}
    elif fmt == "BMP" and im.mode not in ("RGB", "L", "RGBA"):
        im = im.convert("RGB")
    elif fmt == "TIFF" and im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGB")
    if exif is not None and fmt in ("JPEG", "PNG", "WEBP", "TIFF") and len(exif):
        kw["exif"] = exif
    if icc and fmt in ("JPEG", "PNG", "WEBP", "TIFF"):
        kw["icc_profile"] = icc
    tmp = f"{path}.part-{os.getpid()}"
    try:
        try:
            im.save(tmp, fmt, **kw)
        except Exception:  # noqa: BLE001 - retry without optional metadata
            kw.pop("exif", None)
            kw.pop("icc_profile", None)
            im.save(tmp, fmt, **kw)
        replace_file(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


PIXBUF_SAVE = {".avif": "avif"}


def save_pixbuf(im, path, fmt, quality=85):
    """Save through GdkPixbuf (AVIF, which Pillow lacks here)."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
    im = im.convert("RGBA" if "A" in im.mode else "RGB")
    w, h = im.size
    pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(im.tobytes()),
                                         GdkPixbuf.Colorspace.RGB, im.mode == "RGBA", 8,
                                         w, h, w * len(im.mode))
    tmp = f"{path}.part-{os.getpid()}"
    try:
        try:
            pb.savev(tmp, fmt, ["quality"], [str(int(quality))])
        except Exception:  # noqa: BLE001 - saver without a quality option
            pb.savev(tmp, fmt, [], [])
        replace_file(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_any(im, exif, icc, path, quality=92):
    """Save by extension: Pillow formats, AVIF via GdkPixbuf."""
    ext = Path(path).suffix.lower()
    if ext in PIXBUF_SAVE:
        save_pixbuf(im, path, PIXBUF_SAVE[ext], quality)
    else:
        save_pil(im, exif, icc, path, None, quality)


def save_avif(im, path, quality=85):
    save_pixbuf(im, path, "avif", quality)


# ------------------------------------------------------------ animation ---

ANIM_EXTS = {".gif", ".webp", ".png", ".apng"}      # containers that can be animated
_ORIENT = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
           4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
           6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
           8: Image.Transpose.ROTATE_90}


class Cancelled(Exception):
    """Raised inside a save when its cancel event is set."""


def _webp_lossless(path):
    """True if the first frame of a WebP file is VP8L (lossless)."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(12)
            if hdr[:4] != b"RIFF" or hdr[8:12] != b"WEBP":
                return False
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    return False
                tag, ln = ch[:4], struct.unpack("<I", ch[4:])[0]
                if tag == b"ANMF":
                    sub = f.read(24)[16:20]
                    return sub == b"VP8L"
                if tag in (b"VP8L", b"VP8 "):
                    return tag == b"VP8L"
                f.seek(ln + (ln & 1), 1)
    except Exception:  # noqa: BLE001
        return False


def anim_probe(path):
    """{'n', 'loop', 'lossless', 'fmt'} for an animated GIF/WebP/APNG file, else None.
    `loop` is None when the file carries no loop extension (GIF: play once)."""
    try:
        if Path(path).suffix.lower() not in ANIM_EXTS:
            return None
        with Image.open(path) as im:
            n = getattr(im, "n_frames", 1)
            if not getattr(im, "is_animated", False) or n < 2:
                return None
            return {"n": n, "loop": im.info.get("loop"), "fmt": im.format,
                    "lossless": im.format == "WEBP" and _webp_lossless(path)}
    except Exception:  # noqa: BLE001
        return None


def anim_dest_ok(src_ext, dest_ext):
    """Whether saving an animation from `src_ext` to `dest_ext` keeps it animated."""
    dest_ext = dest_ext.lower()
    if dest_ext in (".gif", ".webp", ".apng"):
        return True
    return dest_ext == ".png" and src_ext.lower() in (".png", ".apng")


def iter_frames(path):
    """Yield (RGBA frame, duration_ms) for every frame, fully composited and upright.
    Streams: only one decoded frame is alive at a time."""
    with Image.open(path) as im:
        n = getattr(im, "n_frames", 1)
        orient = im.getexif().get(0x0112, 1)
        for i in range(n):
            im.seek(i)
            im.load()               # WebP: info["duration"] is only current after load()
            dur = int(im.info.get("duration") or 0)
            fr = im.convert("RGBA")
            if orient in _ORIENT:
                fr = fr.transpose(_ORIENT[orient])
            yield fr, dur


def edited_frames(path, ops, max_px=0):
    """iter_frames() with the op pipeline (and an optional longest-side limit) applied."""
    for fr, dur in iter_frames(path):
        fr = apply_ops(fr, ops)
        if max_px and max(fr.size) > max_px:
            s = max_px / max(fr.size)
            fr = fr.resize((max(1, round(fr.width * s)), max(1, round(fr.height * s))),
                           Image.LANCZOS, reducing_gap=3.0)
        yield fr, dur


def _opaque(fr):
    return fr.mode != "RGBA" or fr.getchannel("A").getextrema()[0] >= 255


def _gif_frame(fr):
    """RGBA -> palette image; fully transparent pixels use palette entry 255."""
    try:
        method = Image.Quantize.LIBIMAGEQUANT if _has_liq() else Image.Quantize.MEDIANCUT
    except Exception:  # noqa: BLE001
        method = Image.Quantize.MEDIANCUT
    if _opaque(fr):
        return fr.convert("RGB").quantize(256, method=method), False
    a = fr.getchannel("A")
    p = fr.convert("RGB").quantize(255, method=method)
    pal = (p.getpalette() or [])[:765]
    p.putpalette(pal + [0] * (768 - len(pal)))
    p.paste(255, mask=a.point(lambda v: 255 if v < 128 else 0))
    p.info["transparency"] = 255
    return p, True


def _has_liq():
    from PIL import features
    return bool(features.check("libimagequant"))


def _png_chunk(typ, payload):
    return (struct.pack(">I", len(payload)) + typ + payload +
            struct.pack(">I", zlib.crc32(typ + payload) & 0xFFFFFFFF))


def _write_apng(fp, frames, n, loop, exif=None, icc=None, step=None):
    """Stream `frames` (RGBA, duration) into an animated PNG, one frame at a time."""
    seq = 0
    count = 0
    for fr, dur in frames:
        buf = io.BytesIO()
        kw = {"compress_level": 6}
        if count == 0:
            if icc:
                kw["icc_profile"] = icc
            if exif is not None and len(exif):
                kw["exif"] = exif
        fr.convert("RGBA").save(buf, "PNG", **kw)
        data = buf.getvalue()
        chunks, pos = [], 8
        while pos < len(data):
            ln = struct.unpack(">I", data[pos:pos + 4])[0]
            chunks.append((data[pos + 4:pos + 8], data[pos + 8:pos + 8 + ln]))
            pos += 12 + ln
        w, h = fr.size
        delay = min(65535, max(1, dur if dur > 0 else 100))
        fctl = struct.pack(">IIIIIHHBB", seq, w, h, 0, 0, delay, 1000, 0, 0)
        seq += 1
        if count == 0:
            fp.write(b"\x89PNG\r\n\x1a\n")
            for typ, pl in chunks:
                if typ == b"IHDR":
                    fp.write(_png_chunk(typ, pl))
            for typ, pl in chunks:
                if typ not in (b"IHDR", b"IDAT", b"IEND"):
                    fp.write(_png_chunk(typ, pl))
            fp.write(_png_chunk(b"acTL", struct.pack(">II", n, loop or 0)))
            fp.write(_png_chunk(b"fcTL", fctl))
            for typ, pl in chunks:
                if typ == b"IDAT":
                    fp.write(_png_chunk(typ, pl))
        else:
            fp.write(_png_chunk(b"fcTL", fctl))
            for typ, pl in chunks:
                if typ == b"IDAT":
                    fp.write(_png_chunk(b"fdAT", struct.pack(">I", seq) + pl))
                    seq += 1
        count += 1
        if step:
            step(count)
    if count != n:      # acTL promised n frames
        fp.seek(0)
        raise OSError(f"animation changed while saving ({count} of {n} frames)")
    fp.write(_png_chunk(b"IEND", b""))


def _write_webp(fp, frames, loop, lossless, quality, exif=None, icc=None, step=None):
    """Stream frames through libwebp's animation encoder (Pillow's own save_all
    would materialise every frame first)."""
    from PIL import _webp
    enc = None
    ts = 0
    count = 0
    kmin, kmax = (9, 17) if lossless else (3, 5)
    for fr, dur in frames:
        if enc is None:
            enc = _webp.WebPAnimEncoder(fr.size, 0, loop or 0, False, kmin, kmax, False, False)
        frame = fr if not _opaque(fr) else fr.convert("RGB")
        enc.add(frame.getim(), round(ts), lossless, quality, 100, 0)
        ts += dur if dur > 0 else 100
        count += 1
        if step:
            step(count)
    if enc is None:
        raise OSError("no frames")
    enc.add(None, round(ts), lossless, quality, 100, 0)
    if isinstance(exif, Image.Exif):
        exif = exif.tobytes()
    data = enc.assemble(icc or b"", exif or b"", b"")
    if data is None:
        raise OSError("cannot write file as WebP (encoder returned None)")
    fp.write(data)


def _write_gif(path, frames, loop, step=None):
    durations, disposal = [], []

    def gen():
        for fr, dur in frames:
            p, alpha = _gif_frame(fr)
            durations.append(dur)
            disposal.append(2 if alpha else 1)
            yield p
    it = gen()
    first = next(it)
    kw = {"save_all": True, "duration": durations, "disposal": disposal,
          "optimize": False}
    if loop is not None:
        kw["loop"] = loop
    if step:
        def counted(g):
            for k, f in enumerate(g, 2):
                step(k)
                yield f
        kw["append_images"] = counted(it)
        step(1)
    else:
        kw["append_images"] = it
    first.save(path, "GIF", **kw)


def save_animated(src, ops, dest, quality=None, max_px=0, progress=None, cancel=None):
    """Write every frame of the animation `src` with `ops` applied to `dest`
    (.gif / .webp / .apng / .png), streaming frame by frame. Keeps per-frame
    durations and the loop count. progress(done, total); `cancel` is an Event."""
    info = anim_probe(src)
    if info is None:
        raise ValueError("not an animation")
    ext = Path(dest).suffix.lower()
    n = info["n"]
    exif = icc = None
    try:
        with Image.open(src) as im:
            icc = im.info.get("icc_profile")
            exif = im.getexif()
            if exif.get(0x0112, 1) != 1:
                exif = None
    except Exception:  # noqa: BLE001
        pass

    def step(k):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if progress:
            progress(k, n)
    frames = edited_frames(src, ops, max_px)
    loop = info["loop"]
    tmp = f"{dest}.part-{os.getpid()}"
    try:
        if ext == ".gif":
            if loop is None and info["fmt"] != "GIF":
                loop = 0
            _write_gif(tmp, frames, loop, step)
        elif ext == ".webp":
            lossless = bool(info["lossless"]) if quality is None else quality >= 100
            q = 90 if quality is None else min(100, max(1, quality))
            with open(tmp, "wb") as fp:
                _write_webp(fp, frames, loop, lossless, q, exif, icc, step)
        else:
            with open(tmp, "wb") as fp:
                _write_apng(fp, frames, n, loop, exif, icc, step)
        replace_file(tmp, dest)
    finally:
        frames.close()
        if os.path.exists(tmp):
            os.unlink(tmp)
    return n


# ----------------------------------------------------------- metadata ---

def _fnum(v):
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def _dms(v, ref):
    try:
        d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
    except Exception:  # noqa: BLE001
        return None
    if str(ref).upper() in ("S", "W"):
        d = -d
    return d


def _xp_text(v):
    if isinstance(v, bytes):
        try:
            return v.decode("utf-16le").rstrip("\x00")
        except Exception:  # noqa: BLE001
            return None
    return str(v) if v else None


_MODEL = {"RGB": "RGB", "RGBA": "RGB", "L": "Gray", "LA": "Gray", "1": "Gray", "P": "Indexed",
          "PA": "Indexed", "CMYK": "CMYK", "YCbCr": "YCbCr", "LAB": "Lab", "HSV": "HSV"}


def _anim_duration(path, n, budget=0.6):
    """Total play time in ms (None if reading every frame takes too long)."""
    t0 = time.monotonic()
    total = 0
    try:
        with Image.open(path) as im:
            for i in range(n):
                im.seek(i)
                im.load()
                total += int(im.info.get("duration") or 0)
                if time.monotonic() - t0 > budget:
                    return None
    except Exception:  # noqa: BLE001
        return None
    return total


def image_info_sections(path, size=None):
    """Sections (title, rows) describing an image file (no File section)."""
    img_rows, cam_rows, gps_rows, desc_rows = [], [], [], []
    if size:
        img_rows.append(("Dimensions", f"{size[0]} × {size[1]} px"))
    if not path:
        return [("Image", img_rows)] if img_rows else []
    try:
        with Image.open(path) as im:
            fmt = im.format
            mode = im.mode
            info = dict(im.info)
            frames = getattr(im, "n_frames", 1)
            ex = im.getexif()
            try:
                exif_ifd = ex.get_ifd(0x8769)
                gps = ex.get_ifd(0x8825)
            except Exception:  # noqa: BLE001
                exif_ifd, gps = {}, {}
            iptc = None
            try:
                from PIL import IptcImagePlugin
                iptc = IptcImagePlugin.getiptcinfo(im)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 - not readable by Pillow (HEIC, SVG...)
        return [("Image", img_rows)] if img_rows else []

    if fmt:
        img_rows.append(("Format", fmt))
    dpi = info.get("dpi")
    if not dpi and ex.get(0x011A):
        dpi = (ex.get(0x011A), ex.get(0x011B) or ex.get(0x011A))
    if dpi:
        try:
            img_rows.append(("Resolution", f"{float(dpi[0]):.0f} × {float(dpi[1]):.0f} dpi"))
        except Exception:  # noqa: BLE001
            pass
    img_rows.append(("Color model", _MODEL.get(mode, mode)))
    depth = 1 if mode == "1" else 16 if mode.startswith("I;16") else 32 if mode in ("I", "F") else 8
    img_rows.append(("Bit depth", f"{depth}"))
    img_rows.append(("Alpha channel", "Yes" if ("A" in mode or "transparency" in info) else "No"))
    icc = info.get("icc_profile")
    prof = "None embedded"
    if icc:
        try:
            from PIL import ImageCms
            prof = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
        except Exception:  # noqa: BLE001
            prof = "Embedded profile"
    img_rows.append(("Color profile", prof))
    if frames and frames > 1:
        img_rows.append(("Frames", f"{frames} (animated)"))
        total = _anim_duration(path, frames)
        if total is not None:
            img_rows.append(("Duration", f"{total / 1000:.2f} s"))
        loop = info.get("loop")
        if fmt == "GIF" and loop is None:
            lp = "Once"
        elif not loop:
            lp = "Forever"
        else:
            lp = f"{loop} time{'s' if loop != 1 else ''}"
        img_rows.append(("Loop", lp))
    orient = ex.get(0x0112)
    if orient and orient != 1:
        img_rows.append(("EXIF orientation", str(orient)))

    def put(rows, label, val):
        if val not in (None, ""):
            rows.append((label, str(val)))

    put(cam_rows, "Make", (ex.get(0x010F) or "").strip() if ex.get(0x010F) else None)
    put(cam_rows, "Model", (ex.get(0x0110) or "").strip() if ex.get(0x0110) else None)
    lens = exif_ifd.get(0xA434)
    put(cam_rows, "Lens", str(lens).strip() if lens else None)
    fl = _fnum(exif_ifd.get(0x920A))
    if fl:
        s = f"{fl:g} mm"
        f35 = exif_ifd.get(0xA405)
        if f35:
            s += f" ({f35} mm equiv.)"
        cam_rows.append(("Focal length", s))
    fn = _fnum(exif_ifd.get(0x829D))
    if fn:
        cam_rows.append(("Aperture", f"f/{fn:.1f}".replace(".0", "")))
    et = _fnum(exif_ifd.get(0x829A))
    if et:
        cam_rows.append(("Exposure", f"1/{round(1 / et)} s" if et < 1 else f"{et:g} s"))
    iso = exif_ifd.get(0x8827)
    if iso:
        put(cam_rows, "ISO", iso if not isinstance(iso, tuple) else iso[0])
    bias = _fnum(exif_ifd.get(0x9204))
    if bias is not None and exif_ifd.get(0x9204) is not None:
        cam_rows.append(("Exposure bias", f"{bias:+.1f} EV"))
    fl_v = exif_ifd.get(0x9209)
    if fl_v is not None:
        cam_rows.append(("Flash", "Fired" if int(fl_v) & 1 else "Did not fire"))
    put(cam_rows, "Date taken", exif_ifd.get(0x9003) or ex.get(0x0132))
    put(cam_rows, "Software", ex.get(0x0131))
    put(cam_rows, "Artist", ex.get(0x013B))
    put(cam_rows, "Copyright", ex.get(0x8298))

    lat = _dms(gps.get(2), gps.get(1)) if gps and gps.get(2) else None
    lon = _dms(gps.get(4), gps.get(3)) if gps and gps.get(4) else None
    if lat is not None and lon is not None:
        gps_rows.append(("Coordinates", f"{lat:.6f}, {lon:.6f}"))
        alt = _fnum(gps.get(6))
        if alt is not None:
            gps_rows.append(("Altitude", f"{alt:.0f} m"))
        gps_rows.append(("Map", f"[Open in Maps](https://www.openstreetmap.org/?mlat={lat:.6f}"
                                f"&mlon={lon:.6f}#map=15/{lat:.6f}/{lon:.6f})"))

    put(desc_rows, "Title", _xp_text(ex.get(0x9C9B)))
    put(desc_rows, "Description", ex.get(0x010E) or _xp_text(ex.get(0x9C9C)))
    kw = _xp_text(ex.get(0x9C9E))
    if not kw and iptc and iptc.get((2, 25)):
        v = iptc[(2, 25)]
        kw = ", ".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)
                       for x in (v if isinstance(v, list) else [v]))
    put(desc_rows, "Keywords", kw)
    if iptc and iptc.get((2, 120)) and not desc_rows:
        v = iptc[(2, 120)]
        put(desc_rows, "Caption", v.decode("utf-8", "replace") if isinstance(v, bytes) else v)

    out = [("Image", img_rows)]
    for title, rows in (("Camera", cam_rows), ("GPS", gps_rows), ("Description", desc_rows)):
        if rows:
            out.append((title, rows))
    return out
