#!/usr/bin/env python3
"""Print the release notes for VERSION (from the AppStream metainfo) as
Markdown, for the GitHub release body.  View's updater shows this text.

    packaging/release-notes.py 0.1.7
"""
import os
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
META = os.path.join(ROOT, "data", "io.github.zdong104.View.metainfo.xml")


def text(el):
    return " ".join("".join(el.itertext()).split())


def notes(version):
    for rel in ET.parse(META).getroot().iter("release"):
        if rel.get("version") != version:
            continue
        out = []
        desc = rel.find("description")
        for el in (desc if desc is not None else []):
            if el.tag == "p":
                out += [text(el), ""]
            elif el.tag in ("ul", "ol"):
                out += [f"- {text(li)}" for li in el.findall("li")] + [""]
        return "\n".join(out).strip()
    return ""


if __name__ == "__main__":
    version = sys.argv[1].lstrip("v")
    body = notes(version)
    print((body + "\n\n" if body else "") +
          "Downloads: .deb (Ubuntu 25.10+/26.04, Debian 13), .snap, Windows installer and "
          "portable zip, Chrome extension (view-chrome-*.zip). Verify with SHA256SUMS. "
          "Older versions are on the [Releases page](https://github.com/Zdong104/View/releases).")
