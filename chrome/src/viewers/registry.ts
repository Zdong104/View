// Kind -> lazily imported viewer module. esbuild code-splits each import() into
// its own chunk, so opening an image never loads pdf.js or three.js.
// Each workstream owns its own src/viewers/<kind>/ directory; this file only
// changes when a new kind is added.
import type { Kind } from "../shared/kinds.ts";
import type { ViewerModule } from "./contract.ts";

export const REGISTRY: Record<Kind, () => Promise<ViewerModule>> = {
  pdf: () => import("./pdf/index.ts").then((m) => m.default),
  image: () => import("./image/index.ts").then((m) => m.default),
  model: () => import("./model/index.ts").then((m) => m.default),
  table: () => import("./table/index.ts").then((m) => m.default),
  text: () => import("./text/index.ts").then((m) => m.default),
  media: () => import("./media/index.ts").then((m) => m.default),
};
