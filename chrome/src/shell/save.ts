// Save plumbing. Viewers produce bytes (Viewer.save -> SaveResult); the shell
// decides where they go:
//   1. opened from a handle (picker / drag & drop) and replacesOriginal
//        -> overwrite in place via FileSystemFileHandle.createWritable()
//   2. otherwise (web URL, file:// URL, Save As, Export)
//        -> showSaveFilePicker() (File System Access API), remembering the new handle
//   3. no FSA (should not happen in Chrome; kept for robustness)
//        -> <a download> with a blob: URL (lands in Downloads)
import type { SaveResult } from "../viewers/contract.ts";
import { ensurePermission } from "./handles.ts";

export type SaveOutcome =
  | { kind: "overwritten"; handle: FileSystemFileHandle }
  | { kind: "saved-as"; handle: FileSystemFileHandle }
  | { kind: "downloaded"; name: string }
  | { kind: "cancelled" };

async function writeTo(handle: FileSystemFileHandle, blob: Blob): Promise<void> {
  const w = await handle.createWritable();
  try {
    await w.write(blob);
  } catch (e) {
    await w.abort();
    throw e;
  }
  await w.close(); // atomic: Chrome writes to a swap file and renames on close
}

export async function saveBlob(
  r: SaveResult,
  opts: { handle?: FileSystemFileHandle; forcePicker?: boolean },
): Promise<SaveOutcome> {
  if (opts.handle && r.replacesOriginal && !opts.forcePicker) {
    if (await ensurePermission(opts.handle, "readwrite")) {
      await writeTo(opts.handle, r.blob);
      return { kind: "overwritten", handle: opts.handle };
    }
  }
  if ("showSaveFilePicker" in window) {
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: r.suggestedName,
        types: r.types,
        ...(opts.handle ? { startIn: opts.handle } : {}),
      });
      await writeTo(handle, r.blob);
      return { kind: "saved-as", handle };
    } catch (e) {
      if ((e as DOMException).name === "AbortError") return { kind: "cancelled" };
      throw e;
    }
  }
  downloadBlob(r.blob, r.suggestedName);
  return { kind: "downloaded", name: r.suggestedName };
}

export function downloadBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
