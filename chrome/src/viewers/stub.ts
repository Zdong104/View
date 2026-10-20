// Placeholder viewer used until a workstream lands the real one. Shows which
// viewer would handle the file and proves routing + Get Info + save plumbing.
import type { Kind } from "../shared/kinds.ts";
import { el, humanSize } from "../ui/kit.ts";
import type { DocumentSource, InfoSection, Viewer, ViewerModule } from "./contract.ts";

export function stubModule(kind: Kind, label: string): ViewerModule {
  return {
    kind,
    create(): Viewer {
      let src: DocumentSource | null = null;
      let root: HTMLElement | null = null;
      return {
        async mount(container, source) {
          src = source;
          root = el("div.placeholder", { "data-viewer": kind, "data-state": "ready" },
            el("strong", { text: `${label} viewer` }),
            el("span", { text: `${source.name} — not implemented yet (stub)` }),
          );
          container.append(root);
          // Load the bytes so the pipeline (fetch, file://, handles) is exercised end to end.
          const blob = await source.blob();
          root.dataset.bytes = String(blob.size);
          root.append(el("span", { text: humanSize(blob.size) }));
        },
        getTitle: () => src?.name ?? label,
        getSubtitle: () => `${label} (stub)`,
        async getInfo(): Promise<InfoSection[]> {
          return [{ title: label, rows: [["Viewer", "stub — see chrome/ARCHITECTURE.md §7"]] }];
        },
        destroy() { root?.remove(); },
      };
    },
  };
}
