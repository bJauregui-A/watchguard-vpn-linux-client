# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""The "new_profile" page: add a domain, or edit an existing one's login
mode/SAML group. Owns the "Fetch automatically" background fetch and the
manual folder-picker fallback; hands a plain (domain, auth_mode,
saml_auth_group, editing) tuple up to the caller on save rather than a
full profile dict, since the profile list itself is owned by
watchguard-vpn-gui.py.
"""
import os
import tempfile
import threading
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, GLib, WebKit2  # noqa: E402

import vpn_network
import vpn_profiles
from ui_saml_common import SamlWebViewController


class NewProfileView:
    def __init__(self, on_back, on_save, domain_exists):
        self._on_back = on_back
        self._on_save = on_save
        self._domain_exists = domain_exists

        self._editing_domain: Optional[str] = None
        self._cert_folder: Optional[str] = None
        self._login_fallback_domain: Optional[str] = None  # non-None while the embedded login-first-then-retry flow (see below) is active

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.widget.set_border_width(24)

        self.form_title = Gtk.Label(label="New domain")
        self.form_title.get_style_context().add_class("title")
        self.widget.pack_start(self.form_title, False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=8)
        self.widget.pack_start(grid, False, False, 0)

        grid.attach(Gtk.Label(label="Domain (e.g. vpn.example.com):", xalign=0), 0, 0, 1, 1)
        self.domain_entry = Gtk.Entry()
        grid.attach(self.domain_entry, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Login mode:", xalign=0), 0, 1, 1, 1)
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.saml_radio = Gtk.RadioButton.new_with_label_from_widget(None, "SAML (web SSO)")
        self.cred_radio = Gtk.RadioButton.new_with_label_from_widget(
            self.saml_radio, "Credentials (username/password)"
        )
        mode_box.pack_start(self.saml_radio, False, False, 0)
        mode_box.pack_start(self.cred_radio, False, False, 0)
        grid.attach(mode_box, 1, 1, 1, 1)

        saml_group_label = Gtk.Label(label="SAML auth group\n(leave empty if unsure):", xalign=0)
        saml_group_label.set_tooltip_text(
            "Auto-filled by \"Fetch automatically\" below, if the "
            "Firebox reports one -- edit it here only if that didn't "
            "run, got it wrong, or you need to override it. It's a "
            "server-side name, not part of the certificates. Wrong (or "
            "wrongly empty when needed) fails with AUTH_FAILED even "
            "with valid certs and a valid SAML login."
        )
        grid.attach(saml_group_label, 0, 2, 1, 1)
        self.saml_group_entry = Gtk.Entry()
        self.saml_group_entry.set_placeholder_text("e.g. Corp_SAML")
        self.saml_group_entry.set_tooltip_text(saml_group_label.get_tooltip_text())
        grid.attach(self.saml_group_entry, 1, 2, 1, 1)

        grid.attach(Gtk.Label(label="Certificates (ca.crt / client.crt / client.pem):", xalign=0), 0, 3, 1, 1)
        cert_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.cert_path_label = Gtk.Label(label="(none chosen)")
        self.cert_path_label.get_style_context().add_class("dim-label")
        self.cert_fetch_btn = Gtk.Button(label="Fetch automatically")
        self.cert_fetch_btn.connect("clicked", self._on_fetch_cert_clicked)
        cert_choose_btn = Gtk.Button(label="Choose folder...")
        cert_choose_btn.connect("clicked", self._on_choose_cert_folder)
        cert_box.pack_start(self.cert_path_label, True, True, 0)
        cert_box.pack_start(self.cert_fetch_btn, False, False, 0)
        cert_box.pack_start(cert_choose_btn, False, False, 0)
        grid.attach(cert_box, 1, 3, 1, 1)

        hint = Gtk.Label(
            label=(
                "\"Fetch automatically\" downloads the certificates AND "
                "detects the login mode/SAML auth group, straight from "
                "the Firebox (same unauthenticated endpoints the "
                "official client uses on first add) -- no manual "
                "extraction needed. \"Choose folder...\" is the "
                "fallback for the certificates: those 3 files pulled "
                "once from a Windows install of the official client, "
                "under %AppData%\\WatchGuard\\Mobile VPN\\, if automatic "
                "fetch doesn't work for some reason."
            )
        )
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("dim-label")
        self.widget.pack_start(hint, False, False, 0)

        self.error_label = Gtk.Label(label="")
        self.error_label.set_line_wrap(True)
        self.widget.pack_start(self.error_label, False, False, 0)

        # Fallback shown only when an anonymous "Fetch automatically" fails
        # on a SAML-enabled Firebox: found (2026-09-01, MITM-comparing a
        # real official-client capture against our own request byte-for-
        # byte) that at least one real Firebox now requires a just-completed
        # login from the same client before it will serve the cert bundle,
        # something it didn't need when this endpoint was first reverse-
        # engineered -- likely tightened as a direct response to exactly the
        # "unauthenticated cert pull" concern this project's own README
        # flags. A login here doesn't need to be kept or used for anything
        # afterward -- its only purpose is to unlock the retry below; the
        # real connection later does its own fresh login regardless.
        self.login_fallback_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        login_fallback_label = Gtk.Label(
            label=(
                "This server didn't allow fetching certificates "
                "anonymously. Some Fireboxes require a login first -- log "
                "in below and the fetch will retry automatically."
            )
        )
        login_fallback_label.set_line_wrap(True)
        self.login_webview = WebKit2.WebView()
        self.login_webview.set_size_request(-1, 320)
        self._login_controller = SamlWebViewController(self.login_webview, self._on_login_success)
        self.login_fallback_box.pack_start(login_fallback_label, False, False, 0)
        self.login_fallback_box.pack_start(self.login_webview, True, True, 0)
        self.login_fallback_box.set_no_show_all(True)
        self.login_fallback_box.hide()
        self.widget.pack_start(self.login_fallback_box, True, True, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_btn = Gtk.Button(label="Back")
        back_btn.connect("clicked", lambda _b: self._on_back())
        save_btn = Gtk.Button(label="Save")
        save_btn.connect("clicked", self._on_save_clicked)
        btn_box.pack_start(back_btn, False, False, 0)
        btn_box.pack_start(save_btn, False, False, 0)
        self.widget.pack_start(btn_box, False, False, 0)

    def reset(
        self,
        *,
        title: str,
        editing_domain: Optional[str] = None,
        domain_text: str = "",
        domain_editable: bool = True,
        saml_active: bool = True,
        saml_group_text: str = "",
        cert_label: str = "(none chosen)",
    ) -> None:
        """Shared reset for the add/edit-domain form's fields -- used by
        both the "+ Add new domain" and "Edit" entry points, and again
        after a successful save to leave the form blank for next time."""
        self._editing_domain = editing_domain
        self.form_title.set_text(title)
        self.domain_entry.set_text(domain_text)
        # The domain is part of the on-disk cert path and every generated
        # config/route -- renaming it here would silently orphan the
        # existing profiles/<domain>/ folder, so editing is scoped to the
        # other fields only (domain_editable=False).
        self.domain_entry.set_sensitive(domain_editable)
        if saml_active:
            self.saml_radio.set_active(True)
        else:
            self.cred_radio.set_active(True)
        self.saml_group_entry.set_text(saml_group_text)
        self._cert_folder = None
        self.cert_path_label.set_text(cert_label)
        self.cert_fetch_btn.set_sensitive(True)
        self.error_label.set_text("")
        self._hide_login_fallback()

    def _on_choose_cert_folder(self, _button) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Choose the folder with ca.crt / client.crt / client.pem",
            parent=self.widget.get_toplevel(),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        if dialog.run() == Gtk.ResponseType.OK:
            self._cert_folder = dialog.get_filename()
            self.cert_path_label.set_text(self._cert_folder)
        dialog.destroy()

    def _on_fetch_cert_clicked(self, _button) -> None:
        domain = self.domain_entry.get_text().strip()
        if not domain:
            self.error_label.set_text("Enter the domain first.")
            return
        self.error_label.set_text("")
        self.cert_fetch_btn.set_sensitive(False)
        self.cert_path_label.set_text("Fetching...")
        threading.Thread(
            target=self._fetch_cert_worker, args=(domain,), daemon=True
        ).start()

    @staticmethod
    def _run_cert_fetch(domain: str):
        """Fetches login-config (best-effort) then the cert bundle for
        domain, in the documented real order -- MITM-capturing the real
        official client's own traffic against a real Firebox showed it
        always hits the lightweight login-config/status check FIRST and
        only requests the cert bundle after, never the other way around.
        Matching that order isn't sufficient by itself to fix a 502 seen
        from one specific Firebox (see the login_fallback_box comment
        below for what was), but it's the documented real order
        regardless. Shared by the initial attempt and the login-fallback
        retry below -- they used to each have their own near-identical
        copy of this, in two different (inconsistent) orders.

        Returns (staging_dir, error, login_config) -- staging_dir is None
        iff error is set. Runs on a background thread; callers marshal
        the result back via GLib.idle_add themselves."""
        try:
            login_config = vpn_network.fetch_login_config(domain)
        except Exception:
            # Best-effort: the SAML auth group comes from this separate,
            # unrelated endpoint. A cert-only Firebox (SAML not configured)
            # is a normal case, not a failure -- don't fail the whole fetch
            # over it, just leave the group field alone if this errors.
            login_config = None

        staging_dir = tempfile.mkdtemp(prefix="watchguard-vpn-fetch-")
        try:
            vpn_network.fetch_certificates_to_dir(domain, staging_dir)
        except Exception as e:
            return None, str(e), login_config
        return staging_dir, None, login_config

    def _fetch_cert_worker(self, domain: str) -> None:
        staging_dir, error, login_config = self._run_cert_fetch(domain)
        if error:
            # Some Fireboxes now reject this anonymous request but accept
            # it right after a real login from the same client (see the
            # login_fallback_box comment above) -- worth trying that before
            # giving up, but only for SAML domains, since that's the only
            # login flow we can drive ourselves without asking for
            # credentials mid-fetch.
            if login_config and login_config.get("saml_enabled"):
                GLib.idle_add(self._start_login_fallback, domain)
            else:
                GLib.idle_add(self._on_fetch_cert_done, domain, None, error, None)
            return
        GLib.idle_add(self._on_fetch_cert_done, domain, staging_dir, None, login_config)

    def _retry_cert_fetch_after_login(self, domain: str) -> None:
        """Same as _fetch_cert_worker, but called after the login-fallback
        webview reports a completed login -- no need to re-check
        saml_enabled or fall back again, this IS the fallback."""
        staging_dir, error, login_config = self._run_cert_fetch(domain)
        GLib.idle_add(self._on_fetch_cert_done, domain, staging_dir, error, login_config)

    def _on_fetch_cert_done(self, domain: str, staging_dir, error, login_config) -> None:
        self.cert_fetch_btn.set_sensitive(True)
        # The user may have edited the domain field while this was in
        # flight -- if it no longer matches what we fetched for, discard
        # the result rather than silently applying it to the wrong domain.
        if self.domain_entry.get_text().strip() != domain:
            self.cert_path_label.set_text("(none chosen)")
            return
        if error:
            self.cert_path_label.set_text("(none chosen)")
            self.error_label.set_text(f"Automatic fetch failed: {error}")
            return
        self._cert_folder = staging_dir
        self.cert_path_label.set_text(f"Fetched automatically from {domain}")

        if login_config:
            if login_config["saml_enabled"]:
                self.saml_radio.set_active(True)
            elif login_config["auth_domains"]:
                self.cred_radio.set_active(True)
            if login_config["saml_idp_name"]:
                self.saml_group_entry.set_text(login_config["saml_idp_name"])

    # ---------- login-first fallback (see login_fallback_box comment above) ----------

    def _start_login_fallback(self, domain: str) -> None:
        self._login_fallback_domain = domain
        self.cert_path_label.set_text("(login required, see below)")
        self.error_label.set_text("")
        self.login_fallback_box.set_no_show_all(False)
        self.login_fallback_box.show_all()
        self._login_controller.load_login(domain)

    def _on_login_success(self, qs: dict) -> None:
        # We don't need anything from this login (no token/user parsing --
        # SamlWebViewController already confirmed result=success and that
        # the redirect genuinely came from the Firebox) -- its only job
        # was to unlock the retry below. Fire that retry immediately, on a
        # fresh request, while whatever this unlocked is still fresh
        # (found empirically: leaving a connection idle for the minutes a
        # real interactive login can take was long enough for it to time
        # out server-side before a retry on it got attempted).
        domain = self._login_fallback_domain
        self._hide_login_fallback()
        self.cert_path_label.set_text("Login OK -- fetching certificates...")
        threading.Thread(
            target=self._retry_cert_fetch_after_login, args=(domain,), daemon=True
        ).start()

    def _hide_login_fallback(self) -> None:
        self._login_controller.stop_and_blank()
        self.login_fallback_box.hide()
        self._login_fallback_domain = None

    def _on_save_clicked(self, _button) -> None:
        domain = self.domain_entry.get_text().strip()
        editing = self._editing_domain is not None
        if not domain:
            self.error_label.set_text("Domain is missing.")
            return
        if not editing and self._domain_exists(domain):
            self.error_label.set_text("A profile for that domain already exists.")
            return
        if not editing and not self._cert_folder:
            self.error_label.set_text("Choose the certificate folder first.")
            return

        # In edit mode the cert folder is optional (existing certs are
        # kept as-is unless a new folder is picked); in add mode it's
        # already required above, so this always runs there.
        if self._cert_folder:
            missing = [
                name for name in vpn_network.CERT_FILES
                if not os.path.isfile(os.path.join(self._cert_folder, name))
            ]
            if missing:
                self.error_label.set_text(
                    f"Missing in that folder: {', '.join(missing)}"
                )
                return

            dest_dir = vpn_profiles.profile_cert_dir(domain)
            os.makedirs(dest_dir, exist_ok=True)
            for name in vpn_network.CERT_FILES:
                src = os.path.join(self._cert_folder, name)
                dst = os.path.join(dest_dir, name)
                with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                    fdst.write(fsrc.read())
            os.chmod(os.path.join(dest_dir, "client.pem"), 0o600)

        auth_mode = "saml" if self.saml_radio.get_active() else "credentials"
        saml_auth_group = self.saml_group_entry.get_text().strip()

        self._on_save(domain, auth_mode, saml_auth_group, editing)
