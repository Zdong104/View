"""Where and how Preview is running: platform, packaging, launcher, paths.

Everything that differs between a source checkout, the .deb, the snap and the
Windows (PyInstaller) build is decided here, so the rest of the code can ask
simple questions (``IS_LINUX``, ``launcher_argv()``, ``user_data_dir()``)
instead of hard-coding ~/.local or /usr paths.
"""
from __future__ import annotations

import os
import shutil
import sys

from . import __version__

APP_ID = "io.github.zdong104.Preview"
APP_NAME = "Preview"
VERSION = __version__
HOMEPAGE = "https://github.com/Zdong104/Preview"
ISSUES = "https://github.com/Zdong104/Preview/issues"
DONATE = "https://buymeacoffee.com/zdong"

IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
FROZEN = bool(getattr(sys, "frozen", False))          # PyInstaller
IN_SNAP = bool(os.environ.get("SNAP")) and bool(os.environ.get("SNAP_NAME"))
IN_FLATPAK = os.path.exists("/.flatpak-info")
SANDBOXED = IN_SNAP or IN_FLATPAK

# The OCR engine is bundled in the snap and Windows builds; elsewhere it is an
# optional, user-level download (see preview.ocr_install).
OCR_BUNDLED = IN_SNAP or FROZEN


def configure_gl_env():
    """PyOpenGL must talk to the same GL platform GTK uses.  On Linux GTK
    renders through EGL (Wayland and, since 4.16, X11 too); on Windows it uses
    WGL, which is PyOpenGL's default there.  Must run before OpenGL is imported."""
    if IS_LINUX:
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")


def configure_frozen_env():
    """Windows (PyInstaller) build: point GTK, GStreamer, fontconfig and the
    icon/schema lookup at the bundled files.  PyInstaller's own runtime hooks
    set most of this; here we fix what they leave unsuitable for an install
    under Program Files (the GStreamer registry must be user-writable)."""
    if not FROZEN:
        return
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    share = os.path.join(base, "share")
    dirs = [d for d in os.environ.get("XDG_DATA_DIRS", "").split(os.pathsep) if d]
    if os.path.isdir(share) and share not in dirs:
        os.environ["XDG_DATA_DIRS"] = os.pathsep.join([share] + dirs)
    schemas = os.path.join(share, "glib-2.0", "schemas")
    if os.path.isdir(schemas):
        os.environ.setdefault("GSETTINGS_SCHEMA_DIR", schemas)
    fonts = os.path.join(base, "etc", "fonts", "fonts.conf")
    if os.path.exists(fonts):
        os.environ.setdefault("FONTCONFIG_FILE", fonts)
    # PyInstaller's GStreamer hook puts the whole bundle dir on GST_PLUGIN_PATH,
    # so GStreamer would try to load every DLL there as a plugin (ggml.dll
    # aborts the process when loaded that way).  Only scan the plugin dir.
    plugins = os.path.join(base, "gst_plugins")
    if os.path.isdir(plugins):
        os.environ["GST_PLUGIN_PATH"] = plugins
        os.environ["GST_PLUGIN_SYSTEM_PATH"] = ""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    cache = os.path.join(local, "Preview", "cache")
    try:
        os.makedirs(cache, exist_ok=True)
        os.environ["GST_REGISTRY"] = os.path.join(cache, "gstreamer-registry.bin")
    except OSError:
        pass
    if IS_WINDOWS:
        # GSettings has no dconf on Windows; keep settings in the registry
        os.environ.setdefault("GSETTINGS_BACKEND", "registry")


def package_root() -> str:
    """Directory that contains the ``preview`` package (for ``-m preview`` fallbacks)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_data_dir() -> str:
    """Per-user data dir for Preview (e.g. ~/.local/share/preview-app)."""
    from gi.repository import GLib
    return os.path.join(GLib.get_user_data_dir(), "preview-app")


def user_cache_dir() -> str:
    from gi.repository import GLib
    return os.path.join(GLib.get_user_cache_dir(), "preview-app")


def launcher_argv() -> list[str]:
    """argv prefix that starts Preview from outside this process (Files
    scripts, D-Bus activation, "Open in Preview").  Absolute when possible."""
    env = os.environ.get("PREVIEW_APP_LAUNCHER")      # set by our wrapper scripts
    if env and os.access(env, os.X_OK):
        return [env]
    if FROZEN:
        return [sys.executable]
    if IN_SNAP:
        return ["/snap/bin/" + os.environ.get("SNAP_INSTANCE_NAME", "preview")]
    exe = shutil.which("preview-app")
    if exe:
        return [os.path.abspath(exe)]
    return [sys.executable, "-m", "preview"]


def launcher_env() -> dict | None:
    """Extra environment needed when launcher_argv() is the ``-m`` fallback."""
    argv = launcher_argv()
    if len(argv) != 3 or argv[1:] != ["-m", "preview"]:
        return None
    env = dict(os.environ)
    root = package_root()
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def has_nautilus() -> bool:
    """GNOME Files is installed (and we could integrate with it)."""
    if not IS_LINUX:
        return False
    if shutil.which("nautilus"):
        return True
    for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        if d and os.path.exists(os.path.join(d, "applications", "org.gnome.Nautilus.desktop")):
            return True
    return False


def files_integration_supported() -> tuple[bool, str]:
    """(supported, reason-if-not) for the GNOME Files integration."""
    if not IS_LINUX:
        return False, "Only available on Linux with GNOME Files."
    if IN_SNAP:
        return False, ("Not available in the snap: strict confinement cannot register a "
                       "D-Bus service or Files scripts for your session. Install the .deb "
                       "to use it.")
    if IN_FLATPAK:
        return False, "Not available in the Flatpak sandbox."
    if not has_nautilus():
        return False, "GNOME Files (Nautilus) is not installed."
    return True, ""


def replace_file(src: str, dst: str):
    """os.replace(), but on Windows also works when `dst` is still open in
    this process (Poppler / pypdf keep the source document open while we save
    over it; Windows refuses to rename over an open file).  Falls back to an
    in-place overwrite there, which open-for-read handles tolerate."""
    try:
        os.replace(src, dst)
        return
    except PermissionError:
        if not IS_WINDOWS:
            raise
    import time
    for _ in range(5):              # antivirus / indexer may hold it briefly
        time.sleep(0.1)
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            pass
    shutil.copyfile(src, dst)
    try:
        os.unlink(src)
    except OSError:
        pass
