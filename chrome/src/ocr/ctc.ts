// CTC greedy decoding for the PP-OCR recognizer (RapidOCR CTCLabelDecode),
// plus where each character sits along the input width, for selection quads.

export interface DecodedLine {
  /** Characters (dictionary entries) in order. */
  chars: string[];
  text: string;
  /** Mean probability of the kept time steps (0 if none), rounded like RapidOCR. */
  score: number;
  /** Per character: [start, end) in time steps of the run that emitted it. */
  spans: [number, number][];
}

/**
 * The model's dictionary as RapidOCR builds it: the ONNX "character" metadata
 * lines, with "blank" (CTC blank) prepended and " " appended.
 */
export function buildCharset(metadataCharacter: string): string[] {
  const list = metadataCharacter.split(/\r?\n/);
  return ["blank", ...list, " "];
}

const round5 = (x: number) => Math.round(x * 1e5) / 1e5;

/**
 * preds: [N, T, C] probabilities. Greedy path: argmax per step, drop repeats
 * and blanks (index 0).
 */
export function ctcDecode(preds: Float32Array, n: number, t: number, c: number, charset: string[]): DecodedLine[] {
  const out: DecodedLine[] = [];
  for (let b = 0; b < n; b++) {
    const idx = new Int32Array(t), prob = new Float32Array(t);
    for (let s = 0; s < t; s++) {
      const base = (b * t + s) * c;
      let best = 0, bv = -Infinity;
      for (let k = 0; k < c; k++) {
        const v = preds[base + k]!;
        if (v > bv) { bv = v; best = k; }
      }
      idx[s] = best;
      prob[s] = bv;
    }
    const chars: string[] = [], spans: [number, number][] = [], confs: number[] = [];
    for (let s = 0; s < t; s++) {
      const k = idx[s]!;
      if (k === 0 || (s > 0 && idx[s - 1] === k)) continue;
      let e = s + 1;
      while (e < t && idx[e] === k) e++;
      chars.push(charset[k] ?? "");
      spans.push([s, e]);
      confs.push(round5(prob[s]!));
    }
    const mean = confs.length ? confs.reduce((a, v) => a + v, 0) / confs.length : 0;
    out.push({ chars, text: chars.join(""), score: round5(mean), spans });
  }
  return out;
}

/**
 * Character boundaries as fractions of the content width (0..1): centres of
 * the emitting runs, boundaries halfway between neighbours, the outer edges
 * half a pitch beyond the first and last centres.
 * stepPx: input pixels per time step; contentPx: width of the resized crop
 * inside the (zero-padded) input.
 */
export function charBounds(spans: [number, number][], stepPx: number, contentPx: number): [number, number][] {
  const n = spans.length;
  if (n === 0) return [];
  if (n === 1) return [[0, 1]];
  const centres = spans.map(([s, e]) => ((s + e) / 2) * stepPx);
  const pitch = (centres[n - 1]! - centres[0]!) / (n - 1);
  const edges = [centres[0]! - pitch / 2];
  for (let i = 0; i < n - 1; i++) edges.push((centres[i]! + centres[i + 1]!) / 2);
  edges.push(centres[n - 1]! + pitch / 2);
  const f = (px: number) => Math.min(1, Math.max(0, px / contentPx));
  return spans.map((_, i) => [f(edges[i]!), f(edges[i + 1]!)]);
}
