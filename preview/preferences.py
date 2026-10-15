"""Preferences dialog: GNOME Files integration and the optional OCR engine."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import runtime  # noqa: E402


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title="Preferences", search_enabled=False)
        self.app = app
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        self.add(page)

        ok, reason = runtime.files_integration_supported()
        if ok:
            page.add(self._files_group())
        elif runtime.IS_LINUX and runtime.SANDBOXED:
            grp = Adw.PreferencesGroup(title="GNOME Files")
            grp.add(Adw.ActionRow(title="Space-bar previews and Files scripts",
                                  subtitle=reason, subtitle_selectable=True))
            page.add(grp)

        self.ocr_group = Adw.PreferencesGroup(title="Text Recognition")
        page.add(self.ocr_group)
        self._ocr_row = None
        self._fill_ocr()

    # ---- GNOME Files ----
    def _files_group(self):
        from . import integration
        grp = Adw.PreferencesGroup(
            title="GNOME Files",
            description="Applies to your user only. Turning these off restores the defaults.")
        ql = Adw.SwitchRow(title="Use View for Space-bar previews in Files",
                           subtitle="Replaces GNOME Sushi’s quick previewer",
                           active=integration.quicklook_enabled())
        sc = Adw.SwitchRow(title="Add “Combine Images into PDF” to Files",
                           subtitle="Right-click selected images → Scripts",
                           active=integration.script_enabled())

        def toggled(row, _pspec, which):
            try:
                if which == "ql":
                    integration.set_quicklook(row.get_active())
                else:
                    integration.set_script(row.get_active())
            except OSError as e:
                self.add_toast(Adw.Toast(title=f"Could not change the setting: {e}"))
        ql.connect("notify::active", toggled, "ql")
        sc.connect("notify::active", toggled, "sc")
        grp.add(ql)
        grp.add(sc)
        return grp

    # ---- OCR ----
    def _fill_ocr(self):
        from . import ocr, ocr_install
        if self._ocr_row is not None:
            self.ocr_group.remove(self._ocr_row)
        if ocr.is_available():
            sub = "Included with this build" if runtime.OCR_BUNDLED else "Installed"
            if ocr_install._activated:
                sub = "Installed in " + ocr_install.venv_dir().replace(
                    os.path.expanduser("~"), "~", 1)
            row = Adw.ActionRow(title="RapidOCR engine", subtitle=sub)
            row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
        else:
            possible, reason = ocr_install.can_install()
            row = Adw.ActionRow(title="RapidOCR engine",
                                subtitle=(f"Not installed ({ocr_install.DOWNLOAD_SIZE} download)"
                                          if possible else reason))
            if possible:
                btn = Gtk.Button(label="Install…", valign=Gtk.Align.CENTER,
                                 css_classes=["suggested-action"])
                btn.connect("clicked", lambda *_: ocr_install.offer(
                    self, lambda: GLib.idle_add(lambda: (self._fill_ocr(), False)[1])))
                row.add_suffix(btn)
        self._ocr_row = row
        self.ocr_group.add(row)
