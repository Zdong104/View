"""Software updates: find a newer View release on GitHub and install it.

View checks once a day in the background (Preferences → Updates turns it
off) and whenever About or "Check for Updates…" is opened.  What "install"
means depends on how View was installed (``install_kind()``):

- Windows installer: download View-Setup-<v>.exe, run it silently (it
  closes View, updates in place and starts the new version).
- Windows portable zip, source checkout: open the download in the browser.
- .deb: download view_<v>_all.deb and install it with ``pkexec apt-get``.
- snap: snapd updates it; we only say so.

Every download is checked against the release's SHA256SUMS before it is run.
Older versions stay downloadable on the Releases page (``RELEASES``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import runtime  # noqa: E402
from .i18n import _

REPO = "Zdong104/View"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES = f"https://github.com/{REPO}/releases"
CHECK_EVERY = 24 * 3600           # automatic checks: at most once a day
TIMEOUT = 20
STATE_PATH = os.path.join(GLib.get_user_config_dir(), "view", "updates.json")


# ------------------------------------------------------------------ versions ---

def parse_version(v: str) -> tuple:
    """'v0.1.10' -> ((0, 1, 10), (1,)); a pre-release ('0.2.0-rc1') sorts
    before its release."""
    v = v.strip().lstrip("vV")
    main, _, pre = v.partition("-")
    nums = tuple(int(n) if n.isdigit() else 0 for n in main.split("+")[0].split("."))
    nums = nums + (0,) * (3 - len(nums))
    return nums, ((0, pre) if pre else (1,))


def is_newer(candidate: str, current: str = runtime.VERSION) -> bool:
    return parse_version(candidate) > parse_version(current)


# ---------------------------------------------------------- install method ---

def install_kind() -> str:
    """'windows-installer', 'windows-portable', 'deb', 'snap' or 'source'."""
    if runtime.IS_WINDOWS and runtime.FROZEN:
        here = os.path.dirname(sys.executable)
        return "windows-installer" if os.path.exists(os.path.join(here, "unins000.exe")) \
            else "windows-portable"
    if runtime.IN_SNAP:
        return "snap"
    if runtime.IS_LINUX and runtime.package_root() == "/usr/lib/view":
        return "deb"
    return "source"


def asset_name(version: str, kind: str | None = None) -> str | None:
    kind = kind or install_kind()
    return {"windows-installer": f"View-Setup-{version}.exe",
            "windows-portable": f"View-{version}-windows-portable.zip",
            "deb": f"view_{version}_all.deb"}.get(kind)


# ------------------------------------------------------------------ network ---

class Release:
    def __init__(self, tag, notes="", page=None, assets=None):
        self.tag = tag
        self.version = tag.lstrip("vV")
        self.notes = notes or ""
        self.page = page or f"{RELEASES}/tag/{tag}"
        # name -> download URL; GitHub's URLs are predictable if the API is unavailable
        self.assets = assets or {}

    def asset_url(self, name):
        return self.assets.get(name) or f"{RELEASES}/download/{self.tag}/{name}"


_ssl_ctx = None


def ssl_context():
    """The system's trusted roots plus Mozilla's (certifi, bundled on Windows).
    Python only sees the roots already in the Windows store, and a fresh
    Windows installs most of them on demand (when Edge first needs one), so
    on its own it may not trust github.com yet."""
    global _ssl_ctx
    if _ssl_ctx is None:
        import ssl
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001 - optional on Linux: the system store is complete
            pass
        _ssl_ctx = ctx
    return _ssl_ctx


def _request(url, accept=None):
    headers = {"User-Agent": f"{runtime.APP_NAME}/{runtime.VERSION}"}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                  timeout=TIMEOUT, context=ssl_context())


def fetch_latest() -> Release:
    """The newest published (non pre-release) release.  Blocking."""
    url = os.environ.get("VIEW_UPDATE_URL") or API_LATEST
    try:
        with _request(url, "application/vnd.github+json") as r:
            data = json.load(r)
        return Release(data["tag_name"], data.get("body") or "", data.get("html_url"),
                       {a["name"]: a["browser_download_url"] for a in data.get("assets", [])})
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429):        # anything but GitHub's API rate limit
            raise
    # rate limited: /releases/latest redirects to /releases/tag/<tag>
    with _request(f"{RELEASES}/latest") as r:
        m = re.search(r"/releases/tag/([^/?#]+)$", r.geturl())
    if not m:
        raise RuntimeError("could not find the latest release")
    return Release(urllib.request.unquote(m.group(1)))


def _expected_sha256(release: Release, name: str) -> str:
    with _request(release.asset_url("SHA256SUMS")) as r:
        for line in r.read().decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                return parts[0].lower()
    raise RuntimeError(f"{name} is not listed in the release's SHA256SUMS")


def download(release: Release, name: str, progress, cancelled) -> str:
    """Download asset `name` into a new temporary folder, verify it against
    SHA256SUMS and return its path.  `progress(fraction or None)` is called
    from this (worker) thread; `cancelled()` aborts."""
    expected = _expected_sha256(release, name)
    _remove_old_downloads()
    folder = tempfile.mkdtemp(prefix="view-update-")
    os.chmod(folder, 0o755)                 # apt's _apt user must be able to read the .deb
    path = os.path.join(folder, name)
    sha = hashlib.sha256()
    with _request(release.asset_url(name)) as r, open(path, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancelled():
                raise InterruptedError
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            sha.update(chunk)
            done += len(chunk)
            progress(done / total if total else None)
    os.chmod(path, 0o644)
    if sha.hexdigest() != expected:
        os.unlink(path)
        raise RuntimeError("the download is damaged (checksum mismatch); try again later")
    return path


def _remove_old_downloads():
    import glob
    import shutil
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "view-update-*")):
        shutil.rmtree(d, ignore_errors=True)


# -------------------------------------------------------------------- state ---

def _load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(**changes):
    state = _load_state()
    state.update(changes)
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def auto_check_enabled() -> bool:
    if os.environ.get("VIEW_NO_UPDATE_CHECK") or install_kind() == "snap":
        return False
    return bool(_load_state().get("auto", True))


def set_auto_check(on: bool):
    _save_state(auto=bool(on))


def _plain_notes(md: str, limit=700) -> str:
    """Release notes (GitHub Markdown) as short plain text."""
    text = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    text = re.sub(r"^Downloads:.*$", "", text, flags=re.M)     # release.yml boilerplate
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)          # [text](url)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[*-]\s+", "• ", text, flags=re.M)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def cli() -> int:
    """``view-app --check-updates``: 0 = up to date, 2 = update available, 1 = error."""
    try:
        rel = fetch_latest()
    except Exception as e:  # noqa: BLE001
        print(f"Could not check for updates: {_short_error(e)}", file=sys.stderr)
        return 1
    kind = install_kind()
    print(f"Installed: {runtime.VERSION} ({kind})\nLatest:    {rel.version}  {rel.page}")
    if not is_newer(rel.version):
        print("Up to date.")
        return 0
    name = asset_name(rel.version, kind)
    print(f"Update available: {rel.asset_url(name) if name else rel.page}")
    return 2


# ----------------------------------------------------------------------- UI ---

class Updater:
    """One per application: background checks and the update dialogs."""

    def __init__(self, app):
        self.app = app
        self._checking = False
        self._notified = set()        # versions already announced this session
        self._busy = False            # downloading / installing

    # ---- checking ----
    def start(self):
        """Begin automatic checks (shortly after startup, then hourly see
        whether a day has passed)."""
        GLib.timeout_add_seconds(20, lambda: (self._auto_tick(), GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add_seconds(3600, self._auto_tick)

    def _auto_tick(self):
        if auto_check_enabled() and \
                time.time() - float(_load_state().get("last_check", 0)) >= CHECK_EVERY:
            self.check(self._auto_result)
        return GLib.SOURCE_CONTINUE

    def check(self, done):
        """Look for a newer release in a thread; `done(release or None, error
        or None)` runs on the main loop (release None = up to date)."""
        if self._checking:
            return
        self._checking = True

        def work():
            try:
                rel, err = fetch_latest(), None
                _save_state(last_check=time.time())
            except Exception as e:  # noqa: BLE001
                rel, err = None, e

            def deliver():
                self._checking = False
                newer = rel if rel is not None and is_newer(rel.version) else None
                done(newer, err)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(deliver)
        threading.Thread(target=work, daemon=True, name="view-update-check").start()

    def _auto_result(self, rel, err):
        if rel is None or rel.version in self._notified or \
                rel.version == _load_state().get("skipped"):
            return
        self._notified.add(rel.version)
        win = self.app._active_main()
        if win is not None:
            win.show_toast(_("{APP_NAME} {version} is available").format(APP_NAME=runtime.APP_NAME, version=rel.version), _("Update…"),
                           lambda: self.offer(rel, win))

    def check_now(self, parent, toast=None):
        """Manual check ("Check for Updates…", About): result as a toast on
        `toast` (a window with show_toast, or an Adw dialog with add_toast)."""
        if not hasattr(toast, "add_toast"):            # Adw.AboutDialog before 1.6
            toast = parent

        def say(msg, button=None, cb=None):
            if hasattr(toast, "show_toast"):
                toast.show_toast(msg, button, cb)
            else:
                t = Adw.Toast(title=msg, timeout=6 if button else 3)
                if button:
                    t.set_button_label(button)
                    t.connect("button-clicked", lambda *_: cb())
                toast.add_toast(t)

        def done(rel, err):
            if err is not None:
                say(_("Could not check for updates: {error}").format(error=_short_error(err)))
            elif rel is None:
                say(_("{APP_NAME} {version} is up to date").format(
                    APP_NAME=runtime.APP_NAME, version=runtime.VERSION))
            else:
                self._notified.add(rel.version)
                say(_("{APP_NAME} {version} is available").format(
                    APP_NAME=runtime.APP_NAME, version=rel.version), _("Update…"),
                    lambda: self.offer(rel, parent))
        self.check(done)

    # ---- offering ----
    def offer(self, rel: Release, parent):
        """The "update available" dialog for `rel`."""
        kind = install_kind()
        notes = _plain_notes(rel.notes)
        body = _("You have version {version}.").format(version=runtime.VERSION)
        if notes:
            body += "\n\n" + GLib.markup_escape_text(notes)
        if kind == "snap":
            body += "\n\n" + _(
                "The Snap Store installs updates automatically. To update now, run "
                "{command}.").format(command="<tt>sudo snap refresh {}</tt>".format(
                    GLib.markup_escape_text(os.environ.get("SNAP_INSTANCE_NAME", "view"))))
        body += (f'\n\n<a href="{GLib.markup_escape_text(rel.page)}">'
                 f'{GLib.markup_escape_text(_("Release notes"))}</a> · '
                 f'<a href="{RELEASES}">{GLib.markup_escape_text(_("All versions"))}</a>')
        dlg = Adw.AlertDialog(heading=_("{APP_NAME} {version} Is Available").format(APP_NAME=runtime.APP_NAME, version=rel.version),
                              body=body, body_use_markup=True)
        dlg.add_response("later", _("Later"))
        dlg.add_response("skip", _("Skip This Version"))
        if kind in ("windows-installer", "deb"):
            dlg.add_response("install", _("Install and Restart") if kind != "deb" else _("Install…"))
        elif kind != "snap":
            dlg.add_response("download", _("Download"))
        if dlg.has_response("install") or dlg.has_response("download"):
            last = "install" if dlg.has_response("install") else "download"
            dlg.set_response_appearance(last, Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response(last)
        dlg.set_close_response("later")

        def response(_d, rid):
            if rid == "skip":
                _save_state(skipped=rel.version)
            elif rid == "download":
                name = asset_name(rel.version, kind)
                _launch_uri(rel.asset_url(name) if name else rel.page, parent)
            elif rid == "install":
                self._install(rel, kind, parent)
        dlg.connect("response", response)
        dlg.present(parent)

    # ---- installing ----
    def _install(self, rel, kind, parent):
        if self._busy:
            return
        if kind == "windows-installer" and self._unsaved(parent):
            return
        self._busy = True
        name = asset_name(rel.version, kind)
        prog = _ProgressDialog(_("Downloading {APP_NAME} {version}").format(
            APP_NAME=runtime.APP_NAME, version=rel.version))
        prog.present(parent)

        def work():
            try:
                path = download(rel, name, prog.set_fraction, prog.is_cancelled)
                if kind == "deb":
                    GLib.idle_add(prog.set_text, _("Installing…"))
                    rc, out = _apt_install(path)
                    result = ("deb", rc, out, path)
                else:
                    result = ("exe", 0, "", path)
            except InterruptedError:
                result = None
            except Exception as e:  # noqa: BLE001
                result = ("error", 0, _short_error(e), None)
            GLib.idle_add(finish, result)

        def finish(result):
            self._busy = False
            prog.close()
            if result is None:
                return GLib.SOURCE_REMOVE
            what, rc, out, path = result
            if what == "error":
                _message(parent, _("Update Failed"), out)
            elif what == "exe":
                if not self._unsaved(parent):
                    try:
                        _run_windows_installer(path)
                    except OSError as e:        # e.g. the UAC prompt was declined
                        _message(parent, _("Update Not Installed"), _short_error(e))
                    else:
                        self.app.quit()
            elif rc == 0:
                self._restart_dialog(parent, rel)
            elif rc in (126, 127) and _have("pkexec"):
                pass                                    # authentication cancelled
            elif not _have("pkexec"):
                # no polkit agent tool: hand the .deb to the software installer
                _launch_uri(GLib.filename_to_uri(path), parent)
            else:
                _message(parent, _("Update Failed"),
                         _("apt could not install the update:") + "\n\n" + out[-600:])
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True, name="view-update").start()

    def _unsaved(self, parent) -> bool:
        """True (and says so) if a window has unsaved changes."""
        for w in self.app.get_windows():
            v = getattr(w, "viewer", None)
            try:
                if v is not None and v.has_unsaved_changes():
                    _message(parent, _("Unsaved Changes"),
                             _("Save or close the documents with unsaved changes, then try "
                               "again. The update has to close {APP_NAME}.").format(
                                 APP_NAME=runtime.APP_NAME))
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def _restart_dialog(self, parent, rel):
        dlg = Adw.AlertDialog(heading=_("{APP_NAME} Was Updated").format(APP_NAME=runtime.APP_NAME),
                              body=_("Version {version} is installed. Restart {APP_NAME} to use it.").format(version=rel.version, APP_NAME=runtime.APP_NAME))
        dlg.add_response("later", _("Later"))
        dlg.add_response("restart", _("Restart"))
        dlg.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("restart")
        dlg.set_close_response("later")

        def response(_d, rid):
            if rid == "restart" and not self._unsaved(parent):
                _relaunch_after_exit()
                self.app.quit()
        dlg.connect("response", response)
        dlg.present(parent)


class _ProgressDialog(Adw.Dialog):
    def __init__(self, title):
        super().__init__(title=title, content_width=380, can_close=False)
        self._cancelled = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18,
                      margin_bottom=18, margin_start=18, margin_end=18)
        self._label = Gtk.Label(label=title + "…", xalign=0, wrap=True)
        self._bar = Gtk.ProgressBar()
        self._cancel = Gtk.Button(label=_("Cancel"), halign=Gtk.Align.END)
        self._cancel.connect("clicked", lambda *_: self._on_cancel())
        for w in (self._label, self._bar, self._cancel):
            box.append(w)
        self.set_child(box)
        self._pulse = GLib.timeout_add(150, self._tick)
        self._fraction = None
        self.connect("closed", lambda *_: GLib.source_remove(self._pulse))

    def _tick(self):
        self._bar.set_show_text(self._fraction is not None)
        if self._fraction is None:
            self._bar.pulse()
        else:
            self._bar.set_fraction(self._fraction)
        return GLib.SOURCE_CONTINUE

    def set_fraction(self, f):          # any thread; read by _tick
        self._fraction = f

    def set_text(self, text):
        self._label.set_label(text)
        self._fraction = None
        self._cancel.set_sensitive(False)
        return GLib.SOURCE_REMOVE

    def is_cancelled(self):
        return self._cancelled

    def _on_cancel(self):
        self._cancelled = True
        self._cancel.set_sensitive(False)

    def close(self):
        self.set_can_close(True)
        super().close()


# ------------------------------------------------------------------ helpers ---

def _short_error(e) -> str:
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        import ssl
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            return _("GitHub’s certificate could not be verified ({reason})").format(
                reason=e.reason.verify_message)
        return _("no connection to GitHub ({reason})").format(reason=e.reason)
    return str(e) or type(e).__name__


def _have(cmd) -> bool:
    import shutil
    return shutil.which(cmd) is not None


def _message(parent, heading, body):
    dlg = Adw.AlertDialog(heading=heading, body=body)
    dlg.add_response("ok", _("OK"))
    dlg.present(parent)


def _launch_uri(uri, parent):
    root = parent.get_root() if hasattr(parent, "get_root") else parent
    Gtk.UriLauncher.new(uri).launch(root if isinstance(root, Gtk.Window) else None,
                                    None, None, None)


def _apt_install(path) -> tuple[int, str]:
    if not _have("pkexec"):
        return 127, _("pkexec not found")
    p = subprocess.run(["pkexec", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
                        "install", "-y", "--allow-downgrades", path],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout or ""


def _run_windows_installer(path):
    """Run the Inno Setup installer without its wizard: it shows progress,
    closes View if still running, installs over this copy (same per-user or
    all-users location) and starts View again (/RELAUNCH, see preview.iss)."""
    app_dir = os.path.normcase(os.path.dirname(sys.executable))
    program_files = [os.path.normcase(os.environ[v]) for v in
                     ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)") if os.environ.get(v)]
    scope = "/ALLUSERS" if any(app_dir.startswith(p + os.sep) for p in program_files) \
        else "/CURRENTUSER"
    args = f"/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RELAUNCH {scope}"
    os.startfile(path, "open", args)  # ShellExecute: the installer may need elevation


def _relaunch_after_exit():
    """Start View again once this process has exited (Linux)."""
    argv = runtime.launcher_argv()
    script = 'while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; exec "$@"'
    subprocess.Popen(["sh", "-c", script, str(os.getpid()), *argv],
                     env=runtime.launcher_env(), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
