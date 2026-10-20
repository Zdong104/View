// Markup layer API — the contract between the markup/signature workstream
// (owner of src/markup, src/signature) and its consumers, the PDF and image
// viewers. Port of preview/markup.py. Both sides build against THIS file;
// changes need agreement from both streams.
//
// Model: annotations are vectors in *surface units*, origin top-left, y down.
//   - PDF:   one surface per page, id "p<index>" (0-based page index in the
//            ORIGINAL document), 1 unit = 1 PDF point of the page's unrotated
//            CropBox. The PDF viewer remaps surfaces when pages are reordered/
//            deleted (MarkupController.remapSurfaces).
//   - Image: one surface "img" (per frame is NOT supported: markup applies to all
//            frames of an animation), 1 unit = 1 source pixel before rotation.
// The layer draws into an overlay element the viewer positions over each
// rendered surface (SVG for vectors, so it scales without re-rasterizing).

export type Tool =
  | "select" | "sketch" | "draw" // sketch = shape-recognizing ink (macOS), draw = raw ink
  | "line" | "arrow" | "rect" | "rrect" | "oval" | "bubble" | "star" // preview/markup.py SHAPES
  | "text" | "note"
  | "highlight" | "underline" | "strike" // text markup: snaps to TextRuns when available
  | "signature";

export interface MarkupStyle {
  stroke: string;        // CSS colour
  fill: string | null;
  width: number;         // surface units
  opacity: number;       // 0..1
  font?: { family: string; size: number; bold?: boolean; italic?: boolean; align?: "left" | "center" | "right" };
}

/** x, y, pressure (0..1, 0.5 when unknown) per point, flattened. */
export type StrokePoints = number[];

interface Base {
  id: string;
  surface: string;
  style: MarkupStyle;
  created: number;
  modified: number;
  author?: string;
  /** Text of a pop-up comment attached to the annotation (PDF /Contents). */
  comment?: string;
}

export type Annotation =
  | (Base & { type: "ink"; strokes: StrokePoints[] })
  | (Base & { type: "rect" | "rrect" | "oval" | "bubble" | "star"; x: number; y: number; w: number; h: number })
  | (Base & { type: "line" | "arrow"; x1: number; y1: number; x2: number; y2: number })
  | (Base & { type: "text"; x: number; y: number; w: number; h: number; text: string })
  | (Base & { type: "note"; x: number; y: number; text: string })
  | (Base & { type: "highlight" | "underline" | "strike"; quads: number[]; text?: string })
  // Strokes are copied from the signature (normalized 0..1 in its box), so the document is self-contained.
  | (Base & { type: "signature"; x: number; y: number; w: number; h: number; strokes: StrokePoints[]; signatureId?: string });

export type AnnotationType = Annotation["type"];

/** A run of text on a surface (from the PDF text layer or OCR) for text markup snapping. */
export interface TextRun {
  text: string;
  /** One quad per character: x1,y1,x2,y2,x3,y3,x4,y4 (TL, TR, BR, BL) in surface units. */
  quads: number[];
}

/** Provided by the viewer for each rendered surface (page / image). */
export interface MarkupHost {
  readonly surface: string;
  /** Surface size in surface units. */
  readonly width: number;
  readonly height: number;
  /** Element exactly covering the rendered surface; the layer appends its SVG here. */
  readonly overlay: HTMLElement;
  /** CSS pixels per surface unit (the viewer's zoom). */
  scale(): number;
  /** Clockwise display rotation in degrees (0/90/180/270) applied by the viewer. */
  rotation(): number;
  textRuns?(): Promise<TextRun[]>;
}

export interface MarkupSurface {
  /** Call after zoom/rotation/layout changes. */
  relayout(): void;
  detach(): void;
}

export interface MarkupOptions {
  /** Affects defaults (e.g. text markup only on PDFs) and the toolbar contents. */
  docKind: "pdf" | "image";
  author?: string;
  /** Initial annotations (e.g. imported from the PDF). */
  initial?: Annotation[];
}

export type MarkupEvent = "change" | "selection" | "tool";

export interface MarkupController {
  attach(host: MarkupHost): MarkupSurface;
  /** The shared Markup toolbar (tools, colour, width, font, signatures). The viewer places it. */
  toolbar(): HTMLElement;

  tool: Tool;
  style: MarkupStyle;

  list(surface?: string): Annotation[];
  add(a: Omit<Annotation, "id" | "created" | "modified"> & { id?: string }): Annotation;
  update(id: string, patch: Partial<Annotation>): void;
  remove(ids: string[]): void;
  selection: string[];
  select(ids: string[]): void;

  undo(): void;
  redo(): void;
  readonly canUndo: boolean;
  readonly canRedo: boolean;

  /** Page reorder/delete support: map old surface id -> new id (null = deleted). */
  remapSurfaces(map: Record<string, string | null>): void;

  on(ev: MarkupEvent, cb: () => void): () => void;
  readonly dirty: boolean;
  markSaved(): void;

  toJSON(): Annotation[];
  /** Draw annotations of a surface onto a 2D canvas (image flattening, thumbnails, export). */
  render(ctx: CanvasRenderingContext2D, surface: string, unitsToPx: number): void;
  destroy(): void;
}

/**
 * Writing annotations into a PDF (implemented by the markup stream in
 * src/markup/pdf-export.ts, called by the PDF viewer's save()).
 * `doc` is an @cantoo/pdf-lib PDFDocument. With flatten=false the result uses
 * real PDF annotations (/Ink, /Square, /Circle, /Line, /Polygon, /FreeText,
 * /Text, /Highlight, /Underline, /StrikeOut) with appearance streams so every
 * reader shows them. Like the desktop (markup.py ANNOT_KINDS), notes and text
 * markup are always real annotations; shapes/ink/text boxes may be flattened;
 * signatures are always drawn into page content (vector paths).
 */
export type WritePdfAnnotations = (
  doc: import("@cantoo/pdf-lib").PDFDocument,
  annotations: Annotation[],
  pageIndexOf: (surface: string) => number | null,
  opts: { flatten: boolean },
) => Promise<void>;
