# Installed by View (io.github.zdong104.View)
# GNOME Files (nautilus-python) extension: adds "Combine Images into PDF" to the
# right-click menu of selected images, in natural file-name order.  Needs
# python3-nautilus; restart Files afterwards with:  nautilus -q
# Remove with:  view-app --disable-files-integration  (or delete this file)
import os
import re
import shutil
import sys

import gi

try:
    gi.require_version("Nautilus", "4.1")      # Files 49+
except ValueError:
    gi.require_version("Nautilus", "4.0")      # Files 43–48
from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402

# Filled in by View's installers (view-app --print-files-extension, the .deb);
# if empty or stale, view-app is looked up in the usual places.
LAUNCHER = []  # @LAUNCHER@
ENV_PYTHONPATH = ""  # @ENV_PYTHONPATH@

_FALLBACKS = ("~/.local/bin/view-app", "/usr/bin/view-app", "/snap/bin/view")


def _launcher():
    if LAUNCHER and os.access(LAUNCHER[0], os.X_OK):
        return list(LAUNCHER)
    found = shutil.which("view-app")
    for cand in ([found] if found else []) + [os.path.expanduser(p) for p in _FALLBACKS]:
        if os.access(cand, os.X_OK):
            return [cand]
    return None


def _natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _output_path(first):
    """DIR/<stem>.pdf, or DIR/<stem> 2.pdf, 3.pdf … if that exists."""
    folder, name = os.path.split(first)
    stem = os.path.splitext(name)[0]
    out, n = os.path.join(folder, stem + ".pdf"), 2
    while os.path.exists(out):
        out = os.path.join(folder, f"{stem} {n}.pdf")
        n += 1
    return out


def _notify(title, body=""):
    if not shutil.which("notify-send"):
        return
    try:
        GLib.spawn_async(
            ["notify-send", "-a", "View", "-i", "application-pdf", title, body],
            flags=(GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_DEV_NULL
                   | GLib.SpawnFlags.STDERR_TO_DEV_NULL))
    except GLib.Error:
        pass


def _combine(paths):
    """Run `view-app --combine-pdf` on the images without blocking Files."""
    argv = _launcher()
    if not argv:
        return
    paths = sorted(paths, key=lambda p: _natural_key(os.path.basename(p)))
    out = _output_path(paths[0])
    launcher = Gio.SubprocessLauncher.new(
        Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE)
    if ENV_PYTHONPATH:
        launcher.setenv("PYTHONPATH", ENV_PYTHONPATH, True)
    try:
        proc = launcher.spawnv(argv + ["--combine-pdf", "-o", out] + paths)
    except GLib.Error as e:
        _notify("Could not create PDF", e.message)
        return

    def done(_proc, res):
        try:
            _ok, _stdout, stderr = proc.communicate_utf8_finish(res)
        except GLib.Error as e:
            stderr = e.message
        folder = os.path.dirname(out)
        if proc.get_if_exited() and proc.get_exit_status() == 0 \
                and os.path.exists(out) and os.path.getsize(out) > 0:
            _notify(f"Created {os.path.basename(out)}",
                    f"{len(paths)} image(s) combined in {folder}")
            try:
                Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(out, None), None)
            except GLib.Error:
                pass
        else:
            try:
                os.unlink(out)
            except OSError:
                pass
            lines = [l for l in (stderr or "").splitlines() if l.strip() and not l.startswith("[")]
            _notify("Could not create PDF", "\n".join(lines[-3:]) or "Unknown error")

    proc.communicate_utf8_async(None, None, done)


def _is_local_image(f):
    return (f.get_uri_scheme() == "file" and not f.is_directory()
            and (f.get_mime_type() or "").startswith("image/"))


# nautilus-python loads ~/.local/share/nautilus-python/extensions and
# /usr/share/nautilus-python/extensions into one interpreter: if a user copy and
# the .deb's copy both exist, only the first one gets to define the provider
# (nautilus-python registers every class it finds in a module's globals).
if not getattr(sys, "_view_app_menu_loaded", False):
    sys._view_app_menu_loaded = True

    class ViewCombinePdfProvider(GObject.GObject, Nautilus.MenuProvider):
        def get_file_items(self, files):
            if not files or not all(_is_local_image(f) for f in files) or not _launcher():
                return []
            paths = [f.get_location().get_path() for f in files]
            if len(paths) == 1:
                label = "Convert to PDF"
            else:
                label = f"Combine {len(paths)} Images into PDF"
            item = Nautilus.MenuItem(name="ViewApp::CombinePdf", label=label)
            item.connect("activate", lambda _item: _combine(paths))
            return [item]
