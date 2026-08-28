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

## Setup

```bash
git clone <this-repo>
cd linux-client
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
- `vpn_network.py` — wifi health check and the "Test connection" ping
  check.
- `vpn_profiles.py` — profile storage (which domains are configured).

All five must stay together in the same directory (plain sibling
imports, no package/install step). Application state (profiles,
per-domain certificates, generated OpenVPN configs) is kept under
`~/.local/share/watchguard-vpn-linux-client/`, not inside the repo.

### Adding a server (domain)

From the app: **"+ Add new domain"** and fill in:

- **Domain**: the server hostname (e.g. `vpn.example.com`).
- **Login mode**: SAML (SSO, opens an embedded browser) or
  username/password (submitted directly).
- **SAML auth group** *(optional)*: the SAML IdP/auth-server name
  configured on that Firebox, prefixed to the username for OpenVPN auth
  (`<group>\<user>`). Required only if that server's SAML config uses one;
  ask the server administrator, or inspect the official client's config
  for that domain if you have access to one.
- **Certificate folder**: a folder containing `ca.crt`, `client.crt` and
  `client.pem` for that server (see below).

### Where the certificates come from

WatchGuard doesn't expose a public way to download `ca.crt` / `client.crt`
/ `client.pem` directly. The official Windows/macOS client fetches and
decrypts them internally from an encrypted blob (`client.wgssl`) served by
the Firebox when a domain is added there. This client does not automate
that step — you need to obtain the three files once from a real install of
the official client for that domain:

- **Windows**: `%AppData%\WatchGuard\Mobile VPN\`
- **macOS**: equivalent path under the official client's profile data

These are certificates issued for the Firebox device itself, not
per-user/per-session — they don't need to be re-extracted on every login,
only once per server (until that server's certificate is rotated).

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
  alone.
- A "Test connection" action pings the tunnel gateway and any DNS servers
  pushed by the server, to confirm the tunnel is actually passing traffic.

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
certificates come from" above).

## License

GPL-3.0-only. See [`LICENSE`](LICENSE). Copyright (C) 2026
[bjauregui-A](https://github.com/bjauregui-A). You're free to use,
modify, and redistribute this, but redistributed copies (modified or
not) must stay under the same license and come with their source.

## Development

Built with [Claude Code](https://claude.com/claude-code) (Anthropic's AI
coding assistant), through iterative development and testing against a
real WatchGuard Firebox and a disposable local test lab.
