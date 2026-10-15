"""PyInstaller entry point for view-ocr.exe (see preview/ocr_helper.py)."""
import sys

from preview.ocr_helper import main

if __name__ == "__main__":
    sys.exit(main())
