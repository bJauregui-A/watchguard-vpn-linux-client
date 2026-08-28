# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""Route management around the OpenVPN tunnel.

The actual route manipulation happens as root, inside route-up.sh /
route-down.sh (run by OpenVPN itself as hooks) -- this module only:
  - copies those two scripts into the app's data dir on launch, so they
    stay in sync with the repo without any manual install step;
  - saves the routing state route-up.sh needs, before the VPN connects
    (done here in Python, not in an OpenVPN --up script, because that
    hook overrides OpenVPN's own automatic DNS handling);
  - builds the matching cleanup command for when OpenVPN's own
    route-pre-down hook doesn't get to run (see cleanup_command below).

No GTK. Plain subprocess/socket/file calls only.
"""
import os
import re
import socket
import subprocess

import vpn_profiles

BASE = os.path.dirname(os.path.abspath(__file__))
ORIG_ROUTE_FILE = "/tmp/.watchguard-vpn-orig-route"

ROUTE_UP_SCRIPT = os.path.join(vpn_profiles.DATA_DIR, "route-up.sh")
ROUTE_DOWN_SCRIPT = os.path.join(vpn_profiles.DATA_DIR, "route-down.sh")


def install_hook_scripts() -> None:
    """Copy route-up.sh/route-down.sh from the repo into DATA_DIR on
    every launch: this keeps them always in sync with the code's
    version, and initial setup doesn't need any manual step beyond
    cloning and running the app."""
    for name, dest in (("route-up.sh", ROUTE_UP_SCRIPT), ("route-down.sh", ROUTE_DOWN_SCRIPT)):
        try:
            with open(os.path.join(BASE, name), "rb") as fsrc:
                data = fsrc.read()
            with open(dest, "wb") as fdst:
                fdst.write(data)
            os.chmod(dest, 0o755)
        except OSError:
            pass


def save_pre_vpn_routes(domain: str) -> None:
    """Save the routing state route-up.sh needs, BEFORE connecting. Two
    lines are written to ORIG_ROUTE_FILE:
      1. The default route -- used to reinforce the original gateway
         itself against being swallowed by a broad pushed route.
      2. The actual pre-VPN route specifically to the Firebox's own IP --
         used to protect that IP from the same problem. This is NOT
         always "via the default gateway": if the Firebox happens to be
         on a network the client already reaches directly (no gateway
         hop -- e.g. the same LAN), assuming the default gateway here
         would install a route that can't actually reach the Firebox,
         breaking the tunnel's own TCP connection right after it comes
         up.
    """
    default_line = ""
    server_line = ""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        default_line = result.stdout.splitlines()[0] if result.stdout else ""
    except Exception:
        pass
    try:
        server_ip = socket.gethostbyname(domain)
        result = subprocess.run(
            ["ip", "route", "get", server_ip],
            capture_output=True, text=True, timeout=5,
        )
        server_line = result.stdout.splitlines()[0] if result.stdout else ""
    except Exception:
        pass
    try:
        with open(ORIG_ROUTE_FILE, "w") as f:
            f.write(default_line + "\n" + server_line + "\n")
    except Exception:
        pass


def clear_saved_routes() -> None:
    if os.path.exists(ORIG_ROUTE_FILE):
        os.remove(ORIG_ROUTE_FILE)


def cleanup_command(domain: str) -> str:
    """Build the 'ip route del' command that undoes the host route
    route-up.sh added for the Firebox's IP. route-down.sh normally does
    this via OpenVPN's route-pre-down hook -- but that only fires on
    OpenVPN's OWN graceful shutdown. If we have to SIGKILL, or the user
    force-abandons a stuck disconnect, that hook never runs and the
    route is left behind, breaking any future direct access to that IP.
    So this is run proactively too, independent of whether OpenVPN
    cooperates. Returns "" if there's nothing to clean up (or the saved
    state can't be parsed)."""
    if not os.path.exists(ORIG_ROUTE_FILE):
        return ""
    try:
        ip = socket.gethostbyname(domain)
        with open(ORIG_ROUTE_FILE) as f:
            lines = f.read().splitlines()
        server_line = lines[1] if len(lines) > 1 else ""
        gw_match = re.search(r"via (\S+)", server_line)
        if_match = re.search(r"dev (\S+)", server_line)
        if not if_match:
            return ""
        iface = if_match.group(1)
        if gw_match:
            return f"ip route del {ip}/32 via {gw_match.group(1)} dev {iface} 2>/dev/null"
        return f"ip route del {ip}/32 dev {iface} 2>/dev/null"
    except Exception:
        return ""
