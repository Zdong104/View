// Zip dist/ for the Chrome Web Store: view-chrome-<version>.zip next to package.json.
import { execFileSync } from "node:child_process";
import { rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { appVersion } from "./version.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const version = appVersion();
const zip = join(root, `view-chrome-${version}.zip`);
rmSync(zip, { force: true });
execFileSync("zip", ["-qr", zip, ".", "-x", "*.map"], { cwd: join(root, "dist"), stdio: "inherit" });
console.log(`wrote ${zip}`);
