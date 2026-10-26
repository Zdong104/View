# PyInstaller spec for the Windows build (run from an MSYS2 UCRT64 shell):
#   pyinstaller --noconfirm --distpath dist --workpath build packaging/windows/preview.spec
# Produces dist/View/{View.exe (GUI), view-app.exe (console: --version,
# --selftest, --combine-pdf)} plus the GTK/GStreamer runtime.
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
APP_ID = "io.github.zdong104.View"

hiddenimports = collect_submodules("preview") + [
    "gi._gi_cairo", "cairo",
    "pygments.lexers", "pygments.styles", "pygments.formatters",
]
hiddenimports += collect_submodules("pygments.lexers") + collect_submodules("pygments.styles")
hiddenimports += collect_submodules("OpenGL.platform") + collect_submodules("OpenGL.arrays")
hiddenimports += ["OpenGL.GL", "openpyxl", "img2pdf", "pypdf", "certifi"]

datas = [
    (os.path.join(ROOT, "data", APP_ID + ".svg"), "share/icons/hicolor/scalable/apps"),
    (os.path.join(ROOT, "LICENSE"), "."),
    (os.path.join(ROOT, "preview", "locale"), "preview/locale"),     # UI translations (.mo)
]
binaries = []

# Text recognition is not in this bundle: it is a separate program,
# ocr\view-ocr.exe, built with python.org's Python (view-ocr.spec).

a = Analysis(
    [os.path.join(SPECPATH, "preview_main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[os.path.join(SPECPATH, "hooks")],
    hooksconfig={
        "gi": {
            "icons": ["Adwaita", "hicolor"],
            "themes": ["Adwaita"],
            "languages": ["en_US", "en_GB", "de", "fr", "es", "it", "pt_BR", "ja", "zh_CN"],
            "module-versions": {"Gtk": "4.0", "Gdk": "4.0", "Gsk": "4.0", "GtkSource": "5"},
        },
        # Only the plugins playback needs.  Bundling all of them is huge and the
        # frozen app loads plugins in-process (no fork scanner): one stray
        # plugin (a ggml-based one) aborted the process during registry scan.
        "gstreamer": {
            "include_plugins": [
                "coreelements", "playback", "typefindfunctions", "app", "autodetect",
                "videoconvertscale", "videorate", "videofilter", "deinterlace",
                "overlaycomposition", "pango", "subparse",
                "audioconvert", "audioresample", "audiorate", "volume", "audiofx",
                "wasapi", "wasapi2", "directsound",
                "d3d11", "d3d12", "mediafoundation", "codectimestamper", "codecalpha",
                "libav", "dav1d", "vpx", "opus", "vorbis", "flac", "mpg123",
                "isomp4", "matroska", "avi", "flv", "ogg", "wavparse", "mpegtsdemux",
                "id3demux", "apetag", "icydemux", "rawparse",
                "videoparsersbad", "audioparsers",
                "gtk4", "opengl",
            ],
        },
    },
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "matplotlib", "IPython",
              "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = os.path.join(ROOT, "build", "preview.ico")
icon = icon if os.path.exists(icon) else None

gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="View", console=False,
          icon=icon, version=None, upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="view-app", console=True,
          icon=icon, upx=False)
coll = COLLECT(gui, cli, a.binaries, a.datas, name="View", upx=False)
