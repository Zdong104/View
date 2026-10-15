#!/usr/bin/env bash
# Run in the MSYS2 UCRT64 shell after PyInstaller: copies the license texts of
# every bundled MSYS2 package and Python distribution into DIST/licenses and
# writes a manifest with exact versions + where to get the corresponding source
# (needed for GPL components such as Poppler, and LGPL GTK/GStreamer).
set -euo pipefail
DIST="${1:-dist/View}"
OUT="$DIST/licenses"
PREFIX="${MINGW_PREFIX:-/ucrt64}"
PKGPFX="${MINGW_PACKAGE_PREFIX:-mingw-w64-ucrt-x86_64}"
mkdir -p "$OUT/msys2" "$OUT/python"

cp LICENSE "$OUT/View-LICENSE.txt"

# MSYS2 packages that ended up in the bundle (+ their dependency closure)
pacman -Qq | grep "^$PKGPFX-" | sort > "$OUT/.pkgs"
{
    echo "Third-party components bundled with View for Windows"
    echo "======================================================="
    echo
    echo "View itself is licensed under the PolyForm Noncommercial License 1.0.0"
    echo "(View-LICENSE.txt). It bundles the following independent components,"
    echo "each under its own license (texts in msys2/ and python/). Their licenses"
    echo "are not changed by View's license."
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
    python "$(dirname "$0")/collect-python-licenses.py" "$OUT/python"
} > "$OUT/THIRD-PARTY-NOTICES.txt"
rm -f "$OUT/.pkgs"
echo "licenses: $(find "$OUT" -type f | wc -l) files in $OUT"
