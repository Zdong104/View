// Markup layer entry point (loaded lazily by the shell's services.markup()).
// See api.ts for the contract and controller.ts for the implementation.
import type { MarkupController, MarkupOptions } from "./api.ts";
import { Markup } from "./controller.ts";

export function createMarkup(opts: MarkupOptions): MarkupController {
  return new Markup(opts);
}
