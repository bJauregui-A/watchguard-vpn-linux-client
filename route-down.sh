#!/bin/bash
# Copyright (C) 2026 bjauregui-A
# SPDX-License-Identifier: GPL-3.0-only
# Companion to route-up.sh: removes the host route it adds for the VPN
# server's real IP ($trusted_ip), when the tunnel is torn down. Without
# this, that route is left behind forever after every disconnect --
# usually harmless in practice (it's still a correct way to reach that
# server directly, without the VPN), but it silently shadows any other,
# more specific route to that same IP that the system should be using
# instead once the VPN session is over.

ORIG_ROUTE_FILE=/tmp/.watchguard-vpn-orig-route

if [ -f "$ORIG_ROUTE_FILE" ]; then
    # Line 2 holds the pre-VPN route specifically to the Firebox's IP --
    # see route-up.sh for why it's not assumed to be "via the default
    # gateway". Must match however route-up.sh actually installed it (with
    # or without a gateway hop) so the delete succeeds.
    SERVER_LINE=$(sed -n '2p' "$ORIG_ROUTE_FILE")
    SERVER_GW=$(echo "$SERVER_LINE" | grep -oP '(?<=via )[0-9.]+')
    SERVER_IF=$(echo "$SERVER_LINE" | grep -oP '(?<=dev )\S+')

    if [ -n "$trusted_ip" ] && [ -n "$SERVER_IF" ]; then
        if [ -n "$SERVER_GW" ]; then
            ip route del "${trusted_ip}/32" via "$SERVER_GW" dev "$SERVER_IF" 2>/dev/null
        else
            ip route del "${trusted_ip}/32" dev "$SERVER_IF" 2>/dev/null
        fi
    fi
    # The default gateway's /32 route (line 1) stays -- it's just the
    # normal gateway restated more specifically, harmless to leave, and
    # route-up.sh will just `replace` it again next time regardless.
fi
