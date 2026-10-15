"""Copy the license files of every installed Python distribution into OUTDIR
and print one "name version license" line per distribution.
Usage: python collect-python-licenses.py OUTDIR"""
import importlib.metadata as md
import os
import shutil
import sys

out = sys.argv[1]
for d in sorted(md.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
    name = d.metadata["Name"]
    if not name:
        continue
    lic = d.metadata.get("License-Expression") or d.metadata.get("License") or ""
    lic = lic.splitlines()[0][:60] if lic else "see license files"
    print(f"  {name:40s} {d.version:22s} {lic}")
    for f in d.files or ():
        base = os.path.basename(str(f)).upper()
        if base.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")):
            src = d.locate_file(f)
            if os.path.isfile(src):
                dst = os.path.join(out, name)
                os.makedirs(dst, exist_ok=True)
                shutil.copy(src, os.path.join(dst, os.path.basename(str(f))))
