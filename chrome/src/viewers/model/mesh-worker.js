// Module worker: parses a model and computes its statistics off the main thread.
// Plain JS (see mesh-core.js). Protocol:
//   main -> { type: "parse", buffer: ArrayBuffer, ext }            (buffer transferred)
//   worker -> { type: "mesh", positions, info }                    (positions transferred)
//   main -> { type: "analyze", positions, smooth: boolean }        (positions transferred back)
//   worker -> { type: "analyzed", positions, stats, normals, seconds } (all transferred back)
//   worker -> { type: "error", message }
// The positions array makes a round trip main -> GPU upload -> worker -> main, so
// big models are never copied.
import { MeshError, analyzeMesh, computeSmoothNormals, parseMesh } from "./mesh-core.js";

/** Above this many triangles the smooth-normal pass is skipped (flat shading only). */
const SMOOTH_LIMIT = 4_000_000;
let mesh = null;
let t0 = 0;

self.onmessage = (ev) => {
  const m = ev.data;
  try {
    if (m.type === "parse") {
      t0 = performance.now();
      mesh = parseMesh(m.buffer, m.ext);
      const positions = mesh.positions;
      mesh.positions = null;
      const { fmt, up, header, triangles, bboxMin, bboxMax, size, origin, fileSize } = mesh;
      self.postMessage({
        type: "mesh",
        positions,
        info: { fmt, up, header, triangles, bboxMin, bboxMax, size, origin, fileSize, seconds: (performance.now() - t0) / 1000 },
      }, [positions.buffer]);
    } else if (m.type === "analyze") {
      const positions = m.positions;
      const { stats, faces, vertexCount } = analyzeMesh(mesh, positions);
      let normals = null;
      if (m.smooth && stats.triangles <= SMOOTH_LIMIT) normals = computeSmoothNormals(positions, faces, vertexCount);
      mesh = null;
      const transfer = [positions.buffer];
      if (normals) transfer.push(normals.buffer);
      self.postMessage({ type: "analyzed", positions, stats, normals, seconds: (performance.now() - t0) / 1000 }, transfer);
    }
  } catch (e) {
    self.postMessage({ type: "error", message: e instanceof MeshError ? e.message : `Could not read this model: ${e?.message ?? e}` });
  }
};
