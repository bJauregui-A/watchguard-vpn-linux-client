# WatchGuard SSL VPN client for Linux

Native, unofficial GTK client for WatchGuard **Mobile VPN with SSL**
(OpenVPN-based), supporting SAML SSO login or direct username/password
login. WatchGuard only ships official clients for Windows and macOS; this
is a from-scratch Linux implementation.

Multi-domain: you can add and switch between multiple WatchGuard
("Firebox") servers, each with its own certificates and login mode.

## Requirements

- Linux with `openvpn` and `polkit` (`pkexec`)
- Python 3 + PyGObject (GTK3) + WebKit2Gtk 4.1

On Arch Linux:

```bash
sudo pacman -S openvpn polkit python-gobject gtk3 webkit2gtk-4.1
```

On Debian/Ubuntu:

```bash
sudo apt install openvpn policykit-1 python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

Tested on **Arch Linux** and **Linux Mint**.

## Setup

```bash
git clone https://github.com/bJauregui-A/watchguard-vpn-linux-client.git
cd watchguard-vpn-linux-client
python3 watchguard-vpn-gui.py
```

No build/install step is required — the app is plain Python source, run
directly with the system interpreter. It's split into a few files by
responsibility rather than one monolithic script:

- `watchguard-vpn-gui.py` — UI layer (GTK + the embedded WebKit2 browser
  for SAML). Entry point.
- `vpn_process.py` — OpenVPN subprocess lifecycle (spawn via `pkexec`,
  stream/parse its log, disconnect). Decoupled from the UI via callbacks.
- `vpn_routes.py` — route capture/cleanup around the tunnel, and
  installs `route-up.sh`/`route-down.sh` into the app's data directory.
- `vpn_network.py` — wifi health check, the "Test connection" ping
  check, and fetching certificates/SAML auth group from the Firebox.
- `vpn_profiles.py` — profile storage (which domains are configured).

All five must stay together in the same directory (plain sibling
imports, no package/install step). Application state (profiles,
per-domain certificates, generated OpenVPN configs) is kept under
`~/.local/share/watchguard-vpn-linux-client/`, not inside the repo.

### Adding a server (domain)

From the app: **"+ Add new domain"**, enter the **domain** (e.g.
`vpn.example.com`) and click **"Fetch automatically"** — this pulls the
certificates and detects the login mode/SAML auth group in one step (see
below for how). Review what it filled in, then **Save**:

- **Login mode**: SAML (SSO, opens an embedded browser) or
  username/password (submitted directly) — pre-selected from what the
  Firebox reports, but you can override it.
- **SAML auth group** *(optional)*: the SAML IdP/auth-server name
  configured on that Firebox, prefixed to the username for OpenVPN auth
  (`<group>\<user>`). Auto-filled when the Firebox reports one; edit it
  yourself if that didn't happen, got it wrong, or needs overriding.
  Leaving it blank when the server actually needs one fails with
  `AUTH_FAILED` right after a successful SAML login and valid certs,
  which can look like a certificate or routing problem but isn't.
- **Certificate folder**: only needed if "Fetch automatically" didn't
  work — a folder containing `ca.crt`, `client.crt` and `client.pem` for
  that server (see below for the manual fallback).

An existing profile can be edited later too (**Edit** button on its row),
to change any of the above without deleting and re-adding it.

### Where the certificates and SAML group come from

Both come straight from the Firebox itself, via two unauthenticated
endpoints the official Windows/macOS client also uses the first time a
domain is added there (found by inspecting that traffic with a
MITM proxy against a real Firebox):

- `GET https://<domain>/?action=sslvpn_download&filename=client.wgssl`
  returns the device certificate bundle. Despite the name/extension
  suggesting encryption, it's just `gzip(tar(ca.crt, client.crt,
  client.pem, ...))` — no crypto involved.
- `GET https://<domain>/?action=sslvpn_logon&style=fw_logon.xsl&fw_logon_type=status`
  returns a small XML status document (`saml_enabled`, `saml_idp_name`,
  `auth-domain-list`) describing the configured login mode(s) — this is
  where the SAML auth group actually comes from.

Neither endpoint requires a session, cookie, or credential of any kind —
confirmed by fetching both anonymously and comparing byte-for-byte
against what the official client received. Worth knowing if you
administer a Firebox: anyone who knows its SSLVPN portal hostname can
pull a valid device certificate + private key this way. That's mTLS
trust for the tunnel, not a full login by itself (a valid `auth-user-pass`
is still required afterwards), but it is unauthenticated access to real
key material.

The certificates are issued for the Firebox device itself, not
per-user/per-session — they don't need to be re-fetched on every login,
only once per server (until that server's certificate is rotated).

**Manual fallback**, if automatic fetch doesn't work for some reason
(e.g. that endpoint is firewalled off, or the Firebox is old enough not
to have it): obtain the three cert files once from a real install of the
official client for that domain —

- **Windows**: `%AppData%\WatchGuard\Mobile VPN\`
- **macOS**: equivalent path under the official client's profile data

— and point "Choose folder..." at them instead. The SAML auth group
would then need asking the server administrator.

## How it works

- Login: SAML is done via an embedded WebKit2 browser view, ending on a
  redirect of the form
  `https://<domain>/sslvpn_success.shtml?result=success&user=...&token=...`.
  That one-time `token` becomes the OpenVPN password; the username is
  `<saml_auth_group>\<user>` (or just `<user>` if no group is configured).
  Username/password mode skips the browser and submits credentials
  directly as OpenVPN `auth-user-pass`.
- Profiles are stored in
  `~/.local/share/watchguard-vpn-linux-client/profiles.json`, with each
  domain's certificates under `profiles/<domain>/`.
- The actual `.ovpn` config is generated per connection from
  `client.ovpn.template` (placeholders: `{{DOMAIN}}`, `{{CA}}`, `{{CERT}}`,
  `{{KEY}}`, `{{VERIFY_X509_NAME}}`, `{{ROUTE_UP}}`, `{{ROUTE_DOWN}}`) —
  there is no static `.ovpn` file in the repo.
- Before connecting, the app records the current default route and the
  actual pre-VPN route to the server's resolved IP (which may or may not
  go through a gateway, depending on the network). `route-up.sh` then runs
  after OpenVPN installs the tunnel routes and re-installs both as
  specific host routes, so that a broad route pushed by the server can't
  swallow the local network or break reconnection to the server itself.
  `route-down.sh` (run via OpenVPN's `route-pre-down` hook) removes the
  server-IP host route again on a clean disconnect; both scripts are
  copied into the app's data directory on every launch, so they always
  match the version in the repo.
- Connection status is read live from the OpenVPN log stream (connected /
  reconnecting / error), rather than assumed from process exit status
  alone. On failure, the log tail is checked against a few known OpenVPN
  failure patterns (`AUTH_FAILED`, TLS handshake/certificate errors, DNS
  resolution failures, connection refused) to show a specific hint
  instead of just the exit code, when one is recognized.
- A "Test connection" action pings the tunnel gateway and any DNS servers
  pushed by the server, to confirm the tunnel is actually passing traffic.
- The domain list shows a warning next to any profile whose certificate
  is already expired or expires within 30 days (checked via the `openssl`
  CLI, already a transitive dependency via `openvpn`).

## Technical notes / known limitations

- **Split-tunnel only.** Full-tunnel (`redirect-gateway def1`, routing all
  traffic through the VPN like the official client does) was tried and
  reverted: some Firebox configurations don't forward general internet
  traffic for VPN clients, so full-tunnel just breaks other connectivity
  without providing working general browsing. Only routes explicitly
  pushed by the server go through the tunnel.
- **Transport is forced to TCP** (`proto tcp-client`), since WatchGuard
  SSLVPN only accepts connections on TCP 443 (no UDP option). This means
  any TCP traffic carried inside the tunnel (e.g. SSH) is TCP-over-TCP, a
  known pattern where the two layers' retransmission timers can interact
  and cause perceptible interactive lag even when raw throughput is fine.
  Mitigated with `tcp-nodelay` in the generated config (disables Nagle's
  algorithm on the tunnel's own TCP socket).
- No persistent session — each connection requires a fresh login (this
  matches the official client's behavior, not a limitation specific to
  this implementation).
- `pkexec` does not reliably forward signals to the `openvpn` process it
  spawns, so disconnecting explicitly kills it by command line (`pkexec
  pkill -TERM -f "openvpn --config <path>"`) in addition to signaling the
  subprocess handle, escalating to `SIGKILL` if it's still alive a few
  seconds later. If disconnecting is still stuck after that (e.g. the
  polkit prompt itself was never answered), the action button becomes a
  "Force back to start" failsafe that resets local state and returns to
  the profile list without waiting on the subprocess any further.

## Certificate/key handling

`ca.crt`, `client.crt`, and especially `client.pem` (a private key) are
real access credentials for whatever Firebox they belong to. Never commit
them to a repository — this project's `.gitignore` excludes common
certificate/key filenames as a safety net, but they are not meant to live
inside the project directory at all in normal use (see "Where the
certificates and SAML group come from" above).

## Trademark

WatchGuard® and Firebox® are registered trademarks of WatchGuard
Technologies, Inc. This is an independent, unofficial project, not
affiliated with, endorsed by, or sponsored by WatchGuard. The name is
used only to describe what this client is compatible with.

## License

GPL-3.0-only. See [`LICENSE`](LICENSE). Copyright (C) 2026
[bjauregui-A](https://github.com/bjauregui-A). You're free to use,
modify, and redistribute this, but redistributed copies (modified or
not) must stay under the same license and come with their source.

## Development

Built with [Claude Code](https://claude.com/claude-code) (Anthropic's AI
coding assistant), through iterative development and testing against a
real WatchGuard Firebox and a disposable local test lab.
