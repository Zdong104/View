// Main-thread side of the table worker (src/viewers/table/worker.js): a typed RPC
// wrapper plus the shape of the state snapshots the worker publishes.

export interface TableState {
  loaded: boolean;
  scanning: boolean;
  /** Rows currently shown (after sort / filter, header excluded). */
  nView: number;
  nbase: number;
  ncols: number;
  hasHeader: boolean;
  headers: string[];
  delim: string;
  delimName: string;
  encoding: string;
  encLabel: string;
  /** "\n", "\r\n" or "\r": the file's line ending, and the one written on save. */
  eol: string;
  outEol: string;
  quoting: "minimal" | "all";
  outQuoting: "minimal" | "all";
  dirty: boolean;
  canUndo: boolean;
  canRedo: boolean;
  undoLabel: string;
  redoLabel: string;
  sortCol: number | null;
  sortAsc: boolean;
  filterOn: boolean;
  search: string;
  matches: number;
  /** Bumped by every data change; `struct` only by changes that move rows or columns. */
  epoch: number;
  struct: number;
  fileSize: number;
  name: string;
  endsWithNewline: boolean;
}

export interface RowsResult { epoch: number; struct: number; rows: string[][] }
export interface SearchResult { count: number; vpos: Int32Array<ArrayBufferLike>; state: TableState; cancelled?: boolean }
export interface SelectionStats { n: number; sum: number; min: number; max: number }
export interface SerializeResult { blob: Blob; note: string; fmt: string }
export interface InfoResult { types: string[]; empties: number[] | null; rows: number }

export type WorkerEvent =
  | { event: "state"; state: TableState }
  | { event: "scan"; nbase: number; ncols: number; frac: number }
  | { event: "progress"; op: "sort" | "search" | "save"; frac: number }
  | { event: "error"; message: string };

export interface LoadOptions { encoding?: string; delimiter?: string; header?: boolean }

export class TableClient {
  private readonly worker: Worker;
  private nextId = 1;
  private readonly pending = new Map<number, { resolve: (v: never) => void; reject: (e: Error) => void }>();
  onEvent: (e: WorkerEvent) => void = () => {};

  constructor() {
    this.worker = new Worker(chrome.runtime.getURL("vendor/table/worker.js"));
    this.worker.onmessage = (ev: MessageEvent) => {
      const m = ev.data as { id?: number; ok?: boolean; result?: unknown; error?: string } & Partial<WorkerEvent>;
      if (m.id === undefined) { this.onEvent(m as WorkerEvent); return; }
      const p = this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      if (m.ok) p.resolve(m.result as never); else p.reject(new Error(m.error ?? "worker error"));
    };
    this.worker.onerror = (ev) => {
      const err = new Error(ev.message || "The table worker crashed");
      for (const p of this.pending.values()) p.reject(err);
      this.pending.clear();
      this.onEvent({ event: "error", message: err.message });
    };
  }

  call<T>(op: string, args: Record<string, unknown> = {}): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (v: never) => void, reject });
      this.worker.postMessage({ id, op, ...args });
    });
  }

  load(blob: Blob, name: string, o: LoadOptions = {}): Promise<TableState> {
    return this.call("load", { blob, name, encoding: o.encoding, delimiter: o.delimiter, header: o.header });
  }
  rows(start: number, count: number): Promise<RowsResult> { return this.call("rows", { start, count }); }

  terminate(): void {
    this.worker.terminate();
    for (const p of this.pending.values()) p.reject(new Error("closed"));
    this.pending.clear();
  }
}
