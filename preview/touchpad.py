"""Absolute-position touchpad capture via evdev, for drawing signatures.

While capturing, the touchpad is grabbed exclusively (EVIOCGRAB) so the
compositor stops moving the pointer, and each finger position is reported in
millimetres on the pad surface -- the same trick macOS Preview uses.

The grab is tied to the open file descriptor, so the kernel releases it
automatically if this process exits or crashes; the touchpad can never be
left stuck.
"""
import array
import os
import struct
import sys
from pathlib import Path

try:                        # Linux only; on other platforms Touchpad() raises TouchpadError
    import fcntl
except ImportError:         # pragma: no cover - Windows / macOS
    fcntl = None

from gi.repository import GLib

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT, SYN_DROPPED = 0, 3
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TOOL_TYPE = 0x37
ABS_MT_TRACKING_ID = 0x39
MT_TOOL_PALM = 2
BTN_TOUCH = 0x14A
INPUT_PROP_POINTER = 0
INPUT_PROP_DIRECT = 1

EVIOCGRAB = 0x40044590              # _IOW('E', 0x90, int)
EVIOCGABS_BASE = 0x80184540         # _IOR('E', 0x40 + abs, struct input_absinfo)
EVIOCGKEY_96 = 0x80604518           # _IOR('E', 0x18, char[96])

EVENT = struct.Struct("llHHi")      # struct input_event on 64-bit


def _bits(text: str) -> int:
    value = 0
    for word in text.split():
        value = (value << 64) | int(word, 16)
    return value


def find_touchpad() -> str | None:
    """Return /dev/input/eventN of the built-in multitouch touchpad."""
    if not sys.platform.startswith("linux"):
        return None
    best = None
    for dev in sorted(Path("/sys/class/input").glob("event*")):
        try:
            caps = _bits((dev / "device/capabilities/abs").read_text())
            props = _bits((dev / "device/properties").read_text())
            name = (dev / "device/name").read_text().strip()
        except OSError:
            continue
        if not caps >> ABS_MT_POSITION_X & 1:
            continue
        if props >> INPUT_PROP_DIRECT & 1:      # touchscreen, not touchpad
            continue
        path = f"/dev/input/{dev.name}"
        if "touchpad" in name.lower():
            return path
        if props >> INPUT_PROP_POINTER & 1 and best is None:
            best = path
    return best


class TouchpadError(Exception):
    pass


class Touchpad:
    """Reads a touchpad. Call grab() to start capturing, release() to stop.

    on_frame(contacts, t) is called once per hardware frame while grabbed;
    contacts is a list of (tracking_id, x_mm, y_mm) for real fingers (palms
    excluded), t is the event time in seconds.
    """

    def __init__(self, on_frame=None, on_lost=None):
        if fcntl is None or not sys.platform.startswith("linux"):
            raise TouchpadError("Absolute touchpad capture is only available on Linux.")
        snap = os.environ.get("SNAP_INSTANCE_NAME", "view")
        snap_hint = (f" In the snap, allow it with: sudo snap connect {snap}:raw-input"
                     if os.environ.get("SNAP") else "")
        try:
            self.path = find_touchpad()
        except OSError:
            self.path = None
        if not self.path:
            raise TouchpadError("No touchpad found." + snap_hint)
        try:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError:
            raise TouchpadError(
                f"No permission to read {self.path}. Add yourself to the "
                "'input' group (sudo usermod -aG input $USER) and log in again."
                + snap_hint)
        self.on_frame = on_frame
        self.on_lost = on_lost
        self.name = Path(f"/sys/class/input/{Path(self.path).name}/device/name").read_text().strip()
        self.x = self._absinfo(ABS_MT_POSITION_X)
        self.y = self._absinfo(ABS_MT_POSITION_Y)
        self.width_mm = self._span(self.x)
        self.height_mm = self._span(self.y)
        self.grabbed = False
        self._watch = None
        self._pending_grab = None
        self._dropped = False
        self.slot = 0
        self.slots = {}

    @staticmethod
    def _span(info):
        _, lo, hi, _, _, res = info
        return (hi - lo) / res if res else (hi - lo) / 30.0

    def _absinfo(self, code):
        buf = array.array("i", [0] * 6)
        fcntl.ioctl(self.fd, EVIOCGABS_BASE + code, buf)
        return tuple(buf)

    def _mm(self, info, raw):
        _, lo, _, _, _, res = info
        return (raw - lo) / (res or 30.0)

    def finger_down(self) -> bool:
        buf = array.array("B", [0] * 96)
        fcntl.ioctl(self.fd, EVIOCGKEY_96, buf)
        return bool(buf[BTN_TOUCH // 8] >> (BTN_TOUCH % 8) & 1)

    # --- grab lifecycle ---------------------------------------------------
    def grab(self, on_grabbed=None):
        """Grab once no finger is touching, so the compositor never sees a
        touch begin without its end (that would leave a stuck click)."""
        if self.grabbed or self._pending_grab:
            return

        def try_grab():
            if self.finger_down():
                return GLib.SOURCE_CONTINUE
            self._pending_grab = None
            self._drain()
            fcntl.ioctl(self.fd, EVIOCGRAB, 1)
            self.grabbed = True
            self.slot = self._absinfo(ABS_MT_SLOT)[0]
            self.slots = {}
            self._watch = GLib.unix_fd_add_full(
                GLib.PRIORITY_HIGH, self.fd,
                GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                self._on_io)
            if on_grabbed:
                on_grabbed()
            return GLib.SOURCE_REMOVE

        if try_grab() == GLib.SOURCE_CONTINUE:
            self._pending_grab = GLib.timeout_add(30, try_grab)

    def release(self):
        if self._pending_grab:
            GLib.source_remove(self._pending_grab)
            self._pending_grab = None
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = None
        if self.grabbed:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 0)
            except OSError:
                pass
            self.grabbed = False

    def close(self):
        self.release()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    # --- event parsing ----------------------------------------------------
    def _drain(self):
        try:
            while os.read(self.fd, EVENT.size * 64):
                pass
        except BlockingIOError:
            pass

    def _on_io(self, fd, condition):
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            return self._lost()
        try:
            data = os.read(fd, EVENT.size * 128)
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        except OSError:
            return self._lost()
        for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
            sec, usec, etype, code, value = EVENT.unpack_from(data, off)
            self._handle(etype, code, value, sec + usec / 1e6)
        return GLib.SOURCE_CONTINUE

    def _lost(self):
        self._watch = None
        self.release()
        if self.on_lost:
            self.on_lost()
        return GLib.SOURCE_REMOVE

    def _handle(self, etype, code, value, t):
        if etype == EV_SYN:
            if code == SYN_DROPPED:
                self._dropped = True
            elif code == SYN_REPORT:
                if self._dropped:       # state unknown until the next clean frame
                    self._dropped = False
                    self.slots = {}
                    if self.on_frame:
                        self.on_frame([], t)
                    return
                contacts = [
                    (s["id"], self._mm(self.x, s["x"]), self._mm(self.y, s["y"]))
                    for s in self.slots.values()
                    if s["id"] >= 0 and s["tool"] != MT_TOOL_PALM
                ]
                if self.on_frame:
                    self.on_frame(contacts, t)
            return
        if etype != EV_ABS or self._dropped:
            return
        if code == ABS_MT_SLOT:
            self.slot = value
            return
        s = self.slots.setdefault(self.slot, {"id": -1, "x": 0, "y": 0, "tool": 0})
        if code == ABS_MT_TRACKING_ID:
            s["id"] = value
            if value < 0:
                s["tool"] = 0
        elif code == ABS_MT_POSITION_X:
            s["x"] = value
        elif code == ABS_MT_POSITION_Y:
            s["y"] = value
        elif code == ABS_MT_TOOL_TYPE:
            s["tool"] = value
