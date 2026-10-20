// Entry of the OCR module Worker. esbuild only bundles the build's entry
// points, so this module doubles as its own worker script: the page imports it
// (cheap: the engine is a separate chunk) only to learn its chunk URL, and the
// Worker started from that URL runs `serve()`.
import type { Engine, EngineConfig } from "./engine.ts";

export const workerUrl: string = import.meta.url;

export type Request =
  | { type: "init"; id: number; config: EngineConfig }
  | { type: "recognize"; id: number; image: ImageBitmap | ImageData | Blob }
  | { type: "cancel"; id: number };

export type Reply = { id: number; ok: true; value: unknown } | { id: number; ok: false; error: string; abort?: boolean };

interface WorkerScope {
  onmessage: ((e: MessageEvent<Request>) => void) | null;
  postMessage(msg: Reply): void;
}

const scope = globalThis as unknown as WorkerScope & { WorkerGlobalScope?: unknown };
if (typeof scope.WorkerGlobalScope === "function") serve();

class Cancelled extends Error {}

function serve() {
  let engine: Promise<Engine> | null = null;
  const cancelled = new Set<number>();
  let queue: Promise<unknown> = Promise.resolve();
  const reply = (r: Reply) => scope.postMessage(r);
  const fail = (id: number, e: unknown) =>
    reply({ id, ok: false, error: e instanceof Error ? e.message : String(e), abort: e instanceof Cancelled });

  scope.onmessage = (e) => {
    const m = e.data;
    if (m.type === "cancel") {
      cancelled.add(m.id);
      return;
    }
    if (m.type === "init") {
      engine = import("./engine.ts").then(({ Engine }) => new Engine(m.config));
      engine.then((en) => en.load()).then(() => reply({ id: m.id, ok: true, value: null }), (err) => fail(m.id, err));
      return;
    }
    // One recognition at a time keeps latency predictable (as on the desktop).
    queue = queue.then(async () => {
      const check = () => {
        if (cancelled.has(m.id)) throw new Cancelled("cancelled");
      };
      try {
        check();
        if (!engine) throw new Error("OCR worker is not initialised");
        reply({ id: m.id, ok: true, value: await (await engine).recognize(m.image, check) });
      } catch (err) {
        fail(m.id, err);
      } finally {
        cancelled.delete(m.id);
        if (m.image instanceof ImageBitmap) m.image.close();
      }
    });
  };
}
