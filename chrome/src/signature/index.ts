// Signature store: vector signatures kept in IndexedDB (the desktop keeps
// them as JSON files in ~/Pictures/Signatures, preview/signature.py), plus the
// capture dialog (capture.ts) and small rendering helpers for thumbnails.
import type { Signature, SignatureStore } from "./api.ts";
import type { StrokePoints } from "../markup/api.ts";
import { drawAnnotation, triples } from "../markup/geom.ts";
import { measure, primsToCanvas, primsToSvg, svgEl } from "../markup/render.ts";

/** What we store: the contract's Signature plus the ink the user picked. */
export interface StoredSignature extends Signature {
  /** Ink colour (CSS hex). */
  color?: string;
  /** Pen width as a fraction of the signature's box width. */
  pen?: number;
}

const DB = "view-signatures";
const STORE = "signatures";

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "id" });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx<T>(db: IDBDatabase, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const t = db.transaction(STORE, mode);
    const req = fn(t.objectStore(STORE));
    t.oncomplete = () => resolve(req.result);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error);
  });
}

/**
 * Normalize captured strokes (any units) into a signature: points in 0..1 of
 * the ink bounding box padded by half the pen (signature.py _bbox).
 */
export function normalizeStrokes(strokes: StrokePoints[], pen: number): { strokes: StrokePoints[]; aspect: number; pen: number } {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const s of strokes) for (const [x, y] of triples(s)) {
    x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
  }
  const pad = pen * 1.4 / 2 + 0.5;
  x0 -= pad; y0 -= pad; x1 += pad; y1 += pad;
  const w = Math.max(x1 - x0, 1e-6), h = Math.max(y1 - y0, 1e-6);
  const r = (v: number) => Math.round(v * 1e4) / 1e4;
  return {
    strokes: strokes.filter((s) => s.length >= 3).map((s) => s.map((v, i) => (i % 3 === 0 ? r((v - x0) / w) : i % 3 === 1 ? r((v - y0) / h) : r(v)))),
    aspect: w / h,
    pen: pen / w,
  };
}

/** A signature drawn into a box (the same drawing a placed signature gets). */
export function signatureDrawing(sig: Signature, w: number) {
  const s = sig as StoredSignature;
  const h = w / Math.max(sig.aspect, 1e-3);
  return drawAnnotation({
    type: "signature", id: sig.id, surface: "", created: 0, modified: 0, x: 0, y: 0, w, h, strokes: sig.strokes,
    style: { stroke: s.color ?? "#141414", fill: null, width: (s.pen ?? 0.01) * w, opacity: 1 },
  }, measure);
}

export function signatureSvg(sig: Signature): SVGSVGElement {
  const w = 300, h = w / Math.max(sig.aspect, 1e-3);
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, "aria-hidden": "true" });
  const g = svgEl("g");
  primsToSvg(signatureDrawing(sig, w), 1, g);
  svg.append(g);
  return svg;
}

/** PNG of the signature (desktop: "Copy as Image"). */
export async function signaturePng(sig: Signature, px = 1200): Promise<Blob> {
  const h = Math.max(1, Math.round(px / Math.max(sig.aspect, 1e-3)));
  const c = new OffscreenCanvas(px, h);
  const ctx = c.getContext("2d")!;
  primsToCanvas(ctx, signatureDrawing(sig, px), 1);
  return c.convertToBlob({ type: "image/png" });
}

export function createSignatureStore(): SignatureStore {
  let db: Promise<IDBDatabase | null> | null = null;
  const mem: StoredSignature[] = [];
  const getDb = () => (db ??= typeof indexedDB === "undefined" ? Promise.resolve(null) : openDb().catch(() => null));

  const store: SignatureStore = {
    async list() {
      const d = await getDb();
      const all = d ? await tx<StoredSignature[]>(d, "readonly", (s) => s.getAll()) : [...mem];
      return all.filter((s) => s.strokes?.length).sort((a, b) => b.created - a.created);
    },
    async add(sig) {
      const full: StoredSignature = { ...structuredClone(sig), id: crypto.randomUUID(), created: Date.now() };
      const d = await getDb();
      if (d) await tx(d, "readwrite", (s) => s.put(full));
      else mem.push(full);
      return full;
    },
    async remove(id) {
      const d = await getDb();
      if (d) await tx(d, "readwrite", (s) => s.delete(id));
      else { const i = mem.findIndex((s) => s.id === id); if (i >= 0) mem.splice(i, 1); }
    },
    async capture(opts) {
      const { captureSignature } = await import("./capture.ts");
      const res = await captureSignature(opts?.mode ?? "pointer");
      if (!res) return null;
      return store.add(res);
    },
  };
  return store;
}
