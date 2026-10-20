"""Screenshot quick edit (GNOME): press Print Screen, mark up, Done.

GNOME Shell's screenshot UI saves every capture to ~/Pictures/Screenshots
(the folder name is translated) and copies it to the clipboard.  A small
resident process, ``view-app --screenshot-watcher``, watches that folder;
when a new image appears it opens it in a compact editor with the markup
toolbar already showing.  Done (Ctrl+S, Ctrl+Enter) writes the edits back to
the same file and puts the edited image on the clipboard.  Closing without
Done leaves GNOME's file and clipboard copy alone.

The watcher has its own application id, so it never receives the files the
main app is asked to open, and GApplication keeps it single-instance.  It
stays running after an editor closes: on Wayland the clipboard content lives
only as long as the process that set it.  It is started at login by an XDG
autostart entry that Preferences writes or removes.

VIEW_SCREENSHOT_DIR overrides the watched folder (for testing).
"""
from __future__ import annotations

import gettext
import os
import signal
import subprocess
import sys
import time

from . import runtime

APP_ID = runtime.APP_ID + ".ScreenshotEditor"
FLAG = "--screenshot-watcher"
EXTS = {".png", ".jpg", ".jpeg", ".webp"}
POLL_MS = 15             # re-check a new file this often until it is complete…
POLL_FOR_S = 3           # …for this long (then only CHANGES_DONE_HINT opens it)
WARM_UP_MS = 1500        # after startup: load the editor before it is needed


def _log(*a):
    print("[screenshots]", *a, file=sys.stderr, flush=True)


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
    """Where GNOME Shell saves screenshots: <Pictures>/<"Screenshots",
    translated with gnome-shell's own catalogue>."""
    override = os.environ.get("VIEW_SCREENSHOT_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    from gi.repository import GLib
    pictures = (GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
                or os.path.expanduser("~/Pictures"))
    names = [gettext.translation("gnome-shell", "/usr/share/locale",
                                 fallback=True).gettext("Screenshots"), "Screenshots"]
    for name in names:                      # prefer whichever already exists
        if os.path.isdir(os.path.join(pictures, name)):
            return os.path.join(pictures, name)
    return os.path.join(pictures, names[0])


# ---------------------------------------------------------- on / off switch ---

def supported() -> bool:
    return runtime.IS_LINUX and not runtime.SANDBOXED


def autostart_path() -> str:
    from gi.repository import GLib
    return os.path.join(GLib.get_user_config_dir(), "autostart", APP_ID + ".desktop")


def enabled() -> bool:
    from . import integration
    return os.path.exists(autostart_path()) and integration._is_ours(autostart_path())


def _desktop_quote(arg: str) -> str:
    """Quote one Exec= argument (Desktop Entry spec; GKeyFile unescapes once more)."""
    arg = arg.replace("%", "%%")
    if not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    for c in ("\\", '"', "`", "$"):
        arg = arg.replace(c, "\\" + c)
    return '"' + arg.replace("\\", "\\\\") + '"'


def _watcher_argv() -> list[str]:
    argv = runtime.launcher_argv() + [FLAG]
    env = runtime.launcher_env()
    if env is not None:                     # source checkout without a wrapper
        argv = ["/usr/bin/env", f"PYTHONPATH={env['PYTHONPATH']}"] + argv
    return argv


def set_enabled(on: bool):
    """Write or remove the autostart entry and start or stop the watcher."""
    from . import integration
    if on:
        integration._write(autostart_path(), (
            f"# {integration.MARKER}\n[Desktop Entry]\nType=Application\n"
            f"Name=View Screenshot Editor\n"
            f"Comment=Opens new screenshots in View for quick markup\n"
            f"Exec={' '.join(_desktop_quote(a) for a in _watcher_argv())}\n"
            f"Icon={runtime.APP_ID}\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n"))
        start()
    else:
        integration._remove(autostart_path())
        stop()


def start():
    """Start the watcher in the background (a running one makes it exit)."""
    subprocess.Popen(_watcher_argv(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def stop():
    from . import integration
    pid = integration._owner_pid(APP_ID)
    if pid and pid != os.getpid() and FLAG in integration._cmdline(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# ----------------------------------------------------------------- watcher ---

def main(argv=None) -> int:
    runtime.configure_gl_env()
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import GLib
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("View")
    return _watcher_class()().run([sys.argv[0]])


def _watcher_class():
    from gi.repository import Adw, Gio, GLib

    class Watcher(Adw.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID)
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

        def _on_changed(self, _mon, gfile, _other, event):
            path = gfile.get_path() or ""
            name = os.path.basename(path)
            E = Gio.FileMonitorEvent
            if event == E.CREATED:
                # only brand-new files; renames / moves arrive as RENAMED / MOVED_IN
                if (not name.startswith(".") and os.path.splitext(name)[1].lower() in EXTS
                        and path not in self._ours):
                    self._pending[path] = st = _Stages(name)
                    st.mark(f"created (file age {_age_ms(path)} ms)")
                    if self._poll(path):
                        GLib.timeout_add(POLL_MS, self._poll, path)
            elif event == E.CHANGES_DONE_HINT and path in self._pending:
                self._ready(path, "closed")
            elif event == E.DELETED:
                self._pending.pop(path, None)

        def _poll(self, path):
            """gnome-shell writes the whole PNG right after creating the file but
            only closes it when its garbage collector gets to it (0.1-2 s later,
            when CHANGES_DONE_HINT arrives).  So open as soon as the image is
            complete; keep checking briefly in case the bytes come in late."""
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


def _give_back_memory():
    """After an editor closes: free its decoded images and hand the memory back
    to the system (the watcher stays resident, so otherwise it would keep it)."""
    import ctypes
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass
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
            self.done_btn = Gtk.Button(label="Done", css_classes=["suggested-action"],
                                       tooltip_text="Save and copy to the clipboard (Ctrl+S)")
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
                    os.replace(tmp, path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                return pil_to_texture(im)

            def finished(tex, err):
                self._saving = False
                if err is not None:
                    self.done_btn.set_sensitive(True)
                    self.show_toast(f"Could not save: {err}")
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
                GLib.timeout_add(500, _give_back_memory)
                return False
            GLib.idle_add(free)
            return False

        def _on_close_request(self, *_):
            if self._saving:
                return True
            if self._finished or not self.viewer.has_unsaved_changes():
                return self._closing()
            dlg = Adw.AlertDialog(heading="Discard changes?",
                                  body="The screenshot and the clipboard keep the original.")
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("discard", "Discard")
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
