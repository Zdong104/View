"""Shared contract between the app shell (app.py) and the document viewers.

Every viewer is a Gtk.Box subclass of Viewer. The window creates exactly one
viewer per document window, calls build_header() once, then load().
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gio, GLib, Graphene, Gtk

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".jpe", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".jxl", ".svg", ".ico", ".tga", ".qoi",
}
PDF_EXTS = {".pdf"}
MODEL_EXTS = {".stl", ".obj", ".ply"}
TABLE_EXTS = {".csv", ".tsv", ".tab"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".ogv", ".3gp"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".log", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml",
    ".json", ".jsonc", ".jsonl", ".ndjson", ".xml", ".html", ".htm", ".xhtml", ".css",
    ".scss", ".sass", ".less", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
    ".svelte", ".py", ".pyw", ".pyi", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat",
    ".cmd", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".cu", ".cuh",
    ".java", ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".m",
    ".mm", ".swift", ".scala", ".cs", ".dart", ".hs", ".ml", ".ex", ".exs", ".erl",
    ".clj", ".jl", ".zig", ".nim", ".sql", ".tex", ".sty", ".cls", ".bib", ".cmake",
    ".mk", ".make", ".diff", ".patch", ".rst", ".org", ".srt", ".vtt", ".proto", ".gradle",
    ".tf", ".nix", ".vim", ".asm", ".s", ".properties", ".service", ".desktop",
    ".gitignore", ".gitattributes", ".gitmodules", ".dockerignore", ".editorconfig",
    ".env", ".dockerfile", ".lock",
}
# extensionless (or dot-only) file names that are text
TEXT_NAMES = {
    "makefile", "gnumakefile", "dockerfile", "containerfile", "rakefile", "gemfile",
    "procfile", "vagrantfile", "jenkinsfile", "justfile", "readme", "license", "licence",
    "copying", "authors", "changelog", "notice", "todo", "news", "install", "cmakelists.txt",
    ".bashrc", ".zshrc", ".profile", ".bash_profile", ".vimrc", ".gitconfig", ".npmrc",
}
SNIFF_BYTES = 8192


def sniff_text(gfile: Gio.File) -> bool:
    """True if an otherwise unknown file looks like text: a text/* content type,
    or an empty file, or a first 8 KB without NUL bytes that decodes as UTF-8."""
    try:
        info = gfile.query_info("standard::content-type,standard::type",
                                Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error:
        return False
    if info.get_file_type() not in (Gio.FileType.REGULAR, Gio.FileType.SYMBOLIC_LINK,
                                    Gio.FileType.UNKNOWN):
        return False
    ct = info.get_content_type() or ""
    if ct.startswith("text/") or Gio.content_type_is_a(ct, "text/plain"):
        return True
    try:
        stream = gfile.read(None)
        try:
            data = stream.read_bytes(SNIFF_BYTES, None).get_data()
        finally:
            stream.close(None)
    except GLib.Error:
        return False
    if not data:
        return True
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as e:
        return e.start > len(data) - 4      # only cut inside a multibyte character
    return True


def kind_of(gfile: Gio.File, sniff: bool = False) -> str:
    """Return 'pdf', 'image', 'model', 'table', 'video', 'text' or 'unknown'.

    With sniff=True an unknown file is looked at (content type, first 8 KB) and
    reported as 'text' if it is text; leave it off for bulk scans (siblings)."""
    name = (gfile.get_basename() or "").lower()
    ext = name[name.rfind("."):] if "." in name else ""
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in MODEL_EXTS:
        return "model"
    if ext in TABLE_EXTS:
        return "table"
    if ext in MEDIA_EXTS:
        return "video"
    if ext in TEXT_EXTS or name in TEXT_NAMES or name.startswith(".env"):
        return "text"
    if sniff and sniff_text(gfile):
        return "text"
    return "unknown"


def _human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000.0


def _fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    try:
        return dt.to_local().format("%x, %X")
    except Exception:  # noqa: BLE001
        return "—"


def file_info_section(gfile: Gio.File):
    """Generic 'File' section for the Inspector: name, kind, size, folder and
    created / modified / last-opened times."""
    attrs = ("standard::size,standard::content-type,standard::display-name,"
             "time::created,time::modified,time::access")
    try:
        info = gfile.query_info(attrs, Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error:
        return ("File", [("File name", gfile.get_basename() or "—")])
    size = info.get_size()
    ct = info.get_content_type()
    path = gfile.get_path()
    parent = gfile.get_parent()
    where = (parent.get_path() if parent is not None else None) or (path or gfile.get_uri())

    opened = None
    try:
        uri = gfile.get_uri()
        for ri in Gtk.RecentManager.get_default().get_items():
            if ri.get_uri() == uri:
                v = ri.get_visited()
                if v is not None and v.to_unix() > 0:
                    opened = v
                break
    except Exception:  # noqa: BLE001
        pass
    if opened is None:
        atime = info.get_attribute_uint64("time::access")
        if atime:
            opened = GLib.DateTime.new_from_unix_local(atime)

    rows = [
        ("File name", info.get_display_name() or gfile.get_basename() or "—"),
        ("Kind", (Gio.content_type_get_description(ct) if ct else None) or "Unknown"),
        ("Size", f"{_human_size(size)} ({size:,} bytes)" if size >= 1000 else f"{size} bytes"),
        ("Where", where),
        ("Created", _fmt_dt(info.get_creation_date_time())),
        ("Modified", _fmt_dt(info.get_modification_date_time())),
        ("Last opened", _fmt_dt(opened)),
    ]
    return ("File", rows)


def device_scale(widget):
    """Device pixels per logical pixel of the widget's surface (fractional-aware)."""
    native = widget.get_native()
    surf = native.get_surface() if native else None
    if surf is not None and hasattr(surf, "get_scale"):
        return surf.get_scale() or 1
    return widget.get_scale_factor() or 1


def surface_origin(widget):
    """The widget's top-left in its surface's logical coordinates."""
    native = widget.get_native()
    if native is None:
        return 0.0, 0.0
    pt = Graphene.Point()
    pt.init(0, 0)
    ok, p = widget.compute_point(native, pt)
    if not ok:
        return 0.0, 0.0
    nx, ny = native.get_surface_transform()
    return p.x + nx, p.y + ny


class Viewer(Gtk.Box):
    """Base class. Subclasses override load() and build_header()."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window  # the Adw.ApplicationWindow hosting this viewer
        self.set_hexpand(True)
        self.set_vexpand(True)

    # --- to override -----------------------------------------------------
    def load(self, files: list[Gio.File]) -> None:
        """Open the given files (several only for images)."""
        raise NotImplementedError

    def build_header(self, header: Adw.HeaderBar) -> None:
        """Pack viewer-specific buttons into the window's header bar.

        Do NOT add an info / Inspector button: the shell owns exactly one
        (Ctrl+I) and shows file_info_section() plus get_info() sections."""

    def get_title(self) -> str:
        """Window title (usually the file name)."""
        return "View"

    def get_subtitle(self) -> str:
        """Window subtitle, e.g. 'Page 2 of 7' or '1920 × 1080'."""
        return ""

    def has_unsaved_changes(self) -> bool:
        return False

    def accepts_drop(self, files: list[Gio.File]) -> bool:
        """Return True if this viewer wants files dropped onto it (e.g. a PDF
        viewer inserting pages, an image viewer adding images). The shell
        calls add_dropped() then; otherwise it opens a new window."""
        return False

    def add_dropped(self, files: list[Gio.File]) -> None:
        pass

    def get_info(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """Inspector content: [(section title, [(label, value), ...]), ...].
        Default: the generic File section for get_current_file() (or a
        gfile/file/current_file attribute). Values written as
        '[text](https://url)' are shown as links."""
        gf = None
        getter = getattr(self, "get_current_file", None)
        if callable(getter):
            try:
                gf = getter()
            except Exception:  # noqa: BLE001
                gf = None
        if gf is None:
            for attr in ("gfile", "file", "current_file"):
                v = getattr(self, attr, None)
                if isinstance(v, Gio.File):
                    gf = v
                    break
        return [file_info_section(gf)] if gf is not None else []

    # --- helpers available to every viewer --------------------------------
    def toast(self, message: str, button_label: str | None = None, on_button=None):
        self.window.show_toast(message, button_label, on_button)

    def refresh_title(self):
        self.window.refresh_title()
