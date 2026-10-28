# PyInstaller spec for the translation helper, view-translate.exe.
# Build it with python.org's Python (NOT MSYS2's: ctranslate2 has no wheels
# for that one), after `pip install -r requirements-translate.txt pyinstaller`:
#   pyinstaller --noconfirm --distpath dist --workpath build/pyi-translate packaging/windows/view-translate.spec
# Produces dist/view-translate/, which goes into the app folder as translate\
# (View looks for translate\view-translate.exe next to View.exe; see
# preview/translate.py).  Language packs are not bundled: View downloads them.
import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

a = Analysis(
    [os.path.join(SPECPATH, "translate_helper_main.py")],
    pathex=[ROOT],
    # ctranslate2.dll and its OpenMP runtime sit next to the extension module
    binaries=collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("sentencepiece"),
    hiddenimports=collect_submodules("ctranslate2") + ["sentencepiece"],
    excludes=["gi", "cairo", "tkinter", "matplotlib", "IPython", "pytest", "torch",
              "PyQt5", "PyQt6", "PySide2", "PySide6", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
# a console program: View talks to it over stdin/stdout and starts it with
# CREATE_NO_WINDOW, so no console window ever shows
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="view-translate", console=True,
          upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="view-translate", upx=False)
