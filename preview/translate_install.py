"""Optional, per-user installation of the translation engine.

Like text recognition (preview.ocr_install, whose installer this reuses): the
.deb does not depend on ctranslate2 + sentencepiece (~45 MB download, not
packaged by Debian/Ubuntu), so View offers to install them into a private
virtual environment,

    <user data dir>/view/translate-venv   (python3 -m venv --system-site-packages)

and appends its site-packages to ``sys.path`` at runtime.  The snap bundles
the engine and the Windows build ships it as a helper process
(view-translate.exe, see preview.translate_helper).  Language packs are
separate: preview.translate downloads them when a pair is first used.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys

from . import ocr_install, runtime
from .i18n import _

PACKAGES = ["ctranslate2", "sentencepiece"]
DOWNLOAD_SIZE = "~45 MB"
_activated: str | None = None


def venv_dir() -> str:
    return os.path.join(runtime.user_data_dir(), "translate-venv")


def activate() -> bool:
    """Make a previously installed engine importable (appended, so the
    system's numpy keeps priority)."""
    global _activated
    if _activated:
        return True
    if runtime.OCR_BUNDLED:      # snap / Windows: bundled like OCR
        return False
    try:
        sp = ocr_install.site_packages(venv_dir())
    except Exception:  # noqa: BLE001  (no GLib yet, odd environment)
        return False
    if os.path.isdir(sp) and sp not in sys.path:
        sys.path.append(sp)
        importlib.invalidate_caches()
        _activated = sp
        return True
    return False


def can_install() -> tuple[bool, str]:
    """(possible, reason-if-not) for offering the download."""
    if runtime.IS_WINDOWS:
        return False, _("Translation is not included in this build "
                        "({path} is missing). Reinstall {APP_NAME} to get it back.").format(
                            path="translate\\view-translate.exe", APP_NAME=runtime.APP_NAME)
    if runtime.OCR_BUNDLED:
        return False, _("Translation should be bundled with this build.")
    if importlib.util.find_spec("venv") is None or importlib.util.find_spec("ensurepip") is None:
        return False, _("Python’s venv module is missing. Install it first "
                        "(e.g. {command}), then try again.").format(
                            command="sudo apt install python3-venv")
    return True, ""


def offer(parent, on_installed=None):
    """Ask, then install with a progress dialog.  ``parent``: any widget."""
    import gi
    gi.require_version("Adw", "1")
    from gi.repository import Adw
    ok, reason = can_install()
    if not ok:
        dlg = Adw.AlertDialog(heading=_("Translation Unavailable"), body=reason)
        dlg.add_response("ok", _("OK"))
        dlg.present(parent)
        return
    where = venv_dir().replace(os.path.expanduser("~"), "~", 1)
    dlg = Adw.AlertDialog(
        heading=_("Install Translation?"),
        body=_("Translating text on this computer, without sending it anywhere, needs the "
               "CTranslate2 engine ({DOWNLOAD_SIZE} download from PyPI). It is installed for "
               "your user only, in {where}, and can be removed by deleting that folder. "
               "Each language pair then downloads its language pack once.").format(
                   DOWNLOAD_SIZE=DOWNLOAD_SIZE, where=where))
    dlg.add_response("cancel", _("Not Now"))
    dlg.add_response("install", _("Install ({DOWNLOAD_SIZE})").format(DOWNLOAD_SIZE=DOWNLOAD_SIZE))
    dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("install")
    dlg.set_close_response("cancel")

    def make(progress, done):
        return ocr_install.Installer(progress, done, vd=venv_dir(), packages=PACKAGES,
                                     activate_fn=activate,
                                     label=_("Downloading the translation engine…"))

    def response(_d, rid):
        if rid == "install":
            ocr_install._progress_dialog(parent, on_installed, title=_("Installing Translation"),
                                         done_text=_("Translation installed"),
                                         fail_heading=_("Could Not Install Translation"),
                                         make_installer=make)
    dlg.connect("response", response)
    dlg.present(parent)
