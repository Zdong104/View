# OCR models and runtime (stream F)

The Live Text engine ships the same three PP-OCR ONNX models as the desktop app
(`preview/ocr.py` runs RapidOCR 3.9.2, whose wheel bundles exactly these files).
They are not committed: `scripts/models.mjs` (run by every build) downloads them
from the URLs below into `src/ocr/models/` and checks each SHA-256, which match
the ones RapidOCR 3.9.2 pins in its `default_models.yaml`.

| File (`src/ocr/models/`, shipped as `ocr/models/`) | Role | Bytes | SHA-256 | Source URL |
|---|---|---|---|---|
| `PP-OCRv6_det_small.onnx` | DB text detection | 9,929,594 | `090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f` | https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/det/PP-OCRv6_det_small.onnx |
| `ch_ppocr_mobile_v2.0_cls_mobile.onnx` | 0/180 direction classifier | 585,532 | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` | https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx |
| `PP-OCRv6_rec_small.onnx` | SVTR/CTC recognition (Chinese + English, 18,708-entry dictionary in the ONNX metadata key `character`) | 21,234,383 | `6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884` | https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx |

**License**: Apache-2.0. The models are PaddleOCR's (https://github.com/PaddlePaddle/PaddleOCR,
Apache-2.0), converted to ONNX and distributed by RapidOCR (https://github.com/RapidAI/RapidOCR,
Apache-2.0). `models/NOTICE.txt` ships next to them.

**Runtime**: `onnxruntime-web` 1.30 (MIT), WASM backend only. `copy.json` ships
`ort.wasm.min.mjs`, its glue `ort-wasm-simd-threaded.mjs` and
`ort-wasm-simd-threaded.wasm` to `vendor/ort/` (14.3 MB). ORT is imported from
that URL at run time inside the OCR worker (not bundled) so the WASM pthread
workers can start from the glue's own URL.

**Total added to the package**: 46.1 MB unpacked (models 31.7 MB + ORT 14.3 MB).

To update: point `MODELS` in `scripts/models.mjs` at the new release's URLs and
sums, update `MODEL_FILES` in `engine.ts` and this table, and re-run
`test/e2e/f.test.mjs`.
