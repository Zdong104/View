# Poppler's GObject-introspection typelib + DLLs (no hook ships with PyInstaller).
from PyInstaller.utils.hooks.gi import GiModuleInfo

module_info = GiModuleInfo("Poppler", "0.18")
if module_info.available:
    binaries, datas, hiddenimports = module_info.collect_typelib_data()
