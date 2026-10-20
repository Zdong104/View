// Main-thread side of the image worker: a typed request/response wrapper.
import type { Progress, Request, Responses } from "./worker.ts";

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void; progress?: (p: Progress) => void };

export class ImageWorker {
  private worker: Worker;
  private seq = 0;
  private pending = new Map<number, Pending>();

  private constructor(url: string) {
    this.worker = new Worker(url, { type: "module", name: "view-image" });
    this.worker.addEventListener("message", (e: MessageEvent<{ seq: number; res?: unknown; error?: string; progress?: Progress }>) => {
      const p = this.pending.get(e.data.seq);
      if (!p) return;
      if (e.data.progress) { p.progress?.(e.data.progress); return; }
      this.pending.delete(e.data.seq);
      if (e.data.error !== undefined) p.reject(new Error(e.data.error));
      else p.resolve(e.data.res);
    });
    this.worker.addEventListener("error", (e) => {
      for (const p of this.pending.values()) p.reject(new Error(e.message || "image worker failed"));
      this.pending.clear();
    });
  }

  static async start(): Promise<ImageWorker> {
    const { workerUrl } = await import("./worker.ts");
    return new ImageWorker(workerUrl);
  }

  call<T extends Request["t"]>(req: Extract<Request, { t: T }>, opts: { transfer?: Transferable[]; progress?: (p: Progress) => void } = {}): Promise<Responses[T]> {
    const seq = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(seq, { resolve: resolve as (v: unknown) => void, reject, progress: opts.progress });
      this.worker.postMessage({ seq, req }, opts.transfer ?? []);
    });
  }

  terminate(): void {
    this.worker.terminate();
    for (const p of this.pending.values()) p.reject(new Error("closed"));
    this.pending.clear();
  }
}
