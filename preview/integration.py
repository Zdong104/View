"""Opt-in integration with GNOME Files (Nautilus), per user.

* Quick Look: a user-level D-Bus service file for org.gnome.NautilusPreviewer
  (~/.local/share/dbus-1/services/…) that starts ``preview-app --quicklook``
  instead of GNOME Sushi when Space is pressed.  A system package cannot do
  this: the system-wide file belongs to gnome-sushi.
* "Combine Images into PDF": a Nautilus script (~/.local/share/nautilus/scripts/).

Both are written with absolute paths to the installed launcher, so they keep
working from the .deb, a source checkout or any other prefix.
"""
from __future__ import annotations

import os
import shlex
import signal
import sys

from . import runtime

BUS_NAME = "org.gnome.NautilusPreviewer"
SCRIPT_NAME = "Combine Images into PDF"
MARKER = f"Installed by Preview ({runtime.APP_ID})"


def _data_home():
    from gi.repository import GLib
    return GLib.get_user_data_dir()


def service_path() -> str:
    return os.path.join(_data_home(), "dbus-1", "services", BUS_NAME + ".service")


def script_path() -> str:
    return os.path.join(_data_home(), "nautilus", "scripts", SCRIPT_NAME)


def _is_ours(path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    return MARKER in head or "preview.quicklook" in head or "preview-quicklook" in head \
        or "preview.convert" in head


def quicklook_enabled() -> bool:
    return os.path.exists(service_path()) and _is_ours(service_path())


def script_enabled() -> bool:
    return os.path.exists(script_path()) and _is_ours(script_path())


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
# file-name order.  Remove with:  preview-app --disable-files-integration
set -u
LAUNCHER=(@LAUNCHER@)
@ENV@
notify() {
    command -v notify-send >/dev/null 2>&1 &&
        notify-send -a "Preview" -i application-pdf "$1" "${2:-}" 2>/dev/null
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


def set_script(enabled: bool):
    if enabled:
        _write(script_path(), _script_text(), 0o755)
    else:
        _remove(script_path())


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


def _owner_pid():
    from gi.repository import GLib, Gio
    try:
        bus = _session_bus()
        owner = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "GetNameOwner",
                              GLib.Variant("(s)", (BUS_NAME,)), GLib.VariantType("(s)"),
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
        apply(quicklook=enable, script=enable)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if enable:
        print(f"Space-bar previews in Files now use Preview ({service_path()}).")
        print(f"Added the Files script “{SCRIPT_NAME}” ({script_path()}).")
    else:
        print("Removed Preview's Files integration; GNOME Sushi handles Space again.")
    return 0
