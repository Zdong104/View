"""Windows: fall back to Mesa's software OpenGL when there is no real driver.

The 3D viewer needs OpenGL 3.2+.  Virtual machines, Remote Desktop sessions
and PCs on the "Microsoft Basic Display Adapter" only have Windows' built-in
OpenGL 1.1, so GTK's GLArea fails there (and then reports its EGL fallback:
"libEGL not available").  The Windows build bundles Mesa (llvmpipe, a fast
software renderer) in ``mesa\\``; ``ensure()`` loads it in place of the
system's opengl32.dll when no OpenGL driver is installed.  It runs at startup
(preview.__main__), before GTK loads: GTK and PyOpenGL load "opengl32.dll" by
name, and Windows hands them whichever module of that name is already loaded
(PyOpenGL by the path it finds on PATH, hence the PATH change).
(So we can't simply try the system driver first: even choosing a pixel format
loads the system opengl32.dll for good.)  Checking costs a few registry reads;
Mesa itself is only loaded on PCs that need it.

If the system driver is used and still fails, ``remember_failure()`` makes
the next start use Mesa.  VIEW_SOFTWARE_GL=1 always uses Mesa, =0 never.
"""
from __future__ import annotations

import os
import sys

_state: str | None = None          # what ensure() decided, for About → Troubleshooting
DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def mesa_dir() -> str | None:
    base = getattr(sys, "_MEIPASS", None)
    d = os.path.join(base, "mesa") if base else None
    return d if d and os.path.exists(os.path.join(d, "opengl32.dll")) else None


def _flag_path() -> str:
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "View", "software-gl")


def opengl_drivers() -> list[str]:
    """OpenGL drivers (ICDs) the display adapters register; empty when only
    Windows' OpenGL 1.1 is available."""
    import winreg
    found = []

    def values(key):
        i = 0
        while True:
            try:
                yield winreg.EnumValue(key, i)
            except OSError:
                return
            i += 1
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DISPLAY_CLASS) as cls:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(cls, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(cls, sub) as k:
                        for name, val, _t in values(k):
                            if name.startswith("OpenGLDriverName") and val:
                                found.append(val[0] if isinstance(val, list) else str(val))
                except OSError:
                    pass
    except OSError:
        pass
    try:                                   # older drivers register here instead
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\OpenGLDrivers") as k:
            i = 0
            while True:
                try:
                    found.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return [d for d in found if d]


def _remote_session() -> bool:
    import ctypes
    return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))      # SM_REMOTESESSION


def load_mesa(folder: str) -> str | None:
    """Load Mesa's opengl32.dll; None on success, else why not."""
    import ctypes
    from ctypes import wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryExW.restype = wt.HMODULE
    k32.LoadLibraryExW.argtypes = [wt.LPCWSTR, wt.HANDLE, wt.DWORD]
    k32.GetModuleHandleW.restype = wt.HMODULE
    k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    k32.GetModuleFileNameW.argtypes = [wt.HMODULE, wt.LPWSTR, wt.DWORD]
    k32.AddDllDirectory.restype = ctypes.c_void_p
    k32.AddDllDirectory.argtypes = [wt.LPCWSTR]
    loaded = k32.GetModuleHandleW("opengl32.dll")
    if loaded:
        buf = ctypes.create_unicode_buffer(1024)
        k32.GetModuleFileNameW(loaded, buf, 1024)
        return f"{buf.value} was loaded first"
    os.environ.setdefault("GALLIUM_DRIVER", "llvmpipe")
    # Its dependencies are in mesa\ (Mesa, LLVM), in _internal\ (shared with
    # the rest of View) and in System32.  Both folders must be registered:
    # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR covers only opengl32.dll's direct
    # imports, not libgallium_wgl.dll's (LLVM).
    k32.AddDllDirectory(folder)
    k32.AddDllDirectory(os.path.dirname(folder))
    flags = 0x100 | 0x1000      # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | _DEFAULT_DIRS
    if not k32.LoadLibraryExW(os.path.join(folder, "opengl32.dll"), None, flags):
        err = ctypes.WinError(ctypes.get_last_error())
        missing = _missing_imports(folder)
        return f"loading it failed: {err}" + (f" (missing {', '.join(missing)})"
                                               if missing else "")
    # PyOpenGL doesn't load "opengl32" by name but by the full path that
    # ctypes.util.find_library() finds on PATH (System32's), which would be a
    # second, Mesa-less opengl32.dll: make it find Mesa's
    os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    return None


def _imports(path: str) -> list[str]:
    """DLL names in a PE file's import table."""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec, optsize = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from(
        "<H", data, pe + 20)[0]
    opt = pe + 24
    dirs = opt + (112 if struct.unpack_from("<H", data, opt)[0] == 0x20B else 96)
    imp_rva = struct.unpack_from("<I", data, dirs + 8)[0]
    secs = [struct.unpack_from("<IIII", data, opt + optsize + 40 * i + 8)
            for i in range(nsec)]           # virtual size, address, raw size, raw offset

    def off(rva):
        for vsize, va, rsize, raw in secs:
            if va <= rva < va + max(vsize, rsize):
                return rva - va + raw
        raise ValueError(rva)
    names, d = [], off(imp_rva) if imp_rva else None
    while d is not None:
        name_rva = struct.unpack_from("<I", data, d + 12)[0]
        if not name_rva:
            break
        n = off(name_rva)
        names.append(data[n:data.index(b"\0", n)].decode("ascii", "replace"))
        d += 20
    return names


def _missing_imports(folder: str) -> list[str]:
    """Ask the loader itself which Mesa DLL fails, and which of its imports it
    can't load (to explain a failed load)."""
    import ctypes
    from ctypes import wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryExW.restype = wt.HMODULE
    k32.LoadLibraryExW.argtypes = [wt.LPCWSTR, wt.HANDLE, wt.DWORD]
    flags = 0x100 | 0x1000
    out = []
    try:
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(".dll"):
                continue
            path = os.path.join(folder, name)
            if k32.LoadLibraryExW(path, None, flags):
                continue
            code = ctypes.get_last_error()
            bad = []
            for imp in _imports(path):
                local = os.path.join(folder, imp)
                ok = k32.LoadLibraryExW(local, None, flags) if os.path.exists(local) else \
                    k32.LoadLibraryExW(imp, None, 0x1000)
                if not ok:
                    bad.append(f"{imp} [{ctypes.get_last_error()}]")
            out.append(f"{name} [{code}]" + (f" needs {', '.join(bad)}" if bad else ""))
    except Exception as e:  # noqa: BLE001
        out.append(f"? ({e})")
    return out


def ensure() -> str:
    """Pick the OpenGL implementation before anything uses OpenGL; returns
    what is in use (shown in About → Troubleshooting)."""
    global _state
    if _state is not None:
        return _state
    if sys.platform != "win32":
        _state = "system"
        return _state
    force = os.environ.get("VIEW_SOFTWARE_GL", "")
    folder = mesa_dir()
    drivers = remote = None
    if force == "1":
        why = "VIEW_SOFTWARE_GL=1"
    elif os.path.exists(_flag_path()):
        why = "the system driver failed before"
    else:
        try:
            drivers, remote = opengl_drivers(), _remote_session()
        except Exception:  # noqa: BLE001
            drivers, remote = ["?"], False
        why = "no OpenGL driver installed" if not drivers else \
            "Remote Desktop" if remote else None
    if why is None or force == "0":
        _state = "system driver" + (f" ({', '.join(os.path.basename(d) for d in drivers)})"
                                    if drivers else "")
    elif folder and (err := load_mesa(folder)) is None:
        _state = f"Mesa software rendering ({why})"
    else:
        _state = f"system driver (wanted Mesa: {why}; " + \
            ("not bundled)" if not folder else f"{err})")
    return _state


def using_mesa() -> bool:
    return (_state or "").startswith("Mesa")


def remember_failure() -> bool:
    """The system driver couldn't give us OpenGL 3.2: use Mesa from the next
    start on.  True if that will help (Mesa is bundled and not in use yet)."""
    if sys.platform != "win32" or using_mesa() or not mesa_dir() or \
            os.environ.get("VIEW_SOFTWARE_GL") == "0":
        return False
    try:
        os.makedirs(os.path.dirname(_flag_path()), exist_ok=True)
        open(_flag_path(), "w").close()
        return True
    except OSError:
        return False
