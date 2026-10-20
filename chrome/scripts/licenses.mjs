// Fail if any package that ships in the extension (production dependency tree)
// is not permissively licensed. View is PolyForm-Noncommercial, so GPL/AGPL/LGPL
// code (e.g. mupdf.js, libheif, ffmpeg.wasm) must never be bundled.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ALLOWED = new Set(["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Zlib",
  "BlueOak-1.0.0", "CC0-1.0", "Unlicense", "Python-2.0", "MIT-0"]);

const tree = JSON.parse(execFileSync("npm", ["ls", "--omit=dev", "--all", "--json", "--long"], {
  encoding: "utf8", maxBuffer: 64 << 20,
}));
const bad = [];
const seen = new Set();
(function walk(deps = {}) {
  for (const [name, d] of Object.entries(deps)) {
    if (!d.version) continue; // optional platform package that is not installed (e.g. pdfjs-dist's node canvas)
    const key = `${name}@${d.version}`;
    if (seen.has(key)) continue;
    seen.add(key);
    let lic = d.license;
    if (!lic && d.path) {
      try { lic = JSON.parse(readFileSync(join(d.path, "package.json"), "utf8")).license; } catch { /* ignore */ }
    }
    // SPDX: "A OR B" needs one allowed choice; "A AND B" needs every part allowed.
    const expr = String(typeof lic === "object" ? lic?.type : lic ?? "UNKNOWN").replace(/[()]/g, "");
    const ok = expr.split(/\s+OR\s+/).some((alt) => alt.split(/\s+AND\s+/).every((id) => ALLOWED.has(id.trim())));
    if (!ok) bad.push(`${key}: ${lic ?? "UNKNOWN"}`);
    walk(d.dependencies);
  }
})(tree.dependencies);

if (bad.length) {
  console.error("Non-permissive or unknown licenses in production dependencies:\n  " + bad.join("\n  "));
  process.exit(1);
}
console.log(`licenses ok: ${seen.size} production packages, all permissive`);
