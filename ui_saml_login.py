# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""The "saml_login" page: an embedded WebKit2 browser doing the SAML SSO
dance, ending on a redirect to sslvpn_success.shtml carrying the one-time
OpenVPN username/token. Also owns the once-at-startup wifi health check
(shown as a banner on this page, since it's the page most sensitive to a
flaky network -- IdP redirects fail silently on a bad connection).
"""
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, GLib  # noqa: E402

import vpn_network
from ui_saml_common import SamlWebViewController


class SamlLoginView:
    def __init__(self, on_back, on_success):
        self._on_back = on_back
        self._on_success = on_success
        self._profile = None

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.wifi_warning_label = Gtk.Label(label="")
        self.wifi_warning_label.set_line_wrap(True)
        self.wifi_warning_label.set_margin_top(8)
        self.wifi_warning_label.set_margin_bottom(8)
        self.wifi_warning_label.set_margin_start(12)
        self.wifi_warning_label.set_margin_end(12)
        self.wifi_warning_label.set_no_show_all(True)
        self.widget.pack_start(self.wifi_warning_label, False, False, 0)

        back_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        back_bar.set_margin_start(6)
        back_bar.set_margin_top(6)
        back_btn = Gtk.Button(label="← Change domain")
        back_btn.connect("clicked", self._on_back_clicked)
        back_bar.pack_start(back_btn, False, False, 0)
        self.widget.pack_start(back_bar, False, False, 0)

        self.webview = WebKit2.WebView()
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.webview)
        self.widget.pack_start(scrolled, True, True, 0)

        self._controller = SamlWebViewController(self.webview, self._on_saml_success)

        threading.Thread(target=self._check_wifi_health, daemon=True).start()

    def load_for_profile(self, profile: dict) -> None:
        self._profile = profile
        self._controller.load_login(profile["domain"])

    def _on_back_clicked(self, _button) -> None:
        self._controller.stop_and_blank()
        self._on_back()

    # ---------- wifi health ----------

    def _check_wifi_health(self) -> None:
        msg = vpn_network.check_wifi_health()
        if msg:
            GLib.idle_add(self._show_wifi_warning, msg)

    def _show_wifi_warning(self, msg: str) -> None:
        self.wifi_warning_label.set_text(msg)
        self.wifi_warning_label.set_no_show_all(False)
        self.wifi_warning_label.show()

    # ---------- login flow ----------

    def _on_saml_success(self, qs: dict) -> None:
        if "user" not in qs or "token" not in qs:
            return  # shouldn't happen once SamlWebViewController already checked result=success, but don't crash if it does
        email = qs["user"][0]
        token = qs["token"][0]
        group = (self._profile.get("saml_auth_group") or "").strip()
        username = f"{group}\\{email}" if group else email
        self._on_success(username, token)
