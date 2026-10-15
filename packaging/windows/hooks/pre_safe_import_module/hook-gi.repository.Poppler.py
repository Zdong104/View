# gi.repository.* modules are created at runtime; tell PyInstaller's module
# graph that gi.repository.Poppler exists so hook-gi.repository.Poppler runs.
def pre_safe_import_module(api):
    api.add_runtime_module(api.module_name)
