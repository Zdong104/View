// IndexedDB storage for FileSystemFileHandles (they are structured-cloneable):
//  - "pending": hand a handle to a new viewer tab (viewer.html?handle=<id>)
//  - "recent":  the Open Recent list on the empty window
const DB = "view";
const STORE = "handles";

function db(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function tx<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const d = await db();
  return new Promise((resolve, reject) => {
    const r = fn(d.transaction(STORE, mode).objectStore(STORE));
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

export async function stashHandle(h: FileSystemFileHandle): Promise<string> {
  const id = crypto.randomUUID();
  await tx("readwrite", (s) => s.put(h, `pending:${id}`));
  return id;
}

export async function takeHandle(id: string): Promise<FileSystemFileHandle | null> {
  const h = await tx<FileSystemFileHandle | undefined>("readonly", (s) => s.get(`pending:${id}`));
  await tx("readwrite", (s) => s.delete(`pending:${id}`));
  return h ?? null;
}

export interface RecentEntry { name: string; handle: FileSystemFileHandle; opened: number }

export async function recentFiles(): Promise<RecentEntry[]> {
  return ((await tx<RecentEntry[] | undefined>("readonly", (s) => s.get("recent"))) ?? []);
}

export async function addRecent(handle: FileSystemFileHandle): Promise<void> {
  const list = await recentFiles();
  const kept: RecentEntry[] = [];
  for (const r of list) if (!(await r.handle.isSameEntry(handle))) kept.push(r);
  kept.unshift({ name: handle.name, handle, opened: Date.now() });
  await tx("readwrite", (s) => s.put(kept.slice(0, 10), "recent"));
}

export async function removeRecent(handle: FileSystemFileHandle): Promise<void> {
  const kept: RecentEntry[] = [];
  for (const r of await recentFiles()) if (!(await r.handle.isSameEntry(handle))) kept.push(r);
  await tx("readwrite", (s) => s.put(kept, "recent"));
}

export async function clearRecent(): Promise<void> {
  await tx("readwrite", (s) => s.delete("recent"));
}

/** Ask for permission again (handles from IndexedDB start as "prompt"). */
export async function ensurePermission(h: FileSystemFileHandle, mode: "read" | "readwrite"): Promise<boolean> {
  const opts = { mode } as const;
  if ((await h.queryPermission(opts)) === "granted") return true;
  return (await h.requestPermission(opts)) === "granted";
}
