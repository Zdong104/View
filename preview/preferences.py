"""Preferences dialog: GNOME Files integration and the optional OCR engine."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import i18n, runtime  # noqa: E402
from .i18n import _  # noqa: E402


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title=_("Preferences"), search_enabled=False)
        self.app = app
        page = Adw.PreferencesPage(title=_("General"), icon_name="preferences-system-symbolic")
        self.add(page)

        ok, reason = runtime.files_integration_supported()
        if ok:
            page.add(self._files_group())
        elif runtime.IS_LINUX and runtime.SANDBOXED:
            grp = Adw.PreferencesGroup(title=_("GNOME Files"))
            grp.add(Adw.ActionRow(title=_("Space-bar previews and Combine Images into PDF"),
                                  subtitle=reason, subtitle_selectable=True))
            page.add(grp)

        from . import screenshot
        if screenshot.supported():
            page.add(self._screenshots_group())

        page.add(self._updates_group())
        page.add(self._language_group())

        self.ocr_group = Adw.PreferencesGroup(title=_("Text Recognition"))
        page.add(self.ocr_group)
        self._ocr_row = None
        self._fill_ocr()

    # ---- GNOME Files ----
    def _files_group(self):
        from . import integration
        grp = Adw.PreferencesGroup(
            title=_("GNOME Files"),
            description=_("Applies to your user only. Turning these off restores the defaults."))
        ql = Adw.SwitchRow(title=_("Use View for Space-bar previews in Files"),
                           subtitle=_("Replaces GNOME Sushi’s quick previewer"),
                           active=integration.quicklook_enabled())
        sc = Adw.SwitchRow(title=_("Add “Combine Images into PDF” to Files"),
                           subtitle=_("Right-click selected images; if it is missing, "
                                      "restart Files (nautilus -q)"),
                           active=integration.script_enabled())
        if integration.system_extension_installed():
            # the .deb's copy serves every user; nothing per user to turn off
            sc.set_subtitle(_("Installed for all users by the View package"))
            sc.set_sensitive(False)

        def toggled(row, _pspec, which):
            try:
                if which == "ql":
                    integration.set_quicklook(row.get_active())
                else:
                    integration.set_script(row.get_active())
            except OSError as e:
                self.add_toast(Adw.Toast(title=_("Could not change the setting: {error}").format(error=e)))
        ql.connect("notify::active", toggled, "ql")
        sc.connect("notify::active", toggled, "sc")
        grp.add(ql)
        grp.add(sc)
        return grp

    # ---- screenshots ----
    def _screenshots_group(self):
        from . import screenshot
        grp = Adw.PreferencesGroup(title=_("Screenshots"))
        row = Adw.SwitchRow(title=_("Edit screenshots after capture"),
                            subtitle=_("Press Print Screen and the screenshot opens in View for "
                                       "quick markup; Done saves it and copies it to the clipboard"),
                            active=screenshot.chosen())

        def toggled(r, _pspec):
            try:
                screenshot.set_enabled(r.get_active())
            except OSError as e:
                self.add_toast(Adw.Toast(title=_("Could not change the setting: {error}").format(error=e)))
        row.connect("notify::active", toggled)
        grp.add(row)
        return grp

    # ---- language ----
    def _language_group(self):
        codes = [i18n.SYSTEM] + [c for c, _name in i18n.LANGUAGES]
        names = [_("System default")] + [n for _c, n in i18n.LANGUAGES]
        row = Adw.ComboRow(title=_("Language"),
                           subtitle=_("Takes effect the next time View starts"),
                           model=Gtk.StringList.new(names))
        row.set_selected(codes.index(i18n.chosen()))

        def changed(r, _pspec):
            try:
                i18n.set_chosen(codes[r.get_selected()])
            except OSError as e:
                self.add_toast(Adw.Toast(
                    title=_("Could not change the setting: {error}").format(error=e)))
        row.connect("notify::selected", changed)
        grp = Adw.PreferencesGroup(title=_("Language"))
        grp.add(row)
        return grp

    # ---- updates ----
    def _updates_group(self):
        from . import updates
        grp = Adw.PreferencesGroup(title=_("Updates"))
        if updates.install_kind() == "snap":
            grp.add(Adw.ActionRow(title=_("Updates come from the Snap Store"),
                                  subtitle=_("snapd installs new versions automatically")))
        else:
            row = Adw.SwitchRow(title=_("Check for updates automatically"),
                                subtitle=_("Once a day, from GitHub"),
                                active=updates.auto_check_enabled())
            row.connect("notify::active", lambda r, _p: updates.set_auto_check(r.get_active()))
            grp.add(row)
            check = Adw.ButtonRow(title=_("Check for Updates Now")) \
                if hasattr(Adw, "ButtonRow") else None
            if check is not None:
                check.connect("activated", lambda *_: self.app._get_updater().check_now(
                    self.app._any_window(), toast=self))
                grp.add(check)
        versions = Adw.ActionRow(title=_("All versions"), subtitle=_("Download any release, "
                                 "including older ones"), activatable=True)
        versions.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        versions.connect("activated", lambda *_: Gtk.UriLauncher.new(
            updates.RELEASES).launch(self.app._any_window(), None, None, None))
        grp.add(versions)
        return grp

    # ---- OCR ----
    def _fill_ocr(self):
        from . import ocr, ocr_install
        if self._ocr_row is not None:
            self.ocr_group.remove(self._ocr_row)
        if ocr.is_available():
            sub = _("Included with this build") if runtime.OCR_BUNDLED else _("Installed")
            if ocr_install._activated:
                sub = _("Installed in {path}").format(path=ocr_install.venv_dir().replace(
                    os.path.expanduser("~"), "~", 1))
            row = Adw.ActionRow(title=_("RapidOCR engine"), subtitle=sub)
            row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
        else:
            possible, reason = ocr_install.can_install()
            row = Adw.ActionRow(title=_("RapidOCR engine"),
                                subtitle=(_("Not installed ({size} download)").format(size=ocr_install.DOWNLOAD_SIZE)
                                          if possible else reason))
            if possible:
                btn = Gtk.Button(label=_("Install…"), valign=Gtk.Align.CENTER,
                                 css_classes=["suggested-action"])
                btn.connect("clicked", lambda *_: ocr_install.offer(
                    self, lambda: GLib.idle_add(lambda: (self._fill_ocr(), False)[1])))
                row.add_suffix(btn)
        self._ocr_row = row
        self.ocr_group.add(row)
