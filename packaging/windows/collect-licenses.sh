#!/usr/bin/env bash
# Run in the MSYS2 UCRT64 shell after PyInstaller: copies the license texts of
# every bundled MSYS2 package and Python distribution into DIST/licenses and
# writes a manifest with exact versions + where to get the corresponding source
# (needed for GPL components such as Poppler, and LGPL GTK/GStreamer).
set -euo pipefail
DIST="${1:-dist/Preview}"
OUT="$DIST/licenses"
PREFIX="${MINGW_PREFIX:-/ucrt64}"
PKGPFX="${MINGW_PACKAGE_PREFIX:-mingw-w64-ucrt-x86_64}"
mkdir -p "$OUT/msys2" "$OUT/python"

cp LICENSE "$OUT/Preview-LICENSE.txt"

# MSYS2 packages that ended up in the bundle (+ their dependency closure)
pacman -Qq | grep "^$PKGPFX-" | sort > "$OUT/.pkgs"
{
    echo "Third-party components bundled with Preview for Windows"
    echo "======================================================="
    echo
    echo "Preview itself is licensed under the PolyForm Noncommercial License 1.0.0"
    echo "(Preview-LICENSE.txt). It bundles the following independent components,"
    echo "each under its own license (texts in msys2/ and python/). Their licenses"
    echo "are not changed by Preview's license."
    echo
    echo "Corresponding source for every MSYS2 package listed below:"
    echo "  build recipes: https://github.com/msys2/MINGW-packages (directory mingw-w64-<name>)"
    echo "  source archives: https://repo.msys2.org/mingw/sources/ (<name>-<version>.src.tar.zst)"
    echo "These source offers are valid for at least three years from the release date."
    echo
    echo "MSYS2 packages (name version license):"
    while read -r p; do
        name="${p#"$PKGPFX"-}"
        ver="$(pacman -Q "$p" | cut -d' ' -f2)"
        lic="$(pacman -Qi "$p" | sed -n 's/^Licenses *: //p')"
        printf '  %-40s %-22s %s\n' "$name" "$ver" "$lic"
        if [ -d "$PREFIX/share/licenses/$name" ]; then
            cp -r "$PREFIX/share/licenses/$name" "$OUT/msys2/$name"
        fi
    done < "$OUT/.pkgs"
    echo
    echo "Python distributions:"
    python - "$OUT/python" <<'PY'
import importlib.metadata as md, os, shutil, sys
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
PY
} > "$OUT/THIRD-PARTY-NOTICES.txt"
rm -f "$OUT/.pkgs"
echo "licenses: $(find "$OUT" -type f | wc -l) files in $OUT"
