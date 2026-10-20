// Service worker: owns the interception rules, the toolbar button, the context
// menu and the file:// fallback. Everything here must be registered
// synchronously at top level (MV3 service workers are restarted on demand).
import { kindFromName } from "../shared/kinds.ts";
import type { SwMessage } from "../shared/messages.ts";
import { loadPrefs, onPrefsChanged, type Prefs } from "../shared/prefs.ts";
import { VIEWER_PATH, viewerUrl } from "../shared/viewer-url.ts";
import { openOriginal } from "./bypass.ts";
import { prepareFetch, rememberReferer } from "./referer.ts";
import { buildRules } from "./rules.ts";

const VIEWER = chrome.runtime.getURL(VIEWER_PATH);

// ---- interception rules ------------------------------------------------------

let syncing: Promise<number> = Promise.resolve(0);

/** Replace all dynamic rules with the ones derived from prefs. Serialized. */
function syncRules(prefs?: Prefs): Promise<number> {
  syncing = syncing.catch(() => 0).then(async () => {
    const p = prefs ?? (await loadPrefs());
    const rules = buildRules(p, VIEWER);
    const old = await chrome.declarativeNetRequest.getDynamicRules();
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: old.map((r) => r.id),
      addRules: rules as unknown as chrome.declarativeNetRequest.Rule[],
    });
    return rules.length;
  });
  syncing.catch((e) => console.error("View: failed to register interception rules", e));
  return syncing;
}

chrome.runtime.onInstalled.addListener(() => {
  void syncRules();
  createMenus();
});
onPrefsChanged((p) => void syncRules(p));

// onInstalled does not fire in every case where rules must exist (e.g. incognito
// "split" mode), so make sure they were synced once per browser session.
void chrome.storage.session.get("rulesSynced").then(async (s) => {
  if (s.rulesSynced) return;
  await syncRules();
  await chrome.storage.session.set({ rulesSynced: true });
});

// ---- file:// without "Allow access to file URLs" --------------------------------
// DNR cannot see file:// navigations then, but webNavigation can. Replace the tab
// with the viewer, which explains how to grant access (or offers "Open file…").

chrome.webNavigation.onBeforeNavigate.addListener(
  (d) => {
    if (d.frameId !== 0) return;
    const kind = kindFromName(d.url);
    if (!kind) return;
    void (async () => {
      if (await chrome.extension.isAllowedFileSchemeAccess()) return; // DNR handles it
      const prefs = await loadPrefs();
      if (!prefs.intercept[kind]) return;
      await chrome.tabs.update(d.tabId, { url: viewerUrl(VIEWER, d.url, kind) });
    })();
  },
  { url: [{ schemes: ["file"] }] },
);

// ---- toolbar button and context menu ----------------------------------------

chrome.action.onClicked.addListener((tab) => {
  void chrome.tabs.create({ url: VIEWER, index: tab.index + 1, openerTabId: tab.id });
});

function createMenus(): void {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "open-link", title: "Open Link in View", contexts: ["link"] });
    chrome.contextMenus.create({ id: "open-src", title: "Open in View", contexts: ["image", "video", "audio"] });
  });
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const target = info.menuItemId === "open-link" ? info.linkUrl : info.srcUrl;
  if (!target) return;
  void (async () => {
    const created = await chrome.tabs.create({ url: viewerUrl(VIEWER, target), index: (tab?.index ?? -1) + 1 });
    // The link's page is the Referer the site expects (see referer.ts).
    const referer = info.frameUrl ?? info.pageUrl;
    if (created.id !== undefined && referer && /^https?:/.test(referer)) {
      await rememberReferer(created.id, 0, target.split("#", 1)[0]!, referer);
    }
  })();
});

// ---- messages from extension pages ------------------------------------------

chrome.runtime.onMessage.addListener((msg: SwMessage, sender, sendResponse) => {
  // Only our own pages may talk to the service worker.
  if (sender.id !== chrome.runtime.id || !sender.url?.startsWith(chrome.runtime.getURL(""))) return;
  switch (msg.type) {
    case "open-extension-settings":
      void chrome.tabs.create({ url: `chrome://extensions/?id=${chrome.runtime.id}` });
      return;
    case "open-original":
      void openOriginal(sender.tab?.id !== undefined && sender.frameId === 0 ? sender.tab.id : null, msg.url);
      return;
    case "prepare-fetch":
      if (sender.tab?.id === undefined) { sendResponse(null); return; }
      prepareFetch(sender.tab.id, sender.frameId ?? 0, msg.url).then(sendResponse, () => sendResponse(null));
      return true;
    case "sync-rules":
      syncRules().then(sendResponse, (e) => sendResponse({ error: String(e) }));
      return true; // async response
  }
});
