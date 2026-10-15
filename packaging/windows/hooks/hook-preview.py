# Viewers are imported lazily (by file type), so list every module.
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("preview")
