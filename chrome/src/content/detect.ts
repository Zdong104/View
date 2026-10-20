// Content script (all frames, document_start, match_origin_as_fallback).
// Catches the PDFs that declarativeNetRequest never sees:
//   1. <embed>/<object> PDFs: the resource loads as "object", not as a frame
//      navigation. Detected with a CSS-animation hook (content.css) — the same
//      trick as pdf.js extensions/chromium/contentscript.js — and rewritten to
//      show viewer.html instead of Chrome's PDF plugin.
//   2. blob: documents that Chrome renders with its PDF viewer (location =
//      blobUrl, <iframe src=blob:…>): we cover the document with a viewer frame
//      and hand it the bytes by postMessage (transfer.ts).
//   3. <iframe src="data:application/pdf…">: handled from the *parent* page,
//      because a data: document has an opaque origin and Chrome refuses to load
//      web-accessible extension pages into it. (Top-level data: navigations are
//      blocked by Chrome itself unless typed.)
// Kept tiny: it runs on every page; prefs are only read when a PDF shows up.
import { loadPrefs } from "../shared/prefs.ts";
import { transferViewerUrl, viewerUrl } from "../shared/viewer-url.ts";
import { serveTransfer } from "./transfer.ts";

const VIEWER = chrome.runtime.getURL("viewer.html");
const EXT_ORIGIN = new URL(VIEWER).origin;
const ANIMATION = "view-detect-object-or-embed";
const seen = new WeakSet<Element>();

let pdfOn: Promise<boolean> | undefined;
const pdfEnabled = () => (pdfOn ??= loadPrefs().then((p) => p.intercept.pdf, () => false));

const isLocalScheme = (u: string) => /^(blob|data):/i.test(u);

function nonce(): string {
  return crypto.randomUUID();
}

/** A viewer URL for `src`: direct for fetchable URLs, transfer handshake for blob:/data:. */
function viewerFor(src: string): string {
  if (!isLocalScheme(src)) return viewerUrl(VIEWER, src, "pdf");
  const id = nonce();
  serveTransfer(id, EXT_ORIGIN, async () => {
    const res = await fetch(src);
    return { buffer: await res.arrayBuffer(), mime: "application/pdf", name: "document.pdf" };
  });
  return transferViewerUrl(VIEWER, id, "document.pdf", "pdf");
}

function absolute(url: string): string {
  try { return new URL(url, document.baseURI).href; } catch { return url; }
}

document.addEventListener("animationstart", (ev) => {
  if (ev.animationName === ANIMATION) void onObjectOrEmbed(ev.target as Element);
}, true);

async function onObjectOrEmbed(elem: Element): Promise<void> {
  if (seen.has(elem)) return;
  if (elem instanceof HTMLIFrameElement) return onDataIframe(elem);
  const isEmbed = elem instanceof HTMLEmbedElement;
  const isObject = elem instanceof HTMLObjectElement;
  if (!isEmbed && !isObject) return;
  const type = (elem.getAttribute("type") ?? "").toLowerCase().split(";", 1)[0]!.trim();
  const raw = isEmbed ? elem.getAttribute("src") : elem.getAttribute("data");
  if (!raw) return;
  const src = absolute(raw);
  if (type ? type !== "application/pdf" : !/\.pdf(?:$|[?#])/i.test(src)) return;
  if (src.startsWith(EXT_ORIGIN)) return;
  // Chrome's own PDF viewer document is <body><embed name="plugin" src=location.href>.
  if (isEmbed && document.contentType === "application/pdf" && src === location.href) return;
  seen.add(elem);
  if (!(await pdfEnabled())) return;
  if (isEmbed) replaceEmbed(elem, src);
  else replaceObject(elem as HTMLObjectElement, src);
}

/** <iframe src="data:application/pdf…">: point the frame at the viewer instead. */
async function onDataIframe(frame: HTMLIFrameElement): Promise<void> {
  const src = frame.src;
  if (!/^data:application\/pdf[;,]/i.test(src)) return;
  seen.add(frame);
  if (!(await pdfEnabled())) return;
  frame.src = viewerFor(src);
}

/** <embed>: becomes an embedded HTML document (keeps the element for page scripts). */
function replaceEmbed(elem: HTMLEmbedElement, src: string): void {
  const parent = elem.parentNode;
  const next = elem.nextSibling;
  elem.remove();
  elem.type = "text/html";
  elem.src = viewerFor(src);
  parent?.insertBefore(elem, next);
}

/** <object>: give it a full-size iframe child and a type that disables the plugin. */
function replaceObject(elem: HTMLObjectElement, src: string): void {
  const frame = document.createElement("iframe");
  frame.style.cssText = "display:block;width:100%;height:100%;border:0;margin:0;background:none";
  frame.src = viewerFor(src);
  elem.replaceChildren(frame);
  elem.type = "application/x-view-replaced-pdf";
  elem.data += ""; // reload with the new (unsupported) type, which shows the fallback content
  elem.style.display = "inline-block";
  elem.style.padding = "0";
}

// ---- blob:/data: documents rendered by Chrome's PDF viewer ------------------------

if (document.contentType === "application/pdf" && /^blob:/i.test(location.href)) {
  void (async () => {
    if (!(await pdfEnabled())) return;
    if (document.readyState === "loading") {
      await new Promise((r) => document.addEventListener("DOMContentLoaded", r, { once: true }));
    }
    const frame = document.createElement("iframe");
    frame.style.cssText = "position:fixed;inset:0;width:100%;height:100%;border:0;margin:0;background:#fff;z-index:2147483647";
    frame.src = viewerFor(location.href);
    frame.setAttribute("allow", "fullscreen; clipboard-write");
    // A closed shadow root on <body> hides Chrome's plugin <embed> (it is no longer
    // slotted) and keeps the frame out of reach of the document's own scripts.
    try {
      document.body.attachShadow({ mode: "closed" }).append(frame);
    } catch {
      document.documentElement.append(frame);
    }
  })();
}
