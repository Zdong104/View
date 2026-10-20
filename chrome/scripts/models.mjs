// OCR models are ~32 MB of binaries, so they are not committed: the build fetches
// them into src/ocr/models/ (gitignored) from the URLs RapidOCR 3.9.2 pins, and
// checks each against its SHA-256 (see src/ocr/MODELS.md). Files already present
// with the right hash are kept, so this is a no-op after the first build.
//   node scripts/models.mjs
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dir = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "ocr", "models");
const BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx";

const MODELS = [
  ["PP-OCRv6_det_small.onnx", `${BASE}/PP-OCRv6/det/PP-OCRv6_det_small.onnx`,
    "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f"],
  ["ch_ppocr_mobile_v2.0_cls_mobile.onnx", `${BASE}/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx`,
    "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"],
  ["PP-OCRv6_rec_small.onnx", `${BASE}/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx`,
    "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"],
];

const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");

export async function ensureModels() {
  for (const [name, url, sum] of MODELS) {
    const path = join(dir, name);
    if (existsSync(path) && sha256(await readFile(path)) === sum) continue;
    console.log(`fetching OCR model ${name}`);
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${name}: HTTP ${res.status} from ${url}`);
    const buf = Buffer.from(await res.arrayBuffer());
    const got = sha256(buf);
    if (got !== sum) throw new Error(`${name}: SHA-256 ${got}, expected ${sum}`);
    await writeFile(`${path}.part`, buf);
    await rename(`${path}.part`, path);
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await ensureModels();
