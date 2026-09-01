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

This file wires together the app's pieces -- it owns the Gtk.Stack, the
shared profile list, and the active-profile/navigation state -- but each
page's actual widgets live in its own module:
  ui_profiles.py            page: pick a saved domain
  ui_new_profile.py         page: add/edit a domain
  ui_saml_login.py          page: SAML login (embedded browser)
  ui_credentials_login.py   page: username/password login
  ui_status.py              page: connection status/log

...which in turn sit on top of the non-GTK modules that do the actual work:
  vpn_profiles.py  profile storage
  vpn_routes.py    route capture/cleanup around the tunnel
  vpn_network.py   wifi health check + the "Test connection" ping check
  vpn_process.py   the OpenVPN subprocess itself

Built with Claude Code (https://claude.com/claude-code).

Requires: gtk3, webkit2gtk (4.1), polkit (pkexec), openvpn.
"""
import os

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk  # noqa: E402

import vpn_profiles
import vpn_routes
from vpn_process import VpnProcess
from ui_credentials_login import CredentialsLoginView
from ui_new_profile import NewProfileView
from ui_profiles import ProfilesView
from ui_saml_login import SamlLoginView
from ui_status import StatusView


class VpnWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="WatchGuard VPN")
        self.set_default_size(560, 780)
        self.set_icon_name("network-vpn")
        self.connect("destroy", self.on_destroy)

        self.active_profile = None
        self.profiles = vpn_profiles.load_profiles()

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.add(self.stack)

        # VpnProcess and StatusView reference each other (VpnProcess reports
        # through StatusView's methods; StatusView drives VpnProcess's
        # is_running/kill/force_reset/test_targets) -- these lambdas defer
        # the self.status_view lookup until each callback actually fires,
        # by which point it's been assigned below.
        self.vpn = VpnProcess(
            on_log_line=lambda line: self.status_view.append_log_line(line),
            on_status=lambda state, extra="": self.status_view.set_status(state, extra),
            on_exited=lambda session_id: self.status_view.on_vpn_exited(session_id),
        )

        self.profiles_view = ProfilesView(
            on_add_new=self._on_add_new,
            on_profile_chosen=self._on_profile_chosen,
            on_profile_edit=self._on_profile_edit,
            on_profile_removed=self._on_profile_removed,
        )
        self.new_profile_view = NewProfileView(
            on_back=lambda: self.stack.set_visible_child_name("profiles"),
            on_save=self._on_new_profile_save,
            domain_exists=lambda domain: any(p["domain"] == domain for p in self.profiles),
        )
        self.saml_view = SamlLoginView(
            on_back=lambda: self.stack.set_visible_child_name("profiles"),
            on_success=self._start_vpn,
        )
        self.credentials_view = CredentialsLoginView(
            on_back=lambda: self.stack.set_visible_child_name("profiles"),
            on_connect=self._start_vpn,
        )
        self.status_view = StatusView(
            vpn=self.vpn,
            on_disconnected=self._return_to_profiles,
            on_reconnect_requested=self._on_reconnect_requested,
            on_force_reset=self._return_to_profiles,
        )

        self.stack.add_named(self.profiles_view.widget, "profiles")
        self.stack.add_named(self.new_profile_view.widget, "new_profile")
        self.stack.add_named(self.saml_view.widget, "saml_login")
        self.stack.add_named(self.credentials_view.widget, "credentials_login")
        self.stack.add_named(self.status_view.widget, "status")

        self.stack.set_visible_child_name("profiles")
        self.profiles_view.refresh(self.profiles)

    # ================= navigation / state glue =================

    def _return_to_profiles(self) -> None:
        self.active_profile = None
        self.stack.set_visible_child_name("profiles")

    def _on_add_new(self) -> None:
        self.new_profile_view.reset(title="New domain")
        self.stack.set_visible_child_name("new_profile")

    def _on_profile_edit(self, profile: dict) -> None:
        self.new_profile_view.reset(
            title=f"Edit {profile['domain']}",
            editing_domain=profile["domain"],
            domain_text=profile["domain"],
            domain_editable=False,
            saml_active=profile["auth_mode"] == "saml",
            saml_group_text=profile.get("saml_auth_group") or "",
            cert_label="(keep existing certificates)",
        )
        self.stack.set_visible_child_name("new_profile")

    def _on_profile_removed(self, profile: dict) -> None:
        self.profiles = [p for p in self.profiles if p["domain"] != profile["domain"]]
        vpn_profiles.save_profiles(self.profiles)
        self.profiles_view.refresh(self.profiles)

    def _on_profile_chosen(self, profile: dict) -> None:
        self.active_profile = profile
        if profile["auth_mode"] == "saml":
            self.stack.set_visible_child_name("saml_login")
            self.saml_view.load_for_profile(profile)
        else:
            self.credentials_view.reset()
            self.stack.set_visible_child_name("credentials_login")

    def _on_new_profile_save(self, domain: str, auth_mode: str, saml_auth_group: str, editing: bool) -> None:
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

        self.new_profile_view.reset(title="New domain")
        self.profiles_view.refresh(self.profiles)
        self.stack.set_visible_child_name("profiles")

    def _on_reconnect_requested(self, profile) -> None:
        if profile and profile["auth_mode"] == "saml":
            self.stack.set_visible_child_name("saml_login")
            self.saml_view.load_for_profile(profile)
        elif profile:
            self.stack.set_visible_child_name("credentials_login")
        else:
            self.stack.set_visible_child_name("profiles")

    def _start_vpn(self, username: str, secret: str) -> None:
        self.status_view.start(self.active_profile, username, secret)
        self.stack.set_visible_child_name("status")

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
