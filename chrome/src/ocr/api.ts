// Live Text OCR service. Port of preview/ocr.py (RapidOCR = PaddleOCR ONNX).
// Implemented by stream F with onnxruntime-web (WASM, in a dedicated Worker)
// and PP-OCR det/cls/rec ONNX models shipped in the package (ocr/models/).

export interface OcrLine {
  text: string;
  score: number;
  /** Quad TL, TR, BR, BL in image pixels: x1,y1,...,x4,y4. */
  box: number[];
  /** Per-character quads (approximate, from CTC alignment) for text selection. */
  charQuads?: number[];
}

export interface OcrResult {
  width: number;
  height: number;
  lines: OcrLine[];
}

export interface OcrService {
  /** Is the engine loaded (models + wasm)? First call takes ~1 s to warm up. */
  readonly ready: boolean;
  recognize(image: ImageBitmap | ImageData | Blob, opts?: { signal?: AbortSignal }): Promise<OcrResult>;
  dispose(): void;
}
