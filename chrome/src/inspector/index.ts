// Get Info (Inspector) panel. Port of preview/inspector.py + viewer_base.file_info_section.
// The shell shows fileInfoSection() followed by the viewer's getInfo() sections.
// Viewer-specific sections (PDF metadata, EXIF/GPS, mesh stats) are produced by
// each viewer; EXIF parsing (exifr) lives in the image stream.
import { extOf } from "../shared/kinds.ts";
import { el, humanSize } from "../ui/kit.ts";
import type { DocumentSource, InfoSection } from "../viewers/contract.ts";

const fmtDate = (t: number | null) => (t ? new Date(t).toLocaleString() : "—");

export function fileInfoSection(src: DocumentSource): InfoSection {
  const rows: InfoSection["rows"] = [["File name", src.name]];
  const mime = src.mime?.split(";", 1)[0];
  rows.push(["Kind", mime || (extOf(src.name) ? `${extOf(src.name).toUpperCase()} file` : "Unknown")]);
  if (src.size !== null) {
    rows.push(["Size", src.size >= 1000
      ? `${humanSize(src.size)} (${src.size.toLocaleString()} bytes)` : `${src.size} bytes`]);
  }
  if (src.url) {
    const where = src.url.startsWith("file:")
      ? decodeURIComponent(new URL(".", src.url).pathname)
      : src.url;
    rows.push(["Where", /^https?:/.test(where) ? `[${where}](${where})` : where]);
  } else if (src.handle) {
    rows.push(["Where", "Opened from this computer"]);
  }
  rows.push(["Modified", fmtDate(src.lastModified)]);
  return { title: "File", rows };
}

const LINK = /^\[([^\]]*)\]\((https?:\/\/[^)]+)\)$/;

export function renderInfo(target: HTMLElement, sections: InfoSection[]): void {
  target.replaceChildren();
  for (const s of sections) {
    target.append(el("h2", { text: s.title }));
    const dl = el("dl");
    for (const [label, value] of s.rows) {
      const m = LINK.exec(value);
      dl.append(el("dt", { text: label }),
        el("dd", {}, m ? el("a", { href: m[2], target: "_blank", rel: "noopener", text: m[1] }) : value));
    }
    target.append(dl);
  }
}
