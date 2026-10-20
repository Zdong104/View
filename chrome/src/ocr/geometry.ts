// Small computational-geometry kit for the OCR post-processing (instead of
// OpenCV): convex hull, minimum-area rectangle, quad ordering, homographies.
// Points are [x, y] pairs; quads are 4 points TL, TR, BR, BL.

export type Pt = [number, number];
export type Quad = [Pt, Pt, Pt, Pt];

/** Andrew's monotone chain; returns the hull counter-clockwise (y down: clockwise on screen). */
export function convexHull(points: Pt[]): Pt[] {
  if (points.length < 3) return points.slice();
  const p = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o: Pt, a: Pt, b: Pt) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower: Pt[] = [];
  for (const q of p) {
    while (lower.length >= 2 && cross(lower[lower.length - 2]!, lower[lower.length - 1]!, q) <= 0) lower.pop();
    lower.push(q);
  }
  const upper: Pt[] = [];
  for (let i = p.length - 1; i >= 0; i--) {
    const q = p[i]!;
    while (upper.length >= 2 && cross(upper[upper.length - 2]!, upper[upper.length - 1]!, q) <= 0) upper.pop();
    upper.push(q);
  }
  lower.pop();
  upper.pop();
  return lower.concat(upper);
}

export interface MinRect {
  /** The 4 corners in no particular order (like cv2.boxPoints). */
  corners: Pt[];
  /** Length of the shorter side (`min(cv2.minAreaRect()[1])`). */
  short: number;
}

/** cv2.minAreaRect + cv2.boxPoints over a point set (rotating calipers on the hull). */
export function minAreaRect(points: Pt[]): MinRect {
  const hull = convexHull(points);
  if (hull.length === 0) return { corners: [[0, 0], [0, 0], [0, 0], [0, 0]], short: 0 };
  if (hull.length < 3) {
    const a = hull[0]!, b = hull[hull.length - 1]!;
    return { corners: [a, b, b, a], short: 0 };
  }
  let best = Infinity;
  let out: MinRect = { corners: [], short: 0 };
  const n = hull.length;
  for (let i = 0; i < n; i++) {
    const a = hull[i]!, b = hull[(i + 1) % n]!;
    let ux = b[0] - a[0], uy = b[1] - a[1];
    const len = Math.hypot(ux, uy);
    if (len === 0) continue;
    ux /= len;
    uy /= len;
    // Project every hull point on the edge direction u and its normal v.
    let minU = Infinity, maxU = -Infinity, minV = Infinity, maxV = -Infinity;
    for (const q of hull) {
      const du = (q[0] - a[0]) * ux + (q[1] - a[1]) * uy;
      const dv = -(q[0] - a[0]) * uy + (q[1] - a[1]) * ux;
      if (du < minU) minU = du;
      if (du > maxU) maxU = du;
      if (dv < minV) minV = dv;
      if (dv > maxV) maxV = dv;
    }
    const w = maxU - minU, h = maxV - minV;
    const area = w * h;
    if (area < best - 1e-9) {
      best = area;
      const at = (u: number, v: number): Pt => [a[0] + u * ux - v * uy, a[1] + u * uy + v * ux];
      out = { corners: [at(minU, minV), at(maxU, minV), at(maxU, maxV), at(minU, maxV)], short: Math.min(w, h) };
    }
  }
  return out;
}

/** DBPostProcess.get_mini_boxes: minAreaRect corners ordered TL, TR, BR, BL. */
export function miniBox(points: Pt[]): { box: Quad; short: number } {
  const { corners, short } = minAreaRect(points);
  const p = corners.slice().sort((a, b) => a[0] - b[0]); // stable, like Python's sorted
  let i1 = 0, i2 = 1, i3 = 2, i4 = 3;
  if (p[1]![1] > p[0]![1]) { i1 = 0; i4 = 1; } else { i1 = 1; i4 = 0; }
  if (p[3]![1] > p[2]![1]) { i2 = 2; i3 = 3; } else { i2 = 3; i3 = 2; }
  return { box: [p[i1]!, p[i2]!, p[i3]!, p[i4]!], short };
}

/** DBPostProcess.order_points_clockwise (imutils): TL, TR, BR, BL. */
export function orderClockwise(pts: Pt[]): Quad {
  const xs = pts.slice().sort((a, b) => a[0] - b[0]);
  const left = xs.slice(0, 2).sort((a, b) => a[1] - b[1]);
  const right = xs.slice(2, 4).sort((a, b) => a[1] - b[1]);
  return [left[0]!, right[0]!, right[1]!, left[1]!];
}

/** preview/ocr.py _order_corners: clockwise on screen, starting nearest the top-left. */
export function orderCorners(q: Pt[]): Pt[] {
  if (q.length !== 4) return q;
  const cx = (q[0]![0] + q[1]![0] + q[2]![0] + q[3]![0]) / 4;
  const cy = (q[0]![1] + q[1]![1] + q[2]![1] + q[3]![1]) / 4;
  const s = q.slice().sort((a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx));
  let i = 0;
  for (let k = 1; k < 4; k++) if (s[k]![0] + s[k]![1] < s[i]![0] + s[i]![1]) i = k;
  return s.slice(i).concat(s.slice(0, i));
}

export function polygonArea(p: Pt[]): number {
  let a = 0;
  for (let i = 0; i < p.length; i++) {
    const q = p[i]!, r = p[(i + 1) % p.length]!;
    a += q[0] * r[1] - r[0] * q[1];
  }
  return Math.abs(a) / 2;
}

export function polygonPerimeter(p: Pt[]): number {
  let l = 0;
  for (let i = 0; i < p.length; i++) {
    const q = p[i]!, r = p[(i + 1) % p.length]!;
    l += Math.hypot(r[0] - q[0], r[1] - q[1]);
  }
  return l;
}

/**
 * DBPostProcess.unclip: grow the polygon by area * ratio / perimeter with round
 * joins (pyclipper JT_ROUND). The offset of a convex polygon is its Minkowski
 * sum with a disk, so we return the vertices of that sum (discs sampled at 64
 * steps): only its min-area rectangle is used afterwards. pyclipper works on
 * integers, so the input is truncated first, as pyclipper does.
 */
export function unclip(box: Pt[], ratio: number): Pt[] {
  const area = polygonArea(box);
  const length = polygonPerimeter(box);
  const d = length > 0 ? (area * ratio) / length : 0;
  const ints = box.map((p): Pt => [Math.trunc(p[0]), Math.trunc(p[1])]);
  const out: Pt[] = [];
  const steps = 64;
  for (const p of ints) {
    for (let k = 0; k < steps; k++) {
      const a = (k / steps) * 2 * Math.PI;
      out.push([p[0] + d * Math.cos(a), p[1] + d * Math.sin(a)]);
    }
  }
  return out;
}

/** numpy.round: half to even. */
export function roundHalfEven(x: number): number {
  const r = Math.round(x);
  return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
}

/** 3x3 homography (row-major, h[8] = 1) mapping src[i] -> dst[i] (cv2.getPerspectiveTransform). */
export function perspectiveTransform(src: Pt[], dst: Pt[]): number[] {
  const A: number[][] = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = src[i]!;
    const [u, v] = dst[i]!;
    A.push([x, y, 1, 0, 0, 0, -x * u, -y * u, u]);
    A.push([0, 0, 0, x, y, 1, -x * v, -y * v, v]);
  }
  // Gaussian elimination with partial pivoting on the 8x9 augmented matrix.
  for (let c = 0; c < 8; c++) {
    let piv = c;
    for (let r = c + 1; r < 8; r++) if (Math.abs(A[r]![c]!) > Math.abs(A[piv]![c]!)) piv = r;
    [A[c], A[piv]] = [A[piv]!, A[c]!];
    const d = A[c]![c]!;
    if (Math.abs(d) < 1e-12) return [1, 0, 0, 0, 1, 0, 0, 0, 1];
    for (let r = 0; r < 8; r++) {
      if (r === c) continue;
      const f = A[r]![c]! / d;
      if (f === 0) continue;
      for (let k = c; k < 9; k++) A[r]![k]! -= f * A[c]![k]!;
    }
  }
  const h = A.map((row, i) => row[8]! / row[i]!);
  h.push(1);
  return h;
}

export function applyH(h: number[], x: number, y: number): Pt {
  const w = h[6]! * x + h[7]! * y + h[8]!;
  return [(h[0]! * x + h[1]! * y + h[2]!) / w, (h[3]! * x + h[4]! * y + h[5]!) / w];
}

export const dist = (a: Pt, b: Pt) => Math.hypot(a[0] - b[0], a[1] - b[1]);
