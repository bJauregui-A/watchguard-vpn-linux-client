# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
"""OpenVPN subprocess lifecycle: renders the .ovpn config for a profile,
spawns it via pkexec, streams its log, tracks connection state, and
handles disconnecting (including working around pkexec's unreliable
signal delivery).

Decoupled from the UI via callbacks -- this module never touches a GTK
widget. It does use GLib/Gio (for Gio.Subprocess and to schedule its
callbacks back onto the main loop with GLib.idle_add, since the actual
work runs on a background thread), but nothing from Gtk or WebKit2.
"""
import os
import re
import subprocess
import threading

from gi.repository import GLib, Gio

import vpn_profiles
import vpn_routes

BASE = os.path.dirname(os.path.abspath(__file__))
OVPN_TEMPLATE = os.path.join(BASE, "client.ovpn.template")

AUTH_FILE = "/tmp/.watchguard-vpn-auth"
LOG_FILE = "/tmp/watchguard-vpn-gui-openvpn.log"
ACTIVE_OVPN_FILE = "/tmp/.watchguard-vpn-active.ovpn"


class VpnProcess:
    """Owns at most one OpenVPN subprocess at a time.

    Callbacks (all invoked on the GTK main loop, safe to touch widgets
    from):
      on_log_line(line: str)
      on_status(state: str, extra: str) -- state is one of "connected",
        "reconnecting", "error", or "detail" (a still-connecting progress
        line; only `extra` is meaningful).
      on_exited(session_id: int) -- fires once the openvpn process has
        actually exited. Compare session_id against .session_id before
        acting on it: a stale callback from an abandoned/force-reset
        session must be ignored.
    """

    def __init__(self, on_log_line, on_status, on_exited):
        self._on_log_line = on_log_line
        self._on_status = on_status
        self._on_exited = on_exited

        self.proc = None  # openvpn's Gio.Subprocess (via pkexec)
        self.active_profile = None
        self.active_ovpn_path = None
        self.tunnel_gateway = None
        self.tunnel_dns = []
        self._session_id = 0

    @property
    def session_id(self) -> int:
        return self._session_id

    def is_running(self) -> bool:
        return self.proc is not None

    def test_targets(self):
        """(label, ip) pairs worth pinging to confirm the tunnel is
        actually passing traffic -- built from whatever the server
        pushed in its PUSH_REPLY for the current session."""
        targets = []
        if self.tunnel_gateway:
            targets.append(("tunnel gateway", self.tunnel_gateway))
        for i, dns in enumerate(self.tunnel_dns):
            targets.append((f"pushed DNS #{i + 1}", dns))
        return targets

    def _render_config(self, profile: dict) -> str:
        cert_dir = vpn_profiles.profile_cert_dir(profile["domain"])
        verify = profile.get("verify_x509_name") or ""
        verify_line = f'verify-x509-name "{verify}"' if verify else ""
        with open(OVPN_TEMPLATE) as f:
            template = f.read()
        return (
            template
            .replace("{{DOMAIN}}", profile["domain"])
            .replace("{{CA}}", os.path.join(cert_dir, "ca.crt"))
            .replace("{{CERT}}", os.path.join(cert_dir, "client.crt"))
            .replace("{{KEY}}", os.path.join(cert_dir, "client.pem"))
            .replace("{{VERIFY_X509_NAME}}", verify_line)
            .replace("{{ROUTE_UP}}", vpn_routes.ROUTE_UP_SCRIPT)
            .replace("{{ROUTE_DOWN}}", vpn_routes.ROUTE_DOWN_SCRIPT)
        )

    def start(self, profile: dict, username: str, secret: str) -> None:
        self.active_profile = profile
        self.tunnel_gateway = None
        self.tunnel_dns = []

        fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"{username}\n{secret}\n")

        self._session_id += 1
        threading.Thread(target=self._run, args=(self._session_id,), daemon=True).start()

    def _run(self, session_id: int) -> None:
        vpn_routes.save_pre_vpn_routes(self.active_profile["domain"])

        config_text = self._render_config(self.active_profile)
        fd = os.open(ACTIVE_OVPN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(config_text)
        self.active_ovpn_path = ACTIVE_OVPN_FILE

        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
        )
        launcher.set_cwd(BASE)
        try:
            self.proc = launcher.spawnv(
                ["pkexec", "openvpn", "--config", ACTIVE_OVPN_FILE, "--auth-user-pass", AUTH_FILE]
            )
        except GLib.Error as e:
            GLib.idle_add(self._on_status, "error", f"Could not start openvpn: {e}")
            return

        stdout = self.proc.get_stdout_pipe()
        stream = Gio.DataInputStream.new(stdout)
        connected = False
        with open(LOG_FILE, "w") as logf:
            while True:
                line, _ = stream.read_line_utf8()
                if line is None:
                    break
                logf.write(line + "\n")
                logf.flush()
                GLib.idle_add(self._on_log_line, line)
                if "PUSH_REPLY" in line:
                    gw_m = re.search(r"route-gateway (\d+\.\d+\.\d+\.\d+)", line)
                    if gw_m:
                        self.tunnel_gateway = gw_m.group(1)
                    self.tunnel_dns = re.findall(r"dhcp-option DNS (\d+\.\d+\.\d+\.\d+)", line)
                if (
                    "Initialization Sequence Completed" in line
                    or "Data Channel: cipher" in line
                ) and not connected:
                    connected = True
                    GLib.idle_add(self._on_status, "connected", "")
                elif connected and (
                    "Restart pause" in line
                    or "SIGUSR1" in line
                ):
                    # NOTE: do NOT include "SIGTERM" here -- a deliberate
                    # disconnect also logs "SIGTERM[hard,] received,
                    # process exiting", and that is NOT a retry, it's the
                    # normal final shutdown. Confusing the two made every
                    # manual disconnect end up showing "Connection error".
                    #
                    # The connection dropped and openvpn is retrying on
                    # its own (persist-tun keeps the interface "up", but
                    # there's no real tunnel until the handshake completes
                    # again). Without this the UI would keep showing
                    # "Connected" while the tunnel is actually broken
                    # underneath.
                    connected = False
                    GLib.idle_add(self._on_status, "reconnecting", "")
                if not connected:
                    GLib.idle_add(self._on_status, "detail", line[-120:])

        self.proc.wait()
        if not connected:
            exit_code = self.proc.get_exit_status()
            GLib.idle_add(
                self._on_status,
                "error",
                f"openvpn exited (code {exit_code}). Full log: {LOG_FILE}",
            )
        GLib.idle_add(self._on_exited, session_id)

    def _route_cleanup_command(self) -> str:
        if not self.active_profile:
            return ""
        return vpn_routes.cleanup_command(self.active_profile["domain"])

    def kill(self) -> None:
        # pkexec doesn't always reliably forward the signal to its actual
        # child (openvpn can keep running as root even after the app
        # closes) -- kill it explicitly by its exact command line, in
        # addition to signaling the subprocess normally.
        if self.proc is not None:
            try:
                self.proc.send_signal(15)
            except GLib.Error:
                pass
        if self.active_ovpn_path:
            path = self.active_ovpn_path
            cleanup = self._route_cleanup_command()
            script = f'pkill -TERM -f "openvpn --config {path}"'
            if cleanup:
                script += f"; {cleanup}"
            try:
                subprocess.run(["pkexec", "bash", "-c", script], timeout=10)
            except Exception:
                pass
            # If SIGTERM still doesn't actually kill it (pkexec signal
            # delivery is unreliable, see above), escalate to SIGKILL so
            # we don't hang forever -- the background read loop in _run
            # blocks on openvpn's stdout pipe until the process is well
            # and truly dead and closes it.
            GLib.timeout_add_seconds(4, self._escalate_kill, path)

    def _escalate_kill(self, path: str) -> bool:
        if self.proc is not None and self.active_ovpn_path == path:
            try:
                subprocess.run(
                    ["pkexec", "pkill", "-KILL", "-f", f"openvpn --config {path}"],
                    timeout=10,
                )
            except Exception:
                pass
        return GLib.SOURCE_REMOVE

    def clear_after_exit(self) -> None:
        """Call once on_exited has actually fired for the current
        session: clears the transient per-connection files and the
        process handle."""
        if os.path.exists(AUTH_FILE):
            os.remove(AUTH_FILE)
        vpn_routes.clear_saved_routes()
        self.proc = None

    def force_reset(self) -> None:
        """Give up waiting on a stuck disconnect. openvpn may still be
        alive out there somewhere (both kill attempts failed) -- this
        doesn't confirm it's actually gone, it just stops the UI from
        waiting on it. Bumping the session id makes an eventual late
        on_exited call from the abandoned background thread get ignored
        instead of clobbering whatever runs next."""
        self._session_id += 1
        self.proc = None
        self.active_profile = None
        if os.path.exists(AUTH_FILE):
            os.remove(AUTH_FILE)
        vpn_routes.clear_saved_routes()
