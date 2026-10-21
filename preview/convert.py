"""Images -> PDF conversion (library, CLI and a GTK export dialog).

CLI:  view-app --combine-pdf -o out.pdf img1 img2 ...  (or python -m preview.convert)
"""
import argparse
import io
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import img2pdf
from PIL import Image, ImageOps
from pypdf import PdfWriter, PdfReader
from .runtime import replace_file
from .i18n import _, N_, ngettext

MM_TO_PT = 72.0 / 25.4
PAGE_SIZES = {  # points
    "a4": (595.276, 841.890),
    "letter": (612.0, 792.0),
}
# Formats that Pillow decodes reliably but img2pdf cannot embed directly.
_PIXBUF_FIRST_EXTS = {".heic", ".heif", ".avif", ".jxl", ".svg", ".qoi", ".tga", ".ico"}
_DEFAULT_DPI = 150.0     # used when a file carries no real resolution
_EXIF_ORIENTATION = 0x0112


class ConvertError(Exception):
    pass


# ----------------------------------------------------------------- utils ---

def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def unique_path(path):
    """Return `path`, or 'name 2.ext', 'name 3.ext' ... if it already exists."""
    p = Path(path)
    if not p.exists():
        return p
    n = 2
    while True:
        cand = p.with_name(f"{p.stem} {n}{p.suffix}")
        if not cand.exists():
            return cand
        n += 1


def default_output_path(paths):
    first = Path(paths[0])
    return unique_path(first.with_suffix(".pdf"))


# ------------------------------------------------------- image preparation ---

def _pil_to_png_bytes(im):
    im = ImageOps.exif_transpose(im)
    has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    if im.mode not in ("1", "L", "RGB", "RGBA"):
        im = im.convert("RGBA" if has_alpha else "RGB")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _pixbuf_to_png_bytes(path):
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    if Path(path).suffix.lower() == ".svg":
        info = GdkPixbuf.Pixbuf.get_file_info(str(path))
        w, h = (info[1], info[2]) if info[0] else (0, 0)
        big = 1600
        if w and h:
            s = max(1.0, big / max(w, h))
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), int(w * s), int(h * s))
        else:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), big, big)
    else:
        pb = GdkPixbuf.Pixbuf.new_from_file(str(path))
    pb = pb.apply_embedded_orientation() or pb
    ok, data = pb.save_to_bufferv("png", [], [])
    if not ok:
        raise ConvertError(f"Cannot encode {path}")
    return bytes(data)


def _to_png_bytes(path):
    """Decode any supported image to normalised, upright PNG bytes."""
    ext = Path(path).suffix.lower()
    errors = []
    order = ("pixbuf", "pil") if ext in _PIXBUF_FIRST_EXTS else ("pil", "pixbuf")
    for how in order:
        try:
            if how == "pil":
                with Image.open(path) as im:
                    im.load()
                    return _pil_to_png_bytes(im)
            return _pixbuf_to_png_bytes(path)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    raise ConvertError(f"Cannot read {Path(path).name}: {errors[-1]}")


def _prepare(path):
    """Return (data, is_direct): bytes/str for img2pdf. JPEG and plain PNG are
    embedded losslessly; everything else is normalised to PNG first."""
    ext = Path(path).suffix.lower()
    if ext in (".jpg", ".jpeg", ".jpe", ".png"):
        try:
            with Image.open(path) as im:
                fmt = im.format
                orient = im.getexif().get(_EXIF_ORIENTATION, 1)
                mode = im.mode
            if fmt == "JPEG" and orient in (1, 3, 6, 8) and mode in ("L", "RGB", "CMYK"):
                return str(path)
            if fmt == "PNG" and orient in (0, 1):
                return str(path)
        except Exception:  # noqa: BLE001
            pass
    return _to_png_bytes(path)


# ----------------------------------------------------------------- layout ---

def _make_layout(page_size, margin_mm, landscape_auto):
    margin = max(0.0, float(margin_mm)) * MM_TO_PT

    def dpi_of(ndpi):
        d = ndpi[0] if ndpi and ndpi[0] else _DEFAULT_DPI
        return _DEFAULT_DPI if abs(d - 96.0) < 0.5 or d <= 1 else d

    if page_size == "fit":
        def layout(wpx, hpx, ndpi):
            dx = dpi_of(ndpi)
            dy = ndpi[1] if ndpi and ndpi[1] and abs(ndpi[1] - 96.0) >= 0.5 else dx
            w, h = wpx * 72.0 / dx, hpx * 72.0 / dy
            return w + 2 * margin, h + 2 * margin, w, h
        return layout

    if page_size not in PAGE_SIZES:
        raise ValueError(f"Unknown page size {page_size!r}")
    pw, ph = PAGE_SIZES[page_size]

    def layout(wpx, hpx, ndpi):
        w_page, h_page = pw, ph
        if landscape_auto and wpx > hpx:
            w_page, h_page = ph, pw
        aw, ah = max(1.0, w_page - 2 * margin), max(1.0, h_page - 2 * margin)
        s = min(aw / wpx, ah / hpx)
        return w_page, h_page, wpx * s, hpx * s
    return layout


# ------------------------------------------------------------- conversion ---

def images_to_pdf(paths, out_path, page_size="fit", margin_mm=0, landscape_auto=True,
                  progress=None):
    """Combine images into one PDF (one page per image, in the given order).

    page_size: "fit" (page = image), "a4" or "letter". Overwrites out_path.
    progress(done, total) is called after each image. Returns the output Path.
    """
    paths = [str(p) for p in paths]
    if not paths:
        raise ConvertError("No images to convert")
    layout = _make_layout(page_size, margin_mm, landscape_auto)
    writer = PdfWriter()
    for i, p in enumerate(paths):
        data = _prepare(p)
        try:
            pdf = img2pdf.convert(data, layout_fun=layout,
                                  rotation=img2pdf.Rotation.ifvalid)
        except Exception:  # noqa: BLE001  (e.g. odd JPEG/PNG): normalise and retry
            if isinstance(data, str):
                data = _to_png_bytes(p)
                try:
                    pdf = img2pdf.convert(data, layout_fun=layout)
                except Exception as e:  # noqa: BLE001
                    raise ConvertError(f"Cannot convert {Path(p).name}: {e}") from e
            else:
                raise ConvertError(f"Cannot convert {Path(p).name}")
        writer.append(PdfReader(io.BytesIO(pdf)))
        if progress:
            progress(i + 1, len(paths))
    out_path = Path(out_path)
    tmp = out_path.with_name(out_path.name + ".part")
    try:
        with open(tmp, "wb") as f:
            writer.write(f)
        replace_file(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return out_path


# -------------------------------------------------------------------- CLI ---

def main(argv=None):
    ap = argparse.ArgumentParser(prog="view-app --combine-pdf",
                                 description=_("Combine images into a PDF."))
    ap.add_argument("images", nargs="+")
    ap.add_argument("-o", "--output", help="output PDF (default: next to the first image)")
    ap.add_argument("--page-size", choices=["fit", "a4", "letter"], default="fit")
    ap.add_argument("--margin", type=float, default=0.0, help="margin in mm")
    ap.add_argument("--no-landscape-auto", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite the output if it exists")
    a = ap.parse_args(argv)
    out = Path(a.output) if a.output else default_output_path(a.images)
    if out.exists() and not a.force:
        out = unique_path(out)
    try:
        images_to_pdf(a.images, out, a.page_size, a.margin, not a.no_landscape_auto,
                      progress=lambda d, t: print(f"[{d}/{t}]", file=sys.stderr))
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


# ---------------------------------------------------------- GTK export UI ---

def show_export_dialog(window, paths, cleanup=None, default_dir_from=None, parent=None):
    """Present the 'Export as PDF' dialog over `window`.

    `window` needs show_toast() and open_files(). `cleanup()` (optional) is
    called when the dialog is closed (to delete temp files).
    `default_dir_from`: path whose folder/name seed the default output (when
    `paths` are temp copies). `parent`: widget to present over (default window)."""
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk

    host = parent or window
    paths = [str(p) for p in paths]
    if not paths:
        window.show_toast(_("No images to export"))
        if cleanup:
            cleanup()
        return

    state = {"out": default_output_path([default_dir_from or paths[0]]), "explicit": False, "busy": False}

    dlg = Adw.Dialog(title=_("Export as PDF"), content_width=440)
    tv = Adw.ToolbarView()
    hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
    cancel = Gtk.Button(label=_("Cancel"))
    cancel.connect("clicked", lambda *_: dlg.close())
    export = Gtk.Button(label=_("Export"), css_classes=["suggested-action"])
    hb.pack_start(cancel)
    hb.pack_end(export)
    tv.add_top_bar(hb)

    page = Adw.PreferencesPage()
    group = Adw.PreferencesGroup(
        description=ngettext("{n} image, one per page", "{n} images, one per page",
                             len(paths)).format(n=len(paths)))
    page.add(group)

    sizes = [(N_("Fit to Image"), "fit"), ("A4", "a4"), (N_("US Letter"), "letter")]
    size_row = Adw.ComboRow(title=_("Page Size"), model=Gtk.StringList.new([_(s[0]) for s in sizes]))
    group.add(size_row)

    margin_row = Adw.SpinRow(title=_("Margin"), subtitle=_("Millimetres"),
                             adjustment=Gtk.Adjustment(value=0, lower=0, upper=50,
                                                       step_increment=1, page_increment=5))
    group.add(margin_row)

    out_row = Adw.ActionRow(title=_("Save To"), subtitle=str(state["out"]), subtitle_lines=2,
                            title_lines=1)
    choose = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                        tooltip_text=_("Choose…"), css_classes=["flat"])
    out_row.add_suffix(choose)
    group.add(out_row)

    progress = Gtk.ProgressBar(visible=False, margin_start=12, margin_end=12, margin_bottom=12)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    page.set_vexpand(True)
    box.append(page)
    box.append(progress)
    tv.set_content(box)
    dlg.set_child(tv)
    dlg.set_default_widget(export)

    def set_out(p):
        state["out"] = Path(p)
        out_row.set_subtitle(str(p))

    def on_choose(*_a):
        fd = Gtk.FileDialog(title=_("Save PDF"), initial_name=state["out"].name,
                            initial_folder=Gio.File.new_for_path(str(state["out"].parent)))
        f = Gtk.FileFilter(name=_("PDF documents"))
        f.add_suffix("pdf")
        fd.set_default_filter(f)

        def done(fd_, res):
            try:
                gf = fd_.save_finish(res)
            except GLib.Error:
                return
            p = gf.get_path()
            if p:
                if not p.lower().endswith(".pdf"):
                    p += ".pdf"
                set_out(p)
                state["explicit"] = True   # the dialog already confirmed overwriting

        root = host.get_root() if isinstance(host, Gtk.Widget) else None
        fd.save(root if isinstance(root, Gtk.Window) else None, None, done)

    choose.connect("clicked", on_choose)

    def set_busy(busy):
        state["busy"] = busy
        for w in (size_row, margin_row, choose, export, cancel):
            w.set_sensitive(not busy)
        progress.set_visible(busy)

    def on_export(*_a):
        if state["busy"]:
            return
        out = state["out"]
        if out.exists() and not state["explicit"]:
            out = unique_path(out)
            set_out(out)
        page_size = sizes[size_row.get_selected()][1]
        margin = margin_row.get_value()
        set_busy(True)
        progress.set_fraction(0)

        def prog(done, total):
            GLib.idle_add(progress.set_fraction, done / total)

        def work():
            try:
                images_to_pdf(paths, out, page_size, margin, True, progress=prog)
                err = None
            except Exception as e:  # noqa: BLE001
                err = e
            GLib.idle_add(finished, err)

        def finished(err):
            dlg.force_close()
            if err is not None:
                window.show_toast(_("PDF export failed: {error}").format(error=err))
            else:
                gf = Gio.File.new_for_path(str(out))
                window.show_toast(_("Saved {name}").format(name=out.name), _("Open"),
                                  lambda: window.open_files([gf]))
            return False

        threading.Thread(target=work, daemon=True).start()

    export.connect("clicked", on_export)
    if cleanup:
        dlg.connect("closed", lambda *_: cleanup())
    dlg.present(host)


if __name__ == "__main__":
    sys.exit(main())
