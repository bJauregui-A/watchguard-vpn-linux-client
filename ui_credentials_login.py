# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""The "credentials_login" page: direct username/password entry for
domains not using SAML, submitted straight through as OpenVPN's
auth-user-pass (no embedded browser involved)."""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402


class CredentialsLoginView:
    def __init__(self, on_back, on_connect):
        self._on_back = on_back
        self._on_connect = on_connect

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.widget.set_border_width(24)

        back_btn = Gtk.Button(label="← Change domain")
        back_btn.set_halign(Gtk.Align.START)
        back_btn.connect("clicked", lambda _b: self._on_back())
        self.widget.pack_start(back_btn, False, False, 0)

        title_label = Gtk.Label(label="Log in")
        title_label.get_style_context().add_class("title")
        self.widget.pack_start(title_label, False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=8)
        grid.attach(Gtk.Label(label="Username:", xalign=0), 0, 0, 1, 1)
        self.user_entry = Gtk.Entry()
        grid.attach(self.user_entry, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Password:", xalign=0), 0, 1, 1, 1)
        self.pass_entry = Gtk.Entry()
        self.pass_entry.set_visibility(False)
        self.pass_entry.connect("activate", self._on_connect_clicked)
        grid.attach(self.pass_entry, 1, 1, 1, 1)
        self.widget.pack_start(grid, False, False, 0)

        connect_btn = Gtk.Button(label="Connect")
        connect_btn.connect("clicked", self._on_connect_clicked)
        self.widget.pack_start(connect_btn, False, False, 0)

    def reset(self) -> None:
        self.user_entry.set_text("")
        self.pass_entry.set_text("")

    def _on_connect_clicked(self, _button) -> None:
        user = self.user_entry.get_text().strip()
        password = self.pass_entry.get_text()
        if not user or not password:
            return
        self._on_connect(user, password)
