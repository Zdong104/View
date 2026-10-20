// A tiny TrueType font builder: the glyphs a text box uses, copied as outlines
// from an installed font (TrueType or CFF, via fontkit) into a minimal glyf
// font that pdf-lib embeds whole. This sidesteps font subsetting (fontkit's
// CFF subsetter corrupts CID-keyed CJK fonts such as Noto Sans CJK / Source
// Han Sans) and works the same for every source format. Cubic outlines are
// approximated by quadratic splines (TrueType has no cubics).
import type { Font as FkFont } from "@pdf-lib/fontkit";

interface Pt { x: number; y: number; on: boolean }
interface OutGlyph { cps: number[]; advance: number; contours: Pt[][] }

type Cmd = { command: string; args: number[] };

/** fontkit path commands -> TrueType contours (font units, y up). */
export function contoursOf(cmds: Cmd[], upem: number): Pt[][] {
  const out: Pt[][] = [];
  let cur: Pt[] = [];
  let x = 0, y = 0, sx = 0, sy = 0;
  const close = () => {
    if (cur.length > 1) {
      const a = cur[0]!, b = cur[cur.length - 1]!;
      if (b.on && Math.round(a.x) === Math.round(b.x) && Math.round(a.y) === Math.round(b.y)) cur.pop();
    }
    if (cur.length > 1) out.push(cur);
    cur = [];
  };
  for (const { command, args: a } of cmds) {
    if (command === "moveTo") {
      close();
      x = sx = a[0]!; y = sy = a[1]!;
      cur.push({ x, y, on: true });
    } else if (command === "lineTo") {
      x = a[0]!; y = a[1]!;
      cur.push({ x, y, on: true });
    } else if (command === "quadraticCurveTo") {
      cur.push({ x: a[0]!, y: a[1]!, on: false });
      x = a[2]!; y = a[3]!;
      cur.push({ x, y, on: true });
    } else if (command === "bezierCurveTo") {
      const [c1x, c1y, c2x, c2y, ex, ey] = a as [number, number, number, number, number, number];
      const len = Math.hypot(c1x - x, c1y - y) + Math.hypot(c2x - c1x, c2y - c1y) + Math.hypot(ex - c2x, ey - c2y);
      const n = Math.min(8, Math.max(1, Math.ceil(len / (upem * 0.12))));
      const P = (t: number) => {
        const u = 1 - t;
        return [u * u * u * x + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t * t * t * ex,
          u * u * u * y + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t * t * t * ey] as const;
      };
      const D = (t: number) => { // derivative
        const u = 1 - t;
        return [3 * u * u * (c1x - x) + 6 * u * t * (c2x - c1x) + 3 * t * t * (ex - c2x),
          3 * u * u * (c1y - y) + 6 * u * t * (c2y - c1y) + 3 * t * t * (ey - c2y)] as const;
      };
      for (let i = 0; i < n; i++) {
        const t0 = i / n, t1 = (i + 1) / n, h = t1 - t0;
        const p0 = P(t0), p1 = P(t1), d0 = D(t0), d1 = D(t1);
        // quadratic control point: midpoint of the two tangent-based estimates
        const qx = ((p0[0] + d0[0] * h / 2) + (p1[0] - d1[0] * h / 2)) / 2;
        const qy = ((p0[1] + d0[1] * h / 2) + (p1[1] - d1[1] * h / 2)) / 2;
        cur.push({ x: qx, y: qy, on: false }, { x: p1[0], y: p1[1], on: true });
      }
      x = ex; y = ey;
    } else if (command === "closePath") {
      close();
      x = sx; y = sy;
    }
  }
  close();
  return out;
}

class W {
  private b: number[] = [];
  get length() { return this.b.length; }
  u8(v: number) { this.b.push(v & 255); return this; }
  u16(v: number) { return this.u8(v >> 8).u8(v); }
  i16(v: number) { return this.u16(Math.max(-32768, Math.min(32767, Math.round(v))) & 0xffff); }
  u32(v: number) { return this.u16((v >>> 16) & 0xffff).u16(v & 0xffff); }
  bytes(a: ArrayLike<number>) { for (let i = 0; i < a.length; i++) this.b.push(a[i]! & 255); return this; }
  pad4() { while (this.b.length % 4) this.b.push(0); return this; }
  out() { return Uint8Array.from(this.b); }
}

function checksum(t: Uint8Array): number {
  let s = 0;
  for (let i = 0; i < t.length; i += 4) s = (s + (((t[i] ?? 0) << 24) | ((t[i + 1] ?? 0) << 16) | ((t[i + 2] ?? 0) << 8) | (t[i + 3] ?? 0))) >>> 0;
  return s;
}

function tag6(s: string): string {
  let h = 2166136261;
  for (const ch of s) h = Math.imul(h ^ ch.codePointAt(0)!, 16777619) >>> 0;
  let t = "";
  for (let i = 0; i < 6; i++) { t += String.fromCharCode(65 + (h % 26)); h = Math.floor(h / 26) + i * 7919; }
  return t;
}

/**
 * Build a TrueType font with the glyphs for `text` taken from `src`.
 * Code points without a glyph are left out (they map to .notdef).
 */
export function buildSubsetTtf(src: FkFont, text: string, opts: { bold?: boolean; italic?: boolean } = {}): Uint8Array {
  const upem = src.unitsPerEm || 1000;
  const glyphs: OutGlyph[] = [{ cps: [], advance: Math.round(upem / 2), contours: [] }];
  const bySrc = new Map<number, OutGlyph>();
  for (const cp of new Set(Array.from(text, (c) => c.codePointAt(0)!))) {
    if (cp < 32 || !src.hasGlyphForCodePoint(cp)) continue;
    const g = src.glyphForCodePoint(cp);
    let og = bySrc.get(g.id);
    if (!og) {
      const cmds = (g.path as unknown as { commands: Cmd[] }).commands ?? [];
      og = { cps: [], advance: Math.round(g.advanceWidth), contours: contoursOf(cmds, upem) };
      bySrc.set(g.id, og);
      glyphs.push(og);
    }
    og.cps.push(cp);
  }
  const n = glyphs.length;

  // glyf + loca
  const glyf = new W();
  const loca: number[] = [];
  let gxMin = 0, gyMin = 0, gxMax = 0, gyMax = 0, maxPts = 0, maxCont = 0, advMax = 0, minLsb = 0, minRsb = 0, maxExt = 0;
  const lsbs: number[] = [];
  glyphs.forEach((g, i) => {
    loca.push(glyf.length);
    advMax = Math.max(advMax, g.advance);
    const pts = g.contours.flat().map((p) => ({ x: Math.round(p.x), y: Math.round(p.y), on: p.on }));
    if (!pts.length) { lsbs.push(0); return; }
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const [x0, y0, x1, y1] = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
    lsbs.push(x0);
    if (i === 1 || (gxMin === 0 && gxMax === 0)) { gxMin = x0; gyMin = y0; gxMax = x1; gyMax = y1; }
    gxMin = Math.min(gxMin, x0); gyMin = Math.min(gyMin, y0); gxMax = Math.max(gxMax, x1); gyMax = Math.max(gyMax, y1);
    minLsb = Math.min(minLsb, x0); minRsb = Math.min(minRsb, g.advance - x1); maxExt = Math.max(maxExt, x1);
    maxPts = Math.max(maxPts, pts.length); maxCont = Math.max(maxCont, g.contours.length);
    glyf.i16(g.contours.length).i16(x0).i16(y0).i16(x1).i16(y1);
    let end = -1;
    for (const c of g.contours) { end += c.length; glyf.u16(end); }
    glyf.u16(0); // no instructions
    for (const p of pts) glyf.u8(p.on ? 1 : 0);
    let px = 0;
    for (const p of pts) { glyf.i16(p.x - px); px = p.x; }
    let py = 0;
    for (const p of pts) { glyf.i16(p.y - py); py = p.y; }
    glyf.pad4();
  });
  loca.push(glyf.length);

  const bold = !!opts.bold, italic = !!opts.italic;
  const ascent = Math.round(src.ascent || upem * 0.88), descent = Math.round(src.descent || -upem * 0.12);
  const lineGap = Math.round(src.lineGap || 0);
  const cps = glyphs.flatMap((g, gid) => g.cps.map((cp) => [cp, gid] as const)).sort((a, b) => a[0] - b[0]);
  const srcPs = (src.postscriptName || "Font").replace(/[^A-Za-z0-9-]/g, "");
  const ps = `${tag6(srcPs + cps.map((c) => c[0]).join(","))}+${srcPs}`;
  const family = src.familyName || srcPs;

  const head = new W().u32(0x00010000).u32(0x00010000).u32(0).u32(0x5f0f3cf5).u16(0x000b).u16(upem)
    .u32(0).u32(0).u32(0).u32(0).i16(gxMin).i16(gyMin).i16(gxMax).i16(gyMax)
    .u16((bold ? 1 : 0) | (italic ? 2 : 0)).u16(8).i16(2).i16(1).i16(0);
  const hhea = new W().u32(0x00010000).i16(ascent).i16(descent).i16(lineGap).u16(advMax).i16(minLsb).i16(minRsb)
    .i16(maxExt).i16(1).i16(0).i16(0).i16(0).i16(0).i16(0).i16(0).i16(0).u16(n);
  const maxp = new W().u32(0x00010000).u16(n).u16(maxPts).u16(maxCont).u16(0).u16(0).u16(2)
    .u16(0).u16(0).u16(0).u16(0).u16(0).u16(0).u16(0).u16(0);
  const hmtx = new W();
  glyphs.forEach((g, i) => hmtx.u16(g.advance).i16(lsbs[i] ?? 0));
  const locaT = new W();
  for (const o of loca) locaT.u32(o);
  const cmap = new W().u16(0).u16(1).u16(3).u16(10).u32(12)
    .u16(12).u16(0).u32(16 + 12 * cps.length).u32(0).u32(cps.length);
  for (const [cp, gid] of cps) cmap.u32(cp).u32(cp).u32(gid);
  const avg = Math.round(glyphs.slice(1).reduce((s, g) => s + g.advance, 0) / Math.max(1, n - 1));
  const os2 = new W().u16(4).i16(avg).u16(bold ? 700 : 400).u16(5).u16(0)
    .i16(upem * 0.65).i16(upem * 0.6).i16(0).i16(upem * 0.075).i16(upem * 0.65).i16(upem * 0.6).i16(0).i16(upem * 0.35)
    .i16(upem * 0.05).i16(upem * 0.25).i16(0).bytes(new Array(10).fill(0)).u32(0).u32(0).u32(0).u32(0)
    .bytes([86, 73, 69, 87]).u16(bold || italic ? (bold ? 0x20 : 0) | (italic ? 1 : 0) : 0x40)
    .u16(Math.min(cps[0]?.[0] ?? 32, 0xffff)).u16(Math.min(cps.at(-1)?.[0] ?? 32, 0xffff))
    .i16(ascent).i16(descent).i16(lineGap).u16(Math.max(ascent, gyMax)).u16(Math.max(-descent, -gyMin)).u32(1).u32(0)
    .i16(src.xHeight || upem * 0.5).i16(src.capHeight || upem * 0.7).u16(0).u16(32).u16(0);
  const post = new W().u32(0x00030000).u32(Math.round((src.italicAngle || 0) * 65536))
    .i16(src.underlinePosition || -upem * 0.1).i16(src.underlineThickness || upem * 0.05).u32(0).u32(0).u32(0).u32(0).u32(0);
  const names: [number, string][] = [[1, family], [2, bold && italic ? "Bold Italic" : bold ? "Bold" : italic ? "Italic" : "Regular"],
    [3, ps], [4, `${family} (View subset)`], [6, ps]];
  const strs = names.map(([, s]) => { const w = new W(); for (const ch of s) { const c = ch.codePointAt(0)!; if (c > 0xffff) w.u16(0xfffd); else w.u16(c); } return w.out(); });
  const name = new W().u16(0).u16(names.length).u16(6 + 12 * names.length);
  let off = 0;
  names.forEach(([id], i) => { name.u16(3).u16(1).u16(0x409).u16(id).u16(strs[i]!.length).u16(off); off += strs[i]!.length; });
  for (const s of strs) name.bytes(s);

  const tables: [string, Uint8Array][] = ([
    ["OS/2", os2.out()], ["cmap", cmap.out()], ["glyf", glyf.out()], ["head", head.out()], ["hhea", hhea.out()],
    ["hmtx", hmtx.out()], ["loca", locaT.out()], ["maxp", maxp.out()], ["name", name.out()], ["post", post.out()],
  ] as [string, Uint8Array][]).sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const nt = tables.length;
  const es = Math.floor(Math.log2(nt));
  const font = new W().u32(0x00010000).u16(nt).u16(16 * 2 ** es).u16(es).u16(nt * 16 - 16 * 2 ** es);
  let pos = 12 + 16 * nt;
  for (const [t, d] of tables) {
    font.bytes([...t].map((c) => c.charCodeAt(0))).u32(checksum(d)).u32(pos).u32(d.length);
    pos += (d.length + 3) & ~3;
  }
  for (const [, d] of tables) font.bytes(d).pad4();
  const bytes = font.out();
  // head.checkSumAdjustment
  const headOff = new DataView(bytes.buffer).getUint32(12 + 16 * tables.findIndex(([t]) => t === "head") + 8);
  new DataView(bytes.buffer).setUint32(headOff + 8, (0xb1b0afba - checksum(bytes)) >>> 0);
  return bytes;
}
