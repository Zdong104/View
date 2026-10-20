// Save worker: runs a SaveJob (job.ts) off the main thread. Built by the
// `*.worker.ts` convention to viewers/pdf/save.worker.js. Markup text fonts
// are requested from the page (Local Font Access only exists there).
import type { FontProvider } from "../../markup/pdf-fonts.ts";
import { runSaveJob, type FromWorker, type ToWorker } from "./job.ts";

const post = (m: FromWorker, transfer: Transferable[] = []) => (self as unknown as Worker).postMessage(m, transfer);

let nextId = 1;
const waiting = new Map<number, (v: unknown) => void>();
const ask = <T>(m: (id: number) => FromWorker): Promise<T> => new Promise((resolve) => {
  const id = nextId++;
  waiting.set(id, resolve as (v: unknown) => void);
  post(m(id));
});

const fonts: FontProvider = async (req) => {
  const list = await ask<{ key: number; name?: string; postscriptName?: string }[]>((id) => ({ type: "fonts", id, req }));
  return list.map((f) => ({
    name: f.name,
    postscriptName: f.postscriptName,
    load: async () => {
      const b = await ask<Uint8Array | null>((id) => ({ type: "font-load", id, key: f.key }));
      if (!b) throw new Error("font unavailable");
      return b;
    },
  }));
};

self.onmessage = async (e: MessageEvent<ToWorker>) => {
  const m = e.data;
  if (m.type === "fonts") { waiting.get(m.id)?.(m.list); waiting.delete(m.id); return; }
  if (m.type === "font-bytes") { waiting.get(m.id)?.(m.bytes); waiting.delete(m.id); return; }
  try {
    const result = await runSaveJob(m.job, {
      progress: (msg) => post({ type: "progress", msg }),
      warn: (msg) => post({ type: "warn", msg }),
      fonts,
    });
    post({ type: "done", result }, [result.bytes.buffer]);
  } catch (err) {
    post({ type: "error", message: (err as Error)?.message ?? String(err) });
  }
};
