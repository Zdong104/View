"""Preferences dialog: GNOME Files integration and the optional OCR and
translation engines."""
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

        from . import instance
        if instance.supported():
            page.add(self._background_group())

        page.add(self._updates_group())
        page.add(self._language_group())

        self.ocr_group = Adw.PreferencesGroup(title=_("Text Recognition"))
        page.add(self.ocr_group)
        self._ocr_row = None
        self._fill_ocr()

        self.tr_group = Adw.PreferencesGroup(
            title=_("Translation"),
            description=_("Translation runs on this computer: text is never sent to a server."))
        page.add(self.tr_group)
        self._tr_rows = []
        self._fill_translate()

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

    # ---- background ----
    def _background_group(self):
        from . import instance, tray
        grp = Adw.PreferencesGroup(title=_("Background"))
        choice = instance.background_choice()
        if runtime.IS_WINDOWS:
            sub = _("Files then open faster. Quit from View’s icon in the notification area "
                    "or with Ctrl+Q")
        else:
            sub = _("Files then open faster. Quit from View’s icon in the top bar or with Ctrl+Q")
        if choice is None and not tray.available():
            sub += "\n" + _("Off by default here: the desktop shows no tray icons "
                            "(on GNOME, the AppIndicator extension adds them)")
        row = Adw.SwitchRow(title=_("Keep View running in the background when windows are closed"),
                            subtitle=sub,
                            active=tray.available() if choice is None else choice)

        def toggled(r, _pspec):
            try:
                instance.set_background_choice(r.get_active())
            except OSError as e:
                self.add_toast(Adw.Toast(
                    title=_("Could not change the setting: {error}").format(error=e)))
                return
            self.app.background_changed()
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

    # ---- translation ----
    def _fill_translate(self):
        from . import translate, translate_install
        for r in self._tr_rows:
            self.tr_group.remove(r)
        rows = []
        target = Adw.ComboRow(title=_("Translate into"),
                              model=Gtk.StringList.new(
                                  [translate.language_name(c) for c in translate.CODES]))
        target.set_selected(translate.CODES.index(translate.target()))

        def changed(r, _pspec):
            try:
                translate.set_target(translate.CODES[r.get_selected()])
            except OSError as e:
                self.add_toast(Adw.Toast(
                    title=_("Could not change the setting: {error}").format(error=e)))
        target.connect("notify::selected", changed)
        rows.append(target)

        refill = lambda: GLib.idle_add(lambda: (self._fill_translate(), False)[1])  # noqa: E731
        rows += self._model_rows(refill)
        if translate.engine_available():
            sub = _("Included with this build") if runtime.OCR_BUNDLED else _("Installed")
            if translate_install._activated:
                sub = _("Installed in {path}").format(path=translate_install.venv_dir().replace(
                    os.path.expanduser("~"), "~", 1))
            row = Adw.ActionRow(title=_("CTranslate2 engine"), subtitle=sub)
            row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
        else:
            possible, reason = translate_install.can_install()
            row = Adw.ActionRow(title=_("CTranslate2 engine"),
                                subtitle=(_("Not installed ({size} download)").format(
                                    size=translate_install.DOWNLOAD_SIZE) if possible else reason))
            if possible:
                btn = Gtk.Button(label=_("Install…"), valign=Gtk.Align.CENTER,
                                 css_classes=["suggested-action"])
                btn.connect("clicked", lambda *_: translate_install.offer(self, refill))
                row.add_suffix(btn)
        rows.append(row)

        packs = [p for p in translate.installed() if p != translate.MODEL_PACK]
        if packs:
            names = ", ".join(translate.pack_name(p) for p in packs)
            sub = f"{names} ({GLib.format_size(translate.installed_size(model=False))})"
        else:
            sub = _("None yet: each language pair is downloaded the first time it is used")
        row = Adw.ActionRow(title=_("Language packs"), subtitle=sub)
        if packs:
            btn = Gtk.Button(label=_("Remove All…"), valign=Gtk.Align.CENTER,
                             css_classes=["destructive-action"])

            def remove(*_a):
                dlg = Adw.AlertDialog(heading=_("Remove Language Packs?"),
                                      body=_("They are downloaded again when needed."))
                dlg.add_response("cancel", _("Cancel"))
                dlg.add_response("remove", _("Remove"))
                dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
                dlg.set_close_response("cancel")
                dlg.connect("response", lambda _d, rid: (translate.remove_packs(), refill())
                            if rid == "remove" else None)
                dlg.present(self)
            btn.connect("clicked", remove)
            row.add_suffix(btn)
        rows.append(row)
        for r in rows:
            self.tr_group.add(r)
        self._tr_rows = rows

    def _model_rows(self, refill):
        """Model: Automatic / Fast / High quality, and the high-quality
        model's row: the device it runs on, its size and Remove."""
        from . import translate, translate_llm
        if not translate_llm.supported():
            return []
        choices = [translate.MODEL_AUTO, translate.MODEL_FAST, translate.MODEL_QUALITY]
        about = {
            translate.MODEL_AUTO: _("High quality with a dedicated GPU, otherwise Fast"),
            translate.MODEL_FAST: _("Small, works on any computer, lower quality "
                                    "(Argos, 70–300 MB per language pair)"),
            translate.MODEL_QUALITY: _("Hy-MT2 1.8B, 1.2 GB, best with a GPU (23 languages; "
                                       "the others use Fast)"),
        }
        choice = translate.model_choice()
        model = Adw.ComboRow(title=_("Model"), subtitle=about[choice],
                             model=Gtk.StringList.new([_("Automatic"), _("Fast"),
                                                       _("High quality")]))
        model.set_selected(choices.index(choice))

        def changed(r, _pspec):
            try:
                translate.set_model_choice(choices[r.get_selected()])
            except OSError as e:
                self.add_toast(Adw.Toast(
                    title=_("Could not change the setting: {error}").format(error=e)))
            refill()
        model.connect("notify::selected", changed)

        have = translate_llm.installed()
        state = (_("Downloaded ({size})").format(size=GLib.format_size(translate_llm.size()))
                 if have else _("Not downloaded yet ({size}, on first use)").format(
                     size=GLib.format_size(translate_llm.download_size())))
        row = Adw.ActionRow(title=_("High-quality model"),
                            subtitle=_("Checking for a GPU…") + "\n" + state)
        if have:
            btn = Gtk.Button(label=_("Remove…"), valign=Gtk.Align.CENTER,
                             css_classes=["destructive-action"])

            def remove(*_a):
                dlg = Adw.AlertDialog(heading=_("Remove the High-Quality Model?"),
                                      body=_("It is downloaded again when needed."))
                dlg.add_response("cancel", _("Cancel"))
                dlg.add_response("remove", _("Remove"))
                dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
                dlg.set_close_response("cancel")
                dlg.connect("response", lambda _d, rid: (translate_llm.remove(), refill())
                            if rid == "remove" else None)
                dlg.present(self)
            btn.connect("clicked", remove)
            row.add_suffix(btn)

        def probe():                     # (llama.cpp lists the Vulkan devices: ~1 s)
            dev = translate_llm.device()
            if dev["kind"] == "gpu":
                text = _("Runs on the GPU: {name}").format(name=dev["name"])
            elif dev["why"] == "forced":
                text = _("Runs on the processor (VIEW_TRANSLATE_CPU is set)")
            elif dev["why"] == "failed":
                text = _("The GPU could not run it, so it runs on the processor: "
                         "expect minutes per document")
            else:
                text = _("No GPU found: it runs on the processor, slowly "
                         "(minutes per document)")
            GLib.idle_add(lambda: (row.set_subtitle(text + "\n" + state), False)[1])
        import threading
        threading.Thread(target=probe, daemon=True, name="translate-gpu").start()
        return [model, row]
