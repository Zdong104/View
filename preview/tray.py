"""Tray icon for View running in the background (no window open).

Shown only then: while a window is open, the dash or taskbar already stands
for View; once the last one closes, the icon is the visible sign that View
still runs, and the way to quit it.

  Linux    a StatusNotifierItem (/StatusNotifierItem) with a com.canonical.dbusmenu
           menu (/MenuBar), registered with org.kde.StatusNotifierWatcher (GNOME
           needs the AppIndicator extension; Ubuntu ships it).  No bus name of
           our own: the watcher takes our unique name, so the snap needs no dbus
           slot (the unity7 plug lets it talk to the watcher and the shell).
           Hidden is Status "Passive", which hosts don't draw.
  Windows  Shell_NotifyIconW with a hidden top-level window (which also hears
           TaskbarCreated when Explorer restarts).  The window exists for the
           whole run: WM_CLOSE to it (taskkill without /F, the uninstaller) and
           WM_ENDSESSION (logoff, Restart Manager) quit View, which a View with
           no window open would otherwise never hear.

Menu: Open…, New Window, Quit.  A left click brings a window up.  Every
failure here only means no icon: it never breaks the app.
"""
from __future__ import annotations

import os
import sys
import traceback

from . import runtime
from .i18n import _, N_

_tray = None                # the platform tray, once start() ran
_token = ""                 # Linux: XDG activation token the host gave us for a click


def start(activate, open_files, new_window, quit):
    """Set up the tray (hidden) with the four actions (called on the main loop)."""
    global _tray
    if _tray is not None:
        return
    actions = {"activate": activate, "open": open_files, "new-window": new_window, "quit": quit}
    try:
        if runtime.IS_LINUX:
            _tray = _SniTray(actions)
        elif runtime.IS_WINDOWS:
            _tray = _WinTray(actions)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        _tray = None


def available() -> bool:
    """Is there somewhere to show the icon (a StatusNotifierWatcher on the
    bus; on Windows, always)?"""
    return _tray is not None and _tray.available()


def show():
    if _tray is not None:
        try:
            _tray.show()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def hide():
    if _tray is not None:
        try:
            _tray.hide()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def take_token() -> str:
    """The activation token of the last tray click, once (else "")."""
    global _token
    t, _token = _token, ""
    return t


def _run(actions, name):
    """Run a menu action on the main loop, outside the D-Bus / window-message
    callback that asked for it."""
    from gi.repository import GLib

    def go():
        try:
            actions[name]()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return GLib.SOURCE_REMOVE
    GLib.idle_add(go)


# ------------------------------------------------------------------ Linux ---

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

SNI_XML = """<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <method name="ProvideXdgActivationToken"><arg name="token" type="s" direction="in"/></method>
  <signal name="NewIcon"/>
  <signal name="NewTitle"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
 </interface>
</node>"""

MENU_XML = """<node>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg name="parentId" type="i" direction="in"/>
   <arg name="recursionDepth" type="i" direction="in"/>
   <arg name="propertyNames" type="as" direction="in"/>
   <arg name="revision" type="u" direction="out"/>
   <arg name="layout" type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg name="ids" type="ai" direction="in"/>
   <arg name="propertyNames" type="as" direction="in"/>
   <arg name="properties" type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg name="id" type="i" direction="in"/>
   <arg name="name" type="s" direction="in"/>
   <arg name="value" type="v" direction="out"/>
  </method>
  <method name="Event">
   <arg name="id" type="i" direction="in"/>
   <arg name="eventId" type="s" direction="in"/>
   <arg name="data" type="v" direction="in"/>
   <arg name="timestamp" type="u" direction="in"/>
  </method>
  <method name="EventGroup">
   <arg name="events" type="a(isvu)" direction="in"/>
   <arg name="idErrors" type="ai" direction="out"/>
  </method>
  <method name="AboutToShow">
   <arg name="id" type="i" direction="in"/>
   <arg name="needUpdate" type="b" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
   <arg name="ids" type="ai" direction="in"/>
   <arg name="updatesNeeded" type="ai" direction="out"/>
   <arg name="idErrors" type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
   <arg name="updatedProps" type="a(ia{sv})"/>
   <arg name="removedProps" type="a(ias)"/>
  </signal>
  <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
  <signal name="ItemActivationRequested"><arg name="id" type="i"/><arg name="timestamp" type="u"/></signal>
 </interface>
</node>"""

# (id, label or None for a separator, action)
_MENU = [(1, N_("Open…"), "open"), (2, N_("New Window"), "new-window"), (3, None, None),
         (4, N_("Quit"), "quit")]


def _icon_theme_path() -> str:
    """A directory with hicolor/…/apps/<APP_ID>.svg for the shell to load our
    icon from (the snap's lives under $SNAP; the shell reads it there)."""
    roots = []
    if runtime.IN_SNAP:
        roots.append(os.path.join(os.environ["SNAP"], "usr", "share"))
    from gi.repository import GLib
    roots += [GLib.get_user_data_dir()] + list(GLib.get_system_data_dirs())
    for r in roots:
        d = os.path.join(r, "icons")
        if os.path.exists(os.path.join(d, "hicolor", "scalable", "apps", runtime.APP_ID + ".svg")):
            return d
    return ""


def _icon_file() -> str | None:
    d = _icon_theme_path()
    if d:
        return os.path.join(d, "hicolor", "scalable", "apps", runtime.APP_ID + ".svg")
    p = os.path.join(runtime.package_root(), "data", runtime.APP_ID + ".svg")   # source checkout
    return p if os.path.exists(p) else None


def _pixmaps() -> list:
    """IconPixmap: the icon as ARGB32 (network byte order) at a few sizes, for
    hosts that can't find it by name."""
    path = _icon_file()
    if not path:
        return []
    out = []
    try:
        from gi.repository import GdkPixbuf
        for size in (22, 32, 48):
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size)
            if not pb.get_has_alpha():
                pb = pb.add_alpha(False, 0, 0, 0)
            w, h, stride = pb.get_width(), pb.get_height(), pb.get_rowstride()
            px = pb.get_pixels()
            argb = bytearray(w * h * 4)
            for y in range(h):
                row = px[y * stride:y * stride + w * 4]
                o = y * w * 4
                argb[o + 0:o + w * 4:4] = row[3::4]
                argb[o + 1:o + w * 4:4] = row[0::4]
                argb[o + 2:o + w * 4:4] = row[1::4]
                argb[o + 3:o + w * 4:4] = row[2::4]
            out.append((w, h, bytes(argb)))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return out


class _SniTray:
    def __init__(self, actions):
        from gi.repository import Gio
        self.actions = actions
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.status = "Passive"
        self.watcher = False
        self.registered = False      # with the watcher that is on the bus now
        self.revision = 1
        self._reg_ids = []
        self._theme_path = None
        self._pixmap = None
        Gio.bus_watch_name_on_connection(self.conn, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                         self._appeared, self._vanished)

    def available(self):
        return self.watcher

    def show(self):
        if not self._reg_ids:
            self._export()
        self._set_status("Active")
        self._register()

    def hide(self):
        self._set_status("Passive")

    # ---- the bus ----
    def _appeared(self, _conn, _name, _owner):
        self.watcher = True
        self.registered = False
        if self._reg_ids:
            self._register()                 # the shell (or its extension) restarted

    def _vanished(self, _conn, _name):
        self.watcher = False
        self.registered = False

    def _export(self):
        from gi.repository import Gio
        reg = getattr(self.conn, "register_object_with_closures2", None) or self.conn.register_object
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._reg_ids = [reg(ITEM_PATH, sni, self._item_call, self._item_prop, None),
                         reg(MENU_PATH, menu, self._menu_call, self._menu_prop, None)]

    def _register(self):
        from gi.repository import Gio, GLib
        if not self.watcher or self.registered:
            return
        self.registered = True

        def done(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error as e:
                self.registered = False
                print(f"View: tray icon not registered: {e.message}", file=sys.stderr)
        self.conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                       GLib.Variant("(s)", (ITEM_PATH,)), None, Gio.DBusCallFlags.NONE, -1,
                       None, done)

    def _set_status(self, status):
        from gi.repository import GLib
        if status == self.status:
            return
        self.status = status
        if self._reg_ids:
            self.conn.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", "NewStatus",
                                  GLib.Variant("(s)", (status,)))

    # ---- StatusNotifierItem ----
    def _item_prop(self, _conn, _sender, _path, _iface, prop):
        from gi.repository import GLib
        if prop == "IconThemePath":
            if self._theme_path is None:
                self._theme_path = _icon_theme_path()
            return GLib.Variant("s", self._theme_path)
        if prop == "IconPixmap":
            if self._pixmap is None:
                self._pixmap = _pixmaps()
            return GLib.Variant("a(iiay)", self._pixmap)
        if prop == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", (runtime.APP_ID, [], runtime.APP_NAME,
                                                 _("Running in the background")))
        values = {"Category": ("s", "ApplicationStatus"), "Id": ("s", runtime.APP_ID),
                  "Title": ("s", runtime.APP_NAME), "Status": ("s", self.status),
                  "WindowId": ("i", 0), "IconName": ("s", runtime.APP_ID),
                  "OverlayIconName": ("s", ""), "AttentionIconName": ("s", ""),
                  "ItemIsMenu": ("b", False), "Menu": ("o", MENU_PATH)}
        if prop in values:
            return GLib.Variant(*values[prop])
        return None

    def _item_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        global _token
        if method in ("Activate", "SecondaryActivate"):
            _run(self.actions, "activate")
        elif method == "ProvideXdgActivationToken":
            _token = params.unpack()[0]
        invocation.return_value(None)

    # ---- com.canonical.dbusmenu ----
    @staticmethod
    def _props(item_id):
        from gi.repository import GLib
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        for i, label, _action in _MENU:
            if i == item_id:
                if label is None:
                    return {"type": GLib.Variant("s", "separator")}
                return {"label": GLib.Variant("s", _(label)), "enabled": GLib.Variant("b", True),
                        "visible": GLib.Variant("b", True)}
        return {}

    def _layout(self, item_id):
        from gi.repository import GLib
        kids = []
        if item_id == 0:
            kids = [GLib.Variant("(ia{sv}av)", (i, self._props(i), [])) for i, *_r in _MENU]
        return (item_id, self._props(item_id), kids)

    def _menu_prop(self, _conn, _sender, _path, _iface, prop):
        from gi.repository import GLib
        values = {"Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"),
                  "IconThemePath": ("as", [])}
        return GLib.Variant(*values[prop]) if prop in values else None

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        from gi.repository import GLib
        args = params.unpack()
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))",
                                                 (self.revision, self._layout(args[0]))))
        elif method == "GetGroupProperties":
            ids = args[0] or [0] + [i for i, *_r in _MENU]
            invocation.return_value(GLib.Variant("(a(ia{sv}))",
                                                 ([(i, self._props(i)) for i in ids],)))
        elif method == "GetProperty":
            v = self._props(args[0]).get(args[1])
            if v is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error.NoSuchProperty",
                                             args[1])
            else:
                invocation.return_value(GLib.Variant("(v)", (v,)))
        elif method == "Event":
            self._event(args[0], args[1])
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _ts in args[0]:
                self._event(item_id, event)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _event(self, item_id, event):
        if event != "clicked":
            return
        for i, _label, action in _MENU:
            if i == item_id and action:
                _run(self.actions, action)


# ---------------------------------------------------------------- Windows ---

_WM_APP_TRAY = 0x8000 + 1          # WM_APP + 1: the icon's mouse messages
_WM_CLOSE, _WM_QUERYENDSESSION, _WM_ENDSESSION = 0x0010, 0x0011, 0x0016


class _WinTray:
    """Shell_NotifyIconW.  The hidden window is created on the main thread:
    GDK's Win32 event source dispatches every message of that thread, ours
    included."""

    def __init__(self, actions):
        import ctypes
        from ctypes import wintypes as wt
        self.actions = actions
        self.ct, self.wt = ctypes, wt
        self.user32, self.shell32 = ctypes.windll.user32, ctypes.windll.shell32
        k32 = ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        u = self.user32
        u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
                                      wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        u.CreateWindowExW.restype = wt.HWND
        u.CreatePopupMenu.restype = wt.HMENU
        u.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
        u.TrackPopupMenuEx.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, wt.HWND,
                                       wt.LPVOID]
        u.TrackPopupMenuEx.restype = wt.BOOL       # (with TPM_RETURNCMD: the item id)
        u.DestroyMenu.argtypes = [wt.HMENU]
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        u.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
        u.RegisterWindowMessageW.restype = wt.UINT
        u.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPVOID]
        u.LoadIconW.restype = wt.HICON
        self.shell32.ExtractIconExW.argtypes = [wt.LPCWSTR, ctypes.c_int,
                                                ctypes.POINTER(wt.HICON),
                                                ctypes.POINTER(wt.HICON), wt.UINT]
        self.shell32.ExtractIconExW.restype = wt.UINT
        self.shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, wt.LPVOID]
        k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        k32.GetModuleHandleW.restype = wt.HMODULE

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", self.WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
                        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD),
                        ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                        ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64),
                        ("dwInfoFlags", wt.DWORD), ("guidItem", ctypes.c_byte * 16),
                        ("hBalloonIcon", wt.HICON)]
        self.NOTIFYICONDATAW = NOTIFYICONDATAW
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]

        hinst = k32.GetModuleHandleW(None)
        # (before CreateWindowExW: the window procedure runs during it already)
        self.taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
        self.shown = False
        self.wanted = False          # show() was called: re-add after Explorer restarts
        self._proc = self.WNDPROC(self._wndproc)          # keep a reference
        cls = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst,
                        lpszClassName="ViewTrayWindow")
        u.RegisterClassW(ctypes.byref(cls))               # (fails harmlessly if registered)
        # a hidden top-level window, not HWND_MESSAGE: those miss TaskbarCreated
        self.hwnd = u.CreateWindowExW(0, "ViewTrayWindow", "View tray", 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        if not self.hwnd:
            raise OSError("cannot create the tray window")
        # the exe's own icon at the small-icon size (SM_CXSMICON, DPI-scaled:
        # the .ico has 16/24/32 px), not the 32 px one squeezed into 16
        small = wt.HICON()
        n = self.shell32.ExtractIconExW(sys.executable, 0, None, ctypes.byref(small), 1)
        self.icon = small.value if n and small.value else \
            u.LoadIconW(None, ctypes.c_void_p(32512))     # IDI_APPLICATION

    def available(self):
        return True

    def _data(self):
        nid = self.NOTIFYICONDATAW()
        nid.cbSize = self.ct.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = 0x1 | 0x2 | 0x4                      # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = _WM_APP_TRAY
        nid.hIcon = self.icon
        nid.szTip = f"{runtime.APP_NAME} — {_('Running in the background')}"[:127]
        return nid

    def show(self):
        self.wanted = True
        if not self.shown:
            self.shown = bool(self.shell32.Shell_NotifyIconW(0, self.ct.byref(self._data())))

    def hide(self):
        self.wanted = False
        if self.shown:
            self.shell32.Shell_NotifyIconW(2, self.ct.byref(self._data()))     # NIM_DELETE
            self.shown = False

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == _WM_APP_TRAY:
                event = lparam & 0xFFFF
                if event == 0x0202:                       # WM_LBUTTONUP
                    _run(self.actions, "activate")
                elif event in (0x0205, 0x007B):           # WM_RBUTTONUP, WM_CONTEXTMENU
                    self._menu()
                return 0
            if msg == self.taskbar_created:
                self.shown = False                        # Explorer (re)started: add it again
                if self.wanted:
                    self.show()
                return 0
            if msg == _WM_CLOSE:
                # not DefWindowProc (it would destroy the window): taskkill
                # without /F, as the uninstaller does, means "quit"
                _run(self.actions, "quit")
                return 0
            if msg == _WM_QUERYENDSESSION:
                return 1                                  # never hold up logoff or shutdown
            if msg == _WM_ENDSESSION:
                if wparam:                                # it is ending: logoff, or Restart Manager
                    self.hide()
                    _run(self.actions, "quit")
                return 0
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        u = self.user32
        menu = u.CreatePopupMenu()
        cmd = 0
        try:
            for i, label, _action in _MENU:
                if label is None:
                    u.AppendMenuW(menu, 0x800, 0, None)                 # MF_SEPARATOR
                else:
                    u.AppendMenuW(menu, 0, i, _(label).replace("&", "&&"))   # MF_STRING
            pt = self.wt.POINT()
            u.GetCursorPos(self.ct.byref(pt))
            u.SetForegroundWindow(self.hwnd)    # or the menu won't close on a click elsewhere
            # TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON
            cmd = u.TrackPopupMenuEx(menu, 0x100 | 0x80 | 0x2, pt.x, pt.y, self.hwnd, None)
            u.PostMessageW(self.hwnd, 0, 0, 0)  # WM_NULL (the documented workaround)
        finally:
            u.DestroyMenu(menu)
        for i, _label, action in _MENU:
            if i == cmd and action:
                _run(self.actions, action)
