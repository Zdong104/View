// Main-thread side of the save worker (save.worker.ts): runs a SaveJob there,
// relays progress and warnings, and answers font requests for markup text
// with the Local Font Access API (only available on the page). Falls back to
// running the job on this thread if the worker can't start.
import type { FontSource } from "../../markup/pdf-fonts.ts";
import { runSaveJob, type FromWorker, type JobResult, type SaveJob, type ToWorker } from "./job.ts";

export interface RunHooks { progress(msg: string): void; warn(msg: string): void }

export function runJob(job: SaveJob, hooks: RunHooks): Promise<JobResult> {
  let worker: Worker;
  try {
    worker = new Worker(chrome.runtime.getURL("viewers/pdf/save.worker.js"), { type: "module" });
  } catch {
    return runSaveJob(job, hooks).then((r) => ({ ...r, via: "main" as const }));
  }
  return new Promise<JobResult>((resolve, reject) => {
    let started = false;
    const fontSources = new Map<number, FontSource>();
    let nextKey = 1;
    const send = (m: ToWorker, transfer: Transferable[] = []) => worker.postMessage(m, transfer);
    const finish = () => worker.terminate();
    worker.onerror = (e) => {
      e.preventDefault();
      finish();
      // The worker script failed to load: do the work here instead.
      if (!started) runSaveJob(job, hooks).then((r) => resolve({ ...r, via: "main" }), reject);
      else reject(new Error(e.message || "The save worker crashed"));
    };
    worker.onmessage = async (e: MessageEvent<FromWorker>) => {
      started = true;
      const m = e.data;
      switch (m.type) {
        case "progress": hooks.progress(m.msg); break;
        case "warn": hooks.warn(m.msg); break;
        case "done": finish(); resolve({ ...m.result, via: "worker" }); break;
        case "error": finish(); reject(new Error(m.message)); break;
        case "fonts": {
          let list: { key: number; name?: string; postscriptName?: string }[] = [];
          try {
            const { localFontProvider } = await import("../../markup/pdf-fonts.ts");
            list = (await localFontProvider(m.req)).map((f) => {
              const key = nextKey++;
              fontSources.set(key, f);
              return { key, name: f.name, postscriptName: f.postscriptName };
            });
          } catch { /* no fonts: the exporter falls back to standard fonts */ }
          send({ type: "fonts", id: m.id, list });
          break;
        }
        case "font-load": {
          let bytes: Uint8Array | null = null;
          try { bytes = new Uint8Array(await fontSources.get(m.key)!.load()); } catch { bytes = null; }
          send({ type: "font-bytes", id: m.id, bytes }, bytes ? [bytes.buffer] : []);
          break;
        }
      }
    };
    // Sources are copied (structured clone): the viewer keeps its own bytes.
    send({ type: "run", job });
    // A worker that dies before answering never fires onmessage: the
    // first message proves it loaded; errors before that fall back above.
  });
}
