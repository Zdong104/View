// Referer preservation (port of pdf.js extensions/chromium/preserve-referer.js).
//
// When DNR redirects a navigation to the viewer, the viewer re-requests the
// document from the extension origin, without the page's Referer. Sites that
// check it (hot-link protection, some paywalls/CDNs) then answer 403.
//   1. webRequest.onSendHeaders remembers the Referer of main/sub-frame requests
//      (in memory, by requestId; only for the life of the request).
//   2. webRequest.onBeforeRedirect sees DNR's redirect to viewer.html and stores
//      {url, referer} for that tab+frame in storage.session (survives SW restarts).
//   3. Before fetching, the viewer sends "prepare-fetch"; we add a session DNR
//      modifyHeaders rule that sets Referer on requests to exactly that URL, made
//      by this extension, in that tab. Rules are dropped when the tab closes.
import { VIEWER_PATH } from "../shared/viewer-url.ts";

const VIEWER = chrome.runtime.getURL(VIEWER_PATH);
const RULE_BASE = 2_000_000;
const pending = new Map<string, string>(); // requestId -> Referer
const filter: chrome.webRequest.RequestFilter = {
  urls: ["http://*/*", "https://*/*"],
  types: ["main_frame", "sub_frame"] as chrome.webRequest.ResourceType[],
};

const key = (tabId: number, frameId: number) => `ref:${tabId}:${frameId}`;

interface RefEntry { url: string; referer: string }

chrome.webRequest.onSendHeaders.addListener((d) => {
  const ref = d.requestHeaders?.find((h) => h.name.toLowerCase() === "referer")?.value;
  if (ref) pending.set(d.requestId, ref);
}, filter, ["requestHeaders", "extraHeaders"] as chrome.webRequest.OnSendHeadersOptions[]);

chrome.webRequest.onBeforeRedirect.addListener((d) => {
  const ref = pending.get(d.requestId);
  pending.delete(d.requestId);
  if (ref && d.redirectUrl.startsWith(VIEWER) && d.tabId >= 0) {
    void rememberReferer(d.tabId, d.frameId, d.url, ref);
  }
}, filter);

const forget = (d: { requestId: string }) => void pending.delete(d.requestId);
chrome.webRequest.onCompleted.addListener(forget, filter);
chrome.webRequest.onErrorOccurred.addListener(forget, filter);

export async function rememberReferer(tabId: number, frameId: number, url: string, referer: string): Promise<void> {
  const entry: RefEntry = { url, referer };
  await chrome.storage.session.set({ [key(tabId, frameId)]: entry });
}

/**
 * Called (via message) by a viewer page right before it fetches `url`. Installs the
 * Referer rule if this tab+frame was redirected from `url`. Resolves with the
 * Referer used, or null.
 */
export async function prepareFetch(tabId: number, frameId: number, url: string): Promise<string | null> {
  const k = key(tabId, frameId);
  const entry = (await chrome.storage.session.get(k))[k] as RefEntry | undefined;
  if (!entry || entry.url !== url.split("#", 1)[0]) return null;
  const s = await chrome.storage.session.get(["refNextId", `refRules:${tabId}`]);
  const id = RULE_BASE + ((s.refNextId as number | undefined) ?? 0) % 1_000_000;
  const ids = [...((s[`refRules:${tabId}`] as number[] | undefined) ?? []), id];
  await chrome.storage.session.set({ refNextId: id - RULE_BASE + 1, [`refRules:${tabId}`]: ids });
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [id],
    addRules: [{
      id,
      priority: 1,
      action: {
        type: "modifyHeaders" as chrome.declarativeNetRequest.RuleActionType,
        requestHeaders: [{
          header: "referer",
          operation: "set" as chrome.declarativeNetRequest.HeaderOperation,
          value: entry.referer,
        }],
      },
      condition: {
        urlFilter: `|${entry.url}|`,
        initiatorDomains: [chrome.runtime.id],
        tabIds: [tabId],
        resourceTypes: ["xmlhttprequest", "media", "image", "other"] as chrome.declarativeNetRequest.ResourceType[],
      },
    }],
  });
  return entry.referer;
}

chrome.tabs.onRemoved.addListener((tabId) => {
  void (async () => {
    const rk = `refRules:${tabId}`;
    const s = await chrome.storage.session.get(rk);
    const ids = (s[rk] as number[] | undefined) ?? [];
    if (ids.length) await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: ids });
    const all = await chrome.storage.session.get(null);
    await chrome.storage.session.remove([rk, ...Object.keys(all).filter((k) => k.startsWith(`ref:${tabId}:`))]);
  })();
});
