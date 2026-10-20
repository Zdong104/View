// Container probing for Get Info (src/viewers/media/probe.ts) on synthetic files.
import assert from "node:assert/strict";
import { test } from "node:test";
import { probe, type Reader } from "../../src/viewers/media/probe.ts";
import { fmtTime } from "../../src/viewers/media/index.ts";

const reader = (b: Uint8Array): Reader => ({ size: b.length, read: async (o, n) => b.slice(o, o + n) });
const cat = (...p: Uint8Array[]) => { const out = new Uint8Array(p.reduce((n, x) => n + x.length, 0)); let o = 0; for (const x of p) { out.set(x, o); o += x.length; } return out; };
const ascii = (s: string) => Uint8Array.from(s, (c) => c.charCodeAt(0));
const be32 = (n: number) => Uint8Array.of(n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255);
const be16 = (n: number) => Uint8Array.of(n >> 8, n & 255);
const box = (type: string, ...body: Uint8Array[]) => { const b = cat(...body); return cat(be32(8 + b.length), ascii(type), b); };
const zeros = (n: number) => new Uint8Array(n);

function mp4(): Uint8Array {
  const ts = 1000;
  const mvhd = box("mvhd", zeros(4), zeros(8), be32(ts), be32(10_000), zeros(80));
  const tkhd = (w: number, h: number) => box("tkhd", zeros(4), zeros(72), be32(w << 16), be32(h << 16));
  const mdhd = box("mdhd", zeros(4), zeros(8), be32(30_000), be32(300_000), zeros(4)); // 10 s at 30 kHz
  const hdlr = (t: string) => box("hdlr", zeros(4), zeros(4), ascii(t), zeros(12));
  const avcC = box("avcC", Uint8Array.of(1, 100, 0, 40), zeros(4));
  const avc1 = box("avc1", zeros(6), be16(1), zeros(16), be16(1280), be16(720), zeros(50), avcC); // 8 + 78 + children
  const mp4a = box("mp4a", zeros(6), be16(1), zeros(8), be16(2), be16(16), zeros(4), be32(44100 << 16));
  const stsd = (entry: Uint8Array) => box("stsd", zeros(4), be32(1), entry);
  const stts = box("stts", zeros(4), be32(1), be32(300), be32(1000)); // 300 samples * 1000 ticks / 30000 = 10 s -> 30 fps
  const stsz = box("stsz", zeros(4), be32(25_000), be32(300)); // 25 kB each -> 6 Mb/s
  const trak = (w: number, h: number, hd: string, entry: Uint8Array, extra: Uint8Array[]) =>
    box("trak", tkhd(w, h), box("mdia", mdhd, hdlr(hd), box("minf", box("stbl", stsd(entry), ...extra))));
  const moov = box("moov", mvhd, trak(1280, 720, "vide", avc1, [stts, stsz]), trak(0, 0, "soun", mp4a, []));
  return cat(box("ftyp", ascii("isom"), zeros(4), ascii("isomavc1")), box("mdat", zeros(64)), moov);
}

test("MP4: container, duration, H.264 profile, resolution, fps, bit rate, audio", async () => {
  const r = (await probe(reader(mp4())))!;
  assert.equal(r.container, "MPEG-4");
  assert.equal(r.duration, 10);
  assert.equal(r.video.length, 1);
  assert.equal(r.video[0]!.width, 1280);
  assert.equal(r.video[0]!.height, 720);
  assert.equal(r.video[0]!.codec, "H.264 / AVC (High, L4.0)");
  assert.equal(Math.round(r.video[0]!.fps!), 30);
  assert.equal(Math.round(r.video[0]!.bitrate! / 1000), 6000);
  assert.equal(r.audio[0]!.codec, "AAC");
  assert.equal(r.audio[0]!.channels, 2);
  assert.equal(r.audio[0]!.rate, 44100);
});

const vint = (n: number) => Uint8Array.of(0x80 | n); // 1-byte EBML size
function ebml(id: number[], ...body: Uint8Array[]) { const b = cat(...body); return cat(Uint8Array.from(id), vint(b.length), b); }

test("WebM: doc type, duration, VP9 + Opus tracks", async () => {
  const f64 = (v: number) => { const d = new DataView(new ArrayBuffer(8)); d.setFloat64(0, v); return new Uint8Array(d.buffer); };
  const header = ebml([0x1a, 0x45, 0xdf, 0xa3], ebml([0x42, 0x82], ascii("webm")));
  const info = ebml([0x15, 0x49, 0xa9, 0x66], ebml([0x2a, 0xd7, 0xb1], be32(1_000_000)), ebml([0x44, 0x89], f64(2500)));
  const video = ebml([0xae], ebml([0x83], Uint8Array.of(1)), ebml([0x86], ascii("V_VP9")),
    ebml([0x23, 0xe3, 0x83], be32(33_333_333)), ebml([0xe0], ebml([0xb0], be16(640)), ebml([0xba], be16(360))));
  const audio = ebml([0xae], ebml([0x83], Uint8Array.of(2)), ebml([0x86], ascii("A_OPUS")), ebml([0xe1], ebml([0x9f], Uint8Array.of(2)), ebml([0xb5], f64(48000).slice(0, 8))));
  const segment = ebml([0x18, 0x53, 0x80, 0x67], info, ebml([0x16, 0x54, 0xae, 0x6b], video, audio));
  const r = (await probe(reader(cat(header, segment, zeros(16)))))!;
  assert.equal(r.container, "WebM");
  assert.equal(r.duration, 2.5);
  assert.deepEqual([r.video[0]!.codec, r.video[0]!.width, r.video[0]!.height, Math.round(r.video[0]!.fps!)], ["VP9", 640, 360, 30]);
  assert.equal(r.audio[0]!.codec, "Opus");
  assert.equal(r.audio[0]!.channels, 2);
});

test("WAV, FLAC and MP3 headers", async () => {
  const le32 = (n: number) => Uint8Array.of(n & 255, (n >> 8) & 255, (n >> 16) & 255, n >>> 24);
  const le16 = (n: number) => Uint8Array.of(n & 255, n >> 8);
  const wavBytes = cat(ascii("RIFF"), le32(36 + 32000), ascii("WAVEfmt "), le32(16), le16(1), le16(2), le32(8000), le32(32000), le16(4), le16(16), ascii("data"), le32(32000), zeros(32000));
  const w = (await probe(reader(wavBytes)))!;
  assert.equal(w.container, "WAV");
  assert.equal(w.duration, 1);
  assert.deepEqual([w.audio[0]!.channels, w.audio[0]!.rate, w.audio[0]!.bitrate], [2, 8000, 256000]);

  // FLAC STREAMINFO: 44.1 kHz, stereo, 16 bit, 441000 samples
  const si = new Uint8Array(34);
  si.set([0x0a, 0xc4, 0x42, 0xf0, 0x00, 0x06, 0xba, 0xa8], 10); // rate(20) ch-1(3) bps-1(5) total(36)
  const flac = cat(ascii("fLaC"), Uint8Array.of(0x80, 0, 0, 34), si);
  const f = (await probe(reader(flac)))!;
  assert.equal(f.container, "FLAC");
  assert.deepEqual([f.audio[0]!.rate, f.audio[0]!.channels], [44100, 2]);
  assert.equal(f.duration, 10);

  // MP3: ID3v2.3 title + one MPEG-1 Layer III 128 kbit/s 44.1 kHz frame header
  const tit2 = cat(ascii("TIT2"), be32(6), be16(0), Uint8Array.of(0), ascii("Song"));
  const id3 = cat(ascii("ID3"), Uint8Array.of(3, 0, 0, 0, 0, 0, tit2.length), tit2);
  const mp3 = cat(id3, Uint8Array.of(0xff, 0xfb, 0x90, 0x00), zeros(1000));
  const m = (await probe(reader(mp3)))!;
  assert.equal(m.tags.Title, "Song");
  assert.deepEqual([m.audio[0]!.codec, m.audio[0]!.rate, m.audio[0]!.bitrate, m.audio[0]!.channels], ["MP3", 44100, 128000, 2]);
});

test("unknown data gives null; time formatting matches the desktop", async () => {
  assert.equal(await probe(reader(ascii("hello hello hello"))), null);
  assert.equal(fmtTime(0), "0:00");
  assert.equal(fmtTime(65.9), "1:05");
  assert.equal(fmtTime(3725), "1:02:05");
});
