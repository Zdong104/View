// Mesh parsing (STL ASCII/binary, OBJ, PLY ASCII/binary) and geometry statistics.
// Port of preview/mesh_io.py. Plain ESM with no imports on purpose: it is shipped
// as a package file (see copy.json) and loaded by mesh-worker.js, because the
// build has no worker bundling step; the unit tests import it directly.
//
// parseMesh()   -> triangle soup (Float32Array, 9 floats per triangle), recentred on the bbox centre
// analyzeMesh() -> welded topology + area, volume, centre of mass, watertightness
// computeSmoothNormals() -> crease-aware per-corner normals (Int8, 4 bytes per corner)

export class MeshError extends Error {}

// --------------------------------------------------------------------------
// byte helpers
// --------------------------------------------------------------------------
const isSpace = (c) => c === 32 || c === 9 || c === 10 || c === 13 || c === 11 || c === 12;

/** Cleaned latin-1 text of a byte range: stops at NUL, control chars become spaces, runs collapse. */
function cleanText(u8, start, end) {
  let out = "";
  for (let i = start; i < end && u8[i] !== 0; i++) {
    const c = u8[i];
    out += c >= 32 && c < 127 || c >= 160 ? String.fromCharCode(c) : " ";
  }
  return out.split(/\s+/).filter(Boolean).join(" ");
}

const latin1 = (u8, s, e) => {
  let out = "";
  for (let i = s; i < e; i++) out += String.fromCharCode(u8[i]);
  return out;
};

/** Parse the number in u8[s, e). Fast path for plain decimals/exponents, Number() otherwise. */
function parseNum(u8, s, e) {
  let i = s;
  let neg = false;
  if (i < e && (u8[i] === 45 || u8[i] === 43)) { neg = u8[i] === 45; i++; }
  let mant = 0;
  let digits = 0;
  let exp = 0;
  let ok = i < e;
  for (; i < e; i++) {
    const c = u8[i] - 48;
    if (c < 0 || c > 9) break;
    if (digits < 17) { mant = mant * 10 + c; if (mant) digits++; } else exp++;
  }
  if (i < e && u8[i] === 46) {
    i++;
    for (; i < e; i++) {
      const c = u8[i] - 48;
      if (c < 0 || c > 9) break;
      if (digits < 17) { mant = mant * 10 + c; if (mant) digits++; exp--; }
    }
  }
  if (i < e && (u8[i] === 101 || u8[i] === 69)) {
    i++;
    let eneg = false;
    if (i < e && (u8[i] === 45 || u8[i] === 43)) { eneg = u8[i] === 45; i++; }
    let ev = 0;
    let any = false;
    for (; i < e; i++) {
      const c = u8[i] - 48;
      if (c < 0 || c > 9) break;
      ev = ev * 10 + c;
      any = true;
    }
    if (!any) ok = false;
    exp += eneg ? -ev : ev;
  }
  if (!ok || i !== e) return Number(latin1(u8, s, e)); // "nan", "inf", odd forms
  const v = exp === 0 ? mant : exp < 0 ? mant / 10 ** -exp : mant * 10 ** exp;
  return neg ? -v : v;
}

/** Growable typed array. */
class Grow {
  constructor(Type, cap = 1 << 16) {
    this.Type = Type;
    this.a = new Type(cap);
    this.n = 0;
  }
  push(v) {
    if (this.n === this.a.length) this.reserve(this.n + 1);
    this.a[this.n++] = v;
  }
  reserve(min) {
    let cap = this.a.length * 2;
    while (cap < min) cap *= 2;
    const b = new this.Type(cap);
    b.set(this.a);
    this.a = b;
  }
  done() { return this.a.length === this.n ? this.a : this.a.slice(0, this.n); }
}

// --------------------------------------------------------------------------
// public entry point
// --------------------------------------------------------------------------
/**
 * @param {ArrayBuffer | Uint8Array} data
 * @param {string} ext  file extension without dot ("stl", "obj", "ply")
 */
export function parseMesh(data, ext) {
  const u8 = data instanceof Uint8Array ? data : new Uint8Array(data);
  ext = String(ext).toLowerCase().replace(/^\./, "");
  let fmt, up = "z", header = "", corners, indexed = null;
  if (ext === "obj") {
    const r = parseObj(u8);
    fmt = "OBJ"; up = "y";
    ({ corners, indexed } = fromIndexed(r.verts, r.faces));
  } else if (ext === "ply") {
    const r = parsePly(u8);
    fmt = "PLY"; up = "y";
    ({ corners, indexed } = fromIndexed(r.verts, r.faces));
  } else {
    const r = parseStl(u8);
    fmt = r.fmt; header = r.header; corners = r.corners;
  }
  return finish(corners, indexed, { fmt, up, header, fileSize: u8.length });
}

/** Expand (verts, faces) to a corner soup, dropping triangles with non-finite coordinates. */
function fromIndexed(verts, faces) {
  const nt = faces.length / 3;
  const corners = new Float32Array(nt * 9);
  let k = 0;
  const keep = new Uint8Array(nt);
  for (let t = 0; t < nt; t++) {
    const a = faces[3 * t] * 3, b = faces[3 * t + 1] * 3, c = faces[3 * t + 2] * 3;
    const x0 = verts[a], y0 = verts[a + 1], z0 = verts[a + 2];
    const x1 = verts[b], y1 = verts[b + 1], z1 = verts[b + 2];
    const x2 = verts[c], y2 = verts[c + 1], z2 = verts[c + 2];
    if (!(Number.isFinite(x0 + y0 + z0 + x1 + y1 + z1 + x2 + y2 + z2))) continue;
    keep[t] = 1;
    corners[k++] = x0; corners[k++] = y0; corners[k++] = z0;
    corners[k++] = x1; corners[k++] = y1; corners[k++] = z1;
    corners[k++] = x2; corners[k++] = y2; corners[k++] = z2;
  }
  let outFaces = faces;
  if (k !== nt * 9) {
    outFaces = new Uint32Array((k / 9) * 3);
    let j = 0;
    for (let t = 0; t < nt; t++) if (keep[t]) { outFaces[j++] = faces[3 * t]; outFaces[j++] = faces[3 * t + 1]; outFaces[j++] = faces[3 * t + 2]; }
  }
  return { corners: k === corners.length ? corners : corners.slice(0, k), indexed: { verts, faces: outFaces } };
}

/** Drop non-finite triangles from a soup; compute the bbox; recentre everything on its centre. */
function finish(corners, indexed, meta) {
  let n = corners.length;
  // soups (STL): filter in place
  if (!indexed) {
    let k = 0;
    for (let i = 0; i < n; i += 9) {
      let ok = true;
      for (let j = 0; j < 9; j++) if (!Number.isFinite(corners[i + j])) { ok = false; break; }
      if (!ok) continue;
      if (k !== i) corners.copyWithin(k, i, i + 9);
      k += 9;
    }
    if (k !== n) corners = corners.slice(0, k);
    n = corners.length;
  }
  if (n === 0) throw new MeshError("The file contains no usable triangles.");
  let lx = Infinity, ly = Infinity, lz = Infinity, hx = -Infinity, hy = -Infinity, hz = -Infinity;
  for (let i = 0; i < n; i += 3) {
    const x = corners[i], y = corners[i + 1], z = corners[i + 2];
    if (x < lx) lx = x; if (x > hx) hx = x;
    if (y < ly) ly = y; if (y > hy) hy = y;
    if (z < lz) lz = z; if (z > hz) hz = z;
  }
  const bboxMin = [lx, ly, lz];
  const bboxMax = [hx, hy, hz];
  const origin = [(lx + hx) / 2, (ly + hy) / 2, (lz + hz) / 2];
  const o32 = origin.map(Math.fround);
  for (let i = 0; i < n; i += 3) {
    corners[i] -= o32[0]; corners[i + 1] -= o32[1]; corners[i + 2] -= o32[2];
  }
  if (indexed) {
    const v = indexed.verts;
    for (let i = 0; i < v.length; i += 3) { v[i] -= o32[0]; v[i + 1] -= o32[1]; v[i + 2] -= o32[2]; }
  }
  return {
    positions: corners,
    indexed,
    triangles: n / 9,
    bboxMin, bboxMax,
    size: [hx - lx, hy - ly, hz - lz],
    origin: o32.map(Number),
    ...meta,
  };
}

// --------------------------------------------------------------------------
// STL
// --------------------------------------------------------------------------
function stlIsBinary(u8, dv) {
  const size = u8.length;
  if (size >= 84) {
    const n = dv.getUint32(80, true);
    if (84 + 50 * n === size) return true; // exact size match: binary even if it starts with "solid"
  }
  const lim = Math.min(1024, size);
  let i = 0;
  while (i < lim && isSpace(u8[i])) i++;
  const lower = (k) => u8[k] | 32;
  const solid = i + 5 <= lim && lower(i) === 115 && lower(i + 1) === 111 && lower(i + 2) === 108 && lower(i + 3) === 105 && lower(i + 4) === 100;
  if (!solid) return true;
  if (size >= 84) for (let k = 0; k < lim; k++) if (u8[k] === 0) return true;
  const head = latin1(u8, i, lim);
  if (head.includes("facet") || head.includes("endsolid")) return false;
  return size >= 84 && (size - 84) % 50 === 0;
}

function parseStl(u8) {
  if (u8.length === 0) throw new MeshError("The file is empty.");
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  if (stlIsBinary(u8, dv)) {
    if (u8.length < 84) throw new MeshError("File is too small to be a binary STL.");
    let n = dv.getUint32(80, true);
    const avail = Math.floor((u8.length - 84) / 50);
    if (n === 0 && (u8.length - 84) % 50 === 0) n = avail; // count left as 0 by some exporters
    if (n > avail) {
      throw new MeshError(`Binary STL is truncated: header promises ${n.toLocaleString("en-US")} triangles but only ${avail.toLocaleString("en-US")} are present.`);
    }
    if (n === 0) throw new MeshError("The STL file contains no triangles.");
    const out = new Float32Array(n * 9);
    let off = 84 + 12;
    for (let i = 0; i < n * 9; i += 9, off += 50) {
      out[i] = dv.getFloat32(off, true); out[i + 1] = dv.getFloat32(off + 4, true); out[i + 2] = dv.getFloat32(off + 8, true);
      out[i + 3] = dv.getFloat32(off + 12, true); out[i + 4] = dv.getFloat32(off + 16, true); out[i + 5] = dv.getFloat32(off + 20, true);
      out[i + 6] = dv.getFloat32(off + 24, true); out[i + 7] = dv.getFloat32(off + 28, true); out[i + 8] = dv.getFloat32(off + 32, true);
    }
    return { corners: out, fmt: "Binary STL", header: cleanText(u8, 0, 80) };
  }
  // ASCII: collect the three numbers after every "vertex" keyword
  let i = 0;
  while (i < u8.length && isSpace(u8[i])) i++;
  let lineEnd = u8.indexOf(10, i);
  if (lineEnd < 0) lineEnd = u8.length;
  const first = latin1(u8, i, Math.min(lineEnd, i + 256));
  const name = /^solid/i.test(first) ? first.slice(5) : "";
  const out = new Grow(Float32Array, Math.max(1 << 16, Math.floor(u8.length / 8)));
  const n = u8.length;
  let pos = 0;
  const token = () => { // -> [start, end) or null; keeps `tokEnd`
    while (pos < n && isSpace(u8[pos])) pos++;
    if (pos >= n) return -1;
    const s = pos;
    while (pos < n && !isSpace(u8[pos])) pos++;
    return s;
  };
  for (;;) {
    const s = token();
    if (s < 0) break;
    if (pos - s !== 6) continue;
    if ((u8[s] | 32) !== 118 || (u8[s + 1] | 32) !== 101 || (u8[s + 2] | 32) !== 114 || (u8[s + 3] | 32) !== 116 || (u8[s + 4] | 32) !== 101 || (u8[s + 5] | 32) !== 120) continue;
    for (let k = 0; k < 3; k++) {
      const t = token();
      if (t < 0) throw new MeshError("Malformed ASCII STL: file ends inside a vertex.");
      const v = parseNum(u8, t, pos);
      if (Number.isNaN(v) && latin1(u8, t, pos).toLowerCase() !== "nan") throw new MeshError(`Malformed ASCII STL: bad number (${latin1(u8, t, pos)}).`);
      out.push(v);
    }
  }
  if (out.n === 0) throw new MeshError("This doesn't look like a valid STL file (no vertices found).");
  if (out.n % 9) throw new MeshError("Malformed ASCII STL: vertex count is not a multiple of 3.");
  return { corners: out.done(), fmt: "ASCII STL", header: cleanText(new TextEncoder().encode(name), 0, name.length) };
}

// --------------------------------------------------------------------------
// OBJ
// --------------------------------------------------------------------------
function parseObj(u8) {
  const n = u8.length;
  const verts = new Grow(Float32Array, 3 << 14);
  const faces = new Grow(Uint32Array, 3 << 14);
  let nv = 0;
  let pos = 0;
  const poly = [];
  while (pos < n) {
    let e = u8.indexOf(10, pos);
    if (e < 0) e = n;
    // line = [pos, e); join backslash continuations
    let s = pos;
    let next = e + 1;
    while (s < n && (u8[s] === 32 || u8[s] === 9)) s++;
    const c0 = u8[s], c1 = u8[s + 1];
    if ((c0 === 118 || c0 === 102) && (c1 === 32 || c1 === 9) && s + 1 < e) { // "v " | "f "
      let end = e;
      // continuation lines: a trailing "\" (before an optional CR) joins the next line
      const cont = () => { let k = end - 1; while (k > s && (u8[k] === 13 || u8[k] === 32 || u8[k] === 9)) k--; return u8[k] === 92; };
      const ranges = [];
      ranges.push([s + 2, end]);
      while (cont() && next < n) {
        let e2 = u8.indexOf(10, next);
        if (e2 < 0) e2 = n;
        ranges.push([next, e2]);
        end = e2;
        next = e2 + 1;
      }
      const toks = tokenizeLine(u8, ranges);
      if (c0 === 118) { // vertex: x y z [w | r g b]
        if (toks.length < 3) throw new MeshError("Malformed OBJ: vertex with fewer than 3 coordinates.");
        for (let k = 0; k < 3; k++) {
          const v = parseNum(u8, toks[k][0], toks[k][1]);
          if (Number.isNaN(v) && latin1(u8, toks[k][0], toks[k][1]).toLowerCase() !== "nan") throw new MeshError(`Malformed OBJ vertex line (${latin1(u8, toks[k][0], toks[k][1])}).`);
          verts.push(v);
        }
        nv++;
      } else if (toks.length >= 3) { // face: keep the position index of "v/vt/vn"
        poly.length = 0;
        for (const [ts, te] of toks) {
          let q = ts;
          while (q < te && u8[q] !== 47) q++;
          const idx = parseNum(u8, ts, q);
          if (!Number.isInteger(idx) || idx === 0) throw new MeshError("Malformed OBJ face indices.");
          poly.push(idx < 0 ? nv + idx : idx - 1);
        }
        for (let k = 1; k + 1 < poly.length; k++) { faces.push(poly[0]); faces.push(poly[k]); faces.push(poly[k + 1]); }
      }
    }
    pos = next;
  }
  if (nv === 0) throw new MeshError("This doesn't look like a valid OBJ file (no vertices).");
  if (faces.n === 0) throw new MeshError("The OBJ file has no faces (point clouds aren't supported).");
  const f = faces.done();
  for (let i = 0; i < f.length; i++) if (f[i] >= nv) throw new MeshError("The OBJ file references vertices that don't exist.");
  return { verts: verts.done(), faces: f };
}

/** Whitespace-separated tokens of one logical line, stopping at "#" and ignoring "\". */
function tokenizeLine(u8, ranges) {
  const toks = [];
  for (const [a, b] of ranges) {
    let i = a;
    while (i < b) {
      while (i < b && (isSpace(u8[i]) || u8[i] === 92)) i++;
      if (i >= b || u8[i] === 35) { if (i < b) return toks; break; }
      const s = i;
      while (i < b && !isSpace(u8[i]) && u8[i] !== 35 && u8[i] !== 92) i++;
      toks.push([s, i]);
      if (i < b && u8[i] === 35) return toks;
    }
  }
  return toks;
}

// --------------------------------------------------------------------------
// PLY
// --------------------------------------------------------------------------
const PLY_TYPES = {
  char: "i1", int8: "i1", uchar: "u1", uint8: "u1",
  short: "i2", int16: "i2", ushort: "u2", uint16: "u2",
  int: "i4", int32: "i4", uint: "u4", uint32: "u4",
  float: "f4", float32: "f4", double: "f8", float64: "f8",
};
const PLY_SIZE = { i1: 1, u1: 1, i2: 2, u2: 2, i4: 4, u4: 4, f4: 4, f8: 8 };

function readScalar(dv, off, t, le) {
  switch (t) {
    case "i1": return dv.getInt8(off);
    case "u1": return dv.getUint8(off);
    case "i2": return dv.getInt16(off, le);
    case "u2": return dv.getUint16(off, le);
    case "i4": return dv.getInt32(off, le);
    case "u4": return dv.getUint32(off, le);
    case "f4": return dv.getFloat32(off, le);
    default: return dv.getFloat64(off, le);
  }
}

function parsePly(u8) {
  if (!(u8[0] === 112 && u8[1] === 108 && u8[2] === 121)) throw new MeshError("This doesn't look like a PLY file (missing 'ply' magic).");
  const key = [101, 110, 100, 95, 104, 101, 97, 100, 101, 114]; // "end_header"
  let end = -1;
  const limit = Math.min(u8.length - key.length, 1 << 20);
  outer: for (let i = 0; i <= limit; i++) {
    for (let k = 0; k < key.length; k++) if (u8[i + k] !== key[k]) continue outer;
    end = i;
    break;
  }
  if (end < 0) throw new MeshError("Malformed PLY: no end_header.");
  let nl = u8.indexOf(10, end);
  const bodyStart = nl >= 0 ? nl + 1 : u8.length;
  const lines = latin1(u8, 0, end).replaceAll("\r", "").split("\n");
  let fmt = null;
  const elements = [];
  for (const line of lines) {
    const tok = line.trim().split(/\s+/);
    if (!tok[0]) continue;
    if (tok[0] === "format") fmt = tok[1];
    else if (tok[0] === "element") elements.push({ name: tok[1], count: parseInt(tok[2], 10), props: [] });
    else if (tok[0] === "property" && elements.length) {
      const el = elements[elements.length - 1];
      if (tok[1] === "list") {
        const ct = PLY_TYPES[tok[2]], it = PLY_TYPES[tok[3]];
        if (!ct || !it || !tok[4]) throw new MeshError(`Malformed PLY property line: ${line}`);
        el.props.push({ name: tok[4], list: true, ct, it });
      } else {
        const t = PLY_TYPES[tok[1]];
        if (!t || !tok[2]) throw new MeshError(`Malformed PLY property line: ${line}`);
        el.props.push({ name: tok[2], list: false, t });
      }
    }
  }
  if (!["ascii", "binary_little_endian", "binary_big_endian"].includes(fmt)) throw new MeshError(`Unsupported PLY format: ${fmt}`);
  if (!elements.some((e) => e.name === "vertex")) throw new MeshError("The PLY file has no vertex element.");
  if (!elements.some((e) => e.name === "face")) throw new MeshError("The PLY file has no faces (point clouds aren't supported).");

  let verts = null;
  const faces = new Grow(Uint32Array, 3 << 14);
  const reader = fmt === "ascii" ? asciiReader(u8, bodyStart) : binaryReader(u8, bodyStart, fmt === "binary_little_endian");
  for (const el of elements) {
    const isVertex = el.name === "vertex";
    const isFace = el.name === "face";
    if (isVertex) {
      const names = el.props.map((p) => p.name);
      const ix = ["x", "y", "z"].map((a) => names.indexOf(a));
      if (ix.some((i) => i < 0 || el.props[i].list)) throw new MeshError("PLY vertex element lacks x/y/z.");
      verts = new Float32Array(el.count * 3);
      for (let r = 0; r < el.count; r++) {
        for (let p = 0; p < el.props.length; p++) {
          const prop = el.props[p];
          const v = prop.list ? reader.skipList(prop) : reader.scalar(prop.t);
          const slot = ix.indexOf(p);
          if (slot >= 0) verts[r * 3 + slot] = v;
        }
      }
    } else if (isFace) {
      const lists = el.props.filter((p) => p.list);
      if (!lists.length) throw new MeshError("PLY face element has no index list.");
      const want = lists.find((p) => p.name === "vertex_indices" || p.name === "vertex_index") ?? lists[0];
      const poly = [];
      for (let r = 0; r < el.count; r++) {
        for (const prop of el.props) {
          if (!prop.list) { reader.scalar(prop.t); continue; }
          if (prop !== want) { reader.skipList(prop); continue; }
          const k = reader.scalar(prop.ct);
          poly.length = 0;
          for (let q = 0; q < k; q++) poly.push(reader.scalar(prop.it));
          for (let q = 1; q + 1 < k; q++) { faces.push(poly[0]); faces.push(poly[q]); faces.push(poly[q + 1]); }
        }
      }
    } else {
      for (let r = 0; r < el.count; r++) for (const prop of el.props) { if (prop.list) reader.skipList(prop); else reader.scalar(prop.t); }
    }
  }
  if (!verts || faces.n === 0) throw new MeshError(verts ? "The PLY file contains no faces." : "The PLY file is missing vertices or faces.");
  const f = faces.done();
  const nv = verts.length / 3;
  for (let i = 0; i < f.length; i++) if (f[i] >= nv) throw new MeshError("The PLY file references vertices that don't exist.");
  return { verts, faces: f };
}

function binaryReader(u8, start, le) {
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  let off = start;
  const need = (k) => { if (off + k > u8.length) throw new MeshError("Malformed PLY: file ends early."); };
  const scalar = (t) => { const s = PLY_SIZE[t]; need(s); const v = readScalar(dv, off, t, le); off += s; return v; };
  return {
    scalar,
    skipList(p) {
      const k = scalar(p.ct);
      const b = k * PLY_SIZE[p.it];
      need(b);
      off += b;
      return 0;
    },
  };
}

function asciiReader(u8, start) {
  const n = u8.length;
  let pos = start;
  const next = () => {
    while (pos < n && isSpace(u8[pos])) pos++;
    if (pos >= n) throw new MeshError("Malformed PLY: file ends early.");
    const s = pos;
    while (pos < n && !isSpace(u8[pos])) pos++;
    const v = parseNum(u8, s, pos);
    if (Number.isNaN(v)) throw new MeshError(`Malformed PLY data (${latin1(u8, s, pos)}).`);
    return v;
  };
  return {
    scalar: () => next(),
    skipList(p) {
      const k = next();
      for (let i = 0; i < k; i++) next();
      return 0;
    },
  };
}

// --------------------------------------------------------------------------
// welding + statistics
// --------------------------------------------------------------------------
const NEG_ZERO = 0x80000000;

/**
 * Merge bit-identical float32 positions. `pts` is a Float32Array of xyz triples.
 * Returns { ids: Int32Array (per point), count }.
 */
export function weldPoints(pts) {
  const n = pts.length / 3;
  const bits = new Uint32Array(pts.buffer, pts.byteOffset, pts.length);
  let size = 16;
  while (size < n * 2) size *= 2;
  const mask = size - 1;
  const table = new Int32Array(size).fill(-1);
  const ids = new Int32Array(n);
  let count = 0;
  for (let i = 0; i < n; i++) {
    let x = bits[3 * i], y = bits[3 * i + 1], z = bits[3 * i + 2];
    if (x === NEG_ZERO) x = 0; // -0 == +0
    if (y === NEG_ZERO) y = 0;
    if (z === NEG_ZERO) z = 0;
    let h = Math.imul(x, 0x9e3779b1) ^ Math.imul(y, 0x85ebca6b) ^ Math.imul(z, 0xc2b2ae35);
    h ^= h >>> 15;
    h = Math.imul(h, 0x2c1b3c6d);
    h ^= h >>> 12;
    h &= mask;
    for (;;) {
      const t = table[h];
      if (t < 0) { table[h] = i; ids[i] = count++; break; }
      let tx = bits[3 * t], ty = bits[3 * t + 1], tz = bits[3 * t + 2];
      if (tx === NEG_ZERO) tx = 0;
      if (ty === NEG_ZERO) ty = 0;
      if (tz === NEG_ZERO) tz = 0;
      if (tx === x && ty === y && tz === z) { ids[i] = ids[t]; break; }
      h = (h + 1) & mask;
    }
  }
  return { ids, count };
}

/** Watertightness: every undirected edge used by exactly two triangles (degenerate triangles ignored). */
export function edgeStats(faces, nv) {
  const nt = faces.length / 3;
  let good = 0;
  for (let t = 0; t < nt; t++) {
    const a = faces[3 * t], b = faces[3 * t + 1], c = faces[3 * t + 2];
    if (a !== b && b !== c && c !== a) good++;
  }
  const keys = new Float64Array(good * 3);
  let k = 0;
  for (let t = 0; t < nt; t++) {
    const a = faces[3 * t], b = faces[3 * t + 1], c = faces[3 * t + 2];
    if (a === b || b === c || c === a) continue;
    keys[k++] = Math.min(a, b) * nv + Math.max(a, b);
    keys[k++] = Math.min(b, c) * nv + Math.max(b, c);
    keys[k++] = Math.min(c, a) * nv + Math.max(c, a);
  }
  keys.sort();
  let boundary = 0, nonmanifold = 0;
  for (let i = 0; i < keys.length;) {
    let j = i + 1;
    while (j < keys.length && keys[j] === keys[i]) j++;
    const cnt = j - i;
    if (cnt === 1) boundary++;
    else if (cnt > 2) nonmanifold++;
    i = j;
  }
  return { closed: boundary === 0 && nonmanifold === 0 && keys.length > 0, boundaryEdges: boundary, nonmanifoldEdges: nonmanifold };
}

/**
 * Welded topology + full statistics. `mesh` is the result of parseMesh() with
 * `positions` possibly restored after a transfer round trip (pass it explicitly).
 * Returns { stats, faces (Int32Array, welded vertex id per corner), vertexCount }.
 */
export function analyzeMesh(mesh, positions = mesh.positions) {
  const nt = positions.length / 9;
  let faces, nv;
  if (mesh.indexed) { // indexed input: weld only the vertex table
    const { ids, count } = weldPoints(mesh.indexed.verts);
    const f = mesh.indexed.faces;
    faces = new Int32Array(f.length);
    for (let i = 0; i < f.length; i++) faces[i] = ids[f[i]];
    nv = count;
  } else {
    const w = weldPoints(positions);
    faces = w.ids;
    nv = w.count;
  }
  // signed tetrahedron sum about the bbox centre (better precision)
  const ctr = [0, 1, 2].map((a) => (mesh.bboxMin[a] + mesh.bboxMax[a]) / 2 - mesh.origin[a]);
  let area = 0, vol6 = 0, cx = 0, cy = 0, cz = 0;
  for (let t = 0; t < nt; t++) {
    const o = t * 9;
    const ax = positions[o], ay = positions[o + 1], az = positions[o + 2];
    const e1x = positions[o + 3] - ax, e1y = positions[o + 4] - ay, e1z = positions[o + 5] - az;
    const e2x = positions[o + 6] - ax, e2y = positions[o + 7] - ay, e2z = positions[o + 8] - az;
    const nx = e1y * e2z - e1z * e2y, ny = e1z * e2x - e1x * e2z, nz = e1x * e2y - e1y * e2x;
    area += Math.sqrt(nx * nx + ny * ny + nz * nz);
    const tet = (ax - ctr[0]) * nx + (ay - ctr[1]) * ny + (az - ctr[2]) * nz;
    vol6 += tet;
    cx += tet * (ax + positions[o + 3] + positions[o + 6] - 3 * ctr[0]);
    cy += tet * (ay + positions[o + 4] + positions[o + 7] - 3 * ctr[1]);
    cz += tet * (az + positions[o + 5] + positions[o + 8] - 3 * ctr[2]);
  }
  const volume = vol6 / 6;
  const com = Math.abs(volume) > 0
    ? [mesh.origin[0] + ctr[0] + cx / 6 / 4 / volume, mesh.origin[1] + ctr[1] + cy / 6 / 4 / volume, mesh.origin[2] + ctr[2] + cz / 6 / 4 / volume]
    : [mesh.origin[0] + ctr[0], mesh.origin[1] + ctr[1], mesh.origin[2] + ctr[2]];
  const edges = edgeStats(faces, nv);
  const stats = {
    vertices: nv,
    triangles: nt,
    area: 0.5 * area,
    volume: Math.abs(volume),
    volumeSigned: volume,
    centerOfMass: com,
    ...edges,
  };
  return { stats, faces, vertexCount: nv };
}

// --------------------------------------------------------------------------
// smooth normals
// --------------------------------------------------------------------------
/**
 * Per-corner normals (Int8Array, 4 bytes per corner, xyz in [-127, 127]).
 * Smooth across soft edges, sharp across creases wider than `creaseDeg`
 * (like corner_normals_packed in mesh_io.py).
 */
export function computeSmoothNormals(positions, faces, nv, creaseDeg = 40) {
  const nt = faces.length / 3;
  const fn = new Float32Array(nt * 3);  // area-weighted face normals
  const fnu = new Float32Array(nt * 3); // unit face normals (0 for degenerate faces)
  const vn = new Float64Array(nv * 3);
  for (let t = 0; t < nt; t++) {
    const o = t * 9;
    const ax = positions[o], ay = positions[o + 1], az = positions[o + 2];
    const e1x = positions[o + 3] - ax, e1y = positions[o + 4] - ay, e1z = positions[o + 5] - az;
    const e2x = positions[o + 6] - ax, e2y = positions[o + 7] - ay, e2z = positions[o + 8] - az;
    const nx = e1y * e2z - e1z * e2y, ny = e1z * e2x - e1x * e2z, nz = e1x * e2y - e1y * e2x;
    fn[3 * t] = nx; fn[3 * t + 1] = ny; fn[3 * t + 2] = nz;
    const l = Math.sqrt(nx * nx + ny * ny + nz * nz);
    if (l > 0) { fnu[3 * t] = nx / l; fnu[3 * t + 1] = ny / l; fnu[3 * t + 2] = nz / l; }
    for (let k = 0; k < 3; k++) {
      const v = faces[3 * t + k] * 3;
      vn[v] += nx; vn[v + 1] += ny; vn[v + 2] += nz;
    }
  }
  const vnu = new Float32Array(nv * 3);
  for (let v = 0; v < nv; v++) {
    const x = vn[3 * v], y = vn[3 * v + 1], z = vn[3 * v + 2];
    const l = Math.sqrt(x * x + y * y + z * z);
    if (l > 0) { vnu[3 * v] = x / l; vnu[3 * v + 1] = y / l; vnu[3 * v + 2] = z / l; }
  }
  // a vertex is "soft" if every incident face is within crease/2 of the average
  const cosHalf = Math.cos((creaseDeg / 2) * Math.PI / 180);
  const cosT = Math.cos(creaseDeg * Math.PI / 180);
  const vmin = new Float32Array(nv).fill(2);
  const deg = new Int32Array(nv + 1);
  for (let c = 0; c < nt * 3; c++) {
    const t = (c / 3) | 0, v = faces[c];
    deg[v + 1]++;
    let d = 1;
    if (fnu[3 * t] || fnu[3 * t + 1] || fnu[3 * t + 2]) d = vnu[3 * v] * fnu[3 * t] + vnu[3 * v + 1] * fnu[3 * t + 1] + vnu[3 * v + 2] * fnu[3 * t + 2];
    if (d < vmin[v]) vmin[v] = d;
  }
  for (let v = 0; v < nv; v++) deg[v + 1] += deg[v];
  // CSR of incident corners, only needed around sharp vertices
  const fill = deg.slice(0, nv);
  const inc = new Int32Array(nt * 3);
  for (let c = 0; c < nt * 3; c++) inc[fill[faces[c]]++] = c;

  const out = new Int8Array(nt * 3 * 4);
  const put = (c, x, y, z) => {
    out[4 * c] = Math.round(x * 127); out[4 * c + 1] = Math.round(y * 127); out[4 * c + 2] = Math.round(z * 127);
  };
  for (let c = 0; c < nt * 3; c++) {
    const t = (c / 3) | 0, v = faces[c];
    if (vmin[v] >= cosHalf) { put(c, vnu[3 * v], vnu[3 * v + 1], vnu[3 * v + 2]); continue; }
    const fx = fnu[3 * t], fy = fnu[3 * t + 1], fz = fnu[3 * t + 2];
    const d = deg[v + 1] - deg[v];
    if (d <= 64) { // exact smoothing group: faces around the vertex within the crease angle
      let ax = 0, ay = 0, az = 0;
      for (let i = deg[v]; i < deg[v + 1]; i++) {
        const j = (inc[i] / 3) | 0;
        if (fx * fnu[3 * j] + fy * fnu[3 * j + 1] + fz * fnu[3 * j + 2] > cosT) { ax += fn[3 * j]; ay += fn[3 * j + 1]; az += fn[3 * j + 2]; }
      }
      const l = Math.sqrt(ax * ax + ay * ay + az * az);
      if (l > 0) put(c, ax / l, ay / l, az / l); else put(c, fx, fy, fz);
    } else {
      const dot = vnu[3 * v] * fx + vnu[3 * v + 1] * fy + vnu[3 * v + 2] * fz;
      if (dot > cosT) put(c, vnu[3 * v], vnu[3 * v + 1], vnu[3 * v + 2]); else put(c, fx, fy, fz);
    }
  }
  return out;
}
