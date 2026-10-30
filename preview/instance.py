"""One View per user session, without D-Bus, and the background (resident) mode.

GApplication's uniqueness needs our ID owned on the session bus, and the Snap
Store holds every upload with a dbus slot for manual review.  So View runs
NON_UNIQUE everywhere and does it itself, before GTK is even imported:

  Linux    a lock file and a Unix socket, $XDG_RUNTIME_DIR/view/main.{lock,sock}
           (in the snap that is /run/user/UID/snap.view/view, writable under
           strict confinement).  Outside the snap the name carries a hash of the
           package directory, so a source checkout and the .deb don't meet.
  Windows  a named pipe, \\\\.\\pipe\\io.github.zdong104.View-<session>-<user>,
           and a lock file in %LOCALAPPDATA%\\view.

A launch that only opens files (or nothing, or --new-signature) first tries
to hand them to the running View: one JSON line {version, cwd, args, env},
answered {ok, pid} (or {restart} when the running View is another version and
has no window open: it quits and the new one starts).  The first launch takes
the lock, listens, and queues what arrives until the app is ready for it.

stdlib only: this runs before i18n (GLib) and GTK, so a forwarded open costs
little more than Python's own start.

VIEW_NO_FORWARD=1 runs a separate, standalone View.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import runtime

FORWARDED, PRIMARY, STANDALONE = "forwarded", "primary", "standalone"
PROTOCOL = 1
LOCK_AT = 4096            # Windows locks a byte range: past the PID text
_MAX_REQUEST = 1 << 20
_ENV = ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID", "VIEW_OPEN_T0")
RETRY_S = 0.05            # a View that is starting: ask again this often…
WAIT_S = 5.0              # …for this long, then run on our own
ACK_S = 5.0               # how long the running View has to answer

server: "Server | None" = None    # set when this process is the primary


# ------------------------------------------------------------------ locks ---

def try_lock(f, at=LOCK_AT) -> bool:
    """Take an exclusive lock on the open file `f` without waiting (also used
    by the screenshot watcher).  False if another process holds it."""
    try:
        if runtime.IS_WINDOWS:
            import msvcrt
            f.seek(at)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def read_pid(path) -> int | None:
    """The PID a lock holder wrote at the start of its lock file."""
    try:
        # unbuffered: a buffered read asks Windows for 8 KiB, which reaches the
        # locked byte (LOCK_AT) and fails as a whole
        with open(path, "rb", buffering=0) as f:
            return int(f.read(32).split()[0])
    except (OSError, ValueError, IndexError):
        return None


# -------------------------------------------------------------- addresses ---

def supported() -> bool:
    return runtime.IS_LINUX or runtime.IS_WINDOWS


def run_dir() -> str:
    """Where the lock (and on Linux the socket) live: GLib's user runtime dir,
    computed without GLib (it falls back to the cache dir, so do we)."""
    if runtime.IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_CACHE_HOME")
                or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(base, "view")


def _key() -> str:
    """"main" for the snap and the Windows build; elsewhere also a hash of
    the package directory (a dev checkout and the .deb run side by side)."""
    if runtime.IN_SNAP or runtime.FROZEN:
        return "main"
    import hashlib
    root = os.path.realpath(runtime.package_root())
    return "main-" + hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()[:8]


def lock_path() -> str:
    return os.path.join(run_dir(), _key() + ".lock")


def address() -> str:
    if runtime.IS_WINDOWS:
        user = "".join(c if c.isalnum() else "_" for c in os.environ.get("USERNAME", "user"))
        suffix = _key()[4:]                 # "" or "-<hash>"
        return rf"\\.\pipe\{runtime.APP_ID}-{_session_id()}-{user}{suffix}"
    return os.path.join(run_dir(), _key() + ".sock")


def _session_id() -> int:
    try:
        import ctypes
        sid = ctypes.c_ulong()
        k32 = ctypes.windll.kernel32
        if k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid)):
            return sid.value
    except Exception:  # noqa: BLE001
        pass
    return 0


# ---------------------------------------------------------- connections ---

class _Pipe:
    """Windows: the client end of the running View's pipe, with _winapi alone
    (what multiprocessing.connection uses underneath).  Importing that module
    costs a forwarded launch some 20-40 ms more, and its Client() keeps
    retrying a busy pipe for 20 s.  One message per send / receive (the
    server's PipeConnection is in message mode); every wait has a timeout."""

    def __init__(self, addr, timeout):
        import _winapi as w
        self._w = w
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.h = w.CreateFile(addr, w.GENERIC_READ | w.GENERIC_WRITE, 0, w.NULL,
                                      w.OPEN_EXISTING, w.FILE_FLAG_OVERLAPPED, w.NULL)
                break
            except OSError as e:
                if getattr(e, "winerror", None) != w.ERROR_PIPE_BUSY or time.monotonic() > deadline:
                    raise
            try:
                w.WaitNamedPipe(addr, 100)
            except OSError:
                pass
        try:
            w.SetNamedPipeHandleState(self.h, w.PIPE_READMODE_MESSAGE, None, None)
        except OSError:
            self.close()
            raise

    def _finish(self, ov, err, timeout):
        """Wait for an overlapped read/write (cancelled after `timeout`)."""
        w = self._w
        try:
            if err == w.ERROR_IO_PENDING:
                if w.WaitForMultipleObjects([ov.event], False,
                                            int(timeout * 1000)) != w.WAIT_OBJECT_0:
                    ov.cancel()
        finally:
            _n, err = ov.GetOverlappedResult(True)
        return err

    def send_bytes(self, data, timeout=ACK_S):
        ov, err = self._w.WriteFile(self.h, data, overlapped=True)
        if self._finish(ov, err, timeout) != 0:
            raise OSError("the running View did not take the request")

    def recv_message(self, timeout):
        """One message, or None on timeout (or one too long for us)."""
        ov, err = self._w.ReadFile(self.h, 65536, overlapped=True)
        if self._finish(ov, err, timeout) != 0:
            return None
        return bytes(ov.getbuffer())

    def close(self):
        h, self.h = getattr(self, "h", None), None
        if h is not None:
            self._w.CloseHandle(h)


class _Conn:
    """One request/answer exchange of JSON objects: newline-terminated over a
    Unix socket, one message per send_bytes() over a Windows pipe (never
    pickle: anyone in the session can connect)."""

    def __init__(self, raw):
        self.raw = raw
        self._buf = b""

    @classmethod
    def connect(cls, addr, timeout):
        """None when nobody listens at `addr`.  PermissionError: somebody
        does, but we may not talk to it (an elevated View, for one that is not)."""
        if runtime.IS_WINDOWS:
            try:
                return cls(_Pipe(addr, timeout))
            except PermissionError:
                raise
            except OSError:
                return None
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(addr)
        except PermissionError:
            s.close()
            raise
        except OSError:
            s.close()
            return None
        return cls(s)

    def send(self, obj):
        data = json.dumps(obj).encode()
        if runtime.IS_WINDOWS:
            self.raw.send_bytes(data)
        else:
            self.raw.sendall(data + b"\n")

    def recv(self, timeout):
        """The next object, or None on timeout / a closed connection."""
        try:
            if isinstance(self.raw, _Pipe):                  # Windows, client
                data = self.raw.recv_message(timeout)
                if data is None:
                    return None
            elif runtime.IS_WINDOWS:                         # Windows, server
                if not self.raw.poll(timeout):
                    return None
                data = self.raw.recv_bytes(_MAX_REQUEST)
            else:
                self.raw.settimeout(timeout)
                while b"\n" not in self._buf:
                    chunk = self.raw.recv(65536)
                    if not chunk:
                        return None
                    self._buf += chunk
                    if len(self._buf) > _MAX_REQUEST:
                        return None
                data, self._buf = self._buf.split(b"\n", 1)
            obj = json.loads(data)
            return obj if isinstance(obj, dict) else None
        except (OSError, EOFError, ValueError):
            return None

    def close(self):
        try:
            self.raw.close()
        except OSError:
            pass


def _same_user(sock) -> bool:
    """Linux: the peer runs as us (the socket's directory is private anyway)."""
    try:
        import socket
        import struct
        cred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", cred)[1] == os.getuid()
    except (OSError, AttributeError):
        return True


# ------------------------------------------------------------------ client ---

def wanted(args) -> bool:
    """Hand this launch to a running View?  Only plain opens: files, URIs,
    nothing, or --new-signature (never --startup-trace, --selftest, --help…)."""
    if os.environ.get("VIEW_NO_FORWARD") or not supported():
        return False
    return all(a == "--new-signature" or not a.startswith("-") for a in args)


def _request(args) -> dict:
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    return {"version": runtime.VERSION, "protocol": PROTOCOL, "cwd": cwd, "args": list(args),
            "env": {k: os.environ[k] for k in _ENV if os.environ.get(k)}}


def _allow_foreground():
    """Windows lets only the foreground process bring a window to the front:
    pass that right to the running View (its PID is in the lock file)."""
    try:
        import ctypes
        pid = read_pid(lock_path())
        ctypes.windll.user32.AllowSetForegroundWindow(pid if pid else -1)   # -1: ASFW_ANY
    except Exception:  # noqa: BLE001
        pass


def _forward(req):
    """Send `req` to the running View: its answer, or None (nobody there, or
    it went away before answering)."""
    try:
        conn = _Conn.connect(address(), ACK_S)
    except PermissionError:
        return {"denied": True}
    if conn is None:
        return None
    try:
        if runtime.IS_WINDOWS:
            _allow_foreground()
        conn.send(req)
        reply = conn.recv(ACK_S)
        if reply and reply.get("ok") and "VIEW_OPEN_T0" in req["env"]:
            shown = conn.recv(60)              # the open-latency measurement
            if shown and "shown" in shown:
                print(f"View: first frame {shown['shown']:.3f} s after VIEW_OPEN_T0 "
                      f"(opened in the running View, pid {reply.get('pid')})",
                      file=sys.stderr, flush=True)
        return reply
    except OSError:
        return None
    finally:
        conn.close()


def claim(args) -> str:
    """FORWARDED: a running View took `args`, exit.  PRIMARY: this process is
    the one (``server`` listens; run the app).  STANDALONE: neither worked in
    time; run the app on its own."""
    global server
    req = _request(args)
    deadline = time.monotonic() + WAIT_S
    while True:
        reply = _forward(req)
        if reply is not None and reply.get("ok"):
            return FORWARDED
        if reply is not None and reply.get("denied"):
            return STANDALONE           # (not 5 s of retrying first)
        if reply is not None and reply.get("restart"):
            deadline = time.monotonic() + WAIT_S    # the other version is quitting
        try:
            lock = _take_lock()
        except OSError:
            return STANDALONE
        if lock is not None:
            try:
                server = Server(address(), lock)
            except OSError as e:
                print(f"View: cannot listen for other launches: {e}", file=sys.stderr)
                lock.close()
                return STANDALONE
            return PRIMARY
        if time.monotonic() > deadline:
            return STANDALONE
        time.sleep(RETRY_S)


def _take_lock():
    """The open lock file with our PID in it, or None if a View holds it."""
    d = run_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    f = open(lock_path(), "a+b")
    if not try_lock(f):
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n".encode())
    f.flush()
    return f


# ------------------------------------------------------------------ server ---

class Request:
    """One forwarded launch, handed to the app on its main thread."""

    def __init__(self, data, conn=None):
        self.args = [a for a in data.get("args") or [] if isinstance(a, str)]
        self.cwd = data.get("cwd") or None
        env = data.get("env") or {}
        self.env = {k: str(env[k]) for k in _ENV if env.get(k)}
        self._conn = conn                    # kept open only to report VIEW_OPEN_T0

    @property
    def token(self) -> str:
        """The activation token of the launch (Wayland, else X11 startup id)."""
        return self.env.get("XDG_ACTIVATION_TOKEN") or self.env.get("DESKTOP_STARTUP_ID") or ""

    def report_shown(self):
        """First frame of the window this launch opened: tell a waiting
        client (open-latency.sh) how long it took since VIEW_OPEN_T0."""
        conn, self._conn = self._conn, None
        try:
            secs = time.time() - float(self.env["VIEW_OPEN_T0"])
        except (KeyError, ValueError):
            secs = None
        if secs is not None:
            print(f"View: first frame {secs:.3f} s after VIEW_OPEN_T0 (forwarded open)",
                  file=sys.stderr, flush=True)
        if conn is not None:
            try:
                if secs is not None:
                    conn.send({"shown": secs})
            except OSError:
                pass
            conn.close()


class Server:
    """Listens for other launches (a thread) and queues them until the app
    installs its handler with set_handler()."""

    def __init__(self, addr, lock=None):
        self.address = addr
        self.lock = lock                     # held (open) until the process exits
        self.idle = False                    # the app: True while no window is open
        self.on_restart = None               # the app: quit (called from the thread)
        self._handler = None
        self._queue = []
        import threading
        self._mutex = threading.Lock()
        self._closed = False
        self._ino = None
        self._listener = self._listen()
        import atexit
        atexit.register(self.close)
        threading.Thread(target=self._serve, daemon=True, name="view-instance").start()

    def _listen(self):
        if runtime.IS_WINDOWS:
            from multiprocessing.connection import Listener
            return Listener(self.address, family="AF_PIPE")
        import socket
        try:
            os.unlink(self.address)          # left behind by a View that crashed
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(self.address)
            os.chmod(self.address, 0o600)
            self._ino = os.stat(self.address).st_ino
            s.listen(16)
        except OSError:
            s.close()
            raise
        return s

    def set_handler(self, fn):
        """`fn(Request)` is called on the listening thread from now on (the
        app passes it to its main loop); queued requests are handed over now."""
        with self._mutex:
            self._handler = fn
            queued, self._queue = self._queue, []
        for r in queued:
            fn(r)

    def _serve(self):
        while not self._closed:
            try:
                if runtime.IS_WINDOWS:
                    conn = _Conn(self._listener.accept())
                else:
                    raw, _addr = self._listener.accept()
                    if not _same_user(raw):
                        raw.close()
                        continue
                    conn = _Conn(raw)
            except OSError:
                if self._closed:
                    return
                time.sleep(0.1)
                continue
            if self._closed:
                conn.close()
                return
            try:
                self._handle(conn)
            except Exception as e:  # noqa: BLE001
                print(f"View: bad request from another launch: {e}", file=sys.stderr)
                conn.close()

    def _handle(self, conn):
        data = conn.recv(3)
        if data is None:
            conn.close()
            return
        if data.get("version") != runtime.VERSION and self.idle and self.on_restart:
            # another version, and nothing open here: make way for it
            conn.send({"restart": True})
            conn.close()
            self.close()
            self.on_restart()
            return
        conn.send({"ok": True, "pid": os.getpid()})
        req = Request(data, conn if "VIEW_OPEN_T0" in (data.get("env") or {}) else None)
        if req._conn is None:
            conn.close()
        with self._mutex:
            handler = self._handler
            if handler is None:
                self._queue.append(req)
        if handler is not None:
            handler(req)

    def close(self):
        """Stop listening (quitting): later launches start a View of their own."""
        if self._closed:
            return
        self._closed = True
        if runtime.IS_WINDOWS:
            try:
                self._listener.close()
            except OSError:
                pass
            try:
                conn = _Conn.connect(self.address, 0.2)  # wake the blocked accept()
            except OSError:
                conn = None
            if conn is not None:
                conn.close()
            return
        import socket
        try:
            self._listener.shutdown(socket.SHUT_RDWR)     # wakes accept()
        except OSError:
            pass
        self._listener.close()
        try:
            if os.stat(self.address).st_ino == self._ino:
                os.unlink(self.address)
        except OSError:
            pass


# ------------------------------------------------------- background mode ---

def background_choice() -> bool | None:
    """Preferences: keep running with no window open?  None (never chosen):
    only where there is a tray icon to quit from."""
    from . import i18n
    v = i18n._read().get("background")
    return v if isinstance(v, bool) else None


def set_background_choice(on: bool):
    from . import i18n
    data = i18n._read()
    data["background"] = bool(on)
    path = i18n.settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
