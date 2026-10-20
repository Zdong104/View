// Container probing for Get Info: duration, codecs, resolution, frame rate, bit
// rate and tags read straight from the file headers (MP4/MOV/M4A, WebM/Matroska,
// Ogg, MP3, WAV, FLAC). The desktop app asks GStreamer's Discoverer for the same
// summary (preview/video_view.py: discover()). Everything here is best effort and
// returns null / fewer fields when a file doesn't match.

/** Random access into a file (a Blob, or HTTP / file: range requests). */
export interface Reader {
  /** Total size in bytes when known. */
  size: number | null;
  /** Up to `length` bytes starting at `offset` (fewer at the end of the file, empty past it). */
  read(offset: number, length: number): Promise<Uint8Array>;
}

export interface VideoTrack { codec?: string; width?: number; height?: number; fps?: number; bitrate?: number }
export interface AudioTrack { codec?: string; channels?: number; rate?: number; bitrate?: number }
export interface ProbeResult {
  container?: string;
  /** Seconds. */
  duration?: number;
  video: VideoTrack[];
  audio: AudioTrack[];
  tags: Record<string, string>;
}

const latin1 = (b: Uint8Array, s: number, e: number): string => {
  let out = "";
  for (let i = s; i < Math.min(e, b.length); i++) out += String.fromCharCode(b[i]!);
  return out;
};
const utf8 = (b: Uint8Array, s: number, e: number): string => new TextDecoder().decode(b.subarray(s, Math.min(e, b.length)));
const dv = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);
const u32 = (b: Uint8Array, o: number) => dv(b).getUint32(o);
const u16 = (b: Uint8Array, o: number) => dv(b).getUint16(o);
const u64 = (b: Uint8Array, o: number) => Number(dv(b).getBigUint64(o));

export async function probe(reader: Reader): Promise<ProbeResult | null> {
  try {
    const head = await reader.read(0, 4096);
    if (head.length < 12) return null;
    if (latin1(head, 4, 8) === "ftyp") return await probeMp4(reader, head);
    if (u32(head, 0) === 0x1a45dfa3) return await probeMatroska(reader);
    if (latin1(head, 0, 4) === "OggS") return await probeOgg(reader);
    if (latin1(head, 0, 4) === "RIFF" && latin1(head, 8, 12) === "WAVE") return probeWav(head, reader.size);
    if (latin1(head, 0, 4) === "fLaC") return await probeFlac(reader, head);
    if (latin1(head, 0, 3) === "ID3" || (head[0] === 0xff && (head[1]! & 0xe0) === 0xe0)) return await probeMp3(reader, head);
  } catch { /* unreadable: fall back to what the media element reports */ }
  return null;
}

// --------------------------------------------------------------------------
// MP4 / QuickTime / M4A
// --------------------------------------------------------------------------
interface Box { type: string; start: number; end: number } // body range

function* boxes(b: Uint8Array, start: number, end: number): Generator<Box> {
  let o = start;
  while (o + 8 <= end) {
    let size = u32(b, o);
    const type = latin1(b, o + 4, o + 8);
    let hdr = 8;
    if (size === 1) { size = u64(b, o + 8); hdr = 16; } else if (size === 0) size = end - o;
    if (size < hdr) return;
    yield { type, start: o + hdr, end: Math.min(o + size, end) };
    o += size;
  }
}

const MP4_VIDEO: Record<string, string> = {
  avc1: "H.264 / AVC", avc3: "H.264 / AVC", hvc1: "H.265 / HEVC", hev1: "H.265 / HEVC", av01: "AV1", vp09: "VP9", vp08: "VP8",
  mp4v: "MPEG-4 Visual", apcn: "Apple ProRes 422", apch: "Apple ProRes 422 HQ", ap4h: "Apple ProRes 4444", dvh1: "Dolby Vision", jpeg: "Motion JPEG", mjp2: "Motion JPEG 2000",
};
const MP4_AUDIO: Record<string, string> = {
  mp4a: "AAC", Opus: "Opus", "ac-3": "AC-3", "ec-3": "E-AC-3", alac: "Apple Lossless", fLaC: "FLAC", ".mp3": "MP3", sowt: "PCM", twos: "PCM", lpcm: "PCM", "in24": "PCM", ulaw: "µ-law", alaw: "A-law",
};
const H264_PROFILES: Record<number, string> = { 66: "Baseline", 77: "Main", 88: "Extended", 100: "High", 110: "High 10", 122: "High 4:2:2", 244: "High 4:4:4" };
const AAC_TYPES: Record<number, string> = { 1: "AAC Main", 2: "AAC LC", 3: "AAC SSR", 4: "AAC LTP", 5: "HE-AAC", 29: "HE-AAC v2", 23: "AAC LD", 39: "AAC ELD" };

async function probeMp4(reader: Reader, head: Uint8Array): Promise<ProbeResult> {
  const res: ProbeResult = { video: [], audio: [], tags: {} };
  let off = 0;
  for (let i = 0; i < 128; i++) {
    const h = i === 0 ? head : await reader.read(off, 16);
    if (h.length < 8) break;
    let size = u32(h, 0);
    const type = latin1(h, 4, 8);
    let hdr = 8;
    if (size === 1 && h.length >= 16) { size = u64(h, 8); hdr = 16; } else if (size === 0) size = (reader.size ?? off) - off;
    if (type === "ftyp") {
      const brand = latin1(h, hdr, hdr + 4);
      res.container = brand === "qt  " ? "QuickTime" : brand === "M4A " ? "MPEG-4 Audio (M4A)" : brand === "M4V " ? "MPEG-4 Video (M4V)" : brand === "3gp4" || brand.startsWith("3gp") ? "3GPP" : "MPEG-4";
    } else if (type === "moov") {
      if (size > 64 << 20) break;
      const moov = await reader.read(off, size);
      parseMoov(moov, hdr, moov.length, res);
      break;
    }
    if (size < hdr) break;
    off += size;
    if (reader.size !== null && off >= reader.size) break;
  }
  if (reader.size && res.duration && !res.video.some((v) => v.bitrate) && !res.audio.some((a) => a.bitrate)) {
    // whole-file bit rate as a hint when the tracks don't carry one
    const overall = (reader.size * 8) / res.duration;
    const only = res.video.length + res.audio.length === 1 ? (res.video[0] ?? res.audio[0]) : null;
    if (only) only.bitrate = overall;
  }
  return res;
}

function parseMoov(b: Uint8Array, start: number, end: number, res: ProbeResult): void {
  for (const bx of boxes(b, start, end)) {
    if (bx.type === "mvhd") {
      const v = b[bx.start]!;
      const ts = u32(b, bx.start + (v === 1 ? 20 : 12));
      const dur = v === 1 ? u64(b, bx.start + 24) : u32(b, bx.start + 16);
      if (ts) res.duration = dur / ts;
    } else if (bx.type === "trak") parseTrak(b, bx, res);
    else if (bx.type === "udta") {
      for (const m of boxes(b, bx.start, bx.end)) if (m.type === "meta") parseIlst(b, m.start + 4, m.end, res);
    }
  }
}

function parseIlst(b: Uint8Array, start: number, end: number, res: ProbeResult): void {
  for (const bx of boxes(b, start, end)) {
    if (bx.type !== "ilst") continue;
    for (const item of boxes(b, bx.start, bx.end)) {
      const label = { "©nam": "Title", "©ART": "Artist", "©alb": "Album" }[item.type];
      if (!label) continue;
      for (const d of boxes(b, item.start, item.end)) if (d.type === "data") res.tags[label] = utf8(b, d.start + 8, d.end);
    }
  }
}

function parseTrak(b: Uint8Array, trak: Box, res: ProbeResult): void {
  let width = 0, height = 0, ts = 0, dur = 0, handler = "";
  let stsd: Box | null = null, stts: Box | null = null, stsz: Box | null = null;
  for (const bx of boxes(b, trak.start, trak.end)) {
    if (bx.type === "tkhd") {
      const v = b[bx.start]!;
      width = u32(b, bx.start + (v === 1 ? 88 : 76)) / 65536;
      height = u32(b, bx.start + (v === 1 ? 92 : 80)) / 65536;
    } else if (bx.type === "mdia") {
      for (const m of boxes(b, bx.start, bx.end)) {
        if (m.type === "mdhd") {
          const v = b[m.start]!;
          ts = u32(b, m.start + (v === 1 ? 20 : 12));
          dur = v === 1 ? u64(b, m.start + 24) : u32(b, m.start + 16);
        } else if (m.type === "hdlr") handler = latin1(b, m.start + 8, m.start + 12);
        else if (m.type === "minf") {
          for (const s of boxes(b, m.start, m.end)) {
            if (s.type !== "stbl") continue;
            for (const t of boxes(b, s.start, s.end)) {
              if (t.type === "stsd") stsd = t; else if (t.type === "stts") stts = t; else if (t.type === "stsz") stsz = t;
            }
          }
        }
      }
    }
  }
  const seconds = ts ? dur / ts : 0;
  let bitrate: number | undefined;
  if (stsz && seconds > 0) {
    const sample = u32(b, stsz.start + 4), n = u32(b, stsz.start + 8);
    let bytes = sample * n;
    if (!sample) for (let i = 0; i < n; i++) bytes += u32(b, stsz.start + 12 + 4 * i);
    bitrate = (bytes * 8) / seconds;
  }
  const entry = stsd ? stsd.start + 8 : 0; // first sample entry
  const fourcc = stsd && entry + 8 <= stsd.end ? latin1(b, entry + 4, entry + 8) : "";
  if (handler === "vide") {
    const t: VideoTrack = { codec: MP4_VIDEO[fourcc] ?? (fourcc || undefined), bitrate };
    if (entry && entry + 36 <= b.length) { t.width = u16(b, entry + 32); t.height = u16(b, entry + 34); }
    if (!t.width) { t.width = width; t.height = height; }
    if (stts && seconds > 0) {
      const cnt = u32(b, stts.start + 4);
      let samples = 0;
      for (let i = 0; i < cnt; i++) samples += u32(b, stts.start + 8 + 8 * i);
      if (samples > 1) t.fps = samples / seconds;
    }
    // profile / level from avcC / hvcC
    if (entry && entry + 86 < stsd!.end) {
      for (const c of boxes(b, entry + 86, stsd!.end)) {
        if (c.type === "avcC") t.codec = `H.264 / AVC (${H264_PROFILES[b[c.start + 1]!] ?? `profile ${b[c.start + 1]}`}, L${(b[c.start + 3]! / 10).toFixed(1)})`;
        else if (c.type === "hvcC") t.codec = `H.265 / HEVC (${{ 1: "Main", 2: "Main 10", 3: "Main Still Picture", 4: "RExt" }[b[c.start + 1]! & 0x1f] ?? "profile " + (b[c.start + 1]! & 0x1f)}, L${(b[c.start + 12]! / 30).toFixed(1)})`;
      }
    }
    res.video.push(t);
  } else if (handler === "soun") {
    const t: AudioTrack = { codec: MP4_AUDIO[fourcc] ?? (fourcc || undefined), bitrate };
    if (entry && entry + 36 <= b.length) { t.channels = u16(b, entry + 24); t.rate = u32(b, entry + 32) / 65536; }
    if (fourcc === "mp4a" && entry && entry + 36 < stsd!.end) {
      for (const c of boxes(b, entry + 36, stsd!.end)) {
        if (c.type !== "esds") continue;
        const info = parseEsds(b, c.start + 4, c.end);
        if (info.codec) t.codec = info.codec;
        if (info.bitrate && !t.bitrate) t.bitrate = info.bitrate;
      }
    }
    res.audio.push(t);
  }
}

function parseEsds(b: Uint8Array, start: number, end: number): { codec?: string; bitrate?: number } {
  // ES_Descriptor (0x03) > DecoderConfigDescriptor (0x04) > DecoderSpecificInfo (0x05)
  const len = (o: number): [number, number] => { let n = 0, i = 0; for (; i < 4; i++) { const c = b[o + i]!; n = (n << 7) | (c & 0x7f); if (!(c & 0x80)) { i++; break; } } return [n, i]; };
  let o = start;
  if (b[o] !== 3) return {};
  o += 1 + len(o + 1)[1];
  o += 3; // ES_ID + flags
  if (b[o] !== 4) return {};
  o += 1 + len(o + 1)[1];
  const oti = b[o]!;
  const avg = u32(b, o + 9);
  let codec: string | undefined = { 0x40: "AAC", 0x66: "AAC", 0x67: "AAC", 0x68: "AAC", 0x69: "MP3", 0x6b: "MP3", 0xa5: "AC-3", 0xa6: "E-AC-3", 0xdd: "Vorbis" }[oti];
  o += 13;
  if (oti === 0x40 && o < end && b[o] === 5) {
    o += 1 + len(o + 1)[1];
    codec = AAC_TYPES[b[o]! >> 3] ?? "AAC";
  }
  return { codec, bitrate: avg || undefined };
}

// --------------------------------------------------------------------------
// Matroska / WebM (EBML)
// --------------------------------------------------------------------------
const MKV_CODECS: Record<string, string> = {
  V_VP8: "VP8", V_VP9: "VP9", V_AV1: "AV1", "V_MPEG4/ISO/AVC": "H.264 / AVC", "V_MPEGH/ISO/HEVC": "H.265 / HEVC", V_THEORA: "Theora", "V_MPEG4/ISO/SP": "MPEG-4 Visual", "V_MPEG4/ISO/ASP": "MPEG-4 Visual", V_MPEG2: "MPEG-2",
  A_OPUS: "Opus", A_VORBIS: "Vorbis", A_AAC: "AAC", "A_AAC/MPEG4/LC": "AAC LC", A_FLAC: "FLAC", A_AC3: "AC-3", A_EAC3: "E-AC-3", A_DTS: "DTS", "A_MPEG/L3": "MP3", "A_MPEG/L2": "MP2", "A_PCM/INT/LIT": "PCM", A_TRUEHD: "TrueHD",
};
// master elements we descend into
const MKV_MASTERS = new Set([0x1a45dfa3, 0x18538067, 0x1549a966, 0x1654ae6b, 0xae, 0xe0, 0xe1]);

function ebmlId(b: Uint8Array, o: number): [number, number] | null {
  const first = b[o];
  if (first === undefined || first === 0) return null;
  const len = Math.clz32(first) - 23; // 1..4
  if (len > 4 || o + len > b.length) return null;
  let id = 0;
  for (let i = 0; i < len; i++) id = id * 256 + b[o + i]!;
  return [id, len];
}
function ebmlSize(b: Uint8Array, o: number): [number, number] | null {
  const first = b[o];
  if (first === undefined || first === 0) return null;
  const len = Math.clz32(first) - 23; // 1..8
  if (o + len > b.length) return null;
  let v = first & (0xff >> len);
  let allOnes = v === 0xff >> len;
  for (let i = 1; i < len; i++) { v = v * 256 + b[o + i]!; if (b[o + i] !== 0xff) allOnes = false; }
  return [allOnes ? -1 : v, len]; // -1 = unknown size
}

async function probeMatroska(reader: Reader): Promise<ProbeResult> {
  const b = await reader.read(0, 1 << 20);
  const res: ProbeResult = { video: [], audio: [], tags: {} };
  let scale = 1_000_000;
  let cur: { type?: number; codec?: string; w?: number; h?: number; fps?: number; ch?: number; rate?: number } = {};
  const num = (s: number, e: number) => { let v = 0; for (let i = s; i < e; i++) v = v * 256 + b[i]!; return v; };
  const flt = (s: number, e: number) => (e - s === 4 ? dv(b).getFloat32(s) : e - s === 8 ? dv(b).getFloat64(s) : 0);
  let duration = 0;
  const walk = (start: number, end: number): boolean => { // false = stop (reached the clusters)
    let o = start;
    while (o < end) {
      const id = ebmlId(b, o);
      if (!id) return true;
      const sz = ebmlSize(b, o + id[1]);
      if (!sz) return true;
      const body = o + id[1] + sz[1];
      const stop = sz[0] < 0 ? b.length : Math.min(body + sz[0], b.length);
      switch (id[0]) {
        case 0x4282: res.container = utf8(b, body, stop) === "webm" ? "WebM" : "Matroska"; break;
        case 0x1f43b675: return false; // Cluster: tracks and info come before it
        case 0x2ad7b1: scale = num(body, stop); break;
        case 0x4489: duration = flt(body, stop); break;
        case 0x7ba9: res.tags.Title = utf8(b, body, stop); break;
        case 0x83: cur.type = num(body, stop); break;
        case 0x86: cur.codec = latin1(b, body, stop); break;
        case 0x23e383: { const ns = num(body, stop); if (ns) cur.fps = 1e9 / ns; break; }
        case 0xb0: cur.w = num(body, stop); break;
        case 0xba: cur.h = num(body, stop); break;
        case 0xb5: cur.rate = flt(body, stop); break;
        case 0x9f: cur.ch = num(body, stop); break;
        default: break;
      }
      if (MKV_MASTERS.has(id[0])) {
        if (id[0] === 0xae) cur = {};
        const cont = walk(body, stop);
        if (id[0] === 0xae) {
          const name = (c: string | undefined) => (c ? MKV_CODECS[c] ?? c : undefined);
          if (cur.type === 1) res.video.push({ codec: name(cur.codec), width: cur.w, height: cur.h, fps: cur.fps });
          else if (cur.type === 2) res.audio.push({ codec: name(cur.codec), channels: cur.ch, rate: cur.rate });
        }
        if (!cont) return false;
      }
      if (sz[0] < 0) return true;
      o = stop;
    }
    return true;
  };
  walk(0, b.length);
  if (duration) res.duration = (duration * scale) / 1e9;
  return res;
}

// --------------------------------------------------------------------------
// Ogg (Opus / Vorbis / Theora)
// --------------------------------------------------------------------------
async function probeOgg(reader: Reader): Promise<ProbeResult> {
  const b = await reader.read(0, 65536);
  const res: ProbeResult = { container: "Ogg", video: [], audio: [], tags: {} };
  let rate = 0;
  let o = 0;
  for (let page = 0; page < 8 && o + 27 <= b.length; page++) {
    if (latin1(b, o, o + 4) !== "OggS") break;
    const nseg = b[o + 26]!;
    let size = 0;
    for (let i = 0; i < nseg; i++) size += b[o + 27 + i]!;
    const p = o + 27 + nseg;
    if (latin1(b, p, p + 8) === "OpusHead") {
      res.audio.push({ codec: "Opus", channels: b[p + 9], rate: 48000 });
      rate = 48000;
    } else if (b[p] === 1 && latin1(b, p + 1, p + 7) === "vorbis") {
      const br = dv(b).getInt32(p + 20, true);
      rate = dv(b).getUint32(p + 12, true);
      res.audio.push({ codec: "Vorbis", channels: b[p + 11], rate, bitrate: br > 0 ? br : undefined });
    } else if (b[p] === 0x80 && latin1(b, p + 1, p + 7) === "theora") {
      const w = (b[p + 14]! << 16) | (b[p + 15]! << 8) | b[p + 16]!;
      const h = (b[p + 17]! << 16) | (b[p + 18]! << 8) | b[p + 19]!;
      const fps = u32(b, p + 22) / (u32(b, p + 26) || 1);
      res.video.push({ codec: "Theora", width: w, height: h, fps });
    } else if (b[p] === 0x7f && latin1(b, p + 1, p + 5) === "FLAC") res.audio.push({ codec: "FLAC" });
    o = p + size;
  }
  if (reader.size && rate) { // duration from the last page's granule position
    const tail = await reader.read(Math.max(0, reader.size - 65536), 65536);
    for (let i = tail.length - 14; i >= 0; i--) {
      if (tail[i] === 0x4f && latin1(tail, i, i + 4) === "OggS") {
        const granule = Number(dv(tail).getBigInt64(i + 6, true));
        if (granule > 0) res.duration = granule / rate;
        break;
      }
    }
  }
  return res;
}

// --------------------------------------------------------------------------
// WAV, FLAC, MP3
// --------------------------------------------------------------------------
function probeWav(b: Uint8Array, size: number | null): ProbeResult {
  const res: ProbeResult = { container: "WAV", video: [], audio: [], tags: {} };
  let o = 12;
  const d = dv(b);
  let byteRate = 0;
  while (o + 8 <= b.length) {
    const id = latin1(b, o, o + 4);
    const len = d.getUint32(o + 4, true);
    if (id === "fmt ") {
      const tag = d.getUint16(o + 8, true);
      const bits = d.getUint16(o + 22, true);
      byteRate = d.getUint32(o + 16, true);
      const names: Record<number, string> = { 1: "PCM", 3: "PCM (float)", 6: "A-law", 7: "µ-law", 0x11: "IMA ADPCM", 0x55: "MP3", 0xfffe: "PCM (extensible)" };
      res.audio.push({
        codec: `${names[tag] ?? `format 0x${tag.toString(16)}`}${bits ? `, ${bits}-bit` : ""}`,
        channels: d.getUint16(o + 10, true), rate: d.getUint32(o + 12, true), bitrate: byteRate * 8,
      });
    } else if (id === "data") {
      const n = size ? Math.min(len, size - o - 8) : len;
      if (byteRate) res.duration = n / byteRate;
      break;
    }
    o += 8 + len + (len & 1);
  }
  return res;
}

async function probeFlac(reader: Reader, head: Uint8Array): Promise<ProbeResult> {
  const res: ProbeResult = { container: "FLAC", video: [], audio: [], tags: {} };
  let o = 4;
  let buf = head;
  for (let i = 0; i < 16; i++) {
    if (o + 4 > buf.length) buf = await reader.read(0, o + 65536);
    if (o + 4 > buf.length) break;
    const last = buf[o]! & 0x80, type = buf[o]! & 0x7f;
    const len = (buf[o + 1]! << 16) | (buf[o + 2]! << 8) | buf[o + 3]!;
    if (o + 4 + len > buf.length) buf = await reader.read(0, o + 4 + len);
    const p = o + 4;
    if (type === 0) {
      const rate = (buf[p + 10]! << 12) | (buf[p + 11]! << 4) | (buf[p + 12]! >> 4);
      const ch = ((buf[p + 12]! >> 1) & 7) + 1;
      const bits = (((buf[p + 12]! & 1) << 4) | (buf[p + 13]! >> 4)) + 1;
      const total = (buf[p + 13]! & 15) * 2 ** 32 + u32(buf, p + 14);
      res.audio.push({ codec: `FLAC, ${bits}-bit`, channels: ch, rate });
      if (total && rate) res.duration = total / rate;
    } else if (type === 4) { // Vorbis comments
      const d = dv(buf);
      let q = p + 4 + d.getUint32(p, true);
      const n = d.getUint32(q, true);
      q += 4;
      for (let k = 0; k < n && q + 4 <= p + len; k++) {
        const l = d.getUint32(q, true);
        const [key, ...val] = utf8(buf, q + 4, q + 4 + l).split("=");
        const label = { TITLE: "Title", ARTIST: "Artist", ALBUM: "Album" }[key!.toUpperCase()];
        if (label) res.tags[label] = val.join("=");
        q += 4 + l;
      }
    }
    if (last) break;
    o = p + len;
  }
  return res;
}

const MPEG_BITRATES: Record<string, number[]> = { // kbit/s, index 1..14
  "1-3": [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
  "1-2": [32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
  "1-1": [32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
  "2-3": [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
};
const MPEG_RATES: Record<number, number[]> = { 1: [44100, 48000, 32000], 2: [22050, 24000, 16000], 25: [11025, 12000, 8000] };

async function probeMp3(reader: Reader, head: Uint8Array): Promise<ProbeResult | null> {
  const res: ProbeResult = { container: "MP3", video: [], audio: [], tags: {} };
  let start = 0;
  if (latin1(head, 0, 3) === "ID3") {
    const tagLen = ((head[6]! & 0x7f) << 21) | ((head[7]! & 0x7f) << 14) | ((head[8]! & 0x7f) << 7) | (head[9]! & 0x7f);
    start = 10 + tagLen;
    const tag = await reader.read(0, Math.min(start, 1 << 20));
    const ver = tag[3]!;
    let o = 10;
    while (ver >= 3 && o + 10 <= tag.length && tag[o] !== 0) {
      const id = latin1(tag, o, o + 4);
      const len = ver === 4 ? ((tag[o + 4]! & 0x7f) << 21) | ((tag[o + 5]! & 0x7f) << 14) | ((tag[o + 6]! & 0x7f) << 7) | (tag[o + 7]! & 0x7f) : u32(tag, o + 4);
      const label = { TIT2: "Title", TPE1: "Artist", TALB: "Album" }[id];
      if (label && len > 1) {
        const enc = tag[o + 10]!;
        const text = enc === 1 || enc === 2 ? new TextDecoder(enc === 2 ? "utf-16be" : tag[o + 11] === 0xfe ? "utf-16be" : "utf-16le").decode(tag.subarray(o + 11, o + 10 + len)) : enc === 3 ? utf8(tag, o + 11, o + 10 + len) : latin1(tag, o + 11, o + 10 + len);
        res.tags[label] = text.replace(/^﻿|\0+$/g, "");
      }
      o += 10 + len;
    }
  }
  const frames = await reader.read(start, 8192);
  for (let i = 0; i + 4 <= frames.length; i++) {
    if (frames[i] !== 0xff || (frames[i + 1]! & 0xe0) !== 0xe0) continue;
    const h = frames[i + 1]!;
    const verBits = (h >> 3) & 3, layerBits = (h >> 1) & 3;
    if (verBits === 1 || layerBits === 0) continue;
    const version = verBits === 3 ? 1 : 2;
    const layer = 4 - layerBits;
    const br = frames[i + 2]! >> 4, sr = (frames[i + 2]! >> 2) & 3;
    if (br === 0 || br === 15 || sr === 3) continue;
    const table = MPEG_BITRATES[`${version}-${layer}`] ?? MPEG_BITRATES["1-3"]!;
    const kbps = table[br - 1]!;
    const rate = MPEG_RATES[verBits === 3 ? 1 : verBits === 2 ? 2 : 25]![sr]!;
    res.audio.push({ codec: layer === 3 ? "MP3" : `MPEG Layer ${layer}`, channels: (frames[i + 3]! >> 6) === 3 ? 1 : 2, rate, bitrate: kbps * 1000 });
    if (reader.size) res.duration = ((reader.size - start) * 8) / (kbps * 1000);
    return res;
  }
  return res.tags.Title || res.tags.Artist ? res : null;
}

// --------------------------------------------------------------------------
// formatting (shared by Get Info and the tests)
// --------------------------------------------------------------------------
export const channelsText = (n: number): string => ({ 1: "mono", 2: "stereo" } as Record<number, string>)[n] ?? `${n} channels`;
