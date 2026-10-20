// Build the unpacked extension into dist/.
//
// Conventions (so workstreams never need to edit this file):
//   src/viewers/<kind>/style.css   -> dist/viewers/<kind>.css (load with ui/kit loadStyle)
//   src/**/copy.json               -> [{from, to}] files/dirs copied into dist/ (vendor wasm,
//                                     workers, models). `from` is relative to chrome/, `to` to dist/.
//   src/**/dev/<name>.html + .ts   -> dist/dev/<area>-<name>.{html,js}, dev builds only:
//                                     standalone test pages for e2e tests of a module.
//   src/**/<name>.worker.ts        -> dist/<same path>/<name>.worker.js, a self-contained ESM
//                                     bundle for `new Worker(chrome.runtime.getURL(
//                                     "viewers/x/foo.worker.js"), { type: "module" })`.
//   node scripts/build.mjs            development build (sourcemaps)
//   node scripts/build.mjs --release  minified, no sourcemaps (for the Web Store zip)
//   node scripts/build.mjs --watch    rebuild on change
import * as esbuild from "esbuild";
import { cp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { basename, dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { ensureModels } from "./models.mjs";
import { appVersion } from "./version.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
// VIEW_DIST lets parallel checkouts/agents build into their own folder.
const out = process.env.VIEW_DIST ? join(root, process.env.VIEW_DIST) : join(root, "dist");
const release = process.argv.includes("--release");
const watch = process.argv.includes("--watch");
const r = (...p) => join(root, ...p);

async function findFiles(dir, pred) {
  const out = [];
  for (const e of await readdir(dir, { withFileTypes: true, recursive: true })) {
    if (e.isFile() && pred(e.name, e.parentPath)) out.push(join(e.parentPath, e.name));
  }
  return out;
}

/** src/<area...>/dev/<name>.ts -> { in, out: "dev/<area>-<name>" } (dev builds only). */
async function devEntries() {
  if (release) return [];
  const files = await findFiles(r("src"), (n, p) => n.endsWith(".ts") && !n.endsWith(".d.ts") && p.endsWith("/dev"));
  return files.map((f) => {
    const area = relative(r("src"), dirname(dirname(f))).replaceAll("/", "-");
    return { in: f, out: `dev/${area}-${basename(f, ".ts")}` };
  });
}

async function viewerStyleEntries() {
  const dirs = await readdir(r("src/viewers"), { withFileTypes: true });
  return dirs
    .filter((d) => d.isDirectory() && existsSync(r("src/viewers", d.name, "style.css")))
    .map((d) => ({ in: r("src/viewers", d.name, "style.css"), out: `viewers/${d.name}` }));
}

const common = {
  bundle: true,
  format: "esm",
  target: "chrome128",
  minify: release,
  sourcemap: release ? false : "linked",
  legalComments: "linked", // keeps third-party license headers in *.LEGAL.txt
  logLevel: "info",
  loader: { ".svg": "file", ".png": "file", ".gif": "file", ".woff2": "file", ".ttf": "file" },
  assetNames: "assets/[name]-[hash]",
  define: { "process.env.NODE_ENV": JSON.stringify(release ? "production" : "development") },
};

/** Extension pages: one entry per page, shared chunks, lazily loaded viewers. */
const pages = async () => ({
  ...common,
  entryPoints: [
    { in: r("src/shell/main.ts"), out: "viewer" },
    { in: r("src/shell/viewer.css"), out: "viewer" },
    { in: r("src/options/options.ts"), out: "options" },
    { in: r("src/options/options.css"), out: "options" },
    ...(await viewerStyleEntries()),
    ...(await devEntries()),
  ],
  outdir: out,
  splitting: true,
  chunkNames: "chunks/[name]-[hash]",
});

/** The service worker must be a single file (no dynamic import() in MV3 workers). */
const worker = {
  ...common,
  entryPoints: [{ in: r("src/background/sw.ts"), out: "sw" }],
  outdir: out,
  splitting: false,
};

// src/**/<name>.worker.ts -> <same path>.worker.js: one self-contained module per worker.
async function workerEntries() {
  const files = await findFiles(r("src"), (n) => n.endsWith(".worker.ts"));
  return files.map((f) => ({ in: f, out: relative(r("src"), f).replace(/\.ts$/, "") }));
}

const workers = async () => ({
  ...common,
  entryPoints: await workerEntries(),
  outdir: out,
  splitting: false,
});

/** Content scripts are classic scripts (no ES modules): one self-contained IIFE. */
const content = {
  ...common,
  format: "iife",
  entryPoints: [{ in: r("src/content/detect.ts"), out: "content" }],
  outdir: out,
  splitting: false,
};

async function copyStatic() {
  const manifest = JSON.parse(await readFile(r("static/manifest.json"), "utf8"));
  manifest.version = appVersion();
  await writeFile(join(out, "manifest.json"), JSON.stringify(manifest, null, 2));
  await cp(r("static/icons"), join(out, "icons"), { recursive: true });
  await cp(r("src/shell/viewer.html"), join(out, "viewer.html"));
  await cp(r("src/options/options.html"), join(out, "options.html"));
  await cp(r("src/content/content.css"), join(out, "content.css"));
  // Runtime files that must ship as package files (MV3: no remote code): workers,
  // wasm, models, fonts. E.g. src/viewers/pdf/copy.json copies pdf.js's worker,
  // wasm decoders, CMaps, standard fonts and ICC profiles to dist/vendor/pdfjs/.
  for (const manifestFile of await findFiles(r("src"), (n) => n === "copy.json")) {
    for (const { from, to } of JSON.parse(await readFile(manifestFile, "utf8"))) {
      await mkdir(dirname(join(out, to)), { recursive: true });
      await cp(r(from), join(out, to), { recursive: true });
    }
  }
  if (!release) {
    for (const html of await findFiles(r("src"), (n, p) => n.endsWith(".html") && p.endsWith("/dev"))) {
      const area = relative(r("src"), dirname(dirname(html))).replaceAll("/", "-");
      await mkdir(join(out, "dev"), { recursive: true });
      await cp(html, join(out, "dev", `${area}-${basename(html)}`));
    }
  }
}

await ensureModels();  // OCR models are fetched, not committed
await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });
if (watch) {
  const a = await esbuild.context(await pages());
  const b = await esbuild.context(worker);
  const c = await esbuild.context(content);
  const w = await workers();
  const d = w.entryPoints.length ? await esbuild.context(w) : null;
  await copyStatic();
  await Promise.all([a.watch(), b.watch(), c.watch(), d?.watch()]);
  console.log("watching… (static files are copied once; re-run for manifest/html changes)");
} else {
  const w = await workers();
  await Promise.all([esbuild.build(await pages()), esbuild.build(worker), esbuild.build(content),
    w.entryPoints.length ? esbuild.build(w) : null]);
  await copyStatic();
  console.log(`built ${release ? "release" : "dev"} extension -> ${out}`);
}
