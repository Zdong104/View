// Fonts for text in exported PDF annotations (text boxes). WinAnsi-only text
// uses the 14 standard fonts (nothing embedded, small files: the old
// behaviour). Anything else (CJK, Cyrillic, Greek, emoji…) needs an embedded
// font: a subset of an installed font, found through a FontProvider. The
// default provider uses the Local Font Access API (queryLocalFonts(), which
// asks for permission once); no CJK font is bundled with the extension.
// Tests inject their own provider.

/** What the exporter asks for: a font in this style that covers `text`. */
export interface FontRequest {
  family: string;
  bold: boolean;
  italic: boolean;
  text: string;
}

export interface FontSource {
  /** Font bytes: an sfnt (TTF/OTF) or a collection (TTC; `postscriptName` picks the face). */
  load(): Promise<Uint8Array | ArrayBuffer>;
  postscriptName?: string;
  /** Human-readable name, for messages. */
  name?: string;
}

/**
 * Candidate fonts for a request, best first. The exporter loads them in order
 * and embeds the first that covers every character.
 */
export type FontProvider = (req: FontRequest) => Promise<FontSource[]>;

// WinAnsiEncoding (cp1252) as code points: what the standard 14 fonts can show.
const WINANSI_EXTRA = [0x20ac, 0x201a, 0x0192, 0x201e, 0x2026, 0x2020, 0x2021, 0x02c6, 0x2030, 0x0160, 0x2039, 0x0152,
  0x017d, 0x2018, 0x2019, 0x201c, 0x201d, 0x2022, 0x2013, 0x2014, 0x02dc, 0x2122, 0x0161, 0x203a, 0x0153, 0x017e, 0x0178];
const WINANSI = new Set<number>([...WINANSI_EXTRA]);
for (let c = 0x20; c <= 0x7e; c++) WINANSI.add(c);
for (let c = 0xa0; c <= 0xff; c++) WINANSI.add(c);

/** Can the standard fonts show this text? (Line breaks and tabs are layout, not glyphs.) */
export function isWinAnsi(text: string): boolean {
  for (const ch of text) {
    const cp = ch.codePointAt(0)!;
    if (cp === 10 || cp === 13 || cp === 9) continue;
    if (!WINANSI.has(cp)) return false;
  }
  return true;
}

export const hasCjk = (text: string) => /[⺀-鿿가-힯豈-﫿＀-￯]|[\u{20000}-\u{3134f}]/u.test(text);

/** The face at `index` of a TrueType/OpenType collection, as a standalone sfnt. */
export function ttcFace(buf: Uint8Array, index: number): Uint8Array {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const n = dv.getUint32(8);
  if (index < 0 || index >= n) throw new RangeError(`font collection has ${n} faces`);
  const off = dv.getUint32(12 + 4 * index);
  const numTables = dv.getUint16(off + 4);
  const tables: { rec: number; offset: number; length: number }[] = [];
  for (let i = 0; i < numTables; i++) {
    const rec = off + 12 + 16 * i;
    tables.push({ rec, offset: dv.getUint32(rec + 8), length: dv.getUint32(rec + 12) });
  }
  const headerLen = 12 + 16 * numTables;
  let size = headerLen;
  for (const t of tables) size += (t.length + 3) & ~3;
  const out = new Uint8Array(size);
  const odv = new DataView(out.buffer);
  out.set(buf.subarray(off, off + 12), 0);
  let pos = headerLen;
  tables.forEach((t, i) => {
    const rec = 12 + 16 * i;
    out.set(buf.subarray(t.rec, t.rec + 16), rec);
    odv.setUint32(rec + 8, pos);
    out.set(buf.subarray(t.offset, t.offset + t.length), pos);
    pos += (t.length + 3) & ~3;
  });
  return out;
}

/** PostScript name (name ID 6) of each face of a collection. */
export function ttcNames(buf: Uint8Array): string[] {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const n = dv.getUint32(8);
  const out: string[] = [];
  for (let f = 0; f < n; f++) {
    const off = dv.getUint32(12 + 4 * f);
    let ps = "";
    for (let i = 0; i < dv.getUint16(off + 4); i++) {
      const rec = off + 12 + 16 * i;
      if (String.fromCharCode(buf[rec]!, buf[rec + 1]!, buf[rec + 2]!, buf[rec + 3]!) !== "name") continue;
      const t = dv.getUint32(rec + 8);
      const count = dv.getUint16(t + 2), strOff = t + dv.getUint16(t + 4);
      for (let r = 0; r < count; r++) {
        const nr = t + 6 + 12 * r;
        const [plat, , , id, len, so] = [0, 2, 4, 6, 8, 10].map((k) => dv.getUint16(nr + k)) as [number, number, number, number, number, number];
        if (id !== 6) continue;
        const bytes = buf.subarray(strOff + so, strOff + so + len);
        ps = plat === 3 || plat === 0
          ? String.fromCharCode(...Array.from({ length: len >> 1 }, (_, k) => (bytes[2 * k]! << 8) | bytes[2 * k + 1]!))
          : String.fromCharCode(...bytes);
        if (ps) break;
      }
    }
    out.push(ps);
  }
  return out;
}

export const isCollection = (b: Uint8Array) => b.length > 12 && b[0] === 0x74 && b[1] === 0x74 && b[2] === 0x63 && b[3] === 0x66; // "ttcf"

// ------------------------------------------------- Local Font Access API ---

interface LocalFontData {
  family: string;
  fullName: string;
  postscriptName: string;
  style: string;
  blob(): Promise<Blob>;
}

const SANS_CJK = ["Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "Source Han Sans CN", "Microsoft YaHei",
  "PingFang SC", "Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans CJK KR", "Source Han Sans", "Hiragino Sans GB",
  "WenQuanYi Micro Hei", "Droid Sans Fallback", "SimHei", "Malgun Gothic", "Yu Gothic", "Meiryo"];
const SERIF_CJK = ["Noto Serif CJK SC", "Noto Serif SC", "Source Han Serif SC", "Source Han Serif CN", "SimSun", "Songti SC",
  "Noto Serif CJK TC", "Noto Serif CJK JP", "Source Han Serif"];
const GENERIC_SANS = ["Noto Sans", "Arial Unicode MS", "Arial", "Segoe UI", "DejaVu Sans", "Liberation Sans", "Helvetica Neue", "Helvetica"];
const GENERIC_SERIF = ["Noto Serif", "Times New Roman", "DejaVu Serif", "Liberation Serif", "Georgia"];
const GENERIC_MONO = ["Noto Sans Mono", "DejaVu Sans Mono", "Consolas", "Menlo", "Liberation Mono", "Courier New"];

let fontList: Promise<LocalFontData[] | null> | null = null;

/** The installed fonts (asks for permission the first time); null if unavailable or denied. */
export function localFonts(): Promise<LocalFontData[] | null> {
  const q = (globalThis as { queryLocalFonts?: () => Promise<LocalFontData[]> }).queryLocalFonts;
  if (!q) return Promise.resolve(null);
  fontList ??= q().then((l) => (l.length ? l : null), () => null).then((l) => {
    if (!l) fontList = null; // denied or no activation: try again next time
    return l;
  });
  return fontList;
}

/**
 * Ask for font access early, while the user is typing non-WinAnsi text (the
 * permission prompt needs a user gesture, which a save may no longer have).
 */
export function prefetchFontsFor(text: string): void {
  if (!isWinAnsi(text)) void localFonts();
}

function styleScore(f: LocalFontData, bold: boolean, italic: boolean): number {
  const s = `${f.style} ${f.fullName}`.toLowerCase();
  const isBold = /bold|black|heavy|semibold|demibold/.test(s);
  const isItalic = /italic|oblique/.test(s);
  const odd = /light|thin|medium|extra|ultra|condensed|narrow|black|heavy/.test(s.replace(/bold/, "")) ? 1 : 0;
  return (isBold === bold ? 0 : 4) + (isItalic === italic ? 0 : 2) + odd;
}

/**
 * Candidate faces for a request, best first: the requested family, then CJK
 * families (sans or serif to match) when the text has CJK, then broad fallbacks.
 */
export function rankFonts(all: LocalFontData[], req: FontRequest): LocalFontData[] {
  const fam = req.family.toLowerCase();
  const serif = /serif|times|georgia|song|ming/i.test(req.family) && !/sans/i.test(req.family);
  const mono = /mono|courier|consol/i.test(req.family);
  const order = [
    ...(["sans-serif", "serif", "monospace", "system-ui"].includes(fam) ? [] : [req.family]),
    ...(hasCjk(req.text) ? (serif ? [...SERIF_CJK, ...SANS_CJK] : SANS_CJK) : []),
    ...(mono ? GENERIC_MONO : serif ? GENERIC_SERIF : GENERIC_SANS),
    ...(hasCjk(req.text) ? [] : SANS_CJK),
  ].map((n) => n.toLowerCase());
  const byFamily = new Map<string, LocalFontData[]>();
  for (const f of all) {
    const k = f.family.toLowerCase();
    let l = byFamily.get(k);
    if (!l) byFamily.set(k, (l = []));
    l.push(f);
  }
  const out: LocalFontData[] = [];
  for (const name of new Set(order)) {
    const faces = byFamily.get(name);
    if (!faces) continue;
    out.push([...faces].sort((a, b) => styleScore(a, req.bold, req.italic) - styleScore(b, req.bold, req.italic))[0]!);
  }
  return out;
}

/** Default provider: installed fonts via queryLocalFonts() (empty if unsupported or denied). */
export const localFontProvider: FontProvider = async (req) => {
  const all = await localFonts();
  if (!all) return [];
  return rankFonts(all, req).slice(0, 8).map((f) => ({
    name: f.fullName,
    postscriptName: f.postscriptName,
    load: async () => new Uint8Array(await (await f.blob()).arrayBuffer()),
  }));
};
