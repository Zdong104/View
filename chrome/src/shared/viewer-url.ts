// The viewer page URL format, shared by the DNR rules (service worker) and the
// shell that parses it.
//
//   viewer.html                         empty window ("Open file…", drag & drop)
//   viewer.html?kind=pdf&url=<RAW URL>  open a web or file:// URL
//   viewer.html?handle=<id>             open a FileSystemFileHandle stashed in IndexedDB
//   viewer.html?kind=pdf&transfer=<nonce>&name=<enc>
//                                       bytes arrive by postMessage from the embedding
//                                       page's content script (blob:/data: documents,
//                                       which the extension cannot fetch itself)
//
// `url=` is always LAST and NOT percent-encoded: declarativeNetRequest's
// regexSubstitution can only paste the matched URL verbatim ("\0"), so the shell
// takes everything after "url=" literally. The original fragment (#page=3) ends
// up in location.hash and is passed to the viewer as-is.
import type { Kind } from "./kinds.ts";

export const VIEWER_PATH = "viewer.html";

export function viewerUrl(base: string, target: string, kind?: Kind | null): string {
  const hashAt = target.indexOf("#");
  const bare = hashAt >= 0 ? target.slice(0, hashAt) : target;
  const hash = hashAt >= 0 ? target.slice(hashAt) : "";
  return `${base}?${kind ? `kind=${kind}&` : ""}url=${bare}${hash}`;
}

/** The regexSubstitution used by DNR redirect rules for one kind. */
export function dnrSubstitution(base: string, kind: Kind): string {
  return `${base}?kind=${kind}&url=\\0`;
}

/** Viewer URL for the postMessage transfer handshake (see src/content/transfer.ts). */
export function transferViewerUrl(base: string, nonce: string, name: string, kind: Kind): string {
  return `${base}?kind=${kind}&transfer=${encodeURIComponent(nonce)}&name=${encodeURIComponent(name)}`;
}

export interface ViewerParams {
  url?: string;
  kind?: Kind;
  handle?: string;
  transfer?: string;
  name?: string;
  hash: string;
}

export function parseViewerUrl(href: string): ViewerParams {
  const hashAt = href.indexOf("#");
  const noHash = hashAt >= 0 ? href.slice(0, hashAt) : href;
  const hash = hashAt >= 0 ? href.slice(hashAt) : "";
  const q = noHash.indexOf("?");
  if (q < 0) return { hash };
  const query = noHash.slice(q + 1);
  const out: ViewerParams = { hash };
  const urlAt = query.startsWith("url=") ? 0 : query.indexOf("&url=");
  const head = urlAt < 0 ? query : query.slice(0, urlAt);
  if (urlAt >= 0) out.url = query.slice(urlAt === 0 ? 4 : urlAt + 5);
  // Hand-made links (bookmarks, other tools) may percent-encode the whole URL;
  // a raw one never has an encoded colon right after its scheme.
  if (out.url && /^[a-z][a-z0-9+.-]*%3A/i.test(out.url)) {
    try { out.url = decodeURIComponent(out.url); } catch { /* keep it raw */ }
  }
  for (const part of head.split("&")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    const key = part.slice(0, eq);
    const val = decodeURIComponent(part.slice(eq + 1));
    if (key === "kind") out.kind = val as Kind;
    else if (key === "handle") out.handle = val;
    else if (key === "transfer") out.transfer = val;
    else if (key === "name") out.name = val;
  }
  return out;
}
