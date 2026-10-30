"""The high-quality translation model: Tencent Hy-MT2-1.8B on llama.cpp.

Argos's small sentence models (preview.translate) are fast but often wrong
on technical text ("Strengths" -> 强度, benchmark names translated word by
word).  Hy-MT2-1.8B is a translation-tuned 1.8B LLM (Apache-2.0, 33
languages) that writes fluent text, follows a "keep these names" list and
sees a whole paragraph at a time.  It cannot run on ctranslate2 (its
attention normalises queries and keys after RoPE, which CTranslate2 has no
spec for), so it runs on llama.cpp's own ``llama-server``:

  * the model: the official Q4_K_M GGUF (1.13 GB) from Hugging Face, pinned
    to a commit and checked against its SHA-256;
  * the engine: llama.cpp's prebuilt Vulkan release (Linux x64, Windows x64;
    ~36 MB, pinned and checked the same way).  The Vulkan build carries the
    CPU backends too, so one archive runs on the GPU and, without a usable
    one, on the processor.  No Python packages are needed.

Both are downloaded on first use into ``<packs dir>`` (in the snap
``$SNAP_USER_COMMON/translate``, so that a refresh does not copy 1.2 GB per
revision; strict confinement may execute files there).  One server process
is started on first use and reused: bound to 127.0.0.1 on a free port with a
random API key, 4 parallel slots, the GPU (Vulkan, all layers) when there is
one, else -- or when Vulkan fails -- the CPU with one thread per performance
core.  It is stopped when View exits and when the model is removed.

    supported() / installed() / size() / remove() / download(...)
    device() -> {"kind": "gpu"|"cpu", "name": ...}     what it runs on
    has_gpu() -> bool                                   for "Automatic"
    translate_pieces(pieces, src, tgt, on_piece, cancelled)
    keep_terms(text), prompt(text, src, tgt), clean(output, source)

Environment: VIEW_TRANSLATE_CPU=1 never uses the GPU.
"""
from __future__ import annotations

import atexit
import concurrent.futures as cf
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from .i18n import _

# ---------------------------------------------------------------------------
# What is downloaded (pinned: a new version is a code change, not a surprise)
# ---------------------------------------------------------------------------
LLAMA_BUILD = "b11970"
_LLAMA_BASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/"
# platform -> (archive, size, sha256 (GitHub's asset digest))
_LLAMA = {
    "linux-x86_64": (f"llama-{LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz", 35474920,
                     "344cbee7d2d58921111ff21f8bc3cec775cd845d4fd8f2a61404c38fb1eff5e2"),
    "win-amd64": (f"llama-{LLAMA_BUILD}-bin-win-vulkan-x64.zip", 37346727,
                  "2d043e717386c03caa1f150fbcf6c37b513d9872ed44f5d41706b2fecc14617b"),
}
MODEL_FILE = "Hy-MT2-1.8B-Q4_K_M.gguf"
MODEL_URL = ("https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF/resolve/"
             "a0c709d9fac510f2c807aa3af52872340dc37a4a/" + MODEL_FILE)
MODEL_SIZE = 1133080448
MODEL_SHA256 = "dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699"

# the 23 of View's languages the model covers (Swedish, Greek, Romanian,
# Hungarian, Finnish, Danish and Norwegian stay with the Argos packs)
CODES = {"en", "zh", "zt", "es", "fr", "de", "it", "pt", "ru", "ja", "ko", "ar", "hi", "nl",
         "pl", "tr", "uk", "vi", "id", "th", "cs", "he", "fa"}
# The model card's prompts: Chinese ones (with Chinese language names) when
# Chinese is on either side, English ones otherwise.
_ZH_NAMES = {"en": "英语", "zh": "中文", "zt": "繁体中文", "es": "西班牙语", "fr": "法语",
             "de": "德语", "it": "意大利语", "pt": "葡萄牙语", "ru": "俄语", "ja": "日语",
             "ko": "韩语", "ar": "阿拉伯语", "hi": "印地语", "nl": "荷兰语", "pl": "波兰语",
             "tr": "土耳其语", "uk": "乌克兰语", "vi": "越南语", "id": "印尼语", "th": "泰语",
             "cs": "捷克语", "he": "希伯来语", "fa": "波斯语"}
_EN_NAMES = {"en": "English", "zh": "Simplified Chinese", "zt": "Traditional Chinese",
             "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
             "pt": "Portuguese", "ru": "Russian", "ja": "Japanese", "ko": "Korean",
             "ar": "Arabic", "hi": "Hindi", "nl": "Dutch", "pl": "Polish", "tr": "Turkish",
             "uk": "Ukrainian", "vi": "Vietnamese", "id": "Indonesian", "th": "Thai",
             "cs": "Czech", "he": "Hebrew", "fa": "Persian"}

SLOTS = 4                     # requests the server runs at once (one per paragraph)
CONTEXT = 8192                # shared by the slots: 2048 tokens each
MAX_PIECE = 1000              # characters per request; longer paragraphs go by sentences
_FORCE_CPU = os.environ.get("VIEW_TRANSLATE_CPU", "") not in ("", "0")


def _platform() -> str | None:
    m = platform.machine().lower()
    if sys.platform.startswith("linux") and m in ("x86_64", "amd64"):
        return "linux-x86_64"
    if sys.platform == "win32" and m in ("amd64", "x86_64"):
        return "win-amd64"
    return None


def supported() -> bool:
    """Whether llama.cpp ships a build for this platform."""
    return _platform() is not None


def download_size() -> int:
    """Bytes still to download (both parts if neither is there)."""
    total = 0 if os.path.isfile(model_path()) else MODEL_SIZE
    if supported() and not server_exe():
        total += _LLAMA[_platform()][1]
    return total


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def root() -> str:
    """Where the model and llama.cpp live (see the module docstring)."""
    common = os.environ.get("SNAP_USER_COMMON")
    if common and os.environ.get("SNAP_NAME"):
        return os.path.join(common, "translate")
    from . import translate
    return translate.packs_dir()


def model_path() -> str:
    return os.path.join(root(), "hy-mt2", MODEL_FILE)


def _llama_dir() -> str:
    return os.path.join(root(), f"llama-{LLAMA_BUILD}")


def dirs() -> list[str]:
    """The folders this module keeps its files in."""
    return [os.path.dirname(model_path()), _llama_dir()]


def server_exe() -> str | None:
    exe = os.path.join(_llama_dir(), "llama-server.exe" if sys.platform == "win32"
                       else "llama-server")
    if not os.path.isfile(exe):
        return None
    if sys.platform == "win32":
        _vc_runtime(os.path.dirname(exe))
    return exe


_VC_DLLS = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")


def _vc_runtime(dest):
    """llama.cpp's Windows build needs the Visual C++ runtime 14.40 or newer:
    with an older msvcp140.dll in System32 (14.36 on a Windows 11 that never
    updated it) every llama.cpp program crashes at start (0xC0000005).
    View's own bundles ship a new one; a copy next to llama-server.exe is
    loaded before System32's."""
    if os.path.isfile(os.path.join(dest, _VC_DLLS[0])):
        return
    base = os.path.dirname(sys.executable)
    for d in (os.path.join(base, "translate", "_internal"), os.path.join(base, "ocr", "_internal"),
              getattr(sys, "_MEIPASS", base)):
        try:
            names = {n.lower(): n for n in os.listdir(d)}
        except OSError:
            continue
        if all(n in names for n in _VC_DLLS):
            try:                  # (msvcp140.dll last: it marks a complete copy)
                for n in reversed(_VC_DLLS):
                    shutil.copyfile(os.path.join(d, names[n]), os.path.join(dest, n))
            except OSError:
                pass
            return


def installed() -> bool:
    try:
        return server_exe() is not None and os.path.getsize(model_path()) == MODEL_SIZE
    except OSError:
        return False


def size() -> int:
    total = 0
    for d in dirs():
        for base, _dirs, files in os.walk(d):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
    return total


def stop():
    """Stop llama-server (a resident View with no window open gives its memory
    back); the next translation starts it again."""
    _server.stop()


def remove():
    """Stop the server and delete the model and llama.cpp."""
    _server.stop()
    shutil.rmtree(os.path.dirname(model_path()), ignore_errors=True)
    shutil.rmtree(_llama_dir(), ignore_errors=True)
    _devices_cache.clear()


def download(on_progress=None, cancelled=None):
    """Fetch what is missing: llama.cpp's archive (unpacked) and the model,
    each checked against its pinned size and SHA-256.  on_progress(fraction,
    text) from this thread; cancelled() -> True stops it ("Cancelled")."""
    if not supported():
        raise RuntimeError(_("The high-quality model is not available on this computer."))
    from . import translate
    incoming = os.path.join(root(), ".incoming")
    os.makedirs(incoming, exist_ok=True)
    jobs = []
    if not server_exe():
        name, nbytes, sha = _LLAMA[_platform()]
        jobs.append((_LLAMA_BASE + name, nbytes, sha, "archive"))
    if not _model_ok():
        jobs.append((MODEL_URL, MODEL_SIZE, MODEL_SHA256, "model"))
    total = sum(n for _u, n, _s, _k in jobs) or 1
    label = _("Downloading the high-quality translation model…")
    done = 0
    for url, nbytes, sha, kind in jobs:
        def prog(got, base=done):
            if on_progress:
                on_progress(min(0.99, (base + got) / total * 0.98),
                            f"{label}  {(base + got) / 1e6:.0f} / {total / 1e6:.0f} MB")
        fd, part = tempfile.mkstemp(prefix=kind + ".", suffix=".part", dir=incoming)
        try:
            try:
                _fetch(translate, url, fd, nbytes, sha, prog, cancelled)
            except OSError as e:
                raise RuntimeError(_("Could not download the language pack. Check your "
                                     "internet connection and try again.") + f"\n({e})") from e
            if kind == "model":
                os.makedirs(os.path.dirname(model_path()), exist_ok=True)
                os.replace(part, model_path())
            else:
                if on_progress:
                    on_progress(min(0.99, (done + nbytes) / total * 0.98), _("Unpacking…"))
                _unpack_llama(part, incoming)
        finally:
            try:
                os.remove(part)
            except OSError:
                pass
        done += nbytes
    _devices_cache.clear()
    device()                     # ask llama.cpp for the GPUs now, off the main thread


def _model_ok() -> bool:
    try:
        return os.path.getsize(model_path()) == MODEL_SIZE
    except OSError:
        return False


def _fetch(translate, url, fd, nbytes, sha, prog, cancelled):
    h = hashlib.sha256()
    got = 0
    with os.fdopen(fd, "wb") as f, translate._urlopen(url, timeout=60) as r:
        while True:
            if cancelled and cancelled():
                raise RuntimeError("Cancelled")
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            prog(got)
    if got != nbytes or h.hexdigest() != sha:
        raise RuntimeError(_("The download was damaged (its checksum does not match). "
                             "Try again."))


def _unpack_llama(archive, incoming):
    """llama.cpp's .tar.gz (one top folder, .so symlinks) or .zip (flat) ->
    <root>/llama-<build>/, unpacked aside and renamed into place."""
    tmp = tempfile.mkdtemp(prefix="llama.", suffix=".tmp", dir=incoming)
    try:
        if archive.endswith(".zip") or _is_zip(archive):
            import zipfile
            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    parts = info.filename.replace("\\", "/").split("/")
                    if not parts[-1] or any(p in ("", ".", "..") for p in parts) \
                            or ":" in info.filename:
                        continue
                    target = os.path.join(tmp, *parts)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out, 1 << 20)
        else:
            import tarfile
            with tarfile.open(archive) as t:
                t.extractall(tmp, filter="data")       # (no member may leave tmp)
        exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
        found = next((base for base, _d, files in os.walk(tmp) if exe in files), None)
        if found is None:
            raise RuntimeError(f"not a llama.cpp build: {exe} is missing")
        if sys.platform != "win32":
            os.chmod(os.path.join(found, exe), 0o755)
        dest = _llama_dir()
        if not server_exe():
            shutil.rmtree(dest, ignore_errors=True)
            try:
                os.replace(found, dest)
            except OSError:
                if not server_exe():
                    raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _is_zip(path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"PK"


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------
# software Vulkan (runs on the CPU, slower than llama.cpp's own CPU code)
_SOFTWARE = re.compile(r"llvmpipe|lavapipe|swiftshader|basic render", re.I)
# names of dedicated GPUs; integrated ones (Intel(R) Graphics, Radeon(TM)
# Graphics, Arc(TM) Graphics) are used too, but don't make "Automatic" pick
# the big model on their own
_DISCRETE = re.compile(r"nvidia|geforce|quadro|\brtx\b|tesla|radeon(\(tm\))? (rx|pro)|"
                       r"\barc(\(tm\))? [ab]\d|instinct|firepro", re.I)
_devices_cache: dict = {}


def _devices() -> list[tuple[str, str]]:
    """[(llama.cpp device id "Vulkan1", name)] usable for the model, the
    dedicated GPUs first.  Empty without llama.cpp, a Vulkan driver or a GPU."""
    if "list" in _devices_cache:
        return _devices_cache["list"]
    exe = server_exe()
    out = []
    if exe:
        try:
            res = subprocess.run([exe, "--list-devices"], capture_output=True, text=True,
                                 timeout=30, creationflags=_NO_WINDOW, errors="replace")
            for m in re.finditer(r"^\s*(Vulkan\d+): (.+?) \(\d+ MiB, \d+ MiB free\)\s*$",
                                 res.stdout + "\n" + res.stderr, re.M):
                if not _SOFTWARE.search(m.group(2)):
                    out.append((m.group(1), m.group(2).strip()))
        except (OSError, subprocess.SubprocessError):
            out = []
        out.sort(key=lambda d: not _DISCRETE.search(d[1]))
        _devices_cache["list"] = out
    return out


def _guess_gpus() -> list[str]:
    """Dedicated GPUs, before llama.cpp is downloaded (it then asks Vulkan)."""
    if "guess" in _devices_cache:
        return _devices_cache["guess"]
    names = []
    try:
        if sys.platform.startswith("linux"):
            import glob
            for info in glob.glob("/proc/driver/nvidia/gpus/*/information"):
                with open(info, encoding="utf-8", errors="replace") as f:
                    m = re.search(r"^Model:\s*(.+)$", f.read(), re.M)
                names.append(m.group(1).strip() if m else "NVIDIA GPU")
            for card in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
                with open(os.path.join(card, "vendor")) as f:
                    vendor = f.read().strip()
                if vendor == "0x10de" and not names:
                    names.append("NVIDIA GPU")
                elif vendor == "0x1002":       # AMD: a dedicated card has its own VRAM
                    try:
                        with open(os.path.join(card, "mem_info_vram_total")) as f:
                            if int(f.read()) >= 2 << 30:
                                names.append("AMD Radeon GPU")
                    except (OSError, ValueError):
                        pass
        elif sys.platform == "win32":
            res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  "(Get-CimInstance Win32_VideoController).Name"],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=_NO_WINDOW, errors="replace")
            names = [n.strip() for n in res.stdout.splitlines() if _DISCRETE.search(n)]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _devices_cache["guess"] = names
    return names


def has_gpu() -> bool:
    """A dedicated GPU the model can use (what "Automatic" goes by)."""
    if _FORCE_CPU:
        return False
    if server_exe():
        return any(_DISCRETE.search(name) for _d, name in _devices())
    return bool(_guess_gpus())


def device() -> dict:
    """What the model runs (or will run) on: {"kind": "gpu", "name": ...} or
    {"kind": "cpu", "name": None, "why": "forced" | "failed" | "none"}.
    May run llama.cpp for a second: call it off the main thread."""
    if _FORCE_CPU:
        return {"kind": "cpu", "name": None, "why": "forced"}
    if _server.gpu_failed:
        return {"kind": "cpu", "name": None, "why": "failed"}
    if server_exe():
        devs = _devices()
        return {"kind": "gpu", "name": devs[0][1]} if devs else \
            {"kind": "cpu", "name": None, "why": "none"}
    guess = _guess_gpus()
    return {"kind": "gpu", "name": guess[0]} if guess else \
        {"kind": "cpu", "name": None, "why": "none"}


def cpu_threads() -> int:
    """One thread per performance core: llama.cpp's token generation is
    memory-bound, and threads on efficiency or hyper-threaded siblings only
    slow it down (6 P-cores of a Core Ultra 7: 16 tok/s; 12 threads: 5)."""
    n = 0
    try:
        if sys.platform.startswith("linux"):
            cpus = _cpu_list("/sys/devices/cpu_core/cpus")      # hybrid Intel: P-cores
            if not cpus:
                cpus = _cpu_list("/sys/devices/system/cpu/online")
            cores = set()
            for c in cpus:
                base = f"/sys/devices/system/cpu/cpu{c}/topology/"
                with open(base + "core_id") as f, open(base + "physical_package_id") as g:
                    cores.add((g.read().strip(), f.read().strip()))
            n = len(cores)
    except (OSError, ValueError):
        n = 0
    if not n:
        n = (os.cpu_count() or 4) // 2
    return max(1, min(8, n))


def _cpu_list(path) -> list[int]:
    try:
        with open(path) as f:
            spec = f.read().strip()
    except OSError:
        return []
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    return out


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0      # CREATE_NO_WINDOW


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Spawner:
    """Starts processes from one long-lived thread.  On Linux the server
    gets PR_SET_PDEATHSIG, so it goes when View dies, even if killed; the
    signal follows the thread that started it, and View's worker threads
    come and go."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = []
        self.cv = threading.Condition(self.lock)
        self.thread = None

    def popen(self, args, **kw):
        if not sys.platform.startswith("linux"):
            return subprocess.Popen(args, **kw)
        box, ev = [], threading.Event()
        with self.cv:
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True,
                                               name="translate-spawner")
                self.thread.start()
            self.jobs.append((args, kw, box, ev))
            self.cv.notify()
        ev.wait()
        if isinstance(box[0], BaseException):
            raise box[0]
        return box[0]

    def _loop(self):
        import ctypes
        import signal
        try:
            prctl = ctypes.CDLL(None, use_errno=True).prctl
        except (OSError, AttributeError):
            prctl = None

        def child():
            if prctl is not None:
                prctl(1, signal.SIGTERM)            # PR_SET_PDEATHSIG
        while True:
            with self.cv:
                while not self.jobs:
                    self.cv.wait()
                args, kw, box, ev = self.jobs.pop(0)
            try:
                box.append(subprocess.Popen(args, preexec_fn=child, **kw))
            except BaseException as e:  # noqa: BLE001  (handed to the caller)
                box.append(e)
            ev.set()


_spawner = _Spawner()


class _Server:
    """One llama-server for the whole app, started on first use."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.base = None
        self.key = None
        self.on = None             # the device name it runs on, None: the CPU
        self.gpu_failed = False
        self.epoch = 0             # +1 on each stop(): requests then in flight don't restart it
        self._atexit = False

    def url(self) -> str:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return self.base
            self._stop()
            exe = server_exe()
            if not exe or not _model_ok():
                from . import translate
                raise translate.NeedsDownload([translate.MODEL_PACK])
            if not self._atexit:
                atexit.register(self.stop)
                self._atexit = True
            devs = [] if (_FORCE_CPU or self.gpu_failed) else _devices()
            if devs:
                try:
                    self._start(exe, devs[0])
                    return self.base
                except RuntimeError:
                    self.gpu_failed = True       # Vulkan could not run it: the CPU then
            self._start(exe, None)
            return self.base

    def _start(self, exe, dev):
        from . import runtime
        port, key = _free_port(), secrets.token_hex(16)
        threads = cpu_threads()
        args = [exe, "-m", model_path(), "--host", "127.0.0.1", "--port", str(port),
                "--api-key", key, "-np", str(SLOTS), "-c", str(CONTEXT),
                "-t", str(threads), "-tb", str(threads), "--no-webui"]
        args += ["-ngl", "99", "-dev", dev[0]] if dev else ["-ngl", "0", "--device", "none"]
        log = os.path.join(runtime.user_cache_dir(), "translate-server.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "wb") as logf:
            proc = _spawner.popen(args, stdin=subprocess.DEVNULL, stdout=logf,
                                  stderr=subprocess.STDOUT, creationflags=_NO_WINDOW,
                                  cwd=os.path.dirname(exe))
        deadline = time.monotonic() + 180       # (a first Vulkan start compiles shaders)
        while True:
            if proc.poll() is not None:
                break
            try:
                with _opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if r.status == 200:
                        self.proc, self.base, self.key = proc, f"http://127.0.0.1:{port}", key
                        self.on = dev[1] if dev else None
                        return
            except OSError:
                pass
            if time.monotonic() > deadline:
                break
            time.sleep(0.25)
        _kill(proc)
        tail = ""
        try:
            with open(log, "rb") as f:
                tail = f.read()[-600:].decode("utf-8", "replace").strip()
        except OSError:
            pass
        raise RuntimeError(_("The translation model could not start.") + (f"\n{tail}" if tail else ""))

    def stop(self):
        with self.lock:
            self.epoch += 1
            self._stop()

    def _stop(self):
        p, self.proc = self.proc, None
        if p is not None:
            _kill(p)

    def complete(self, prompt: str, max_tokens: int) -> tuple[str, str]:
        """(text, finish_reason) for one user message.  A server that died is
        started again once (on the CPU if it was the GPU that failed)."""
        body = json.dumps({"messages": [{"role": "user", "content": prompt}],
                           "temperature": 0, "top_k": 1, "repeat_penalty": 1.05,
                           "max_tokens": max_tokens, "cache_prompt": True}).encode()
        epoch = self.epoch
        for attempt in (0, 1):
            base = self.url()
            import urllib.request
            req = urllib.request.Request(base + "/v1/chat/completions", body, {
                "Content-Type": "application/json", "Authorization": "Bearer " + self.key})
            try:
                with _opener.open(req, timeout=900) as r:
                    res = json.load(r)
                choice = res["choices"][0]
                return choice["message"].get("content") or "", choice.get("finish_reason") or ""
            except (OSError, ValueError, KeyError, IndexError) as e:
                with self.lock:
                    if self.epoch != epoch:          # stopped on purpose (removed, exiting)
                        raise RuntimeError("Cancelled") from e
                    died = self.proc is None or self.proc.poll() is not None
                    if died and self.on:
                        self.gpu_failed = True
                    self._stop()
                if attempt:
                    raise RuntimeError(_("The translation model stopped: {error}").format(
                        error=e)) from e
        raise AssertionError("unreachable")


def _kill(p):
    try:
        p.terminate()
        p.wait(3)
    except Exception:  # noqa: BLE001
        try:
            p.kill()
            p.wait(2)
        except Exception:  # noqa: BLE001
            pass


def _make_opener():
    import urllib.request
    # never through a proxy: the server is on this computer
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


_opener = _make_opener()
_server = _Server()


def running_on() -> str | None:
    """The GPU the running server uses, "" for the CPU, None if not running."""
    p = _server.proc
    if p is None or p.poll() is not None:
        return None
    return _server.on or ""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
_W = r"[A-Za-z0-9τ²][\w.²]*"
_TERM = re.compile(
    r"`[^`\n]{1,60}`"                                         # `inline code`
    r"|https?://[^\s<>()\[\]\"']+"                            # URLs
    r"|\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+(?:\(\))?"        # os.path.join, foo.py, a.b()
    r"|\b[A-Za-z_]\w*_\w*\b(?:\(\))?"                         # snake_case
    r"|\b[A-Za-z_]\w*\(\)"                                    # call()
    rf"|{_W}(?:-{_W})+"                                       # Terminal-Bench, Cost-of-Pass
    r"|\b[A-Z][a-z]+[A-Z]\w*|\b[a-z]+[A-Z]\w*"                # CamelCase, camelCase
    r"|\b[A-Z]{2,}[0-9]*s?\b|\b[A-Z]{2,}[a-z]\w*"              # ACRONYMS, GPUs, OSWorld
    r"|\bX\d+\b")                                             # md_translate's placeholders
_TAIL = re.compile(r"(?:\s+(?:[A-Z][a-z]+|\d+(?:\.\d+)+))+")
_NOT_TAIL = re.compile(r"\s+(Section|Sections|Fig|Figure|Figures|Table|Tables|Appendix|Eq|"
                       r"Chapter|Step|Part)\b")
_ABBR = {"e.g", "i.e", "etc", "vs", "cf", "al", "et.al"}


def keep_terms(text: str, limit: int = 16) -> list[str]:
    """Names in ``text`` to leave untranslated: benchmark and product names
    (SWE-bench Verified, Terminal-Bench 2.0, Cost-of-Pass), acronyms (HAL),
    CamelCase, and code: `spans`, snake_case, dotted.names, call(), URLs."""
    out = []
    for m in _TERM.finditer(text):
        t = m.group(0).rstrip(".,;:")
        low = t.lower().strip("`")
        if low in _ABBR or len(t) > 60 or len(t) < 2:
            continue
        if "-" in t and not t.startswith(("`", "http")) and "_" not in t:
            # a hyphenated word is a name if it has two capitals or is a "-bench"
            if sum(c.isupper() for c in t) < 2 and "bench" not in low and \
                    not re.search(r"\d", t):
                continue
            if re.fullmatch(r"[\d.\-]+%?", t):          # 76-80, 0.5-1
                continue
        if "." in t and not t.startswith(("`", "http")) and "(" not in t:
            parts = t.split(".")
            if all(len(p) <= 1 for p in parts) or re.fullmatch(r"[\d.]+", t):
                continue
        if t[0].isalpha() and re.match(r"[\w-]+$", t) and not t.startswith("X"):
            tail = _TAIL.match(text, m.end())
            if tail and not _NOT_TAIL.match(tail.group(0)) and \
                    (sum(c.isupper() for c in t) >= 2 or "-" in t):
                t += tail.group(0)
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


# Chinese wording for frequent terms of the papers people read, which the
# model otherwise translates loosely (代理 for agents, 极限 for ceiling effect)
_GLOSSARY = [  # (English, Simplified, Traditional)
    ("leaderboards", "排行榜", "排行榜"), ("leaderboard", "排行榜", "排行榜"),
    ("agents", "智能体", "智能體"), ("agent", "智能体", "智能體"),
    ("benchmarks", "基准", "基準"), ("benchmark", "基准", "基準"),
    ("binary tasks", "二值任务", "二值任務"), ("binary task", "二值任务", "二值任務"),
    ("pass/fail", "二值（通过/失败）", "二值（通過/失敗）"),
    ("pass-fail", "二值（通过/失败）", "二值（通過/失敗）"),
    ("ceiling effect", "上限效应", "上限效應"),
]


def glossary(text: str, src: str, tgt: str) -> list[tuple[str, str]]:
    """[(English term, Chinese)] of the glossary's terms in an English
    ``text`` going into Chinese; the longer of overlapping terms only."""
    if src != "en" or tgt not in ("zh", "zt"):
        return []
    out, taken = [], []
    for en, zh, zt in _GLOSSARY:
        for m in re.finditer(rf"(?<![\w/-]){re.escape(en)}(?![\w/-])", text, re.I):
            if any(a <= m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            pair = (m.group(0), zh if tgt == "zh" else zt)
            if pair not in out:
                out.append(pair)
    return out


def prompt(text: str, src: str, tgt: str) -> str:
    """The model card's prompt (terminology form when there are terms)."""
    pairs = [(t, t) for t in keep_terms(text)] + glossary(text, src, tgt)
    if "zh" in (src, tgt) or "zt" in (src, tgt):
        head = ("参考下面的翻译：\n" + "".join(f"{a} 翻译成 {b}\n" for a, b in pairs)) \
            if pairs else ""
        return (head + f"将以下文本翻译为{_ZH_NAMES.get(tgt, tgt)}，注意只需要输出翻译后的结果，"
                "不要额外解释：\n\n" + text)
    head = ("Reference the following translations:\n"
            + "".join(f"{a} translates to {b}\n" for a, b in pairs) + "\n") if pairs else ""
    return (head + f"Translate the following text into {_EN_NAMES.get(tgt, tgt)}. Note that "
            "you should only output the translated result without any additional "
            "explanation:\n\n" + text)


# bits of the prompt that a reply must not contain
_ECHO = ("将以下文本翻译为", "只需要输出翻译后的结果", "不要额外解释", "参考下面的翻译",
         "参考上面的信息", "Translate the following text into", "without any additional explanation",
         "Reference the following translations")
_HEADER = re.compile(r"^\s*(【[^】\n]{1,12}】|\[(Source Text|Translation|Target)\]|"
                     r"(译文|翻译结果|翻译|Translation)\s*[:：])\s*", re.I)


def clean(output: str, source: str) -> str:
    """The model's reply -> the translation: without echoed instructions or
    term lists, section headers (【待翻译文本】), format tags or special
    tokens.  Empty -> the source as it was."""
    s = re.sub(r"<｜hy_[^｜]*｜>|</?(target|source|sn)>", "", output or "").strip()
    lines = s.split("\n")
    out = []
    for ln in lines:
        if any(e in ln and e not in source for e in _ECHO):
            continue
        if re.fullmatch(r"\s*.{1,80}? (翻译成|translates to) .{1,80}\s*", ln) and \
                not re.search(r"翻译成|translates to", source):
            continue
        head = _HEADER.match(ln)
        if head and not _HEADER.match(source):
            ln = ln[head.end():]
            if not ln.strip():
                continue
        out.append(ln)
    while out and not out[0].strip():
        out.pop(0)
    res = "\n".join(out).strip()
    return res or source


# ---------------------------------------------------------------------------
# Translating
# ---------------------------------------------------------------------------
def _one(text, src, tgt) -> str:
    max_tokens = min(1536, 256 + 2 * len(text))
    out, why = _server.complete(prompt(text, src, tgt), max_tokens)
    if why == "length" and len(text) > 200:
        # ran away (a loop): once more, sentence by sentence
        from . import translate
        sep = "" if tgt in translate._NO_SPACE else " "
        sents = translate._sentences(text)
        if len(sents) > 1:
            return sep.join(_one(s, src, tgt) for s in sents)
    return clean(out, text)


def translate_pieces(pieces, src, tgt, on_piece=None, cancelled=None) -> list[str]:
    """Translate each of ``pieces`` (a paragraph or a line, at most
    MAX_PIECE characters) on its own, SLOTS at a time.  on_piece(i, text) is
    called in order as they finish; cancelled() -> True stops it
    (RuntimeError("Cancelled")), unstarted pieces are dropped."""
    out = [None] * len(pieces)
    if not pieces:
        return out
    _server.url()                                 # start it (or NeedsDownload) once, here
    pool = cf.ThreadPoolExecutor(SLOTS, thread_name_prefix="translate-llm")
    try:
        futs = [pool.submit(_one, p, src, tgt) for p in pieces]
        emitted = 0
        pending = set(futs)
        while emitted < len(futs):
            if cancelled and cancelled():
                raise RuntimeError("Cancelled")
            done, pending = cf.wait(pending, timeout=0.2, return_when=cf.FIRST_COMPLETED)
            while emitted < len(futs) and futs[emitted].done():
                out[emitted] = futs[emitted].result()
                if on_piece:
                    on_piece(emitted, out[emitted])
                emitted += 1
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
