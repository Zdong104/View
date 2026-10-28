# View

**A macOS-Preview-style viewer for Linux (GNOME) and Windows, also available as a Chrome extension.** View, annotate and sign PDFs, images, CAD drawings, 3D models, Markdown, tables and video in one fast GTK4 + libadwaita app, sign with your laptop touchpad, and translate documents offline.

[![License: PolyForm Noncommercial](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue)](LICENSE)
[![Release](https://img.shields.io/github/v/release/Zdong104/View?include_prereleases)](https://github.com/Zdong104/View/releases)
[![Downloads](https://img.shields.io/github/downloads/Zdong104/View/total)](https://github.com/Zdong104/View/releases)
[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-red)](README.zh-CN.md)

![A PDF translated side by side into Chinese, offline](docs/screenshots/translate-pdf.png)

## What's new in 0.2.0

- **Offline translation.** Translate a selection in a floating card, or a whole document side by side: PDFs, Markdown, text, code and CSV files. Runs on your computer; nothing is sent anywhere.
- **DXF drawings (2D CAD)** with a layers panel, zoom and pan, dark mode and export to PDF, SVG or PNG.
- **Markdown preview** with a Source / Split / Preview switch, updated live while you edit.
- **Faster start**: images open without loading the heavy libraries, and the update check waits until the window is shown.
- **Windows: updating from 0.1.6–0.1.9 is fixed.** Setup could stop silently because the background screenshot watcher kept View's files open; see [Windows](#windows-experimental) if an update still doesn't go through.
- Smaller fixes, for example YAML files are highlighted again instead of failing on some token types, and turning off Screenshots in Preferences now really stops the background watcher on Windows.

## Screenshots

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/translate-card.png" alt="Translating a selected paragraph in a floating card"><br><sub>Translate a selection in a card you can move and resize</sub></td>
    <td width="50%"><img src="docs/screenshots/translate-markdown.png" alt="A Markdown file translated into Spanish, structure kept"><br><sub>Markdown translated with its structure kept</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/translate-code.png" alt="A Python file with its comments and docstrings translated"><br><sub>Code: only comments, docstrings and strings are translated</sub></td>
    <td><img src="docs/screenshots/markdown-split.png" alt="Markdown source and rendered preview side by side"><br><sub>Markdown in Split view</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/dxf.png" alt="A mechanical drawing in the DXF viewer"><br><sub>DXF drawings</sub></td>
    <td><img src="docs/screenshots/dxf-dark.png" alt="The DXF viewer in dark mode with the Layers panel open"><br><sub>Dark mode and the Layers panel</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/pdf-sign.png" alt="Signing a PDF in View"><br><sub>Signing a PDF</sub></td>
    <td><img src="docs/screenshots/image-markup.png" alt="Markup on an image"><br><sub>Markup on an image</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/model-3d.png" alt="3D model viewer"><br><sub>3D models</sub></td>
    <td><img src="docs/screenshots/table.png" alt="CSV viewer"><br><sub>CSV / TSV tables</sub></td>
  </tr>
</table>

## Features

### Documents

- **PDF**: fast on huge files (1,000-page and 600 MB scanned PDFs tested), thumbnails, search, text selection, form filling, print, Get Info.
- **Page editing**: select thumbnails and press Delete, reorder, rotate, insert, merge several PDFs, drag pages out. Ctrl+S with pages selected saves just those pages as a new PDF.
- **Export and protect**: export pages to images, password-encrypt, reduce file size.
- **Sign with the touchpad**: your touchpad becomes a 1:1 drawing surface, like macOS Preview. Signatures are stored as vectors. Mouse and pen work too.
- **Markup** on PDFs and images: sketch, shapes, arrows, text boxes, notes, highlight / underline / strikethrough.

### Images

- **Images**: zoom, pan, pinch, rotate, flip, crop, adjust colour, instant alpha, resize, EXIF/GPS info. Animated GIF / WebP / APNG keep their animation when edited.
- **Screenshots**: Print Screen (Linux) or Snipping Tool / Win+Print Screen (Windows) opens the capture in View for quick markup; Done saves it back to Screenshots and copies it to the clipboard.
- **Batch tools**: convert selected images, combine images into a PDF (also from the GNOME Files right-click menu).

### CAD and 3D

- **DXF drawings (2D CAD)**: lines, arcs, splines, text, dimensions, hatches and blocks with their layer colours, linetypes and lineweights. A Layers panel shows and hides layers (off and frozen layers start hidden); touchpad pan and pinch, mouse-wheel zoom at the pointer; colours adapt to dark mode. Get Info shows units, size, extents and layer / block / entity counts; Export As writes PDF, SVG or PNG. Also opens loosely written DXF files, such as CATIA or script exports without subclass markers or handles, that strict parsers refuse.
- **3D models**: STL / OBJ / PLY, orbit with the touchpad, dimensions / volume / area (10M triangles tested).

### Text, code and tables

- **Markdown**: rendered preview with a Source / Split / Preview switch (Preview by default, your choice is remembered), updated while you edit. Headings, tables, task lists, block quotes, highlighted code blocks, local images and clickable links (web, relative files, `#heading` anchors). No web engine needed, so it works the same on Windows.
- **Text and code** viewer and editor with syntax highlighting.
- **Tables**: CSV / TSV viewer and editor (1M rows).

### Translation and Live Text

- **Translate, offline**: select text and choose **Translate** to see the translation in a card next to the selection (move it, resize it, copy it), or choose **Translate Document…** to see the whole document side by side, original on the left, translation on the right, scrolling together. The translation pane can also move to a window of its own.
  - **PDF**: follows the page you are looking at and translates the next one ahead. Formulas and figures are left as they are. **Save** writes the whole document's translation.
  - **Markdown**: the structure is kept; only the prose is translated, code, links and markup stay untouched, and the result can be saved as `.md`.
  - **Code**: only comments, docstrings and strings are translated; the lines stay where they were.
  - **CSV / TSV**: same rows and columns; numbers, dates, IDs and codes are skipped. Save writes the translated table.
  - **Images and scanned pages** go through Live Text first (needs the OCR engine).
  - The source language is detected automatically; parts of the text already in the target language are kept as written.
  - 30 languages, including English, Spanish, French, German, Japanese and Chinese, using Argos Translate language packs on CTranslate2. Each language pair is downloaded the first time you use it. Pick the default target language, and remove downloaded packs, in **Preferences → Translation**.
  - Nothing leaves your computer. The engine is built into the snap and the Windows build; with the .deb or a source install, View offers to download it (about 45 MB) the first time you translate.
- **Live Text (OCR)**: select and copy text in images and scanned PDFs, export searchable PDFs (RapidOCR, CPU, English + Chinese).

### Media

- **Video and audio**, with hardware (VA-API) decoding on Linux.

### System integration

- **Quick Look for GNOME Files**: press Space to preview a file (replaces Sushi; opt-in).
- **Interface languages**: English and 简体中文 (Preferences → Language, or follow the system). Translations welcome, see [CONTRIBUTING.md](CONTRIBUTING.md#translations).
- **Updates**: View tells you about new releases and can install them for you (see [Updating](#updating)).

## Install

### Ubuntu / Debian (.deb)

Download [`view_0.2.0_all.deb`](https://github.com/Zdong104/View/releases/download/v0.2.0/view_0.2.0_all.deb) (or grab it from the [Releases page](https://github.com/Zdong104/View/releases)), then:

```bash
sudo apt install ./view_0.2.0_all.deb
```

Or in one go:

```bash
wget https://github.com/Zdong104/View/releases/download/v0.2.0/view_0.2.0_all.deb && sudo apt install ./view_0.2.0_all.deb
```

`apt` is recommended because it also installs the dependencies. If you prefer `dpkg`:

```bash
sudo dpkg -i view_0.2.0_all.deb && sudo apt -f install
```

### Snap (edge channel)

```bash
sudo snap install view --edge && sudo snap connect view:raw-input
```

The second half lets the sandboxed snap read the touchpad for touchpad signing; without it you can still sign with the mouse, a pen or a finger. If you already installed the snap, just run `sudo snap connect view:raw-input`. Like every install, touchpad signing also needs your user in the `input` group (`sudo usermod -aG input $USER`, then log out and back in).

From a terminal, start it with `snap run view` (plain `view` is usually Vim's read-only mode). The snap is sandboxed, so Space-bar Quick Look for GNOME Files is only available in the .deb / source install. The "Combine Images into PDF" right-click item in Files works with a one-time setup (it needs `python3-nautilus`; the last step restarts Files):

```bash
sudo apt install python3-nautilus && mkdir -p ~/.local/share/nautilus-python/extensions && snap run view --print-files-extension > ~/.local/share/nautilus-python/extensions/view-app.py && nautilus -q
```

The .deb installs that menu item for you when `python3-nautilus` is present (`apt` recommends it).

### Windows (**experimental**)

Download [`View-Setup-0.2.0.exe`](https://github.com/Zdong104/View/releases/download/v0.2.0/View-Setup-0.2.0.exe) (installer) or [`View-0.2.0-windows-portable.zip`](https://github.com/Zdong104/View/releases/download/v0.2.0/View-0.2.0-windows-portable.zip) (no install). It is tested on a real Windows 11 laptop, but feedback is still very welcome. Touchpad signing works like on Linux: press **Space** (or **Start**) and sign with one finger, no clicking. It needs a Precision Touchpad (most laptops since 2016); otherwise draw with the mouse or a pen.

**Updating from 0.1.6–0.1.9:** in those versions the background screenshot watcher kept View's files open, so Setup could stop without a message. The 0.2.0 installer stops the watcher itself, also when an older View starts the update. If an update still doesn't go through, end the background **View** process in Task Manager (or run `taskkill /F /IM View.exe`), then run `View-Setup-0.2.0.exe` again.

The **first start after installing** can take 20 seconds or more while Windows checks each new file (the build isn't code-signed yet, see below). Later starts take about a second.

**Known problem:** the Windows build isn't code-signed yet, so on PCs with **Smart App Control** turned on (or a work PC with an App Control policy) it stops at startup with *"DLL load failed … An Application Control policy has blocked this file"* ([#3](https://github.com/Zdong104/View/issues/3)). Signed builds fix this; until then it only runs where Smart App Control is off (Windows Security → App & browser control → Smart App Control settings). Without Smart App Control, Windows may show a SmartScreen warning instead: click **More info → Run anyway**.

Text recognition (OCR) and translation each run in a small helper program, `ocr\view-ocr.exe` and `translate\view-translate.exe`, which View starts in the background. Translation language packs are downloaded the first time a language pair is used.

### Chrome extension

View is also supported in Chrome as an extension.

1. Download [`view-chrome-0.2.0.zip`](https://github.com/Zdong104/View/releases/download/v0.2.0/view-chrome-0.2.0.zip) and unzip it.
2. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and choose the unzipped folder.
3. To open local files, click **Details** on View and turn on **Allow access to file URLs**.

### From source

```bash
git clone https://github.com/Zdong104/View.git
cd View
make run
```

`make install` adds this checkout to your app grid as **View (Dev)**, next to any snap or .deb copy; `make uninstall` removes it again (`make purge` also deletes its Python venv). `make ocr` and `make translate` add the optional OCR and translation engines to the venv.

### Updating

View checks GitHub for a new release once a day (turn it off in **Preferences → Updates**), and every time you open **About View** or **Check for Updates…** in the main menu. When there is one, it shows what's new and can install it for you:

| Installed with | Update |
| --- | --- |
| Windows installer | **Install and Restart** downloads the new installer, updates View in place and starts it again |
| .deb | **Install…** downloads the new .deb and installs it with `apt` (asks for your password) |
| Snap | The Snap Store updates View automatically; `sudo snap refresh view` updates now |
| Windows portable zip, source | **Download** opens the new download in your browser |

Downloads are checked against the release's `SHA256SUMS` before they are installed. From a terminal, `view-app --check-updates` tells you whether a newer version exists. Every version, including older ones, stays on the [Releases page](https://github.com/Zdong104/View/releases); install an older .deb with `sudo apt install --allow-downgrades ./view_<version>_all.deb`, or run an older Windows installer over the current one.

## Touchpad signing

On Windows it works out of the box with a Precision Touchpad: open **New Signature...** and press **Start**. While you sign, the mouse pointer is paused; press any key to finish. On Linux:

1. Add yourself to the `input` group (needed to read the touchpad directly): `sudo usermod -aG input $USER`, then log out and back in.
2. Open **New Signature...** from the app menu (or run `view-app --new-signature`).
3. Draw with one finger. Where you touch the pad is where the ink goes. Press any key to finish, then place it on a page.

## Compatibility

| System | Status |
| --- | --- |
| Ubuntu 26.04 | ✅ Tested daily |
| Ubuntu 25.10, Debian 13 | Installs and passes the self-test in a container|
| Ubuntu 24.04 | Tested on the snap install |
| Fedora, Arch, other distros | ❓ Untested |
| Windows | ✅ Tested, feedback wanted |
| Chrome extension | ✅ Tested |
| macOS | Use Mac Preview 🙂 |

## 🙏 Feedback wanted

View is developed and tested **only on Ubuntu 26.04 on a Lenovo ThinkPad P1 Gen 8**. Everything else is a guess, and your report is the most useful thing you can send.

- Tried it on your system? File a [**Platform report**](https://github.com/Zdong104/View/issues/new?template=platform_report.yml), even if everything works.
- Found a problem? Open a [**Bug report**](https://github.com/Zdong104/View/issues/new?template=bug_report.yml).
- Missing something? Open a [**Feature request**](https://github.com/Zdong104/View/issues/new?template=feature_request.yml).
- Questions and ideas: use [Discussions](https://github.com/Zdong104/View/discussions).

## Contributing

Pull requests are welcome, from typo fixes to new viewers. Read [CONTRIBUTING.md](CONTRIBUTING.md) for how to run from source, the project layout and the PR checklist. Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Contributors

- [@azkuang](https://github.com/azkuang): UI translation framework and the Simplified Chinese localization.

## Support the project

View is free. If it saves you time, you can support development:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/zdong)

GitHub Sponsors is coming soon.

## License

View is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE): free for personal, educational, research and other noncommercial use. Commercial use or distribution requires permission; open a GitHub issue or contact the maintainer to ask. Note that this makes View *source-available*, not OSI-approved open source.
