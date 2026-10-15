"""Command-line entry point: ``preview-app`` / ``python -m preview``.

Options handled here (before GTK starts, no display needed):
  --version                      print the version and exit
  --selftest [--no-ui]           check that every viewer works, exit 0/1
  --quicklook [--debug] [FILE…]  run the Quick Look service for GNOME Files
  --combine-pdf -o OUT IMG…      combine images into a PDF (see --combine-pdf --help)
  --enable-files-integration     Space-bar previews + "Combine Images into PDF" in Files
  --disable-files-integration    undo the above
Everything else (files, --new-signature, GApplication options) goes to the app.
"""
import sys

from . import runtime

runtime.configure_gl_env()
runtime.configure_frozen_env()


def _usage_extra():
    return ("\nPreview options:\n"
            "  --version                     Show the version and exit\n"
            "  --new-signature               Draw a new signature\n"
            "  --selftest [--no-ui]          Check that all viewers work and exit\n"
            "  --combine-pdf -o OUT IMG...   Combine images into one PDF\n"
            "  --enable-files-integration    Use Preview for Space-bar previews in GNOME Files\n"
            "  --disable-files-integration   Give Space-bar previews back to GNOME Sushi\n")


def main(argv=None):
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
    if first == "--combine-pdf":
        from .convert import main as convert_main
        return convert_main(args[1:])
    if first in ("--enable-files-integration", "--disable-files-integration"):
        from . import integration
        return integration.cli(first == "--enable-files-integration")
    if first in ("-h", "--help", "--help-all"):
        # let GApplication print its help, then ours
        rc = _run_app([argv[0], first])
        print(_usage_extra())
        return rc
    return _run_app(argv)


def _run_app(argv):
    from .app import PreviewApp
    return PreviewApp().run(argv)


if __name__ == "__main__":
    # PyInstaller's frozen entry runs this module as __main__ without a package
    # context; relative imports above still work because the spec uses
    # preview/__main__.py through a tiny bootstrap (packaging/windows/preview_main.py).
    sys.exit(main())
