"""Where and how View is running: platform, packaging, launcher, paths.

Everything that differs between a source checkout, the .deb, the snap and the
Windows (PyInstaller) build is decided here, so the rest of the code can ask
simple questions (``IS_LINUX``, ``launcher_argv()``, ``user_data_dir()``)
instead of hard-coding ~/.local or /usr paths.
"""
from __future__ import annotations

import os
import shutil
import sys
import time

from . import __version__

APP_ID = "io.github.zdong104.View"
APP_NAME = "View"
VERSION = __version__
HOMEPAGE = "https://github.com/Zdong104/View"
ISSUES = "https://github.com/Zdong104/View/issues"
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
    WGL, which is PyOpenGL's default there.  Must run before OpenGL is imported.

    On Windows (GTK 4.24) View draws with GTK's cairo renderer on top of
    Direct Composition, which is still opt-in (GDK_DEBUG=dcomp): at 4K that
    presents frames ~50% faster than plain cairo (layered window).  GTK's GL
    and Vulkan renderers need Direct Composition too, but with it they paint
    the transparent margin around every window, dialog and popover (shadows,
    rounded corners, resize border) black - GTK keeps DComp off by default
    for that reason - so they aren't used.  VIEW_NO_DCOMP=1 turns this off;
    an explicit GSK_RENDERER is respected."""
    if IS_LINUX:
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    if IS_WINDOWS and not os.environ.get("VIEW_NO_DCOMP"):
        os.environ.setdefault("GSK_RENDERER", "cairo")
        flags = os.environ.get("GDK_DEBUG", "").replace(",", ":").replace(" ", ":")
        if "dcomp" not in flags.split(":"):
            os.environ["GDK_DEBUG"] = ":".join(f for f in (flags, "dcomp") if f)


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
    cache = os.path.join(local, "View", "cache")
    try:
        os.makedirs(cache, exist_ok=True)
        os.environ["GST_REGISTRY"] = os.path.join(cache, "gstreamer-registry.bin")
    except OSError:
        pass
    if IS_WINDOWS:
        # group taskbar buttons with the Start-menu shortcut (Inno sets the same ID)
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Zdong104.View")
        except Exception:  # noqa: BLE001
            pass
        # GSettings has no dconf on Windows; keep settings in the registry
        os.environ.setdefault("GSETTINGS_BACKEND", "registry")


def process_age() -> float | None:
    """Seconds since this process started (includes the PyInstaller
    bootloader and Python's own startup), or None if unknown."""
    try:
        if IS_WINDOWS:
            import ctypes
            from ctypes import wintypes as wt
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wt.HANDLE
            created, ex, kt, ut, now = (wt.FILETIME() for _ in range(5))
            k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
            if not k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(created),
                                       ctypes.byref(ex), ctypes.byref(kt), ctypes.byref(ut)):
                return None
            k32.GetSystemTimeAsFileTime(ctypes.byref(now))

            def t(ft):
                return (ft.dwHighDateTime << 32 | ft.dwLowDateTime) / 1e7
            return t(now) - t(created)
        if IS_LINUX:
            with open("/proc/self/stat") as f:
                start = int(f.read().rsplit(")", 1)[1].split()[19])
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            return up - start / os.sysconf("SC_CLK_TCK")
    except Exception:  # noqa: BLE001
        return None
    return None


startup_seconds: float | None = None     # process start -> first window shown (set by the app)

# ---- startup timing --------------------------------------------------------
# Seconds since the process started at named points on the way to the first
# window, shown in About → Troubleshooting and by ``view-app --startup-trace``.
# The first mark is taken when this module is imported, so it covers the
# PyInstaller bootloader and Python's own startup.
startup_trace = False                     # --startup-trace: print the marks, then quit
startup_marks: list[tuple[str, float]] = []
_age_offset = (process_age() or 0.0) - time.perf_counter()


def mark(label: str):
    """Record that startup reached `label` (once the first window is up,
    further marks are ignored)."""
    if startup_seconds is None:
        startup_marks.append((label, time.perf_counter() + _age_offset))


mark("Python started")


def startup_report() -> str:
    """The marks as a table: seconds since process start, and each step."""
    lines, prev = [], 0.0
    for label, t in startup_marks:
        lines.append(f"{t:7.2f} s  +{t - prev:5.2f}  {label}")
        prev = t
    return "\n".join(lines)


def loaded_modules() -> list[str]:
    """Paths of the DLLs loaded in this process (Windows; an antivirus scans
    each of them on first load, so their number and size matter)."""
    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes as wt
    psapi, k32 = ctypes.WinDLL("psapi"), ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = wt.HANDLE
    proc = k32.GetCurrentProcess()
    mods = (wt.HMODULE * 4096)()
    need = wt.DWORD()
    psapi.EnumProcessModules.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                                         ctypes.POINTER(wt.DWORD)]
    if not psapi.EnumProcessModules(proc, mods, ctypes.sizeof(mods), ctypes.byref(need)):
        return []
    psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
    out, buf = [], ctypes.create_unicode_buffer(1024)
    for h in mods[:need.value // ctypes.sizeof(wt.HMODULE)]:
        if psapi.GetModuleFileNameExW(proc, h, buf, 1024):
            out.append(buf.value)
    return out


def package_root() -> str:
    """Directory that contains the ``preview`` package (for ``-m preview`` fallbacks)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_data_dir() -> str:
    """Per-user data dir for View (e.g. ~/.local/share/view)."""
    from gi.repository import GLib
    return os.path.join(GLib.get_user_data_dir(), "view")


def user_cache_dir() -> str:
    from gi.repository import GLib
    return os.path.join(GLib.get_user_cache_dir(), "view")


def launcher_argv() -> list[str]:
    """argv prefix that starts View from outside this process (Files
    scripts, D-Bus activation, "Open in View").  Absolute when possible."""
    env = os.environ.get("VIEW_APP_LAUNCHER")      # set by our wrapper scripts
    if env and os.access(env, os.X_OK):
        return [env]
    if FROZEN:
        return [sys.executable]
    if IN_SNAP:
        return ["/snap/bin/" + os.environ.get("SNAP_INSTANCE_NAME", "view")]
    exe = shutil.which("view-app")
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
