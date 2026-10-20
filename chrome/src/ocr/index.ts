// Live Text OCR service (port of preview/ocr.py): PP-OCR det/cls/rec models
// (the ones the desktop's RapidOCR ships) on onnxruntime-web WASM, in a module
// Worker. Every createOcr() handle shares one worker; it is started (and the
// models are loaded) on the first createOcr() and stopped when the last handle
// is disposed. So: call ctx.services.ocr() when Live Text is wanted, and
// dispose() the handle in the viewer's destroy().
//
// OcrLine.charQuads holds one quad (8 numbers) per Unicode code point of
// `text` (Array.from(text)), in reading direction: TL, TR, BR, BL of the glyph.
// For a selectable overlay, see ./text-layer.ts (createTextLayer).
import type { OcrResult, OcrService } from "./api.ts";
import type { EngineConfig } from "./engine.ts";
import type { Timings } from "./pipeline.ts";
import type { Reply, Request } from "./worker.ts";

export type { OcrLine, OcrResult, OcrService } from "./api.ts";
export { createTextLayer, linesToText, type TextLayer, type TextLayerOptions } from "./text-layer.ts";

type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;
type Pending = { resolve: (v: unknown) => void; reject: (e: unknown) => void };

class Client {
  refs = 0;
  ready = false;
  /** Timings of the last recognition (for tests and benchmarks). */
  lastTimings: Timings | null = null;
  private worker: Promise<Worker>;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private loaded: Promise<unknown>;

  constructor(config: Partial<EngineConfig> = {}) {
    this.worker = import("./worker.ts").then(({ workerUrl }) => {
      const w = new Worker(workerUrl, { type: "module", name: "view-ocr" });
      w.onmessage = (e: MessageEvent<Reply>) => this.settle(e.data);
      w.onerror = (e) => this.failAll(new Error(e.message || "OCR worker failed to start"));
      return w;
    });
    const base = typeof chrome !== "undefined" && chrome.runtime?.getURL ? chrome.runtime.getURL("") : new URL("/", location.href).href;
    this.loaded = this.call({ type: "init", config: { base, ...config } }).then(() => (this.ready = true));
    this.loaded.catch(() => {}); // surfaced by recognize()
  }

  private settle(r: Reply) {
    const p = this.pending.get(r.id);
    if (!p) return;
    this.pending.delete(r.id);
    if (r.ok) p.resolve(r.value);
    else p.reject(r.abort ? new DOMException("Text recognition was cancelled", "AbortError") : new Error(r.error));
  }

  private failAll(e: Error) {
    for (const p of this.pending.values()) p.reject(e);
    this.pending.clear();
  }

  private call(msg: DistributiveOmit<Extract<Request, { type: "init" | "recognize" }>, "id">, signal?: AbortSignal): Promise<unknown> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      if (signal) {
        const onAbort = () => {
          if (!this.pending.delete(id)) return;
          void this.worker.then((w) => w.postMessage({ type: "cancel", id } satisfies Request));
          reject(new DOMException("Text recognition was cancelled", "AbortError"));
        };
        signal.addEventListener("abort", onAbort, { once: true });
      }
      void this.worker.then((w) => w.postMessage({ ...msg, id } as Request), reject);
    });
  }

  async recognize(image: ImageBitmap | ImageData | Blob, signal?: AbortSignal): Promise<OcrResult> {
    signal?.throwIfAborted();
    await this.loaded;
    signal?.throwIfAborted();
    const { result, timings } = (await this.call({ type: "recognize", image }, signal)) as { result: OcrResult; timings: Timings };
    this.lastTimings = timings;
    return result;
  }

  terminate() {
    this.failAll(new Error("OCR service was disposed"));
    void this.worker.then((w) => w.terminate());
  }
}

let shared: Client | null = null;

export function createOcr(): OcrService & { readonly lastTimings: Timings | null } {
  const client = (shared ??= new Client());
  client.refs++;
  let disposed = false;
  return {
    get ready() {
      return !disposed && client.ready;
    },
    get lastTimings() {
      return client.lastTimings;
    },
    recognize(image, opts) {
      if (disposed) return Promise.reject(new Error("OCR service was disposed"));
      return client.recognize(image, opts?.signal);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      if (--client.refs === 0) {
        client.terminate();
        if (shared === client) shared = null;
      }
    },
  };
}
