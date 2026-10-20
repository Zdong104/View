// Number formatting shared by the subtitle, the statistics overlay and Get Info.
// Ports of _fmt_len / _fmt_coord / _fmt_num in preview/stl_view.py.
const group = (v: number, digits: number): string =>
  v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

/** 3 significant digits below 1, one decimal up to 1000, grouped integer above. */
export function fmtLen(v: number): string {
  if (v >= 1000) return group(v, 0);
  if (v >= 1) return v.toFixed(1);
  return String(Number(v.toPrecision(3)));
}

export function fmtCoord(v: number): string {
  v = v + 0;
  if (Math.abs(v) < 1e-6) v = 0;
  return Math.abs(v) < 1e5 ? group(v, 2) : group(v, 0);
}

export function fmtNum(v: number, unit: string): string {
  return v >= 100 ? `${group(v, 0)} ${unit}` : `${group(v, 2)} ${unit}`;
}

export const fmtInt = (n: number): string => n.toLocaleString("en-US");

export function srgbToLinear(c: readonly [number, number, number]): [number, number, number] {
  const f = (x: number) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);
  return [f(c[0]), f(c[1]), f(c[2])];
}
