// postMessage handshake that hands document bytes from a page (content script)
// to an embedded viewer frame. Used for blob: and data: PDFs: the extension
// origin cannot fetch those URLs, but the content script (running in the page's
// origin) can.
//
//   viewer  -> parent : { type: "view:transfer-ready", nonce }          (targetOrigin "*": only the nonce leaks)
//   content -> viewer : { type: "view:transfer", nonce, name, mime, buffer } (targetOrigin = extension origin,
//                                                                         buffer transferred, not copied)
// A hostile page could answer the ready message itself, but it could only make
// View show bytes the page already controls — no more power than serving a PDF.

export const READY = "view:transfer-ready";
export const DATA = "view:transfer";

export interface TransferData {
  type: typeof DATA;
  nonce: string;
  name: string;
  mime: string;
  buffer: ArrayBuffer;
}

/** Content-script side: answer the viewer's ready message with the bytes of `url`. */
export function serveTransfer(nonce: string, extensionOrigin: string, load: () => Promise<{ buffer: ArrayBuffer; mime: string; name: string }>): void {
  const onMessage = async (ev: MessageEvent) => {
    if (ev.origin !== extensionOrigin || ev.data?.type !== READY || ev.data?.nonce !== nonce) return;
    removeEventListener("message", onMessage);
    const { buffer, mime, name } = await load();
    const msg: TransferData = { type: DATA, nonce, name, mime, buffer };
    (ev.source as Window).postMessage(msg, extensionOrigin, [buffer]);
  };
  addEventListener("message", onMessage);
}

/** Viewer side: ask the parent for the bytes and wait (default 20 s). */
export function receiveTransfer(nonce: string, timeoutMs = 20000): Promise<TransferData> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { removeEventListener("message", onMessage); reject(new Error("The page did not hand over the document")); }, timeoutMs);
    const onMessage = (ev: MessageEvent) => {
      const d = ev.data as Partial<TransferData> | undefined;
      if (ev.source !== window.parent || d?.type !== DATA || d.nonce !== nonce || !(d.buffer instanceof ArrayBuffer)) return;
      clearTimeout(timer);
      removeEventListener("message", onMessage);
      resolve(d as TransferData);
    };
    addEventListener("message", onMessage);
    window.parent.postMessage({ type: READY, nonce }, "*");
  });
}
