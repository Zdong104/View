"""PyInstaller entry point for view-translate.exe (see preview/translate_helper.py)."""
import sys

from preview.translate_helper import main

if __name__ == "__main__":
    sys.exit(main())
