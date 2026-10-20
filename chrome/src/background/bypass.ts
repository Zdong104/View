// "Open in Chrome": show a URL with Chrome's own handling once, skipping View.
// A session "allow" rule (priority above every redirect) scoped to the tab and
// the exact URL is added, the tab navigates, and the rule is removed as soon as
// that navigation commits or fails (a download ends in onErrorOccurred), so a
// later reload goes back to View. Pending bypasses live in storage.session so a
// service-worker restart in between doesn't leak the rule.
import { PRIORITY } from "./rules.ts";

const RULE_BASE = 1_000_000;
const k = (tabId: number) => `bypass:${tabId}`;

export async function openOriginal(tabId: number | null, url: string): Promise<void> {
  // From a viewer embedded in a page (iframe/embed) open the original in a new tab.
  const tab = tabId ?? (await chrome.tabs.create({ url: "about:blank" })).id!;
  const id = RULE_BASE + tab;
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [id],
    addRules: [{
      id,
      priority: PRIORITY.bypass,
      action: { type: "allow" as chrome.declarativeNetRequest.RuleActionType },
      condition: {
        urlFilter: `|${url.split("#", 1)[0]}|`,
        tabIds: [tab],
        resourceTypes: ["main_frame" as chrome.declarativeNetRequest.ResourceType],
      },
    }],
  });
  await chrome.storage.session.set({ [k(tab)]: id });
  await chrome.tabs.update(tab, { url });
}

async function clear(tabId: number): Promise<void> {
  const s = await chrome.storage.session.get(k(tabId));
  const id = s[k(tabId)] as number | undefined;
  if (id === undefined) return;
  await chrome.storage.session.remove(k(tabId));
  await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [id] });
}

const done = (d: { tabId: number; frameId: number; url: string }) => {
  if (d.frameId === 0 && !d.url.startsWith("about:")) void clear(d.tabId);
};
chrome.webNavigation.onCommitted.addListener(done);
chrome.webNavigation.onErrorOccurred.addListener(done);
chrome.tabs.onRemoved.addListener((tabId) => void clear(tabId));
