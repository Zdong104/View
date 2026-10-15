"""PyInstaller entry point (the package is imported normally, so relative
imports inside ``preview`` keep working)."""
import sys

from preview.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
