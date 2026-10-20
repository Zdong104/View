// Signatures: vector strokes captured once, stored locally, placed as markup.
// Port of preview/signature.py + touchpad.py. Owned by stream B.
//
// Capture modes (browsers cannot read absolute touchpad coordinates — there is
// no web API for raw touchpad contacts — so the desktop's 1:1 touchpad signing
// can't be reproduced exactly):
//   "pointer"  draw with mouse / pen / touchscreen in a canvas (PointerEvents,
//              pressure + coalesced events for smooth pen ink). Default.
//   "trackpad" Pointer-Lock relative mode: the pad is locked to a capture box,
//              movementX/Y are integrated into ink; the stroke is "pen down"
//              while a mouse button / the touchpad click is held, OR (toggle)
//              while the pointer moves after pressing Space, ending on a pause
//              > 350 ms. Unadjusted movement (`unadjustedMovement: true`) turns
//              off OS acceleration where supported so shape is preserved.

import type { StrokePoints } from "../markup/api.ts";

export interface Signature {
  id: string;
  name: string;
  created: number;
  /** Aspect ratio w/h of the bounding box. */
  aspect: number;
  /** Points normalized to the bounding box: x, y in 0..1, pressure 0..1. */
  strokes: StrokePoints[];
}

export interface SignatureStore {
  list(): Promise<Signature[]>;
  add(sig: Omit<Signature, "id" | "created">): Promise<Signature>;
  remove(id: string): Promise<void>;
  /** Opens the capture dialog; resolves null if cancelled. */
  capture(opts?: { mode?: "pointer" | "trackpad" }): Promise<Signature | null>;
}
