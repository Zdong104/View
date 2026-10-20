// One save job: everything that turns the page model into PDF bytes (pdf-lib
// restructuring, markup, invisible OCR text, image re-encoding, encryption).
// No DOM: it runs in save.worker.ts, or on the main thread if the worker can't
// start. The viewer collects the source bytes (pdf.js saveDocument() for
// edited forms) and posts a SaveJob; fonts for markup text come back to the
// main thread, where the Local Font Access API lives.
import type { Annotation } from "../../markup/api.ts";
import type { FontProvider } from "../../markup/pdf-fonts.ts";
import { buildPdf, type BuildPage, type BuildSource, type EncryptOptions } from "./build.ts";
import type { SurfaceLine } from "./search.ts";

export interface SaveJob {
  sources: Record<number, BuildSource>;
  pages: BuildPage[];
  base: number;
  keepPassword?: boolean;
  basePassword?: string | null;
  encrypt?: EncryptOptions;
  /** Output page index -> OCR lines (surface units). */
  textLayers?: [number, SurfaceLine[]][];
  reduce?: boolean;
  markup?: {
    annotations: Annotation[];
    /** Surface id -> output page index. */
    surfaces: [string, number][];
    /** pdf.js ids of source-0 annotations the markup layer owns (removed first). */
    removeIds: string[];
    flatten: boolean;
  };
}

export interface JobHooks {
  progress(msg: string): void;
  warn(msg: string): void;
  /** Font provider for markup text (undefined = the markup module's default). */
  fonts?: FontProvider | null;
}

export interface JobResult {
  bytes: Uint8Array;
  reduced: number;
  /** Where it ran (the worker normally). */
  via?: "worker" | "main";
}

export async function runSaveJob(job: SaveJob, hooks: JobHooks): Promise<JobResult> {
  let reduced = 0;
  const m = job.markup;
  const exp = m && (m.annotations.length || m.removeIds.length) ? await import("../../markup/pdf-export.ts") : null;
  const bytes = await buildPdf(async (i) => {
    const s = job.sources[i];
    if (!s) throw new Error(`Missing source ${i}`);
    return s;
  }, job.pages, {
    base: job.base,
    keepPassword: job.keepPassword,
    basePassword: job.basePassword,
    encrypt: job.encrypt,
    textLayers: job.textLayers ? new Map(job.textLayers) : undefined,
    progress: hooks.progress,
    prepare: exp && m!.removeIds.length ? (src, doc) => { if (src === 0) exp.removePdfAnnotations(doc, m!.removeIds); } : undefined,
    annotate: exp && m!.annotations.length ? async (doc) => {
      const index = new Map(m!.surfaces);
      const { warnings } = await exp.exportPdfAnnotations(doc, m!.annotations, (s) => index.get(s) ?? null, {
        flatten: m!.flatten, ...(hooks.fonts !== undefined ? { fonts: hooks.fonts } : {}),
      });
      for (const w of warnings) hooks.warn(w);
    } : undefined,
    transform: job.reduce ? async (doc) => {
      const { reduceImages } = await import("./reduce.ts");
      reduced = await reduceImages(doc, hooks.progress);
    } : undefined,
  });
  return { bytes, reduced };
}

// ---- worker protocol ---------------------------------------------------------------

export type ToWorker =
  | { type: "run"; job: SaveJob }
  | { type: "fonts"; id: number; list: { key: number; name?: string; postscriptName?: string }[] }
  | { type: "font-bytes"; id: number; bytes: Uint8Array | null };

export type FromWorker =
  | { type: "progress"; msg: string }
  | { type: "warn"; msg: string }
  | { type: "fonts"; id: number; req: import("../../markup/pdf-fonts.ts").FontRequest }
  | { type: "font-load"; id: number; key: number }
  | { type: "done"; result: JobResult }
  | { type: "error"; message: string };
