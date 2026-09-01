# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""Lightweight network diagnostics: a pre-connect wifi sanity check, and
the post-connect "Test connection" ping check.

Pure functions -- no GTK. These are meant to be called from a background
thread; callers are responsible for marshalling results back onto the
GTK main loop (GLib.idle_add) themselves.
"""
import gzip
import io
import os
import re
import subprocess
import tarfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Callable, Iterable, Optional, Tuple

CERT_FILES = ("ca.crt", "client.crt", "client.pem")

CERT_EXPIRY_WARN_DAYS = 30


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


def check_cert_expiry(cert_path: str, warn_days: int = CERT_EXPIRY_WARN_DAYS) -> Optional[str]:
    """Warns if a certificate is already expired or expiring soon.

    Shells out to the `openssl` CLI rather than pulling in a crypto
    library -- openssl is already a transitive dependency (openvpn links
    against it), so this adds nothing new to install. Returns a short
    fragment like "expires in 12d (2026-09-13)" or "expired 3d ago
    (2026-08-29)", or None if it's not close to expiring (or the check
    itself failed for any reason -- this is a convenience warning, not a
    hard requirement, so failures are silent rather than surfaced)."""
    try:
        result = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", cert_path],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"notAfter=(.+)", result.stdout)
        if not m:
            return None
        # openssl's format looks like "Sep  1 00:00:00 2027 GMT" (note the
        # double space before a single-digit day, and a trailing timezone
        # name strptime can't reliably parse) -- normalize both away. It's
        # always UTC regardless of the abbreviation shown.
        raw = " ".join(m.group(1).split())
        raw = re.sub(r"\s+\S+$", "", raw)
        expiry = datetime.strptime(raw, "%b %d %H:%M:%S %Y")
        days_left = (expiry - datetime.utcnow()).days
    except Exception:
        return None
    if days_left < 0:
        return f"expired {-days_left}d ago ({expiry:%Y-%m-%d})"
    if days_left <= warn_days:
        return f"expires in {days_left}d ({expiry:%Y-%m-%d})"
    return None


def fetch_certificates_to_dir(domain: str, dest_dir: str) -> None:
    """Downloads the Firebox's device certificate bundle and writes
    ca.crt/client.crt/client.pem into dest_dir.

    This is WatchGuard's own device-provisioning endpoint, the same one
    the official Windows/macOS client uses the first time a domain is
    added there (found by MITM-inspecting that traffic against a real
    Firebox). Despite the .wgssl extension suggesting encryption, the
    response is just gzip(tar(ca.crt, client.crt, client.pem, ...)) --
    no crypto at all. It also requires no authentication whatsoever: any
    request that reaches the Firebox's SSLVPN portal gets the same
    device cert bundle back, unauthenticated -- confirmed by fetching it
    with a plain anonymous request and comparing MD5s against what the
    official client downloaded. That's a real device certificate/key
    (mTLS trust for the tunnel), not a login by itself -- the server
    still requires a valid auth-user-pass afterwards -- but it does mean
    anyone who knows a Firebox's hostname can pull this, worth being
    aware of if you administer one.

    Raises OSError/ValueError (via urllib, gzip, or tarfile) on any
    failure -- network error, non-200 response, unexpected format, or a
    bundle missing one of the three files. Caller's problem to catch and
    show the user something sensible.
    """
    url = f"https://{domain}/?action=sslvpn_download&filename=client.wgssl"
    with urllib.request.urlopen(url, timeout=15) as resp:
        raw = resp.read()
    tar_bytes = gzip.decompress(raw)
    os.makedirs(dest_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        names = set(tar.getnames())
        missing = [name for name in CERT_FILES if name not in names]
        if missing:
            raise ValueError(
                f"Bundle from {domain} is missing: {', '.join(missing)}"
            )
        for name in CERT_FILES:
            data = tar.extractfile(name).read()
            path = os.path.join(dest_dir, name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)


def fetch_login_config(domain: str) -> dict:
    """Asks the Firebox what login modes/realms it has configured, via
    the same unauthenticated status check the official client polls
    before showing its login UI.

    Found the same way as fetch_certificates_to_dir: MITM-inspecting a
    real official-client session. This is a SEPARATE endpoint from the
    device cert bundle -- it's what actually answers the "what's the
    SAML auth group?" question we couldn't find in the SAML AuthnRequest
    or in the cert bundle's client.ovpn template. Confirmed with a plain
    anonymous request too, no session/auth needed.

    Returns {"saml_enabled": bool, "saml_idp_name": str, "auth_domains":
    [str, ...]}. saml_idp_name is "" if SAML isn't configured (or the
    Firebox didn't name one -- some setups don't need a group prefix at
    all). auth_domains lists the realm(s) available for direct
    username/password login (e.g. "Firebox-DB").

    Raises OSError/ValueError on any failure (network, non-200, or a
    response that isn't the expected XML shape).
    """
    url = f"https://{domain}/?action=sslvpn_logon&style=fw_logon.xsl&fw_logon_type=status"
    with urllib.request.urlopen(url, timeout=15) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    saml_enabled = (root.findtext("saml_enabled") or "0") == "1"
    saml_idp_name = root.findtext("saml_idp_name") or ""
    auth_domains = [
        el.findtext("name") or ""
        for el in root.findall("./auth-domain-list/auth-domain")
    ]
    return {
        "saml_enabled": saml_enabled,
        "saml_idp_name": saml_idp_name,
        "auth_domains": [d for d in auth_domains if d],
    }


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
