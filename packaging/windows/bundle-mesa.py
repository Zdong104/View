"""Copy MSYS2's Mesa OpenGL (llvmpipe software renderer) into the PyInstaller
bundle, for PCs without an OpenGL driver (see preview/opengl_win.py).

    python packaging/windows/bundle-mesa.py MESA_OPENGL32_DLL dist/View

Copies opengl32.dll into <bundle>/_internal/mesa/ together with every MSYS2
DLL it needs (libgallium_wgl.dll, LLVM, ...) that the bundle doesn't have yet.  Run with MSYS2's Python, which has PyInstaller.
"""
import os
import shutil
import sys

from PyInstaller.depend.bindepend import get_imports

src = os.path.abspath(sys.argv[1])
bundle = os.path.abspath(sys.argv[2])
internal = os.path.join(bundle, "_internal")
dest = os.path.join(internal, "mesa")
bindir = os.path.dirname(src)
system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
have = {n.lower() for n in os.listdir(internal)}

os.makedirs(dest, exist_ok=True)
queue, seen, total = [os.path.basename(src)], set(), 0
while queue:
    name = queue.pop()
    if name.lower() in seen:
        continue
    seen.add(name.lower())
    path = os.path.join(bindir, name)
    # anything MSYS2 provides is copied (a VM may lack e.g. vulkan-1.dll even
    # if this machine has it), unless the bundle has it already
    if name.lower() in have or not os.path.exists(path):
        if name.lower() not in have and not os.path.exists(os.path.join(system, name)) \
                and not name.lower().startswith(("api-ms-win-", "ext-ms-")):
            sys.exit(f"bundle-mesa: {name} is neither in {bindir} nor in Windows")
        continue
    shutil.copy2(path, dest)
    total += os.path.getsize(path)
    print(f"  {os.path.getsize(path) / 1e6:7.1f} MB  {name}")
    # PyInstaller 6 returns (name, resolved path) pairs, older versions names
    queue += [i[0] if isinstance(i, tuple) else i for i in get_imports(path)]
print(f"bundle-mesa: {len(os.listdir(dest))} files, {total / 1e6:.0f} MB in {dest}")
