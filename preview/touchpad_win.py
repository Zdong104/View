"""Absolute-position touchpad capture on Windows, for drawing signatures.

Windows has no equivalent of evdev's exclusive grab, so this reads the
Precision Touchpad's own HID reports through the Raw Input API (usage page
0x0D Digitizer, usage 0x05 Touch Pad): every report carries each finger's
absolute X/Y, contact ID, tip switch and palm ("confidence") bit, decoded
with hid.dll's HidP_* parser.  While capturing, a low-level mouse hook
swallows the pointer motion and taps Windows synthesizes from the same
touches, so the cursor stays put and nothing gets clicked.  Windows removes
the hook by itself if this process exits or stops responding, so the mouse
can never be left stuck.

Same interface as preview.touchpad.Touchpad (which re-exports this class on
Windows).  Older touchpads without Precision Touchpad drivers report no
digitizer data; they raise TouchpadError.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes as wt

from gi.repository import GLib

from .touchpad import TouchpadError

# --- Win32 / HID constants ------------------------------------------------
RIM_TYPEHID = 2
RIDI_PREPARSEDDATA = 0x20000005
RIDI_DEVICENAME = 0x20000007
RIDI_DEVICEINFO = 0x2000000B
RID_INPUT = 0x10000003
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
RIDEV_DEVNOTIFY = 0x00002000
WM_INPUT = 0x00FF
WM_INPUT_DEVICE_CHANGE = 0x00FE
GIDC_REMOVAL = 2
WM_QUIT = 0x0012
WM_APP = 0x8000
MSG_HOOK_ON, MSG_HOOK_OFF = WM_APP + 1, WM_APP + 2
WH_MOUSE_LL = 14
HWND_MESSAGE = wt.HWND(-3)

HIDP_INPUT = 0
HIDP_STATUS_SUCCESS = 0x00110000
PAGE_GENERIC, PAGE_DIGITIZER = 0x01, 0x0D
USAGE_X, USAGE_Y = 0x30, 0x31
USAGE_TOUCHPAD = 0x05
USAGE_TIP_SWITCH, USAGE_CONFIDENCE = 0x42, 0x47
USAGE_CONTACT_ID, USAGE_CONTACT_COUNT, USAGE_SCAN_TIME = 0x51, 0x54, 0x56
UNIT_CM, UNIT_INCH = 0x11, 0x13

LRESULT = ctypes.c_ssize_t


class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [("hDevice", wt.HANDLE), ("dwType", wt.DWORD)]


class RID_DEVICE_INFO_HID(ctypes.Structure):
    _fields_ = [("dwVendorId", wt.DWORD), ("dwProductId", wt.DWORD),
                ("dwVersionNumber", wt.DWORD), ("usUsagePage", wt.USHORT),
                ("usUsage", wt.USHORT)]


class RID_DEVICE_INFO(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("hid", RID_DEVICE_INFO_HID), ("_pad", wt.DWORD * 6)]
    _fields_ = [("cbSize", wt.DWORD), ("dwType", wt.DWORD), ("u", _U)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT), ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT), ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT), ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT), ("NumberOutputValueCaps", wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT), ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [("UsagePage", wt.USHORT), ("ReportID", ctypes.c_ubyte),
                ("IsAlias", ctypes.c_ubyte), ("BitField", wt.USHORT),
                ("LinkCollection", wt.USHORT), ("LinkUsage", wt.USHORT),
                ("LinkUsagePage", wt.USHORT), ("IsRange", ctypes.c_ubyte),
                ("IsStringRange", ctypes.c_ubyte), ("IsDesignatorRange", ctypes.c_ubyte),
                ("IsAbsolute", ctypes.c_ubyte), ("HasNull", ctypes.c_ubyte),
                ("Reserved", ctypes.c_ubyte), ("BitSize", wt.USHORT),
                ("ReportCount", wt.USHORT), ("Reserved2", wt.USHORT * 5),
                ("UnitsExp", wt.ULONG), ("Units", wt.ULONG),
                ("LogicalMin", wt.LONG), ("LogicalMax", wt.LONG),
                ("PhysicalMin", wt.LONG), ("PhysicalMax", wt.LONG),
                # NotRange view of the union (Usage, Reserved1, StringIndex, ...)
                ("Usage", wt.USHORT), ("_u", wt.USHORT * 7)]


_api = None


def _load_api():
    """Bind the Win32 functions once, with argtypes (64-bit handles!)."""
    global _api
    if _api is not None:
        return _api
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid")
    P = ctypes.POINTER
    sigs = {
        user32: {
            "GetRawInputDeviceList": (wt.UINT, [P(RAWINPUTDEVICELIST), P(wt.UINT), wt.UINT]),
            "GetRawInputDeviceInfoW": (wt.UINT, [wt.HANDLE, wt.UINT, wt.LPVOID, P(wt.UINT)]),
            "RegisterRawInputDevices": (wt.BOOL, [P(RAWINPUTDEVICE), wt.UINT, wt.UINT]),
            "GetRawInputData": (wt.UINT, [wt.HANDLE, wt.UINT, wt.LPVOID, P(wt.UINT), wt.UINT]),
            "CreateWindowExW": (wt.HWND, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                          wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]),
            "DestroyWindow": (wt.BOOL, [wt.HWND]),
            "GetMessageW": (wt.BOOL, [P(wt.MSG), wt.HWND, wt.UINT, wt.UINT]),
            "TranslateMessage": (wt.BOOL, [P(wt.MSG)]),
            "DispatchMessageW": (LRESULT, [P(wt.MSG)]),
            "PostThreadMessageW": (wt.BOOL, [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]),
            "SetWindowsHookExW": (wt.HHOOK, [ctypes.c_int, wt.LPVOID, wt.HINSTANCE, wt.DWORD]),
            "UnhookWindowsHookEx": (wt.BOOL, [wt.HHOOK]),
            "CallNextHookEx": (LRESULT, [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]),
        },
        kernel32: {
            "GetCurrentThreadId": (wt.DWORD, []),
            "GetModuleHandleW": (wt.HMODULE, [wt.LPCWSTR]),
            "CreateFileW": (wt.HANDLE, [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD,
                                        wt.DWORD, wt.HANDLE]),
            "CloseHandle": (wt.BOOL, [wt.HANDLE]),
        },
        hid: {
            "HidP_GetCaps": (wt.LONG, [wt.LPVOID, P(HIDP_CAPS)]),
            "HidP_GetValueCaps": (wt.LONG, [ctypes.c_int, P(HIDP_VALUE_CAPS), P(wt.USHORT),
                                            wt.LPVOID]),
            "HidP_GetUsageValue": (wt.LONG, [ctypes.c_int, wt.USHORT, wt.USHORT, wt.USHORT,
                                             P(wt.ULONG), wt.LPVOID, wt.LPVOID, wt.ULONG]),
            "HidP_GetUsages": (wt.LONG, [ctypes.c_int, wt.USHORT, wt.USHORT, P(wt.USHORT),
                                         P(wt.ULONG), wt.LPVOID, wt.LPVOID, wt.ULONG]),
            "HidD_GetProductString": (wt.BOOLEAN, [wt.HANDLE, wt.LPVOID, wt.ULONG]),
        },
    }
    for dll, fns in sigs.items():
        for name, (res, args) in fns.items():
            f = getattr(dll, name)
            f.restype, f.argtypes = res, args
    _api = (user32, kernel32, hid)
    return _api


# --- device discovery -----------------------------------------------------
class _PadInfo:
    """A Precision Touchpad: its parser data and per-finger layout."""

    def __init__(self, handle, name, preparsed):
        self.handle = handle
        self.name = name
        self.preparsed = preparsed          # ctypes buffer, must stay alive
        self.fingers = []                   # link collections that carry X/Y
        self.x = self.y = None              # HIDP_VALUE_CAPS of the first finger
        self.has_confidence = False


def _device_info(user32, h, kind, struct=None):
    if struct is not None:
        buf = struct()
        buf.cbSize = ctypes.sizeof(struct)
        size = wt.UINT(ctypes.sizeof(struct))
    else:
        size = wt.UINT(0)
        user32.GetRawInputDeviceInfoW(h, kind, None, ctypes.byref(size))
        if not size.value:
            return None
        buf = (ctypes.create_unicode_buffer(size.value + 1) if kind == RIDI_DEVICENAME
               else ctypes.create_string_buffer(size.value))
    if user32.GetRawInputDeviceInfoW(h, kind, ctypes.byref(buf), ctypes.byref(size)) == 0xFFFFFFFF:
        return None
    return buf


def _product_name(path):
    """The HID product string ("Synaptics Touchpad" ...), or None."""
    if not path:
        return None
    _, kernel32, hid = _load_api()
    # no access rights needed to query strings; touchpads refuse read access
    h = kernel32.CreateFileW(path, 0, 3, None, 3, 0, None)  # FILE_SHARE_READ|WRITE, OPEN_EXISTING
    if not h or h == wt.HANDLE(-1).value:
        return None
    try:
        buf = ctypes.create_unicode_buffer(127)
        if hid.HidD_GetProductString(h, buf, ctypes.sizeof(buf)):
            return buf.value.strip() or None
    finally:
        kernel32.CloseHandle(h)
    return None


def _span_mm(caps):
    """Physical extent of an axis in mm, or None if the report descriptor
    doesn't say."""
    phys = caps.PhysicalMax - caps.PhysicalMin
    if phys <= 0:
        return None
    exp = caps.UnitsExp & 0xF
    exp = exp - 16 if exp > 7 else exp          # 4-bit two's complement
    unit = caps.Units & 0xFF
    per = {UNIT_CM: 10.0, UNIT_INCH: 25.4}.get(unit)
    if per is None:
        return None
    return phys * (10.0 ** exp) * per


def find_touchpads():
    user32, _, hid = _load_api()
    n = wt.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(n), ctypes.sizeof(RAWINPUTDEVICELIST))
    if not n.value:
        return []
    devs = (RAWINPUTDEVICELIST * n.value)()
    got = user32.GetRawInputDeviceList(devs, ctypes.byref(n), ctypes.sizeof(RAWINPUTDEVICELIST))
    if got == 0xFFFFFFFF:
        return []
    pads = []
    for d in devs[:got]:
        if d.dwType != RIM_TYPEHID:
            continue
        info = _device_info(user32, d.hDevice, RIDI_DEVICEINFO, RID_DEVICE_INFO)
        if info is None or (info.u.hid.usUsagePage, info.u.hid.usUsage) != \
                (PAGE_DIGITIZER, USAGE_TOUCHPAD):
            continue
        name = _device_info(user32, d.hDevice, RIDI_DEVICENAME)
        pre = _device_info(user32, d.hDevice, RIDI_PREPARSEDDATA)
        if pre is None:
            continue
        caps = HIDP_CAPS()
        if hid.HidP_GetCaps(pre, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
            continue
        count = wt.USHORT(caps.NumberInputValueCaps)
        vcaps = (HIDP_VALUE_CAPS * max(1, count.value))()
        if hid.HidP_GetValueCaps(HIDP_INPUT, vcaps, ctypes.byref(count), pre) != \
                HIDP_STATUS_SUCCESS:
            continue
        path = name.value if name is not None else None
        try:
            label = _product_name(path)
        except OSError:
            label = None
        if label and "hid" in label.lower():         # "HIDI2C Device": says nothing
            label = None
        pad = _PadInfo(d.hDevice, label or "Precision Touchpad", pre)
        xs = {}
        ys = {}
        for vc in vcaps[:count.value]:
            if vc.UsagePage == PAGE_GENERIC and not vc.IsRange:
                if vc.Usage == USAGE_X:
                    xs[vc.LinkCollection] = vc
                elif vc.Usage == USAGE_Y:
                    ys[vc.LinkCollection] = vc
        pad.fingers = sorted(set(xs) & set(ys))
        if not pad.fingers:
            continue
        pad.x, pad.y = xs[pad.fingers[0]], ys[pad.fingers[0]]
        pads.append(pad)
    return pads


# --- report parsing ---------------------------------------------------------
class _FrameAssembler:
    """Windows touchpads may split one frame across several reports ("hybrid
    mode"): the first carries the frame's contact count, later ones count 0.
    Collect contacts until the frame is complete."""

    def __init__(self):
        self.expected = 0
        self.contacts = []

    def feed(self, count, contacts):
        """count: this report's contact-count value; contacts: the fingers in
        this report (already limited to valid slots).  Returns a complete
        frame (list) or None."""
        if count > 0:
            self.expected = count
            self.contacts = []
        if self.expected == 0:
            return None
        take = min(len(contacts), self.expected - len(self.contacts))
        self.contacts.extend(contacts[:take])
        if len(self.contacts) >= self.expected:
            frame, self.contacts, self.expected = self.contacts, [], 0
            return frame
        return None


class Touchpad:
    """Reads a Precision Touchpad. Call grab() to start capturing, release()
    to stop.

    on_frame(contacts, t) is called on the GTK main loop once per hardware
    frame while grabbed; contacts is a list of (contact_id, x_mm, y_mm) for
    fingers on the pad (palms excluded), t is a time in seconds.
    """

    def __init__(self, on_frame=None, on_lost=None):
        try:
            pads = find_touchpads()
        except OSError as e:
            raise TouchpadError(f"Could not read input devices ({e}).")
        if not pads:
            raise TouchpadError("No Precision Touchpad found (Windows reports no touchpad "
                                "finger data; older touchpad drivers don't provide it).")
        self.pads = {p.handle: p for p in pads}
        first = pads[0]
        self.name = first.name
        self.width_mm = _span_mm(first.x) or 100.0
        self.height_mm = _span_mm(first.y) or self.width_mm * (
            (first.y.LogicalMax - first.y.LogicalMin) /
            max(1, first.x.LogicalMax - first.x.LogicalMin))
        self.on_frame = on_frame
        self.on_lost = on_lost
        self.grabbed = False
        self._pending_grab = None
        self._down = False
        self._last_report = 0.0
        self._frames = {}
        self._tid = None
        self._hook = None
        self._hook_proc = None
        self._error = None
        self._closed = False
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True,
                                        name="touchpad-rawinput")
        self._thread.start()
        ready.wait(5)
        if self._error:
            raise TouchpadError(self._error)

    # --- grab lifecycle ---------------------------------------------------
    def finger_down(self) -> bool:
        # A touching finger makes the pad report ~100 times a second, so a
        # quiet pad means no finger even if the last lift report was lost.
        return self._down and time.monotonic() - self._last_report < 0.25

    def grab(self, on_grabbed=None):
        """Start capturing once no finger touches the pad (so Windows never
        sees a touch begin without its end)."""
        if self.grabbed or self._pending_grab or self._closed:
            return

        def try_grab():
            if self.finger_down():
                return GLib.SOURCE_CONTINUE
            self._pending_grab = None
            self.grabbed = True
            self._post(MSG_HOOK_ON)
            if on_grabbed:
                on_grabbed()
            return GLib.SOURCE_REMOVE

        if try_grab() == GLib.SOURCE_CONTINUE:
            self._pending_grab = GLib.timeout_add(30, try_grab)

    def release(self):
        if self._pending_grab:
            GLib.source_remove(self._pending_grab)
            self._pending_grab = None
        if self.grabbed:
            self.grabbed = False
            self._post(MSG_HOOK_OFF)

    def close(self):
        self.release()
        if not self._closed:
            self._closed = True
            self._post(WM_QUIT)
            self._thread.join(2)

    def _post(self, msg):
        if self._tid:
            user32, _, _ = _load_api()
            user32.PostThreadMessageW(self._tid, msg, 0, 0)

    # --- input thread -------------------------------------------------------
    def _run(self, ready):
        user32, kernel32, _ = _load_api()
        self._tid = kernel32.GetCurrentThreadId()
        hwnd = user32.CreateWindowExW(0, "STATIC", "View touchpad", 0, 0, 0, 0, 0,
                                      HWND_MESSAGE, None, None, None)
        rid = RAWINPUTDEVICE(PAGE_DIGITIZER, USAGE_TOUCHPAD,
                             RIDEV_INPUTSINK | RIDEV_DEVNOTIFY, hwnd)
        if not hwnd or not user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                                          ctypes.sizeof(RAWINPUTDEVICE)):
            self._error = f"Could not register for touchpad input (error {ctypes.get_last_error()})."
            ready.set()
            return
        ready.set()
        msg = wt.MSG()
        lost = False
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_INPUT:
                    self._on_input(msg.lParam)
                elif msg.message == WM_INPUT_DEVICE_CHANGE and msg.wParam == GIDC_REMOVAL:
                    if msg.lParam in self.pads:
                        lost = True
                        break
                elif msg.message == MSG_HOOK_ON:
                    self._hook_on()
                    continue
                elif msg.message == MSG_HOOK_OFF:
                    self._hook_off()
                    continue
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))   # DefWindowProc cleans up WM_INPUT
        finally:
            self._hook_off()
            rid = RAWINPUTDEVICE(PAGE_DIGITIZER, USAGE_TOUCHPAD, RIDEV_REMOVE, None)
            user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE))
            user32.DestroyWindow(hwnd)
            self._tid = None
        if lost and not self._closed:
            GLib.idle_add(self._lost)

    def _lost(self):
        self.release()
        if self.on_lost:
            self.on_lost()
        return False

    def _hook_on(self):
        """Swallow the mouse motion/clicks Windows makes from the touches."""
        if self._hook:
            return
        user32, kernel32, _ = _load_api()
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

        def proc(code, wparam, lparam):
            if code >= 0 and self.grabbed:
                return 1
            return user32.CallNextHookEx(None, code, wparam, lparam)
        self._hook_proc = HOOKPROC(proc)                    # keep a reference
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, ctypes.cast(self._hook_proc, wt.LPVOID),
                                              kernel32.GetModuleHandleW(None), 0)

    def _hook_off(self):
        if self._hook:
            user32, _, _ = _load_api()
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _on_input(self, hraw):
        user32, _, hid = _load_api()
        size = wt.UINT(0)
        hdr = ctypes.sizeof(RAWINPUTHEADER)
        user32.GetRawInputData(hraw, RID_INPUT, None, ctypes.byref(size), hdr)
        buf = ctypes.create_string_buffer(size.value)
        if user32.GetRawInputData(hraw, RID_INPUT, buf, ctypes.byref(size), hdr) != size.value:
            return
        header = RAWINPUTHEADER.from_buffer(buf)
        pad = self.pads.get(header.hDevice)
        if pad is None or header.dwType != RIM_TYPEHID:
            return
        report_size, count = (wt.DWORD * 2).from_buffer(buf, hdr)
        base = hdr + 8
        t = time.monotonic()
        asm = self._frames.setdefault(pad.handle, _FrameAssembler())
        for i in range(count):
            off = base + i * report_size
            report = (ctypes.c_char * report_size).from_buffer(buf, off)
            frame = asm.feed(*self._parse(hid, pad, report, report_size))
            if frame is not None:
                frame = [c for c in frame if c is not None]    # lifted fingers, palms
                self._down = bool(frame)
                self._last_report = t
                if self.grabbed and self.on_frame:
                    # high priority like the Linux fd watch: ink must not wait
                    # behind redraws and page rendering
                    GLib.idle_add(self._deliver, frame, t, priority=GLib.PRIORITY_HIGH)

    def _deliver(self, frame, t):
        if self.grabbed and self.on_frame:
            self.on_frame(frame, t)
        return False

    def _value(self, hid, pad, page, coll, usage, report, size):
        v = wt.ULONG(0)
        st = hid.HidP_GetUsageValue(HIDP_INPUT, page, coll, usage, ctypes.byref(v),
                                    pad.preparsed, report, size)
        return v.value if st == HIDP_STATUS_SUCCESS else None

    def _parse(self, hid, pad, report, size):
        """(contact count, [(id, x_mm, y_mm)]) for one report."""
        count = self._value(hid, pad, PAGE_DIGITIZER, 0, USAGE_CONTACT_COUNT, report, size) or 0
        xi, yi = pad.x, pad.y
        sx = self.width_mm / max(1, xi.LogicalMax - xi.LogicalMin)
        sy = self.height_mm / max(1, yi.LogicalMax - yi.LogicalMin)
        contacts = []
        for coll in pad.fingers:
            usages = (wt.USHORT * 16)()
            n = wt.ULONG(16)
            if hid.HidP_GetUsages(HIDP_INPUT, PAGE_DIGITIZER, coll, usages, ctypes.byref(n),
                                  pad.preparsed, report, size) != HIDP_STATUS_SUCCESS:
                continue
            pressed = set(usages[:n.value])
            x = self._value(hid, pad, PAGE_GENERIC, coll, USAGE_X, report, size)
            y = self._value(hid, pad, PAGE_GENERIC, coll, USAGE_Y, report, size)
            cid = self._value(hid, pad, PAGE_DIGITIZER, coll, USAGE_CONTACT_ID, report, size)
            contacts.append((pressed, cid, x, y))
        # Every slot is returned (None = not a finger on the pad): the
        # assembler counts slots against the frame's contact count, which
        # includes fingers being lifted.
        out = []
        for pressed, cid, x, y in contacts:
            if USAGE_CONFIDENCE in pressed:
                pad.has_confidence = True       # then a contact without it is a palm
            entry = None
            if (USAGE_TIP_SWITCH in pressed and x is not None and y is not None
                    and (USAGE_CONFIDENCE in pressed or not pad.has_confidence)):
                entry = (cid if cid is not None else 0,
                         (x - xi.LogicalMin) * sx, (y - yi.LogicalMin) * sy)
            out.append(entry)
        return count, out
