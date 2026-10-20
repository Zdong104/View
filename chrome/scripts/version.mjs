// The extension ships with the same version as the desktop app: the single
// source is __version__ in ../preview/__init__.py (bumped by each release).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const init = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "preview", "__init__.py");

export function appVersion() {
  const m = readFileSync(init, "utf8").match(/^__version__\s*=\s*["']([^"']+)["']/m);
  if (!m) throw new Error(`no __version__ in ${init}`);
  // Chrome wants 1-4 dot-separated integers; drop a pre-release suffix like "-rc1".
  const v = m[1].split(/[-+]/)[0];
  if (!/^\d+(\.\d+){0,3}$/.test(v)) throw new Error(`version ${m[1]} is not valid for Chrome`);
  return v;
}
