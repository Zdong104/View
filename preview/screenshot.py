"""Screenshot quick edit: press Print Screen, mark up, Done.

GNOME Shell saves every capture to ~/Pictures/Screenshots (the folder name is
translated) and copies it to the clipboard; on Windows 11 PrtSc / Win+Shift+S
(Snipping Tool) and Win+PrtSc save to the Screenshots known folder.  A small
resident process, ``view-app --screenshot-watcher``, watches that folder;
when a new image appears it opens it in a compact editor with the markup
toolbar already showing.  Done (Ctrl+S, Ctrl+Enter) writes the edits back to
the same file and puts the edited image on the clipboard.  Closing without
Done leaves the file and the clipboard copy alone.

The watcher has its own application id, so it never receives the files the
main app is asked to open, and a lock file keeps it single-instance (the
snap may not own D-Bus names).  It stays running after an editor closes: the
clipboard content lives only as long as the process that set it.

On by default; the choice (Preferences) is kept in ~/.config/view/
screenshots.json, so turning it off survives upgrades.  How it starts at login:
  .deb      /etc/xdg/autostart/<id>.ScreenshotEditor.desktop, shipped by the
            package; "off" is a per-user copy with Hidden=true
  snap      the screenshot-watcher app's autostart file in
            $SNAP_USER_DATA/.config/autostart (snapd's userd starts it)
  Windows   a Run value in HKCU (the installer starts the watcher, which
            registers it)
  source    a per-user ~/.config/autostart entry
The main app calls sync() shortly after it starts, so a new install or a
crashed watcher is back without logging out.

VIEW_SCREENSHOT_DIR overrides the watched folder (for testing; the watcher
then leaves the autostart setup alone).
"""
from __future__ import annotations

import gettext
import json
import os
import signal
import subprocess
import sys
import time

from . import runtime
from .i18n import _

APP_ID = runtime.APP_ID + ".ScreenshotEditor"
AUTOSTART_NAME = APP_ID + ".desktop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "View Screenshot Editor"
FLAG = "--screenshot-watcher"
EXTS = {".png", ".jpg", ".jpeg", ".webp"}
POLL_MS = 15             # re-check a new file this often until it is complete…
POLL_FOR_S = 3           # …for this long (then only CHANGES_DONE_HINT opens it)
WARM_UP_MS = 1500        # after startup: load the editor before it is needed
_LOCK_AT = 4096          # Windows locks a byte range: past the PID text (instance.LOCK_AT)


_logfile = None


def _log(*a):
    """To stderr; the windowed Windows exe has none, so there it goes to
    %LOCALAPPDATA%\\view\\screenshot-watcher.log."""
    global _logfile
    out = sys.stderr
    if out is None:
        if _logfile is None:
            try:
                d = runtime.user_data_dir()
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, "screenshot-watcher.log")
                big = os.path.exists(path) and os.path.getsize(path) > 2**20
                _logfile = open(path, "w" if big else "a", encoding="utf-8")
            except OSError:
                return
        out = _logfile
    print("[screenshots]", *a, file=out, flush=True)


class _Stages:
    """Timing of one screenshot, PrtSc to editor, logged stage by stage
    (ms since the file appeared, +ms since the previous stage)."""

    def __init__(self, name):
        self.name = name
        self.t0 = self.prev = time.monotonic()

    def mark(self, label):
        now = time.monotonic()
        _log(f"{self.name}: {(now - self.t0) * 1000:6.0f} ms  +{(now - self.prev) * 1000:5.0f}"
             f"  {label}")
        self.prev = now


# ----------------------------------------------------------------- folder ---

def screenshot_dir() -> str:
    """Where the desktop saves screenshots (see the module docstring)."""
    override = os.environ.get("VIEW_SCREENSHOT_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if runtime.IS_WINDOWS:
        known = _windows_known_folder("b7bede81-df94-4682-a7d8-57a52620b86f")  # Screenshots
        if known:
            return known
    from gi.repository import GLib
    home = os.environ.get("SNAP_REAL_HOME") if runtime.IN_SNAP else None
    pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
    if home and not (pictures or "").startswith(home + os.sep):
        pictures = os.path.join(home, "Pictures")   # the snap's $HOME is $SNAP_USER_DATA
    pictures = pictures or os.path.expanduser("~/Pictures")
    names = ["Screenshots"]
    if runtime.IS_LINUX:                    # gnome-shell translates the folder name
        names.insert(0, gettext.translation("gnome-shell", "/usr/share/locale",
                                            fallback=True).gettext("Screenshots"))
    for name in names:                      # prefer whichever already exists
        if os.path.isdir(os.path.join(pictures, name)):
            return os.path.join(pictures, name)
    return os.path.join(pictures, names[0])


def _windows_known_folder(guid: str) -> str | None:
    """SHGetKnownFolderPath (follows a OneDrive / user redirection)."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                        ("d4", ctypes.c_ubyte * 8)]
        g = GUID.from_buffer_copy(uuid.UUID(guid).bytes_le)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None,
                                                      ctypes.byref(out)) != 0:
            return None
        try:
            return out.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------- on / off switch ---

def supported() -> bool:
    return (runtime.IS_LINUX and not runtime.IN_FLATPAK) or (runtime.IS_WINDOWS and
                                                             runtime.FROZEN)


def _state_path() -> str:
    from gi.repository import GLib
    return os.path.join(GLib.get_user_config_dir(), "view", "screenshots.json")


def chosen() -> bool:
    """The user's choice; on until they turn it off."""
    try:
        with open(_state_path()) as f:
            return bool(json.load(f).get("enabled", True))
    except Exception:  # noqa: BLE001
        return True


def set_enabled(on: bool):
    """Preferences switch: remember the choice, then apply it."""
    from . import integration
    integration._write(_state_path(), json.dumps({"enabled": bool(on)}) + "\n")
    sync()


def sync():
    """Make the login autostart and the running watcher match the choice."""
    on = chosen()
    _set_autostart(on)
    if on and not running():
        start()
    elif not on:
        stop()


def _mode() -> str:
    if runtime.IS_WINDOWS:
        return "windows"
    if runtime.IN_SNAP:
        return "snap"
    return "system" if _system_entry() else "user"


def _system_entry() -> str | None:
    """The .deb's /etc/xdg/autostart entry, if installed."""
    from gi.repository import GLib
    for d in GLib.get_system_config_dirs():
        p = os.path.join(d, "autostart", AUTOSTART_NAME)
        if os.path.exists(p):
            return p
    return None


def autostart_path() -> str:
    """Per-user autostart file (in the snap: $SNAP_USER_DATA/.config/autostart)."""
    from gi.repository import GLib
    return os.path.join(GLib.get_user_config_dir(), "autostart", AUTOSTART_NAME)


def _desktop_quote(arg: str) -> str:
    """Quote one Exec= argument (Desktop Entry spec; GKeyFile unescapes once more)."""
    arg = arg.replace("%", "%%")
    if not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    for c in ("\\", '"', "`", "$"):
        arg = arg.replace(c, "\\" + c)
    return '"' + arg.replace("\\", "\\\\") + '"'


def _desktop_entry(exec_line: str, extra: str = "") -> str:
    from . import integration
    return (f"# {integration.MARKER}\n[Desktop Entry]\nType=Application\n"
            f"Name=View Screenshot Editor\n"
            f"Comment=Opens new screenshots in View for quick markup\n"
            f"Exec={exec_line}\nIcon={runtime.APP_ID}\nNoDisplay=true\n"
            f"X-GNOME-Autostart-enabled=true\n{extra}")


def _set_autostart(on: bool):
    from . import integration
    mode = _mode()
    if mode == "windows":
        _set_run_value(on)
    elif mode == "system":
        # the package's entry starts it; a Hidden=true copy turns it off for this user
        if on:
            integration._remove(autostart_path())
        else:
            integration._write(autostart_path(), _desktop_entry("true", "Hidden=true\n"))
    elif not on:
        integration._remove(autostart_path())
    elif mode == "snap":
        # snapd replaces the command with the app that names this file in snapcraft.yaml
        integration._write(autostart_path(), _desktop_entry("view.screenshot-watcher"))
    else:
        integration._write(autostart_path(), _desktop_entry(
            " ".join(_desktop_quote(a) for a in _watcher_argv(for_desktop=True))))


def autostart_on() -> bool:
    """Whether the watcher starts at the next login."""
    mode = _mode()
    if mode == "windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_VALUE)
            return True
        except OSError:
            return False
    if mode == "system":
        try:
            with open(autostart_path(), encoding="utf-8") as f:
                return "Hidden=true" not in f.read()
        except OSError:
            return True
    return os.path.exists(autostart_path())


def _set_run_value(on: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        if on:
            cmd = " ".join(f'"{a}"' if " " in a else a for a in _watcher_argv())
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def _watcher_argv(for_desktop=False) -> list[str]:
    if runtime.IS_WINDOWS and runtime.FROZEN:
        # View.exe is the windowed build: no console window
        gui = os.path.join(os.path.dirname(sys.executable), "View.exe")
        return [gui if os.path.exists(gui) else sys.executable, FLAG]
    if runtime.IN_SNAP:
        # inside the sandbox: run our launcher directly (not /snap/bin)
        return [os.path.join(os.environ["SNAP"], "usr", "bin", "view-app"), FLAG]
    argv = runtime.launcher_argv() + [FLAG]
    env = runtime.launcher_env()
    if for_desktop and env is not None:     # source checkout without a wrapper
        argv = ["/usr/bin/env", f"PYTHONPATH={env['PYTHONPATH']}"] + argv
    return argv


def start():
    """Start the watcher in the background (it exits if one is running)."""
    kw = {}
    if runtime.IS_WINDOWS:
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(_watcher_argv(), env=runtime.launcher_env(), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def stop():
    """Stop the running watcher, if any."""
    pid = _lock_owner()
    if pid and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)    # Windows: TerminateProcess
        except OSError:
            pass


# ------------------------------------------------------- single instance ---

def _lock_path() -> str:
    from gi.repository import GLib
    d = os.path.join(GLib.get_user_runtime_dir(), "view")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "screenshot-watcher.lock")


def _lock():
    """Take the watcher lock: the open lock file, or None if a watcher has it.
    (A lock, not a D-Bus name: the snap may not own names.)"""
    from . import instance
    f = open(_lock_path(), "a+b")
    if not instance.try_lock(f, _LOCK_AT):
        f.close()
        return None
    return f


def running() -> bool:
    """A watcher holds the lock."""
    try:
        f = _lock()
    except OSError:
        return False
    if f is not None:                       # we could take it: nobody holds it
        f.close()
        return False
    return True


def _lock_owner() -> int | None:
    """PID of the running watcher, None if there is none."""
    if not running():
        return None
    from . import instance
    return instance.read_pid(_lock_path())


# ----------------------------------------------------------------- watcher ---

def main(argv=None) -> int:
    """``--screenshot-watcher``: run the watcher; ``--screenshot-watcher --stop``
    stops a running one (the Windows installer calls this)."""
    argv = list(argv or [])
    if "--stop" in argv:
        stop()
        return 0
    if not supported() or not chosen():
        _log("turned off in Preferences" if supported() else "not available here")
        return 0
    lock = _lock()
    if lock is None:
        _log("already running")
        return 0
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n".encode())
    lock.flush()
    if not os.environ.get("VIEW_SCREENSHOT_DIR"):
        try:
            _set_autostart(True)            # e.g. started by the Windows installer
        except OSError as e:
            _log(f"cannot set up autostart: {e}")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import GLib
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("View")
    app = _watcher_class()()
    app.lock = lock                         # held until the process exits
    return app.run([sys.argv[0]])


def _watcher_class():
    from gi.repository import Adw, Gio, GLib

    class Watcher(Adw.Application):
        def __init__(self):
            # NON_UNIQUE: the lock file makes it single-instance, and the snap
            # may not own a D-Bus name
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.folder = screenshot_dir()
            self.monitor = None
            self._pending = {}         # created, not yet finished writing: path -> _Stages
            self._ours = set()         # files we are writing ourselves

        def do_startup(self):
            Adw.Application.do_startup(self)
            self.hold()                # resident: keeps owning the clipboard we set
            try:
                os.makedirs(self.folder, exist_ok=True)
                self.monitor = Gio.File.new_for_path(self.folder).monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None)
            except (OSError, GLib.Error) as e:
                _log(f"cannot watch {self.folder}: {e}")
                self.release()
                return
            self.monitor.connect("changed", self._on_changed)
            if hasattr(GLib, "unix_signal_add"):
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_term)
            _log(f"watching {self.folder}")
            GLib.timeout_add(WARM_UP_MS, self._warm_up)

        def _warm_up(self):
            """Pay the one-time costs now, not when the first screenshot arrives:
            import the image viewer and markup, and set up a renderer, CSS and
            icons with a window that is realized but never shown."""
            t0 = time.monotonic()
            try:
                cls = _editor_class()
                win = cls(self, None)
                win.realize()
                win.destroy()
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
            _log(f"warmed up in {(time.monotonic() - t0) * 1000:.0f} ms")
            return GLib.SOURCE_REMOVE

        def do_activate(self):
            pass                       # a second launch lands here: nothing to do

        def _on_term(self):
            self.quit()
            return GLib.SOURCE_REMOVE

        def _wanted(self, path):
            name = os.path.basename(path)
            return (not name.startswith(".") and os.path.splitext(name)[1].lower() in EXTS
                    and path not in self._ours)

        def _on_changed(self, _mon, gfile, other, event):
            path = gfile.get_path() or ""
            E = Gio.FileMonitorEvent
            if event == E.CREATED:
                # only brand-new files: moves into the folder arrive as MOVED_IN
                if self._wanted(path):
                    self._track(path)
            elif event == E.RENAMED and other is not None:
                # a tool that writes "x.tmp" and renames it to "x.png" (renaming
                # one image to another is the user, not a new capture)
                new, old = other.get_path() or "", os.path.basename(path)
                if self._wanted(new) and (old.startswith(".") or
                                          os.path.splitext(old)[1].lower() not in EXTS):
                    self._track(new)
            elif event == E.CHANGES_DONE_HINT and path in self._pending:
                self._ready(path, "closed")
            elif event == E.DELETED:
                self._pending.pop(path, None)

        def _track(self, path):
            self._pending[path] = st = _Stages(os.path.basename(path))
            st.mark(f"created (file age {_age_ms(path)} ms)")
            if self._poll(path):
                GLib.timeout_add(POLL_MS, self._poll, path)

        def _poll(self, path):
            """gnome-shell writes the whole PNG right after creating the file but
            only closes it when its garbage collector gets to it (0.1-2 s later,
            when CHANGES_DONE_HINT arrives; on Windows GLib only makes one up ~2 s
            after the last change).  So open as soon as the image is complete;
            keep checking briefly in case the bytes come in late (or the writer
            still has the file locked)."""
            st = self._pending.get(path)
            if st is None:
                return GLib.SOURCE_REMOVE           # opened, or deleted
            if _complete(path):
                self._ready(path, "complete")
                return GLib.SOURCE_REMOVE
            return time.monotonic() - st.t0 < POLL_FOR_S

        def _ready(self, path, why):
            st = self._pending.pop(path)
            st.mark(f"{why} (file age {_age_ms(path)} ms)")
            self.open_editor(path, st)

        def open_editor(self, path, st=None):
            st = st or _Stages(os.path.basename(path))
            try:
                cls = _editor_class()
                st.mark("editor code loaded")
                win = cls(self, path, st)
                st.mark("window built")
                win.present()
                st.mark("presented")
                if runtime.IS_WINDOWS:
                    GLib.timeout_add(50, _raise_windows, win.get_title())
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()

    return Watcher


def _complete(path) -> bool:
    """True when `path` is a whole PNG (ends with IEND) or JPEG (ends with EOI);
    other formats wait for the writer to close the file."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            f.seek(0, os.SEEK_END)
            if f.tell() < 20:
                return False
            f.seek(-12, os.SEEK_END)
            tail = f.read(12)
    except OSError:
        return False
    if head == b"\x89PNG\r\n\x1a\n":
        return tail == b"\x00\x00\x00\x00IEND\xaeB`\x82"
    if head[:2] == b"\xff\xd8":
        return tail[-2:] == b"\xff\xd9"
    return False


def _raise_windows(title):
    """Windows only lets the foreground app activate windows, so present()
    from this background process would only flash the taskbar button.
    Borrow the foreground thread's input state for a moment (the usual
    AttachThreadInput workaround) and bring our window to the front."""
    try:
        import ctypes
        from ctypes import wintypes as wt
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wt.HWND
        user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        user32.GetWindowThreadProcessId.restype = wt.DWORD
        user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        for fn in (user32.IsWindowVisible, user32.BringWindowToTop, user32.SetForegroundWindow):
            fn.argtypes = [wt.HWND]
        user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
        found = []
        buf = ctypes.create_unicode_buffer(512)
        pid = wt.DWORD()

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def each(hwnd, _lp):
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid() and user32.IsWindowVisible(hwnd):
                user32.GetWindowTextW(hwnd, buf, 512)
                if buf.value == title:
                    found.append(hwnd)
                    return False
            return True
        user32.EnumWindows(each, 0)
        if not found:
            return False
        fg = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        me = kernel32.GetCurrentThreadId()
        attached = fg and fg != me and user32.AttachThreadInput(fg, me, True)
        try:
            user32.BringWindowToTop(found[0])
            user32.SetForegroundWindow(found[0])
        finally:
            if attached:
                user32.AttachThreadInput(fg, me, False)
    except Exception as e:  # noqa: BLE001
        _log(f"could not bring the editor to the front: {e}")
    return False


def _age_ms(path):
    """How long ago the file was last written (ms), to see monitor latency."""
    try:
        return round((time.time() - os.stat(path).st_mtime) * 1000)
    except OSError:
        return -1


# ------------------------------------------------------------------ editor ---

_EDITOR = None


def _editor_class():
    """The editor window class, built on first use (pulls in the image viewer)."""
    global _EDITOR
    if _EDITOR is not None:
        return _EDITOR
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk
    from PIL import Image
    from .image_ops import save_any
    from .image_view import ImageViewer, pil_to_texture

    class QuickViewer(ImageViewer):
        def _cancel(self):
            if not super()._cancel():
                self.window.close()         # nothing left to back out of: close
            return True

    class ScreenshotEditor(Adw.ApplicationWindow):
        def __init__(self, app, path, stages=None):
            """path=None: an empty editor, only built to warm up (never shown)."""
            name = os.path.basename(path) if path else ""
            self.stages = stages or _Stages(name)
            w, h = self._fit(path) if path else (1000, 720)
            super().__init__(application=app if path else None, title=name,
                             default_width=w, default_height=h)
            self.app = app
            self.path = path
            self._finished = False          # Done or Discard: close without asking
            self._saving = False

            self.toasts = Adw.ToastOverlay()
            self.header = Adw.HeaderBar()
            self.wtitle = Adw.WindowTitle(title=name)
            self.header.set_title_widget(self.wtitle)
            self.done_btn = Gtk.Button(label=_("Done"), css_classes=["suggested-action"],
                                       tooltip_text=_("Save and copy to the clipboard (Ctrl+S)"))
            self.done_btn.connect("clicked", lambda *_: self.done())
            self.header.pack_end(self.done_btn)
            toolbar = Adw.ToolbarView()
            toolbar.add_top_bar(self.header)
            toolbar.set_content(self.toasts)
            self.set_content(toolbar)

            self.viewer = QuickViewer(self)
            self.toasts.set_child(self.viewer)
            self.viewer.build_header(self.header)
            if path:
                self.viewer.load([Gio.File.new_for_path(path)])
            self.viewer._siblings, self.viewer._sib_idx = [], -1   # no browsing the folder
            self.viewer._toggle_markup(True)

            keys = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)
            keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>s"),
                Gtk.CallbackAction.new(lambda *_: (self.done(), True)[1])))
            self.add_controller(keys)
            enter = Gtk.ShortcutController()     # bubble: a text box's Ctrl+Enter wins
            enter.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>Return|<Control>KP_Enter"),
                Gtk.CallbackAction.new(lambda *_: (self.done(), True)[1])))
            self.add_controller(enter)
            self.connect("close-request", self._on_close_request)
            self.add_tick_callback(self._watch_paint)
            self._painted = False

        def _watch_paint(self, *_):
            """Log the first frame, then the first frame with the image in it."""
            if not self._painted:
                self._painted = True
                self.stages.mark("first frame")
            it = self.viewer.current
            if it is not None and it.texture is not None:
                self.stages.mark("image on screen")
                return False
            return True

        @staticmethod
        def _fit(path):
            """Window size that shows the whole screenshot within ~80% of the screen."""
            try:
                with Image.open(path) as im:
                    iw, ih = im.size
                mons = Gdk.Display.get_default().get_monitors()
                mon = mons.get_item(0)
                geo, scale = mon.get_geometry(), mon.get_scale_factor() or 1
                sw, sh = geo.width, geo.height
            except Exception:  # noqa: BLE001
                return 1000, 720
            chrome = 110                   # header bar + markup toolbar
            z = min(1.0, sw * 0.8 / (iw / scale), (sh * 0.8 - chrome) / (ih / scale))
            return (max(760, int(iw / scale * z)), max(420, int(ih / scale * z) + chrome))

        # the shell methods an ImageViewer expects of its window
        def show_toast(self, message, button_label=None, on_button=None):
            toast = Adw.Toast(title=message, timeout=4)
            if button_label:
                toast.set_button_label(button_label)
                if on_button:
                    toast.connect("button-clicked", lambda *_: on_button())
            self.toasts.add_toast(toast)

        def refresh_title(self):
            self.wtitle.set_subtitle(self.viewer.get_subtitle() or "")

        # ---- Done -----------------------------------------------------------
        def done(self):
            it = self.viewer.current
            if self._saving or it is None or it.rendered is None:
                return
            self._saving = True
            self.done_btn.set_sensitive(False)
            edited = bool(it.final_ops())
            path, rendered = self.path, it.rendered
            folder, name = os.path.split(path)
            stem, ext = os.path.splitext(name)
            tmp = os.path.join(folder, f".{stem}.view-tmp{ext}")   # hidden: the watcher skips it
            self.app._ours.add(path)

            def work():
                if not edited:
                    return pil_to_texture(rendered)
                im, exif, icc = it.final_image()
                try:
                    save_any(im, exif, icc, tmp)
                    runtime.replace_file(tmp, path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                return pil_to_texture(im)

            def finished(tex, err):
                self._saving = False
                if err is not None:
                    self.done_btn.set_sensitive(True)
                    self.show_toast(_("Could not save: {err}").format(err=err))
                    return
                self.viewer._set_clip(tex)
                _log(f"done {name} ({'edited' if edited else 'unchanged'}), copied")
                self._finished = True
                self.close()
            self.viewer._run_bg(work, finished)

        # ---- close ----------------------------------------------------------
        def _closing(self):
            # signal closures keep the window object alive (a reference cycle
            # through GObject): drop the decoded screenshot explicitly so the
            # resident watcher doesn't hold on to it
            def free():
                self.viewer.release()
                GLib.timeout_add(500, runtime.give_back_memory)
                return False
            GLib.idle_add(free)
            return False

        def _on_close_request(self, *_a):
            if self._saving:
                return True
            if self._finished or not self.viewer.has_unsaved_changes():
                return self._closing()
            dlg = Adw.AlertDialog(heading=_("Discard changes?"),
                                  body=_("The screenshot and the clipboard keep the original."))
            dlg.add_response("cancel", _("Cancel"))
            dlg.add_response("discard", _("Discard"))
            dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")

            def response(_d, rid):
                if rid == "discard":
                    self._finished = True
                    self.close()
            dlg.connect("response", response)
            dlg.present(self)
            return True

    _EDITOR = ScreenshotEditor
    return _EDITOR
