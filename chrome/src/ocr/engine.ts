// Worker side of the OCR service: loads onnxruntime-web (WASM backend) and the
// three PP-OCR models from the package, decodes the image and runs the
// pipeline. onnxruntime is loaded from vendor/ort/ at run time rather than
// bundled, so its threaded WASM glue can start its pthread workers from its
// own URL (extension pages may share memory with their workers even without
// cross-origin isolation, so multi-threaded WASM works here).
import type * as Ort from "onnxruntime-web";
import { onnxMetadata } from "./onnx-meta.ts";
import { fromRGBA, type Img } from "./imgproc.ts";
import { DESKTOP, runPipeline, type Models, type PipelineOptions, type Tensor, type Timings } from "./pipeline.ts";
import type { OcrResult } from "./api.ts";

export const MODEL_FILES = {
  det: "PP-OCRv6_det_small.onnx",
  cls: "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
  rec: "PP-OCRv6_rec_small.onnx",
} as const;

export interface EngineConfig {
  /** Extension root URL (chrome.runtime.getURL("")). */
  base: string;
  threads?: number;
  options?: Partial<PipelineOptions>;
}

export class Engine {
  private models: Models | null = null;
  private loading: Promise<Models> | null = null;
  private opts: PipelineOptions;
  private readonly cfg: EngineConfig;

  constructor(cfg: EngineConfig) {
    this.cfg = cfg;
    this.opts = { ...DESKTOP, ...cfg.options };
  }

  load(): Promise<Models> {
    this.loading ??= this.doLoad().then((m) => (this.models = m));
    return this.loading;
  }

  get ready() {
    return this.models !== null;
  }

  private async doLoad(): Promise<Models> {
    const ortBase = new URL("vendor/ort/", this.cfg.base).href;
    const modelBase = new URL("ocr/models/", this.cfg.base).href;
    const ort = (await import(/* webpackIgnore: true */ ortBase + "ort.wasm.min.mjs")) as typeof Ort;
    ort.env.wasm.wasmPaths = ortBase;
    ort.env.wasm.numThreads = this.cfg.threads ?? defaultThreads();
    ort.env.wasm.proxy = false;
    ort.env.logLevel = "error";

    const fetchBytes = async (name: string) => {
      const res = await fetch(modelBase + name);
      if (!res.ok) throw new Error(`OCR model ${name}: HTTP ${res.status}`);
      return new Uint8Array(await res.arrayBuffer());
    };
    const so: Ort.InferenceSession.SessionOptions = { executionProviders: ["wasm"], graphOptimizationLevel: "all" };
    const [detB, clsB, recB] = await Promise.all([fetchBytes(MODEL_FILES.det), fetchBytes(MODEL_FILES.cls), fetchBytes(MODEL_FILES.rec)]);
    const characters = onnxMetadata(recB)["character"];
    if (!characters) throw new Error("OCR recognizer has no character dictionary");
    // Sessions are created one after another: WASM session creation is not concurrent anyway.
    const det = await ort.InferenceSession.create(detB, so);
    const cls = await ort.InferenceSession.create(clsB, so);
    const rec = await ort.InferenceSession.create(recB, so);

    const runner = (s: Ort.InferenceSession) => async (input: Float32Array, dims: number[]): Promise<Tensor> => {
      const feeds = { [s.inputNames[0]!]: new ort.Tensor("float32", input, dims) };
      const out = await s.run(feeds);
      const t = out[s.outputNames[0]!]!;
      const res = { data: t.data as Float32Array, dims: t.dims };
      for (const v of Object.values(out)) v.dispose?.();
      return res;
    };
    return { det: runner(det), cls: runner(cls), rec: runner(rec), characters };
  }

  async recognize(image: ImageBitmap | ImageData | Blob, check: () => void): Promise<{ result: OcrResult; timings: Timings }> {
    const models = await this.load();
    check();
    return runPipeline(await toImg(image), models, check, this.opts);
  }
}

/**
 * preview/ocr.py uses max(2, min(8, cpu_count // 2)); in WASM more than 4
 * threads measured no faster (and often slower) on a 16-thread laptop CPU.
 */
function defaultThreads(): number {
  const n = (typeof navigator !== "undefined" && navigator.hardwareConcurrency) || 4;
  return Math.max(2, Math.min(4, Math.floor(n / 2)));
}

/** Decode to BGR pixels; blobs are decoded with their EXIF orientation applied (what a viewer shows). */
async function toImg(image: ImageBitmap | ImageData | Blob): Promise<Img> {
  if (typeof ImageData !== "undefined" && image instanceof ImageData) return fromRGBA(image.data, image.width, image.height);
  const owned = image instanceof Blob;
  const bmp: ImageBitmap = owned ? await createImageBitmap(image, { imageOrientation: "from-image" }) : (image as ImageBitmap);
  try {
    const canvas = new OffscreenCanvas(bmp.width, bmp.height);
    const g = canvas.getContext("2d", { willReadFrequently: true })!;
    g.drawImage(bmp, 0, 0);
    const data = g.getImageData(0, 0, bmp.width, bmp.height);
    return fromRGBA(data.data, bmp.width, bmp.height);
  } finally {
    if (owned) bmp.close();
  }
}
