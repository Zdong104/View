// DocumentSource implementations: from a URL (web / file://) or a local File.
import { basename } from "../shared/kinds.ts";
import type { DocumentSource } from "../viewers/contract.ts";

export class FileAccessError extends Error {
  readonly url: string;
  constructor(url: string) {
    super("View needs “Allow access to file URLs” to open local files from the address bar.");
    this.url = url;
  }
}

export class HttpError extends Error {
  readonly status: number;
  readonly url: string;
  constructor(status: number, url: string) {
    super(`The server answered ${status} for ${url}`);
    this.status = status;
    this.url = url;
  }
}

/** filename / filename* from a Content-Disposition header. */
export function dispositionName(cd: string | null): string | null {
  if (!cd) return null;
  const star = /filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i.exec(cd);
  if (star?.[2]) {
    try { return decodeURIComponent(star[2].trim().replace(/^"|"$/g, "")); } catch { /* fall through */ }
  }
  const plain = /filename\s*=\s*("([^"]*)"|[^;]+)/i.exec(cd);
  return plain ? (plain[2] ?? plain[1] ?? "").trim() || null : null;
}

function readFileUrl(url: string): Promise<Blob> {
  // fetch() does not support file: — XHR does, for extensions with file access.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("GET", url);
    xhr.responseType = "blob";
    xhr.onload = () => (xhr.response ? resolve(xhr.response as Blob) : reject(new FileAccessError(url)));
    xhr.onerror = () => reject(new FileAccessError(url));
    xhr.send();
  });
}

class UrlSource implements DocumentSource {
  name: string;
  mime: string | null = null;
  size: number | null = null;
  lastModified: number | null = null;
  readonly streamable: boolean;
  private cached?: Promise<Blob>;

  readonly url: string;

  constructor(url: string) {
    this.url = url;
    this.name = basename(url) || "Untitled";
    this.streamable = /^https?:/i.test(url);
  }

  blob(): Promise<Blob> {
    this.cached ??= this.load();
    return this.cached;
  }

  private async load(): Promise<Blob> {
    if (/^file:/i.test(this.url)) {
      if (!(await chrome.extension.isAllowedFileSchemeAccess())) throw new FileAccessError(this.url);
      const b = await readFileUrl(this.url);
      this.size = b.size;
      return b;
    }
    // Extension pages with host permissions bypass CORS; send the user's cookies so
    // documents behind a login load like they did in the tab.
    const res = await fetch(this.url, { credentials: "include", cache: "force-cache" });
    if (!res.ok) throw new HttpError(res.status, this.url);
    this.mime = res.headers.get("content-type");
    const lm = res.headers.get("last-modified");
    this.lastModified = lm ? Date.parse(lm) : null;
    const named = dispositionName(res.headers.get("content-disposition"));
    if (named) this.name = named;
    const b = await res.blob();
    this.size = b.size;
    return b;
  }

  async head(n = 8192): Promise<Uint8Array> {
    return new Uint8Array(await (await this.blob()).slice(0, n).arrayBuffer());
  }
}

class FileSource implements DocumentSource {
  readonly name: string;
  readonly mime: string | null;
  readonly size: number;
  readonly lastModified: number;
  readonly streamable = false;
  readonly url?: string;
  readonly handle?: FileSystemFileHandle;
  private file: File;

  constructor(file: File, handle?: FileSystemFileHandle) {
    this.file = file;
    this.handle = handle;
    this.name = file.name;
    this.mime = file.type || null;
    this.size = file.size;
    this.lastModified = file.lastModified;
  }

  async blob(): Promise<Blob> { return this.file; }
  async head(n = 8192): Promise<Uint8Array> {
    return new Uint8Array(await this.file.slice(0, n).arrayBuffer());
  }
}

export const sourceFromUrl = (url: string): DocumentSource => new UrlSource(url);
export const sourceFromFile = (file: File, handle?: FileSystemFileHandle): DocumentSource =>
  new FileSource(file, handle);
export async function sourceFromHandle(handle: FileSystemFileHandle): Promise<DocumentSource> {
  return new FileSource(await handle.getFile(), handle);
}
