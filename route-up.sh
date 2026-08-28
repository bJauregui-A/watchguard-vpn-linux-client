#!/bin/bash
# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
# Runs AFTER OpenVPN installs the tunnel routes (including any broad CIDR
# the server may push, e.g. 10.0.0.0/8). Without this, that broad route
# would swallow any local network that also uses 10.x addresses (common on
# home/office WiFi routers), breaking connectivity.
#
# Re-installs a more specific route back to the original local network via
# the usual interface -- being more specific, it wins over the tunnel's
# broad route without needing to drop that route (which is still needed to
# reach real internal resources behind the VPN that also use 10.x).

ORIG_ROUTE_FILE=/tmp/.watchguard-vpn-orig-route

if [ -f "$ORIG_ROUTE_FILE" ]; then
    # Line 1: the original default route (used to reinforce the gateway
    # itself). Line 2: the actual pre-VPN route specifically to the
    # Firebox's own IP -- NOT assumed to be "via the default gateway",
    # because it might not be (e.g. the Firebox is already directly
    # reachable on a network the client has no gateway hop to, such as a
    # host-only test network or a Firebox on the same LAN). Using the
    # wrong gateway here would install a route that can't actually reach
    # the Firebox, breaking the tunnel's own TCP connection right after it
    # comes up.
    DEFAULT_LINE=$(sed -n '1p' "$ORIG_ROUTE_FILE")
    SERVER_LINE=$(sed -n '2p' "$ORIG_ROUTE_FILE")
    ORIG_GW=$(echo "$DEFAULT_LINE" | awk '{print $3}')
    ORIG_IF=$(echo "$DEFAULT_LINE" | awk '{print $5}')
    SERVER_GW=$(echo "$SERVER_LINE" | grep -oP '(?<=via )[0-9.]+')
    SERVER_IF=$(echo "$SERVER_LINE" | grep -oP '(?<=dev )\S+')

    if [ -n "$ORIG_GW" ] && [ -n "$ORIG_IF" ]; then
        # The local subnet route is already more specific than the
        # tunnel's broad route and Linux prioritizes it on its own -- the
        # only thing that needs reinforcing is the gateway host itself.
        ip route replace "${ORIG_GW}/32" dev "$ORIG_IF" 2>/dev/null
    fi

    # IMPORTANT: some servers push broad routes that can include their own
    # public IP. Without this, a reconnect attempt (persist-tun keeps
    # these routes installed) tries to reach the server THROUGH the
    # tunnel itself -- an unroutable loop. "trusted_ip" is the server's
    # real IP, provided by OpenVPN to this script. We protect it
    # explicitly via whatever route actually reached it before the VPN
    # came up -- with a gateway hop if there was one, direct on-link if
    # there wasn't.
    if [ -n "$trusted_ip" ] && [ -n "$SERVER_IF" ]; then
        if [ -n "$SERVER_GW" ]; then
            ip route replace "${trusted_ip}/32" via "$SERVER_GW" dev "$SERVER_IF" 2>/dev/null
        else
            ip route replace "${trusted_ip}/32" dev "$SERVER_IF" 2>/dev/null
        fi
    fi
    # The state file is NOT removed here: route-up fires again on every
    # internal OpenVPN reconnect (persist-tun), and needs to be able to
    # reinforce these routes each time. vpn_routes.py cleans it up when
    # the connection is fully torn down.
fi
