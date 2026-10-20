// Entry point of viewer.html.
import { loadPrefs, onPrefsChanged, type Prefs } from "../shared/prefs.ts";
import { parseViewerUrl } from "../shared/viewer-url.ts";
import { Shell } from "./shell.ts";

function applyTheme(p: Prefs): void {
  if (p.theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = p.theme;
}

const shell = new Shell(document.body);
void loadPrefs().then(applyTheme);
onPrefsChanged(applyTheme);
void shell.start(parseViewerUrl(location.href));

// For tests and debugging from DevTools.
(globalThis as { viewShell?: Shell }).viewShell = shell;
