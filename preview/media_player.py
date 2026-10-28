"""Reusable video / audio player widget: playbin3 -> gtk4paintablesink -> Gtk.Picture.

Why not Gtk.Video / Gtk.MediaFile: GTK's built-in GStreamer backend
  * picks the highest-ranked decoder, which on this laptop is NVDEC on the
    NVIDIA dGPU (wakes it, then copies every frame across PCIe),
  * pushes every frame through glsinkbin (glupload + glcolorconvert into a
    GL texture) and playsink's videoconvert/videobalance, while GSK renders
    with Vulkan - 5x the CPU of the zero-copy path and multi-second paint
    stalls on 4K,
  * cannot change the playback rate, and its GstPlay pipelines outlive the
    widget (closing a window leaked one decoder pipeline per open).

Here VA-API decoders on the display GPU are preferred and their DMABufs go
straight into GTK's Vulkan renderer (no glupload / videoconvert /
videobalance / deinterlace on the path).  Software
decoders still work: the sink takes system-memory YUV as well.

One pipeline per player, torn down (state NULL) on release() and whenever the
widget is unrealized (window closed); it is rebuilt at the same position if
the widget is realized again.
"""
from __future__ import annotations

import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gst, Gtk  # noqa: E402

from . import runtime  # noqa: E402
from .i18n import _  # noqa: E402

RATES = ("0.5", "1", "1.25", "1.5", "2")
HIDE_DELAY_MS = 2000
POS_INTERVAL_MS = 250
START_DELAY_MS = 40
SCRUB_SETTLE_MS = 180

CSS = """
.mp-stage { background-color: black; }
.mp-bar { border-radius: 14px; padding: 4px 6px; margin: 12px; }
.mp-bar.mp-compact { margin: 8px; }
.mp-time { font-feature-settings: "tnum"; font-size: 0.9em; min-width: 3.2em; }
.mp-bar scale { min-width: 60px; }
"""
_css_done = False
_gst_state = None
_live = {"pipelines": 0}


def live_pipelines() -> int:
    """Playback pipelines not yet finalized (for leak checks)."""
    return _live["pipelines"]


def _reap(pipeline):
    """Set a dropped pipeline (whose video sink is already NULL) to NULL off
    the main thread.  Going to NULL joins the streaming threads; GStreamer
    1.28's typefind can sit in its pull loop for a long time when a pipeline
    is torn down mid-preroll, and doing that on the main thread froze the UI
    (rapid file switching)."""
    def work():
        try:
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_state(Gst.CLOCK_TIME_NONE)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=work, name="media-teardown", daemon=True).start()


def _track(obj):
    _live["pipelines"] += 1

    def gone(*_):
        _live["pipelines"] -= 1
    obj.weak_ref(gone)          # None = not initialised, else True/False (usable)


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


def init_gst() -> bool:
    """Gst.init once and fix decoder ranks for this process.  True if the
    gtk4paintablesink player can be used."""
    global _gst_state
    if _gst_state is not None:
        return _gst_state
    try:
        Gst.init(None)
    except Exception:  # noqa: BLE001
        _gst_state = False
        return False
    windows = sys.platform == "win32"
    hw_env = os.environ.get("PREVIEW_VIDEO_HW")
    hw = hw_env == "1" if windows else hw_env != "0"
    reg = Gst.Registry.get()
    for f in reg.get_feature_list(Gst.ElementFactory):
        klass = f.get_metadata("klass") or ""
        if "Decoder" not in klass or "Video" not in klass:
            continue
        name = f.get_name()
        if windows and ("Hardware" in klass or
                        name.startswith(("d3d11", "d3d12", "nv", "qsv", "mf"))):
            # Direct3D 11/12, NVDEC and Quick Sync decoders don't negotiate
            # with gtk4paintablesink here: decodebin3 recreates the same
            # failing decoder over and over and a 10 s clip never starts.
            # The software decoders (FFmpeg, dav1d, libvpx) open it at once.
            f.set_rank(Gst.Rank.PRIMARY + 3 if hw else Gst.Rank.NONE)
        elif name.startswith("va") and "Hardware" in klass:
            # VA-API on the iGPU that drives the panel: DMABuf zero-copy.
            f.set_rank(Gst.Rank.PRIMARY + 1 if hw else Gst.Rank.NONE)
        elif name.startswith(("nv", "cuda")) or (name.startswith("vulkan") and
                                                 "Hardware" in klass):
            # NVDEC on the dGPU (wakes it, copies every frame back to the
            # iGPU) - keep only as a last resort behind the software decoders.
            f.set_rank(Gst.Rank.MARGINAL)
    _gst_state = Gst.ElementFactory.find("gtk4paintablesink") is not None and \
        Gst.ElementFactory.find("playbin3") is not None
    return _gst_state


_pref_caps = None


def _dmabuf_pref_caps():
    """Caps for the sink input that make VA decoders export NV12 / P010 DMABufs
    with a modifier GTK can import.  Without this, vah26xdec (GStreamer 1.28)
    picks the first DMA_DRM format in gtk4paintablesink's list (P012) for 8-bit
    4:2:0 streams and fails to negotiate.  System-memory raw video stays
    allowed for software decoders."""
    global _pref_caps
    if _pref_caps is not None:
        return _pref_caps or None
    _pref_caps = ""
    disp = Gdk.Display.get_default()
    try:
        fm = disp.get_dmabuf_formats() if disp is not None else None
    except Exception:  # noqa: BLE001
        fm = None
    if fm is None:
        return None
    want = {0x3231564E: "NV12", 0x30313050: "P010"}       # DRM fourccs
    entries = {"NV12": [], "P010": []}
    for i in range(fm.get_n_formats()):
        fourcc, mod = fm.get_format(i)
        name = want.get(fourcc)
        if name is None or mod == 0x00FFFFFFFFFFFFFF:      # DRM_FORMAT_MOD_INVALID
            continue
        e = name if mod == 0 else f"{name}:0x{mod:016x}"
        if e not in entries[name]:
            entries[name].append(e)
    # tiled (compressed-free) modifiers first, linear last
    lst = [e for n in ("NV12", "P010") for e in sorted(entries[n], key=lambda x: ":" not in x)]
    if not lst:
        return None
    caps = Gst.Caps.from_string(
        "video/x-raw(memory:DMABuf),format=DMA_DRM,drm-format={%s}; video/x-raw"
        % ",".join(f'(string)"{e}"' for e in lst))
    _pref_caps = caps if caps is not None else ""
    return caps


def _wrap_with_caps(sink, caps):
    b = Gst.Bin.new("vsinkbin")
    cf = Gst.ElementFactory.make("capsfilter", None)
    cf.set_property("caps", caps)
    b.add(cf)
    b.add(sink)
    cf.link(sink)
    b.add_pad(Gst.GhostPad.new("sink", cf.get_static_pad("sink")))
    return b


def fmt_time(ns: int) -> str:
    s = max(0, int(ns // Gst.SECOND)) if ns and ns > 0 else 0
    h, m, sec = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _rate_label(r: float) -> str:
    return f"{r:g}×"


class MediaPlayer(Adw.Bin):
    """Video/audio player with its own auto-hiding control bar.

    compact=True: only play/pause + seek bar (Quick Look).
    audio_widget: shown instead of the picture for audio-only media.
    """

    __gsignals__ = {
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "info-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "osd": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, compact: bool = False, audio_widget: Gtk.Widget | None = None,
                 click_to_pause: bool = True):
        super().__init__(hexpand=True, vexpand=True, focusable=True)
        _install_css()
        init_gst()
        self.compact = compact
        self.uri: str | None = None
        self.pipeline: Gst.Element | None = None
        self.sink: Gst.Element | None = None
        self.paintable = None
        self.audio_only = False
        self.has_audio = False
        self.loop = False
        self.rate = 1.0
        self.volume = 1.0          # cubic (perceived) 0..1
        self.muted = False
        self.duration = -1         # ns
        self.position = 0          # ns
        self.video_size = (0, 0)
        self.decoder_name: str | None = None
        self.decoder_hw = False
        self.failed = False
        self.ended = False
        self.qos_dropped = 0
        self._want_playing = False
        self._state = Gst.State.NULL
        self._seekable = False
        self._seek_busy = False
        self._pending_seek = None   # (pos_ns, accurate)
        self._bus = None
        self._bus_hid = 0
        self._pos_timer = 0
        self._hide_timer = 0
        self._settle_timer = 0
        self._osd_timer = 0
        self._scrubbing = False
        self._suspended = None     # (uri, pos, playing, audio) while unrealized
        self._sink_mode = "dmabuf"
        self._start_id = 0
        self._vsink = None

        self._paused_by_unmap = False
        self._traced = set()
        self._last_xy = None
        self._updating_scale = False

        # ---------------------------------------------------------- stage
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                                   hexpand=True, vexpand=True)
        # Gtk.GraphicsOffload is opt-in: GTK rarely offloads here anyway (the
        # control bar / rounded corners clip it) and when it does the
        # compositor downscales 4K without mipmaps, which shimmers.
        if os.environ.get("PREVIEW_VIDEO_OFFLOAD", "0") == "1":
            offload = Gtk.GraphicsOffload(child=self.picture, hexpand=True, vexpand=True)
            try:
                offload.set_black_background(True)
            except AttributeError:
                pass
        else:
            offload = self.picture
        self.stage = Gtk.Stack(hexpand=True, vexpand=True, css_classes=["mp-stage"])
        self.stage.add_named(offload, "video")
        self.audio_widget = audio_widget
        if audio_widget is not None:
            self.stage.add_named(audio_widget, "audio")

        self.osd_label = Gtk.Label(css_classes=["preview-osd"], halign=Gtk.Align.CENTER,
                                   valign=Gtk.Align.START, margin_top=28, visible=False,
                                   can_target=False)
        self.bar = self._build_bar()
        self.revealer = Gtk.Revealer(child=self.bar, reveal_child=True, valign=Gtk.Align.END,
                                     transition_type=Gtk.RevealerTransitionType.CROSSFADE,
                                     transition_duration=200)
        self.overlay = Gtk.Overlay(child=self.stage)
        self.overlay.add_overlay(self.osd_label)
        self.overlay.add_overlay(self.revealer)
        self.set_child(self.overlay)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *_: self._schedule_hide(400))
        self.overlay.add_controller(motion)
        if click_to_pause and not compact:
            click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
            click.connect("released", self._on_stage_click)
            self.stage.add_controller(click)

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self.connect("destroy", lambda *_: self.release())

    # ================================================================ UI build
    def _build_bar(self):
        bar = Gtk.Box(spacing=4, css_classes=["mp-bar", "osd", "toolbar"],
                      halign=Gtk.Align.FILL, valign=Gtk.Align.END)
        if self.compact:
            bar.add_css_class("mp-compact")
        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic",
                                   tooltip_text=_("Play (Space)"), css_classes=["flat"])
        self.play_btn.connect("clicked", lambda *_: self.toggle_play())
        bar.append(self.play_btn)
        self.pos_label = Gtk.Label(label="0:00", css_classes=["mp-time", "numeric"], xalign=1)
        bar.append(self.pos_label)
        self.adj = Gtk.Adjustment(lower=0, upper=1, step_increment=1, page_increment=10)
        self.scale = Gtk.Scale(adjustment=self.adj, hexpand=True, draw_value=False,
                               valign=Gtk.Align.CENTER)
        self.scale.connect("change-value", self._on_scale_change)
        press = Gtk.GestureClick()
        press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        press.connect("pressed", lambda *_: setattr(self, "_scrubbing", True))
        press.connect("released", self._on_scale_released)
        press.connect("stopped", self._on_scale_released)
        self.scale.add_controller(press)
        bar.append(self.scale)
        self.dur_label = Gtk.Label(label="0:00", css_classes=["mp-time", "numeric"], xalign=0)
        bar.append(self.dur_label)
        self.mute_btn = self.vol_scale = self.rate_btn = self.loop_btn = self.fs_btn = None
        if self.compact:
            return bar

        self.mute_btn = Gtk.Button(icon_name="audio-volume-high-symbolic",
                                   tooltip_text=_("Mute (M)"), css_classes=["flat"])
        self.mute_btn.connect("clicked", lambda *_: self.toggle_mute())
        bar.append(self.mute_btn)
        self.vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        self.vol_scale.set_draw_value(False)
        self.vol_scale.set_size_request(90, -1)
        self.vol_scale.set_valign(Gtk.Align.CENTER)
        self.vol_scale.set_value(self.volume)
        self.vol_scale.set_tooltip_text(_("Volume (↑/↓)"))
        self.vol_scale.connect("value-changed", self._on_vol_scale)
        bar.append(self.vol_scale)

        group = Gio.SimpleActionGroup()
        ra = Gio.SimpleAction.new_stateful("rate", GLib.VariantType.new("s"),
                                          GLib.Variant("s", "1"))
        ra.connect("activate", lambda a, v: self.set_rate(float(v.get_string())))
        group.add_action(ra)
        self._rate_action = ra
        self.insert_action_group("player", group)
        menu = Gio.Menu()
        for r in RATES:
            menu.append(_rate_label(float(r)), f"player.rate::{r}")
        self.rate_btn = Gtk.MenuButton(label="1×", menu_model=menu, tooltip_text=_("Playback Speed"),
                                       css_classes=["flat"], direction=Gtk.ArrowType.UP)
        self.rate_btn.connect("notify::active", lambda *_: self._poke())
        bar.append(self.rate_btn)
        self.loop_btn = Gtk.ToggleButton(icon_name="media-playlist-repeat-symbolic",
                                         tooltip_text=_("Loop"), css_classes=["flat"])
        self.loop_btn.connect("toggled", lambda b: self.set_loop(b.get_active()))
        bar.append(self.loop_btn)
        self.fs_btn = Gtk.Button(icon_name="view-fullscreen-symbolic",
                                 tooltip_text=_("Full Screen (F)"), css_classes=["flat"])
        self.fs_btn.connect("clicked", lambda *_: self.toggle_fullscreen())
        bar.append(self.fs_btn)
        return bar

    # ============================================================ pipeline
    def load_uri(self, uri: str, audio_hint: bool = False, start_ns: int = 0,
                 play: bool = True, sink_mode: str | None = None):
        self._teardown()
        self._sink_mode = sink_mode or os.environ.get("PREVIEW_VIDEO_SINK", "dmabuf")
        self.uri = uri
        self.audio_only = bool(audio_hint)
        self.failed = self.ended = False
        self.duration, self.position = -1, start_ns
        self.video_size = (0, 0)
        self.decoder_name, self.decoder_hw = None, False
        self.qos_dropped = 0
        self._want_playing = play
        self._pending_seek = (start_ns, True) if start_ns > 0 or self.loop else None
        self._show_stage()
        self._sync_controls()
        # Coalesce bursts (sibling switching, Quick Look arrowing through
        # files): only the last request within START_DELAY_MS builds a pipeline.
        self._start_id = GLib.timeout_add(START_DELAY_MS, self._start_pipeline)

    def _start_pipeline(self):
        self._start_id = 0
        uri = self.uri
        if uri is None or self.pipeline is not None:
            return False
        if not init_gst():
            self._fail(_("GStreamer’s GTK 4 video sink ({element}) is not installed").format(
                element="gtk4paintablesink"))
            return
        pb = Gst.ElementFactory.make("playbin3", None)
        sink = Gst.ElementFactory.make("gtk4paintablesink", None)
        if pb is None or sink is None:
            self._fail(_("Could not create the GStreamer playback pipeline"))
            return
        self.sink = sink
        self.paintable = sink.get_property("paintable")
        vsink = sink
        mode = self._sink_mode
        if mode == "gl":
            # GL path (glupload -> RGBA texture).  Not the default: GSK draws
            # with Vulkan here, so DMABufs are the zero-copy currency.
            try:
                ctx = self.paintable.get_property("gl-context")
            except TypeError:
                ctx = None
            if ctx is not None and Gst.ElementFactory.find("glsinkbin") is not None:
                vsink = Gst.ElementFactory.make("glsinkbin", None)
                vsink.set_property("sink", sink)
        elif mode == "sysmem":
            # diagnostic: force system-memory frames (download + upload)
            vsink = Gst.Bin.new(None)
            conv = Gst.ElementFactory.make("videoconvert", None)
            caps = Gst.ElementFactory.make("capsfilter", None)
            caps.set_property("caps", Gst.Caps.from_string("video/x-raw"))
            for e in (conv, caps, sink):
                vsink.add(e)
            conv.link(caps)
            caps.link(sink)
            vsink.add_pad(Gst.GhostPad.new("sink", conv.get_static_pad("sink")))
        else:
            caps = _dmabuf_pref_caps()
            if caps is not None:
                vsink = _wrap_with_caps(sink, caps)
        pb.set_property("video-sink", vsink)
        self._vsink = vsink
        # No videoconvert / videoscale / deinterlace / videobalance in playsink:
        # any of them would force a download of every decoded frame.
        Gst.util_set_object_arg(pb, "flags",
                                "video+audio+text+soft-volume+native-video")
        if Gst.ElementFactory.find("scaletempo") is not None:
            pb.set_property("audio-filter", Gst.ElementFactory.make("scaletempo", None))
        pb.set_property("uri", uri)
        pb.set_property("volume", self.volume ** 3)
        pb.set_property("mute", self.muted)
        pb.connect("element-setup", self._on_element_setup)
        _track(pb)
        self.pipeline = pb
        self.picture.set_paintable(self.paintable)
        bus = pb.get_bus()
        bus.add_signal_watch()
        self._bus = bus
        self._bus_hid = bus.connect("message", self._on_bus_message)
        pb.set_state(Gst.State.PAUSED)
        self._trace("pipeline started")
        self._sync_controls()
        self.show_controls()
        return False

    def _trace(self, what):
        """--startup-trace: when the video got to `what` (once per player)."""
        if runtime.startup_trace and what not in self._traced:
            self._traced.add(what)
            age = runtime.process_age()
            print(f"{age if age is not None else 0:7.2f} s  video: {what}", flush=True)

    def _teardown(self):
        for attr in ("_pos_timer", "_settle_timer", "_start_id"):
            tid = getattr(self, attr)
            if tid:
                GLib.source_remove(tid)
                setattr(self, attr, 0)
        pb, self.pipeline = self.pipeline, None
        bus, self._bus = self._bus, None
        if bus is not None:
            if self._bus_hid:
                bus.disconnect(self._bus_hid)
            bus.remove_signal_watch()
        self._bus_hid = 0
        self.picture.set_paintable(None)
        vs, self._vsink = self._vsink, None
        if vs is not None:
            # gtk4paintablesink must change state on the GTK thread (it
            # releases its paintable/GL objects through the main context), so
            # stop it here; it does not wait on the demuxers.
            vs.set_locked_state(True)
            vs.set_state(Gst.State.NULL)
        if pb is not None:
            _reap(pb)
        self.sink = None
        self.paintable = None
        self._state = Gst.State.NULL
        self._seek_busy = False
        self._pending_seek = None
        self._seekable = False

    def release(self):
        """Stop and free the pipeline (idempotent)."""
        self._suspended = None
        self._teardown()
        for attr in ("_hide_timer", "_osd_timer"):
            tid = getattr(self, attr)
            if tid:
                GLib.source_remove(tid)
                setattr(self, attr, 0)

    # ---- widget lifecycle: never keep a decoder alive without a window -----
    def _on_realize(self, *_):
        s, self._suspended = self._suspended, None
        if s is not None and self.pipeline is None:
            uri, pos, playing, audio = s
            self.load_uri(uri, audio_hint=audio, start_ns=pos, play=playing)

    def _on_unrealize(self, *_):
        if self.pipeline is not None or self._start_id:
            self._suspended = (self.uri, self.position, self._want_playing, self.audio_only)
            self._teardown()

    def _on_map(self, *_):
        if self._paused_by_unmap:
            self._paused_by_unmap = False
            self.play()

    def _on_unmap(self, *_):
        if self.pipeline is not None and self._want_playing and not self.ended:
            self._paused_by_unmap = True
            self.pause()

    # ---- bus --------------------------------------------------------------
    def _on_element_setup(self, _pb, element):
        # streaming thread: only record, UI update goes through idle
        f = element.get_factory()
        if f is None:
            return
        klass = f.get_metadata("klass") or ""
        if "Decoder" in klass and "Video" in klass:
            self.decoder_name = f.get_name()
            self.decoder_hw = "Hardware" in klass
            GLib.idle_add(self._emit_info)

    def _emit_info(self):
        if self.pipeline is not None:
            self.emit("info-changed")
        return False

    def _on_bus_message(self, _bus, msg):
        pb = self.pipeline
        if pb is None:
            return
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            if self._sink_mode != "sysmem" and not self.failed and \
                    "not-negotiated" in (dbg or "") and self.uri:
                # e.g. 4:4:4 / 12-bit streams the display GPU cannot import:
                # retry with a videoconvert in front of the sink.
                if os.environ.get("PREVIEW_DEBUG"):
                    print("media_player: zero-copy negotiation failed, using system memory")
                GLib.idle_add(self._retry_sysmem, self.uri, self.position, self._want_playing,
                              self.audio_only)
                self._teardown()
                return
            # missing decoder for an audio track only: keep the video going
            self._fail(err.message if err is not None else _("Playback failed"), dbg)
        elif t == Gst.MessageType.SEGMENT_DONE:
            if self.loop:
                pb.seek(self.rate, Gst.Format.TIME,
                        Gst.SeekFlags.SEGMENT | Gst.SeekFlags.ACCURATE,
                        Gst.SeekType.SET, 0, Gst.SeekType.NONE, -1)
            else:                   # loop switched off during this pass
                self._on_end()
        elif t == Gst.MessageType.EOS:
            if self.loop:
                self._do_seek(0, True)
            else:
                self._on_end()
        elif t == Gst.MessageType.STATE_CHANGED and msg.src is pb:
            _old, new, _pending = msg.parse_state_changed()
            self._state = new
            if new == Gst.State.PLAYING:
                self._trace("playing")
            if new == Gst.State.PLAYING and not self._pos_timer:
                self._pos_timer = GLib.timeout_add(POS_INTERVAL_MS, self._tick)
            elif new != Gst.State.PLAYING and self._pos_timer:
                GLib.source_remove(self._pos_timer)
                self._pos_timer = 0
            self._sync_controls()
        elif t == Gst.MessageType.ASYNC_DONE:
            self._trace(f"first frame ready ({self.describe_decoder() or 'no video decoder'})")
            self._seek_busy = False
            self._query_duration()
            self._query_size()
            if self._pending_seek is not None:
                pos, acc = self._pending_seek
                self._pending_seek = None
                self._do_seek(pos, acc)
                return
            if self._want_playing and self._state != Gst.State.PLAYING:
                pb.set_state(Gst.State.PLAYING)
            self._tick()
        elif t == Gst.MessageType.DURATION_CHANGED:
            self._query_duration()
        elif t in (Gst.MessageType.STREAM_COLLECTION, Gst.MessageType.STREAMS_SELECTED):
            coll = (msg.parse_stream_collection() if t == Gst.MessageType.STREAM_COLLECTION
                    else msg.parse_streams_selected())
            nv = na = 0
            for i in range(coll.get_size()):
                st = coll.get_stream(i).get_stream_type()
                if st & Gst.StreamType.VIDEO:
                    nv += 1
                if st & Gst.StreamType.AUDIO:
                    na += 1
            self.has_audio = na > 0
            audio_only = nv == 0 and na > 0
            if (nv or na) and audio_only != self.audio_only:
                self.audio_only = audio_only
                self._show_stage()
                self.emit("info-changed")
        elif t == Gst.MessageType.QOS:
            try:
                _fmt, _proc, dropped = msg.parse_qos_stats()
                if msg.src is self.sink:
                    self.qos_dropped = max(self.qos_dropped, dropped)
            except Exception:  # noqa: BLE001
                pass

    def _on_end(self):
        pb = self.pipeline
        if pb is None:
            return
        self.ended = True
        self._want_playing = False
        pb.set_state(Gst.State.PAUSED)
        if self.duration > 0:
            self.position = self.duration
        self._sync_controls()
        self.show_controls(pin=True)

    def _retry_sysmem(self, uri, pos, playing, audio):
        if self.uri == uri and self.pipeline is None and not self.failed:
            self.load_uri(uri, audio_hint=audio, start_ns=pos, play=playing, sink_mode="sysmem")
        return False

    def _fail(self, message, debug=None):
        if debug and os.environ.get("PREVIEW_DEBUG"):
            print("media_player:", message, debug)
        self.failed = True
        self._teardown()
        self.emit("error", (message or _("Playback failed")).strip())

    def _query_duration(self):
        pb = self.pipeline
        if pb is None:
            return
        ok, d = pb.query_duration(Gst.Format.TIME)
        if ok and d > 0 and d != self.duration:
            self.duration = d
            self.adj.set_upper(d / Gst.SECOND)
            self.dur_label.set_text(fmt_time(d))
            self.emit("info-changed")
        q = Gst.Query.new_seeking(Gst.Format.TIME)
        if pb.query(q):
            self._seekable = bool(q.parse_seeking()[1])
        else:
            self._seekable = self.duration > 0

    def _query_size(self):
        if self.sink is None or self.video_size != (0, 0):
            return
        pad = self.sink.get_static_pad("sink")
        caps = pad.get_current_caps() if pad is not None else None
        if caps is None or caps.get_size() == 0:
            return
        s = caps.get_structure(0)
        ok_w, w = s.get_int("width")
        ok_h, h = s.get_int("height")
        if ok_w and ok_h:
            ok, pn, pd = s.get_fraction("pixel-aspect-ratio")
            if ok and pn > 0 and pd > 0 and pn != pd:
                w = round(w * pn / pd)
            self.video_size = (w, h)
            self.emit("info-changed")

    def _tick(self):
        pb = self.pipeline
        if pb is None:
            self._pos_timer = 0
            return False
        if not self._seek_busy and self._pending_seek is None:
            ok, p = pb.query_position(Gst.Format.TIME)
            if ok and p >= 0:
                self.position = p
                if p >= 2 * Gst.SECOND:
                    self._trace(f"played 2 s ({self.qos_dropped} frames dropped)")
        if self.duration <= 0:
            self._query_duration()
        self._update_position_ui()
        return self._state == Gst.State.PLAYING

    def _update_position_ui(self):
        self.pos_label.set_text(fmt_time(self.position))
        if not self._scrubbing and not self._settle_timer:
            self._updating_scale = True
            self.adj.set_value(self.position / Gst.SECOND)
            self._updating_scale = False

    # ---- seeking ------------------------------------------------------------
    def _do_seek(self, pos_ns, accurate=True):
        pb = self.pipeline
        if pb is None:
            return
        pos_ns = max(0, int(pos_ns))
        if self.duration > 0:
            pos_ns = min(pos_ns, self.duration)
        self.position = pos_ns
        if self._seek_busy or self._state < Gst.State.PAUSED:
            self._pending_seek = (pos_ns, accurate)       # coalesce scrubbing
            return
        flags = Gst.SeekFlags.FLUSH | (Gst.SeekFlags.ACCURATE if accurate else
                                       Gst.SeekFlags.KEY_UNIT | Gst.SeekFlags.SNAP_NEAREST)
        if self.loop:
            # segment seek: SEGMENT_DONE instead of EOS, so we can loop
            # without draining the audio sink (no underflow click)
            flags |= Gst.SeekFlags.SEGMENT
        if pb.seek(self.rate, Gst.Format.TIME, flags, Gst.SeekType.SET, pos_ns,
                   Gst.SeekType.NONE, -1):
            self._seek_busy = True
        if self.ended:
            self.ended = False
        self._update_position_ui()

    def seek(self, pos_ns, accurate=True):
        if self.pipeline is None or self.failed:
            return
        if self.ended:
            self.ended = False
        self._do_seek(pos_ns, accurate)

    def seek_by(self, delta_ns):
        if self.pipeline is None or self.failed:
            return
        if self.ended and delta_ns < 0:
            self.ended = False
        self.seek(self.position + delta_ns, True)
        self.show_controls()

    def _on_scale_change(self, _scale, _scroll, value):
        if self.pipeline is None or self._updating_scale:
            return False
        if self.duration > 0:
            value = max(0.0, min(value, self.duration / Gst.SECOND))
        self._scrubbing = True
        # live scrub: fast keyframe seeks, coalesced to one in flight
        self._do_seek(int(value * Gst.SECOND), False)
        self.pos_label.set_text(fmt_time(int(value * Gst.SECOND)))
        if self._settle_timer:
            GLib.source_remove(self._settle_timer)
        self._settle_timer = GLib.timeout_add(SCRUB_SETTLE_MS, self._scrub_settled, value)
        self._poke()
        return False

    def _on_scale_released(self, *_):
        if not self._settle_timer:
            self._scrubbing = False

    def _scrub_settled(self, value):
        self._settle_timer = 0
        self._scrubbing = False
        self._do_seek(int(value * Gst.SECOND), True)          # exact frame on release
        return False

    # ---- transport ----------------------------------------------------------
    def play(self):
        pb = self.pipeline
        if pb is None or self.failed:
            return
        if self.ended:
            self.ended = False
            self._want_playing = True
            self._do_seek(0, True)
        self._want_playing = True
        if not self._seek_busy:
            pb.set_state(Gst.State.PLAYING)
        self._sync_controls()
        self._schedule_hide()

    def pause(self):
        pb = self.pipeline
        if pb is None:
            return
        self._want_playing = False
        pb.set_state(Gst.State.PAUSED)
        self._sync_controls()
        self.show_controls(pin=True)

    def toggle_play(self):
        if self._want_playing and not self.ended:
            self.pause()
        else:
            self.play()

    def is_playing(self):
        return self._want_playing and not self.ended

    def get_position_us(self):
        return self.position // 1000

    def set_rate(self, rate: float):
        rate = max(0.25, min(4.0, float(rate)))
        self.rate = rate
        if getattr(self, "_rate_action", None) is not None:
            self._rate_action.set_state(GLib.Variant("s", f"{rate:g}"))
        if self.rate_btn is not None:
            self.rate_btn.set_label(_rate_label(rate))
        pb = self.pipeline
        if pb is not None and self._state >= Gst.State.PAUSED:
            ok, p = pb.query_position(Gst.Format.TIME)
            if ok:
                self.position = p
            # instant rate change keeps playing without a flush; scaletempo keeps pitch
            done = pb.seek(rate, Gst.Format.TIME, Gst.SeekFlags.INSTANT_RATE_CHANGE,
                           Gst.SeekType.NONE, 0, Gst.SeekType.NONE, 0)
            if not done:
                self._do_seek(self.position, True)
        self.emit("osd", f"Speed {_rate_label(rate)}")

    def set_loop(self, loop: bool):
        was = self.loop
        self.loop = bool(loop)
        pb = self.pipeline
        if self.loop and not was and pb is not None and self._state >= Gst.State.PAUSED \
                and not self.ended:
            ok, p = pb.query_position(Gst.Format.TIME)
            self._do_seek(p if ok else self.position, True)    # re-issue as segment seek
        if self.loop_btn is not None and self.loop_btn.get_active() != self.loop:
            self.loop_btn.set_active(self.loop)

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, v))
        if self.pipeline is not None:
            self.pipeline.set_property("volume", self.volume ** 3)
        if self.vol_scale is not None and abs(self.vol_scale.get_value() - self.volume) > 1e-3:
            self.vol_scale.set_value(self.volume)
        self._sync_controls()

    def set_muted(self, muted: bool):
        self.muted = bool(muted)
        if self.pipeline is not None:
            self.pipeline.set_property("mute", self.muted)
        self._sync_controls()

    def toggle_mute(self):
        self.set_muted(not self.muted)
        self.emit("osd", "Muted" if self.muted else "Unmuted")

    def _on_vol_scale(self, sc):
        v = sc.get_value()
        if abs(v - self.volume) > 1e-3:
            self.set_volume(v)
            if self.muted and v > 0:
                self.set_muted(False)
        self._poke()

    def toggle_fullscreen(self):
        w = self.get_root()
        if w is None or not isinstance(w, Gtk.Window):
            return
        if w.is_fullscreen():
            w.unfullscreen()
        else:
            w.fullscreen()

    def _sync_controls(self):
        playing = self._want_playing and not self.ended
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if playing
                                    else "media-playback-start-symbolic")
        self.play_btn.set_tooltip_text(_("Pause (Space)") if playing else _("Play (Space)"))
        sens = self.pipeline is not None and not self.failed
        self.play_btn.set_sensitive(sens)
        self.scale.set_sensitive(sens and (self._seekable or self.duration > 0))
        if self.mute_btn is not None:
            if self.muted or self.volume == 0:
                icon = "audio-volume-muted-symbolic"
            elif self.volume < 0.34:
                icon = "audio-volume-low-symbolic"
            elif self.volume < 0.67:
                icon = "audio-volume-medium-symbolic"
            else:
                icon = "audio-volume-high-symbolic"
            self.mute_btn.set_icon_name(icon)
            self.mute_btn.set_tooltip_text(_("Unmute (M)") if self.muted else _("Mute (M)"))
        if self.fs_btn is not None:
            root = self.get_root()
            fs = isinstance(root, Gtk.Window) and root.is_fullscreen()
            self.fs_btn.set_icon_name("view-restore-symbolic" if fs else
                                      "view-fullscreen-symbolic")
        if self.duration > 0:
            self.dur_label.set_text(fmt_time(self.duration))

    def _show_stage(self):
        if self.audio_only and self.audio_widget is not None:
            self.stage.set_visible_child_name("audio")
            self.show_controls(pin=True)
        else:
            self.stage.set_visible_child_name("video")

    # ---- auto-hide bar --------------------------------------------------------
    def show_controls(self, pin=False):
        self.revealer.set_reveal_child(True)
        self.overlay.set_cursor(None)
        if pin:
            if self._hide_timer:
                GLib.source_remove(self._hide_timer)
                self._hide_timer = 0
        else:
            self._schedule_hide()

    def _poke(self):
        self.show_controls()

    def _schedule_hide(self, delay=HIDE_DELAY_MS):
        if self._hide_timer:
            GLib.source_remove(self._hide_timer)
        self._hide_timer = GLib.timeout_add(delay, self._auto_hide)

    def _auto_hide(self):
        self._hide_timer = 0
        if not self.is_playing() or self.audio_only or self._scrubbing:
            return False
        if self.rate_btn is not None and self.rate_btn.get_active():
            return False
        self.revealer.set_reveal_child(False)
        self.overlay.set_cursor_from_name("none")
        return False

    def _on_motion(self, _c, x, y):
        if self._last_xy is not None and abs(x - self._last_xy[0]) < 1 and \
                abs(y - self._last_xy[1]) < 1:
            return
        self._last_xy = (x, y)
        self.show_controls()
        # keep the bar while the pointer rests on it
        ok, pt = self.overlay.compute_point(self.bar, Graphene.Point().init(x, y))
        if ok and self.bar.contains(pt.x, pt.y) and self._hide_timer:
            GLib.source_remove(self._hide_timer)
            self._hide_timer = 0

    def _on_stage_click(self, gesture, n_press, _x, _y):
        if self.audio_only or self.failed:
            return
        if n_press == 2:
            self.toggle_fullscreen()
            self.toggle_play()      # undo the first click's toggle
        elif n_press == 1:
            self.toggle_play()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    # ---- OSD --------------------------------------------------------------------
    def show_osd(self, text):
        self.osd_label.set_text(text)
        self.osd_label.set_visible(True)
        if self._osd_timer:
            GLib.source_remove(self._osd_timer)

        def hide():
            self._osd_timer = 0
            self.osd_label.set_visible(False)
            return False
        self._osd_timer = GLib.timeout_add(1000, hide)

    # ---- frame grab -------------------------------------------------------------
    def grab_frame(self):
        """The frame on screen as a Gdk.Texture at its native size, or None."""
        p = self.paintable
        if p is None or self.audio_only:
            return None
        w, h = p.get_intrinsic_width(), p.get_intrinsic_height()
        if w <= 0 or h <= 0:
            w, h = self.video_size
        if w <= 0 or h <= 0:
            return None
        snap = Gtk.Snapshot()
        p.snapshot(snap, w, h)
        node = snap.to_node()
        native = self.get_native()
        if node is None or native is None:
            return None
        return native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))

    def describe_decoder(self) -> str | None:
        if not self.decoder_name:
            return None
        if self.decoder_hw:
            api = "VA-API" if self.decoder_name.startswith("va") else \
                "NVDEC" if self.decoder_name.startswith("nv") else "GPU"
            return f"hardware ({api} {self.decoder_name})"
        return f"software ({self.decoder_name})"
