"""Video / audio viewer around media_player.MediaPlayer (playbin3 ->
gtk4paintablesink, VA-API hardware decode, DMABuf zero-copy).

Space play/pause, Left/Right seek 5 s, Up/Down volume, M mute, F/F11 full
screen, Ctrl+S save frame.  The control bar (speed, loop, volume, full screen)
auto-hides after 2 s without pointer motion while playing.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .media_player import MediaPlayer, init_gst  # noqa: E402
from .i18n import _
from .viewer_base import AUDIO_EXTS, Viewer, file_info_section  # noqa: E402

SEEK_STEP_US = 5_000_000
SEEK_STEP_NS = SEEK_STEP_US * 1000
VOLUME_STEP = 0.05
CODEC_HINT = ("sudo apt install gstreamer1.0-libav gstreamer1.0-plugins-good "
              "gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly")

CSS = """
.vv-audio-title { font-size: 1.4em; font-weight: bold; }
"""
_css_done = False


def _install_css():
    global _css_done
    if _css_done:
        return
    disp = Gdk.Display.get_default()
    if disp is None:
        return
    prov = Gtk.CssProvider()
    prov.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(disp, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_done = True


def fmt_time(us: int) -> str:
    s = max(0, int(us // 1_000_000))
    h, m, sec = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


_probe_cache: OrderedDict = OrderedDict()
_probe_lock = threading.Lock()
_probe_gate = threading.Semaphore(1)        # one Discoverer at a time


def _probe_key(uri):
    try:
        info = Gio.File.new_for_uri(uri).query_info(
            "time::modified,standard::size", Gio.FileQueryInfoFlags.NONE, None)
        return (uri, info.get_attribute_uint64("time::modified"), info.get_size())
    except GLib.Error:
        return (uri, 0, 0)


def discover(uri: str) -> dict | None:
    """Gst.Discoverer summary of a media file (blocking: run in a thread).
    Cached per (uri, mtime, size): each file is probed at most once."""
    key = _probe_key(uri)
    with _probe_lock:
        if key in _probe_cache:
            _probe_cache.move_to_end(key)
            return _probe_cache[key]
    res = _discover(uri)
    with _probe_lock:
        _probe_cache[key] = res
        while len(_probe_cache) > 64:
            _probe_cache.popitem(last=False)
    return res


def _discover(uri: str) -> dict | None:
    try:
        gi.require_version("Gst", "1.0")
        gi.require_version("GstPbutils", "1.0")
        from gi.repository import Gst, GstPbutils
        init_gst()                      # same decoder ranks as playback (no dGPU wake-up)
        disc = GstPbutils.Discoverer.new(8 * Gst.SECOND)
        try:
            info = disc.discover_uri(uri)
        finally:
            del disc                    # synchronous use: pipeline is freed with it
    except Exception:  # noqa: BLE001
        return None
    try:
        out = {"duration": info.get_duration() // 1000 if info.get_duration() > 0 else 0,
               "video": [], "audio": [], "container": None, "tags": {}}
        top = info.get_stream_info()
        if top is not None and isinstance(top, GstPbutils.DiscovererContainerInfo):
            caps = top.get_caps()
            if caps is not None:
                out["container"] = GstPbutils.pb_utils_get_codec_description(caps)
        for v in info.get_video_streams():
            caps = v.get_caps()
            num, den = v.get_framerate_num(), v.get_framerate_denom()
            out["video"].append({
                "codec": GstPbutils.pb_utils_get_codec_description(caps) if caps else None,
                "w": v.get_width(), "h": v.get_height(),
                "fps": (num / den) if den else 0,
                "bitrate": v.get_bitrate() or v.get_max_bitrate(),
            })
        for a in info.get_audio_streams():
            caps = a.get_caps()
            out["audio"].append({
                "codec": GstPbutils.pb_utils_get_codec_description(caps) if caps else None,
                "channels": a.get_channels(), "rate": a.get_sample_rate(),
                "bitrate": a.get_bitrate() or a.get_max_bitrate(),
            })
        tags = info.get_tags()
        if tags is not None:
            for key, label in (("title", "Title"), ("artist", "Artist"), ("album", "Album")):
                ok, val = tags.get_string(key)
                if ok and val:
                    out["tags"][label] = val
        return out
    except Exception:  # noqa: BLE001
        return None


class VideoViewer(Viewer):
    def __init__(self, window):
        super().__init__(window)
        _install_css()
        self.compact = False
        self.set_focusable(True)
        self.gfile: Gio.File | None = None
        self.player: MediaPlayer | None = None
        self.audio_only = False
        self.failed = False
        self._probe = None
        self._load_serial = 0

        self.stack = Gtk.Stack(hexpand=True, vexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.append(self.stack)

        # audio page (shown inside the player for audio-only media)
        self.audio_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                                 valign=Gtk.Align.CENTER, halign=Gtk.Align.FILL,
                                 margin_start=48, margin_end=48, margin_bottom=64)
        self.audio_icon = Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=160,
                                    css_classes=["dim-label"])
        self.audio_title = Gtk.Label(css_classes=["vv-audio-title"], wrap=True,
                                     justify=Gtk.Justification.CENTER)
        self.audio_sub = Gtk.Label(css_classes=["dim-label"])
        for w in (self.audio_icon, self.audio_title, self.audio_sub):
            self.audio_box.append(w)

        # error page
        self.error_page = Adw.StatusPage(icon_name="dialog-warning-symbolic",
                                         title=_("Can’t play this file"))
        self.stack.add_named(self.error_page, "error")

        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        g = Gio.SimpleActionGroup()
        for name, cb in (("save-frame", self.save_frame),
                         ("toggle-mute", self.toggle_mute),
                         ("toggle-play", self.toggle_play)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            g.add_action(a)
        self._actions = g
        self.insert_action_group("video", g)

        sc = Gtk.ShortcutController()
        sc.set_scope(Gtk.ShortcutScope.LOCAL)
        sc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control>s"),
                                         Gtk.NamedAction.new("video.save-frame")))
        self.add_controller(sc)
        self.connect("destroy", lambda *_: self.release())

    def _ensure_player(self) -> MediaPlayer:
        if self.player is None:
            p = MediaPlayer(compact=self.compact, audio_widget=self.audio_box)
            p.connect("error", self._on_error)
            p.connect("info-changed", lambda *_: self._on_meta())
            p.connect("osd", lambda _p, text: _p.show_osd(text))
            self.stack.add_named(p, "player")
            self.player = p
        return self.player

    # ------------------------------------------------------------------
    # Viewer API
    # ------------------------------------------------------------------
    def build_header(self, header: Adw.HeaderBar) -> None:
        if self.compact:
            return
        self.frame_btn = Gtk.Button(icon_name="camera-photo-symbolic",
                                    tooltip_text=_("Save Frame as PNG… (Ctrl+S)"),
                                    action_name="video.save-frame")
        header.pack_start(self.frame_btn)

    def set_compact(self, compact: bool = True):
        compact = bool(compact)
        if compact != self.compact and self.player is not None and self.player.uri is None:
            self.stack.remove(self.player)
            self.player = None
        self.compact = compact

    def get_current_file(self):
        return self.gfile

    def get_title(self) -> str:
        return (self.gfile.get_basename() if self.gfile is not None else None) or "Video"

    def get_subtitle(self) -> str:
        p = self.player
        if p is None or self.failed:
            return ""
        parts = []
        w, h = p.video_size
        if not self.audio_only and w > 0:
            parts.append(f"{w} × {h}")
        if p.duration > 0:
            parts.append(fmt_time(p.duration // 1000))
        return " · ".join(parts)

    def get_info(self):
        sections = [file_info_section(self.gfile)] if self.gfile is not None else []
        rows = []
        p = self._probe
        pl = self.player
        if p is not None:
            dur = p["duration"]
            if dur:
                rows.append(("Duration", fmt_time(dur)))
            if p["container"]:
                rows.append(("Container", p["container"]))
            for i, v in enumerate(p["video"]):
                tag = "Video" if len(p["video"]) == 1 else f"Video {i + 1}"
                bits = []
                if v["w"] and v["h"]:
                    bits.append(f"{v['w']} × {v['h']}")
                if v["fps"]:
                    bits.append(f"{v['fps']:.3g} fps")
                if v["codec"]:
                    bits.append(v["codec"])
                if v["bitrate"]:
                    bits.append(f"{v['bitrate'] / 1000:,.0f} kb/s")
                if bits:
                    rows.append((tag, " · ".join(bits)))
            for i, a in enumerate(p["audio"]):
                tag = "Audio" if len(p["audio"]) == 1 else f"Audio {i + 1}"
                bits = []
                if a["codec"]:
                    bits.append(a["codec"])
                if a["channels"]:
                    bits.append({1: "mono", 2: "stereo"}.get(a["channels"],
                                                             f"{a['channels']} channels"))
                if a["rate"]:
                    bits.append(f"{a['rate'] / 1000:g} kHz")
                if a["bitrate"]:
                    bits.append(f"{a['bitrate'] / 1000:,.0f} kb/s")
                if bits:
                    rows.append((tag, " · ".join(bits)))
            rows += list(p["tags"].items())
        elif pl is not None and not self.failed:
            if pl.duration > 0:
                rows.append(("Duration", fmt_time(pl.duration // 1000)))
            w, h = pl.video_size
            if not self.audio_only and w > 0:
                rows.append(("Resolution", f"{w} × {h}"))
        if pl is not None and not self.failed and not self.audio_only:
            dec = pl.describe_decoder()
            if dec:
                rows.append(("Decoder", dec))
        if rows:
            sections.append(("Media", rows))
        return sections

    def load(self, files):
        gf = files[0]
        self.gfile = gf
        self.failed = False
        self._probe = None
        self._load_serial += 1
        serial = self._load_serial
        name = (gf.get_basename() or "").lower()
        ext = name[name.rfind("."):] if "." in name else ""
        self.audio_only = ext in AUDIO_EXTS
        self.audio_title.set_text(gf.get_basename() or "")
        self.audio_sub.set_text("")
        player = self._ensure_player()
        uri = gf.get_uri()
        player.load_uri(uri, audio_hint=self.audio_only)
        self._show_page()
        GLib.idle_add(lambda: (self.grab_focus(), False)[1])

        def work():
            with _probe_gate:
                if serial != self._load_serial:     # user already moved on
                    return
                res = discover(uri)
            GLib.idle_add(self._on_probe, res, serial)
        threading.Thread(target=work, name="video-probe", daemon=True).start()
        self.refresh_title()

    def release(self):
        p = self.player
        if p is not None:
            p.release()

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    def _show_page(self):
        a = self._actions.lookup_action("save-frame")
        if a is not None:
            a.set_enabled(not (self.failed or self.audio_only))
        if self.failed:
            self.stack.set_visible_child_name("error")
        elif self.player is not None:
            self.stack.set_visible_child_name("player")

    def _on_meta(self):
        p = self.player
        if p is None:
            return
        if p.audio_only != self.audio_only:
            self.audio_only = p.audio_only        # e.g. an audio-only .mp4 / .mkv
            self._show_page()
        if self.audio_only and not self.audio_sub.get_text() and p.duration > 0:
            self.audio_sub.set_text(fmt_time(p.duration // 1000))
        self.refresh_title()

    def _on_probe(self, res, serial=None):
        if serial is not None and serial != self._load_serial:
            return False
        self._probe = res
        if res is not None:
            t = res["tags"].get("Title")
            if t and self.audio_only:
                self.audio_title.set_text(t)
            artist = res["tags"].get("Artist")
            if artist and self.audio_only:
                self.audio_sub.set_text(artist)
            self.refresh_title()
        return False

    def _on_error(self, _player, message):
        self.failed = True
        msg = (message or "").strip()
        msg = GLib.markup_escape_text(msg.splitlines()[0] if msg else "Playback failed")
        self.error_page.set_description(
            _("{msg}\n\nIf the file is fine, GStreamer is probably missing a codec for it. On Ubuntu try:\n<tt>{CODEC_HINT}</tt>").format(msg=msg, CODEC_HINT=CODEC_HINT))
        btn = Gtk.Button(label=_("Open with Default App"), halign=Gtk.Align.CENTER,
                         css_classes=["pill"])
        btn.connect("clicked", self._open_external)
        self.error_page.set_child(btn)
        self._show_page()
        self.refresh_title()

    def _open_external(self, *_):
        if self.gfile is not None:
            Gtk.FileLauncher.new(self.gfile).launch(self.get_root(), None, None, None)

    # ------------------------------------------------------------------
    # controls
    # ------------------------------------------------------------------
    def _osd_show(self, text):
        if self.player is not None:
            self.player.show_osd(text)

    def _usable(self):
        return self.player is not None and self.player.pipeline is not None and not self.failed

    def toggle_play(self):
        if self._usable():
            self.player.toggle_play()
            self.player.show_controls()

    def seek_by(self, delta_us):
        p = self.player
        if not self._usable():
            return
        p.seek_by(delta_us * 1000)
        pos, dur = p.position // 1000, p.duration // 1000
        self._osd_show(f"{fmt_time(pos)} / {fmt_time(dur)}" if dur > 0 else fmt_time(pos))

    def change_volume(self, delta):
        p = self.player
        if not self._usable():
            return
        p.set_volume(p.volume + delta)
        if p.muted and delta > 0:
            p.set_muted(False)
        p.show_controls()
        self._osd_show(f"Volume {round(p.volume * 100)}%")

    def toggle_mute(self):
        if self._usable():
            self.player.toggle_mute()
            self.player.show_controls()

    def toggle_fullscreen(self):
        if self.player is not None:
            self.player.toggle_fullscreen()

    def _on_key(self, _c, keyval, _code, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK |
                    Gdk.ModifierType.SUPER_MASK):
            return False
        if not self._usable():
            return False
        k = Gdk.keyval_name(keyval) or ""
        if k == "space":
            self.toggle_play()
        elif k == "Left":
            self.seek_by(-SEEK_STEP_US)
        elif k == "Right":
            self.seek_by(SEEK_STEP_US)
        elif k == "Up":
            self.change_volume(VOLUME_STEP)
        elif k == "Down":
            self.change_volume(-VOLUME_STEP)
        elif k in ("m", "M"):
            self.toggle_mute()
        elif k in ("f", "F", "F11"):
            if self.compact:
                return False
            self.toggle_fullscreen()
        elif k == "Escape":
            w = self.get_root()
            if w is not None and w.is_fullscreen():
                w.unfullscreen()
            else:
                return False
        else:
            return False
        return True

    # ------------------------------------------------------------------
    # frame export
    # ------------------------------------------------------------------
    def _grab_frame(self):
        """The current video frame as a Gdk.Texture, or None."""
        if self.player is None or self.audio_only:
            return None
        return self.player.grab_frame()

    def save_frame(self):
        if not self._usable():
            return
        if self.audio_only:
            self.toast(_("Audio files have no video frame to save"))
            return
        tex = self._grab_frame()
        if tex is None:
            self.toast(_("No video frame is available yet"))
            return
        stem = os.path.splitext(self.gfile.get_basename() or "frame")[0]
        pos = fmt_time(self.player.get_position_us()).replace(":", "-")
        dlg = Gtk.FileDialog(title=_("Save Frame as PNG"))
        dlg.set_initial_name(f"{stem} {pos}.png")
        if self.gfile is not None and self.gfile.get_parent() is not None:
            dlg.set_initial_folder(self.gfile.get_parent())
        flt = Gtk.FileFilter(name="PNG image")
        flt.add_suffix("png")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)

        def done(d, res):
            try:
                gf = d.save_finish(res)
            except GLib.Error:
                return
            path = gf.get_path()
            if path is None:
                self.toast(_("Only local files are supported"))
                return
            if not path.lower().endswith(".png"):
                path += ".png"
            if tex.save_to_png(path):
                self.toast(_("Saved “{v}”").format(v=os.path.basename(path)))
            else:
                self.toast(_("Could not save the frame"))
        dlg.save(self.get_root(), None, done)
