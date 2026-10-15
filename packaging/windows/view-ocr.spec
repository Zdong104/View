# PyInstaller spec for the text-recognition helper, view-ocr.exe.
# Build it with python.org's Python (NOT MSYS2's: onnxruntime has no wheels
# for that one), after `pip install -r requirements-ocr.txt pyinstaller`:
#   pyinstaller --noconfirm --distpath dist --workpath build/pyi-ocr packaging/windows/view-ocr.spec
# Produces dist/view-ocr/, which goes into the app folder as ocr\ (View
# looks for ocr\view-ocr.exe next to View.exe; see preview/ocr.py).
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

a = Analysis(
    [os.path.join(SPECPATH, "ocr_helper_main.py")],
    pathex=[ROOT],
    datas=collect_data_files("rapidocr"),           # ONNX models + YAML configs
    hiddenimports=collect_submodules("rapidocr"),
    excludes=["gi", "cairo", "tkinter", "matplotlib", "IPython", "pytest",
              "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
# a console program: View talks to it over stdin/stdout and starts it with
# CREATE_NO_WINDOW, so no console window ever shows
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="view-ocr", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="view-ocr", upx=False)
