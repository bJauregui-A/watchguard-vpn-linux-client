# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""Profile storage: which WatchGuard domains are configured, their login
mode, and where their certificates live on disk.

Pure data layer -- no GTK, no subprocess calls, just JSON + plain file
I/O. Every other module that needs to know where the app keeps its state
imports DATA_DIR from here rather than recomputing it.
"""
import json
import os

from gi.repository import GLib

DATA_DIR = os.path.join(GLib.get_user_data_dir(), "watchguard-vpn-linux-client")
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")


def load_profiles() -> list:
    try:
        with open(PROFILES_FILE) as f:
            return json.load(f).get("profiles", [])
    except Exception:
        return []


def save_profiles(profiles: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROFILES_FILE, "w") as f:
        json.dump({"profiles": profiles}, f, indent=2)


def profile_cert_dir(domain: str) -> str:
    return os.path.join(PROFILES_DIR, domain)
