# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""The "profiles" page: pick an already-saved domain to connect to, or
jump to adding/editing one. Pure UI -- profile storage/mutation stays
with the caller (watchguard-vpn-gui.py), this view only renders whatever
list it's given and reports back which profile/button was activated.
"""
import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

import vpn_network
import vpn_profiles


class ProfilesView:
    def __init__(self, on_add_new, on_profile_chosen, on_profile_edit, on_profile_removed):
        self._on_add_new = on_add_new
        self._on_profile_chosen = on_profile_chosen
        self._on_profile_edit = on_profile_edit
        self._on_profile_removed = on_profile_removed

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.widget.set_border_width(24)

        title = Gtk.Label(label="Choose a domain")
        title.get_style_context().add_class("title")
        self.widget.pack_start(title, False, False, 0)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.widget.pack_start(self.list_box, True, True, 0)

        add_button = Gtk.Button(label="+ Add new domain")
        add_button.connect("clicked", lambda _b: self._on_add_new())
        self.widget.pack_start(add_button, False, False, 0)

    def refresh(self, profiles: list) -> None:
        for child in list(self.list_box.get_children()):
            self.list_box.remove(child)
        if not profiles:
            empty = Gtk.Label(label="No domains configured yet.")
            empty.get_style_context().add_class("dim-label")
            row = Gtk.ListBoxRow()
            row.add(empty)
            row.set_selectable(False)
            row.set_activatable(False)
            self.list_box.add(row)
        else:
            for profile in profiles:
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
                connect_btn.connect("clicked", lambda _b, p=profile: self._on_profile_chosen(p))
                edit_btn = Gtk.Button(label="Edit")
                edit_btn.connect("clicked", lambda _b, p=profile: self._on_profile_edit(p))
                remove_btn = Gtk.Button(label="Remove")
                remove_btn.connect("clicked", lambda _b, p=profile: self._on_profile_removed(p))
                row_box.pack_start(label_box, True, True, 0)
                row_box.pack_start(connect_btn, False, False, 0)
                row_box.pack_start(edit_btn, False, False, 0)
                row_box.pack_start(remove_btn, False, False, 0)
                row = Gtk.ListBoxRow()
                row.add(row_box)
                row.set_activatable(False)
                self.list_box.add(row)
        self.list_box.show_all()
