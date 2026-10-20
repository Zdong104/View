// Fixture generators for the model and media e2e tests (stream E). Nothing is
// committed in binary form: the tests build everything on the fly.

/** Axis-aligned box [0,w]x[0,h]x[0,d] shifted by `at`, as 12 outward-wound triangles. */
export function boxTris(w, h, d, at = [0, 0, 0]) {
  const p = (x, y, z) => [x * w + at[0], y * h + at[1], z * d + at[2]];
  const quads = [
    [p(0, 0, 0), p(0, 1, 0), p(1, 1, 0), p(1, 0, 0)], [p(0, 0, 1), p(1, 0, 1), p(1, 1, 1), p(0, 1, 1)],
    [p(0, 0, 0), p(1, 0, 0), p(1, 0, 1), p(0, 0, 1)], [p(0, 1, 0), p(0, 1, 1), p(1, 1, 1), p(1, 1, 0)],
    [p(0, 0, 0), p(0, 0, 1), p(0, 1, 1), p(0, 1, 0)], [p(1, 0, 0), p(1, 1, 0), p(1, 1, 1), p(1, 0, 1)],
  ];
  return quads.flatMap((q) => [[q[0], q[1], q[2]], [q[0], q[2], q[3]]]);
}

export function binaryStl(tris, header = "View test") {
  const buf = Buffer.alloc(84 + 50 * tris.length);
  buf.write(header, 0, "latin1");
  buf.writeUInt32LE(tris.length, 80);
  tris.forEach((t, i) => {
    let o = 84 + 50 * i + 12;
    for (const v of t) for (const c of v) { buf.writeFloatLE(c, o); o += 4; }
  });
  return buf;
}

export function asciiStl(tris, name = "box") {
  return Buffer.from(`solid ${name}\n${tris.map((t) =>
    `facet normal 0 0 0\nouter loop\n${t.map((v) => `vertex ${v.join(" ")}`).join("\n")}\nendloop\nendfacet`).join("\n")}\nendsolid ${name}\n`);
}

/** Unit-ish cube (w x h x d) as OBJ with quads, Y-up by convention. */
export function boxObj(w, h, d) {
  const v = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]];
  const f = [[1, 4, 3, 2], [5, 6, 7, 8], [1, 2, 6, 5], [2, 3, 7, 6], [3, 4, 8, 7], [1, 5, 8, 4]];
  return Buffer.from(`# box\n${v.map(([x, y, z]) => `v ${x * w} ${y * h} ${z * d}`).join("\n")}\n${f.map((q) => `f ${q.join(" ")}`).join("\n")}\n`);
}

export function boxPly(w, h, d, format = "ascii") {
  const v = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]].map(([x, y, z]) => [x * w, y * h, z * d]);
  const f = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]];
  const header = `ply\nformat ${format === "ascii" ? "ascii" : "binary_little_endian"} 1.0\nelement vertex 8\nproperty float x\nproperty float y\nproperty float z\nelement face 6\nproperty list uchar int vertex_indices\nend_header\n`;
  if (format === "ascii") return Buffer.from(header + v.map((p) => p.join(" ")).join("\n") + "\n" + f.map((q) => `4 ${q.join(" ")}`).join("\n") + "\n");
  const body = Buffer.alloc(8 * 12 + 6 * 17);
  let o = 0;
  for (const p of v) for (const c of p) { body.writeFloatLE(c, o); o += 4; }
  for (const q of f) { body.writeUInt8(4, o++); for (const i of q) { body.writeInt32LE(i, o); o += 4; } }
  return Buffer.concat([Buffer.from(header), body]);
}

/** Binary STL of an n x n height field (2 n^2 triangles), made fast with typed arrays. */
export function heightFieldStl(n, size = 200) {
  const tris = 2 * n * n;
  const buf = Buffer.alloc(84 + 50 * tris);
  buf.write("height field", 0, "latin1");
  buf.writeUInt32LE(tris, 80);
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const s = size / n;
  const z = (i, j) => 10 * Math.sin(i * 0.02) * Math.cos(j * 0.03) + 4 * Math.sin((i + j) * 0.05);
  let o = 84;
  const put = (x, y, zz) => { dv.setFloat32(o, x, true); dv.setFloat32(o + 4, y, true); dv.setFloat32(o + 8, zz, true); o += 12; };
  for (let j = 0; j < n; j++) {
    for (let i = 0; i < n; i++) {
      o += 12; // normal
      put(i * s, j * s, z(i, j)); put((i + 1) * s, j * s, z(i + 1, j)); put((i + 1) * s, (j + 1) * s, z(i + 1, j + 1)); o += 2;
      o += 12;
      put(i * s, j * s, z(i, j)); put((i + 1) * s, (j + 1) * s, z(i + 1, j + 1)); put(i * s, (j + 1) * s, z(i, j + 1)); o += 2;
    }
  }
  return buf;
}

/** 16-bit mono PCM WAV with a sine tone. */
export function wav(seconds = 3, rate = 8000, freq = 440) {
  const n = Math.floor(seconds * rate);
  const buf = Buffer.alloc(44 + n * 2);
  buf.write("RIFF", 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write("WAVE", 8);
  buf.write("fmt ", 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(rate, 24); buf.writeUInt32LE(rate * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write("data", 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.round(Math.sin((2 * Math.PI * freq * i) / rate) * 8000), 44 + i * 2);
  return buf;
}
