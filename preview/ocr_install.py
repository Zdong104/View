"""Optional, per-user installation of the text-recognition engine.

The .deb does not depend on RapidOCR/onnxruntime (~280 MB, not packaged by
Debian/Ubuntu).  When they are missing, View offers to install them into a
private virtual environment (never into the system Python -- PEP 668):

    <user data dir>/view/ocr-venv   (python3 -m venv --system-site-packages)

and appends that venv's site-packages to ``sys.path`` at runtime.  The snap
and the Windows build bundle the engine, so no offer is made there.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import re
import subprocess
import sys
import sysconfig
import threading

from . import runtime

PACKAGES = ["rapidocr", "onnxruntime"]
DOWNLOAD_SIZE = "~280 MB"
_activated: str | None = None


def venv_dir() -> str:
    return os.path.join(runtime.user_data_dir(), "ocr-venv")


def _legacy_venv_dir() -> str:
    """Where pre-release builds (named "Preview") put the engine."""
    from gi.repository import GLib
    return os.path.join(GLib.get_user_data_dir(), "preview-app", "ocr-venv")


def _venv_python(vd=None) -> str:
    vd = vd or venv_dir()
    if runtime.IS_WINDOWS:
        return os.path.join(vd, "Scripts", "python.exe")
    return os.path.join(vd, "bin", "python")


def site_packages(vd=None) -> str:
    """The venv's site-packages for *this* interpreter's version."""
    vd = vd or venv_dir()
    if runtime.IS_WINDOWS:
        return os.path.join(vd, "Lib", "site-packages")
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return os.path.join(vd, "lib", ver, "site-packages")


def activate() -> bool:
    """Make a previously installed engine importable.  Appended (not
    prepended) so the system's numpy/Pillow keep priority."""
    global _activated
    if _activated:
        return True
    if runtime.OCR_BUNDLED:
        return False
    try:
        sp = site_packages()
        if not os.path.isdir(sp):
            sp = site_packages(_legacy_venv_dir())
    except Exception:  # noqa: BLE001  (no GLib yet, odd environment)
        return False
    if os.path.isdir(sp) and sp not in sys.path:
        sys.path.append(sp)
        importlib.invalidate_caches()
        _activated = sp
        return True
    return False


def engine_importable() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in PACKAGES)


def can_install() -> tuple[bool, str]:
    """(possible, reason-if-not) for offering the download."""
    if runtime.IS_WINDOWS:
        return False, "Text recognition is not available in the Windows build yet."
    if runtime.OCR_BUNDLED:
        return False, "Text recognition should be bundled with this build."
    if importlib.util.find_spec("venv") is None or importlib.util.find_spec("ensurepip") is None:
        return False, ("Python’s venv module is missing. Install it first "
                       "(e.g. sudo apt install python3-venv), then try again.")
    return True, ""


def stale_install() -> bool:
    """A venv exists but was made for another Python version (after an OS upgrade)."""
    vd = venv_dir()
    return os.path.isdir(vd) and not os.path.isdir(site_packages(vd))


class Installer:
    """Runs venv + pip in a worker thread.

    on_progress(fraction_or_None, text) and on_done(error_or_None) are called
    on the GTK main loop.  fraction None means "pulse"."""

    def __init__(self, on_progress, on_done):
        self.on_progress = on_progress
        self.on_done = on_done
        self.proc = None
        self.cancelled = False

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="ocr-install").start()

    def cancel(self):
        self.cancelled = True
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    # ---- worker ----
    def _emit(self, frac, text):
        from gi.repository import GLib
        GLib.idle_add(lambda: (self.on_progress(frac, text), False)[1])

    def _finish(self, err):
        from gi.repository import GLib
        if err is None:
            activate()
        GLib.idle_add(lambda: (self.on_done(err), False)[1])

    def _run(self):
        vd = venv_dir()
        try:
            if stale_install():
                import shutil
                shutil.rmtree(vd, ignore_errors=True)
            if not os.path.exists(_venv_python(vd)):
                self._emit(0.02, "Creating a private Python environment…")
                base = getattr(sys, "_base_executable", None) or sys.executable
                self._call([base, "-m", "venv", "--system-site-packages", vd])
            self._emit(0.05, "Downloading the text recognition engine…")
            self._pip(vd)
            if self.cancelled:
                raise RuntimeError("Cancelled")
            sp = site_packages(vd)
            if not os.path.isdir(sp):
                raise RuntimeError(f"Installation finished but {sp} is missing")
            self._finish(None)
        except Exception as e:  # noqa: BLE001
            self._finish(e if not self.cancelled else RuntimeError("Cancelled"))

    def _call(self, argv):
        if self.cancelled:
            raise RuntimeError("Cancelled")
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, errors="replace")
        out = self.proc.communicate()[0]
        if self.proc.returncode != 0:
            raise RuntimeError(_last_lines(out) or f"{argv[0]} failed")

    def _pip(self, vd):
        env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONUNBUFFERED="1")
        env.pop("PYTHONPATH", None)
        argv = [_venv_python(vd), "-m", "pip", "install", "--progress-bar", "off",
                "--upgrade-strategy", "only-if-needed", *PACKAGES]
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, errors="replace",
                                     env=env, bufsize=1)
        seen, tail = 0, []
        frac = 0.05
        for line in self.proc.stdout:
            line = line.rstrip()
            tail = (tail + [line])[-12:]
            m = re.match(r"\s*(Collecting|Downloading|Using cached)\s+(\S+)", line)
            if m:
                seen += 1
                frac = min(0.85, frac + (0.8 - frac) * 0.12)
                name = re.split(r"[<>=!~;\[,]", os.path.basename(m.group(2)))[0]
                name = name.split("-")[0] if name.endswith((".whl", ".gz")) else name
                self._emit(frac, f"Downloading {name}…")
            elif line.startswith("Installing collected packages"):
                self._emit(0.9, "Installing…")
            if self.cancelled:
                break
        rc = self.proc.wait()
        if self.cancelled:
            raise RuntimeError("Cancelled")
        if rc != 0:
            raise RuntimeError(_last_lines("\n".join(tail)))


def _last_lines(text, n=3):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return "\n".join(lines[-n:])


# --------------------------------------------------------------- UI ---

_OFFER_KEY = "ocr-offer-shown"


def _flag_path():
    return os.path.join(runtime.user_data_dir(), _OFFER_KEY)


def offer_shown_before() -> bool:
    return os.path.exists(_flag_path())


def mark_offer_shown():
    try:
        os.makedirs(os.path.dirname(_flag_path()), exist_ok=True)
        open(_flag_path(), "w").close()
    except OSError:
        pass


def maybe_offer_once(host, on_installed=None):
    """For automatic (Live Text) paths: offer the download once per user,
    as a toast with an Install… button.  ``host`` needs toast()/show_toast()."""
    ok, _ = can_install()
    if not ok or offer_shown_before():
        return
    mark_offer_shown()
    toast = getattr(host, "toast", None) or getattr(host, "show_toast")
    widget = host if hasattr(host, "get_root") else None
    toast("Text recognition is not installed", "Install…",
          lambda *_: offer(widget, on_installed))


def offer(parent, on_installed=None):
    """Ask, then install with a progress dialog.  ``parent``: any widget."""
    import gi
    gi.require_version("Adw", "1")
    from gi.repository import Adw
    mark_offer_shown()
    ok, reason = can_install()
    if not ok:
        dlg = Adw.AlertDialog(heading="Text Recognition Unavailable", body=reason)
        dlg.add_response("ok", "OK")
        dlg.present(parent)
        return
    where = venv_dir().replace(os.path.expanduser("~"), "~", 1)
    dlg = Adw.AlertDialog(
        heading="Install Text Recognition?",
        body=(f"Recognizing text in images and scanned PDFs needs the RapidOCR engine "
              f"({DOWNLOAD_SIZE} download from PyPI). It is installed for your user only, "
              f"in {where}, and can be removed by deleting that folder."))
    dlg.add_response("cancel", "Not Now")
    dlg.add_response("install", f"Install ({DOWNLOAD_SIZE})")
    dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("install")
    dlg.set_close_response("cancel")

    def response(_d, rid):
        if rid == "install":
            _progress_dialog(parent, on_installed)
    dlg.connect("response", response)
    dlg.present(parent)


def _progress_dialog(parent, on_installed):
    from gi.repository import Adw, Gtk
    bar = Gtk.ProgressBar(show_text=False)
    label = Gtk.Label(label="Starting…", wrap=True, xalign=0, css_classes=["dim-label"])
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18,
                  margin_bottom=18, margin_start=18, margin_end=18, width_request=360)
    box.append(Gtk.Label(label="Installing Text Recognition", css_classes=["title-3"], xalign=0))
    box.append(bar)
    box.append(label)
    cancel = Gtk.Button(label="Cancel", halign=Gtk.Align.END)
    box.append(cancel)
    dlg = Adw.Dialog(child=box, can_close=False, title="Installing Text Recognition")
    state = {}

    def progress(frac, text):
        if frac is None:
            bar.pulse()
        else:
            bar.set_fraction(frac)
        label.set_label(text)

    def done(err):
        state["done"] = True
        dlg.set_can_close(True)
        dlg.close()
        root = parent.get_root() if parent is not None else None
        toast = getattr(root, "show_toast", None)
        if err is None:
            if toast:
                toast("Text recognition installed")
            if on_installed:
                on_installed()
        elif str(err) != "Cancelled":
            a = Adw.AlertDialog(heading="Could Not Install Text Recognition", body=str(err))
            a.add_response("ok", "OK")
            a.present(parent)

    inst = Installer(progress, done)
    cancel.connect("clicked", lambda *_: (inst.cancel(), label.set_label("Cancelling…")))
    dlg.present(parent)
    inst.start()
