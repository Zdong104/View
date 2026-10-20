// File-kind routing. Mirrors preview/viewer_base.py (IMAGE_EXTS, PDF_EXTS, ...)
// so the extension and the desktop app agree on what opens where.
// Pure module: no DOM, no chrome.* — unit-tested under node.

export type Kind = "pdf" | "image" | "model" | "table" | "text" | "media";
export const KINDS: readonly Kind[] = ["pdf", "image", "model", "table", "text", "media"];

export const KIND_LABELS: Record<Kind, string> = {
  pdf: "PDF documents",
  image: "Images",
  model: "3D models (STL / OBJ / PLY)",
  table: "Tables (CSV / TSV)",
  text: "Text and code",
  media: "Video and audio",
};

export const EXTS: Record<Kind, readonly string[]> = {
  pdf: ["pdf"],
  image: ["png", "jpg", "jpeg", "jpe", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif",
    "avif", "jxl", "svg", "ico", "tga", "qoi", "apng"],
  model: ["stl", "obj", "ply"],
  table: ["csv", "tsv", "tab"],
  media: ["mp4", "mov", "m4v", "webm", "mkv", "avi", "ogv", "3gp",
    "mp3", "m4a", "wav", "flac", "ogg", "opus"],
  text: ["txt", "md", "markdown", "log", "ini", "cfg", "conf", "toml", "yaml", "yml",
    "json", "jsonc", "jsonl", "ndjson", "xml", "html", "htm", "xhtml", "css",
    "scss", "sass", "less", "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue",
    "svelte", "py", "pyw", "pyi", "sh", "bash", "zsh", "fish", "ps1", "bat",
    "cmd", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "cu", "cuh",
    "java", "kt", "kts", "go", "rs", "rb", "php", "pl", "pm", "lua", "r", "m",
    "mm", "swift", "scala", "cs", "dart", "hs", "ml", "ex", "exs", "erl",
    "clj", "jl", "zig", "nim", "sql", "tex", "sty", "cls", "bib", "cmake",
    "mk", "make", "diff", "patch", "rst", "org", "srt", "vtt", "proto", "gradle",
    "tf", "nix", "vim", "asm", "s", "properties", "service", "desktop",
    "gitignore", "gitattributes", "gitmodules", "dockerignore", "editorconfig",
    "env", "dockerfile", "lock"],
};

/** Extensionless file names that are text (viewer_base.TEXT_NAMES). */
export const TEXT_NAMES = new Set([
  "makefile", "gnumakefile", "dockerfile", "containerfile", "rakefile", "gemfile",
  "procfile", "vagrantfile", "jenkinsfile", "justfile", "readme", "license", "licence",
  "copying", "authors", "changelog", "notice", "todo", "news", "install", "cmakelists.txt",
  ".bashrc", ".zshrc", ".profile", ".bash_profile", ".vimrc", ".gitconfig", ".npmrc",
]);

/**
 * MIME types that identify a kind. Used both for DNR response-header rules
 * (exact values, with and without ";*" parameters) and for the shell's sniffing.
 * Keep these specific: anything listed here is intercepted when the kind is on.
 */
export const MIMES: Record<Kind, readonly string[]> = {
  pdf: ["application/pdf", "application/x-pdf"],
  image: ["image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff",
    "image/avif", "image/x-icon", "image/vnd.microsoft.icon", "image/apng", "image/heic",
    "image/heif", "image/jxl", "image/qoi", "image/x-tga"],
  // image/svg+xml is deliberately absent: SVG documents are often interactive pages.
  model: ["model/stl", "model/x.stl-ascii", "model/x.stl-binary", "application/sla",
    "application/vnd.ms-pki.stl", "model/obj", "text/x-obj", "application/x-tgif", "model/ply",
    "application/ply"],
  table: ["text/csv", "text/tab-separated-values", "application/csv"],
  text: ["text/plain", "text/markdown", "text/x-markdown", "application/json",
    "application/x-yaml", "text/yaml", "text/x-python", "text/x-c", "application/x-sh"],
  media: ["video/mp4", "video/webm", "video/quicktime", "video/x-matroska", "video/ogg",
    "audio/mpeg", "audio/mp4", "audio/wav", "audio/x-wav", "audio/flac", "audio/ogg",
    "audio/opus", "audio/webm"],
};

export function extOf(name: string): string {
  const base = basename(name).toLowerCase();
  const dot = base.lastIndexOf(".");
  return dot >= 0 ? base.slice(dot + 1) : "";
}

/** Last path segment of a URL or path, query and fragment stripped, %-decoded. */
export function basename(nameOrUrl: string): string {
  let s = nameOrUrl.split(/[?#]/, 1)[0] ?? "";
  s = s.replace(/\/+$/, "");
  s = s.slice(s.lastIndexOf("/") + 1);
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

export function kindFromName(name: string): Kind | null {
  const base = basename(name).toLowerCase();
  const ext = extOf(base);
  for (const k of KINDS) {
    if (k !== "text" && EXTS[k].includes(ext)) return k;
  }
  if (EXTS.text.includes(ext) || TEXT_NAMES.has(base) || base.startsWith(".env")) return "text";
  return null;
}

export function kindFromMime(mime: string | null | undefined): Kind | null {
  if (!mime) return null;
  const m = mime.split(";", 1)[0]!.trim().toLowerCase();
  for (const k of KINDS) if (MIMES[k].includes(m)) return k;
  if (m.startsWith("image/") && m !== "image/svg+xml") return "image";
  if (m.startsWith("video/") || m.startsWith("audio/")) return "media";
  if (m.startsWith("model/")) return "model";
  return null;
}

/** Magic-number sniffing on the first bytes of a file. */
export function kindFromMagic(head: Uint8Array): Kind | null {
  const ascii = (off: number, s: string) =>
    head.length >= off + s.length && [...s].every((c, i) => head[off + i] === c.charCodeAt(0));
  // %PDF may be preceded by junk; the spec allows it anywhere in the first 1024 bytes.
  const scan = String.fromCharCode(...head.subarray(0, Math.min(head.length, 1024)));
  if (scan.includes("%PDF-")) return "pdf";
  if (ascii(0, "\x89PNG") || ascii(0, "GIF8") || ascii(0, "BM") ||
      (head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) ||
      (ascii(0, "RIFF") && ascii(8, "WEBP")) || ascii(0, "II*\0") || ascii(0, "MM\0*") ||
      (ascii(4, "ftyp") && (ascii(8, "avif") || ascii(8, "heic") || ascii(8, "mif1")))) {
    return "image";
  }
  if (ascii(4, "ftyp") || ascii(0, "\x1aE\xdf\xa3") || ascii(0, "OggS") || ascii(0, "ID3") ||
      ascii(0, "fLaC") || (ascii(0, "RIFF") && ascii(8, "WAVE"))) {
    return "media";
  }
  if (ascii(0, "ply\n") || ascii(0, "ply\r\n")) return "model";
  if (ascii(0, "solid ") && scan.includes("facet")) return "model";
  return null;
}

/** viewer_base.sniff_text: empty, or no NUL and valid UTF-8 in the first 8 KB. */
export function looksLikeText(head: Uint8Array): boolean {
  if (head.length === 0) return true;
  if (head.includes(0)) return false;
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(head);
    return true;
  } catch {
    // Allow a multibyte character cut at the end of the sniff window.
    try {
      new TextDecoder("utf-8", { fatal: true }).decode(head.subarray(0, head.length - 3));
      return head.length > 3;
    } catch {
      return false;
    }
  }
}

/**
 * Decide the kind of a document. Priority: magic bytes (authoritative for binary
 * formats), then the server's Content-Type, then the file name, then a hint
 * (e.g. the kind the interception rule matched), then text sniffing.
 */
export function detectKind(opts: {
  name?: string;
  mime?: string | null;
  head?: Uint8Array;
  hint?: Kind | null;
}): Kind | null {
  const byName = opts.name ? kindFromName(opts.name) : null;
  if (opts.head) {
    const magic = kindFromMagic(opts.head);
    // Containers like ftyp are shared by HEIC (image) and MP4 (media): trust the name then.
    if (magic && !(byName && magic === "media" && byName === "image")) return magic;
  }
  const byMime = kindFromMime(opts.mime);
  // text/plain is a common lie for CSV and code; prefer a more specific name.
  if (byMime && !(byMime === "text" && byName)) return byMime;
  if (byName) return byName;
  if (opts.hint) return opts.hint;
  if (opts.head && looksLikeText(opts.head)) return "text";
  return null;
}
