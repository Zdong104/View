// Messages from extension pages (viewer, options) to the service worker.

export type SwMessage =
  /** Open chrome://extensions for this extension so the user can allow file URLs. */
  | { type: "open-extension-settings" }
  /**
   * Show `url` with Chrome's own handling (native PDF viewer / download) in the
   * sender's tab, skipping View's interception once.
   */
  | { type: "open-original"; url: string }
  /**
   * The viewer is about to fetch `url` (http/https). Re-attaches the Referer of the
   * navigation View intercepted. Resolves with the Referer used, or null.
   */
  | { type: "prepare-fetch"; url: string }
  /** Re-sync the interception rules now (options page, tests). Resolves with the rule count. */
  | { type: "sync-rules" };

export function sendToSw<T = unknown>(msg: SwMessage): Promise<T> {
  return chrome.runtime.sendMessage(msg) as Promise<T>;
}
