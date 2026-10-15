# cairo-1.0.typelib: lets PyGObject pass cairo contexts to GTK/Poppler.
from PyInstaller.utils.hooks.gi import GiModuleInfo

module_info = GiModuleInfo("cairo", "1.0")
if module_info.available:
    binaries, datas, hiddenimports = module_info.collect_typelib_data()
hiddenimports = list(globals().get("hiddenimports", [])) + ["gi._gi_cairo", "cairo"]
