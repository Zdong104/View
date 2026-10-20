// Video and audio viewer. Port of preview/video_view.py + media_player.py: a
// <video> element (also used for audio) with a View-styled control bar that hides
// itself after 2 s of no pointer movement while playing, an OSD for seeks and
// volume, and the desktop's keyboard shortcuts.
//
// Space play/pause, Left/Right seek 5 s, Up/Down volume 5 %, M mute, F / F11 full
// screen, Ctrl+S save the current frame as PNG (extra: J/L 10 s, Home/End, 0-9).
import type { IconName } from "../../ui/icons.ts";
import { button, el, icon, loadStyle, setIcon } from "../../ui/kit.ts";
import { extOf } from "../../shared/kinds.ts";
import type { DocumentSource, ExportFormat, InfoSection, SaveRequest, SaveResult, Viewer, ViewerContext, ViewerModule } from "../contract.ts";
import { channelsText, probe, type ProbeResult, type Reader } from "./probe.ts";

const SEEK_STEP = 5;
const VOLUME_STEP = 0.05;
const HIDE_DELAY_MS = 2000;
const RATES = [0.5, 1, 1.25, 1.5, 2];
const AUDIO_EXTS = new Set(["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "weba", "oga"]);
const MIME_BY_EXT: Record<string, string> = {
  mp4: "video/mp4", m4v: "video/mp4", mov: "video/quicktime", webm: "video/webm", mkv: "video/x-matroska", ogv: "video/ogg", "3gp": "video/3gpp",
  mp3: "audio/mpeg", m4a: "audio/mp4", wav: "audio/wav", flac: "audio/flac", ogg: "audio/ogg", opus: "audio/ogg", aac: "audio/aac", weba: "audio/webm",
};

export function fmtTime(seconds: number): string {
  const s = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const kbps = (b: number) => `${Math.round(b / 1000).toLocaleString("en-US")} kb/s`;

// ---- a small slider (seek bar, volume) ---------------------------------------------

interface Slider {
  el: HTMLElement;
  set(frac: number, text?: string): void;
  setBuffered(frac: number): void;
}

function slider(cls: string, label: string, onScrub: (frac: number) => void, onDrag: (dragging: boolean) => void): Slider {
  const buf = el("div.mv-buf");
  const fill = el("div.mv-fill");
  const thumb = el("div.mv-thumb");
  const root = el("div", { class: `mv-slider ${cls}`, role: "slider", tabindex: 0, "aria-label": label, "aria-valuemin": 0, "aria-valuemax": 100 },
    el("div.mv-track", {}, buf, fill, thumb));
  const frac = (ev: PointerEvent) => {
    const r = root.getBoundingClientRect();
    return clamp((ev.clientX - r.left) / Math.max(r.width, 1), 0, 1);
  };
  root.addEventListener("pointerdown", (ev) => {
    root.setPointerCapture(ev.pointerId);
    root.classList.add("drag");
    onDrag(true);
    onScrub(frac(ev));
  });
  root.addEventListener("pointermove", (ev) => { if (root.hasPointerCapture(ev.pointerId)) onScrub(frac(ev)); });
  const up = (ev: PointerEvent) => {
    if (!root.hasPointerCapture(ev.pointerId)) return;
    root.releasePointerCapture(ev.pointerId);
    root.classList.remove("drag");
    onDrag(false);
  };
  root.addEventListener("pointerup", up);
  root.addEventListener("pointercancel", up);
  return {
    el: root,
    set(f, text) {
      const p = `${(clamp(f, 0, 1) * 100).toFixed(2)}%`;
      fill.style.width = p;
      thumb.style.left = p;
      root.setAttribute("aria-valuenow", String(Math.round(clamp(f, 0, 1) * 100)));
      if (text) root.setAttribute("aria-valuetext", text);
    },
    setBuffered(f) { buf.style.width = `${(clamp(f, 0, 1) * 100).toFixed(2)}%`; },
  };
}

// ---- range readers for probing --------------------------------------------------------

function readerFor(source: DocumentSource): Reader {
  const url = source.url;
  if (url && /^https?:/i.test(url)) {
    const r: Reader = {
      size: source.size,
      async read(offset, length) {
        const res = await fetch(url, { headers: { Range: `bytes=${offset}-${offset + length - 1}` }, credentials: "include" });
        if (res.status === 416) return new Uint8Array(0);
        const total = /\/(\d+)\s*$/.exec(res.headers.get("content-range") ?? "")?.[1];
        if (total) r.size = Number(total);
        if (res.status === 206) return new Uint8Array(await res.arrayBuffer());
        if (res.status !== 200 || offset !== 0) throw new Error(`no range support (${res.status})`);
        // server ignored Range: take the first bytes and stop the download
        const reader = res.body!.getReader();
        const chunks: Uint8Array[] = [];
        let got = 0;
        while (got < length) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          got += value.length;
        }
        void reader.cancel();
        const out = new Uint8Array(Math.min(got, length));
        let o = 0;
        for (const c of chunks) { const n = Math.min(c.length, out.length - o); out.set(c.subarray(0, n), o); o += n; }
        return out;
      },
    };
    return r;
  }
  if (url && /^file:/i.test(url)) {
    const r: Reader = {
      size: source.size,
      read: (offset, length) => new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("GET", url);
        xhr.responseType = "arraybuffer";
        xhr.setRequestHeader("Range", `bytes=${offset}-${offset + length - 1}`);
        xhr.onload = () => {
          const total = /\/(\d+)\s*$/.exec(xhr.getResponseHeader("content-range") ?? "")?.[1];
          if (total) r.size = Number(total);
          const buf = new Uint8Array(xhr.response as ArrayBuffer);
          resolve(xhr.status === 206 ? buf : buf.subarray(offset, offset + length));
        };
        xhr.onerror = () => reject(new Error("file read failed"));
        xhr.send();
      }),
    };
    return r;
  }
  return {
    size: source.size,
    async read(offset, length) { return new Uint8Array(await (await source.blob()).slice(offset, offset + length).arrayBuffer()); },
  };
}

// ---- the viewer -----------------------------------------------------------------------

class MediaViewer implements Viewer {
  private source!: DocumentSource;
  private ctx!: ViewerContext;
  private root!: HTMLElement;
  private video!: HTMLVideoElement;
  private audioTitle!: HTMLElement;
  private audioSub!: HTMLElement;
  private osdEl!: HTMLElement;
  private errorEl!: HTMLElement;
  private bar!: HTMLElement;
  private playBtn!: HTMLButtonElement;
  private muteBtn!: HTMLButtonElement;
  private rateBtn!: HTMLButtonElement;
  private loopBtn!: HTMLButtonElement;
  private pipBtn!: HTMLButtonElement;
  private fsBtn!: HTMLButtonElement;
  private curEl!: HTMLElement;
  private durEl!: HTMLElement;
  private seek!: Slider;
  private volSlider!: Slider;

  private audioOnly = false;
  private failed = false;
  private scrubbing = false;
  private objectUrl: string | null = null;
  private triedBlob = false;
  private vol = 1;
  private hideTimer = 0;
  private osdTimer = 0;
  private menu: HTMLElement | null = null;
  private probeResult: ProbeResult | null = null;
  private probePromise: Promise<void> = Promise.resolve();
  private raf = 0;

  async mount(container: HTMLElement, source: DocumentSource, ctx: ViewerContext): Promise<void> {
    this.source = source;
    this.ctx = ctx;
    await loadStyle("media");
    const ext = extOf(source.name);
    this.audioOnly = AUDIO_EXTS.has(ext) || !!source.mime?.startsWith("audio/");

    this.video = el("video.mv-video", { playsinline: true, preload: "metadata", "aria-label": source.name });
    this.audioTitle = el("div.mv-audio-title", { text: source.name });
    this.audioSub = el("div.mv-audio-sub");
    const audioPage = el("div.mv-audio", {}, icon("audio"), this.audioTitle, this.audioSub);
    this.osdEl = el("div.mv-osd", { role: "status" });
    this.errorEl = el("div.mv-error", { role: "alert" });
    this.buildBar();
    this.root = el("div.media-view", { "data-viewer": "media", "data-state": "loading", tabindex: 0 },
      this.video, audioPage, el("div.mv-spinner"), this.osdEl, this.bar, this.errorEl);
    this.syncAudioClass();
    container.append(this.root);
    const signal = ctx.signal;

    // stage interaction
    this.root.addEventListener("click", (ev) => {
      if ((ev.target as Element).closest(".mv-bar, .popover, .mv-error")) return;
      if (this.audioOnly || this.failed) return;
      this.togglePlay();
    }, { signal });
    this.root.addEventListener("dblclick", (ev) => {
      if ((ev.target as Element).closest(".mv-bar, .popover, .mv-error") || this.audioOnly || this.failed) return;
      void this.toggleFullscreen(); // the two clicks of the double click cancel each other out
    }, { signal });
    this.root.addEventListener("pointermove", () => this.poke(), { signal });
    this.root.addEventListener("pointerdown", () => this.root.focus({ preventScroll: true }), { signal });
    this.root.addEventListener("focusin", () => this.poke(), { signal });
    document.addEventListener("fullscreenchange", () => {
      const fs = document.fullscreenElement === this.root;
      setIcon(this.fsBtn, fs ? "exitFullscreen" : "fullscreen");
      this.fsBtn.title = this.fsBtn.ariaLabel = fs ? "Exit Full Screen (F)" : "Full Screen (F)";
      this.root.dataset.fullscreen = String(fs);
      this.poke();
    }, { signal });

    this.wireVideo();
    this.registerShortcuts();
    signal.addEventListener("abort", () => this.cleanup(), { once: true });

    this.probePromise = probe(readerFor(source)).then((r) => { this.probeResult = r; this.applyTags(); }, () => {});
    this.loadSource();
    this.root.focus({ preventScroll: true });
  }

  // ---- loading ---------------------------------------------------------------------

  private loadSource(): void {
    const url = this.source.url;
    if (url && /^(https?|file|blob):/i.test(url)) this.video.src = url;
    else void this.loadBlob();
  }

  private async loadBlob(): Promise<void> {
    this.triedBlob = true;
    try {
      const blob = await this.source.blob();
      const type = blob.type || MIME_BY_EXT[extOf(this.source.name)] || "";
      this.objectUrl = URL.createObjectURL(type && !blob.type ? new Blob([blob], { type }) : blob);
      this.video.src = this.objectUrl;
    } catch (e) {
      this.showError((e as Error).message);
    }
  }

  private cleanedUp = false;
  private cleanup(): void {
    if (this.cleanedUp) return;
    this.cleanedUp = true;
    cancelAnimationFrame(this.raf);
    clearTimeout(this.hideTimer);
    clearTimeout(this.osdTimer);
    this.menu?.remove();
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
    if (document.fullscreenElement === this.root) void document.exitFullscreen();
    if (document.pictureInPictureElement === this.video) void document.exitPictureInPicture();
    this.root.remove();
  }

  // ---- video events ----------------------------------------------------------------

  private wireVideo(): void {
    const v = this.video;
    const on = (ev: string, fn: () => void) => v.addEventListener(ev, fn, { signal: this.ctx.signal });
    on("loadedmetadata", () => {
      if (v.videoWidth === 0 && !this.audioOnly) { this.audioOnly = true; this.syncAudioClass(); }
      else if (v.videoWidth > 0 && this.audioOnly && !AUDIO_EXTS.has(extOf(this.source.name))) { this.audioOnly = false; this.syncAudioClass(); }
      if (v.duration === Infinity) this.fixDuration();
      this.root.dataset.width = String(v.videoWidth);
      this.root.dataset.height = String(v.videoHeight);
      this.root.dataset.state = "ready";
      this.updateTimes();
      this.updateAudioSub();
      this.ctx.refreshTitle();
    });
    on("durationchange", () => { this.root.dataset.duration = Number.isFinite(v.duration) ? v.duration.toFixed(3) : ""; this.updateTimes(); this.ctx.refreshTitle(); });
    on("timeupdate", () => this.updateTimes());
    on("progress", () => this.updateBuffered());
    on("play", () => this.syncPlay());
    on("pause", () => this.syncPlay());
    on("ended", () => { this.syncPlay(); this.root.dataset.state = "ended"; });
    on("seeked", () => this.updateTimes());
    on("waiting", () => this.root.classList.add("waiting"));
    on("playing", () => this.root.classList.remove("waiting"));
    on("canplay", () => this.root.classList.remove("waiting"));
    on("volumechange", () => this.syncVolume());
    on("ratechange", () => { this.rateBtn.firstElementChild!.textContent = `${v.playbackRate}×`; this.root.dataset.rate = String(v.playbackRate); });
    on("error", () => this.onError());
    v.addEventListener("enterpictureinpicture", () => this.pipBtn.setAttribute("aria-pressed", "true"), { signal: this.ctx.signal });
    v.addEventListener("leavepictureinpicture", () => this.pipBtn.setAttribute("aria-pressed", "false"), { signal: this.ctx.signal });
    this.syncVolume();
    this.syncPlay();
  }

  /** MediaRecorder / live WebM files report an infinite duration until the end is seen once. */
  private fixDuration(): void {
    const v = this.video;
    const done = () => { v.removeEventListener("timeupdate", done); if (Number.isFinite(v.duration)) v.currentTime = 0; };
    v.addEventListener("timeupdate", done);
    v.currentTime = 1e101;
  }

  private onError(): void {
    const v = this.video;
    if (!v.error || !v.src) return;
    // a direct URL the page can't load itself (file access, odd redirects): retry from bytes
    if (!this.triedBlob && this.source.url && v.error.code !== MediaError.MEDIA_ERR_DECODE) {
      void this.loadBlob();
      return;
    }
    const names: Record<number, string> = {
      [MediaError.MEDIA_ERR_ABORTED]: "Playback was cancelled.",
      [MediaError.MEDIA_ERR_NETWORK]: "A network error interrupted the download.",
      [MediaError.MEDIA_ERR_DECODE]: "The file looks damaged or uses features Chrome can't decode.",
      [MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED]: "Chrome can't play this format or codec.",
    };
    void this.probePromise.then(() => {
      const codecs = [...(this.probeResult?.video ?? []), ...(this.probeResult?.audio ?? [])].map((t) => t.codec).filter(Boolean);
      this.showError(`${names[v.error?.code ?? 0] ?? "Playback failed."}${codecs.length ? `\nCodecs in this file: ${codecs.join(", ")}.` : ""}\n\nChrome's built-in player handles MP4 (H.264/AAC), WebM, Ogg, MP3, WAV and FLAC. Other codecs, such as HEVC or AVI, need a different player.`);
    });
  }

  private showError(text: string): void {
    this.failed = true;
    this.root.dataset.state = "error";
    this.root.classList.remove("waiting");
    this.errorEl.replaceChildren(icon("warning"), el("h2", { text: "Can’t play this file" }), el("p", { text }));
    this.errorEl.classList.add("show");
    this.ctx.refreshTitle();
  }

  // ---- control bar -----------------------------------------------------------------

  private buildBar(): void {
    const mk = (name: IconName, title: string, on: () => void, toggle = false) => {
      const b = button({ title, onClick: on, toggle });
      b.prepend(icon(name));
      return b;
    };
    this.playBtn = mk("play", "Play (Space)", () => this.togglePlay());
    this.curEl = el("span.mv-time.cur", { text: "0:00" });
    this.seek = slider("seek", "Seek", (f) => {
      const d = this.video.duration;
      if (Number.isFinite(d)) { this.video.currentTime = f * d; this.updateTimes(); }
    }, (d) => { this.scrubbing = d; if (!d) this.poke(); });
    this.durEl = el("span.mv-time", { text: "0:00" });
    this.muteBtn = mk("volume", "Mute (M)", () => this.toggleMute());
    this.volSlider = slider("vol", "Volume (↑/↓)", (f) => {
      this.setVolume(f);
      if (this.video.muted && f > 0) this.video.muted = false;
    }, () => {});
    this.rateBtn = button({ label: "1×", title: "Playback Speed", onClick: (ev) => { ev.stopPropagation(); this.openRateMenu(); } });
    this.rateBtn.setAttribute("aria-haspopup", "menu");
    this.loopBtn = mk("loop", "Loop", () => { this.video.loop = this.loopBtn.getAttribute("aria-pressed") === "true"; this.root.dataset.loop = String(this.video.loop); }, true);
    this.pipBtn = mk("pip", "Picture in Picture", () => void this.togglePip(), true);
    this.pipBtn.hidden = !document.pictureInPictureEnabled;
    this.fsBtn = mk("fullscreen", "Full Screen (F)", () => void this.toggleFullscreen());
    this.bar = el("div.mv-bar", { role: "group", "aria-label": "Playback controls" },
      this.playBtn, this.curEl, this.seek.el, this.durEl, this.muteBtn, this.volSlider.el, this.rateBtn, this.loopBtn, this.pipBtn, this.fsBtn);
    this.bar.addEventListener("click", (ev) => ev.stopPropagation());
    this.bar.addEventListener("dblclick", (ev) => ev.stopPropagation());
    this.seek.set(0, "0:00");
  }

  private syncAudioClass(): void {
    if (!this.root) return;
    this.root.classList.toggle("is-audio", this.audioOnly);
    this.root.dataset.audio = String(this.audioOnly);
    if (this.pipBtn) this.pipBtn.hidden = this.audioOnly || !document.pictureInPictureEnabled;
    if (this.fsBtn) this.fsBtn.hidden = this.audioOnly;
    if (this.audioOnly) this.root.classList.remove("hide-controls");
  }

  private updateTimes(): void {
    const v = this.video;
    const d = Number.isFinite(v.duration) ? v.duration : 0;
    this.seek.set(d ? v.currentTime / d : 0, `${fmtTime(v.currentTime)} of ${fmtTime(d)}`);
    this.curEl.textContent = fmtTime(v.currentTime);
    this.durEl.textContent = fmtTime(d);
    this.root.dataset.current = v.currentTime.toFixed(2);
    this.updateBuffered();
  }

  private updateBuffered(): void {
    const v = this.video;
    const d = v.duration;
    if (!Number.isFinite(d) || !d || !v.buffered.length) return this.seek.setBuffered(0);
    let end = 0;
    for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= v.currentTime + 0.5) end = Math.max(end, v.buffered.end(i));
    this.seek.setBuffered(end / d);
  }

  private syncPlay(): void {
    const v = this.video;
    const playing = !v.paused && !v.ended;
    setIcon(this.playBtn, playing ? "pause" : "play");
    this.playBtn.title = this.playBtn.ariaLabel = playing ? "Pause (Space)" : "Play (Space)";
    this.root.dataset.paused = String(!playing);
    if (this.root.dataset.state !== "error" && this.root.dataset.state !== "loading") this.root.dataset.state = playing ? "playing" : v.ended ? "ended" : "paused";
    cancelAnimationFrame(this.raf);
    if (playing) { // smooth seek bar while playing
      const tick = () => { this.updateTimes(); this.raf = requestAnimationFrame(tick); };
      this.raf = requestAnimationFrame(tick);
    }
    this.poke();
  }

  private syncVolume(): void {
    const v = this.video;
    const muted = v.muted || v.volume === 0;
    setIcon(this.muteBtn, muted ? "muted" : this.vol < 0.4 ? "volumeLow" : "volume");
    this.muteBtn.title = this.muteBtn.ariaLabel = v.muted ? "Unmute (M)" : "Mute (M)";
    this.volSlider.set(v.muted ? 0 : this.vol, `${Math.round(this.vol * 100)}%`);
    this.root.dataset.volume = this.vol.toFixed(2);
    this.root.dataset.muted = String(v.muted);
  }

  /** The slider and shortcuts work on perceived loudness; the element wants amplitude (cubic, like the desktop). */
  private setVolume(f: number): void {
    this.vol = clamp(f, 0, 1);
    this.video.volume = this.vol ** 3;
    this.syncVolume();
  }

  // ---- OSD, auto-hide, menus -------------------------------------------------------

  private osd(text: string): void {
    this.osdEl.textContent = text;
    this.osdEl.classList.add("show");
    clearTimeout(this.osdTimer);
    this.osdTimer = window.setTimeout(() => this.osdEl.classList.remove("show"), 1000);
  }

  private poke(): void {
    this.root.classList.remove("hide-controls");
    clearTimeout(this.hideTimer);
    this.hideTimer = window.setTimeout(() => {
      const busy = this.bar.matches(":hover") || !!this.menu || this.scrubbing;
      if (!this.video.paused && !this.video.ended && !this.audioOnly && !this.failed && !busy) this.root.classList.add("hide-controls");
      else if (!this.video.paused && !this.audioOnly && busy) this.poke();
    }, HIDE_DELAY_MS);
  }

  private openRateMenu(): void {
    if (this.menu) { this.menu.remove(); this.menu = null; return; }
    const menu = el("div.popover", { role: "menu" });
    for (const r of RATES) {
      menu.append(el("button.menu-item", {
        type: "button", role: "menuitemradio", "aria-checked": this.video.playbackRate === r,
        onclick: () => { this.video.playbackRate = r; this.osd(`Speed ${r}×`); close(); },
      }, el("span", { text: `${this.video.playbackRate === r ? "✓ " : ""}${r}×` })));
    }
    const rect = this.rateBtn.getBoundingClientRect();
    menu.style.minWidth = "110px";
    menu.style.bottom = `${innerHeight - rect.top + 6}px`;
    menu.style.left = `${Math.max(8, rect.left - 20)}px`;
    this.root.append(menu);
    menu.style.left = `${Math.max(8, Math.min(rect.left - 20, innerWidth - menu.offsetWidth - 8))}px`;
    this.menu = menu;
    const close = () => { menu.remove(); this.menu = null; removeEventListener("click", close); removeEventListener("keydown", esc, true); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    setTimeout(() => { addEventListener("click", close); addEventListener("keydown", esc, true); });
    this.ctx.signal.addEventListener("abort", close, { once: true });
  }

  // ---- commands --------------------------------------------------------------------

  private usable(): boolean { return !this.failed && !!this.video.src; }

  togglePlay(): void {
    if (!this.usable()) return;
    const v = this.video;
    if (v.paused || v.ended) {
      if (v.ended) v.currentTime = 0;
      void v.play().catch(() => {});
    } else v.pause();
    this.poke();
  }

  private seekBy(delta: number): void {
    if (!this.usable()) return;
    const v = this.video;
    const d = Number.isFinite(v.duration) ? v.duration : Infinity;
    v.currentTime = clamp(v.currentTime + delta, 0, d);
    this.osd(Number.isFinite(d) ? `${fmtTime(v.currentTime)} / ${fmtTime(d)}` : fmtTime(v.currentTime));
    this.poke();
  }

  private seekTo(frac: number): void {
    if (!this.usable() || !Number.isFinite(this.video.duration)) return;
    this.video.currentTime = frac * this.video.duration;
    this.osd(`${fmtTime(this.video.currentTime)} / ${fmtTime(this.video.duration)}`);
  }

  private changeVolume(delta: number): void {
    if (!this.usable()) return;
    this.setVolume(this.vol + delta);
    if (this.video.muted && delta > 0) this.video.muted = false;
    this.osd(`Volume ${Math.round(this.vol * 100)}%`);
    this.poke();
  }

  private toggleMute(): void {
    if (!this.usable()) return;
    this.video.muted = !this.video.muted;
    this.osd(this.video.muted ? "Muted" : "Unmuted");
    this.poke();
  }

  private async toggleFullscreen(): Promise<void> {
    if (this.audioOnly) return;
    if (document.fullscreenElement === this.root) await document.exitFullscreen();
    else await this.root.requestFullscreen().catch(() => {});
  }

  private async togglePip(): Promise<void> {
    try {
      if (document.pictureInPictureElement === this.video) await document.exitPictureInPicture();
      else await this.video.requestPictureInPicture();
    } catch (e) {
      this.pipBtn.setAttribute("aria-pressed", String(document.pictureInPictureElement === this.video));
      this.ctx.toast(`Picture in Picture isn't available: ${(e as Error).message}`);
    }
  }

  private registerShortcuts(): void {
    const sc = this.ctx.shortcuts;
    sc.add({ keys: "Space", description: "Play / pause", run: () => this.togglePlay() });
    sc.add({ keys: "K", description: "Play / pause", run: () => this.togglePlay() });
    sc.add({ keys: "ArrowLeft", description: "Back 5 seconds", run: () => this.seekBy(-SEEK_STEP) });
    sc.add({ keys: "ArrowRight", description: "Forward 5 seconds", run: () => this.seekBy(SEEK_STEP) });
    sc.add({ keys: "J", description: "Back 10 seconds", run: () => this.seekBy(-2 * SEEK_STEP) });
    sc.add({ keys: "L", description: "Forward 10 seconds", run: () => this.seekBy(2 * SEEK_STEP) });
    sc.add({ keys: "ArrowUp", description: "Volume up", run: () => this.changeVolume(VOLUME_STEP) });
    sc.add({ keys: "ArrowDown", description: "Volume down", run: () => this.changeVolume(-VOLUME_STEP) });
    sc.add({ keys: "M", description: "Mute", run: () => this.toggleMute() });
    sc.add({ keys: "F", description: "Full screen", run: () => void this.toggleFullscreen() });
    sc.add({ keys: "F11", description: "Full screen", run: () => void this.toggleFullscreen() });
    sc.add({ keys: "Home", description: "Go to start", run: () => this.seekTo(0) });
    sc.add({ keys: "End", description: "Go to end", run: () => this.seekTo(1) });
    for (let i = 0; i <= 9; i++) sc.add({ keys: String(i), description: i === 0 ? "Go to start" : `Jump to ${i}0 %`, run: () => this.seekTo(i / 10) });
  }

  // ---- title, tags, Get Info -------------------------------------------------------

  private applyTags(): void {
    const t = this.probeResult?.tags ?? {};
    if (this.audioOnly) {
      if (t.Title) this.audioTitle.textContent = t.Title;
      this.updateAudioSub();
    }
    this.ctx.refreshTitle();
  }

  private updateAudioSub(): void {
    const d = this.video.duration;
    const parts = [this.probeResult?.tags.Artist, Number.isFinite(d) && d > 0 ? fmtTime(d) : null].filter(Boolean);
    this.audioSub.textContent = parts.join(" · ");
  }

  getTitle(): string {
    return this.source.name;
  }

  getSubtitle(): string {
    if (this.failed || !this.video) return "";
    const v = this.video;
    const parts: string[] = [];
    if (!this.audioOnly && v.videoWidth > 0) parts.push(`${v.videoWidth} × ${v.videoHeight}`);
    if (Number.isFinite(v.duration) && v.duration > 0) parts.push(fmtTime(v.duration));
    return parts.join(" · ");
  }

  async getInfo(): Promise<InfoSection[]> {
    await Promise.race([this.probePromise, new Promise((r) => setTimeout(r, 5000))]);
    const p = this.probeResult;
    const v = this.video;
    const rows: InfoSection["rows"] = [];
    const duration = Number.isFinite(v.duration) && v.duration > 0 ? v.duration : p?.duration;
    if (duration) rows.push(["Duration", fmtTime(duration)]);
    if (p) {
      if (p.container) rows.push(["Container", p.container]);
      p.video.forEach((t, i) => {
        const bits: string[] = [];
        if (t.width && t.height) bits.push(`${t.width} × ${t.height}`);
        if (t.fps) bits.push(`${Number(t.fps.toPrecision(4))} fps`);
        if (t.codec) bits.push(t.codec);
        if (t.bitrate) bits.push(kbps(t.bitrate));
        if (bits.length) rows.push([p.video.length === 1 ? "Video" : `Video ${i + 1}`, bits.join(" · ")]);
      });
      p.audio.forEach((t, i) => {
        const bits: string[] = [];
        if (t.codec) bits.push(t.codec);
        if (t.channels) bits.push(channelsText(t.channels));
        if (t.rate) bits.push(`${Number((t.rate / 1000).toPrecision(4))} kHz`);
        if (t.bitrate) bits.push(kbps(t.bitrate));
        if (bits.length) rows.push([p.audio.length === 1 ? "Audio" : `Audio ${i + 1}`, bits.join(" · ")]);
      });
      for (const [k, val] of Object.entries(p.tags)) rows.push([k, val]);
    }
    if (!this.audioOnly && v.videoWidth > 0 && !p?.video.some((t) => t.width)) rows.push(["Resolution", `${v.videoWidth} × ${v.videoHeight}`]);
    return rows.length ? [{ title: "Media", rows }] : [];
  }

  // ---- save the current frame -----------------------------------------------------

  exportFormats(): ExportFormat[] {
    return this.audioOnly ? [] : [{ id: "frame", label: "Current Frame as PNG…" }];
  }

  async save(_req: SaveRequest): Promise<SaveResult | null> {
    if (this.failed || !this.usable()) return null;
    if (this.audioOnly) { this.ctx.toast("Audio files have no video frame to save"); return null; }
    const v = this.video;
    if (v.videoWidth === 0 || v.readyState < 2) { this.ctx.toast("No video frame is available yet"); return null; }
    const blob = await this.grabFrame();
    if (!blob) { this.ctx.toast("Could not save the frame"); return null; }
    const stem = this.source.name.replace(/\.[^.]*$/, "") || "frame";
    return {
      blob, replacesOriginal: false,
      suggestedName: `${stem} ${fmtTime(v.currentTime).replaceAll(":", "-")}.png`,
      types: [{ description: "PNG image", accept: { "image/png": [".png"] } }],
    };
  }

  private async grabFrame(): Promise<Blob | null> {
    const draw = (src: HTMLVideoElement) => {
      const c = document.createElement("canvas");
      c.width = src.videoWidth; c.height = src.videoHeight;
      c.getContext("2d")!.drawImage(src, 0, 0);
      return new Promise<Blob | null>((r) => c.toBlob(r, "image/png"));
    };
    try {
      return await draw(this.video);
    } catch { // cross-origin video taints the canvas: decode a same-origin copy at the same time
      const url = URL.createObjectURL(await this.source.blob());
      const tmp = document.createElement("video");
      tmp.muted = true;
      tmp.src = url;
      try {
        await new Promise<void>((res, rej) => { tmp.onloadedmetadata = () => res(); tmp.onerror = () => rej(new Error("decode failed")); });
        tmp.currentTime = this.video.currentTime;
        await new Promise<void>((res) => { tmp.onseeked = () => res(); });
        return await draw(tmp);
      } catch {
        return null;
      } finally {
        URL.revokeObjectURL(url);
      }
    }
  }

  destroy(): void {
    this.cleanup();
  }
}

const mod: ViewerModule = { kind: "media", create: () => new MediaViewer() };
export default mod;
