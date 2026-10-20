// Multi-file outputs (batch convert): write each file into a folder the user picks
// with showDirectoryPicker(), never overwriting ("name 2.png", like
// convert.unique_path). Without a directory picker the files are zipped (fflate)
// and handed to the shell's save path by the caller.
import { zipSync } from "fflate";

export interface OutFile { name: string; blob: Blob }

const split = (name: string): [string, string] => {
  const i = name.lastIndexOf(".");
  return i > 0 ? [name.slice(0, i), name.slice(i)] : [name, ""];
};

/** `name`, or "stem 2.ext", "stem 3.ext"… when `taken(name)` says it exists. */
export async function uniqueName(name: string, taken: (n: string) => boolean | Promise<boolean>): Promise<string> {
  if (!(await taken(name))) return name;
  const [stem, ext] = split(name);
  for (let n = 2; ; n++) {
    const cand = `${stem} ${n}${ext}`;
    if (!(await taken(cand))) return cand;
  }
}

async function exists(dir: FileSystemDirectoryHandle, name: string): Promise<boolean> {
  try {
    await dir.getFileHandle(name);
    return true;
  } catch {
    return false;
  }
}

/** Write `blob` into `dir` under a unique name; returns the name used. */
export async function writeInto(dir: FileSystemDirectoryHandle, name: string, blob: Blob, used = new Set<string>()): Promise<string> {
  const final = await uniqueName(name, async (n) => used.has(n) || (await exists(dir, n)));
  used.add(final);
  const h = await dir.getFileHandle(final, { create: true });
  const w = await h.createWritable();
  try {
    await w.write(blob);
  } catch (e) {
    await w.abort();
    throw e;
  }
  await w.close();
  return final;
}

/** Ask for an output folder; null when cancelled or unsupported. */
export async function pickFolder(): Promise<FileSystemDirectoryHandle | null | "unsupported"> {
  const pick = (window as { showDirectoryPicker?: (o?: object) => Promise<FileSystemDirectoryHandle> }).showDirectoryPicker;
  if (!pick) return "unsupported";
  try {
    return await pick({ mode: "readwrite", id: "view-export" });
  } catch {
    return null;
  }
}

/** One zip of `files` (stored, not recompressed: images are already compressed). */
export async function zipFiles(files: OutFile[]): Promise<Blob> {
  const entries: Record<string, [Uint8Array, { level: 0 }]> = {};
  const used = new Set<string>();
  for (const f of files) {
    const n = await uniqueName(f.name, (x) => used.has(x));
    used.add(n);
    entries[n] = [new Uint8Array(await f.blob.arrayBuffer()), { level: 0 }];
  }
  return new Blob([zipSync(entries) as BlobPart], { type: "application/zip" });
}
