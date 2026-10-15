"""PDF document model + save/export pipeline for the PDF viewer (no widgets).

Coordinate frame for markup ("display frame"): the page as Poppler shows it
(crop box, /Rotate applied), then turned by the user's extra rotation, in PDF
points, origin top-left, y down. Everything the user sees and drags lives in
this frame; the save code maps it back into the page's default user space.

On save:
  * shapes, ink, text boxes and signatures are flattened into the page as
    VECTOR content (an overlay drawn with cairo's PDFSurface, merged with
    pypdf) -- the page's /Rotate and content stream are left untouched; the
    overlay is drawn pre-rotated instead, so links/annotations keep working;
  * notes become real /Text annotations, highlight/underline/strikethrough
    become real /Highlight, /Underline, /StrikeOut annotations;
  * edited AcroForm values are written into the fields (/V, /AS) and the
    form is flagged /NeedAppearances.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field

import cairo
import gi

gi.require_version("Poppler", "0.18")
from gi.repository import GLib, Poppler

from . import markup
from .markup import ANNOT_KINDS, Item, TEXTMARK_KINDS
from .runtime import replace_file


# ---------------------------------------------------------------- model ---

@dataclass
class Source:
    """A PDF that pages come from (an opened file, a dropped file, a converted
    image, a blank page)."""
    name: str
    path: str | None = None       # local file
    data: bytes | None = None     # in-memory PDF
    password: str | None = None


@dataclass
class PageRef:
    src: int                      # index into the viewer's sources
    index: int                    # page index inside that source
    rotation: int = 0             # user rotation, clockwise, multiple of 90
    items: list = field(default_factory=list)     # markup.Item, display frame
    forms: dict = field(default_factory=dict)     # field name -> str | bool

    def copy(self) -> "PageRef":
        return PageRef(self.src, self.index, self.rotation,
                       [i.copy() for i in self.items], dict(self.forms))

    def key(self):
        return (self.src, self.index, self.rotation,
                tuple(i.fingerprint() for i in self.items),
                tuple(sorted(self.forms.items())))


rotate_point = markup.rotate_point


def poppler_open(src: Source):
    if src.data is not None:
        return Poppler.Document.new_from_bytes(GLib.Bytes.new(src.data), src.password)
    return Poppler.Document.new_from_file(GLib.filename_to_uri(src.path), src.password)


def apply_forms(page, forms: dict, originals: dict | None = None):
    """Set form values on a Poppler page (its document is modified).
    `originals` (name -> value) is filled with the values before the change."""
    for m in page.get_form_field_mapping():
        f = m.field
        name = f.get_name()
        if name is None:
            continue
        t = f.get_field_type()
        if originals is not None and name not in originals:
            originals[name] = form_value(f)
        if name not in forms:
            continue
        v = forms[name]
        try:
            if t == Poppler.FormFieldType.TEXT:
                f.text_set_text(str(v))
            elif t == Poppler.FormFieldType.BUTTON:
                f.button_set_state(bool(v))
            elif t == Poppler.FormFieldType.CHOICE:
                f.choice_unselect_all()
                for i in range(f.choice_get_n_items()):
                    if f.choice_get_item(i) == v:
                        f.choice_select_item(i)
                if f.choice_is_editable():
                    f.choice_set_text(str(v))
        except Exception:
            pass


def form_value(f):
    t = f.get_field_type()
    if t == Poppler.FormFieldType.TEXT:
        return f.text_get_text() or ""
    if t == Poppler.FormFieldType.BUTTON:
        return bool(f.button_get_state())
    if t == Poppler.FormFieldType.CHOICE:
        for i in range(f.choice_get_n_items()):
            if f.choice_is_item_selected(i):
                return f.choice_get_item(i)
        return f.choice_get_text() or "" if f.choice_is_editable() else ""
    return None


# ------------------------------------------------------------- geometry ---

def display_to_overlay_matrix(rotation, wu, hu) -> cairo.Matrix:
    """Matrix mapping display-frame points (u, v) to cairo coordinates (X, Y)
    of an overlay covering the page's visible box (wu × hu in unrotated user
    space, top-left origin) on a page with total /Rotate `rotation`."""
    r = rotation % 360
    if r == 0:
        return cairo.Matrix(1, 0, 0, 1, 0, 0)
    if r == 90:     # X = v, Y = hu - u
        return cairo.Matrix(0, -1, 1, 0, 0, hu)
    if r == 180:    # X = wu - u, Y = hu - v
        return cairo.Matrix(-1, 0, 0, -1, wu, hu)
    return cairo.Matrix(0, 1, -1, 0, wu, 0)        # 270: X = wu - v, Y = u


def _visible_box(page):
    """Crop box clipped to the media box, like Poppler does."""
    mb, cb = page.mediabox, page.cropbox
    l = max(float(mb.left), float(cb.left))
    b = max(float(mb.bottom), float(cb.bottom))
    r = min(float(mb.right), float(cb.right))
    t = min(float(mb.top), float(cb.top))
    if r <= l or t <= b:
        l, b, r, t = float(mb.left), float(mb.bottom), float(mb.right), float(mb.top)
    return l, b, r, t


class PageFrame:
    """Maps the display frame of a pypdf page (after its final /Rotate is set)
    to default user space."""

    def __init__(self, page):
        self.l, self.b, self.r, self.t = _visible_box(page)
        self.wu, self.hu = self.r - self.l, self.t - self.b
        self.rot = int(page.rotation or 0) % 360
        self.m = display_to_overlay_matrix(self.rot, self.wu, self.hu)

    def to_user(self, u, v):
        X, Y = self.m.transform_point(u, v)
        return self.l + X, self.t - Y


# ------------------------------------------------------------ annotations ---

def _pdf_date():
    tz = time.strftime("%z")
    return "D:" + time.strftime("%Y%m%d%H%M%S") + (tz[:3] + "'" + tz[3:] + "'" if tz else "")


def _author():
    try:
        return GLib.get_real_name() or GLib.get_user_name()
    except Exception:
        return ""


def _annotation(it: Item, frame: PageFrame):
    from pypdf.generic import (ArrayObject, DictionaryObject, FloatObject, NameObject,
                               NumberObject, TextStringObject, BooleanObject)

    def arr(vals):
        return ArrayObject([FloatObject(round(v, 3)) for v in vals])

    color = arr(it.stroke[:3])
    d = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/T"): TextStringObject(_author()),
        NameObject("/M"): TextStringObject(_pdf_date()),
        NameObject("/NM"): TextStringObject(str(uuid.uuid4())),
        NameObject("/C"): color,
    })
    if it.kind == "note":
        pts = [frame.to_user(it.x, it.y), frame.to_user(it.x + it.w, it.y + it.h)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        d.update({
            NameObject("/Subtype"): NameObject("/Text"),
            NameObject("/Rect"): arr([min(xs), min(ys), max(xs), max(ys)]),
            NameObject("/Contents"): TextStringObject(it.text or ""),
            NameObject("/Name"): NameObject("/Comment"),
            NameObject("/Open"): BooleanObject(False),
            NameObject("/F"): NumberObject(4 | 8 | 16),     # print, no-zoom, no-rotate
        })
        return d
    sub = {"highlight": "/Highlight", "underline": "/Underline", "strike": "/StrikeOut"}[it.kind]
    quad, xs, ys = [], [], []
    for x, y, w, h in it.points:
        for u, v in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):   # TL TR BL BR
            ux, uy = frame.to_user(u, v)
            quad += [ux, uy]
            xs.append(ux)
            ys.append(uy)
    d.update({
        NameObject("/Subtype"): NameObject(sub),
        NameObject("/Rect"): arr([min(xs), min(ys), max(xs), max(ys)]),
        NameObject("/QuadPoints"): arr(quad),
        NameObject("/F"): NumberObject(4),
    })
    if it.text:
        d[NameObject("/Contents")] = TextStringObject(it.text)
    return d


def _add_annots(writer, page, annots):
    from pypdf.generic import ArrayObject, NameObject
    if "/Annots" in page:
        arr = page["/Annots"].get_object()
    else:
        arr = ArrayObject()
        page[NameObject("/Annots")] = arr
    for a in annots:
        a[NameObject("/P")] = page.indirect_reference
        arr.append(writer._add_object(a))


# ------------------------------------------------------------------ forms ---

def _field_name(obj):
    parts = []
    o = obj
    seen = 0
    while o is not None and seen < 32:
        if "/T" in o:
            parts.append(str(o["/T"]))
        o = o.get("/Parent")
        o = o.get_object() if o is not None else None
        seen += 1
    return ".".join(reversed(parts))


def _write_forms(writer, page, forms):
    """Write edited values into the page's widget annotations. Returns the
    terminal field objects touched."""
    from pypdf.generic import NameObject, TextStringObject
    touched = []
    if "/Annots" not in page:
        return touched
    for ref in page["/Annots"].get_object():
        w = ref.get_object()
        if w.get("/Subtype") != "/Widget":
            continue
        name = _field_name(w)
        if name not in forms:
            continue
        fld = w if "/T" in w else (w.get("/Parent").get_object() if "/Parent" in w else w)
        ft = fld.get("/FT") or w.get("/FT")
        v = forms[name]
        if ft == "/Btn":
            on = "/Yes"
            ap = w.get("/AP")
            if ap is not None:
                n = ap.get_object().get("/N")
                if n is not None:
                    keys = [k for k in n.get_object().keys() if k != "/Off"]
                    if keys:
                        on = keys[0]
            state = NameObject(on if v else "/Off")
            fld[NameObject("/V")] = state
            w[NameObject("/AS")] = state
        else:
            fld[NameObject("/V")] = TextStringObject(str(v))
            if "/AP" in w:
                del w["/AP"]
        touched.append(fld)
    return touched


def _ensure_acroform(writer, readers, fields):
    from pypdf.generic import ArrayObject, BooleanObject, DictionaryObject, NameObject
    root = writer._root_object
    if "/AcroForm" in root:
        af = root["/AcroForm"].get_object()
    else:
        af = DictionaryObject()
        for r in readers:
            src = r.trailer["/Root"].get("/AcroForm")
            if src is not None:
                src = src.get_object()
                for k in ("/DR", "/DA"):
                    if k in src:
                        af[NameObject(k)] = src[k].clone(writer) if hasattr(src[k], "clone") else src[k]
                break
        af[NameObject("/Fields")] = ArrayObject()
        root[NameObject("/AcroForm")] = writer._add_object(af)
    flds = af.get("/Fields")
    if flds is not None:
        flds = flds.get_object()
        have = {id(f.get_object()) for f in flds}
        for f in fields:
            top = f
            while "/Parent" in top:
                top = top["/Parent"].get_object()
            if id(top) not in have and top.indirect_reference is not None:
                flds.append(top.indirect_reference)
                have.add(id(top))
    af[NameObject("/NeedAppearances")] = BooleanObject(True)


# ------------------------------------------------------------------- save ---

def open_reader(src: Source):
    """pypdf reader. For files we pass an open handle so pypdf reads lazily
    instead of slurping the whole file into memory."""
    from pypdf import PdfReader
    if src.data is None:
        fh = open(src.path, "rb")
        reader = PdfReader(fh)
        reader._preview_fh = fh
    else:
        reader = PdfReader(io.BytesIO(src.data))
    if reader.is_encrypted:
        if not reader.decrypt(src.password or ""):
            raise ValueError(f"Could not decrypt {src.name}")
    return reader


def _stamp(page, items):
    """Merge vector items onto a pypdf page (in place)."""
    from pypdf import PdfReader, Transformation
    fr = PageFrame(page)
    buf = io.BytesIO()
    surf = cairo.PDFSurface(buf, fr.wu, fr.hu)
    cr = cairo.Context(surf)
    cr.set_matrix(fr.m)
    for it in items:
        markup.render_item(cr, it)
    surf.finish()
    overlay = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
    page.merge_transformed_page(overlay, Transformation().translate(fr.l, fr.b), over=True)


def _add_text_layer(page, lines):
    """Invisible (render mode 3) text over a scanned page so it can be
    selected and searched. lines: [(poly 4 points TL,TR,BR,BL, text)] in the
    display frame. Fonts are embedded by cairo; we flip every BT to 3 Tr."""
    import math
    from pypdf import PdfReader, Transformation
    from pypdf.generic import NameObject
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo
    fr = PageFrame(page)
    buf = io.BytesIO()
    surf = cairo.PDFSurface(buf, fr.wu, fr.hu)
    cr = cairo.Context(surf)
    cr.set_matrix(fr.m)
    for poly, text in lines:
        if not text.strip() or len(poly) < 4:
            continue
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = poly[:4]
        width = math.hypot(x1 - x0, y1 - y0)
        height = math.hypot(x3 - x0, y3 - y0)
        if width < 1 or height < 1:
            continue
        layout = PangoCairo.create_layout(cr)
        fd = Pango.FontDescription.from_string("Sans")
        fd.set_absolute_size(height * 0.8 * Pango.SCALE)
        layout.set_font_description(fd)
        layout.set_text(text, -1)
        _ink, log = layout.get_extents()
        lw = max(log.width / Pango.SCALE, 1e-3)
        lh = max(log.height / Pango.SCALE, 1e-3)
        cr.save()
        cr.translate(x0, y0)
        cr.rotate(math.atan2(y1 - y0, x1 - x0))
        cr.scale(width / lw, height / lh)
        cr.move_to(0, 0)
        PangoCairo.show_layout(cr, layout)
        cr.restore()
    surf.finish()
    overlay = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
    cs = overlay.get_contents()
    if cs is None:
        return
    data = cs.get_data().replace(b"BT\n", b"BT\n3 Tr\n").replace(b"BT ", b"BT 3 Tr ")
    cs.set_data(data)
    overlay[NameObject("/Contents")] = cs
    page.merge_transformed_page(overlay, Transformation().translate(fr.l, fr.b), over=False)


def build_writer(sources, pages, base_src=0, metadata=None, text_layers=None):
    """A pypdf PdfWriter with the edited pages."""
    from pypdf import PdfWriter
    readers = {}

    def reader(i):
        if i not in readers:
            readers[i] = open_reader(sources[i])
        return readers[i]

    identity = (bool(pages) and all(p.src == base_src for p in pages)
                and [p.index for p in pages] == list(range(len(reader(base_src).pages))))
    if identity:
        # Same pages in the same order: clone so forms, outlines, metadata survive.
        writer = PdfWriter(clone_from=reader(base_src))
        out_pages = list(writer.pages)
    else:
        writer = PdfWriter()
        out_pages = [writer.add_page(reader(p.src).pages[p.index]) for p in pages]
        try:
            r0 = reader(base_src)
            if r0.metadata:
                writer.add_metadata({k: v for k, v in r0.metadata.items() if isinstance(k, str)})
        except Exception:
            pass

    form_fields = []
    for n, (ref, page) in enumerate(zip(pages, out_pages)):
        if ref.rotation % 360:
            page.rotate(ref.rotation % 360)
        flat = [i for i in ref.items if i.kind not in ANNOT_KINDS]
        notes = [i for i in ref.items if i.kind in ANNOT_KINDS]
        if flat:
            _stamp(page, flat)
        if notes:
            fr = PageFrame(page)
            _add_annots(writer, page, [_annotation(i, fr) for i in notes])
        if ref.forms:
            form_fields += _write_forms(writer, page, ref.forms)
        if text_layers and text_layers.get(n):
            _add_text_layer(page, text_layers[n])
    if form_fields:
        _ensure_acroform(writer, list(readers.values()), form_fields)
    if metadata:
        writer.add_metadata({k: v for k, v in metadata.items() if v is not None})
    writer._preview_readers = list(readers.values())
    return writer


def build_pdf(sources: list[Source], pages: list[PageRef], out_path: str,
              base_src: int = 0, keep_password: bool = True, encrypt: dict | None = None,
              metadata: dict | None = None, text_layers: dict | None = None):
    """Write the edited document to out_path (atomically). Thread-safe."""
    writer = build_writer(sources, pages, base_src, metadata, text_layers)
    try:
        _finish_write(writer, out_path, sources, base_src, keep_password, encrypt)
    finally:
        _close_readers(writer)


def _close_readers(writer):
    for r in getattr(writer, "_preview_readers", ()):
        fh = getattr(r, "_preview_fh", None)
        if fh is not None:
            fh.close()


def _finish_write(writer, out_path, sources, base_src, keep_password, encrypt):
    if encrypt:
        _encrypt(writer, encrypt)
    elif keep_password and sources[base_src].password:
        writer.encrypt(user_password=sources[base_src].password, owner_password=None,
                       algorithm="AES-256")
    write_atomic(out_path, writer.write)


def _encrypt(writer, e):
    from pypdf.constants import UserAccessPermissions as P
    perms = P.all()
    if not e.get("allow_print", True):
        perms &= ~(P.PRINT | P.PRINT_TO_REPRESENTATION)
    if not e.get("allow_copy", True):
        perms &= ~P.EXTRACT_TEXT_AND_GRAPHICS & ~P.EXTRACT
    if not e.get("allow_modify", True):
        perms &= ~P.MODIFY & ~P.ASSEMBLE_DOC
    if not e.get("allow_annotate", True):
        perms &= ~P.ADD_OR_MODIFY & ~P.FILL_FORM_FIELDS
    writer.encrypt(user_password=e.get("user") or "", owner_password=e.get("owner") or None,
                   permissions_flag=perms, algorithm="AES-256")


def write_atomic(out_path: str, write_fn):
    """Write via a temp file in the destination dir, then os.replace()."""
    d = os.path.dirname(os.path.abspath(out_path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".preview-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        try:
            st = os.stat(out_path)
            os.chmod(tmp, st.st_mode & 0o7777)
        except FileNotFoundError:
            os.chmod(tmp, 0o666 & ~_umask())
        replace_file(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _umask():
    m = os.umask(0)
    os.umask(m)
    return m


# ----------------------------------------------------------------- export ---

def render_page_surface(doc, ref: PageRef, scale, transparent=False):
    """cairo ImageSurface of a page (rotation, forms and markup applied)."""
    import math
    page = doc.get_page(ref.index)
    if ref.forms:
        apply_forms(page, ref.forms)
    w, h = page.get_size()
    dw, dh = (h, w) if ref.rotation % 180 else (w, h)
    pw, ph = max(1, round(dw * scale)), max(1, round(dh * scale))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
    cr = cairo.Context(surf)
    if not transparent:
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
    cr.scale(pw / dw, ph / dh)
    cr.save()
    r = ref.rotation % 360
    if r == 90:
        cr.translate(dw, 0)
        cr.rotate(math.pi / 2)
    elif r == 180:
        cr.translate(dw, dh)
        cr.rotate(math.pi)
    elif r == 270:
        cr.translate(0, dh)
        cr.rotate(-math.pi / 2)
    page.render_for_printing(cr)
    cr.restore()
    markup.render_items(cr, ref.items)
    surf.flush()
    return surf


def export_images(sources, pages, folder, stem, fmt="png", dpi=150, progress=None):
    """Render each page to <folder>/<stem>-NNN.<ext>. Returns the paths."""
    import numpy as np
    from PIL import Image
    docs = {}
    out = []
    ext = {"png": "png", "jpeg": "jpg", "tiff": "tif"}[fmt]
    digits = max(3, len(str(len(pages))))
    for n, ref in enumerate(pages):
        if ref.src not in docs:
            docs[ref.src] = poppler_open(sources[ref.src])
        surf = render_page_surface(docs[ref.src], ref, dpi / 72.0)
        a = np.frombuffer(surf.get_data(), np.uint8).reshape(
            surf.get_height(), surf.get_stride() // 4, 4)[:, :surf.get_width(), :]
        im = Image.fromarray(a[..., [2, 1, 0]].copy(), "RGB")
        path = os.path.join(folder, f"{stem}-{n + 1:0{digits}d}.{ext}")
        kw = {"dpi": (dpi, dpi)}
        if fmt == "jpeg":
            kw["quality"] = 90
        elif fmt == "tiff":
            kw["compression"] = "tiff_lzw"
        im.save(path, {"png": "PNG", "jpeg": "JPEG", "tiff": "TIFF"}[fmt], **kw)
        out.append(path)
        if progress:
            progress(n + 1, len(pages))
    return out


def ghostscript():
    return shutil.which("gs")


def reduce_size(in_path, out_path, quality="ebook"):
    """Re-distil with Ghostscript (images downsampled). Atomic."""
    gs = ghostscript()
    if not gs:
        raise RuntimeError("Ghostscript (gs) is not installed")
    d = os.path.dirname(os.path.abspath(out_path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".preview-", suffix=".part")
    os.close(fd)
    try:
        subprocess.run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
                        "-dCompatibilityLevel=1.6", f"-dPDFSETTINGS=/{quality}",
                        "-dDetectDuplicateImages=true", f"-sOutputFile={tmp}", in_path],
                       check=True, capture_output=True, timeout=600)
        os.chmod(tmp, 0o666 & ~_umask())
        replace_file(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def blank_pdf_bytes(w, h) -> bytes:
    buf = io.BytesIO()
    s = cairo.PDFSurface(buf, w, h)
    cairo.Context(s).show_page()
    s.finish()
    return buf.getvalue()


def image_to_pdf_bytes(path: str, fit_w: float = 612, fit_h: float = 792) -> bytes:
    """One-page PDF of an image, scaled down to fit fit_w × fit_h points."""
    import img2pdf
    from PIL import Image, ImageOps
    try:
        im = Image.open(path)
        im.load()
    except Exception:
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk
        tex = Gdk.Texture.new_from_filename(path)
        im = Image.open(io.BytesIO(tex.save_to_png_bytes().get_data()))
        im.load()
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode not in ("RGB", "L", "CMYK"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    if im.mode == "CMYK" or (path.lower().endswith((".jpg", ".jpeg")) and im.mode in ("RGB", "L")):
        im.save(buf, "JPEG", quality=92)
    else:
        im.save(buf, "PNG")
    iw, ih = im.size
    nat_w, nat_h = iw * 72 / 96, ih * 72 / 96
    s = min(1.0, fit_w / nat_w, fit_h / nat_h)
    layout = img2pdf.get_layout_fun((nat_w * s, nat_h * s))
    return img2pdf.convert(buf.getvalue(), layout_fun=layout)


PAPER_SIZES = [  # name, width mm, height mm
    ("A3", 297, 420), ("A4", 210, 297), ("A5", 148, 210), ("A6", 105, 148),
    ("B4", 250, 353), ("B5", 176, 250), ("Letter", 215.9, 279.4),
    ("Legal", 215.9, 355.6), ("Tabloid", 279.4, 431.8), ("Executive", 184.15, 266.7),
]


def paper_name(w_pt, h_pt):
    wm, hm = w_pt * 25.4 / 72, h_pt * 25.4 / 72
    a, b = sorted((wm, hm))
    for name, pw, ph in PAPER_SIZES:
        if abs(a - pw) < 2 and abs(b - ph) < 2:
            return name + (" (landscape)" if wm > hm else "")
    return None
