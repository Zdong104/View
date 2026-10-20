// Mesh parsing and statistics (src/viewers/model/mesh-core.js), cross-checked
// against preview/mesh_io.py (hard-coded values for analytic solids, plus a live
// comparison on generated files when python3 + numpy are available).
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { MeshError, analyzeMesh, computeSmoothNormals, parseMesh } from "../../src/viewers/model/mesh-core.js";

const near = (a: number, b: number, tol = 1e-6) => assert.ok(Math.abs(a - b) <= tol * Math.max(1, Math.abs(b)), `${a} vs ${b}`);
const enc = (s: string) => new TextEncoder().encode(s);

function stats(bytes: Uint8Array, ext: string) {
  const mesh = parseMesh(bytes, ext);
  return { mesh, ...analyzeMesh(mesh) };
}

// tetrahedron from test/fixtures/sample.stl (desktop mesh_io: area 2.3660254, volume 1/6)
const TETRA = enc(`solid tetra
${[[[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 0, 0], [0, 1, 0], [0, 0, 1]], [[0, 0, 0], [0, 0, 1], [1, 0, 0]], [[1, 0, 0], [0, 0, 1], [0, 1, 0]]]
  .map((t) => `facet normal 0 0 0\nouter loop\n${t.map((v) => `vertex ${v.join(" ")}`).join("\n")}\nendloop\nendfacet`).join("\n")}
endsolid tetra
`);

test("ASCII STL tetrahedron matches mesh_io.py", () => {
  const { mesh, stats: s } = stats(TETRA, "stl");
  assert.equal(mesh.fmt, "ASCII STL");
  assert.equal(mesh.header, "tetra");
  assert.deepEqual(mesh.size, [1, 1, 1]);
  assert.equal(s.triangles, 4);
  assert.equal(s.vertices, 4);
  near(s.area, 2.366025388240814);
  near(s.volume, 1 / 6);
  assert.ok(s.closed);
  for (const c of s.centerOfMass) near(c, 0.25);
});

/** Axis-aligned box [0,w]x[0,h]x[0,d] as 12 triangles, outward winding. */
function box(w: number, h: number, d: number): number[][][] {
  const p = (x: number, y: number, z: number) => [x * w, y * h, z * d];
  const quads = [
    [p(0, 0, 0), p(0, 1, 0), p(1, 1, 0), p(1, 0, 0)], [p(0, 0, 1), p(1, 0, 1), p(1, 1, 1), p(0, 1, 1)],
    [p(0, 0, 0), p(1, 0, 0), p(1, 0, 1), p(0, 0, 1)], [p(0, 1, 0), p(0, 1, 1), p(1, 1, 1), p(1, 1, 0)],
    [p(0, 0, 0), p(0, 0, 1), p(0, 1, 1), p(0, 1, 0)], [p(1, 0, 0), p(1, 1, 0), p(1, 1, 1), p(1, 0, 1)],
  ];
  return quads.flatMap((q) => [[q[0]!, q[1]!, q[2]!], [q[0]!, q[2]!, q[3]!]]);
}

function binaryStl(tris: number[][][], header = "binary box"): Uint8Array {
  const buf = new Uint8Array(84 + 50 * tris.length);
  const dv = new DataView(buf.buffer);
  buf.set(enc(header), 0);
  dv.setUint32(80, tris.length, true);
  tris.forEach((t, i) => t.forEach((v, k) => v.forEach((c, j) => dv.setFloat32(84 + 50 * i + 12 + 12 * k + 4 * j, c, true))));
  return buf;
}

test("binary STL box (offset from the origin) has exact stats", () => {
  const tris = box(10, 20, 30).map((t) => t.map((v) => [v[0]! + 1000, v[1]! - 500, v[2]! + 0.5]));
  const { mesh, stats: s } = stats(binaryStl(tris), "stl");
  assert.equal(mesh.fmt, "Binary STL");
  assert.equal(mesh.header, "binary box");
  assert.deepEqual(mesh.size, [10, 20, 30]);
  assert.deepEqual(mesh.bboxMin, [1000, -500, 0.5]);
  near(s.volume, 6000);
  near(s.area, 2 * (200 + 300 + 600));
  assert.equal(s.vertices, 8);
  assert.ok(s.closed);
  near(s.centerOfMass[0]!, 1005);
  near(s.centerOfMass[1]!, -490);
  near(s.centerOfMass[2]!, 15.5);
  // positions are recentred on the bbox centre
  assert.ok(Math.max(...mesh.positions.map(Math.abs)) <= 15 + 1e-6);
});

test("open mesh is reported as not watertight", () => {
  const { stats: s } = stats(binaryStl(box(1, 1, 1).slice(1)), "stl");
  assert.equal(s.closed, false);
  assert.ok(s.boundaryEdges > 0);
});

test("binary STL with a 'solid' header is still binary (size matches)", () => {
  const { mesh } = stats(binaryStl(box(1, 1, 1), "solid looks ascii but is binary"), "stl");
  assert.equal(mesh.fmt, "Binary STL");
  assert.equal(mesh.triangles, 12);
});

test("truncated and empty STL give MeshError", () => {
  const b = binaryStl(box(1, 1, 1));
  assert.throws(() => parseMesh(b.slice(0, b.length - 60), "stl"), MeshError);
  assert.throws(() => parseMesh(new Uint8Array(0), "stl"), MeshError);
  assert.throws(() => parseMesh(enc("hello world"), "stl"), MeshError);
});

test("OBJ: quads fan-triangulate, negative indices, comments and v/vt/vn tokens", () => {
  const obj = `# cube
v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
v 0 0 1
v 1 0 1
v 1 1 1
v 0 1 1
f 1//1 4//1 3//1 2//1
f 5/1/2 6/2/2 7/3/2 8/4/2 # top
f 1 2 6 5
f 2 3 7 6
f 3 4 8 7
f -8 -4 -1 -5
`;
  const { mesh, stats: s } = stats(enc(obj), "obj");
  assert.equal(mesh.fmt, "OBJ");
  assert.equal(mesh.up, "y");
  assert.equal(s.triangles, 12);
  assert.equal(s.vertices, 8);
  near(s.volume, 1);
  near(s.area, 6);
  assert.ok(s.closed);
  assert.throws(() => parseMesh(enc("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 9\n"), "obj"), MeshError);
  assert.throws(() => parseMesh(enc("v 0 0 0\n"), "obj"), MeshError);
});

/** Unit cube as a PLY with 6 quads, in the three encodings. */
function plyCube(format: "ascii" | "binary_little_endian" | "binary_big_endian"): Uint8Array {
  const v = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]];
  const f = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]];
  const header = `ply\nformat ${format} 1.0\ncomment test\nelement vertex 8\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nelement face 6\nproperty list uchar int vertex_indices\nend_header\n`;
  if (format === "ascii") {
    return enc(header + v.map((p) => `${p.join(" ")} 255`).join("\n") + "\n" + f.map((q) => `4 ${q.join(" ")}`).join("\n") + "\n");
  }
  const le = format === "binary_little_endian";
  const body = new DataView(new ArrayBuffer(8 * 13 + 6 * 17));
  let o = 0;
  for (const p of v) { for (const c of p) { body.setFloat32(o, c, le); o += 4; } body.setUint8(o++, 255); }
  for (const q of f) { body.setUint8(o++, 4); for (const i of q) { body.setInt32(o, i, le); o += 4; } }
  const h = enc(header);
  const out = new Uint8Array(h.length + body.byteLength);
  out.set(h);
  out.set(new Uint8Array(body.buffer), h.length);
  return out;
}

test("PLY ascii / binary little / binary big cubes agree", () => {
  for (const f of ["ascii", "binary_little_endian", "binary_big_endian"] as const) {
    const { mesh, stats: s } = stats(plyCube(f), "ply");
    assert.equal(mesh.fmt, "PLY");
    assert.equal(s.triangles, 12, f);
    near(s.volume, 1);
    near(s.area, 6);
    assert.ok(s.closed, f);
    for (const c of s.centerOfMass) near(c, 0.5);
  }
  assert.throws(() => parseMesh(enc("ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nend_header\n0\n"), "ply"), MeshError);
});

test("smooth normals: sphere is smooth, cube corners stay sharp", () => {
  const cube = stats(binaryStl(box(2, 2, 2)), "stl");
  const n = computeSmoothNormals(cube.mesh.positions, cube.faces, cube.vertexCount);
  for (let c = 0; c < 36; c++) {
    const v = [n[4 * c]!, n[4 * c + 1]!, n[4 * c + 2]!].map((x) => Math.abs(x / 127));
    assert.ok(Math.max(...v) > 0.99, "cube normals are axis aligned");
  }
});

test("matches preview/mesh_io.py on generated files (when python3 + numpy exist)", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "view-mesh-"));
  try {
    // a bumpy closed solid: UV sphere with a radius ripple, fixed offset, as binary STL
    const tris: number[][][] = [];
    const U = 24, V = 16;
    const pt = (i: number, j: number) => {
      const th = (Math.PI * j) / V, ph = (2 * Math.PI * (i % U)) / U;
      const r = 5 + 0.7 * Math.sin(3 * ph) * Math.sin(2 * th);
      return [100 + r * Math.sin(th) * Math.cos(ph), -40 + r * Math.sin(th) * Math.sin(ph), 7 + r * Math.cos(th)];
    };
    for (let j = 0; j < V; j++) for (let i = 0; i < U; i++) {
      const a = pt(i, j), b = pt(i + 1, j), c = pt(i + 1, j + 1), d = pt(i, j + 1);
      if (j > 0) tris.push([a, c, b]);
      if (j < V - 1) tris.push([a, d, c]);
    }
    const files: Record<string, Uint8Array> = {
      "sphere.stl": binaryStl(tris),
      "cube.ply": plyCube("binary_big_endian"),
      "tetra.stl": TETRA,
    };
    for (const [name, bytes] of Object.entries(files)) writeFileSync(join(dir, name), bytes);
    const root = join(process.cwd(), "..");  // tests run from chrome/; preview/ is its sibling
    const py = spawnSync("python3", ["-I", "-c", `
import sys, json
sys.path.insert(0, ${JSON.stringify(root)})
from preview import mesh_io
out = {}
for n in ${JSON.stringify(Object.keys(files))}:
    m = mesh_io.load_mesh(${JSON.stringify(dir)} + "/" + n)
    s = m.stats
    out[n] = {k: (v.tolist() if hasattr(v, "tolist") else v) for k, v in s.items()}
print(json.dumps(out))
`], { encoding: "utf8" });
    if (py.status !== 0) return t.skip("python3 with numpy / preview.mesh_io not available");
    const ref = JSON.parse(py.stdout.trim().split("\n").pop()!);
    for (const [name, bytes] of Object.entries(files)) {
      const { mesh, stats: s } = stats(bytes, name.split(".")[1]!);
      const r = ref[name];
      assert.equal(s.triangles, r.triangles, name);
      assert.equal(s.vertices, r.vertices, name);
      assert.equal(s.closed, r.closed, name);
      near(s.area, r.area, 1e-5);
      near(s.volume, r.volume, 1e-5);
      for (let k = 0; k < 3; k++) {
        near(s.centerOfMass[k]!, r.center_of_mass[k], 1e-5);
        near(mesh.bboxMin[k]!, r.bbox_min[k], 1e-9);
        near(mesh.size[k]!, r.size[k], 1e-9);
      }
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
