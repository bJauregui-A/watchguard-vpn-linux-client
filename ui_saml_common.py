# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""Shared logic for driving an embedded WebKit2 SAML login: clearing
cookies/session before every attempt, and detecting the Firebox's own
success redirect. Used by both ui_saml_login.py (the real login page)
and ui_new_profile.py's login-first fallback (unlocking a cert fetch a
Firebox rejected anonymously) -- extracted after the two ended up with
~70 lines of copy-pasted cookie-clearing/redirect-detection between them.
"""
from urllib.parse import parse_qs, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import WebKit2, GLib, Gio  # noqa: E402


class SamlWebViewController:
    """Owns one WebKit2.WebView's login lifecycle: cookie-clearing before
    each attempt, and calling `on_success(query_params)` once the
    Firebox's own success redirect lands (never for the intermediate IdP
    hop -- see _on_load_changed)."""

    def __init__(self, webview: WebKit2.WebView, on_success):
        self._webview = webview
        self._on_success = on_success
        self._domain = None
        self._cancellable = None  # guards against stale/overlapping attempts, see _clear_cookies_and_load
        webview.connect("load-changed", self._on_load_changed)

    def load_login(self, domain: str) -> None:
        self._domain = domain
        self._clear_cookies_and_load()

    def stop_and_blank(self) -> None:
        """Cancel any in-flight cookie-clear and blank the webview --
        call this whenever leaving the page/hiding the fallback, so the
        single shared WebView doesn't keep showing whatever the last
        domain's login page was."""
        if self._cancellable is not None:
            self._cancellable.cancel()
            self._cancellable = None
        self._webview.stop_loading()
        self._webview.load_uri("about:blank")

    def _clear_cookies_and_load(self) -> None:
        # We clear ALL cookies before every login. We tried clearing only
        # the Firebox's own cookie and leaving the Microsoft session alive
        # to skip password/MFA, but it made no perceptible difference
        # (the official client likely doesn't do it either) and it added a
        # real domain-matching bug -- not worth the complexity, so we went
        # back to the simple approach: without this, the browser reuses
        # the stored SAML session cookie and hands back a stale/already-used
        # token (AUTH_FAILED), or -- with SAML enabled -- skips the login
        # prompt entirely via silent SSO.
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
        self._webview.stop_loading()
        self._webview.load_uri("about:blank")
        cancellable = Gio.Cancellable()
        self._cancellable = cancellable
        manager = self._webview.get_website_data_manager()
        manager.clear(WebKit2.WebsiteDataTypes.COOKIES, 0, cancellable, self._on_cookies_cleared, cancellable)

    def _on_cookies_cleared(self, manager, result, cancellable) -> None:
        if cancellable is not self._cancellable:
            return  # superseded by a newer attempt, ignore
        try:
            manager.clear_finish(result)
        except GLib.Error:
            pass
        self._webview.load_uri(f"https://{self._domain}/auth/saml/login?from=sslvpn_client")

    def _on_load_changed(self, webview, event) -> None:
        if event != WebKit2.LoadEvent.FINISHED:
            return
        uri = webview.get_uri() or ""
        if "sslvpn_success.shtml" not in uri:
            return
        parsed = urlparse(uri)
        # The FIRST redirect (Firebox -> IdP) carries a RelayState query
        # param whose (percent-encoded) value is literally
        # "https://<domain>/sslvpn_success.shtml?result=success&user=None
        # &token=None" -- a placeholder the IdP is meant to substitute
        # real values into and bounce back to only AFTER a real login, not
        # an actual destination on its own. A bare substring match plus
        # naive query-param parsing on that intermediate URL (host is the
        # IdP, not the Firebox) can mistake it for the real thing -- found
        # 2026-09-01 on the Android client, which extracted a literal
        # "None" username/token this way. Requiring the redirect's own
        # host to actually be the Firebox rules that out here too.
        if parsed.hostname != self._domain:
            return
        qs = parse_qs(parsed.query)
        if qs.get("result", [""])[0] != "success":
            return  # login failed or ended the session; stay on the login page
        self._on_success(qs)
