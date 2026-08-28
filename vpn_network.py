# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""Lightweight network diagnostics: a pre-connect wifi sanity check, and
the post-connect "Test connection" ping check.

Pure functions -- no GTK. These are meant to be called from a background
thread; callers are responsible for marshalling results back onto the
GTK main loop (GLib.idle_add) themselves.
"""
import os
import re
import subprocess
from typing import Callable, Iterable, Optional, Tuple


def check_wifi_health() -> Optional[str]:
    """Most of the connectivity issues we ran into weren't the tunnel's
    fault but the underlying wifi's -- this checks BEFORE connecting
    whether the network is already unstable, so it doesn't get mistaken
    for a VPN problem. Returns a warning message, or None if nothing
    looks wrong (or it couldn't tell)."""
    try:
        route = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        line = route.stdout.splitlines()[0] if route.stdout else ""
        m = re.search(r"via (\S+)", line)
        if not m:
            return None
        gateway = m.group(1)
        env = dict(os.environ, LC_ALL="C")
        ping = subprocess.run(
            ["ping", "-c", "4", "-W", "2", gateway],
            capture_output=True, text=True, timeout=15, env=env,
        )
        loss_m = re.search(r"(\d+)% packet loss", ping.stdout)
        if not loss_m:
            return None
        loss = int(loss_m.group(1))
        if loss >= 25:
            return (
                f"⚠ Your wifi is unstable right now ({loss}% packet loss "
                "to the router). The VPN may fail or drop until it "
                "settles -- this isn't necessarily a VPN problem."
            )
    except Exception:
        pass
    return None


def run_connection_test(
    targets: Iterable[Tuple[str, str]], on_line: Callable[[str], None]
) -> None:
    """Pings each (label, ip) target and reports one formatted line per
    result via on_line, as each ping finishes (not batched at the end),
    so the caller can stream them into a live log."""
    on_line("")
    on_line("===== Testing connection =====")
    targets = list(targets)
    if not targets:
        on_line("No session data yet, wait a few seconds and try again.")
    else:
        env = dict(os.environ, LC_ALL="C")
        for label, ip in targets:
            try:
                result = subprocess.run(
                    ["ping", "-c", "3", "-W", "2", ip],
                    capture_output=True, text=True, timeout=15, env=env,
                )
                loss_m = re.search(r"(\d+)% packet loss", result.stdout)
                if loss_m:
                    loss = loss_m.group(1)
                    status = "OK" if loss == "0" else f"{loss}% packet loss"
                else:
                    status = "no response"
                on_line(f"  ping {label} ({ip}): {status}")
            except Exception as e:
                on_line(f"  ping {label} ({ip}): error ({e})")
    on_line("===== Test finished =====")
