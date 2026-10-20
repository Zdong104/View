// Text of the pages, for Find (Ctrl+F) and for the markup layer's TextRuns.
// Built from pdf.js getTextContent() (or from Live Text OCR lines on scanned
// pages), with one quad per character in surface units (geometry.ts), so
// search hits and text markup snap to real glyph boxes.
import type { PDFPageProxy } from "pdfjs-dist";
import type { TextItem, TextStyle } from "pdfjs-dist/types/src/display/api";
import type { TextRun } from "../../markup/api.ts";
import { button, el } from "../../ui/kit.ts";
import { bbox } from "./geometry.ts";
import type { PageInfo } from "./sources.ts";

export interface PageText {
  /** Normalized (lower-case, single-spaced) text of the page. */
  norm: string;
  /** Per normalized char: index into `quads` (char), or -1 for synthetic spaces. */
  map: Int32Array;
  /** Surface-unit quads (8 numbers each) of the raw chars. */
  quads: Float32Array;
  runs: TextRun[];
  /** True when the page has real text (not just OCR / nothing). */
  hasText: boolean;
}

/** An OCR line in surface units. */
export interface SurfaceLine { text: string; box: number[]; charQuads?: number[] }

const SPACE = /\s/;

/** Build a PageText from runs (text + one quad per char). */
export function pageTextFromRuns(runs: TextRun[], hasText: boolean, eol: boolean[] = []): PageText {
  const norm: string[] = [];
  const map: number[] = [];
  const quads: number[] = [];
  let lastSpace = true;
  let q = 0;
  runs.forEach((run, ri) => {
    const chars = [...run.text];
    chars.forEach((ch, ci) => {
      const qi = q++;
      quads.push(...run.quads.slice(ci * 8, ci * 8 + 8));
      if (SPACE.test(ch)) {
        if (!lastSpace) { norm.push(" "); map.push(qi); lastSpace = true; }
        return;
      }
      for (const lc of ch.toLowerCase()) { norm.push(lc); map.push(qi); }
      lastSpace = false;
    });
    if ((eol[ri] ?? true) && !lastSpace) { norm.push(" "); map.push(-1); lastSpace = true; }
  });
  return { norm: norm.join(""), map: Int32Array.from(map), quads: Float32Array.from(quads), runs, hasText };
}

/** Text runs from pdf.js text content, quads in surface units. */
export function runsFromTextContent(items: TextItem[], styles: Record<string, TextStyle>, info: PageInfo): { runs: TextRun[]; eol: boolean[] } {
  const runs: TextRun[] = [];
  const eol: boolean[] = [];
  const toS = (x: number, y: number): [number, number] => [x - info.x0, info.y0 + info.h - y];
  for (const it of items) {
    if (!it.str) { if (it.hasEOL && eol.length) eol[eol.length - 1] = true; continue; }
    const [a, b, c, d, e, f] = it.transform as number[];
    const len = Math.hypot(a!, b!) || 1;
    const dx = a! / len, dy = b! / len;             // baseline direction (user space)
    const fh = Math.hypot(c!, d!) || it.height || 1;
    const st = styles[it.fontName];
    const asc = st?.ascent ? st.ascent : 0.8;
    const desc = st?.descent ? st.descent : -0.2;
    const ux = -dy, uy = dx;                        // "up" (user space)
    const chars = [...it.str];
    const adv = it.width / Math.max(1, chars.length);
    const quads: number[] = [];
    for (let i = 0; i < chars.length; i++) {
      const s0 = i * adv, s1 = (i + 1) * adv;
      const bx0 = e! + dx * s0, by0 = f! + dy * s0, bx1 = e! + dx * s1, by1 = f! + dy * s1;
      const top = asc * fh, bot = desc * fh;
      quads.push(
        ...toS(bx0 + ux * top, by0 + uy * top), ...toS(bx1 + ux * top, by1 + uy * top),
        ...toS(bx1 + ux * bot, by1 + uy * bot), ...toS(bx0 + ux * bot, by0 + uy * bot));
    }
    runs.push({ text: it.str, quads });
    eol.push(!!it.hasEOL);
  }
  return { runs, eol };
}

/** Runs from OCR lines (surface units); per-char quads approximated when missing. */
export function runsFromOcr(lines: SurfaceLine[]): TextRun[] {
  return lines.filter((l) => l.text.trim()).map((l) => {
    const chars = [...l.text];
    if (l.charQuads && l.charQuads.length >= chars.length * 8) return { text: l.text, quads: l.charQuads.slice(0, chars.length * 8) };
    const [x1, y1, x2, y2, x3, y3, x4, y4] = l.box as [number, number, number, number, number, number, number, number];
    const quads: number[] = [];
    const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
    for (let i = 0; i < chars.length; i++) {
      const t0 = i / chars.length, t1 = (i + 1) / chars.length;
      quads.push(lerp(x1, x2, t0), lerp(y1, y2, t0), lerp(x1, x2, t1), lerp(y1, y2, t1),
        lerp(x4, x3, t1), lerp(y4, y3, t1), lerp(x4, x3, t0), lerp(y4, y3, t0));
    }
    return { text: l.text, quads };
  });
}

export async function extractPageText(page: PDFPageProxy, info: PageInfo): Promise<PageText> {
  const tc = await page.getTextContent();
  const items = tc.items.filter((i): i is TextItem => "str" in i);
  const { runs, eol } = runsFromTextContent(items, tc.styles, info);
  return pageTextFromRuns(runs, runs.some((r) => r.text.trim()), eol);
}

export function normalizeQuery(q: string): string {
  return q.trim().replace(/\s+/g, " ").toLowerCase();
}

/** Matches of a normalized query: one array of surface quads (union bbox per line) per hit. */
export function findInPage(t: PageText, query: string): number[][] {
  const out: number[][] = [];
  if (!query) return out;
  let i = t.norm.indexOf(query);
  while (i >= 0) {
    const quads: number[] = [];
    for (let k = i; k < i + query.length; k++) {
      const qi = t.map[k]!;
      if (qi >= 0) quads.push(...t.quads.subarray(qi * 8, qi * 8 + 8));
    }
    if (quads.length) out.push(mergeQuads(quads));
    i = t.norm.indexOf(query, i + Math.max(1, query.length));
  }
  return out;
}

/** Merge per-char quads into per-line boxes (x, y, w, h list, surface units). */
function mergeQuads(quads: number[]): number[] {
  const boxes: [number, number, number, number][] = [];
  for (let i = 0; i < quads.length; i += 8) {
    const b = bbox(quads.slice(i, i + 8));
    const last = boxes[boxes.length - 1];
    if (last && Math.abs(last[1] - b[1]) < Math.max(2, b[3] * 0.4) && b[0] >= last[0] - 2 && b[0] <= last[0] + last[2] + b[3]) {
      const x1 = Math.max(last[0] + last[2], b[0] + b[2]), y0 = Math.min(last[1], b[1]), y1 = Math.max(last[1] + last[3], b[1] + b[3]);
      last[2] = x1 - last[0]; last[1] = y0; last[3] = y1 - y0;
    } else boxes.push(b);
  }
  return boxes.flat();
}

// ---- find bar --------------------------------------------------------------------------

export interface FindBarHandlers {
  search(q: string): void;
  step(dir: 1 | -1): void;
  close(): void;
}

export class FindBar {
  readonly el: HTMLDivElement;
  readonly input: HTMLInputElement;
  private label: HTMLSpanElement;

  constructor(h: FindBarHandlers, signal: AbortSignal) {
    this.input = el("input.pdf-find-input", { type: "search", placeholder: "Search document", "aria-label": "Search document", spellcheck: "false" });
    this.label = el("span.pdf-find-label", { "aria-live": "polite" });
    const prev = button({ icon: "chevronUp", title: "Previous Match (Shift+Enter)", onClick: () => h.step(-1) });
    const next = button({ icon: "chevronDown", title: "Next Match (Enter)", onClick: () => h.step(1) });
    this.el = el("div.pdf-findbar", { hidden: true, role: "search" },
      this.input, el("div.linked", {}, prev, next), this.label,
      button({ icon: "close", title: "Close (Escape)", onClick: () => h.close() }));
    let t = 0;
    const o = { signal };
    this.input.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => h.search(this.input.value), 120) as unknown as number;
    }, o);
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); h.step(e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); h.close(); }
    }, o);
  }

  get open(): boolean { return !this.el.hidden; }

  show(): void {
    this.el.hidden = false;
    this.input.focus();
    this.input.select();
  }

  hide(): void { this.el.hidden = true; }

  setLabel(s: string): void { this.label.textContent = s; }
}
