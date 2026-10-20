// Types for mesh-core.js (plain JS shipped as a package file; see copy.json).
export class MeshError extends Error {}

export interface IndexedMesh {
  /** Recentred vertex table (xyz triples). */
  verts: Float32Array;
  /** Triangle indices into `verts` (3 per triangle, non-finite triangles dropped). */
  faces: Uint32Array;
}

export interface ParsedMesh {
  /** Triangle soup, 9 floats per triangle, recentred on the bbox centre. */
  positions: Float32Array;
  indexed: IndexedMesh | null;
  triangles: number;
  bboxMin: number[];
  bboxMax: number[];
  size: number[];
  /** The bbox centre that was subtracted from `positions`. */
  origin: number[];
  fmt: string;
  /** Natural up axis of the format. */
  up: "z" | "y";
  header: string;
  fileSize: number;
}

export interface MeshStats {
  vertices: number;
  triangles: number;
  area: number;
  volume: number;
  volumeSigned: number;
  centerOfMass: number[];
  closed: boolean;
  boundaryEdges: number;
  nonmanifoldEdges: number;
}

export function parseMesh(data: ArrayBuffer | Uint8Array, ext: string): ParsedMesh;
export function weldPoints(pts: Float32Array): { ids: Int32Array; count: number };
export function edgeStats(faces: ArrayLike<number>, nv: number): { closed: boolean; boundaryEdges: number; nonmanifoldEdges: number };
export function analyzeMesh(
  mesh: Omit<ParsedMesh, "positions"> & { positions?: Float32Array },
  positions?: Float32Array,
): { stats: MeshStats; faces: Int32Array; vertexCount: number };
export function computeSmoothNormals(positions: Float32Array, faces: Int32Array, nv: number, creaseDeg?: number): Int8Array;
