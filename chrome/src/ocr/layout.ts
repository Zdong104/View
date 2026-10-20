// Reading order and plain-text joining of recognized lines (port of
// preview/ocr.py _reading_order / lines_to_text): rows by vertical overlap
// after de-skewing, left to right within a row, a blank line between
// paragraphs, no space between two CJK characters.
import type { OcrLine } from "./api.ts";

type P = [number, number];

export function isWide(ch: string): boolean {
  const o = ch.codePointAt(0) ?? 0;
  return (0x2e80 <= o && o <= 0xa4cf) || (0xac00 <= o && o <= 0xd7a3) || (0xf900 <= o && o <= 0xfaff)
    || (0xfe30 <= o && o <= 0xfe6f) || (0xff00 <= o && o <= 0xff60) || (0xffe0 <= o && o <= 0xffe6)
    || (0x20000 <= o && o <= 0x3ffff);
}

export const corners = (l: OcrLine): P[] => [0, 2, 4, 6].map((i): P => [l.box[i]!, l.box[i + 1]!]);

/** [index, separator before it] in reading order; separators are "", " ", "\n" or "\n\n". */
export function readingOrder(lines: readonly OcrLine[]): [number, string][] {
  const n = lines.length;
  if (!n) return [];
  const angs: number[] = [];
  for (const l of lines) {
    const b = corners(l);
    const dx = b[1]![0] - b[0]![0], dy = b[1]![1] - b[0]![1];
    if (Math.hypot(dx, dy) > 1.5 * Math.hypot(b[3]![0] - b[0]![0], b[3]![1] - b[0]![1])) {
      const a = Math.atan2(dy, dx);
      if (Math.abs(a) < Math.PI / 4) angs.push(a);
    }
  }
  let rot: [number, number] | null = null;
  if (angs.length) {
    angs.sort((a, b) => a - b);
    const med = angs[Math.floor(angs.length / 2)]!;
    if (Math.abs(med) > (1 * Math.PI) / 180) rot = [Math.cos(-med), Math.sin(-med)];
  }
  const g = lines.map((l) => {
    let b = corners(l);
    if (rot) {
      const [c, s] = rot;
      b = b.map(([x, y]): P => [x * c - y * s, x * s + y * c]);
    }
    const xs = b.map((p) => p[0]), ys = b.map((p) => p[1]);
    const along = Math.hypot(b[1]![0] - b[0]![0], b[1]![1] - b[0]![1]);
    const across = Math.hypot(b[3]![0] - b[0]![0], b[3]![1] - b[0]![1]);
    const h = Math.max(1, along > 0 && across > 0 ? Math.min(along, across) : Math.max(along, across));
    return { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys), h };
  });
  const order = [...g.keys()].sort((i, j) => (g[i]!.y0 + g[i]!.y1) / 2 - (g[j]!.y0 + g[j]!.y1) / 2 || g[i]!.x0 - g[j]!.x0);
  const rows: number[][] = [];
  const bounds: [number, number][] = [];
  for (const i of order) {
    const { y0, y1 } = g[i]!;
    const last = bounds[bounds.length - 1];
    if (last) {
      const [top, bot] = last;
      if (Math.min(bot, y1) - Math.max(top, y0) > 0.5 * Math.min(bot - top, y1 - y0)) {
        rows[rows.length - 1]!.push(i);
        bounds[bounds.length - 1] = [Math.min(top, y0), Math.max(bot, y1)];
        continue;
      }
    }
    rows.push([i]);
    bounds.push([y0, y1]);
  }
  const out: [number, string][] = [];
  let prevBot = 0;
  rows.forEach((row, r) => {
    row.sort((i, j) => g[i]!.x0 - g[j]!.x0);
    const [top, bot] = bounds[r]!;
    const rh = row.reduce((s, i) => s + g[i]!.h, 0) / row.length;
    row.forEach((i, k) => {
      const sep = r === 0 && k === 0 ? "" : k > 0 ? " " : top - prevBot > 0.7 * rh ? "\n\n" : "\n";
      out.push([i, sep]);
    });
    prevBot = bot;
  });
  return out;
}

/** The last code point of s (a surrogate pair stays whole). */
function lastChar(s: string): string {
  const c = s.charCodeAt(s.length - 1);
  return c >= 0xdc00 && c <= 0xdfff && s.length > 1 ? s.slice(-2) : s.slice(-1);
}

export function joinText(a: string, b: string, sep: string): string {
  if (sep === " " && a && b && isWide(lastChar(a)) && isWide(b)) sep = "";
  return a + sep + b;
}

/** Plain text of all lines in reading order (preview/ocr.py lines_to_text). */
export function linesToText(lines: readonly OcrLine[]): string {
  let out = "";
  for (const [i, sep] of readingOrder(lines)) out = out ? joinText(out, lines[i]!.text, sep) : lines[i]!.text;
  return out;
}
