# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""The "status" page: live connection status/log for the active VpnProcess,
plus its connect/disconnect/reconnect/force-reset and "Test connection"
controls. Holds the VpnProcess instance itself (constructed by the caller
and handed in) since this is the page that actually drives it -- the rest
of the app only needs to know whether a connection is active.
"""
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

import vpn_network


class StatusView:
    def __init__(self, vpn, on_disconnected, on_reconnect_requested, on_force_reset):
        self.vpn = vpn
        self._on_disconnected = on_disconnected
        self._on_reconnect_requested = on_reconnect_requested
        self._on_force_reset = on_force_reset
        self._profile = None

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.widget.set_border_width(24)

        self.status_icon = Gtk.Image.new_from_icon_name(
            "network-vpn-acquiring-symbolic", Gtk.IconSize.DIALOG
        )
        self.status_label = Gtk.Label(label="Connecting...")
        self.status_label.set_line_wrap(True)
        self.detail_label = Gtk.Label(label="")
        self.detail_label.set_line_wrap(True)
        self.detail_label.get_style_context().add_class("dim-label")

        self.action_button = Gtk.Button(label="Disconnect")
        self.action_button.connect("clicked", self._on_action_clicked)
        self.action_button.set_halign(Gtk.Align.CENTER)

        self.test_button = Gtk.Button(label="Test connection")
        self.test_button.connect("clicked", self._on_test_clicked)
        self.test_button.set_halign(Gtk.Align.CENTER)
        self.test_button.set_sensitive(False)

        for w in (self.status_icon, self.status_label, self.detail_label,
                  self.action_button, self.test_button):
            self.widget.pack_start(w, False, False, 0)

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
        self.widget.pack_start(log_scrolled, True, True, 0)

    def start(self, profile: dict, username: str, secret: str) -> None:
        self._profile = profile
        self.status_label.set_text(f"Connecting as {username}...")
        self.detail_label.set_text("")
        self.action_button.set_sensitive(False)
        self.log_buffer.set_text("")
        self.vpn.start(profile, username, secret)

    # ---------- VpnProcess callbacks ----------

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
            domain = self._profile["domain"] if self._profile else ""
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
            self._on_disconnected()

    # ---------- connection test ----------

    def _on_test_clicked(self, _button) -> None:
        self.test_button.set_sensitive(False)
        threading.Thread(target=self._run_connection_test, daemon=True).start()

    def _run_connection_test(self) -> None:
        vpn_network.run_connection_test(
            self.vpn.test_targets(),
            on_line=lambda line: GLib.idle_add(self.append_log_line, line),
        )
        GLib.idle_add(self.test_button.set_sensitive, True)

    # ---------- buttons ----------

    def _on_action_clicked(self, _button) -> None:
        if self.action_button.get_label() == "Force back to start":
            self.vpn.force_reset()
            self.test_button.set_sensitive(False)
            self._on_force_reset()
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
            self._on_reconnect_requested(self._profile)

    def _on_disconnect_stuck(self, session_id: int) -> bool:
        if session_id == self.vpn.session_id and self.vpn.is_running():
            self.action_button.set_label("Force back to start")
            self.action_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE
