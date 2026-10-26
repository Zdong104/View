"""UI translation (gettext).  No GTK imports (GLib only), so it is safe to call first.

Modules do ``from .i18n import _, ngettext, N_`` and write ``_("Open")``.
``setup()`` runs once at start-up, before any widget is built; the language is
the user's choice in Preferences (``language`` in view/settings.json) or else
the system's.  Changing it takes effect after a restart.

Catalogs: ``po/*.po`` are compiled by ``make i18n`` into
``preview/locale/<lang>/LC_MESSAGES/view.mo``.
"""
from __future__ import annotations

import gettext as _gettext
import json
import os
import sys

DOMAIN = "view"
SYSTEM = "system"
# (code, name shown in Preferences — always in its own language)
LANGUAGES = [("en", "English"), ("zh_CN", "简体中文")]

_translation: _gettext.NullTranslations = _gettext.NullTranslations()


def locale_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")


def settings_path() -> str:
    """``view/settings.json`` in the user config dir, next to View's other
    settings files (GLib's dir: ~/.config, or %LOCALAPPDATA% on Windows)."""
    from gi.repository import GLib      # GLib only; GTK is not loaded here
    return os.path.join(GLib.get_user_config_dir(), "view", "settings.json")


def _read() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def chosen() -> str:
    """The saved choice: SYSTEM or a code from LANGUAGES."""
    lang = _read().get("language", SYSTEM)
    return lang if lang in dict(LANGUAGES) else SYSTEM


def set_chosen(lang: str):
    data = _read()
    data["language"] = lang
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _windows_ui_language() -> str | None:
    """The Windows display language as a gettext code (zh-CN -> zh_CN).  Not
    GetUserDefaultLocaleName: that is the regional format, not the UI."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(85)
        if k32.LCIDToLocaleName(k32.GetUserDefaultUILanguage(), buf, 85, 0):
            return buf.value.replace("-", "_")
    except Exception:  # noqa: BLE001
        pass
    return None


def _system_languages() -> list[str] | None:
    """Languages to try for the "system" choice.  None on POSIX: gettext then
    reads LANGUAGE, LC_ALL, LC_MESSAGES and LANG itself, like GTK does."""
    if sys.platform != "win32":
        return None
    langs = [l for l in os.environ.get("LANGUAGE", "").split(":") if l]
    if not langs:
        code = _windows_ui_language()
        langs = [code] if code else []
    return langs or None


def setup():
    """Install the catalog and point GTK/libadwaita at the same language."""
    global _translation
    choice = chosen()
    if choice == SYSTEM:
        langs = _system_languages()
    else:
        langs = [choice]
        # GTK and libadwaita read LANGUAGE for their own strings
        os.environ["LANGUAGE"] = choice
    _translation = _gettext.translation(
        DOMAIN, locale_dir(), languages=langs, fallback=True)


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


def N_(message: str) -> str:
    """Mark a string for extraction without translating it yet (module-level
    constants); translate with ``_()`` where it is shown."""
    return message
