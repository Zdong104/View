// Run unit tests (test/unit/**/*.test.ts) with node:test. They are bundled with
// esbuild first, because not every Node 22 build can strip TypeScript types.
import * as esbuild from "esbuild";
import { spawnSync } from "node:child_process";
import { readdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = join(root, "test/unit");
const out = join(root, ".cache", process.env.VIEW_DIST ? `unit-${process.env.VIEW_DIST}` : "unit");
const tests = readdirSync(src, { recursive: true })
  .filter((f) => String(f).endsWith(".test.ts"))
  .map((f) => join(src, String(f)));

rmSync(out, { recursive: true, force: true });
await esbuild.build({
  entryPoints: tests,
  outdir: out,
  outbase: src,
  outExtension: { ".js": ".mjs" },
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node22",
  packages: "external",
  sourcemap: "inline",
  logLevel: "warning",
});
const files = readdirSync(out, { recursive: true }).filter((f) => String(f).endsWith(".mjs")).map((f) => join(out, String(f)));
const r = spawnSync(process.execPath, ["--enable-source-maps", "--test", ...files], { stdio: "inherit" });
process.exit(r.status ?? 1);
