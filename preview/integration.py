"""Opt-in integration with GNOME Files (Nautilus), per user.

* Quick Look: a user-level D-Bus service file for org.gnome.NautilusPreviewer
  (~/.local/share/dbus-1/services/…) that starts ``view-app --quicklook``
  instead of GNOME Sushi when Space is pressed.  A system package cannot do
  this: the system-wide file belongs to gnome-sushi.
* "Combine Images into PDF": a top-level right-click item from a nautilus-python
  extension (~/.local/share/nautilus-python/extensions/; the .deb ships a system
  copy instead).  Without python3-nautilus it falls back to a Nautilus script
  (~/.local/share/nautilus/scripts/, shown under Scripts).

Everything is written with absolute paths to the installed launcher, so it
keeps working from the .deb, a source checkout or any other prefix.
"""
from __future__ import annotations

import glob
import json
import os
import shlex
import signal
import sys

from . import runtime

BUS_NAME = "org.gnome.NautilusPreviewer"
SCRIPT_NAME = "Combine Images into PDF"
EXTENSION_FILE = "view-app.py"
EXTENSION_TEMPLATE = "view-nautilus-extension.py"
SYSTEM_EXTENSION = "/usr/share/nautilus-python/extensions/" + EXTENSION_FILE
MARKER = f"Installed by View ({runtime.APP_ID})"


def _data_home():
    from gi.repository import GLib
    return GLib.get_user_data_dir()


def service_path() -> str:
    return os.path.join(_data_home(), "dbus-1", "services", BUS_NAME + ".service")


def script_path() -> str:
    return os.path.join(_data_home(), "nautilus", "scripts", SCRIPT_NAME)


def extension_path() -> str:
    return os.path.join(_data_home(), "nautilus-python", "extensions", EXTENSION_FILE)


def system_extension_installed() -> bool:
    """The .deb's system-wide extension is present (it makes a per-user one redundant)."""
    return os.path.exists(SYSTEM_EXTENSION)


def has_nautilus_python() -> bool:
    """python3-nautilus (the nautilus-python extension loader for Files 43+) is installed."""
    for lib in ("/usr/lib/*/nautilus/extensions-4", "/usr/lib/nautilus/extensions-4",
                "/usr/lib64/nautilus/extensions-4"):
        if glob.glob(os.path.join(lib, "libnautilus-python.so*")):
            return True
    return False


def _is_ours(path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    # (also files written by pre-release builds under the name "Preview")
    return MARKER in head or "Installed by Preview" in head or "preview.quicklook" in head \
        or "preview-quicklook" in head or "preview.convert" in head


def quicklook_enabled() -> bool:
    return os.path.exists(service_path()) and _is_ours(service_path())


def script_enabled() -> bool:
    """"Combine Images into PDF" is available in Files (extension or script)."""
    return (system_extension_installed()
            or (os.path.exists(extension_path()) and _is_ours(extension_path()))
            or (os.path.exists(script_path()) and _is_ours(script_path())))


# ------------------------------------------------------------- writers ---

def _dbus_exec_line() -> str:
    """Exec= value for the D-Bus service (dbus uses shell-like quoting)."""
    argv = runtime.launcher_argv() + ["--quicklook"]
    env = runtime.launcher_env()
    if env is not None:                     # source checkout without a wrapper
        argv = ["/usr/bin/env", f"PYTHONPATH={env['PYTHONPATH']}"] + argv
    return " ".join(shlex.quote(a) for a in argv)


def _write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _remove(path):
    if os.path.exists(path) and _is_ours(path):
        os.unlink(path)
        return True
    return False


SCRIPT_TEMPLATE = r'''#!/usr/bin/env bash
# @MARKER@
# GNOME Files script: combine the selected images into one PDF, in natural
# file-name order.  Remove with:  view-app --disable-files-integration
set -u
LAUNCHER=(@LAUNCHER@)
@ENV@
notify() {
    command -v notify-send >/dev/null 2>&1 &&
        notify-send -a "View" -i application-pdf "$1" "${2:-}" 2>/dev/null
    return 0
}

mapfile -t FILES < <(printf '%s\n' "${NAUTILUS_SCRIPT_SELECTED_FILE_PATHS:-}" | sed '/^$/d')
if [ "$#" -gt 0 ] && [ "${#FILES[@]}" -eq 0 ]; then
    FILES=("$@")
fi
# natural sort by file name (not full path), regular files only
mapfile -t FILES < <(
    for f in "${FILES[@]}"; do [ -f "$f" ] && printf '%s\t%s\n' "$(basename "$f")" "$f"; done \
        | sort -V -t $'\t' -k1,1 | cut -f2-
)
if [ "${#FILES[@]}" -eq 0 ]; then
    notify "Combine Images into PDF" "Select one or more image files."
    exit 1
fi

DIR="$(dirname "${FILES[0]}")"
NAME="$(basename "${FILES[0]}")"
OUT="$DIR/${NAME%.*}.pdf"
n=2
while [ -e "$OUT" ]; do OUT="$DIR/${NAME%.*} $n.pdf"; n=$((n + 1)); done

if ERR="$("${LAUNCHER[@]}" --combine-pdf -o "$OUT" "${FILES[@]}" 2>&1 >/dev/null \
          | grep -v '^\[' | tail -n 3)" && [ -s "$OUT" ]; then
    notify "Created $(basename "$OUT")" "${#FILES[@]} image(s) combined in $DIR"
    (gio open "$OUT" || xdg-open "$OUT") >/dev/null 2>&1 &
else
    rm -f "$OUT"
    notify "Could not create PDF" "${ERR:-Unknown error}"
    exit 1
fi
'''


def _script_text() -> str:
    env = runtime.launcher_env()
    env_line = (f"export PYTHONPATH={shlex.quote(env['PYTHONPATH'])}" if env is not None else "")
    return (SCRIPT_TEMPLATE.replace("@MARKER@", MARKER)
            .replace("@LAUNCHER@", " ".join(shlex.quote(a) for a in runtime.launcher_argv()))
            .replace("@ENV@", env_line))


def _extension_source() -> str:
    """Text of the extension template: next to the package (``data/`` in a source
    checkout; the .deb and the snap install a copy there too)."""
    path = os.path.join(runtime.package_root(), "data", EXTENSION_TEMPLATE)
    with open(path, encoding="utf-8") as f:
        return f.read()


def extension_text() -> str:
    """The nautilus-python extension with this install's launcher baked in."""
    env = runtime.launcher_env()
    return (_extension_source()
            .replace("LAUNCHER = []  # @LAUNCHER@",
                     f"LAUNCHER = {json.dumps(runtime.launcher_argv())}")
            .replace('ENV_PYTHONPATH = ""  # @ENV_PYTHONPATH@',
                     f"ENV_PYTHONPATH = {json.dumps(env['PYTHONPATH'] if env else '')}"))


def print_extension() -> int:
    """--print-files-extension: for sandboxed installs that cannot write ~/.local."""
    try:
        sys.stdout.write(extension_text())
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def set_script(enabled: bool) -> str:
    """Turn the "Combine Images into PDF" menu entry on or off.  Returns what is
    now providing it: "system" (the .deb's extension), "extension", "script",
    or "" when disabled."""
    if not enabled:
        _remove(extension_path())
        _remove(script_path())
        return ""
    if system_extension_installed():
        _remove(extension_path())
        _remove(script_path())
        return "system"
    if has_nautilus_python():
        _write(extension_path(), extension_text())
        _remove(script_path())
        return "extension"
    _write(script_path(), _script_text(), 0o755)
    return "script"


def set_quicklook(enabled: bool):
    if enabled:
        _write(service_path(),
               f"# {MARKER}\n[D-BUS Service]\nName={BUS_NAME}\nExec={_dbus_exec_line()}\n")
        _reload_bus()
        _stop_owner(want_ours=False)        # Sushi keeps the name until it exits
    else:
        removed = _remove(service_path())
        _reload_bus()
        if removed:
            _stop_owner(want_ours=True)     # let Sushi activate on the next Space


# ------------------------------------------------------------ D-Bus bits ---

def _session_bus():
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _reload_bus():
    from gi.repository import Gio
    try:
        _session_bus().call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                 "org.freedesktop.DBus", "ReloadConfig", None, None,
                                 Gio.DBusCallFlags.NONE, 3000, None)
    except Exception:  # noqa: BLE001
        pass


def _owner_pid(name=BUS_NAME):
    from gi.repository import GLib, Gio
    try:
        bus = _session_bus()
        owner = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "GetNameOwner",
                              GLib.Variant("(s)", (name,)), GLib.VariantType("(s)"),
                              Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        pid = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                            "org.freedesktop.DBus", "GetConnectionUnixProcessID",
                            GLib.Variant("(s)", (owner,)), GLib.VariantType("(u)"),
                            Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        return int(pid)
    except Exception:  # noqa: BLE001  (name not owned)
        return None


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _stop_owner(want_ours: bool):
    """Terminate the current owner of org.gnome.NautilusPreviewer if it is
    Sushi (want_ours=False) or our own Quick Look (want_ours=True)."""
    pid = _owner_pid()
    if not pid or pid == os.getpid():
        return
    cmd = _cmdline(pid)
    ours = "--quicklook" in cmd or "preview.quicklook" in cmd
    if ours == want_ours:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# ----------------------------------------------------------------- CLI ---

def apply(quicklook: bool | None = None, script: bool | None = None):
    if quicklook is not None:
        set_quicklook(quicklook)
    if script is not None:
        set_script(script)


def cli(enable: bool) -> int:
    ok, reason = runtime.files_integration_supported()
    if enable and not ok:
        print(f"Files integration is not available: {reason}", file=sys.stderr)
        return 1
    try:
        set_quicklook(enable)
        kind = set_script(enable)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if enable:
        print(f"Space-bar previews in Files now use View ({service_path()}).")
        if kind == "system":
            print(f"“{SCRIPT_NAME}” is in the Files right-click menu (system extension).")
        elif kind == "extension":
            print(f"Added “{SCRIPT_NAME}” to the Files right-click menu ({extension_path()}).")
        else:
            print(f"Added the Files script “{SCRIPT_NAME}” ({script_path()}).")
            print("Install python3-nautilus for a top-level menu item.")
        if kind in ("system", "extension"):
            print("Restart Files to load it:  nautilus -q")
    else:
        print("Removed View's Files integration; GNOME Sushi handles Space again.")
    return 0
