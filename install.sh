#!/usr/bin/env bash
# Install Preview from this source checkout for the current user (no sudo).
# Re-run after moving the checkout.  Options:
#   --no-files-integration   don't take over Space-bar previews in GNOME Files
#   --uninstall              remove the per-user install (keeps the venv)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.zdong104.Preview
OLD_ID=io.github.zihan.Preview
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VENV="$DATA/preview-app/venv"
BIN="$HOME/.local/bin/preview-app"
FILES_INTEGRATION=1
UNINSTALL=0
for a in "$@"; do
    case "$a" in
        --no-files-integration) FILES_INTEGRATION=0 ;;
        --uninstall) UNINSTALL=1 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

# --- clean up files from older versions (old app ID / old launchers) ---
rm -f "$DATA/applications/$OLD_ID.desktop" \
      "$DATA/icons/hicolor/scalable/apps/$OLD_ID.svg" \
      "$HOME/.local/bin/preview-quicklook"

if [ "$UNINSTALL" = 1 ]; then
    [ -x "$BIN" ] && "$BIN" --disable-files-integration || true
    rm -f "$BIN" "$DATA/applications/$APP_ID.desktop" \
          "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg" \
          "$DATA/metainfo/$APP_ID.metainfo.xml"
    update-desktop-database -q "$DATA/applications" 2>/dev/null || true
    echo "Uninstalled (the Python venv in $VENV was kept)."
    exit 0
fi

if [ ! -x "$VENV/bin/python" ]; then
    /usr/bin/python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install -q pypdf PyOpenGL img2pdf openpyxl rapidocr onnxruntime

mkdir -p "$(dirname "$BIN")"
cat > "$BIN" <<EOF
#!/bin/sh
export PREVIEW_APP_LAUNCHER="$BIN"
PYTHONPATH="$SRC\${PYTHONPATH:+:\$PYTHONPATH}" exec "$VENV/bin/python" -m preview "\$@"
EOF
chmod +x "$BIN"

install -Dm644 "$SRC/data/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$SRC/data/$APP_ID.metainfo.xml" "$DATA/metainfo/$APP_ID.metainfo.xml"
mkdir -p "$DATA/applications"
sed -e "s|^Exec=preview-app|Exec=$BIN|" "$SRC/data/$APP_ID.desktop" \
    > "$DATA/applications/$APP_ID.desktop"

# GNOME Files: Space-bar Quick Look + "Combine Images into PDF" script.
# Both are per-user; undo with: preview-app --disable-files-integration
if [ "$FILES_INTEGRATION" = 1 ]; then
    "$BIN" --enable-files-integration || true
fi

update-desktop-database -q "$DATA/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
echo "Installed. Launch “Preview” from the app grid, or run: $BIN [files]"
