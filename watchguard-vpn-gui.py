#!/usr/bin/env python3
# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""
WatchGuard VPN (Mobile VPN with SSL) -- native GUI client for Linux.

Supports any WatchGuard/Fireware domain. Each domain is saved as a
"profile": the Firebox's certificates (ca/cert/key, which you need to pull
once from a Windows install of the official client, under
%AppData%\\WatchGuard\\Mobile VPN\\), and whether login is SAML (embedded
browser) or direct credentials (Firebox username/password).

Everything happens in a single window:
  1. Pick an already-saved profile (domain), or add a new one.
  2. If SAML: an embedded browser opens with the login. If credentials: a
     simple username/password form.
  3. The app assembles the credentials and brings up the VPN (asking for
     the admin password via pkexec's graphical dialog).
  4. Disconnect button, test-connection button, live log.

This file is the UI layer only (GTK + the embedded WebKit2 browser for
SAML) -- it wires together the non-GTK modules that do the actual work:
  vpn_profiles.py  profile storage
  vpn_routes.py    route capture/cleanup around the tunnel
  vpn_network.py   wifi health check + the "Test connection" ping check
  vpn_process.py   the OpenVPN subprocess itself

Built with Claude Code (https://claude.com/claude-code).

Requires: gtk3, webkit2gtk (4.1), polkit (pkexec), openvpn.
"""
import os
import tempfile
import threading
from typing import Optional
from urllib.parse import urlparse, parse_qs

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, GLib, Gio  # noqa: E402

import vpn_network
import vpn_profiles
import vpn_routes
from vpn_process import VpnProcess


class VpnWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="WatchGuard VPN")
        self.set_default_size(560, 780)
        self.set_icon_name("network-vpn")
        self.connect("destroy", self.on_destroy)

        self.active_profile = None
        self.profiles = vpn_profiles.load_profiles()
        self._saml_login_cancellable = None  # guards against stale domain switches, see _clear_saml_cookie_and_load_login
        self._np_editing_domain = None  # non-None while the new/edit-domain form is editing an existing profile

        self.vpn = VpnProcess(
            on_log_line=self.append_log_line,
            on_status=self.set_status,
            on_exited=self.on_vpn_exited,
        )

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.add(self.stack)

        self._build_profiles_page()
        self._build_new_profile_page()
        self._build_saml_login_page()
        self._build_credentials_login_page()
        self._build_status_page()

        self.stack.set_visible_child_name("profiles")
        self._refresh_profiles_list()

    # ================= page: pick profile =================

    def _build_profiles_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(24)

        title = Gtk.Label(label="Choose a domain")
        title.get_style_context().add_class("title")
        box.pack_start(title, False, False, 0)

        self.profiles_list_box = Gtk.ListBox()
        self.profiles_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        box.pack_start(self.profiles_list_box, True, True, 0)

        add_button = Gtk.Button(label="+ Add new domain")
        add_button.connect("clicked", self._on_add_new_domain_clicked)
        box.pack_start(add_button, False, False, 0)

        self.stack.add_named(box, "profiles")

    def _refresh_profiles_list(self) -> None:
        for child in list(self.profiles_list_box.get_children()):
            self.profiles_list_box.remove(child)
        if not self.profiles:
            empty = Gtk.Label(label="No domains configured yet.")
            empty.get_style_context().add_class("dim-label")
            row = Gtk.ListBoxRow()
            row.add(empty)
            row.set_selectable(False)
            row.set_activatable(False)
            self.profiles_list_box.add(row)
        else:
            for profile in self.profiles:
                row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row_box.set_border_width(6)
                label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                label = Gtk.Label(label=profile.get("label") or profile["domain"])
                label.set_xalign(0)
                label_box.pack_start(label, False, False, 0)
                cert_path = os.path.join(
                    vpn_profiles.profile_cert_dir(profile["domain"]), "client.crt"
                )
                expiry_warning = vpn_network.check_cert_expiry(cert_path)
                if expiry_warning:
                    warn_label = Gtk.Label(label=f"⚠ certificate {expiry_warning}")
                    warn_label.set_xalign(0)
                    warn_label.get_style_context().add_class("dim-label")
                    label_box.pack_start(warn_label, False, False, 0)
                connect_btn = Gtk.Button(label="Connect")
                connect_btn.connect("clicked", self._on_profile_chosen, profile)
                edit_btn = Gtk.Button(label="Edit")
                edit_btn.connect("clicked", self._on_profile_edit_clicked, profile)
                remove_btn = Gtk.Button(label="Remove")
                remove_btn.connect("clicked", self._on_profile_removed, profile)
                row_box.pack_start(label_box, True, True, 0)
                row_box.pack_start(connect_btn, False, False, 0)
                row_box.pack_start(edit_btn, False, False, 0)
                row_box.pack_start(remove_btn, False, False, 0)
                row = Gtk.ListBoxRow()
                row.add(row_box)
                row.set_activatable(False)
                self.profiles_list_box.add(row)
        self.profiles_list_box.show_all()

    def _on_profile_removed(self, _button, profile: dict) -> None:
        self.profiles = [p for p in self.profiles if p["domain"] != profile["domain"]]
        vpn_profiles.save_profiles(self.profiles)
        self._refresh_profiles_list()

    def _reset_new_profile_form(
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
        self._np_editing_domain = editing_domain
        self.np_form_title.set_text(title)
        self.np_domain_entry.set_text(domain_text)
        # The domain is part of the on-disk cert path and every generated
        # config/route -- renaming it here would silently orphan the
        # existing profiles/<domain>/ folder, so editing is scoped to the
        # other fields only (domain_editable=False).
        self.np_domain_entry.set_sensitive(domain_editable)
        if saml_active:
            self.np_saml_radio.set_active(True)
        else:
            self.np_cred_radio.set_active(True)
        self.np_saml_group_entry.set_text(saml_group_text)
        self._np_cert_folder = None
        self.np_cert_path_label.set_text(cert_label)
        self.np_cert_fetch_btn.set_sensitive(True)
        self.np_error_label.set_text("")

    def _on_profile_edit_clicked(self, _button, profile: dict) -> None:
        self._reset_new_profile_form(
            title=f"Edit {profile['domain']}",
            editing_domain=profile["domain"],
            domain_text=profile["domain"],
            domain_editable=False,
            saml_active=profile["auth_mode"] == "saml",
            saml_group_text=profile.get("saml_auth_group") or "",
            cert_label="(keep existing certificates)",
        )
        self.stack.set_visible_child_name("new_profile")

    def _on_add_new_domain_clicked(self, _button) -> None:
        self._reset_new_profile_form(title="New domain")
        self.stack.set_visible_child_name("new_profile")

    def _on_profile_chosen(self, _button, profile: dict) -> None:
        self.active_profile = profile
        if profile["auth_mode"] == "saml":
            self.stack.set_visible_child_name("saml_login")
            self._clear_saml_cookie_and_load_login()
        else:
            self.cred_user_entry.set_text("")
            self.cred_pass_entry.set_text("")
            self.stack.set_visible_child_name("credentials_login")

    # ================= page: add new domain =================

    def _build_new_profile_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(24)

        self.np_form_title = Gtk.Label(label="New domain")
        self.np_form_title.get_style_context().add_class("title")
        box.pack_start(self.np_form_title, False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=8)
        box.pack_start(grid, False, False, 0)

        grid.attach(Gtk.Label(label="Domain (e.g. vpn.example.com):", xalign=0), 0, 0, 1, 1)
        self.np_domain_entry = Gtk.Entry()
        grid.attach(self.np_domain_entry, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Login mode:", xalign=0), 0, 1, 1, 1)
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.np_saml_radio = Gtk.RadioButton.new_with_label_from_widget(None, "SAML (web SSO)")
        self.np_cred_radio = Gtk.RadioButton.new_with_label_from_widget(
            self.np_saml_radio, "Credentials (username/password)"
        )
        mode_box.pack_start(self.np_saml_radio, False, False, 0)
        mode_box.pack_start(self.np_cred_radio, False, False, 0)
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
        self.np_saml_group_entry = Gtk.Entry()
        self.np_saml_group_entry.set_placeholder_text("e.g. Corp_SAML")
        self.np_saml_group_entry.set_tooltip_text(saml_group_label.get_tooltip_text())
        grid.attach(self.np_saml_group_entry, 1, 2, 1, 1)

        grid.attach(Gtk.Label(label="Certificates (ca.crt / client.crt / client.pem):", xalign=0), 0, 3, 1, 1)
        cert_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.np_cert_path_label = Gtk.Label(label="(none chosen)")
        self.np_cert_path_label.get_style_context().add_class("dim-label")
        self.np_cert_fetch_btn = Gtk.Button(label="Fetch automatically")
        self.np_cert_fetch_btn.connect("clicked", self._on_fetch_cert_clicked)
        cert_choose_btn = Gtk.Button(label="Choose folder...")
        cert_choose_btn.connect("clicked", self._on_choose_cert_folder)
        cert_box.pack_start(self.np_cert_path_label, True, True, 0)
        cert_box.pack_start(self.np_cert_fetch_btn, False, False, 0)
        cert_box.pack_start(cert_choose_btn, False, False, 0)
        grid.attach(cert_box, 1, 3, 1, 1)
        self._np_cert_folder = None

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
        box.pack_start(hint, False, False, 0)

        self.np_error_label = Gtk.Label(label="")
        self.np_error_label.set_line_wrap(True)
        box.pack_start(self.np_error_label, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_btn = Gtk.Button(label="Back")
        back_btn.connect("clicked", lambda _b: self.stack.set_visible_child_name("profiles"))
        save_btn = Gtk.Button(label="Save")
        save_btn.connect("clicked", self._on_save_new_profile)
        btn_box.pack_start(back_btn, False, False, 0)
        btn_box.pack_start(save_btn, False, False, 0)
        box.pack_start(btn_box, False, False, 0)

        self.stack.add_named(box, "new_profile")

    def _on_choose_cert_folder(self, _button) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Choose the folder with ca.crt / client.crt / client.pem",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        if dialog.run() == Gtk.ResponseType.OK:
            self._np_cert_folder = dialog.get_filename()
            self.np_cert_path_label.set_text(self._np_cert_folder)
        dialog.destroy()

    def _on_fetch_cert_clicked(self, _button) -> None:
        domain = self.np_domain_entry.get_text().strip()
        if not domain:
            self.np_error_label.set_text("Enter the domain first.")
            return
        self.np_error_label.set_text("")
        self.np_cert_fetch_btn.set_sensitive(False)
        self.np_cert_path_label.set_text("Fetching...")
        threading.Thread(
            target=self._fetch_cert_worker, args=(domain,), daemon=True
        ).start()

    def _fetch_cert_worker(self, domain: str) -> None:
        staging_dir = tempfile.mkdtemp(prefix="watchguard-vpn-fetch-")
        try:
            vpn_network.fetch_certificates_to_dir(domain, staging_dir)
        except Exception as e:
            GLib.idle_add(self._on_fetch_cert_done, domain, None, str(e), None)
            return
        # Best-effort: the SAML auth group comes from a separate,
        # unrelated endpoint. A cert-only Firebox (SAML not configured)
        # is a normal case, not a failure -- don't fail the whole fetch
        # over it, just leave the group field alone if this errors.
        try:
            login_config = vpn_network.fetch_login_config(domain)
        except Exception:
            login_config = None
        GLib.idle_add(self._on_fetch_cert_done, domain, staging_dir, None, login_config)

    def _on_fetch_cert_done(self, domain: str, staging_dir, error, login_config) -> None:
        self.np_cert_fetch_btn.set_sensitive(True)
        # The user may have edited the domain field while this was in
        # flight -- if it no longer matches what we fetched for, discard
        # the result rather than silently applying it to the wrong domain.
        if self.np_domain_entry.get_text().strip() != domain:
            self.np_cert_path_label.set_text("(none chosen)")
            return
        if error:
            self.np_cert_path_label.set_text("(none chosen)")
            self.np_error_label.set_text(f"Automatic fetch failed: {error}")
            return
        self._np_cert_folder = staging_dir
        self.np_cert_path_label.set_text(f"Fetched automatically from {domain}")

        if login_config:
            if login_config["saml_enabled"]:
                self.np_saml_radio.set_active(True)
            elif login_config["auth_domains"]:
                self.np_cred_radio.set_active(True)
            if login_config["saml_idp_name"]:
                self.np_saml_group_entry.set_text(login_config["saml_idp_name"])

    def _on_save_new_profile(self, _button) -> None:
        domain = self.np_domain_entry.get_text().strip()
        editing = self._np_editing_domain is not None
        if not domain:
            self.np_error_label.set_text("Domain is missing.")
            return
        if not editing and any(p["domain"] == domain for p in self.profiles):
            self.np_error_label.set_text("A profile for that domain already exists.")
            return
        if not editing and not self._np_cert_folder:
            self.np_error_label.set_text("Choose the certificate folder first.")
            return

        # In edit mode the cert folder is optional (existing certs are
        # kept as-is unless a new folder is picked); in add mode it's
        # already required above, so this always runs there.
        if self._np_cert_folder:
            missing = [
                name for name in vpn_network.CERT_FILES
                if not os.path.isfile(os.path.join(self._np_cert_folder, name))
            ]
            if missing:
                self.np_error_label.set_text(
                    f"Missing in that folder: {', '.join(missing)}"
                )
                return

            dest_dir = vpn_profiles.profile_cert_dir(domain)
            os.makedirs(dest_dir, exist_ok=True)
            for name in vpn_network.CERT_FILES:
                src = os.path.join(self._np_cert_folder, name)
                dst = os.path.join(dest_dir, name)
                with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                    fdst.write(fsrc.read())
            os.chmod(os.path.join(dest_dir, "client.pem"), 0o600)

        auth_mode = "saml" if self.np_saml_radio.get_active() else "credentials"
        saml_auth_group = self.np_saml_group_entry.get_text().strip()

        if editing:
            for p in self.profiles:
                if p["domain"] == domain:
                    p["auth_mode"] = auth_mode
                    p["saml_auth_group"] = saml_auth_group
                    break
        else:
            self.profiles.append({
                "domain": domain,
                "label": domain,
                "auth_mode": auth_mode,
                "saml_auth_group": saml_auth_group,
                "verify_x509_name": "",
            })
        vpn_profiles.save_profiles(self.profiles)

        self._reset_new_profile_form(title="New domain")

        self._refresh_profiles_list()
        self.stack.set_visible_child_name("profiles")

    # ================= page: SAML login =================

    def _build_saml_login_page(self) -> None:
        login_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.wifi_warning_label = Gtk.Label(label="")
        self.wifi_warning_label.set_line_wrap(True)
        self.wifi_warning_label.set_margin_top(8)
        self.wifi_warning_label.set_margin_bottom(8)
        self.wifi_warning_label.set_margin_start(12)
        self.wifi_warning_label.set_margin_end(12)
        self.wifi_warning_label.set_no_show_all(True)
        login_box.pack_start(self.wifi_warning_label, False, False, 0)

        back_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        back_bar.set_margin_start(6)
        back_bar.set_margin_top(6)
        back_btn = Gtk.Button(label="← Change domain")
        back_btn.connect("clicked", self._on_back_to_profiles)
        back_bar.pack_start(back_btn, False, False, 0)
        login_box.pack_start(back_bar, False, False, 0)

        self.webview = WebKit2.WebView()
        self.webview.connect("load-changed", self.on_load_changed)
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.webview)
        login_box.pack_start(scrolled, True, True, 0)

        self.stack.add_named(login_box, "saml_login")
        threading.Thread(target=self._check_wifi_health, daemon=True).start()

    def _on_back_to_profiles(self, _button) -> None:
        # Blank the embedded browser right away -- it's a single WebView
        # reused across every SAML domain, so without this it would keep
        # showing whatever the last domain's login page was until the next
        # one finishes loading (which can take a few seconds).
        if self._saml_login_cancellable is not None:
            self._saml_login_cancellable.cancel()
            self._saml_login_cancellable = None
        self.webview.stop_loading()
        self.webview.load_uri("about:blank")
        self.stack.set_visible_child_name("profiles")

    # ---------- wifi health ----------

    def _check_wifi_health(self) -> None:
        msg = vpn_network.check_wifi_health()
        if msg:
            GLib.idle_add(self._show_wifi_warning, msg)

    def _show_wifi_warning(self, msg: str) -> None:
        self.wifi_warning_label.set_text(msg)
        self.wifi_warning_label.set_no_show_all(False)
        self.wifi_warning_label.show()

    def _clear_saml_cookie_and_load_login(self) -> None:
        # We clear ALL cookies before every login. We tried clearing only
        # the Firebox's own cookie and leaving the Microsoft session alive
        # to skip password/MFA, but it made no perceptible difference
        # (the official client likely doesn't do it either) and it added a
        # real domain-matching bug -- not worth the complexity, so we went
        # back to the simple approach: without this, the browser reuses
        # the stored SAML session cookie and hands back a stale/already-used
        # token (AUTH_FAILED).
        #
        # This clear() is async. If the user picks a domain, then goes back
        # and picks a different one before the first clear() finishes,
        # there'd be two in flight -- and whichever callback fires last
        # would call load_uri() and briefly stomp on/race the other one,
        # which WebKit surfaces as an "Operation cancelled" page. Cancel
        # any previous request explicitly, and also ignore its callback if
        # it still manages to fire (belt and suspenders against the race).
        if self._saml_login_cancellable is not None:
            self._saml_login_cancellable.cancel()
        self.webview.stop_loading()
        self.webview.load_uri("about:blank")
        cancellable = Gio.Cancellable()
        self._saml_login_cancellable = cancellable
        manager = self.webview.get_website_data_manager()
        manager.clear(WebKit2.WebsiteDataTypes.COOKIES, 0, cancellable, self._on_cookies_cleared, cancellable)

    def _on_cookies_cleared(self, manager, result, cancellable) -> None:
        if cancellable is not self._saml_login_cancellable:
            return  # superseded by a newer domain selection, ignore
        try:
            manager.clear_finish(result)
        except GLib.Error:
            pass
        self._load_saml_login()

    def _load_saml_login(self) -> None:
        domain = self.active_profile["domain"]
        self.webview.load_uri(f"https://{domain}/auth/saml/login?from=sslvpn_client")

    def on_load_changed(self, webview, event):
        if event != WebKit2.LoadEvent.FINISHED:
            return
        uri = webview.get_uri() or ""
        if "sslvpn_success.shtml" not in uri:
            return

        qs = parse_qs(urlparse(uri).query)
        if qs.get("result", [""])[0] != "success" or "user" not in qs or "token" not in qs:
            return  # login failed or ended the session; stay on the login page

        email = qs["user"][0]
        token = qs["token"][0]
        group = (self.active_profile.get("saml_auth_group") or "").strip()
        username = f"{group}\\{email}" if group else email
        self.start_vpn(username, token)

    # ================= page: credentials login =================

    def _build_credentials_login_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(24)

        back_btn = Gtk.Button(label="← Change domain")
        back_btn.set_halign(Gtk.Align.START)
        back_btn.connect("clicked", self._on_back_to_profiles)
        box.pack_start(back_btn, False, False, 0)

        self.cred_title_label = Gtk.Label(label="Log in")
        self.cred_title_label.get_style_context().add_class("title")
        box.pack_start(self.cred_title_label, False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=8)
        grid.attach(Gtk.Label(label="Username:", xalign=0), 0, 0, 1, 1)
        self.cred_user_entry = Gtk.Entry()
        grid.attach(self.cred_user_entry, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Password:", xalign=0), 0, 1, 1, 1)
        self.cred_pass_entry = Gtk.Entry()
        self.cred_pass_entry.set_visibility(False)
        self.cred_pass_entry.connect("activate", self._on_credentials_connect)
        grid.attach(self.cred_pass_entry, 1, 1, 1, 1)
        box.pack_start(grid, False, False, 0)

        connect_btn = Gtk.Button(label="Connect")
        connect_btn.connect("clicked", self._on_credentials_connect)
        box.pack_start(connect_btn, False, False, 0)

        self.stack.add_named(box, "credentials_login")

    def _on_credentials_connect(self, _button) -> None:
        user = self.cred_user_entry.get_text().strip()
        password = self.cred_pass_entry.get_text()
        if not user or not password:
            return
        self.start_vpn(user, password)

    # ================= VPN connection =================

    def _build_status_page(self) -> None:
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        status_box.set_border_width(24)

        self.status_icon = Gtk.Image.new_from_icon_name(
            "network-vpn-acquiring-symbolic", Gtk.IconSize.DIALOG
        )
        self.status_label = Gtk.Label(label="Connecting...")
        self.status_label.set_line_wrap(True)
        self.detail_label = Gtk.Label(label="")
        self.detail_label.set_line_wrap(True)
        self.detail_label.get_style_context().add_class("dim-label")

        self.action_button = Gtk.Button(label="Disconnect")
        self.action_button.connect("clicked", self.on_action_clicked)
        self.action_button.set_halign(Gtk.Align.CENTER)

        self.test_button = Gtk.Button(label="Test connection")
        self.test_button.connect("clicked", self.on_test_clicked)
        self.test_button.set_halign(Gtk.Align.CENTER)
        self.test_button.set_sensitive(False)

        for w in (self.status_icon, self.status_label, self.detail_label,
                  self.action_button, self.test_button):
            status_box.pack_start(w, False, False, 0)

        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer)
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.CHAR)
        log_scrolled = Gtk.ScrolledWindow()
        log_scrolled.set_vexpand(True)
        log_scrolled.set_min_content_height(220)
        log_scrolled.add(self.log_view)
        status_box.pack_start(log_scrolled, True, True, 0)

        self.stack.add_named(status_box, "status")

    def start_vpn(self, username: str, secret: str) -> None:
        self.stack.set_visible_child_name("status")
        self.status_label.set_text(f"Connecting as {username}...")
        self.detail_label.set_text("")
        self.action_button.set_sensitive(False)
        self.log_buffer.set_text("")
        self.vpn.start(self.active_profile, username, secret)

    def append_log_line(self, line: str) -> None:
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, line + "\n")
        end = self.log_buffer.get_end_iter()
        self.log_view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)

    def set_status(self, state: str, extra: str = "") -> None:
        if state == "connected":
            self.status_icon.set_from_icon_name(
                "network-vpn-symbolic", Gtk.IconSize.DIALOG
            )
            domain = self.active_profile["domain"] if self.active_profile else ""
            self.status_label.set_text(f"Connected to {domain}")
            self.detail_label.set_text("")
            self.action_button.set_label("Disconnect")
            self.action_button.set_sensitive(True)
            self.test_button.set_sensitive(True)
        elif state == "reconnecting":
            self.status_icon.set_from_icon_name(
                "network-vpn-acquiring-symbolic", Gtk.IconSize.DIALOG
            )
            self.status_label.set_text("Connection dropped, reconnecting...")
            self.action_button.set_sensitive(False)
            self.test_button.set_sensitive(False)
        elif state == "error":
            self.status_icon.set_from_icon_name(
                "network-vpn-disabled-symbolic", Gtk.IconSize.DIALOG
            )
            self.status_label.set_text("Connection error")
            self.detail_label.set_text(extra)
            self.action_button.set_label("Try again")
            self.action_button.set_sensitive(True)
            self.test_button.set_sensitive(False)
        elif state == "detail":
            # Still connecting -- a progress line from the log, not a
            # state change (icon/buttons are left alone).
            self.detail_label.set_text(extra)

    def on_vpn_exited(self, session_id: int) -> None:
        if session_id != self.vpn.session_id:
            # A newer connection attempt (or a force-reset after a stuck
            # disconnect, see _on_disconnect_stuck) has already moved the
            # UI on -- this is a late callback from an old session's
            # background thread, ignore it so it doesn't clobber the
            # current state.
            return
        self.vpn.clear_after_exit()
        was_error = self.status_label.get_text() == "Connection error"
        self.test_button.set_sensitive(False)
        if was_error:
            # Leave the error visible on the status page (with "Try again"
            # for the same domain) so it can be diagnosed.
            self.action_button.set_label("Try again")
            self.action_button.set_sensitive(True)
        else:
            # Normal disconnect (user clicked "Disconnect", or it closed on
            # its own): go straight back to picking a domain instead of
            # staying on the status page waiting for a "reconnect" click.
            self.active_profile = None
            self.stack.set_visible_child_name("profiles")

    # ---------- connection test ----------

    def on_test_clicked(self, _button) -> None:
        self.test_button.set_sensitive(False)
        threading.Thread(target=self._run_connection_test, daemon=True).start()

    def _run_connection_test(self) -> None:
        vpn_network.run_connection_test(
            self.vpn.test_targets(),
            on_line=lambda line: GLib.idle_add(self.append_log_line, line),
        )
        GLib.idle_add(self.test_button.set_sensitive, True)

    # ---------- buttons ----------

    def on_action_clicked(self, _button) -> None:
        if self.action_button.get_label() == "Force back to start":
            self._force_reset_after_stuck_disconnect()
        elif self.vpn.is_running():
            self.vpn.kill()
            self.action_button.set_sensitive(False)
            self.status_label.set_text("Disconnecting...")
            # Belt and suspenders on top of vpn.kill()'s own SIGKILL
            # escalation: if disconnecting is *still* stuck after that
            # (pkexec itself hung, polkit dialog never answered, etc.),
            # don't leave the user staring at a disabled button forever --
            # offer a way to abandon it and go back to the domain list.
            GLib.timeout_add_seconds(8, self._on_disconnect_stuck, self.vpn.session_id)
        else:
            # Reconnect: go back to the corresponding login page (SAML or
            # credentials) with a clean session.
            if self.active_profile and self.active_profile["auth_mode"] == "saml":
                self.stack.set_visible_child_name("saml_login")
                self._clear_saml_cookie_and_load_login()
            elif self.active_profile:
                self.stack.set_visible_child_name("credentials_login")
            else:
                self.stack.set_visible_child_name("profiles")

    def _on_disconnect_stuck(self, session_id: int) -> bool:
        if session_id == self.vpn.session_id and self.vpn.is_running():
            self.action_button.set_label("Force back to start")
            self.action_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _force_reset_after_stuck_disconnect(self) -> None:
        self.vpn.force_reset()
        self.active_profile = None
        self.test_button.set_sensitive(False)
        self.stack.set_visible_child_name("profiles")

    def on_destroy(self, _widget) -> None:
        self.vpn.kill()
        self.vpn.clear_after_exit()
        Gtk.main_quit()


def main() -> None:
    os.makedirs(vpn_profiles.PROFILES_DIR, exist_ok=True)
    # Copies route-up.sh/route-down.sh from the repo into the app's data
    # dir: this keeps them always in sync with the code's version, and
    # initial setup doesn't need any manual step beyond cloning and
    # running this.
    vpn_routes.install_hook_scripts()
    win = VpnWindow()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
