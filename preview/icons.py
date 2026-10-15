"""Custom symbolic icons, written once to the user cache and added to the
icon theme's search path (as a hicolor fallback, so a system theme that has
an icon of the same name still wins).

Covers View's own icons (3D viewer) and the few freedesktop/GNOME icon
names View uses that Adwaita doesn't ship: Linux desktops usually provide
them through the system theme, the Windows build only bundles Adwaita.
"""
from __future__ import annotations

import math
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk


def _strokes(segments, w=1.25):
    """GTK's symbolic loader fills every path and ignores strokes, so line art
    is emitted as filled quads (consistent winding, nonzero = union) with
    round caps."""
    parts, caps = [], set()
    for (x0, y0), (x1, y1) in segments:
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n * w / 2, dx / n * w / 2
        parts.append(f"M{x0 + nx:.3f} {y0 + ny:.3f}L{x1 + nx:.3f} {y1 + ny:.3f}"
                     f"L{x1 - nx:.3f} {y1 - ny:.3f}L{x0 - nx:.3f} {y0 - ny:.3f}Z")
        caps.update({(x0, y0), (x1, y1)})
    body = f'<path fill="#000" d="{"".join(parts)}"/>'
    body += "".join(f'<circle fill="#000" cx="{x}" cy="{y}" r="{w / 2}"/>' for x, y in caps)
    return body


def _poly(*pts, closed=True):
    seg = list(zip(pts, pts[1:]))
    if closed:
        seg.append((pts[-1], pts[0]))
    return seg


_C, _T, _R, _BR, _B, _BL, _L = (8, 8), (8, 1.5), (14, 4.8), (14, 11.2), (8, 14.5), (2, 11.2), (2, 4.8)
_ICONS = {
    "preview-model-wireframe-symbolic": _strokes(
        _poly(_T, _R, _BR, _B, _BL, _L) + [(_C, _B), (_C, _R), (_C, _L), (_L, _B)]),
    "preview-model-shading-symbolic":
        '<path fill="#000" fill-rule="evenodd" d="M8 1.4A6.6 6.6 0 1 1 7.99 1.4ZM8 2.7A5.3 5.3 0 1 0 8.01 2.7Z"/>'
        '<path fill="#000" d="M8 2.7A5.3 5.3 0 0 1 8 13.3Z"/>',
    "preview-model-grid-symbolic": _strokes(
        _poly((4.2, 3.5), (11.8, 3.5), (15, 12.5), (1, 12.5))
        + [((2.6, 8), (13.4, 8)), ((6.7, 3.5), (5.6, 12.5)), ((9.3, 3.5), (10.4, 12.5))]),
    "preview-model-views-symbolic":
        '<path fill="#000" opacity="0.45" d="M8 1 14.5 4.5 8 8 1.5 4.5z"/>'
        '<path fill="#000" d="M1.5 5.4 7.5 8.8V15L1.5 11.6z"/>'
        '<path fill="#000" opacity="0.75" d="M14.5 5.4 8.5 8.8V15l6-3.4z"/>',
    # not in Adwaita: the PDF Markup Toolbar button and the notes sidebar tab
    "marker-symbolic":
        '<path fill="#000" d="M10.6 1.2a1 1 0 0 1 1.4 0l2.8 2.8a1 1 0 0 1 0 1.4L7.5 12.7 3.3 8.5z'
        'M2.6 9.3l4.1 4.1-1.4 1.1H1.5v-2.1z"/>',
    "chat-symbolic":
        '<path fill="#000" d="M3 2h10a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H7.5L4 15v-3H3a2 2 0 0 1-2-2V4'
        'a2 2 0 0 1 2-2z"/>',
}


def install():
    try:
        base = os.path.join(GLib.get_user_cache_dir(), "view", "icons")
        d = os.path.join(base, "hicolor", "scalable", "actions")
        os.makedirs(d, exist_ok=True)
        for name, body in _ICONS.items():
            path = os.path.join(d, name + ".svg")
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                   f'viewBox="0 0 16 16">{body}\n</svg>\n')
            try:
                with open(path) as fh:
                    if fh.read() == svg:
                        continue
            except OSError:
                pass
            with open(path, "w") as fh:
                fh.write(svg)
        display = Gdk.Display.get_default()
        if display is not None:
            theme = Gtk.IconTheme.get_for_display(display)
            if base not in theme.get_search_path():
                theme.add_search_path(base)
    except Exception as exc:  # noqa: BLE001 - icons are cosmetic
        if os.environ.get("PREVIEW_DEBUG"):
            print("[icons] install failed:", exc, file=sys.stderr, flush=True)
