#!/usr/bin/env bash
# Build dist/view_<version>_all.deb without debhelper (needs only
# dpkg-deb, gzip and python3; fakeroot is not required: --root-owner-group).
#
#   packaging/deb/build-deb.sh            # -> dist/view_0.1.0_all.deb
#   DEB_MAINTAINER="Name <mail>" packaging/deb/build-deb.sh
set -euo pipefail
umask 022

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
APP_ID=io.github.zdong104.View
PKG=view
CMD=view-app          # not "view": /usr/bin/view is vim's alternatives link
VERSION="$(python3 -c 'import re;print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", open("preview/__init__.py").read()).group(1))')"
DEB_REVISION="${DEB_REVISION:-}"                 # e.g. "-1"; empty = native package
MAINTAINER="${DEB_MAINTAINER:-Zihan <zdong104@users.noreply.github.com>}"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
P="$STAGE/$PKG"

echo "Building $PKG $VERSION$DEB_REVISION"

# ---- payload ---------------------------------------------------------------
install -d "$P/usr/lib/$PKG" "$P/usr/bin" "$P/usr/share/applications" "$P/usr/share/man" \
    "$P/usr/share/icons/hicolor/scalable/apps" "$P/usr/share/metainfo" \
    "$P/usr/share/doc/$PKG" "$P/DEBIAN"
chmod 755 "$P"

# the Python package, without caches (byte-compiled on install by py3compile)
tar -C "$ROOT" --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' -cf - preview \
    | tar -C "$P/usr/lib/$PKG" -xf -
find "$P/usr/lib/$PKG" -type d -exec chmod 755 {} +
find "$P/usr/lib/$PKG" -type f -exec chmod 644 {} +

cat > "$P/usr/bin/$CMD" <<'EOF'
#!/usr/bin/python3
"""View launcher (installed by the view package)."""
import os
import sys

sys.path.insert(0, "/usr/lib/view")
os.environ.setdefault("VIEW_APP_LAUNCHER", "/usr/bin/view-app")

from preview.__main__ import main  # noqa: E402

sys.exit(main())
EOF
chmod 755 "$P/usr/bin/$CMD"

install -m644 "data/$APP_ID.desktop" "$P/usr/share/applications/$APP_ID.desktop"
install -m644 "data/$APP_ID.svg" "$P/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -m644 "data/$APP_ID.metainfo.xml" "$P/usr/share/metainfo/$APP_ID.metainfo.xml"
install -d "$P/usr/share/man/man1"
gzip -9n -c data/view-app.1 > "$P/usr/share/man/man1/view-app.1.gz"
chmod 644 "$P/usr/share/man/man1/view-app.1.gz"

# ---- docs ------------------------------------------------------------------
{
    cat <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: View
Upstream-Contact: https://github.com/Zdong104/View/issues
Source: https://github.com/Zdong104/View

Files: *
Copyright: 2026 Zdong104
License: PolyForm-Noncommercial-1.0.0
EOF
    # DEP-5 wants the license text indented, empty lines as " ."
    sed -e 's/^$/./' -e 's/^/ /' LICENSE
} > "$P/usr/share/doc/$PKG/copyright"
chmod 644 "$P/usr/share/doc/$PKG/copyright"

DATE="$(date -R -d "${SOURCE_DATE_EPOCH:+@$SOURCE_DATE_EPOCH}" 2>/dev/null || date -R)"
cat > "$STAGE/changelog" <<EOF
$PKG ($VERSION$DEB_REVISION) unstable; urgency=medium

  * Release $VERSION. See https://github.com/Zdong104/View/releases

 -- $MAINTAINER  $DATE
EOF
gzip -9n -c "$STAGE/changelog" > "$P/usr/share/doc/$PKG/changelog.gz"
chmod 644 "$P/usr/share/doc/$PKG/changelog.gz"

# ---- control ---------------------------------------------------------------
# Minimum versions: libadwaita 1.7 (Adw.ToggleGroup; Adw.ShortcutsDialog from 1.8
# is optional), GTK 4.18 (required by libadwaita 1.7).  This rules out
# Ubuntu 24.04 (Adw 1.5, GTK 4.14); Ubuntu >= 25.04 and Debian 13 qualify.
DEPENDS="python3 (>= 3.12), python3-gi (>= 3.48), python3-gi-cairo, python3-cairo,
 gir1.2-glib-2.0, gir1.2-gtk-4.0 (>= 4.18), gir1.2-adw-1 (>= 1.7),
 gir1.2-poppler-0.18, gir1.2-gdkpixbuf-2.0, gir1.2-graphene-1.0, gir1.2-pango-1.0,
 gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0,
 gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-gtk4,
 python3-numpy, python3-pil, python3-pypdf, python3-opengl, python3-img2pdf,
 python3-pygments, python3-openpyxl, libegl1, libgl1"
RECOMMENDS="gstreamer1.0-libav, gstreamer1.0-plugins-bad, gstreamer1.0-plugins-ugly,
 ghostscript, python3-venv, librsvg2-common, webp-pixbuf-loader, heif-gdk-pixbuf,
 7zip | p7zip-full"
SUGGESTS="nautilus, gir1.2-gtksource-5, libreoffice-core"

SIZE_KB="$(du -sk --exclude=DEBIAN "$P" | cut -f1)"
cat > "$P/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION$DEB_REVISION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $SIZE_KB
Depends: $(echo $DEPENDS)
Recommends: $(echo $RECOMMENDS)
Suggests: $(echo $SUGGESTS)
Section: graphics
Priority: optional
Homepage: https://github.com/Zdong104/View
Description: macOS Preview-style viewer for PDFs, images, video, text and 3D
 View is a fast document and media viewer for GNOME built with GTK 4 and
 libadwaita. It opens PDFs (annotate, fill forms, reorder, merge, sign),
 images (markup, crop, adjust, combine into a PDF), video and audio,
 CSV/TSV spreadsheets, source code with syntax highlighting, and STL/OBJ/PLY
 3D models. Signatures can be drawn on the touchpad or with a mouse or pen.
 .
 Optional per-user extras, enabled from Preferences: Space-bar Quick Look
 for GNOME Files, a "Combine Images into PDF" Files script, and a downloadable
 text-recognition (OCR) engine.
 .
 Licensed under the PolyForm Noncommercial License 1.0.0.
EOF

# Byte-compile on install / clean on removal, like dh_python3 does for
# private modules.  Desktop database and icon cache are refreshed by the
# file triggers of desktop-file-utils and gtk-update-icon-cache.
cat > "$P/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v py3compile >/dev/null 2>&1; then
    py3compile -p view /usr/lib/view/ || true
fi
exit 0
EOF
cat > "$P/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if command -v py3clean >/dev/null 2>&1; then
    py3clean -p view || true
else
    find /usr/lib/view -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi
exit 0
EOF
chmod 755 "$P/DEBIAN/postinst" "$P/DEBIAN/prerm"

( cd "$P" && find . -path ./DEBIAN -prune -o -type f -printf '%P\0' | sort -z \
    | xargs -0 md5sum ) > "$P/DEBIAN/md5sums"
chmod 644 "$P/DEBIAN/md5sums" "$P/DEBIAN/control"

mkdir -p "$OUT"
DEB="$OUT/${PKG}_${VERSION}${DEB_REVISION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$P" "$DEB" >/dev/null
echo "$DEB"
