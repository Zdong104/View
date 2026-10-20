#!/usr/bin/env bash
# Install View from this source checkout for the current user (no sudo).
# It shows up as "View (Dev)" so it can't be mistaken for the snap or .deb.
# Re-run after moving the checkout.  Options:
#   --no-files-integration   don't take over Space-bar previews in GNOME Files
#   --uninstall              remove the per-user install (keeps the venv)
#   --purge                  remove it and its Python venv
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.zdong104.View
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VENV="$DATA/view/venv"
BIN="$HOME/.local/bin/view-app"
FILES_INTEGRATION=1
UNINSTALL=0
PURGE=0
for a in "$@"; do
    case "$a" in
        --no-files-integration) FILES_INTEGRATION=0 ;;
        --uninstall) UNINSTALL=1 ;;
        --purge) UNINSTALL=1; PURGE=1 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

# --- clean up files from pre-release names (Preview / older app IDs) ---
for old in io.github.zihan.Preview io.github.zdong104.Preview; do
    rm -f "$DATA/applications/$old.desktop" \
          "$DATA/icons/hicolor/scalable/apps/$old.svg" \
          "$DATA/metainfo/$old.metainfo.xml"
done
rm -f "$HOME/.local/bin/preview-quicklook"
if [ -e "$HOME/.local/bin/preview-app" ] && [ ! -L "$HOME/.local/bin/preview-app" ]; then
    rm -f "$HOME/.local/bin/preview-app"
fi

if [ "$UNINSTALL" = 1 ]; then
    [ -x "$BIN" ] && "$BIN" --disable-files-integration || true
    # screenshot quick edit: stop the watcher, drop its login entry (if it is ours)
    [ -x "$BIN" ] && "$BIN" --screenshot-watcher --stop || true
    AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.ScreenshotEditor.desktop"
    grep -qs "Installed by View" "$AUTOSTART" && rm -f "$AUTOSTART"
    rm -f "$BIN" "$HOME/.local/bin/preview-app" "$DATA/applications/$APP_ID.desktop" \
          "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg" \
          "$DATA/metainfo/$APP_ID.metainfo.xml"
    update-desktop-database -q "$DATA/applications" 2>/dev/null || true
    if [ "$PURGE" = 1 ]; then
        rm -rf "$VENV"
        rmdir "$DATA/view" 2>/dev/null || true
        echo "Removed View (Dev) and its Python venv."
    else
        echo "Removed View (Dev) (the Python venv in $VENV was kept; --purge removes it)."
    fi
    exit 0
fi

if [ ! -x "$VENV/bin/python" ]; then
    /usr/bin/python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install -q -r "$SRC/requirements.txt" -r "$SRC/requirements-ocr.txt"

mkdir -p "$(dirname "$BIN")"
cat > "$BIN" <<EOF
#!/bin/sh
export VIEW_APP_LAUNCHER="$BIN"
PYTHONPATH="$SRC\${PYTHONPATH:+:\$PYTHONPATH}" exec "$VENV/bin/python" -m preview "\$@"
EOF
chmod +x "$BIN"
ln -sfn view-app "$HOME/.local/bin/preview-app"     # old name, for muscle memory

install -Dm644 "$SRC/data/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$SRC/data/$APP_ID.metainfo.xml" "$DATA/metainfo/$APP_ID.metainfo.xml"
mkdir -p "$DATA/applications"
sed -e "s|^Exec=view-app|Exec=$BIN|" -e "s|^Name=View$|Name=View (Dev)|" \
    "$SRC/data/$APP_ID.desktop" > "$DATA/applications/$APP_ID.desktop"

# GNOME Files: Space-bar Quick Look + "Combine Images into PDF" script.
# Both are per-user; undo with: view-app --disable-files-integration
if [ "$FILES_INTEGRATION" = 1 ]; then
    "$BIN" --enable-files-integration || true
fi

update-desktop-database -q "$DATA/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
echo "Installed. Launch “View (Dev)” from the app grid, or run: $BIN [files]"
echo "Remove it again with: $SRC/install.sh --uninstall   (or --purge to drop its venv too)"
