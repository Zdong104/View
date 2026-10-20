#!/bin/sh
# Rasterize the desktop app icon into the extension's PNG icons.
# Output is checked in (static/icons/), so the build does not need inkscape.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
svg="$here/../data/io.github.zdong104.View.svg"
out="$here/static/icons"
mkdir -p "$out"
for s in 16 32 48 128; do
  if command -v rsvg-convert >/dev/null 2>&1; then
    rsvg-convert -w "$s" -h "$s" "$svg" -o "$out/icon$s.png"
  elif command -v inkscape >/dev/null 2>&1; then
    inkscape "$svg" --export-type=png --export-width="$s" --export-height="$s" \
      --export-filename="$out/icon$s.png" >/dev/null 2>&1
  else
    magick -background none -density 384 "$svg" -resize "${s}x${s}" "$out/icon$s.png"
  fi
done
echo "icons written to $out"
