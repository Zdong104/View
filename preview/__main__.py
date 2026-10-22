"""Command-line entry point: ``view-app`` / ``python -m preview``.

Options handled here (before GTK starts, no display needed):
  --version                      print the version and exit
  --startup-trace [FILE…]        time each startup step to the first window, then quit
  --check-updates                print whether a newer release is available
  --selftest [--no-ui]           check that every viewer works, exit 0/1
  --quicklook [--debug] [FILE…]  run the Quick Look service for GNOME Files
  --screenshot-watcher           open new GNOME screenshots for quick markup
  --combine-pdf -o OUT IMG…      combine images into a PDF (see --combine-pdf --help)
  --enable-files-integration     Space-bar previews + "Combine Images into PDF" in Files
  --disable-files-integration    undo the above
  --print-files-extension        print the Files "Combine Images into PDF" extension
Everything else (files, --new-signature, GApplication options) goes to the app.
"""
import sys

from . import runtime

runtime.configure_gl_env()
runtime.configure_frozen_env()
if runtime.IS_WINDOWS and runtime.FROZEN:
    from . import opengl_win
    opengl_win.ensure()      # no OpenGL driver: bundled Mesa, before GTK loads opengl32.dll
    runtime.mark("OpenGL chosen")


def _usage_extra():
    return ("\nPreview options:\n"
            "  --version                     Show the version and exit\n"
            "  --startup-trace [FILE...]     Time each startup step, print it and quit\n"
            "  --check-updates               Check GitHub for a newer release\n"
            "  --new-signature               Draw a new signature\n"
            "  --selftest [--no-ui]          Check that all viewers work and exit\n"
            "  --combine-pdf -o OUT IMG...   Combine images into one PDF\n"
            "  --enable-files-integration    Use View for Space-bar previews in GNOME Files\n"
            "  --disable-files-integration   Give Space-bar previews back to GNOME Sushi\n"
            "  --print-files-extension       Print the Files \"Combine Images into PDF\" extension\n")


def _safe_stdio():
    """Never crash on printing non-ASCII (e.g. a cp1252 Windows console);
    a windowed Windows exe has no stdout at all."""
    for s in (sys.stdout, sys.stderr):
        try:
            if s is not None and hasattr(s, "reconfigure"):
                s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main(argv=None):
    _safe_stdio()
    argv = list(sys.argv if argv is None else argv)
    args = argv[1:]
    first = args[0] if args else ""

    if first in ("--version", "-v", "-V"):
        print(f"{runtime.APP_NAME} {runtime.VERSION}")
        return 0
    if first == "--selftest":
        from .selftest import main as selftest_main
        return selftest_main(args[1:])
    if first == "--quicklook":
        from .quicklook import main as ql_main
        return ql_main([argv[0], *args[1:]])
    if first == "--screenshot-watcher":
        from .screenshot import main as screenshot_main
        return screenshot_main(args[1:])
    if first == "--combine-pdf":
        from .convert import main as convert_main
        return convert_main(args[1:])
    if first in ("--enable-files-integration", "--disable-files-integration"):
        from . import integration
        return integration.cli(first == "--enable-files-integration")
    if first == "--print-files-extension":
        from . import integration
        return integration.print_extension()
    if first == "--check-updates":
        from .updates import cli as updates_cli
        return updates_cli()
    if first == "--startup-trace":
        runtime.startup_trace = True
        runtime.stall_watch = runtime.StallWatch()
        return _run_app([argv[0], *args[1:]])
    if first in ("-h", "--help", "--help-all"):
        # let GApplication print its help, then ours
        rc = _run_app([argv[0], first])
        print(_usage_extra())
        return rc
    return _run_app(argv)


def _run_app(argv):
    # loaded one by one only to time them (runtime.mark); app.py needs them all
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gio  # noqa: F401
    runtime.mark("GLib and GIO loaded")
    from gi.repository import Gtk  # noqa: F401  (also opens the display)
    runtime.mark("GTK loaded, display opened")
    from gi.repository import Adw  # noqa: F401
    runtime.mark("libadwaita loaded")
    from .app import PreviewApp
    runtime.mark("app code imported")
    return PreviewApp().run(argv)


if __name__ == "__main__":
    # PyInstaller's frozen entry runs this module as __main__ without a package
    # context; relative imports above still work because the spec uses
    # preview/__main__.py through a tiny bootstrap (packaging/windows/preview_main.py).
    sys.exit(main())
