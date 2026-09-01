# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""The "saml_login" page: an embedded WebKit2 browser doing the SAML SSO
dance, ending on a redirect to sslvpn_success.shtml carrying the one-time
OpenVPN username/token. Also owns the once-at-startup wifi health check
(shown as a banner on this page, since it's the page most sensitive to a
flaky network -- IdP redirects fail silently on a bad connection).
"""
import threading
from urllib.parse import parse_qs, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, GLib, Gio  # noqa: E402

import vpn_network


class SamlLoginView:
    def __init__(self, on_back, on_success):
        self._on_back = on_back
        self._on_success = on_success
        self._profile = None
        self._cancellable = None  # guards against stale domain switches, see _clear_saml_cookie_and_load_login

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
        self.webview.connect("load-changed", self._on_load_changed)
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.webview)
        self.widget.pack_start(scrolled, True, True, 0)

        threading.Thread(target=self._check_wifi_health, daemon=True).start()

    def load_for_profile(self, profile: dict) -> None:
        self._profile = profile
        self._clear_saml_cookie_and_load_login()

    def _on_back_clicked(self, _button) -> None:
        # Blank the embedded browser right away -- it's a single WebView
        # reused across every SAML domain, so without this it would keep
        # showing whatever the last domain's login page was until the next
        # one finishes loading (which can take a few seconds).
        if self._cancellable is not None:
            self._cancellable.cancel()
            self._cancellable = None
        self.webview.stop_loading()
        self.webview.load_uri("about:blank")
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
        if self._cancellable is not None:
            self._cancellable.cancel()
        self.webview.stop_loading()
        self.webview.load_uri("about:blank")
        cancellable = Gio.Cancellable()
        self._cancellable = cancellable
        manager = self.webview.get_website_data_manager()
        manager.clear(WebKit2.WebsiteDataTypes.COOKIES, 0, cancellable, self._on_cookies_cleared, cancellable)

    def _on_cookies_cleared(self, manager, result, cancellable) -> None:
        if cancellable is not self._cancellable:
            return  # superseded by a newer domain selection, ignore
        try:
            manager.clear_finish(result)
        except GLib.Error:
            pass
        self._load_saml_login()

    def _load_saml_login(self) -> None:
        domain = self._profile["domain"]
        self.webview.load_uri(f"https://{domain}/auth/saml/login?from=sslvpn_client")

    def _on_load_changed(self, webview, event):
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
        group = (self._profile.get("saml_auth_group") or "").strip()
        username = f"{group}\\{email}" if group else email
        self._on_success(username, token)
