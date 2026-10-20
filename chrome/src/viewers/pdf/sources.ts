// Source PDFs: every document whose pages appear in the viewer (the opened
// file, inserted/merged PDFs, converted images, blank pages). Each is one
// pdf.js document. Loading streams where possible:
//   - http(s): pdf.js Range requests (fast first page on huge files)
//   - local Blob/File: a PDFDataRangeTransport over Blob.slice(), so a 600 MB
//     file is not read into memory just to show page 1
//   - small data: handed over directly
import * as pdfjs from "pdfjs-dist";
import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";

const asset = (p: string) => chrome.runtime.getURL(`vendor/pdfjs/${p}`);
pdfjs.GlobalWorkerOptions.workerSrc = asset("pdf.worker.min.mjs");

const COMMON = {
  cMapUrl: asset("cmaps/"),
  cMapPacked: true,
  standardFontDataUrl: asset("standard_fonts/"),
  wasmUrl: asset("wasm/"),
  iccUrl: asset("iccs/"),
  isEvalSupported: false, // MV3 CSP forbids eval
  enableXfa: true,
};

/** Read Blobs through range requests above this size. */
const RANGE_MIN = 4 << 20;

/** Unrotated CropBox of a page and its own /Rotate. */
export interface PageInfo {
  /** CropBox width/height in points (unrotated). */
  w: number;
  h: number;
  /** CropBox origin in user space (lower-left). */
  x0: number;
  y0: number;
  /** The page's own /Rotate. */
  rotate: number;
}

export interface PdfSource {
  readonly id: number;
  name: string;
  readonly doc: PDFDocumentProxy;
  /** Password used to open it, if it was encrypted with a user password. */
  readonly password: string | null;
  /** The original file bytes (fetched on demand, cached by pdf.js / the Blob). */
  bytes(): Promise<Uint8Array>;
  /** Known page infos (index -> info); missing ones use `fallback`. */
  readonly infos: (PageInfo | undefined)[];
  readonly fallback: PageInfo;
  /** True once any form value was changed in the annotation storage. */
  formsEdited: boolean;
  destroy(): Promise<void>;
}

class BlobTransport extends pdfjs.PDFDataRangeTransport {
  private readonly blob: Blob;
  constructor(blob: Blob) {
    super(blob.size, new Uint8Array(0));
    this.blob = blob;
  }
  override requestDataRange(begin: number, end: number): void {
    void this.blob.slice(begin, end).arrayBuffer().then((b) => this.onDataRange(begin, new Uint8Array(b)));
  }
}

export type PasswordAsk = (name: string, wrong: boolean) => Promise<string | null>;

export interface OpenInput {
  url?: string;
  blob?: Blob;
  data?: Uint8Array;
}

export class PasswordCancelled extends Error {
  constructor() { super("Password entry cancelled"); }
}

export function infoOf(page: PDFPageProxy): PageInfo {
  const [x0, y0, x1, y1] = page.view as [number, number, number, number];
  return { w: x1 - x0, h: y1 - y0, x0, y0, rotate: ((page.rotate % 360) + 360) % 360 };
}

export async function openSource(id: number, input: OpenInput, name: string, askPassword: PasswordAsk, signal?: AbortSignal): Promise<PdfSource> {
  let task: ReturnType<typeof pdfjs.getDocument>;
  let bytes: () => Promise<Uint8Array>;
  if (input.url) {
    task = pdfjs.getDocument({ ...COMMON, url: input.url, withCredentials: true, disableAutoFetch: true, rangeChunkSize: 1 << 18 });
    bytes = async () => doc.getData();
  } else if (input.blob && input.blob.size >= RANGE_MIN) {
    const blob = input.blob;
    task = pdfjs.getDocument({ ...COMMON, range: new BlobTransport(blob), disableAutoFetch: true, disableStream: true, rangeChunkSize: 1 << 18 });
    bytes = async () => new Uint8Array(await blob.arrayBuffer());
  } else {
    const data = input.data ?? new Uint8Array(await input.blob!.arrayBuffer());
    // pdf.js transfers the buffer to its worker: keep our own copy for saving.
    task = pdfjs.getDocument({ ...COMMON, data: data.slice() });
    bytes = async () => data;
  }
  let password: string | null = null;
  task.onPassword = (update: (pw: string) => void, reason: number) => {
    void askPassword(name, reason === pdfjs.PasswordResponses.INCORRECT_PASSWORD).then((pw) => {
      if (pw === null) void task.destroy();
      else { password = pw; update(pw); }
    });
  };
  signal?.addEventListener("abort", () => void task.destroy(), { once: true });
  let doc: PDFDocumentProxy;
  try {
    doc = await task.promise;
  } catch (e) {
    if (task.destroyed) throw new PasswordCancelled();
    throw e;
  }
  const first = await doc.getPage(1);
  const infos: (PageInfo | undefined)[] = new Array(doc.numPages);
  infos[0] = infoOf(first);
  const src: PdfSource = {
    id,
    name,
    doc,
    get password() { return password; },
    bytes,
    infos,
    fallback: infos[0],
    formsEdited: false,
    destroy: () => task.destroy(),
  };
  (doc.annotationStorage as { onSetModified: (() => void) | null }).onSetModified = () => { src.formsEdited = true; };
  return src;
}
